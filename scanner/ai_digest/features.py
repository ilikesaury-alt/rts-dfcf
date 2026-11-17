"""候选票**本地特征**预加载（2026-11-11）。

## 为什么有这个模块

用户的疑问很直接：`ai_digest` 里 5 个工具是**纯本地反查**（`get_kline` / `get_concepts` /
`get_announcements` / `get_stock_record` / `get_sector_heatmap`），既然数据都在本地库里，
为什么不直接把信息带进提示词、让模型少调几次工具？

但**全量预加载是错的**。实测（30 只候选）：

| 内容 | 字符 |
|---|---|
| 面板原文 | 2663 |
| 10 日原始 K 线 | 3414 |
| 概念全量（每票 ~14 个标签） | 4749 |
| 公告 3 条 | 1990 |

全塞进去 ≈ 12800 字符（≈8000 tokens），而工具 schema 本身只有 2644 字符。更要紧的是
`agent.run_research` **每轮都重发完整 messages**（无 `cache_control`）⇒ 3 轮就是 3 倍，
省下的往返可能被更大的上下文吃掉。

而模型真正需要的是**判断依据**，不是数据全集：「5 日累计 +24.47%、距 20 日高点 -2.1%、
今日量是 5 日均量的 2.4 倍」比 10 根原始 K 线有用得多，字符还少一个数量级。

故这里只算**结论化特征**（每票一行、约 50 字符）：

- `5日` / `10日` 累计涨幅 —— 位置（是不是已经涨过了）
- `距20日高` —— 上方还有多少空间 / 是不是在冲前高
- `量能` —— 今日量 / 前 5 日均量（放量确认）
- `公告` —— 最近 2 条标题（雷点/催化）
- `主力净占比` —— 主循环每轮已写 `market_extra_cache`，零新增请求
- `系统史` —— `recommendations` 汇总成「出现次数 + 最近类别/次日」，不返回 15 行原文

## 纪律

1. **本地优先**：只读 SQLite / 本地缓存，零新增网络；K 线 "今日"取 `daily_kline` 的
   `MAX(date)`，资金流快照取 `market_extra_cache` 当日行（主循环已落库）。
2. **不猜**：某票缺数据就**明说缺**（`5日 —`），不填 0、不留空 —— 模型会把空缺读成"没有异动"。
3. **可失败**：任何异常退回"无特征"并让调用方照常构造提示词（预加载是增强，不是前置条件）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date

from scanner.db.queries import get_market_extra_cache
from scanner.utils import EXTERNAL_FAILURES

__all__ = [
    "SymbolFeatures",
    "build_features",
    "features_from_bars",
    "render_feature_block",
]

#: 公告最多带几条。实测 30 只票：3 条 → 3356 字符，2 条 → 2702 字符。
#: 取 2：第 3 条基本是「董事会决议」这类无信息量的程序性公告，白占上下文。
_ANN_LIMIT = 2

#: 单条公告标题截断长度。实测 42→28 字只省 37 字符（标题本来就短），
#: 但 28 字足够分辨「激励计划 / 减持 / 业绩预告」，故取 28 保守截断。
_ANN_TITLE_CHARS = 28

#: 某票最新 K 线距今超过这么多**自然日** ⇒ 判定为陈旧，该票特征全部置空。
#:
#: 为什么是自然日而不是交易日：这里只需要挡住「明显过期」（实测有票落后 3.5 个月），
#: 不需要精确到「缺了 1 个交易日」。取 7 天可覆盖周末 + 一两天节假日的正常跳空，
#: 同时能挡住所有真正的陈旧数据。用交易日历会把周末误判成陈旧。
_MAX_STALE_DAYS = 7

#: 预加载补取日线时回溯多少天（够算 10 日累计 + 20 日高点）。
_BACKFILL_DAYS = 25


@dataclass
class SymbolFeatures:
    """单票的本地特征（全部可缺省 —— 缺就渲染成 `—`）。"""

    symbol: str
    pct_5d: float | None = None
    pct_10d: float | None = None
    high_20d_gap: float | None = None  # 现价距 20 日最高价的百分比（负=还在下方）
    vol_ratio_5d: float | None = None  # 今日成交量 / 前 5 日均量（**仅已定稿时**才有值）
    announcements: list[str] = field(default_factory=list)
    #: 最新 bar 是盘中部分 bar（`finalized=0`）⇒ 成交量不可比、量能字段已被置空。
    intraday_partial: bool = False
    #: 该票最新 K 线距今的自然日数；超过 `_MAX_STALE_DAYS` 时其余字段全部为空。
    stale_days: int = 0
    #: 特征由**联网补取的实时日线**算出（不是本地库）—— 说明本地曾陈旧、已补齐。
    backfilled: bool = False
    #: 当日主力净占比（来自 `market_extra_cache`，主循环已写，零新增请求）。
    fund_pct: float | None = None
    #: 系统出现史压缩成一行（次数 + 最近类别/次日），避免模型逐票调 `get_stock_record`。
    history: str | None = None

    @property
    def is_stale(self) -> bool:
        """数据是否已陈旧到不可用（整组特征已置空）。"""
        return self.stale_days > 0


def _f(v: object) -> float | None:
    try:
        return None if v is None else float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _pct_text(v: float | None, *, signed: bool = True) -> str:
    if v is None:
        return "—"
    return f"{v:+.1f}%" if signed else f"{v:.1f}%"


def _bare_symbol(symbol: str) -> str:
    """`SZ300001` → `300001` —— `market_extra_cache` 存的是裸代码。"""
    return symbol[2:] if len(symbol) == 8 and symbol[:2] in ("SH", "SZ", "BJ") else symbol


def _system_history(conn: sqlite3.Connection, symbols: list[str]) -> dict[str, str]:
    """一次 IN 查询把 `recommendations` 压成「次数 + 最近类别/次日」一行/票。

    不返回逐条明细：实测模型为 8 只票调 3~8 次 `get_stock_record`，而它真正用的只有
    「是不是老面孔 + 最近一次表现」。失败/表缺失 fail-open（返回空 dict）。
    """
    if not symbols:
        return {}
    ph = ",".join("?" * len(symbols))
    try:
        rows = conn.execute(
            f"SELECT symbol, date, category, next_day_pct FROM recommendations "  # noqa: S608
            f"WHERE symbol IN ({ph}) ORDER BY symbol, date DESC",  # noqa: S608
            symbols,
        ).fetchall()
    except sqlite3.Error:
        return {}
    grouped: dict[str, list[tuple]] = {}
    for sym, day, category, ndp in rows:
        grouped.setdefault(str(sym), []).append((day, category, ndp))
    out: dict[str, str] = {}
    for sym, items in grouped.items():
        day, category, ndp = items[0]
        out[sym] = (
            f"系统史 {len(items)}次 最近{str(day)[5:]}/{category or '—'}/次日{_pct_text(_f(ndp))}"
        )
    return out


def _fund_flow_pct(conn: sqlite3.Connection, symbols: list[str]) -> dict[str, float]:
    """当日主力净占比（`market_extra_cache`）—— 主循环已在写，零新增外呼。"""
    if not symbols:
        return {}
    # 生产库里个股用带前缀键（SZ300001），ETF 用裸键（159915）——两种都试。
    bare = [_bare_symbol(s) for s in symbols]
    keys = list(dict.fromkeys([*symbols, *bare]))
    cached = get_market_extra_cache(conn, keys, "fund_flow")  # 内部已兜 sqlite3.Error
    reverse = dict(zip(bare, symbols, strict=True))
    out: dict[str, float] = {}
    for key, payload in cached.items():
        pct = _f(payload.get("main_pct"))
        sym = key if key in symbols else reverse.get(key)
        if sym is not None and pct is not None:
            out[sym] = pct
    return out


def _backfill_missing(feats: dict[str, SymbolFeatures], symbols: list[str]) -> None:
    """对陈旧 / 完全没有本地 bar 的票联网补日线，用真值重算特征。

    ⚠ 只改**本次提示词用的特征对象**，不写 `daily_kline`：写库是单独的 data-only 回填动作，
      分析期不碰库，避免把未定稿盘中 bar 当收盘值固化。补不到就保持置空（fail-open）。
    """
    missing = [s for s in symbols if (f := feats.get(s)) and (f.is_stale or f.pct_5d is None)]
    if not missing:
        return
    from scanner.api import fetch_kline, make_session
    from scanner.config import now_beijing

    try:
        session = make_session()
    except EXTERNAL_FAILURES:
        return
    today = now_beijing().date().isoformat()
    try:
        for sym in missing:
            try:
                bars = fetch_kline(session, sym, days=_BACKFILL_DAYS) or []
            except EXTERNAL_FAILURES:
                continue
            ordered = sorted(bars, key=lambda x: str(x["date"]))
            rows: list[tuple[str, float | None, float | None, float | None]] = [
                (str(b["date"]), b["close"], b["high"], b["volume"]) for b in ordered
            ]
            if not rows:
                continue
            # 远程最后一根可能正是今天盘中那根 ⇒ 量能置 None（与本地 finalized 同纪律）。
            fresh = features_from_bars(
                sym, rows, newest_finalized=rows[-1][0] != today, backfilled=True
            )
            fresh.announcements = list(feats[sym].announcements)
            feats[sym] = fresh
    finally:
        session.close()


def build_features(
    conn: sqlite3.Connection,
    symbols: list[str],
    *,
    ann_limit: int = _ANN_LIMIT,
    backfill: bool = False,
) -> dict[str, SymbolFeatures]:
    """按票算本地特征。`symbols` 为空或查询失败时返回空 dict（调用方照常继续）。

    `backfill=True` 时对陈旧 / 无本地 bar 的票联网补一次日线（仅研究入口用，
    `--materials` 与测试默认 False ⇒ 零出网）。

    "今日" 取 `daily_kline` 的 `MAX(date)` —— **不用 `date.today()`**：盘前/周末跑分析时
    今天还没有 K 线，按自然日取会全表 miss，把「没数据」渲染成「没涨」。

    ## ⚠ 按票校验 K 线新鲜度（2026-11-12 修）

    `daily_kline` **只保留被扫描器选中过的票**的 K 线（实测：11-12 盘中仅 8 只有当日行，
    而全表有 1149 个不同代码）。故 `MAX(date)`（全表最新日）**不能当作每只票的"今天"**：

    实测面板 14 只票的最新 K 线日期：

    ```
    SZ300209: 11-12（今天）      SZ300781: 11-11
    SZ300063: 11-10              SZ300236: 11-06
    SZ301520: 11-02              SZ300030: 11-03
    SZ301392: 10-29              SZ301360/SZ301516: 09-29
    SZ300612: 07-27（3.5 个月前）  4 只：完全没有
    ```

    若不校验，会把「3.5 个月前算出的 5日累计」当成当前特征喂给模型 —— **比缺数据更坏**：
    缺数据模型会说"证据不足"，陈旧数据它却会当成事实据此排序。

    故：某票最新 bar 距今超过 `_MAX_STALE_DAYS` 个自然日 ⇒ 该票**整组特征置空**
    （渲染成 `—`），并在块首说明哪些票因数据陈旧而跳过。

    ## ⚠ 量能只在「已定稿」的 bar 上算（2026-11-12 修）

    盘中 `daily_kline` 的今日那根是**未定稿的部分 bar**（`finalized=0`，只累积到当前时刻），
    拿它除以**完整交易日**的前 5 日均量会**系统性低估**：

    实测（11-12 10:29 盘中）SZ300806 斯迪克：当日已成交 2425 万手（才 1 小时），
    前 5 日均量 ~3400 万 ⇒ 算出 `0.8x 萎缩`；而东财量比给的 3.31（**显著放量**）。
    两个数说的是相反的事，模型于是写出「量比 3.31、量能 0.8x 萎缩」这种自相矛盾的句子
    —— 假信号会让它**错误否决一只放量票**。

    故：最新 bar 未定稿时，`vol_ratio_5d` 置 None（渲染成 `—`），由模型改用
    面板/东财的**量比**（那个按时段归一化过，是同一个量的正确算法）。
    百分比类字段（5日/10日累计、距20日高）**不受影响** —— 它们用的是 `close`/`high`，
    盘中拿到的就是**当前价**，那正是想要的。
    """
    out: dict[str, SymbolFeatures] = {}
    syms = [s for s in symbols if s]
    if not syms:
        return out

    ph = ",".join("?" * len(syms))
    try:
        latest = conn.execute("SELECT MAX(date) FROM daily_kline").fetchone()
        today = latest[0] if latest else None
        if not today:
            return out

        # 一次取够 21 根（算 10 日累计 + 20 日高点），按票分组。
        # ⚠ `finalized` 必须取：盘中今日那根是**部分 bar**，量能不能用（见 docstring）。
        sql = (  # noqa: S608 - 占位符由 ",".join("?" * n) 生成，值经参数化传入
            "SELECT symbol, date, close, high, volume, percent, finalized "  # noqa: S608
            f"FROM daily_kline WHERE symbol IN ({ph}) AND date <= ? "  # noqa: S608
            "ORDER BY symbol, date DESC"
        )
        rows = conn.execute(sql, (*syms, today)).fetchall()
    except sqlite3.Error:
        return out

    grouped: dict[str, list[tuple]] = {}
    for r in rows:
        grouped.setdefault(str(r[0]), []).append(r)

    for sym in syms:
        bars = grouped.get(sym) or []
        feat = SymbolFeatures(symbol=sym)

        # 陈旧校验：该票自己的最新 bar 距今太远 ⇒ 整组特征置空（宁缺勿错）。
        # ⚠ 基准用**全表最新日**（today）而不是 `date.today()`：盘前/周末跑时
        #   全表最新日就是上一个交易日，用它算差值才是「落后了几个交易日」。
        if bars and today:
            newest = str(bars[0][1])
            try:
                gap_days = (
                    date.fromisoformat(today) - date.fromisoformat(newest)
                ).days
            except ValueError:
                gap_days = 0
            if gap_days > _MAX_STALE_DAYS:
                feat.stale_days = gap_days
                out[sym] = feat
                continue

        if bars:
            # `bars` 是「新→旧」；公式本体在 `features_from_bars`（**单一份**），
            # 补取路径也用同一个函数 —— 两处各写一份公式是口径漂移的经典入口。
            norm = [(b[1], b[2], b[3], b[4]) for b in bars]
            newest_finalized = bars[0][6] if len(bars[0]) > 6 else 1
            computed = features_from_bars(
                sym, list(reversed(norm)), newest_finalized=bool(newest_finalized)
            )
            computed.announcements = feat.announcements
            feat = computed
        out[sym] = feat

    # 公告：一次 IN 查询，每票取最近 N 条
    if ann_limit > 0:
        try:
            asql = (  # noqa: S608 - 占位符由 ",".join("?" * n) 生成，值经参数化传入
                "SELECT symbol, notice_date, title FROM announcement_cache "  # noqa: S608
                f"WHERE symbol IN ({ph}) ORDER BY symbol, notice_date DESC"  # noqa: S608
            )
            arows = conn.execute(asql, syms).fetchall()
        except sqlite3.Error:
            arows = []
        for r in arows:
            sym = str(r[0])
            target = out.get(sym)
            if target is None or len(target.announcements) >= ann_limit:
                continue
            title = str(r[2] or "").strip()
            if not title:
                continue
            # 去掉「公司简称:」前缀（面板已经有名称了，重复占字符）
            if ":" in title[:12]:
                title = title.split(":", 1)[1].strip()
            if len(title) > _ANN_TITLE_CHARS:
                title = title[:_ANN_TITLE_CHARS] + "…"
            target.announcements.append(f"{str(r[1])[5:]} {title}")

    # 先补取（会替换 feat 对象），再补系统史/资金流，保证两者挂到最终对象上。
    if backfill:
        _backfill_missing(out, syms)

    # 本地补充（零新增网络）：系统出现史 + 当日主力净占比。
    # 这两项与 K 线新鲜度无关，故不放在 `features_from_bars` 里。
    history = _system_history(conn, syms)
    funds = _fund_flow_pct(conn, syms)
    for sym, feat in out.items():
        feat.history = history.get(sym)
        feat.fund_pct = funds.get(sym)

    return out


def features_from_bars(
    symbol: str,
    bars: list[tuple[str, float | None, float | None, float | None]],
    *,
    newest_finalized: bool = True,
    backfilled: bool = False,
) -> SymbolFeatures:
    """从「**旧→新**」的 `(date, close, high, volume)` 算单票特征。

    **公式的唯一实现** —— 本地库路径（`build_features`）与联网补取路径
    （`agent._backfill_stale_features`）都走这里。两处各写一份「5日累计 / 距20日高 /
    量能」是口径漂移的经典入口（同一只票两条路径给出不同涨幅，模型无法察觉）。

    `newest_finalized=False` ⇒ 最新 bar 是盘中部分 bar，**量能置空**（见 `build_features`
    的 docstring：半天量比整日均量会系统性低估，实测斯迪克 0.8x vs 实际量比 3.31）。
    """
    feat = SymbolFeatures(symbol=symbol)
    feat.backfilled = backfilled
    if not bars:
        return feat

    closes = [_f(b[1]) for b in bars]
    highs = [_f(b[2]) for b in bars]
    vols = [_f(b[3]) for b in bars]
    # bars 是旧→新，最新在末尾
    if len(bars) >= 2 and closes[-1]:
        base5 = closes[-6] if len(closes) >= 6 and closes[-6] else None
        if base5:
            feat.pct_5d = (closes[-1] / base5 - 1.0) * 100.0
        base10 = closes[-11] if len(closes) >= 11 and closes[-11] else None
        if base10:
            feat.pct_10d = (closes[-1] / base10 - 1.0) * 100.0
    if closes[-1] and highs:
        window = [h for h in highs[-20:] if h]
        if window:
            peak = max(window)
            if peak:
                feat.high_20d_gap = (closes[-1] / peak - 1.0) * 100.0
    if not newest_finalized:
        feat.intraday_partial = True
    elif vols and vols[-1]:
        prior = [v for v in vols[-6:-1] if v]
        if prior:
            avg = sum(prior) / len(prior)
            if avg:
                feat.vol_ratio_5d = vols[-1] / avg
    return feat


def render_feature_block(features: dict[str, SymbolFeatures], order: list[str]) -> list[str]:
    """渲染成紧凑的提示词行（每票一行）。`order` 决定顺序，缺特征的票也出行（明说 `—`）。

    盘中（最新 bar 未定稿）时，块首会多一行说明 —— 否则模型看到一列 `量能 —` 会
    以为「本地查不到量能」，而不是「盘中算不准所以给了它更可靠的量比」。这两者的
    后续行为完全不同：前者可能让它放弃判断量能，后者会让它去用面板/东财的量比。
    """
    if not features:
        return []
    lines = [
        "本地特征（已替你算好，**不必**再调 get_kline / get_announcements；"
        "主力净占比/系统史也已带上，只有要净额明细或逐条出现史时才调 get_fund_flow / get_stock_record）："
    ]
    if any(f.intraday_partial for f in features.values()):
        lines.append(
            "（⚠ 当前为**盘中**：今日成交量尚未走完，`量能` 无法与前 5 个完整交易日比较，"
            "故一律显示 `—`。**请改用面板「量比」或 get_fund_flow 返回的「量比」** —— "
            "那个已按时段归一化，是同一件事的正确算法。其余字段（5日/10日累计、距20日高）"
            "用的是当前价，盘中同样成立。）"
        )
    stale: list[str] = []
    seen: set[str] = set()
    for sym in order:
        feat = features.get(sym)
        if feat is None or sym in seen:
            # 同一只票可能同时在 `v1 池选` 与 `飙升区` 两个面板区里出现 ——
            # 特征只渲染一次（重复行白占 token，还会让模型以为它们是两只不同的票）。
            continue
        seen.add(sym)
        if feat.is_stale:
            # 补取失败才走到这里 —— 陈旧数据**不渲染成当前事实**，只点名，
            # 让模型知道「这只没有可用历史」，而不是拿 3 个月前的涨幅当今天。
            stale.append(f"{sym}(落后{feat.stale_days}天)")
            continue
        parts = [
            f"{sym}",
            f"5日 {_pct_text(feat.pct_5d)}",
            f"10日 {_pct_text(feat.pct_10d)}",
            f"距20日高 {_pct_text(feat.high_20d_gap)}",
            f"量能 {feat.vol_ratio_5d:.1f}x" if feat.vol_ratio_5d is not None else "量能 —",
        ]
        if feat.announcements:
            parts.append("公告 " + "；".join(feat.announcements))
        else:
            parts.append("公告 —")
        parts.append(f"主力 {_pct_text(feat.fund_pct)}")
        parts.append(feat.history or "系统史 —")
        if feat.backfilled:
            # 标出「这行是联网补的」：与本地算的同样可信，但模型据此知道
            # 本地库对这只票覆盖不全（可解释它为什么在 daily_kline 里查不到）。
            parts.append("※联网补取")
        lines.append(" ".join(parts))
    if stale:
        # 补取也失败的那些 —— 明确说「没有可用特征」，模型会走工具查，而不是以为它们没异动。
        lines.append(
            f"（⚠ 以下票本地 K 线陈旧且联网补取失败，**不提供**特征，请用 get_kline / "
            f"get_realtime_quote 自己查：{'、'.join(stale)}）"
        )
    return lines
