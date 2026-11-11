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


@dataclass
class SymbolFeatures:
    """单票的本地特征（全部可缺省 —— 缺就渲染成 `—`）。"""

    symbol: str
    pct_5d: float | None = None
    pct_10d: float | None = None
    high_20d_gap: float | None = None  # 现价距 20 日最高价的百分比（负=还在下方）
    vol_ratio_5d: float | None = None  # 今日成交量 / 前 5 日均量
    announcements: list[str] = field(default_factory=list)


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

        # 一次取够 21 根（算 10 日累计 + 20 日高点），按票分组
        sql = (  # noqa: S608 - 占位符由 ",".join("?" * n) 生成，值经参数化传入
            "SELECT symbol, date, close, high, volume, percent FROM daily_kline "  # noqa: S608
            f"WHERE symbol IN ({ph}) AND date <= ? ORDER BY symbol, date DESC"  # noqa: S608
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
            if vols and vols[0]:
                prior = [v for v in vols[1:6] if v]
                if prior:
                    avg = sum(prior) / len(prior)
                    if avg:
                        feat.vol_ratio_5d = vols[0] / avg
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
    """渲染成紧凑的提示词行（每票一行）。`order` 决定顺序，缺特征的票也出行（明说 `—`）。"""
    if not features:
        return []
    lines = ["本地特征（已替你算好，**不必**再调 get_kline / get_announcements 重复取）："]
    for sym in order:
        feat = features.get(sym)
        if feat is None:
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
    return lines
