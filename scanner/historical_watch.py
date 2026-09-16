"""「v1 回捞」独立观察区（2026-09-16 新增）。

回答的问题与 v1 池选区**不同**，这是本模块存在的全部理由
--------------------------------------------------------
- v1 池选区问「今天榜上哪只明天可能大涨」：候选 = **今日在榜票**，引擎读当日榜单形态
  （`is_new` 由历史在榜记录决定、动量/反包/超短各自依赖当日涨幅与榜单位置）。
- 本区问「前几日进过 v1 的票，今天回调到位了吗」：候选 = **前 N 个交易日的 v1 产出**，
  引擎只读「今日涨跌幅 + 量能承接」，**完全不读今日榜单上下文**。

动机（用户需求）：飙升榜天然滞后——好票等上榜单时已经涨了一大截，追进去性价比差。
本区把「系统自己曾经认可过的票」留出一个二次观察窗，等它回调再说。

为什么不沿用 v1 筛选规则（实测证据，勿凭直觉推翻）
--------------------------------------------------
`is_new` 由「历史是否在榜」决定 ⇒ **只要一只票昨天进过 v1，它今天必然判不出
new_face**；momentum/rebound/short_term 同样要求当日榜单形态。2026-09-16 实测：
把 09-15 的 15 只 v1 票当候选、原样跑 v1 规则，**仅 1 只**过门（且属四路引擎全不采纳的
「过门未采纳」）。所以本区**不沿用** v1 引擎，改用一套只依赖价量、与榜单无关的规则。

规则与证据（2691 样本，2026-06→09，次日 ≥7% hit 口径）
------------------------------------------------------
| 口径 | n | hit | 相对全市场基线 |
|---|---|---|---|
| 全市场基线（同日同口径） | 41013 | 6.1% | — |
| 前 1~2 日 v1 票（不做其他筛选） | 1923 | 7.5% | +1.4pp |
| **+ 回调到位（今日 ≤ −3%）** | 395 | **11.1%** | **+5.0pp** |
| 交叉验证·训练段(06-01~08-01) | 170 | 12.4% | +6.1pp |
| 交叉验证·验证段(08-01~09-16) | 225 | 10.2% | +4.4pp |

两段独立成立 ⇒ 不是单期拟合。同一实验还给出两条**反直觉**结论，本区据此设计：
1. **缩量是负向的**：缩量档 hit 仅 4.3%（全部分档最低），平量 8.1%、放量 7.7%。
   缩量回调在本样本域里不是「惜售」而是「没人接」。既有 `comeback` 回踩变体把
   「缩量」列为 6 维买点信号之一，与该实证冲突 —— 本区不沿用那套信号。
2. **效应随距上次 v1 的交易日数单调衰减**：1 日 8.6% / 2 日 6.0% / 3 日 5.3%，
   与「飙升榜效应半衰期 3~5 个交易日」一致 ⇒ 回溯窗口取 2 日（3 日以上只会稀释）。

口径独立声明
------------
本区结果**不写 `recommendations`**、不参与复合评分/档位/🎯 画像、不参与 `portfolio_backtest`、
不进飞书推送的**去重键**（`feishu._view_symbols`：它决定「多久推一次」，分钟级刷新的行进去
会击穿节流）。终端与飞书卡片**都画这一节**（`view.render._render_hist_watch_region` /
`feishu.build_feishu_card`），两处共用同一份 `HistCandidate`。
排序键是启发式（见 config_historical 的权重说明），**未做样本外校准**，其可信度低于
`nextday_prob` 那条主线。它是「多一个观察窗口」，不是「多一条选股主线」。

展示标记（2026-09-16）
--------------------
行尾带**资金流图标**与**日线美感「美」**，判定全部复用主线单源：
`signals.fund_flow_signal`（阈值 FUND_FLOW_MAIN_PCT_*）与 `trend_beauty.evaluate_daily_trend`
（6 硬门）。三点口径差异，都是结构性的而非疏漏：
1. 资金流的 ▼▼（≤ -8%）在本区**不可达** —— 那一档已被硬门直接剔除（见 `hard_gate`）。
   所以这里的图标回答的是「-8% 以上这一段的强弱」，而不是「有没有出货」。
2. 美感**只有「美」一档，没有「美★」** —— 本区不抓分时数据，而 ★ 需要分时确认。
   刻意不回落库 score_breakdown：那是「上次推荐当日」的分时，拿它冒充今日会错标 ★。
3. 本区**不显示风险标签**（⚠超买/主力出货/趋势破位）—— 那些标签由候选引擎在扫描时写入
   (`enhancer`)，而本区不跑引擎（模块 docstring 开头已说明为什么不沿用 v1 规则）。
   要加就得在本区复刻一套风险判定，那是本仓最忌讳的「同名不同义」复制。

失败纪律
--------
只捕获 `EXTERNAL_FAILURES`（网络/超时/JSON 脏值），编程错误冒泡；
本区任何异常由调用方（`unified_scanner` 主循环）兜住，主线扫描不受影响。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from scanner.config import (
    HIST_DIP_BROKEN,
    HIST_DIP_BROKEN_FLOOR,
    HIST_DIP_IDEAL_LOW,
    HIST_DIP_PCT,
    HIST_DISPLAY_TOP,
    HIST_EXCLUDE_TODAY_RECS,
    HIST_LOOKBACK_DAYS,
    HIST_MAX_CANDIDATES,
    HIST_MAX_MARKET_CAP,
    HIST_MIN_ELAPSED_MIN,
    HIST_MIN_VOL_RATIO,
    HIST_RECENCY_DECAY,
    HIST_VOL_AVG_DAYS,
    HIST_VOL_FULL,
    HIST_W_DIP,
    HIST_W_RECENCY,
    HIST_W_VOL,
    HIST_WATCH_ENABLED,
    TREND_MARK_ENABLED,
    now_beijing,
)
from scanner.display_gates import beauty_marks_daily, code_of, common_hard_gate
from scanner.trading_session import trading_minutes_elapsed
from scanner.utils import EXTERNAL_FAILURES, to_float

logger = logging.getLogger(__name__)

# v1 五桶（与 scanner/config_scoring 的类别先验表同源口径：这五类共用一个 next_day 靶点）。
# core_dip / pool_pick / comeback 不在内：前者是展示/复盘用途，后两者是**掉榜票**域，
# 与「曾上过榜」的语义不同（comeback 回踩变体已在做自己的事，两区不交叉）。
V1_CATEGORIES: tuple[str, ...] = (
    "momentum",
    "new_face",
    "known_new_face",
    "rebound",
    "short_term",
)

# 当日交易分钟总数（A 股：09:30-11:30 + 13:00-15:00）。
_FULL_SESSION_MIN = 240


@dataclass
class HistCandidate:
    """通过全部门禁的历史 v1 票（纯内存，不落 recommendations）。"""

    symbol: str
    code: str
    name: str
    current: float
    percent: float
    vol_ratio: float
    rec_date: str  # 上一次进 v1 的交易日
    rec_days_ago: int  # 距该日的交易日数（1 = 昨天）
    rec_category: str  # 当时进的是哪个桶
    rec_score: int
    cum_pct: float  # 自 v1 日收盘以来的累计涨跌幅（%）
    market_cap: float
    # ── 展示标记（2026-09-16）──
    # 与主线**同一判定源**（signals.fund_flow_signal / trend_beauty.beauty_mark），
    # 本区只负责取值，成形（ANSI 三角形 / 卡片 emoji）留给各出口。
    # ff_pct：主力净占比（DB 当日快照）。注意硬门已剔除 ≤ FUND_OUTFLOW_NET_PCT(-8%)，
    #   故图标实际只可能落到 ▲▲ / ▲ / ▼ / 中性 四档，「▼▼」在本区结构上不可达。
    # beauty：**只有日线档**（"美" / ""）。本区没有分时数据，且**刻意不传** score_breakdown
    #   （库里那份是「上次推荐当日」的分时，拿来冒充今日会把「美」静默升级成「美★」）。
    ff_pct: float | None = None
    beauty: str = ""
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)


def _f(v, default: float = 0.0) -> float:
    """安全转 float：None/字符串/NaN → default（与 utils.to_float 同语义）。"""
    out = to_float(v, None)
    return default if out is None else out


# ── 候选域 ──────────────────────────────────────────────────────────────────


def collect_v1_history(conn, today: str, lookback_days: int = HIST_LOOKBACK_DAYS) -> list[dict]:
    """取前 N 个「有 v1 产出」交易日的票，同票保留最近一次记录。

    回溯单位是**交易日**而非自然日：以 `recommendations` 里实际有 v1 记录的日期倒序取，
    这样「距上次 v1 = 2」在跨周末时仍是 2 个交易日（与实盘的「隔了两天」直觉一致）。
    单票多次进 v1（连续两天入选）时保留**最近**那次 —— 距离越近证据越强（见模块 docstring）。

    失败返回 []（本区留空，不影响主线）。
    """
    if not conn:
        return []
    qs = ",".join("?" * len(V1_CATEGORIES))
    try:
        days = [
            r[0]
            for r in conn.execute(
                f"""SELECT DISTINCT date FROM recommendations
                    WHERE date < ? AND excluded = 0 AND category IN ({qs})
                    ORDER BY date DESC LIMIT ?""",  # noqa: S608 (列名/表名均为字面量)
                (today, *V1_CATEGORIES, lookback_days),
            )
        ]
        if not days:
            return []
        day_ago = {d: i + 1 for i, d in enumerate(days)}
        dqs = ",".join("?" * len(days))
        rows = conn.execute(
            f"""SELECT date, symbol, name, category, score FROM recommendations
                WHERE date IN ({dqs}) AND excluded = 0 AND category IN ({qs})
                ORDER BY date DESC, score DESC""",  # noqa: S608 (同上)
            (*days, *V1_CATEGORIES),
        ).fetchall()
    except EXTERNAL_FAILURES as e:
        logger.warning("historical_watch 候选查询失败，本区留空: %s", e)
        return []

    seen: set[str] = set()
    out: list[dict] = []
    for date, sym, name, cat, score in rows:
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(
            {
                "symbol": sym,
                "name": name or "",
                "rec_date": date,
                "rec_days_ago": day_ago.get(date, lookback_days),
                "rec_category": cat,
                "rec_score": int(score or 0),
            }
        )
    return out


# ── 量能 ────────────────────────────────────────────────────────────────────


def compute_vol_ratio(volume: float, hist_klines, elapsed_min: int) -> float | None:
    """时间归一化量比 = 投影全天量 / 前 N 日均量。不可判定时返回 None。

    为什么必须投影：盘中成交量只累积到当前时刻，直接比对日线均量会得到「早盘量比
    天然偏低」的假象（10:00 时全市场量比都会 <0.5）。`trading_minutes_elapsed`
    给出已过交易分钟，按线性比例投影回全天口径；收盘后 elapsed=240 即恒等映射，
    与离线回测口径逐个对齐。

    为什么用 max(elapsed, HIST_MIN_ELAPSED_MIN)：早盘量能分布极不均匀（开盘半小时
    成交占比远高于其时间占比），线性投影在 09:31 会放大约 240 倍。用下界把开盘
    半小时的倍数压住 —— 宁可偏保守（漏掉几只）也不要误放。
    """
    if volume <= 0 or not hist_klines or len(hist_klines) < HIST_VOL_AVG_DAYS:
        return None
    vols = [_f(k.get("volume")) for k in hist_klines[-HIST_VOL_AVG_DAYS:]]
    vols = [v for v in vols if v > 0]
    if len(vols) < HIST_VOL_AVG_DAYS:
        return None
    avg = sum(vols) / len(vols)
    if avg <= 0:
        return None
    eff = max(elapsed_min, HIST_MIN_ELAPSED_MIN)
    projected = volume * (_FULL_SESSION_MIN / eff)
    return projected / avg


# ── 硬门 ────────────────────────────────────────────────────────────────────


def hard_gate(meta: dict, quote: dict, vol_ratio: float | None, flow_pct: float | None) -> str | None:
    """必要条件（任一命中即剔除），返回排除原因 / 通过返回 None。

    分两层（2026-09-16 重构）：
      1. **通用风险门** —— ST / 样本面 / 报价 / 价格 / 市值 / 资金流出，走
         `display_gates.common_hard_gate`，与 v1 池选、沪深飙升**同一实现、同一阈值源**。
         市值上限显式收紧到 `HIST_MAX_MARKET_CAP`(500 亿)。
      2. **本区专有** —— 回调到位（≤ `HIST_DIP_PCT`）与量能承接（≥ `HIST_MIN_VOL_RATIO`）。
         这两条是本区的**取样定义**而非风险门：本区要的正是下跌中的票，与飙升区
         「必须上涨」方向相反，故不并入通用层。

    顺序按「先便宜后昂贵」编排：不查库、不依赖 K 线的判断排在前面，让绝大部分
    候选在拿到 K 线之前就被淘汰（本区唯一较贵的操作是按票取缓存 K 线）。
    """
    symbol = meta.get("symbol") or ""
    name = meta.get("name") or ""

    reason = common_hard_gate(
        name=name,
        code=symbol,
        current=_f(quote.get("current")),
        market_cap=_f(quote.get("market_capital")),
        ff_pct=flow_pct,
        max_market_cap=HIST_MAX_MARKET_CAP,
    )
    if reason:
        return reason

    # ── 本区专有：回调到位 + 量能承接 ──
    pct = _f(quote.get("percent"))
    if pct > HIST_DIP_PCT:
        return f"未回调到位({pct:+.2f}% > {HIST_DIP_PCT:.0f}%)"

    if vol_ratio is None:
        return "量能不可判定"
    if vol_ratio < HIST_MIN_VOL_RATIO:
        return f"量能萎缩(量比{vol_ratio:.2f} < {HIST_MIN_VOL_RATIO:.1f})"
    return None


# ── 评分（启发式排序键，非校准分）──────────────────────────────────────────


def dip_factor(pct: float) -> float:
    """回调深度因子 ∈ [BROKEN_FLOOR, 1]：−3%~−8% 满分，更深线性衰减。

    深于 HIST_DIP_IDEAL_LOW 的衰减不是「越深越好」的否定，而是形态判读：−8% 以内
    是回调，−15% 附近已是破位（本区要的是回调不是接刀）。
    """
    if pct > HIST_DIP_PCT:
        return 0.0
    if pct >= HIST_DIP_IDEAL_LOW:
        return 1.0
    span = HIST_DIP_IDEAL_LOW - HIST_DIP_BROKEN
    if span <= 0:
        return HIST_DIP_BROKEN_FLOOR
    frac = (pct - HIST_DIP_BROKEN) / span
    return max(HIST_DIP_BROKEN_FLOOR, min(1.0, frac))


def vol_factor(vol_ratio: float) -> float:
    """量能承接因子 ∈ [0,1]：量比越大越好，到 HIST_VOL_FULL 封顶（防巨量出货虚高）。"""
    return min(max(vol_ratio, 0.0) / HIST_VOL_FULL, 1.0)


def recency_factor(days_ago: int) -> float:
    """时效因子：距上次 v1 越近越好（实测 hit 随天数单调衰减）。"""
    return HIST_RECENCY_DECAY ** max(days_ago - 1, 0)


def compute_score(pct: float, vol_ratio: float, days_ago: int) -> float:
    return HIST_W_DIP * dip_factor(pct) + HIST_W_VOL * vol_factor(vol_ratio) + HIST_W_RECENCY * recency_factor(days_ago)


def build_reasons(c: HistCandidate) -> list[str]:
    """可读的入选理由（日志/JSON 消费，不参与排序）。"""
    rs = [f"{c.rec_days_ago}个交易日前进 v1（{c.rec_category}）"]
    rs.append(f"今日回调 {c.percent:+.2f}%")
    if c.cum_pct:
        rs.append(f"自 v1 日累计 {c.cum_pct:+.2f}%")
    rs.append(f"量比 {c.vol_ratio:.2f}（未缩量，有承接）")
    if c.ff_pct is not None:
        rs.append(f"主力净占比 {c.ff_pct:+.1f}%")
    if c.beauty:
        # 不写「走势漂亮」这种会让读者高估的措辞：trend_beauty 的分档实测显示
        # 「美」的 next_day hit 低于基线，它只表示**尾部回撤更小**。
        rs.append("日线趋势漂亮（尾部回撤更小，非更易大涨；本区无分时档）")
    return rs


# ── 单轮主流程 ──────────────────────────────────────────────────────────────


def run_historical_watch(
    conn,
    adapter,
    today: str | None = None,
    exclude_symbols: set[str] | None = None,
    top_n: int = HIST_DISPLAY_TOP,
) -> list[HistCandidate]:
    """跑一轮本区筛选，返回待展示的 TOP N。

    依赖注入 conn + adapter（与主循环同源），本区**不自己拉榜单**：候选来自
    `recommendations`（历史上系统自己写下的一票），这正是「回捞」的含义。

    exclude_symbols：今日已被 v1/v2 推荐的票 — 剔除，避免与 v1 池选区同屏重复展示
    （见 HIST_EXCLUDE_TODAY_RECS）。

    成本：批量行情 1~2 请求（几十只票一批 50），K 线走 DB 缓存批量读（零网络）。
    **不额外调 collect_market_extra**：主循环本轮已为全市场收集过资金流，历史 v1 票
    大多在缓存内即可命中；未命中时该门 fail-open 放过（宁可放过也不错杀，
    与 comeback/_passes_fund_flow_filter 同语义）。

    fail-open 边界（任一触发即本区留空，主线不受影响）：
      - 开关关闭 / 非交易时段（盘前拿不到当日涨跌幅）
      - 候选为空 / 行情接口不支持或全失败
      - 全部候选被硬门剔除（返回 []，终端整区跳过不留空表）
    """
    if not HIST_WATCH_ENABLED or conn is None:
        return []

    today = today or now_beijing().date().isoformat()
    elapsed = trading_minutes_elapsed()
    if elapsed <= 0:
        # 盘前（09:30 之前）与非交易日：`percent` 无当日含义、量能也投影不出来。
        # 本区不是「隔夜挂单参考」，没有当日行情就没有判断依据 —— 直接留空。
        return []

    metas = collect_v1_history(conn, today, HIST_LOOKBACK_DAYS)
    if HIST_EXCLUDE_TODAY_RECS and exclude_symbols:
        metas = [m for m in metas if m["symbol"] not in exclude_symbols]
    if not metas:
        return []
    metas = metas[:HIST_MAX_CANDIDATES]

    fetch_batch = getattr(adapter, "fetch_hot_quotes_batch", None)
    if fetch_batch is None:
        return []
    symbols = [m["symbol"] for m in metas]
    try:
        quotes = fetch_batch(symbols)
    except EXTERNAL_FAILURES as e:
        logger.warning("historical_watch 批量行情失败，本区留空: %s", e)
        return []
    if not quotes:
        return []

    from scanner.db.queries import get_cached_klines, get_fund_flow_pct_map

    try:
        klines_map = get_cached_klines(conn, symbols)
    except EXTERNAL_FAILURES as e:
        logger.warning("historical_watch K 线缓存读取失败，本区留空: %s", e)
        return []
    try:
        flow_map = get_fund_flow_pct_map(conn, symbols)
    except EXTERNAL_FAILURES as e:
        logger.warning("historical_watch 资金流读取失败（该门跳过）: %s", e)
        flow_map = {}

    return evaluate(metas, quotes, klines_map, flow_map, elapsed, today, top_n)


def evaluate(
    metas: list[dict],
    quotes: dict[str, dict],
    klines_map: dict,
    flow_map: dict[str, float],
    elapsed_min: int,
    today: str,
    top_n: int = HIST_DISPLAY_TOP,
) -> list[HistCandidate]:
    """纯函数：给定候选/行情/K线/资金流/已过交易分钟 → 通过全门的 TOP N。

    与取数解耦（conn/adapter 侧在 `run_historical_watch`），使规则能被单测和离线自检
    直接驱动 —— 不需要 DB、不需要网络，规则改动的验证成本降到毫秒级。
    """
    out: list[HistCandidate] = []
    for m in metas:
        sym = m["symbol"]
        q = quotes.get(sym)
        if not q:
            continue
        # 量比的分母只取**历史** bar：缓存里今天的 bar 是盘中累积值，
        # 混进去会让「今日量 vs 含今日的均量」自我参照，量比被系统性压低。
        hist = [k for k in (klines_map.get(sym) or []) if k.get("date") != today]
        vr = compute_vol_ratio(_f(q.get("volume")), hist, elapsed_min)
        flow_pct = flow_map.get(sym)
        reason = hard_gate(m, q, vr, flow_pct)
        if reason:
            logger.debug("historical_watch 剔除 %s(%s): %s", m.get("name"), sym, reason)
            continue

        current = _f(q.get("current"))
        cum_pct = 0.0
        for k in hist:
            if k.get("date") == m["rec_date"]:
                rc = _f(k.get("close"))
                if rc > 0:
                    cum_pct = (current - rc) / rc * 100
                break

        # 走势美感（展示标记，不改门禁/排序/评分）：走 display_gates.beauty_marks_daily，
        # 与飙升区**同一次判定、同一准入条件**（日线 6 硬门）。本区只取标记、不用其
        # blocked 半（本区不设美感门），且结构上只会出现「美」——
        # 该函数不做分时分级，故不会出现「美★」；也不回落库 score_breakdown（那是
        # 「上次推荐当日」的分时，拿来冒充今日就是错标）。
        # 用含今日的完整 K 线（与主线 assemble 同口径）：今日这根 bar 正是「回调」本身，
        # 排除它会让「近 5 日无暴跌」等硬门看不到今天的跌幅。
        beauty = beauty_marks_daily(klines_map.get(sym))[1] if TREND_MARK_ENABLED else ""

        c = HistCandidate(
            symbol=sym,
            code=code_of(sym),
            name=m["name"],
            current=current,
            percent=_f(q.get("percent")),
            vol_ratio=vr or 0.0,
            rec_date=m["rec_date"],
            rec_days_ago=int(m["rec_days_ago"]),
            rec_category=m["rec_category"],
            rec_score=int(m["rec_score"]),
            cum_pct=round(cum_pct, 2),
            market_cap=_f(q.get("market_capital")),
            ff_pct=None if flow_pct is None else _f(flow_pct),
            beauty=beauty,
        )
        c.score = compute_score(c.percent, c.vol_ratio, c.rec_days_ago)
        c.reasons = build_reasons(c)
        out.append(c)

    # 评分降序；同分按回调更浅者优先（同等条件下少亏一点）
    out.sort(key=lambda x: (-x.score, -x.percent))
    return out[:top_n]


# ── 离线自检 + 独立运行 CLI（python -m scanner.historical_watch）─────────────
# 主循环内本区随扫描自动跑；此入口用于**改规则后的回归自检**与临时查看：
#   --offline-demo 不联网、不读库，用内置样本跑通「硬门→量比→评分→排序」并逐条核对，
#                  任一条不符即退出码 1。它是本区规则的回归哨兵。

_DEMO_AVG_VOL = 1_000_000.0  # 前 5 日日均量（股）：量比 = volume / 该值（elapsed=240 时）


def _demo_hist(close: float = 10.0):
    """固定 5 根历史 bar（含 rec_date），日均量恒为 _DEMO_AVG_VOL。"""
    return [
        {"date": d, "close": close, "volume": _DEMO_AVG_VOL}
        for d in ("2026-09-09", "2026-09-10", "2026-09-11", "2026-09-14", "2026-09-15")
    ]


def _demo_meta(symbol: str, name: str, days_ago: int = 1, cat: str = "momentum") -> dict:
    return {
        "symbol": symbol,
        "name": name,
        "rec_date": "2026-09-15",
        "rec_days_ago": days_ago,
        "rec_category": cat,
        "rec_score": 60,
    }


def _demo_quote(symbol: str, current: float, percent: float, vol: float, cap: float = 5e9) -> dict:
    return {
        "symbol": symbol,
        "code": symbol[2:] if len(symbol) > 6 else symbol,
        "current": current,
        "percent": percent,
        "volume": vol,
        "market_capital": cap,
    }


# 期望列：pass = 应通过；其余为应命中的排除原因关键词。
# 覆盖每一道硬门 + 量能不可判定，改动任一阈值后本表应逐条仍然成立。
_DEMO_CASES: list[tuple[str, dict, dict, bool]] = [
    # (期望, meta, quote, 是否有 K 线)
    ("pass", _demo_meta("SZ300806", "斯迪克"), _demo_quote("SZ300806", 9.50, -5.0, 1.5e6), True),
    (
        "pass",
        _demo_meta("SZ300319", "麦捷科技", days_ago=2, cat="short_term"),
        _demo_quote("SZ300319", 9.20, -8.0, 2.5e6),
        True,
    ),
    ("未回调到位", _demo_meta("SZ301251", "威尔高"), _demo_quote("SZ301251", 10.10, -1.0, 1.5e6), True),
    ("未回调到位", _demo_meta("SZ300490", "华自科技"), _demo_quote("SZ300490", 10.50, 3.2, 1.5e6), True),
    ("量能萎缩", _demo_meta("SZ300862", "蓝盾光电"), _demo_quote("SZ300862", 9.60, -4.0, 0.5e6), True),
    ("量能不可判定", _demo_meta("SZ300684", "中石科技"), _demo_quote("SZ300684", 9.60, -4.0, 1.5e6), False),
    ("ST", _demo_meta("SZ300123", "*ST测试"), _demo_quote("SZ300123", 9.60, -5.0, 1.5e6), True),
    ("非创业板", _demo_meta("SZ002443", "金洲管道"), _demo_quote("SZ002443", 9.60, -5.0, 1.5e6), True),
    ("无有效报价", _demo_meta("SZ300456", "停牌测试"), _demo_quote("SZ300456", 0.0, -5.0, 1.5e6), True),
    ("价格过高", _demo_meta("SZ300789", "高价测试"), _demo_quote("SZ300789", 250.0, -5.0, 1.5e6), True),
    ("市值过大", _demo_meta("SZ300750", "宁德时代"), _demo_quote("SZ300750", 9.60, -5.0, 1.5e6, cap=6e10), True),
    ("主力净流出", _demo_meta("SZ300999", "流出测试"), _demo_quote("SZ300999", 9.60, -5.0, 1.5e6), True),
]

_DEMO_FLOW = {"SZ300999": -9.5}  # 主力净占比 ≤ FUND_OUTFLOW_NET_PCT(-8.0) → 剔除


def evaluate_demo(metas, quotes, klines_map, flow_map, today="2026-09-16", elapsed_min=240):
    """离线自检的规则驱动（固定 elapsed=240 = 收盘后口径，与离线回测口径一致）。"""
    return evaluate(metas, quotes, klines_map, flow_map, elapsed_min, today, top_n=99)


def run_offline_demo(emit_json: bool) -> int:
    """离线自检：不联网、不读库，内置样本逐条核对期望。全绿退出码 0，任一条不符为 1。"""
    metas = [m for _, m, _q, _k in _DEMO_CASES]
    quotes = {q["symbol"]: q for _, _m, q, _k in _DEMO_CASES}
    klines_map = {m["symbol"]: _demo_hist() for _, m, _q, has_k in _DEMO_CASES if has_k}
    passed = evaluate_demo(metas, quotes, klines_map, _DEMO_FLOW)
    passed_syms = {c.symbol for c in passed}

    print("【离线自检】不联网·不读库，内置样本逐条核对")
    print(f"{'代码':<10}{'名称':<12}{'期望':<16}{'实际':<10}结果")
    print("-" * 62)
    ok = True
    for expect, m, q, _has_k in _DEMO_CASES:
        sym = m["symbol"]
        if expect == "pass":
            actual = "通过" if sym in passed_syms else "未通过"
            good = sym in passed_syms
        else:
            # 未通过者逐条复算原因（复用同一套门，保证展示与实际判定同源）
            hist = list(klines_map.get(sym) or [])
            vr = compute_vol_ratio(_f(q.get("volume")), hist, 240)
            actual = hard_gate(m, q, vr, _DEMO_FLOW.get(sym)) or ("通过" if sym in passed_syms else "缺失")
            good = sym not in passed_syms and expect in actual
        ok = ok and good
        print(f"{q['code']:<10}{m['name']:<12}{expect:<16}{actual:<10}{'OK' if good else 'FAIL'}")
    print("-" * 62)
    print(f"通过 {len(passed)} 只 / 样本 {len(_DEMO_CASES)} 条")

    if emit_json:
        print(rows_to_json(passed))
    return 0 if ok else 1


def rows_to_json(rows) -> str:
    """紧凑 JSON（便于程序消费/排查）。"""
    import json

    return json.dumps(
        [
            {
                "code": c.code,
                "symbol": c.symbol,
                "name": c.name,
                "current": round(c.current, 3),
                "percent": round(c.percent, 2),
                "vol_ratio": round(c.vol_ratio, 2),
                "rec_date": c.rec_date,
                "rec_days_ago": c.rec_days_ago,
                "rec_category": c.rec_category,
                "cum_pct": c.cum_pct,
                "market_cap_yi": round(c.market_cap / 1e8, 2),
                # 行尾展示标记（2026-09-16）：JSON 是排查接口，标记必须可见，
                # 否则「终端有 ▼ 卡片没有」这类问题在离线输出里无从对照。
                "ff_pct": None if c.ff_pct is None else round(c.ff_pct, 2),
                "beauty": c.beauty,
                "score": round(c.score, 2),
                "reasons": c.reasons,
            }
            for c in rows
        ],
        ensure_ascii=False,
        indent=2,
    )


def main(argv: "list[str] | None" = None) -> int:
    import argparse
    import sqlite3

    p = argparse.ArgumentParser(
        prog="python -m scanner.historical_watch",
        description="v1 回捞（独立区·独立运行；主循环内已自动执行，此入口用于自检）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--top", type=int, default=HIST_DISPLAY_TOP, help="展示前 N 只")
    p.add_argument("--json", action="store_true", help="额外输出 JSON")
    p.add_argument("--offline-demo", action="store_true", help="离线自检：内置样本，不联网")
    p.add_argument("--date", default=None, help="覆盖扫描日（默认今天）")
    p.add_argument("--verbose", action="store_true", help="调试日志")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="[%(levelname)s] %(message)s",
    )

    if args.offline_demo:
        return run_offline_demo(args.json)

    from scanner.config import DB_PATH
    from scanner.data_source import get_adapter

    # 只读连接：本区不写库（既不落 recommendations 也不写自己的表），只读打开即可，
    # 免去与常驻扫描进程抢 SQLite 写锁的风险。
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        adapter = get_adapter()
        rows = run_historical_watch(conn, adapter, today=args.date, top_n=args.top)
    finally:
        conn.close()

    if not rows:
        print("  本轮无标的通过筛选（无候选 / 全被硬门剔除 / 非交易时段）")
        return 0
    if args.json:
        print(rows_to_json(rows))
    else:
        from scanner.display import render_hist_watch_standalone

        render_hist_watch_standalone(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
