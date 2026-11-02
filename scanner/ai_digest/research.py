"""AI 调研工具箱（2026-11-03）—— 让模型**自己取真实市场数据**，而不是只吃我们给的数字。

为什么有这个模块：用户反馈「不要按桶算账，要真正做市场数据调研」。进程内模型没有联网，
但**网关支持 tool-calling**（实测 `finish_reason=tool_calls`）⇒ 我们把项目里已有的真实
数据源包装成工具，让模型自己决定查什么、查几次，再基于查到的数据给判断。

工具的取材原则（每条都是「真实市场数据」，不是我们编的口径）：

| 工具 | 数据源 | 联网? |
|---|---|---|
| `get_kline` | 本地 `daily_kline`（收盘定稿），不足则雪球实时 K 线 | 缺数据才联 |
| `get_realtime_quote` | 雪球批量报价（现价/涨幅/量比/换手/市值） | 是 |
| `get_fund_flow` | 东财资金流（主力净占比/超大单/大单） | 是（缓存优先） |
| `get_concepts` | 东财 F10 概念板块（本地 `concept_cache` 优先） | 缺则联 |
| `get_sector_heatmap` | `concept_cache` 反查成分 × `daily_kline` 当日涨幅 | 否 |
| `get_market_overview` | 创业板指 + 涨停池 + 本地 K 线样本涨跌家数 | 指数/涨停池需联 |
| `get_board_rank` | 雪球飙升榜（名次/涨幅/排名变化） | 是 |
| `get_stock_record` | 本系统历史（出现次数/类别/次日收益） | 否 |

失败纪律：任一工具失败都**返回一句人话给模型**（不抛异常、不静默），让模型知道「这块查不到」
而不是拿到空数据瞎猜。工具输出会被截断（token 预算），截断也明说。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from scanner.utils import EXTERNAL_FAILURES

__all__ = ["ResearchContext", "ToolSpec", "build_tools"]

#: 单个工具输出的字符上限（超出截断并明说 —— 静默截断会让模型以为数据就这么多）。
_MAX_TOOL_CHARS = 3000


@dataclass
class ResearchContext:
    """工具运行上下文（连接 / 雪球 session / 缓存）。"""

    conn: sqlite3.Connection
    session: Any = None
    _cache: dict[str, Any] = field(default_factory=dict)

    def get_session(self) -> Any:
        """懒加载雪球 session（工具需要原始 session 的：报价 / 榜单 / K 线）。

        ⚠ 刻意**不**走 `adapter.session`：适配器（`XueqiuAdapter`/`FallbackAdapter`）
        没有公开的 session 属性（内部是 `_get_session()`），从外部取会 `AttributeError`
        —— 2026-11-03 实测踩到（`FallbackAdapter object has no attribute 'session'`）。
        这里直接用 `api.make_session()`，与主循环同一条握手路径。
        """
        if self.session is None:
            from scanner.api import make_session

            self.session = make_session()
        return self.session

    def cached(self, key: str, producer: Callable[[], Any]) -> Any:
        """同一次运行内的工具结果缓存（避免模型重复查同一件事）。"""
        if key not in self._cache:
            self._cache[key] = producer()
        return self._cache[key]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any], ResearchContext], str]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _pct(v: Any) -> str:
    """百分比格式化（None → `—`）。"""
    return f"{v:.2f}%" if isinstance(v, int | float) else "—"


def _money(v: Any) -> str:
    """金额人读格式化（元 → 万/亿）。

    ⚠ 为什么在工具里换算：模型若自己把「641890989.56 元」写成「6.42 亿」，那一亿是
    **它换算出来的**、不是工具给的 ⇒ 幻觉巡检（正文数字 ⊆ 工具输出）会判它编造。
    让工具直接给「6.42亿」，模型照抄即可 —— 换算责任留在确定性代码里。
    """
    if not isinstance(v, int | float):
        return "—"
    av = abs(v)
    if av >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if av >= 1e4:
        return f"{v / 1e4:.2f}万"
    return f"{v:.2f}"


def _truncate(text: str) -> str:
    if len(text) <= _MAX_TOOL_CHARS:
        return text
    return text[:_MAX_TOOL_CHARS] + f"\n…（输出过长已截断，原始长度 {len(text)} 字符）"


def _p(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


# ── 工具实现 ──────────────────────────────────────────────────────────────────


def _tool_get_kline(args: dict[str, Any], ctx: ResearchContext) -> str:
    """个股日线（本地优先）。"""
    symbol = str(args.get("symbol") or "").strip().upper()
    n = max(3, min(int(args.get("n") or 15), 60))
    if not symbol:
        return "参数错误：需要 symbol（如 SZ300289）"

    rows = ctx.conn.execute(
        "SELECT date, open, close, high, low, volume FROM daily_kline WHERE symbol = ? ORDER BY date DESC LIMIT ?",
        (symbol, n),
    ).fetchall()
    src = "本地 daily_kline"
    if len(rows) < 3:
        # 本地不够 → 雪球实时补（收盘定稿的日线才是真值，缺失才用实时）
        try:
            from scanner.api import fetch_kline

            session = ctx.get_session()
            bars = fetch_kline(session, symbol, days=n) or []
            if bars:
                src = "雪球实时 K 线"
                lines = [
                    f"{b['date']} O{b['open']:.2f} C{b['close']:.2f} H{b['high']:.2f} L{b['low']:.2f} "
                    f"量{b['volume'] / 1e4:.0f}万股"
                    for b in bars
                ]
                return f"{symbol} 最近 {len(bars)} 根日线（{src}）：\n" + "\n".join(lines)
        except EXTERNAL_FAILURES as e:
            return f"{symbol}：本地日线不足（{len(rows)} 根）且实时拉取失败（{type(e).__name__}）"

    if not rows:
        return f"{symbol}：本地无日线数据（该票不在 daily_kline 覆盖内）"
    lines = []
    prev_close = None
    for date, o, c, h, low, v in reversed(rows):
        chg = ""
        if prev_close:
            chg = f" ({(c / prev_close - 1) * 100:+.2f}%)"
        prev_close = c
        # 成交量给人读单位（万股）—— 模型若自己把 10491942 写成「1049万」，
        # 那是它换算的，幻觉巡检会判它编造（工具已给单位，模型照抄即可）。
        lines.append(f"{date} O{o:.2f} C{c:.2f} H{h:.2f} L{low:.2f} 量{v / 1e4:.0f}万股{chg}")
    return f"{symbol} 最近 {len(rows)} 根日线（{src}）：\n" + "\n".join(lines)


def _tool_get_realtime_quote(args: dict[str, Any], ctx: ResearchContext) -> str:
    """雪球实时批量报价。"""
    syms = [str(s).strip().upper() for s in (args.get("symbols") or []) if str(s).strip()]
    if not syms:
        return '参数错误：需要 symbols（代码数组，如 ["SZ300289"]）'
    syms = syms[:20]

    def produce() -> dict[str, dict]:
        from scanner.api import fetch_hot_quotes_batch

        session = ctx.get_session()
        return fetch_hot_quotes_batch(session, syms)

    try:
        quotes = ctx.cached("quotes:" + ",".join(syms), produce)
    except EXTERNAL_FAILURES as e:
        return f"实时报价失败（{type(e).__name__}）"
    if not quotes:
        return "实时报价为空（雪球接口可能熔断）"
    lines = []
    for s in syms:
        q = quotes.get(s)
        if not q:
            lines.append(f"{s}: 无数据")
            continue
        parts = [
            f"现价 {q.get('current')}",
            f"涨幅 {_pct(q.get('percent'))}",
            f"量比 {q.get('volume_ratio')}",
            f"换手 {_pct(q.get('turnover_rate'))}",
            f"市值 {_money(q.get('market_capital'))}",
            f"成交额 {_money(q.get('amount'))}",
        ]
        lines.append(f"{s}: " + " | ".join(parts))
    return "实时报价：\n" + "\n".join(lines)


def _tool_get_fund_flow(args: dict[str, Any], ctx: ResearchContext) -> str:
    """东财资金流（主力净占比 / 超大单 / 大单）。"""
    syms = [str(s).strip().upper() for s in (args.get("symbols") or []) if str(s).strip()]
    if not syms:
        return "参数错误：需要 symbols"
    syms = syms[:20]
    try:
        from scanner.market_extra import fetch_fund_flow_rank

        rows = ctx.cached("ff:" + ",".join(syms), fetch_fund_flow_rank)
    except EXTERNAL_FAILURES as e:
        return f"资金流查询失败（{type(e).__name__}）"
    if not rows:
        return "资金流数据为空"
    lines = []
    for s in syms:
        # ⚠ 东财行的键名是 `main_net`/`super_net`/`turnover`/`vol_ratio`（实测 2026-11-03），
        #   不是猜的 `xl_net`/`lg_net`/`turnover_rate` —— 后者全是 None。
        r = rows.get(s) or rows.get(s[2:]) or {}
        if not r:
            lines.append(f"{s}: 无资金流数据")
            continue
        lines.append(
            f"{s} {r.get('name') or ''}: 主力净占比 {_pct(r.get('main_pct'))}"
            f" | 主力净额 {_money(r.get('main_net'))}"
            f" | 超大单净额 {_money(r.get('super_net'))}"
            f" | 换手 {_pct(r.get('turnover'))}"
            f" | 量比 {r.get('vol_ratio')}"
            f" | 成交额 {_money(r.get('amount'))}"
            f" | 流通市值 {_money(r.get('float_cap'))}"
        )
    return "资金流（金额单位：万/亿）：" + chr(10).join(lines)


def _tool_get_concepts(args: dict[str, Any], ctx: ResearchContext) -> str:
    """个股所属概念/板块（F10）。"""
    symbol = str(args.get("symbol") or "").strip().upper()
    if not symbol:
        return "参数错误：需要 symbol"
    try:
        from scanner.db.queries import get_concepts_cache

        cached = ctx.cached("concepts:" + symbol, lambda: get_concepts_cache(ctx.conn, [symbol]).get(symbol, []))
        boards = list(cached or [])
        if not boards:
            from scanner.concept import fetch_stock_boards

            boards = fetch_stock_boards(symbol) or []
        if not boards:
            return f"{symbol}：无概念板块数据"
        return f"{symbol} 所属概念/板块：{'、'.join(boards)}"
    except EXTERNAL_FAILURES as e:
        return f"概念查询失败（{type(e).__name__}）"


def _tool_get_sector_heatmap(args: dict[str, Any], ctx: ResearchContext) -> str:
    """某概念下各成分股的当日涨幅（谁领涨/谁掉队）—— 板块共振还是个股孤军。"""
    concept = str(args.get("concept") or "").strip()
    if not concept:
        return "参数错误：需要 concept（概念名）"
    # ⚠ `daily_kline` 列名是 `percent`；`concept_cache.concepts` 存的是 **JSON 数组文本**
    #   （实测：'["算力","东数西算",...]'）⇒ 用 LIKE 匹配 JSON 片段即可。
    rows = ctx.conn.execute(
        "SELECT c.symbol, k.percent, k.close, c.concepts FROM concept_cache c "
        "JOIN daily_kline k ON k.symbol = c.symbol "
        "WHERE k.date = (SELECT MAX(date) FROM daily_kline) AND c.concepts LIKE ?",
        (f"%{concept}%",),
    ).fetchall()
    if not rows:
        return f"概念「{concept}」：本地无成分股当日数据"
    rows = sorted(rows, key=lambda r: r[1] if r[1] is not None else -999, reverse=True)
    # ⚠ SELECT 4 列（symbol, percent, close, concepts）⇒ 解包要 4 个（2026-11-03 实测：
    #   写成 3 个会 ValueError「too many values to unpack」，工具直接不可用）。
    head = "、".join(f"{s} {p:+.2f}%" for s, p, _cl, _c in rows[:8] if p is not None)
    tail = "、".join(f"{s} {p:+.2f}%" for s, p, _cl, _c in rows[-5:] if p is not None)
    return f"概念「{concept}」当日 {len(rows)} 只有本地数据（涨幅最高→最低）：\n领涨：{head}\n靠后：{tail}"


def _tool_get_market_overview(args: dict[str, Any], ctx: ResearchContext) -> str:
    """市场概览：指数、涨停家数、样本涨跌分布 —— 判断这是普涨还是结构行情。"""
    parts: list[str] = []
    # ① 本地 K 线样本的涨跌分布（免费）
    # ⚠ `daily_kline` 的列名是 **`percent`**（不是 `pct`）—— 写错会 OperationalError
    row = ctx.conn.execute(
        "SELECT SUM(CASE WHEN percent > 0 THEN 1 ELSE 0 END), SUM(CASE WHEN percent < 0 THEN 1 ELSE 0 END), "
        "count(*), MAX(date) FROM daily_kline WHERE date = (SELECT MAX(date) FROM daily_kline)"
    ).fetchone()
    if row and row[2]:
        parts.append(f"本地 K 线样本（{row[3]}）：上涨 {row[0]} / 下跌 {row[1]} / 共 {row[2]}（覆盖 {row[2]} 只）")
    # ② 创业板指
    try:
        from scanner.api import fetch_market_index

        session = ctx.get_session()
        pct = ctx.cached("index", lambda: fetch_market_index(session))
        parts.append(f"创业板指：{pct}%" if pct is not None else "创业板指：取不到")
    except EXTERNAL_FAILURES as e:
        parts.append(f"创业板指：取不到（{type(e).__name__}）")
    # ③ 涨停池（情绪温度）
    try:
        from scanner.market_extra import fetch_zt_pool

        zt = ctx.cached("zt", fetch_zt_pool)
        if zt:
            parts.append(f"涨停池：{len(zt)} 家（连板数据见下）")
        else:
            parts.append("涨停池：取不到")
    except EXTERNAL_FAILURES as e:
        parts.append(f"涨停池：取不到（{type(e).__name__}）")
    return "市场概览：\n" + "\n".join(parts)


def _tool_get_board_rank(args: dict[str, Any], ctx: ResearchContext) -> str:
    """雪球飙升榜当前排名 —— 榜单热度是「资金关注度」的独立读数。"""
    try:
        from scanner.api import fetch_biaosheng

        session = ctx.get_session()
        items = ctx.cached("board", lambda: fetch_biaosheng(session, 30))
    except EXTERNAL_FAILURES as e:
        return f"飙升榜获取失败（{type(e).__name__}）"
    if not items:
        return "飙升榜为空（可能熔断中）"
    lines = []
    for i, it in enumerate(items[:15], 1):
        sym = it.get("symbol") or it.get("code") or "?"
        lines.append(f"#{i} {sym} {it.get('name') or ''} 涨幅 {it.get('percent')}%")
    return "飙升榜前 15：\n" + "\n".join(lines)


def _tool_get_stock_record(args: dict[str, Any], ctx: ResearchContext) -> str:
    """该票在本系统的历史记录（出现史 / 类别 / 次日收益）—— 它到底是不是「老面孔」。"""
    symbol = str(args.get("symbol") or "").strip().upper()
    if not symbol:
        return "参数错误：需要 symbol"
    # ⚠ `appearances` 表的列是 (id, symbol, name, date, rank, percent, value, next_day_pct)
    #   —— **没有** category/score/excluded（那是 `recommendations` 的列）⇒ 用后者。
    rows = ctx.conn.execute(
        "SELECT date, category, score, percent, excluded, next_day_pct FROM recommendations "
        "WHERE symbol = ? ORDER BY date DESC LIMIT 15",
        (symbol,),
    ).fetchall()
    if not rows:
        return f"{symbol}：本系统近 15 条出现史为空（该票没进过 v1 推荐池）"
    lines = [
        f"{d} {cat} score={sc} 涨幅={_pct(pct)} excluded={ex} 次日={_pct(ndp) if ndp is not None else '—'}"
        for d, cat, sc, pct, ex, ndp in rows
    ]
    return f"{symbol} 最近 {len(rows)} 条系统记录（v1 推荐池）：" + chr(10).join(lines)


def _tool_get_announcements(args: dict[str, Any], ctx: ResearchContext) -> str:
    """最近公告（东财公开接口）—— 公司层面的真实事件。"""
    symbol = str(args.get("symbol") or "").strip().upper()
    if not symbol:
        return "参数错误：需要 symbol"
    n = max(3, min(int(args.get("n") or 8), 30))
    try:
        from scanner.ai_digest.em_data import fetch_announcements

        items = ctx.cached(f"ann:{symbol}:{n}", lambda: fetch_announcements(symbol, n))
    except EXTERNAL_FAILURES as e:
        return f"公告查询失败（{type(e).__name__}）"
    if not items:
        return f"{symbol}：最近无公告可查（接口空或该票无公告）"
    lines = [f"{it['date']} {it['title']}" + (f"［{it['type']}］" if it.get("type") else "") for it in items]
    return f"{symbol} 最近 {len(items)} 条公告（仅标题与日期，**未经核实其利好/利空**）：\n" + chr(10).join(lines)


def _tool_get_intraday_fund_flow(args: dict[str, Any], ctx: ResearchContext) -> str:
    """当日分时资金流曲线（东财）—— 主力/超大单/大单/中单/小单的分钟级累计净流入。

    看的是**节奏**：早盘冲高回落、尾盘拉升、全天单边流入。
    """
    symbol = str(args.get("symbol") or "").strip().upper()
    if not symbol:
        return "参数错误：需要 symbol"
    try:
        from scanner.ai_digest.em_data import fetch_intraday_fund_flow

        pts = ctx.cached(f"fflow:{symbol}", lambda: fetch_intraday_fund_flow(symbol))
    except EXTERNAL_FAILURES as e:
        return f"分时资金流查询失败（{type(e).__name__}）"
    if not pts:
        return f"{symbol}：当日分时资金流为空（非交易日或接口无数据）"

    # 关键时点：开盘/10:30/午盘/14:00/收盘 + 尾盘 30 分钟增量
    def near(hhmm: str):
        return min(pts, key=lambda p: abs(_hhmm(p["time"]) - _hhmm(hhmm)))

    marks = ["09:30", "10:30", "11:30", "14:00", pts[-1]["time"]]
    lines = []
    for m in marks:
        p = near(m)
        lines.append(
            f"{p['time']} 主力 {_money(p['main'])} | 超大单 {_money(p['xl'])} | 大单 {_money(p['large'])}"
            f" | 中单 {_money(p['medium'])} | 小单 {_money(p['small'])}"
        )
    tail = pts[-1]
    last30 = [p for p in pts if _hhmm(p["time"]) >= _hhmm(tail["time"]) - 30]
    first30 = [p for p in pts if _hhmm(p["time"]) <= _hhmm(pts[0]["time"]) + 30]
    tail_delta = tail["main"] - (last30[0]["main"] if last30 else tail["main"])
    head_delta = (first30[-1]["main"] if first30 else 0) - (pts[0]["main"] if pts else 0)
    lines.append(
        f"节奏：早盘30分钟主力增量 {_money(head_delta)}；尾盘30分钟主力增量 {_money(tail_delta)}"
        f"（全天主力净额 {_money(tail['main'])}）"
    )
    return f"{symbol} 当日分时资金流（累计净额，单位元→万/亿）：\n" + chr(10).join(lines)


def _hhmm(s: str) -> int:
    """'09:30' → 570（分钟数，用于就近取样与区间比较）。"""
    try:
        h, m = str(s).split(":")[:2]
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return 0


def _tool_get_dragon_tiger(args: dict[str, Any], ctx: ResearchContext) -> str:
    """龙虎榜历史（东财）：上榜原因、买卖净额、**D1/D2/D5/D10 后续涨幅**。

    D* 是「上榜后第 N 个交易日收盘涨跌幅」—— 游资参与 + 事后涨幅，硬证据。
    """
    symbol = str(args.get("symbol") or "").strip().upper()
    if not symbol:
        return "参数错误：需要 symbol"
    try:
        from scanner.ai_digest.em_data import fetch_dragon_tiger

        rows = ctx.cached(f"dtb:{symbol}", lambda: fetch_dragon_tiger(symbol, 5))
    except EXTERNAL_FAILURES as e:
        return f"龙虎榜查询失败（{type(e).__name__}）"
    if not rows:
        return f"{symbol}：无龙虎榜记录（该票历史上没上过榜）"
    lines = []
    for r in rows:
        lines.append(
            f"{r['date']} 上榜原因：{r['reason'] or '—'} | 榜单净额 {_money(r['net'])}"
            f" | 成交占比 {_pct(r['deal_ratio'])} | 当日涨跌 {_pct(r['change'])}"
            f" | 后续 D1 {_pct(r['d1'])} D2 {_pct(r['d2'])} D5 {_pct(r['d5'])} D10 {_pct(r['d10'])}"
        )
    return f"{symbol} 龙虎榜历史（{len(rows)} 次）：\n" + chr(10).join(lines)


def _tool_get_fund_flow_trend(args: dict[str, Any], ctx: ResearchContext) -> str:
    """多日资金流趋势（东财日频序列，近 121 个交易日）—— 主力是在持续流入还是持续流出。

    ⚠ 这是**日频**不是分时（`klt=101` 实测返回 121 个交易日）；日内节奏由
    `get_intraday_minute`（雪球分时）负责。
    """
    symbol = str(args.get("symbol") or "").strip().upper()
    if not symbol:
        return "参数错误：需要 symbol"
    try:
        from scanner.ai_digest.em_data import fetch_intraday_fund_flow

        pts = ctx.cached(f"fflow_trend:{symbol}", lambda: fetch_intraday_fund_flow(symbol))
    except EXTERNAL_FAILURES as e:
        return f"资金流趋势查询失败（{type(e).__name__}）"
    if not pts:
        return f"{symbol}：无资金流序列数据"
    lines = [f"覆盖 {len(pts)} 个交易日（{pts[0]['date']} ~ {pts[-1]['date']}）"]
    for n, label in ((5, "近5日"), (20, "近20日"), (60, "近60日"), (len(pts), "全部")):
        window = pts[-n:] if n <= len(pts) else pts
        main = sum(p["main"] for p in window)
        xl = sum(p["xl"] for p in window)
        pos = sum(1 for p in window if p["main"] > 0)
        lines.append(f"{label}：主力净额合计 {_money(main)}（净流入天数 {pos}/{len(window)}）｜超大单合计 {_money(xl)}")
    lines.append("最近5日主力净额：" + "、".join(f"{p['date'][5:]} {_money(p['main'])}" for p in pts[-5:]))
    return f"{symbol} 资金流趋势（日频累计净额）：\n" + chr(10).join(lines)


def _tool_get_intraday_minute(args: dict[str, Any], ctx: ResearchContext) -> str:
    """当日分时（雪球 minute.json）—— 日内节奏：开盘强度、量能分布、均价线、尾盘拉升。"""
    symbol = str(args.get("symbol") or "").strip().upper()
    if not symbol:
        return "参数错误：需要 symbol"
    try:
        from scanner.api import _fetch_minute_data

        session = ctx.get_session()
        items = ctx.cached(f"minute:{symbol}", lambda: _fetch_minute_data(session, symbol) or [])
    except EXTERNAL_FAILURES as e:
        return f"分时查询失败（{type(e).__name__}）"
    items = [i for i in (items or []) if i.get("current")]
    if len(items) < 5:
        return f"{symbol}：无分时数据（非交易时段或接口无数据）"

    first = items[0]
    last = items[-1]
    highs = max(items, key=lambda i: i.get("high") or i["current"])
    lows = min(items, key=lambda i: (i.get("low") or i["current"]) if i.get("low") else i["current"])
    total_vol = sum(i.get("volume") or 0 for i in items)

    # 尾盘 30 分钟量占比（分钟 bar 的时间戳是毫秒）
    def hhmm(item):
        ts = item.get("timestamp") or 0
        try:
            from datetime import datetime

            from scanner.config import BEIJING_TZ

            return datetime.fromtimestamp(float(ts) / 1000, tz=BEIJING_TZ).strftime("%H:%M")
        except (TypeError, ValueError, OSError):
            return "00:00"

    tail_items = [i for i in items if hhmm(i) >= "14:30"]
    tail_vol = sum(i.get("volume") or 0 for i in tail_items)
    tail_share = (tail_vol / total_vol * 100) if total_vol else 0.0
    avg = last.get("avg_price") or 0.0
    close = last.get("current") or 0.0
    lines = [
        f"分时点数 {len(items)}｜开盘 {first['current']:.2f}｜最新 {close:.2f}"
        f"｜日内最高 {highs.get('high') or highs['current']:.2f}｜最低 {lows.get('low') or lows['current']:.2f}"
        f"｜均价 {avg:.2f}",
        f"收盘/均价 {'高于' if close >= avg else '低于'}均价（{close:.2f} vs {avg:.2f}）",
        f"全天成交量 {total_vol / 1e4:.0f}万股｜尾盘(14:30后)量占比 {tail_share:.1f}%",
        f"最新时间 {hhmm(last)}｜尾盘量占比高说明资金在收盘前集中进场",
    ]
    return f"{symbol} 当日分时（雪球）：\n" + chr(10).join(lines)


def build_tools() -> list[ToolSpec]:
    """工具清单（顺序 = 给模型看到的顺序，先便宜后贵）。"""
    return [
        ToolSpec(
            name="get_kline",
            description="取某只创业板股票最近 N 根日线（开收高低量 + 逐日涨跌），用于看形态、位置、量能变化。symbol 如 SZ300289，n 默认 15。",
            parameters=_p(
                {
                    "symbol": {"type": "string", "description": "带交易所前缀的代码，如 SZ300289"},
                    "n": {"type": "integer", "description": "取几根日线，默认 15，最多 60"},
                },
                ["symbol"],
            ),
            handler=_tool_get_kline,
        ),
        ToolSpec(
            name="get_realtime_quote",
            description="取多只票的实时/最新报价：现价、涨幅、量比、换手、市值、成交额。用于确认当前价格与热度。",
            parameters=_p(
                {"symbols": {"type": "array", "items": {"type": "string"}, "description": "代码数组，最多 20 个"}},
                ["symbols"],
            ),
            handler=_tool_get_realtime_quote,
        ),
        ToolSpec(
            name="get_fund_flow",
            description="取多只票的东财资金流：主力净占比、主力净额、超大单净额、大单净额、换手率。用于看资金方向与力度。",
            parameters=_p(
                {"symbols": {"type": "array", "items": {"type": "string"}, "description": "代码数组，最多 20 个"}},
                ["symbols"],
            ),
            handler=_tool_get_fund_flow,
        ),
        ToolSpec(
            name="get_concepts",
            description="取某只票所属的概念/板块（如 CPO、光模块、存储芯片）。用于判断它属于哪个题材、有没有板块效应。",
            parameters=_p({"symbol": {"type": "string"}}, ["symbol"]),
            handler=_tool_get_concepts,
        ),
        ToolSpec(
            name="get_sector_heatmap",
            description="取某概念下所有本地有数据的成分股当日涨幅排名（谁领涨、谁掉队）。用于判断是个股孤军还是板块共振。",
            parameters=_p({"concept": {"type": "string", "description": "概念名，如 CPO"}}, ["concept"]),
            handler=_tool_get_sector_heatmap,
        ),
        ToolSpec(
            name="get_market_overview",
            description="取市场概览：本地样本涨跌家数、创业板指涨跌、涨停池家数。用于判断当前是普涨还是结构行情、情绪冷热。",
            parameters=_p({}, []),
            handler=_tool_get_market_overview,
        ),
        ToolSpec(
            name="get_board_rank",
            description="取雪球飙升榜当前前 15 名（名次、代码、涨幅）。榜单热度是资金关注度的独立读数。",
            parameters=_p({}, []),
            handler=_tool_get_board_rank,
        ),
        ToolSpec(
            name="get_stock_record",
            description="取该票在本扫描系统的出现史（日期、类别、评分）。用于判断它是不是反复上榜的熟悉票。",
            parameters=_p({"symbol": {"type": "string"}}, ["symbol"]),
            handler=_tool_get_stock_record,
        ),
        ToolSpec(
            name="get_announcements",
            description="取该票最近的上市公司公告（标题+日期）。用于判断近期有没有实质公司事件（业绩预告/重组/增减持等）。",
            parameters=_p(
                {
                    "symbol": {"type": "string"},
                    "n": {"type": "integer", "description": "取几条，默认 8，最多 30"},
                },
                ["symbol"],
            ),
            handler=_tool_get_announcements,
        ),
        ToolSpec(
            name="get_fund_flow_trend",
            description=(
                "取该票近 121 个交易日的资金流日频序列汇总（主力/超大单净额合计、净流入天数）。"
                "用于判断主力资金是在持续流入还是持续流出。"
            ),
            parameters=_p({"symbol": {"type": "string"}}, ["symbol"]),
            handler=_tool_get_fund_flow_trend,
        ),
        ToolSpec(
            name="get_intraday_minute",
            description=(
                "取该票当日分时（雪球分钟 bar）：开盘价、日内高低、均价、成交量、尾盘半小时量占比。"
                "用于看日内节奏：开盘冲高回落、尾盘拉升、收盘是否站上均价。"
            ),
            parameters=_p({"symbol": {"type": "string"}}, ["symbol"]),
            handler=_tool_get_intraday_minute,
        ),
        ToolSpec(
            name="get_dragon_tiger",
            description=(
                "取该票龙虎榜历史：上榜原因、榜单买卖净额、成交占比，以及 D1/D2/D5/D10 后续涨幅。"
                "用于看游资参与痕迹，以及「以前上榜后到底涨没涨」的硬证据（该票没上过榜会返回空）。"
            ),
            parameters=_p({"symbol": {"type": "string"}}, ["symbol"]),
            handler=_tool_get_dragon_tiger,
        ),
    ]


def run_tool(spec: ToolSpec, args: dict[str, Any], ctx: ResearchContext) -> str:
    """执行一个工具并返回给模型的文本（**永不抛异常** —— 失败要说人话）。"""
    try:
        raw = spec.handler(args, ctx)
    except EXTERNAL_FAILURES as e:
        raw = f"工具 {spec.name} 查询失败：{type(e).__name__}（该数据本轮不可用）"
    except (KeyError, TypeError, ValueError, AttributeError, IndexError) as e:
        # ⚠ 工具边界上**显式列出**常见数据形状错误，而不是「捕获所有异常」：
        #   一个工具挂掉不该让整轮调研作废（模型会看到「该工具不可用」并继续用别的），
        #   但也不能吞掉一切 —— 异常类型会写进返回文本。
        raw = f"工具 {spec.name} 数据形状异常：{type(e).__name__}（该数据本轮不可用）"
    if not isinstance(raw, str):
        raw = json.dumps(raw, ensure_ascii=False, default=str)
    return _truncate(raw)
