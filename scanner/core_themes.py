"""核心方向低吸（2026-08-19）：大跌市中找「当前市场主线方向的核心股低吸」机会。

回马枪补「掉榜超跌」盲区（越低越近买点）；本模块补「大跌市中主线方向回调低吸」
盲区。两个模块都面向弱市，但语义不同：
- 回马枪：掉榜超跌票反弹企稳（off_list）。
- 核心方向低吸：仍在走强的市场主线性概念（近 N 日持续上榜/强势）里的核心股，
  在上涨途中健康回调（非超跌、非破位）的低吸窗口。

纯展示层推导 + 落库归因：全部基于本地 DB（recommendations / concept_cache /
daily_kline / market_extra_cache / appearances），零新增网络请求。写入
recommendations 表（category=core_dip）供 prevday_perf / nextday_attribution
复盘验证，但不进综合排序主表 / 回测口径（in_main_table=False, nextday_markable=False），
作为独立显示区块提供「大跌时该盯哪些主线回流票」的参考（用户 2026-08-19 需求，
实验性，待样本积累后评估）。

方法论（数据驱动）：
1. 识别当前核心方向：近 N 交易日推荐按概念聚合「持续上榜天数」，再叠加「主题相对
   强度」（成员近期累计涨幅 vs 全市场活跃票中位数）。持续>=min_days 且强度达标
   = 核心方向，取前 top_n（防板块普涨刷屏）。
2. 找核心股：核心方向里近期已走强的成员股（20日累计涨幅 >= run_min，有上涨/龙头
   属性）。
3. 低吸窗口：从 20 日高点回撤 pullback∈[pb_min, pb_max]（健康回调）、未破 MA20、
   今日企稳（非崩盘）、未超买死亡、主力未大幅出逃。回撤越深越接近买点。

统计口径防御：bar 走 make_kline_bar 契约（close 已保证为正数），额外用 isfinite/
正值守卫，脏值票一律跳过；任一步骤异常 fail-open 返回空列表，不阻塞 display 主流程。
"""

import json
import logging
import sqlite3
from collections.abc import Sequence

from scanner.config import (
    CORE_DIP_CATEGORY,
    CORE_FLOW_FLOOR,
    CORE_MA20_BELOW_SLACK,
    CORE_NOT_OVERHEATED,
    CORE_PULLBACK_MAX,
    CORE_PULLBACK_MIN,
    CORE_RUN_MIN,
    CORE_THEME_LOOKBACK_DAYS,
    CORE_THEME_MAX_PER_THEME,
    CORE_THEME_MAX_TOTAL,
    CORE_THEME_MIN_DAYS,
    CORE_THEME_NOISE,
    CORE_THEME_TOP_N,
    CORE_TODAY_FLOOR,
    now_beijing,
)
from scanner.database import (
    _n_trading_days_ago,
    get_cached_klines,
    get_fund_flow_pct_map,
)
from scanner.utils import EXTERNAL_FAILURES, to_float

logger = logging.getLogger(__name__)

_THEME_STRENGTH_WINDOW = 10  # 主题强度：近 10 个交易日累计涨幅
_DIP_HIGH_WINDOW = 20  # 近期高点窗口
_RUN_WINDOW = 20  # 龙头属性：20 日累计涨幅
_MIN_BARS = 24  # 需要至少 24 根 bar 才有可靠的均线/高点


def _median(vals: Sequence[float | None]) -> float | None:
    nums = [v for v in vals if v is not None]
    if not nums:
        return None
    s = sorted(nums)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def _recent_10d_return(closes: list[float]) -> float | None:
    """近 10 日累计涨幅（closes 升序）。"""
    if len(closes) < 12:
        return None
    base = closes[-11]
    if base <= 0:
        return None
    return closes[-1] / base - 1.0


def _dip_metrics(close: list[float], dates: list[str], today: str) -> dict | None:
    """从价格序列计算低吸窗口指标；不满足最低 bar 数返回 None。"""
    if len(close) < _MIN_BARS:
        return None
    latest = close[-1]
    if latest <= 0 or any(c <= 0 for c in close[-_RUN_WINDOW:]):
        return None
    base20 = close[-_RUN_WINDOW - 1]
    if base20 <= 0:
        return None
    run = latest / base20 - 1.0  # 20 日累计涨幅
    hi20 = max(close[-_DIP_HIGH_WINDOW:])
    if hi20 <= 0:
        return None
    pullback = latest / hi20 - 1.0  # 距 20 日高点回撤（负值）
    ma20 = sum(close[-_DIP_HIGH_WINDOW:]) / _DIP_HIGH_WINDOW
    top_gain = hi20 / base20 - 1.0  # 高点相对 20 日前涨幅（超买判断）
    # 跌破 MA20 的超跌比例（0 = 未跌破；正值 = 跌破幅度，判断是否破位）
    below_ma20_ratio = (ma20 - latest) / ma20 if ma20 > 0 and latest < ma20 else 0.0
    return {
        "run": run,
        "pullback": pullback,
        "ma20": ma20,
        "overheated": top_gain > CORE_NOT_OVERHEATED,
        "today_pct": _today_percent(dates, close, today, latest),
        "below_ma20_ratio": below_ma20_ratio,
    }


def _today_percent(dates: list[str], close: list[float], today: str, latest: float) -> float | None:
    """今日 bar 涨幅（盘中/收盘真实值），无今日 bar 返回 None（不误杀/不误判企稳）。"""
    if not dates or dates[-1] != today:
        return None
    prev = close[-2] if len(close) >= 2 else None
    if not prev or prev <= 0:
        return None
    return latest / prev - 1.0


def _symbol_names(conn: sqlite3.Connection, symbols: list[str]) -> dict[str, str]:
    """appearances 取每 symbol 最近一次的 name（概念成员展示用）。"""
    if not symbols:
        return {}
    try:
        ph = ",".join("?" * len(symbols))
        cur = conn.execute(
            f"""SELECT a.symbol, a.name
                FROM appearances a
                JOIN (SELECT symbol, MAX(date) md FROM appearances
                      WHERE symbol IN ({ph}) GROUP BY symbol) t
                  ON a.symbol = t.symbol AND a.date = t.md""",  # noqa: S608 - 占位符由 ",".join("?" * n) 生成，值经参数化传入
            symbols,
        )
        return dict(cur.fetchall())
    except EXTERNAL_FAILURES as e:
        logger.warning(f"_symbol_names failed: {e}")
        return {}


def identify_core_themes(
    conn: sqlite3.Connection,
    today: str,
    lookback_days: int = CORE_THEME_LOOKBACK_DAYS,
    min_days: int = CORE_THEME_MIN_DAYS,
    top_n: int = CORE_THEME_TOP_N,
) -> list[dict]:
    """近 lookback_days 交易日识别「当前核心方向」概念。

    信号 = 概念近 N 日推荐「持续上榜天数」×「主题相对强度」（成员近 10 日累计涨幅
    中位数 − 全市场活跃票中位数）。持续>=min_days 且相对强度>=0 才入选，避免单一
    爆量日概念被误判为主线；按 (天数, 强度) 取前 top_n。

    返回 [{name, days, strength}]，strength 为相对市场强度。
    """
    try:
        lookback = _n_trading_days_ago(lookback_days, as_of=today)
        cur = conn.execute(
            "SELECT symbol, name, date, concept FROM recommendations "
            "WHERE date >= ? AND date < ? AND concept IS NOT NULL AND concept != '' "
            "ORDER BY date DESC, score DESC",
            (lookback, today),
        )
        recs = [{"symbol": r[0], "name": r[1], "date": r[2], "concept": r[3]} for r in cur.fetchall()]
    except EXTERNAL_FAILURES as e:
        logger.warning(f"近期推荐读取失败: {e}")
        return []

    # 按概念聚合：distinct symbol + distinct date
    theme_syms: dict[str, set[str]] = {}
    theme_dates: dict[str, set[str]] = {}
    for r in recs:
        name = (r.get("concept") or "").strip()
        if not name or name in CORE_THEME_NOISE:
            continue
        theme_syms.setdefault(name, set()).add(r["symbol"])
        theme_dates.setdefault(name, set()).add(r["date"])

    if not theme_syms:
        return []

    # 载入所有成员 K 线（单次批量 SQL），算主题强度与市场强度
    all_syms = sorted({s for syms in theme_syms.values() for s in syms})
    klines = get_cached_klines(conn, all_syms)
    rets: dict[str, float | None] = {}
    for sym in all_syms:
        k = klines.get(sym)
        if not k:
            rets[sym] = None
            continue
        rets[sym] = _recent_10d_return([b["close"] for b in k])

    market_med = _median([v for v in rets.values() if v is not None]) or 0.0

    candidates: list[dict] = []
    for name, syms in theme_syms.items():
        if len(theme_dates.get(name, set())) < min_days:
            continue
        theme_rets = [v for s in syms if (v := rets.get(s)) is not None]
        strength = (_median(theme_rets) or 0.0) - market_med
        if strength < 0:
            continue
        candidates.append(
            {
                "name": name,
                "days": len(theme_dates.get(name, set())),
                "strength": strength,
            }
        )

    candidates.sort(key=lambda t: (-t["days"], -t["strength"]))
    return candidates[:top_n]


def _theme_members(conn: sqlite3.Connection, theme_names: list[str]) -> dict[str, list[str]]:
    """反转 concept_cache（symbol→concepts JSON）得 {theme: [symbol,...]}，仅保留核心主题。"""
    theme_set = set(theme_names)
    members: dict[str, list[str]] = {}
    if not theme_set:
        return members
    try:
        rows = conn.execute("SELECT symbol, concepts FROM concept_cache").fetchall()
        for sym, concepts_json in rows:
            try:
                concepts = json.loads(concepts_json) if concepts_json else []
            except (json.JSONDecodeError, TypeError):
                continue
            for name in concepts:
                if name in theme_set:
                    members.setdefault(name, []).append(sym)
    except EXTERNAL_FAILURES as e:
        logger.warning(f"_theme_members failed: {e}")
        return members
    return members


def _dip_score(c: dict) -> int:
    """核心方向低吸候选的展示/去重分数（0-100，与 _low_buy_quality 单调一致）。

    仅用于存储排序与「同票跨扫描取最高分」去重，展示区排序仍按 _low_buy_quality 解析
    breakdown 重算，中间不带分数口径分歧。dict 值经 to_float 统一强转（脏值按 0 兑底，
    调用方为 DB 回读场景不可信）。
    """
    ff = c.get("flow_pct")
    if ff is None:
        flow_tier = 0
    elif ff >= 5:
        flow_tier = 2
    elif ff >= 0:
        flow_tier = 1
    elif ff < -5:
        flow_tier = -1
    else:
        flow_tier = 0
    today = to_float(c.get("today_pct")) or 0.0
    pullback = to_float(c.get("pullback"), default=0.0) or 0.0
    run = to_float(c.get("run"), default=0.0) or 0.0
    s = 50 + flow_tier * 8 - pullback * 250 + run * 80 - today * 120
    try:
        return int(max(0, min(100, round(s))))
    except (TypeError, ValueError):
        return 50


def save_core_dips(conn: sqlite3.Connection | None, dips: list[dict], today: str | None = None) -> None:
    """把核心方向低吸候选落库到 recommendations（category=CORE_DIP_CATEGORY）。

    同日在榜主列表外单独成 category（与 comeback 同族）：供 display 的独立低吸区读取，
    并进 prevday_perf/nextday_attribution 复盘验证。同日同 symbol 取最高分（记录当日
    "最佳低吸时刻"）；percent 存今日涨幅（推荐时刻涨幅，供次日归因）。
    全程 fail-open：落库失败不阻塞扫描主流程。
    """
    if conn is None or not dips:
        return
    today = today or now_beijing().date().isoformat()
    try:
        now = now_beijing().strftime("%H:%M:%S")
        for c in dips:
            score = _dip_score(c)
            percent = round(c["today_pct"] * 100, 2) if c.get("today_pct") is not None else None
            breakdown = json.dumps(
                {
                    "concept": c.get("concept"),
                    "run": round(c.get("run", 0), 4),
                    "pullback": round(c.get("pullback", 0), 4),
                    "below_ma20_ratio": round(c.get("below_ma20_ratio", 0), 4),
                    "flow_pct": c.get("flow_pct"),
                    "today_pct": c.get("today_pct"),
                },
                ensure_ascii=False,
            )
            existing = conn.execute(
                "SELECT id, score FROM recommendations WHERE date=? AND symbol=? AND category=? LIMIT 1",
                (today, c["symbol"], CORE_DIP_CATEGORY),
            ).fetchone()
            if existing:
                if score > existing[1]:
                    # score 列随更优时刻一并更新（2026-08-24 第二轮审查：原 UPDATE
                    # 漏写 score——breakdown/percent 已是更优低吸时刻，score 却留
                    # 首次插入的旧值，两字段互相矛盾且 ORDER BY score DESC 失真）
                    conn.execute(
                        "UPDATE recommendations SET time=?, score=?, percent=?, trend=?, "
                        "score_breakdown=?, concept=?, source=? WHERE id=?",
                        (now, score, percent, "主线回调", breakdown, c.get("concept"), "core_dip", existing[0]),
                    )
                continue
            conn.execute(
                "INSERT INTO recommendations (date, time, symbol, name, category, "
                "score, percent, trend, score_breakdown, source, concept) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    today,
                    now,
                    c["symbol"],
                    c.get("name", c["symbol"]),
                    CORE_DIP_CATEGORY,
                    score,
                    percent,
                    "主线回调",
                    breakdown,
                    "core_dip",
                    c.get("concept"),
                ),
            )
        conn.commit()
    except EXTERNAL_FAILURES as e:
        logger.warning(f"save_core_dips failed: {e}")


def _low_buy_quality(c: dict) -> tuple:
    """低吸质量排序键（升序，越小越优）：今日波动(涨多/跌狠) → 主力回流 → 回撤深 → 龙头强。

    2026-08-29 调整：今日波动剧烈（涨多/跌狠）优先排前（|today| 越大越靠前）；同幅度下
    主力回流 → 回撤深 → 龙头强。低吸语义：资金先于价格（主力转正是接盘确认），
    价格先于气势（同资金下回撤更深=更便宜），最后看龙头强度。
    flow_tier：>=5% 强流入 2 / >=0 转正 1 / <-5% 流出 -1 / 其余(含无数据) 0。
    today_pct 为比率（与 run/pullback 同口径），abs 取波动幅度，涨/跌两向极端均排前。
    """
    ff = c.get("flow_pct")
    if ff is None:
        flow_tier = 0
    elif ff >= 5:
        flow_tier = 2
    elif ff >= 0:
        flow_tier = 1
    elif ff < -5:
        flow_tier = -1
    else:
        flow_tier = 0
    today = c.get("today_pct") or 0.0
    # 今日波动剧烈（涨多/跌狠）优先：|today| 越大 → -abs(today) 越小 → 排前；
    # 同幅度下：主力回流(flow_tier 取负) → 回撤深(pullback 为负，越深越小) → 龙头强(run 取负)。
    return (-abs(today), -flow_tier, c["pullback"], -c["run"])


def core_stock_symbols(conn: sqlite3.Connection | None, today: str | None = None) -> set[str]:
    """当前「核心股」集合（2026-08-19）：属于核心方向（主线概念）且已走强（龙头属性）。

    判定 = identify_core_themes 识别核心主题 → _theme_members 取主题成员
    → 成员 20 日累计涨幅 ≥ CORE_RUN_MIN（走强/龙头属性）。

    **与核心低吸区（core_dip）的关系**：core_dip 候选必须满足「核心主题成员 + run ≥
    CORE_RUN_MIN + 低吸回撤窗口（-18%~-3% 等）」，因此 {core_dip 符号} ⊆ 本集合——
    本集合是其严格超集。综合排序/回马枪高亮用本集合而非 core_dip 列表：低吸区只含
    「回调中的核心股」，会漏掉创新高走强中的主线龙头（如 2026-08-19 江天化学，
    央国企改革主题成员、20 日累计 +22.3%，但距 20 日高点回撤 0% 落不进低吸窗口）。

    全程 fail-open：任何异常返回空集，不阻塞 display 主流程。
    """
    if conn is None:
        return set()
    today = today or now_beijing().date().isoformat()
    try:
        themes = identify_core_themes(conn, today)
        if not themes:
            return set()
        members = _theme_members(conn, [t["name"] for t in themes])
        all_syms = list(dict.fromkeys(s for syms in members.values() for s in syms))
        if not all_syms:
            return set()
        klines = get_cached_klines(conn, all_syms)
        out: set[str] = set()
        for sym in all_syms:
            k = klines.get(sym)
            if not k:
                continue
            m = _dip_metrics([b["close"] for b in k], [b["date"] for b in k], today)
            if m is None or m["run"] < CORE_RUN_MIN:
                continue
            out.add(sym)
        return out
    except EXTERNAL_FAILURES as e:
        logger.warning(f"core_stock_symbols failed: {e}")
        return set()


def find_core_theme_dips(conn: sqlite3.Connection | None, today: str | None = None) -> list[dict]:
    """核心方向低吸主入口：识别核心方向 → 找核心股 → 筛低吸窗口。

    返回候选列表（display 专用），每项：
      {symbol, name, concept, run(20日涨幅), pullback(距高点回撤), today_pct,
       below_ma20, flow_pct(主力净占比, 可 None)}
    已按 (主题排序, run 降序) 排序，并按 per-theme/total 上限裁剪。
    全程 fail-open：任何异常返回 []，不阻塞 display 主流程。
    """
    if conn is None:
        return []
    today = today or now_beijing().date().isoformat()
    try:
        themes = identify_core_themes(conn, today)
        if not themes:
            return []
        members = _theme_members(conn, [t["name"] for t in themes])
        all_syms = list(dict.fromkeys(s for syms in members.values() for s in syms))
        if not all_syms:
            return []
        klines = get_cached_klines(conn, all_syms)
        flow = get_fund_flow_pct_map(conn, all_syms)
        names = _symbol_names(conn, all_syms)

        cands: list[dict] = []
        for theme_name, syms in members.items():
            for sym in syms:
                k = klines.get(sym)
                if not k:
                    continue
                m = _dip_metrics([b["close"] for b in k], [b["date"] for b in k], today)
                if m is None:
                    continue
                if m["overheated"]:
                    continue
                if m["run"] < CORE_RUN_MIN:
                    continue
                if not (CORE_PULLBACK_MIN <= m["pullback"] <= CORE_PULLBACK_MAX):
                    continue
                # 今日企稳（非崩盘）：today_pct 是比率，×100 转 % 与 CORE_TODAY_FLOOR 同口径；
                # None（无今日 bar）不判崩盘，fail-open 放行（避免误杀数据缺口的票）
                if m["today_pct"] is not None and m["today_pct"] * 100 < CORE_TODAY_FLOOR:
                    continue
                # 未破位：允许跌破 MA20 不超过 CORE_MA20_BELOW_SLACK，过深=上涨结构坏
                if m["below_ma20_ratio"] > CORE_MA20_BELOW_SLACK:
                    continue
                flow_pct = flow.get(sym)
                if flow_pct is not None and flow_pct < CORE_FLOW_FLOOR:
                    continue
                cands.append(
                    {
                        "symbol": sym,
                        "name": names.get(sym, sym),
                        "concept": theme_name,
                        "run": m["run"],
                        "pullback": m["pullback"],
                        "today_pct": m["today_pct"],
                        "below_ma20_ratio": m["below_ma20_ratio"],
                        "flow_pct": flow_pct,
                    }
                )

        # 排序：低吸质量（主力回流→回撤深→龙头强→今日企稳），每主题限量 → 总限量。
        # 不按主题序：低吸区语义是「全场最优低吸位置顶」，主题列已标识归属，无需分组。
        # 同 symbol 跨多个核心主题（如激智科技∈华为概念∩电子）只保留质量最高的一次，
        # 避免重复行浪费展示名额。
        cands.sort(key=_low_buy_quality)
        out: list[dict] = []
        per_theme: dict[str, int] = {}
        seen_syms: set[str] = set()
        for c in cands:
            if len(out) >= CORE_THEME_MAX_TOTAL:
                break
            if c["symbol"] in seen_syms:
                continue
            k = c["concept"]
            if per_theme.get(k, 0) >= CORE_THEME_MAX_PER_THEME:
                continue
            per_theme[k] = per_theme.get(k, 0) + 1
            seen_syms.add(c["symbol"])
            out.append(c)
        return out
    except EXTERNAL_FAILURES as e:
        logger.warning(f"find_core_theme_dips failed: {e}")
        return []
