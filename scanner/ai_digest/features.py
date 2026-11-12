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
- `公告` —— 最近 3 条标题（雷点/催化）

## 纪律

1. **纯本地**：只读 SQLite，零网络、零时钟（"今日"取 `daily_kline` 的 `MAX(date)`）。
2. **不猜**：某票缺数据就**明说缺**（`5日 —`），不填 0、不留空 —— 模型会把空缺读成"没有异动"。
3. **可失败**：任何异常退回"无特征"并让调用方照常构造提示词（预加载是增强，不是前置条件）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date

__all__ = [
    "SymbolFeatures",
    "build_features",
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


def build_features(
    conn: sqlite3.Connection,
    symbols: list[str],
    *,
    ann_limit: int = _ANN_LIMIT,
) -> dict[str, SymbolFeatures]:
    """按票算本地特征。`symbols` 为空或查询失败时返回空 dict（调用方照常继续）。

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
            closes = [_f(b[2]) for b in bars]
            highs = [_f(b[3]) for b in bars]
            vols = [_f(b[4]) for b in bars]
            # bars[0] 是最新一根
            if len(bars) >= 2 and closes[0] and closes[-1]:
                base5 = closes[5] if len(closes) > 5 and closes[5] else None
                if base5:
                    feat.pct_5d = (closes[0] / base5 - 1.0) * 100.0
                base10 = closes[10] if len(closes) > 10 and closes[10] else None
                if base10:
                    feat.pct_10d = (closes[0] / base10 - 1.0) * 100.0
            if closes[0] and highs:
                window = [h for h in highs[:20] if h]
                if window:
                    peak = max(window)
                    if peak:
                        feat.high_20d_gap = (closes[0] / peak - 1.0) * 100.0
            # ⚠ 量能只在**最新 bar 已定稿**时算 —— 盘中那根是部分 bar，除以完整交易日的
            #   均量会系统性低估（实测斯迪克 10:29 算出 0.8x"萎缩"，而实际量比 3.31）。
            #   未定稿 ⇒ 置 None（渲染成 `—`），交给模型的量比（按时段归一化）去回答。
            newest_finalized = bars[0][6] if len(bars[0]) > 6 else 1
            if vols and vols[0] and newest_finalized:
                prior = [v for v in vols[1:6] if v]
                if prior:
                    avg = sum(prior) / len(prior)
                    if avg:
                        feat.vol_ratio_5d = vols[0] / avg
            if not newest_finalized:
                feat.intraday_partial = True
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

    return out


def render_feature_block(features: dict[str, SymbolFeatures], order: list[str]) -> list[str]:
    """渲染成紧凑的提示词行（每票一行）。`order` 决定顺序，缺特征的票也出行（明说 `—`）。

    盘中（最新 bar 未定稿）时，块首会多一行说明 —— 否则模型看到一列 `量能 —` 会
    以为「本地查不到量能」，而不是「盘中算不准所以给了它更可靠的量比」。这两者的
    后续行为完全不同：前者可能让它放弃判断量能，后者会让它去用面板/东财的量比。
    """
    if not features:
        return []
    lines = ["本地特征（已替你算好，**不必**再调 get_kline / get_announcements 重复取）："]
    if any(f.intraday_partial for f in features.values()):
        lines.append(
            "（⚠ 当前为**盘中**：今日成交量尚未走完，`量能` 无法与前 5 个完整交易日比较，"
            "故一律显示 `—`。**请改用面板「量比」或 get_fund_flow 返回的「量比」** —— "
            "那个已按时段归一化，是同一件事的正确算法。其余字段（5日/10日累计、距20日高）"
            "用的是当前价，盘中同样成立。）"
        )
    stale: list[str] = []
    for sym in order:
        feat = features.get(sym)
        if feat is None:
            continue
        if feat.is_stale:
            # 陈旧票**不渲染假数字**，只标出来 —— 让模型知道「这只没有本地历史」，
            # 而不是拿到一组 3 个月前的涨幅当成今天的事实。
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
        lines.append(" ".join(parts))
    if stale:
        # 明确说「这些票没有本地特征可用」⇒ 模型会走工具查，而不是以为它们没异动。
        lines.append(
            f"（⚠ 以下票本地 K 线已陈旧，**不提供**特征，请用 get_kline / get_realtime_quote "
            f"自己查：{'、'.join(stale)}）"
        )
    return lines
