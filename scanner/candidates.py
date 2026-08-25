"""候选构建层（P2 从 orchestrator.py 抽出，2026-08-21）。

单只票的完整评分流水线：榜单原始数据过滤（filter_gem_stocks）→ 4 路策略
打分 + 交叉验证 + 价格结构分类（score_stock/try_candidate/classify_category）
→ 候选池级后处理（compute_rps / enrich_candidate_market_cap /
candidate_excluded_by_risk）。只依赖 analysis/validator/features/database 等
通用件，不感知扫描主循环；orchestrator.scan_with_raw 与
historical_rescan 是生产调用方。
"""

import math
import sqlite3
from dataclasses import replace as dataclass_replace

from scanner.analysis import analyze_momentum, analyze_new_face, analyze_rebound, analyze_short_term
from scanner.candidate_pool import ScanSession
from scanner.config import (
    FIRST_BREAKOUT_BONUS,
    FIRST_BREAKOUT_RANK_CHANGE,
    FIRST_BREAKOUT_VOL_RATIO,
    FIRST_TODAY_BONUS,
    HIGH_RISK_TRENDS,
    MOMENTUM_MIN_SCORE,
    NEW_FACE_FIRST_MIN_SCORE,
    NEW_FACE_LOOKBACK_DAYS,
    NEW_FACE_MIN_SCORE,
    REBOUND_MIN_SCORE,
    RISK_FLAGS_HARD_FILTER,
    RPS_BONUS_HIGH,
    RPS_BONUS_LOW,
    RPS_BONUS_MEDIUM,
    RPS_PCTILE_HIGH,
    RPS_PCTILE_LOW,
    RPS_PCTILE_MEDIUM,
    SHORT_TERM_MIN_SCORE,
    YI,
)
from scanner.database import get_symbol_appearances
from scanner.features import build_features
from scanner.models import Candidate, KlineBar, KlineSummary, StockInfo
from scanner.trading_session import is_trading_time
from scanner.utils import is_gem, is_hk_stock, is_st
from scanner.validator import validate


def build_candidate(stock: StockInfo, kline_summary: KlineSummary | None, category: str,
                     is_first_today: bool, first_date: str, kline: list[KlineBar] | None) -> Candidate:
    first_breakout = (stock.rank_change >= FIRST_BREAKOUT_RANK_CHANGE
                      and kline_summary.volume_ratio > FIRST_BREAKOUT_VOL_RATIO)
    return Candidate(
        stock=stock, category=category, score=kline_summary.score,
        reason=kline_summary.trend, kline=kline_summary,
        first_seen=first_date,
        first_today_bonus=FIRST_TODAY_BONUS if is_first_today else 0,
        first_breakout_bonus=FIRST_BREAKOUT_BONUS if first_breakout else 0,
        history_pct=[k["percent"] for k in kline] if kline else [],
    )


def try_candidate(stock: StockInfo, kline_summary: KlineSummary | None, category: str,
                   is_first_today: bool, first_date: str, kline: list[KlineBar] | None,
                   closes: list[float], historical: list[KlineBar],
                   clusters: dict[str, list[str]] | None,
                   feats: dict | None = None, today: str | None = None) -> Candidate | None:
    if kline_summary is None:
        return None
    if kline_summary.trend in HIGH_RISK_TRENDS:
        return None
    min_score = {
        # 首日 new_face 全档负收益提门槛砍量；known_new_face 分数反指（低分档最优）保持低门槛
        "new_face": NEW_FACE_FIRST_MIN_SCORE,
        "known_new_face": NEW_FACE_MIN_SCORE,
        "momentum": MOMENTUM_MIN_SCORE,
        "rebound": REBOUND_MIN_SCORE,
        "short_term": SHORT_TERM_MIN_SCORE,
    }[category]
    if kline_summary.score < min_score:
        return None
    passed, bonus, dims = validate(category, stock, kline_summary, closes, historical,
                                   clusters, feats, kline=kline, today=today)
    if not passed:
        return None
    new_dims = dict(kline_summary.dimensions)
    new_dims["validation_bonus"] = bonus
    new_dims.update(dims)
    # 2026-08-10: validation_bonus 全期 cum_3d IC -0.139（反指）——交叉验证只做通过门禁，
    # 加分不再进 score。bonus 仍写入 dims 供展示与 backtest dimension_ic 归因。
    kline_summary = dataclass_replace(kline_summary, dimensions=new_dims)
    return build_candidate(stock, kline_summary, category, is_first_today, first_date, kline)


def classify_category(stock: StockInfo, is_new: bool,
                       c_mo: Candidate | None,
                       c_nf: Candidate | None, c_st: Candidate | None = None,
                       c_rb: Candidate | None = None) -> str | None:
    """按价格结构（而非尝试顺序）选最贴合的策略标签。

    pullback 已于 2026-07-30 下线（回测 cum_2d 均亏 -8.33%，胜率 15.8%），
    classify_category 不再返回 "pullback"。
    """
    if is_new:
        if c_nf is not None:
            return "new_face"
        if c_rb is not None:
            return "rebound"
        if c_st is not None:
            return "short_term"
        if c_mo is not None:
            return "momentum"
        return None
    # 老股：超跌企稳优先归反弹；弱转强优先归超短；
    # 其它"非弱转强超短合格票"若同时过动量则归动量（避免掏空动量桶），
    # 仅过超短不过动量的票仍留超短（不丢票）。
    # 超跌反弹：前期暴跌+今日企稳阳线，优先于动量/超短（场景互斥，避免反弹票被误归动量）
    if c_rb is not None:
        return "rebound"
    st_is_wts = bool(c_st is not None and c_st.kline is not None
                     and c_st.kline.dimensions.get("st_weak_to_strong"))
    if c_st is not None and st_is_wts:
        return "short_term"
    if c_mo is not None:
        return "momentum"
    if c_st is not None:
        return "short_term"
    if c_nf is not None:
        return "known_new_face"
    return None


def new_face_sort_key(c: Candidate) -> float:
    """new_face 桶排序键：known_new_face 分数反指（低分档收益更好）→ 升序；new_face → 降序。

    与 display._score_sort_key 同口径，保证终端/飞书的新面孔列表与综合排序一致。
    """
    if c.category == "known_new_face":
        return c.score
    return -c.score


def filter_gem_stocks(raw: list[dict]) -> list[StockInfo]:
    gem_stocks: list[StockInfo] = []
    seen_symbols: set[str] = set()
    for i, item in enumerate(raw, 1):
        # symbol/code/name 强转 str：API 偶发返回 None（键存在但值为 null）或数值
        # 类型（int/float）时，is_hk_stock(None).isdigit() / is_gem(300001).startswith()
        # / is_st(None) 抛 AttributeError/TypeError，整轮扫描异常丢失。脏值统一转
        # 空串或 str（下游过滤/比对不崩）。
        symbol = str(item.get("symbol") or "")
        code = str(item.get("code") or "")
        name = str(item.get("name") or "")
        if is_hk_stock(symbol) or not is_gem(code) or is_st(name):
            continue
        # 去重：API 异常返回重复 symbol 时只保留首条，避免下游重复打分/显示
        if symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)
        # 数值强转：API 偶发返回字符串（如 rank_change="-"）时，保持字符串会让下游
        # 的 str vs float/int 比较抛 TypeError（s.current > MAX_STOCK_PRICE、
        # _vol_rank_combo_score 等），整轮扫描异常丢失。脏数据直接跳过该票。
        try:
            percent = float(item.get("percent") or 0.0)
            current = float(item.get("current") or 0.0)
            value = float(item.get("value") or 0.0)
            rc_val = float(item.get("rank_change") or 0)
            # rank 与 rank_change 同口径 float 中转：API 偶发返回 "5.0" 这类数值字符串时，
            # 直接 int("5.0") 抛 ValueError 会让整只票被跳过（漏推荐），float 中转则正常解析。
            rank_val = float(item.get("rank") or i)
        except (TypeError, ValueError):
            continue
        # NaN/inf 防御（Python json 默认解析 JSON 字面量 NaN/Infinity，与字符串脏值同族）：
        # NaN 与任何数值比较均为 False，会绕过 s.current > MAX_STOCK_PRICE / 涨幅档位判断，
        # 产出 NaN 评分并写库为 NULL（sqlite 把 NaN 存为 NULL）；inf 同理。统一按 0 处理
        # （与 api._num 口径一致）。rank/rank_change 为 NaN 时 int(nan) 抛 ValueError
        # 会让整只票被跳过（漏推荐），改回退默认值（0 / 列表下标）。
        if not math.isfinite(percent):
            percent = 0.0
        if not math.isfinite(current):
            current = 0.0
        if not math.isfinite(value):
            value = 0.0
        rank_change = int(rc_val) if math.isfinite(rc_val) else 0
        rank = int(rank_val) if math.isfinite(rank_val) else i
        # 换手率（2026-08-20）：与 rank_change 同族脏值（"-"/空串/NaN/inf）——非数字串
        # 时 float() 直接抛 ValueError（曾放 try 外，整批扫描崩溃）。此处 fail-soft 到
        # 0.0（仅停牌/僵尸识别用，不应整只票跳过）。bool 也算脏（float(True)=1.0）。
        tr_raw = item.get("turnover_rate")
        try:
            tr = float(tr_raw) if tr_raw not in (None, "", True, False) else 0.0
            turnover_rate = tr if math.isfinite(tr) else 0.0
        except (TypeError, ValueError):
            turnover_rate = 0.0
        gem_stocks.append(StockInfo(
            symbol=symbol, name=name, code=code,
            percent=percent, current=current, value=value,
            rank_change=rank_change, rank=rank,
            source_tag=item.get("source_tag", "xueqiu"),
            turnover_rate=turnover_rate,
        ))
    return gem_stocks


def score_stock(stock: StockInfo, conn: sqlite3.Connection, klines: dict[str, list[KlineBar] | None],
                 today: str, session_state: ScanSession,
                 clusters: dict[str, list[str]] | None = None,
                 now=None
                 ) -> tuple[Candidate | None, Candidate | None, Candidate | None,
                            Candidate | None]:
    """对单只票跑完 4 路引擎 + 交叉验证 + 分类，返回各桶候选 (new_face, momentum, rebound, short_term)。

    `today` 是本次扫描锚定的交易日；实时扫描传真实今日，历史回放
    （historical_rescan）传信号日。所有依赖「今天」的下游都由它驱动：
    appearances 回溯窗口（is_new）、analyze_* 的今日 bar 切分。
    `now` 仅用于盘中量能投影；回放时传当日收盘后时刻即可关闭投影，
    使结果不随「跑回测的时刻」漂移。
    """
    is_first_today = session_state.mark_seen(stock.symbol)
    app_history = get_symbol_appearances(conn, stock.symbol, NEW_FACE_LOOKBACK_DAYS, as_of=today)
    previous_dates = [a["date"] for a in app_history]
    is_new = len(previous_dates) == 0
    first_date = previous_dates[0] if previous_dates else today
    kline = klines.get(stock.symbol)
    historical = [k for k in (kline or []) if k["date"] != today]
    closes = [k["close"] for k in historical]

    # 统一特征抽取一次，下发给 5 路 analyze_* 与 validate，避免每只股票重复计算指标
    feats = None
    if len(closes) >= 5:
        highs = [k["high"] for k in historical]
        lows = [k["low"] for k in historical]
        volumes = [k["volume"] for k in historical]
        feats = build_features(closes, highs, lows, volumes)

    nk = analyze_new_face(stock, kline, today_str=today, features=feats, now=now)
    mk = analyze_momentum(stock, kline, today_str=today, features=feats, now=now)
    rk = analyze_rebound(stock, kline, today_str=today, features=feats, now=now)
    sk = analyze_short_term(stock, kline, today_str=today, features=feats, now=now)

    # 四策略独立打分 + 各自交叉验证，再按价格结构选最贴合的标签
    c_nf = try_candidate(stock, nk, "new_face" if is_new else "known_new_face",
                          is_first_today, first_date, kline, closes, historical, clusters, feats,
                          today=today)
    c_mo = try_candidate(stock, mk, "momentum",
                          is_first_today, first_date, kline, closes, historical, clusters, feats,
                          today=today)
    c_rb = try_candidate(stock, rk, "rebound",
                          is_first_today, first_date, kline, closes, historical, clusters, feats,
                          today=today)
    c_st = try_candidate(stock, sk, "short_term",
                          is_first_today, first_date, kline, closes, historical, clusters, feats,
                          today=today)

    # 审计标记（2026-08-14）：评分所用 K 线缺今日 bar（补拉失败旧缓存兜底）时打 stale_kline。
    # 缺今日 bar → 量比基于昨日量（vol_ratio 失真）→ 可能被量比硬门误杀（网宿案例）或
    # 基于旧数据误推。落库供事后审计"该推荐基于什么数据评分"。兜底已由 kline_fetch.fetch_all_klines
    # 的分时构造今日 bar 尽量消除，此处标记残留的兜底失败场景。
    # 非交易时段缓存本就停在最近交易日，缺今日 bar 属正常，不打 stale（与 fail-loud
    # 告警同口径：仅交易时段缺今日 bar 才是数据降级）。historical_rescan 直接调用本函数
    # 时 now_ref 若落在非交易时段同样不应误标。
    _stale = bool(kline) and max(k["date"] for k in kline) < today and is_trading_time()
    for _c in (c_nf, c_mo, c_rb, c_st):
        if _c is not None:
            _c.stale_kline = _stale
    # fail-loud：交易时段仍以缺今日 bar 旧缓存评分（日线补拉 + 分时兜底均失败）→ 逐票告警。
    # 不静默吞掉数据质量下降——这正是网宿类 bug 的隐蔽点（上游降级无感知，下游静默消费）。
    # 非交易时段缺今日 bar 属正常（缓存未更新），不告警。
    # 2026-08-20：停牌/僵尸股（turnover_rate==0，确无今日盘面）降级为 [~] 提示，不炸 [!]。
    if _stale and is_trading_time():
        if stock.turnover_rate == 0.0:
            print(f"  [~] {stock.name}({stock.symbol}) 停牌/僵尸股，今日无交易（旧缓存评分，非故障）")
        else:
            print(f"  [!] {stock.name}({stock.symbol}) 评分基于缺今日bar旧缓存（补拉与分时兜底均失败），"
                  f"量比/涨幅按昨日数据，可能误判")

    category = classify_category(stock, is_new, c_mo, c_nf, c_st, c_rb)
    if category == "short_term":
        return None, None, None, c_st
    if category in ("new_face", "known_new_face"):
        # 首板票若同时满足超短次日，双挂到超短列表（保留新面孔标签）
        if is_new and c_st is not None:
            return c_nf, None, None, c_st
        return c_nf, None, None, None
    if category == "momentum":
        return None, c_mo, None, None
    if category == "rebound":
        return None, None, c_rb, None
    return None, None, None, None


def compute_rps(candidates: list[Candidate],
                 baseline: list[float] | None = None,
                 accum_map: dict[str, float] | None = None) -> dict[str, int]:
    """计算 RPS 相对强弱加分。

    baseline: 全 GEM 监控集的累计涨幅列表（排名基准）。若提供，候选在其中排名，
    恢复 RPS「相对全市场强弱」本意；若不提供则退化为候选池内排名（旧行为）。
    accum_map: 候选 symbol → 历史5日累计涨幅（排除今日）。用于统一 RPS 口径：
    short_term 的 c.kline.accumulated_pct 包含今日（策略语义），与 baseline
    （排除今日）口径不一致，会导致百分位偏高。传入 accum_map 后所有候选用统一
    历史口径，与 baseline 一致。
    """
    scores: dict[str, int] = {}
    # 双挂票（同代码出现在多个桶）只计一次排名，避免拉高 total 扭曲分位
    seen: set[str] = set()
    uniq = [c for c in candidates if not (c.stock.symbol in seen or seen.add(c.stock.symbol))]
    candidates = uniq
    if len(candidates) < 2:
        return {c.stock.symbol: 0 for c in candidates}
    # 优先使用 accum_map 中的统一口径；回退到 c.kline.accumulated_pct
    cand_accum = []
    for c in candidates:
        if accum_map is not None and c.stock.symbol in accum_map:
            cand_accum.append(accum_map[c.stock.symbol])
        else:
            cand_accum.append(c.kline.accumulated_pct if c.kline else 0)
    if baseline:
        base_sorted = sorted(baseline)
        base_total = len(base_sorted)
        def _pctile(v: float) -> int:
            # 在基准分布中的百分位（0~100）
            lo = sum(1 for b in base_sorted if b <= v)
            return lo * 100 // base_total
        pctiles = [_pctile(v) for v in cand_accum]
    else:
        total = len(cand_accum)
        order = sorted(range(total), key=lambda i: cand_accum[i])
        pctiles = [0] * total
        for rank, i in enumerate(order):
            pctiles[i] = (rank + 1) * 100 // total
    for c, pctile in zip(candidates, pctiles):
        # 超跌反弹/回马枪 accumulated 为负必落底部分位，RPS_LOW 惩罚违背策略初衷，豁免
        if c.category in ("rebound", "comeback"):
            scores[c.stock.symbol] = 0
            continue
        if pctile >= RPS_PCTILE_HIGH:
            scores[c.stock.symbol] = RPS_BONUS_HIGH
        elif pctile >= RPS_PCTILE_MEDIUM:
            scores[c.stock.symbol] = RPS_BONUS_MEDIUM
        elif pctile < RPS_PCTILE_LOW:
            scores[c.stock.symbol] = RPS_BONUS_LOW
        else:
            scores[c.stock.symbol] = 0
    return scores


def enrich_candidate_market_cap(c: Candidate, cap_data: dict) -> None:
    """为候选补齐市值字段（榜上票与回马枪 off-list 票同口径）。

    - c.market_cap / c.circ_market_cap：元原始值（供行情侧/资金流查询门禁）
    - c.stock.market_cap：亿元（供 enhancer._apply_market_cap_bonus 的阈值比较）

    榜上票的 stock 对象在 _filter_gem 富集时已赋值 stock.market_cap；回马枪
    off-list 票的 StockInfo 由 evaluate_comeback 新建、market_cap 恒为 0，
    导致小市值加分系统性缺失（c.market_cap 是元原始值，与亿元阈值不是同一
    单位，不能替代）。统一在此按同口径补齐。
    """
    c.market_cap = cap_data.get("market_cap", 0)
    c.circ_market_cap = cap_data.get("circ_market_cap", 0)
    cmc = cap_data.get("circ_market_cap") or cap_data.get("market_cap", 0)
    if cmc > 0:
        c.stock.market_cap = cmc / YI


def candidate_excluded_by_risk(c: Candidate) -> bool:
    """命中硬排除风险标签的候选不进入推荐列表。

    集合见 config.RISK_FLAGS_HARD_FILTER（主力出货 / 趋势破位），
    二者均为明确的卖出 / 止损信号。其余标签（超买 / 涨幅过大 / 疲劳 /
    弱市 / 量价背离）保留为展示型警告，不在此过滤。
    """
    if not c.risk_flags:
        return False
    return bool(set(c.risk_flags) & RISK_FLAGS_HARD_FILTER)
