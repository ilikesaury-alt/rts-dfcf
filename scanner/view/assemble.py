import statistics

from scanner.config import (
    CAT_DISPLAY_PRIORITY,
    COMEBACK_DISPLAY_MAX,
    COMEBACK_DISPLAY_MIN_MAIN,
    CORE_DIP_CATEGORY,
    CORE_PULLBACK_MAX,
    CORE_PULLBACK_MIN,
    DISPLAY_MAX_TODAY_PCT,
    FINAL_PICK_ENABLED,
    FUND_FLOW_HARD_FILTER_ENABLED,
    TACTICS_SELL_TAGS,
    TREND_MARK_ENABLED,
)
from scanner.core_themes import core_stock_symbols
from scanner.database import (
    get_cached_klines,
    get_fund_flow_pct_map,
    get_today_recommendations,
)
from scanner.models import V2_CATEGORY, Candidate, RecommendationRow
from scanner.nextday_rule import scan_rule

# 排序/画像纯逻辑单源在 scanner.ranking；display 只导入渲染所需子集。
# 此前的全量 re-export（供 scripts 的 display._entry_* 属性访问）已下线：
# 消费方直接 import scanner.ranking（scripts/review_tier_replay.py 已改）。
from scanner.ranking import (
    _breakout_profile_key,
    _breakout_structure_ok,
    build_accum_map,
    build_breakout_kline_map,
    comeback_sort_key,
    composite_score,
    composite_tier,
    entry_dims,
    fresh_candidate,
    is_fund_outflow,
    is_nextday_marked,
)

# 走势美感标记判定单源在 scanner.trend_beauty（日线定准入、分时定级别）——经下方
# `from scanner.view.model import *` 带入 _beauty_mark_for，本模块不重复持有判定逻辑。
from scanner.utils import EXTERNAL_FAILURES, to_float
from scanner.view.model import *  # noqa: F401,F403

# ANSI 探测（_is_console / _supports_ansi）/ ANSI / CAT_COLOR / _ANSI_ESCAPE 的单源在
# scanner.view.model —— 上方 `from scanner.view.model import *` 已带入，本模块不再重复定义。
# 2026-09-14 去重：拆分脚本曾把这段 Windows 终端探测头原样复制进三个文件，后果有二：
#   ① SetConsoleMode 被重复调用三次（无害但无谓）；
#   ② `__all__` 由「AST 模块级名 ∩ dir()」推导，把仅 Windows 分支存在的
#      _kernel32/_handle/_mode 也写进了导出表 —— Linux/macOS 下
#      `from scanner.view.model import *` 会因 __all__ 缺名直接 AttributeError。


__all__ = (
    "ANSI",
    "CAT_COLOR",
    "_ANSI_ESCAPE",
    "_adjusted_picks",
    "_is_console",
    "_regime_weak",
    "_supports_ansi",
    "build_scan_view",
)


def _regime_weak(conn, lookback=10):
    """近端主表档(非 comeback/core_dip)次日表现均值 < 0 → 弱市(regime 退潮)。
    纯展示层用：驱动「动态推荐」区域是否在弱市下剔除动量/kNF 类🎯。
    fail-open：无数据/查询异常时返回 False(按强市处理，不误删推荐)。

    注意：OFFSET 必须作用于 DISTINCT date，否则多个推荐行共享同一 date 会使
    OFFSET 始终落在最近一日块内、date>cutoff 恒为空而 fail-open 误判为强市。
    采用逐交易日均值(每个交易日等权)，避免单日高推荐量主导符号。
    """
    try:
        row = conn.execute(
            "SELECT DISTINCT date FROM recommendations ORDER BY date DESC LIMIT 1 OFFSET ?",
            (lookback - 1,),
        ).fetchone()
        if not row:
            return False
        rows = conn.execute(
            "SELECT date, next_day_pct FROM recommendations WHERE date > ? "
            "AND category NOT IN ('comeback','core_dip') AND next_day_pct IS NOT NULL",
            (row[0],),
        ).fetchall()
        by_date: dict = {}
        for d, p in rows:
            by_date.setdefault(d, []).append(p)
        daily_means = [statistics.mean(v) for v in by_date.values()]
        if not daily_means:
            return False
        return statistics.mean(daily_means) < 0.0
    except EXTERNAL_FAILURES:
        # 2026-08-29：原为裸 except Exception——DB/数据类异常与编程错误一律吞成
        # 「强市」，fail-open 语义因此不可信（且掩盖真实故障）。无数据/样本不足的
        # fail-open 由上方 `if not row` / `if not daily_means` 显式分支承担，不靠捕获。
        # sqlite3.Error 属 EXTERNAL_FAILURES；编程错误冒泡到主循环记录 traceback。
        return False


def _adjusted_picks(today_recs, nextday_mark, conn, flow_pct_map, top_n=10, weak=None):
    """regime 自适应推荐序列（纯展示，不改落库/评分/排序）。

    弱市：剔除 momentum/known_new_face/new_face 全类（🎯 与否均剔），按系统自有质量信号精选
    核心低吸(_core_dip_entry_quality) / 回马枪(comeback_sort_key) / 🎯(rebound·弱转强) 各桶
    最前少数几只——而非整个桶堆叠；强市：🎯 前置、其余按评分。返回 (名称, 类别, 是否🎯) 列表。

    weak: 弱市标志。None 时内部自算 _regime_weak(conn)；调用方（display_priority）
    预计算后传入可省掉同轮第二次 SQL（此前每轮跑两遍同一查询）。
    """
    if weak is None:
        weak = _regime_weak(conn)
    core_dip, comeback, yt, other = [], [], [], []
    for e in today_recs:
        sym, cat = e["symbol"], e["category"]
        marked = nextday_mark.get((sym, cat), False)
        if cat == "core_dip":
            sb = entry_dims(e)
            if sb.get("run") is None or sb.get("pullback") is None:
                continue
            core_dip.append(e)
        elif cat == "comeback":
            comeback.append(e)
        elif marked and cat in ("rebound", "short_term"):
            yt.append(e)
        elif weak and cat in ("momentum", "known_new_face", "new_face"):
            continue
        else:
            other.append(e)
    core_dip.sort(key=_core_dip_entry_quality)
    comeback.sort(key=lambda x: comeback_sort_key(x, flow_pct_map))
    yt.sort(key=lambda x: -to_float(x.get("score"), default=0.0))
    other.sort(key=lambda x: -to_float(x.get("score"), default=0.0))
    if weak:
        ordered = core_dip[:4] + comeback[:3] + yt[:2] + other[:1]
    else:
        ordered = yt[:3] + other[:3] + core_dip[:2] + comeback[:2]
    return [(e["name"], e["category"], nextday_mark.get((e["symbol"], e["category"]), False)) for e in ordered[:top_n]]


def build_scan_view(
    conn=None,
    live_quotes: dict[str, dict] | None = None,
    rank_map: dict[str, int] | None = None,
    today_pool: dict[str, Candidate] | None = None,
    last_ranks: dict[str, int] | None = None,
    weak: bool | None = None,
    hot_rows: list | None = None,
):
    """构建一次扫描的展示视图（纯计算，不 print、不写库）：读今日推荐并算出档位/标记/排序。

    返回值供 render_terminal / 飞书卡片共用，保证各出口看到同一份选择。
    无 conn 或今日无推荐时返回 None（由调用方决定是否渲染）。

    live_quotes: {symbol: {percent, current}} 实时行情覆盖，优先于候选池和数据库数据。
    rank_map: {symbol: 飙升榜排名} 当前扫描的榜单排名，为掉榜/重启行补实时排名。
    today_pool: {symbol: Candidate} 本轮候选池快照（缺省空），供掉榜/重启行之外的行
    渲染最新候选数据（实时候选 > DB 快照）。
    last_ranks: 上一轮扫描的榜单排名 {symbol: rank}，供「排名」列显示雪球榜单排名变化
    （+N 升 / -N 降），与已下线策略桶同口径；缺省 None 不显示变化。

    v1 池选排序键（2026-08-30）：榜上优先 → 涨幅升序 → 回调核心 → 排名升序 → 新面孔。
    🎯（次日大涨画像）/⚡（蓄势突破观察）为行尾展示标记，不参与排序、不改评分、不落库。
    """
    if conn is None:
        return None

    # 降级告警收集器：计算阶段不 print，统一由 render_terminal 输出。
    warnings: list[str] = []

    # 盘中操作纪律：全局时段提醒（纯展示，fail-open）
    try:
        from scanner.intraday_tactics import session_advice

        _advice = session_advice()
        if _advice:
            warnings.append(_advice)
    except EXTERNAL_FAILURES:
        pass

    today_recs = get_today_recommendations(conn)
    if not today_recs:
        return None

    today_pool = today_pool or {}
    for entry in today_recs:
        pool_c = today_pool.get(entry["symbol"])
        entry["_candidate"] = pool_c

    if live_quotes:
        for entry in today_recs:
            q = live_quotes.get(entry["symbol"])
            if q is not None:
                # live_quote_available：标记该行拿到了本次实时批量行情（live_percent=0.0
                # 是合法的 0.00%，不能当作缺失；get_today_recommendations 默认填 0.0 需区分）。
                entry["live_quote_available"] = True
                entry["live_percent"] = q.get("percent", 0.0)
                entry["live_current"] = q.get("current", 0.0)
                q_rank = q.get("rank")
                if q_rank is not None:
                    entry["live_rank"] = q_rank

    # 排名实时覆盖：live_quotes（batch/quote）不含 rank，用当前飙升榜排名补上，
    # 使综合排序「排名」列对仍在上榜的票实时可见（掉榜/重启行此前恒为 —）。
    if rank_map:
        for entry in today_recs:
            if entry.get("live_rank") is None:
                r = rank_map.get(entry["symbol"])
                if r is not None:
                    entry["live_rank"] = r

    # 辨识度（↻）行内标记已下线（2026-08-22 标记精简）：prom_map/_prominent 预计算链路
    # 随之移除；get_prominence_map 仍被 today_report 归因使用，不受影响。

    # 资金流图标：从 market_extra_cache 直接读当日资金流，不依赖当前进程 today_pool。
    # 候选存在时优先用其扫描时的最新维度，否则（重启/掉榜/扫描时拉取失败）回退到 DB
    # 保存的全市场快照——避免综合排序大量行因进程重启丢失资金流图标。
    flow_pct_map = get_fund_flow_pct_map(conn, [e["symbol"] for e in today_recs])

    # ── 展示层资金流出硬门（2026-09-14 统一口径，**单一入口**）──
    # 判定单源 ranking.is_fund_outflow（阈值 config_sources.FUND_OUTFLOW_NET_PCT = -8.0%，
    # 回退链：行内 dims/score_breakdown → flow_pct_map 当日全市场快照）。
    # 在此处过滤 `today_recs` 一次，下游全部派生集合（main_recs / pool_pick_recs /
    # comeback_recs / core_dip_recs / adj_picks / 终选输入）自动继承——终端与飞书同源
    # （feishu 只读 build_scan_view 产出的同一份 ScanView），不会再出现「终选区剔了、
    # 上方池选区还在」的同屏口径分叉。
    # ⚠ 刻意**不改 excluded 标记、不写库**：excluded=1 会改回测 / nextday_attribution /
    # prevday_perf 的样本口径（load_attribution_rows 取 excluded=0），把展示层语义泄漏
    # 进历史基线。RTS_FUND_FLOW_HARD_FILTER=0 关闭本门。
    flow_filtered = 0
    if FUND_FLOW_HARD_FILTER_ENABLED:
        _kept_recs: list[RecommendationRow] = []
        for _e in today_recs:
            if is_fund_outflow(_e, flow_pct_map):
                flow_filtered += 1
                continue
            _kept_recs.append(_e)
        today_recs = _kept_recs
        # 全被剔时不提前返回：继续走完终选区（它可能给出「门关 + 原因」，
        # 比直接少一整块输出更可诊断），只是各展示区天然为空。
        # 比直接少一整块输出更可诊断），只是各展示区天然为空。

    # 🎯 标记预计算（2026-08-14 起 map 化：排序+渲染各调一次 is_nextday_marked 会触发
    # 两次 daily_kline 回放全表扫描；预计算后只查一次，判定与行尾渲染共用同一结果）。
    # 键 (symbol, category) 复合——nf∩st 双挂票两行判定口径不同，按 symbol 键控时归属
    # 取决于遍历顺序（隐式依赖）。
    nextday_mark: dict[tuple[str, str], bool] = {}
    # P1-9（2026-08-20）：全部推荐一次性批量回放累计（build_accum_map 单查询），
    # 替代逐行 _nextday_entry_accum 的 N+1 daily_kline 查询。
    accum_map = build_accum_map(conn, today_recs)
    for e in today_recs:
        nextday_mark[(e["symbol"], e["category"])] = is_nextday_marked(e, conn, accum_map=accum_map)
    # 双挂票归一（用户确认）：同 symbol 存在 short_term 行时，其余类别行沿用 st 行的
    # 🎯 判定，与「nf∩st 双挂恒存 short_term」的池内事实对齐。
    st_marks = {(s, c): v for (s, c), v in nextday_mark.items() if c == "short_term"}
    for key in list(nextday_mark):
        st_key = (key[0], "short_term")
        if key[1] != "short_term" and st_key in st_marks:
            nextday_mark[key] = st_marks[st_key]

    # 回马枪独立成区（2026-08-07 方案A）：comeback 是 off_list 掉榜跟踪票，语义与榜上票不同，
    # 从主排序表抽出放到末尾独立区块；主表只排榜上五类（rebound/known_new_face/new_face/
    # momentum/short_term，不含 comeback/pullback）。
    comeback_recs = [e for e in today_recs if e["category"] == "comeback"]
    core_dip_recs = [e for e in today_recs if e["category"] == CORE_DIP_CATEGORY]
    # 双跑同屏（2026-09-02 用户确认）：主表显 v1 五桶；v2 pool_pick 原独立成区
    # （两套排序口径不同：v1 档位序 / v2 涨幅降序，合并单表会破坏各自语义）。
    # 2026-09-14：v2 池选展示区已隐藏，但 pool_pick_recs 仍单独取出——它是终选参考区
    # 合池输入之一，混进 main_recs 会同时改变 v1 主表内容与终选结果。
    # RTS_PIPELINE 不再影响显示层。
    main_recs = [e for e in today_recs if e["category"] not in ("comeback", CORE_DIP_CATEGORY, V2_CATEGORY)]
    pool_pick_recs = [e for e in today_recs if e["category"] == V2_CATEGORY]

    # 核心股高亮（2026-08-19）：综合排序/回马枪列表里属于当前主线方向核心股的票，
    # 名称加粗高亮。**判定 = core_stock_symbols（核心主题成员 + 20日累计≥CORE_RUN_MIN
    # 走强龙头），不用 core_dip 列表**——低吸区只含「回调中的核心股」，会漏掉创新高走强
    # 中的主线龙头（2026-08-19 江天化学案例：央国企改革成员、20日+22.3%，回撤0%落不进
    # 低吸窗口）；core_dip 候选必然同时满足「主题成员+走强」，故本集合是低吸区的严格超集，
    # 低吸区里的票全部仍会高亮。单次 DB-only 推导 ~0.1s，纯展示层不改评分不落库。
    core_syms = core_stock_symbols(conn)
    for e in today_recs:
        e["_core_stock"] = e["symbol"] in core_syms

    # kNF 分数反指等组内分数键语义单源在 ranking.score_sort_key（today_report 归因复用
    # 同一实现）；v1 池选（下方 5 级排序键）不再使用它。

    # 蓄势突破观察标记（2026-08-21，⚡）：新面孔/首推或重上榜 short_term + 横盘缩量回调位
    # + MA 多头。纯展示层观察——不改排序/评分/落库（用户决策：先观察积累样本，达标后再评估
    # 是否升级为排序因子）。仅主表五类参与判定；批量取 K 线防 N+1。
    breakout_kmap = build_breakout_kline_map(conn, main_recs)
    # 2026-08-22 标记精简：两个变体判定保留（样本统计需区分），渲染合并为单一 ⚡。
    # 键 (symbol, category)：nf∩st 双挂票两行类别门不同（⚡ vs ⚡R 按构造不相交），
    # 按 symbol 键控时后写覆盖会随机丢掉其中一行的判定。
    # 2026-08-26：类别门走 _breakout_profile_key 单源（dispatcher 判变体归属一次，
    # 结构条件 _breakout_structure_ok 跑一次——替代原两谓词各判一遍门）。
    breakout_mark: dict[tuple[str, str], bool] = {
        (e["symbol"], e["category"]): (
            _breakout_profile_key(e) is not None
            and _breakout_structure_ok(e, conn, accum_map=accum_map, klines=breakout_kmap.get(e["symbol"]))
        )
        for e in main_recs
    }

    # 走势美感标记（2026-09-09 上线 / 2026-09-15 分档）：判定单源 trend_beauty.beauty_mark，
    # 纯展示预判「这票的走势口径」（硬拦降级后仅作买入体验参考），不改过滤/排序/落库。
    # 仅 v1/v2 池选行渲染；回马枪/核心低吸区不标（日线门与低位类语义冲突）。
    # 批量取 K 线防 N+1；RTS_TREND_MARK=0 时标记整体为空。
    beauty_mark: dict[tuple[str, str], str] = {}  # (symbol, category) → "" / "美" / "美★"
    if TREND_MARK_ENABLED:
        _beauty_entries = main_recs + pool_pick_recs
        _beauty_klines = get_cached_klines(conn, sorted({e["symbol"] for e in _beauty_entries}))
        for e in _beauty_entries:
            beauty_mark[(e["symbol"], e["category"])] = _beauty_mark_for(e, _beauty_klines.get(e["symbol"]))

    # 综合排序主表已隐藏（2026-08-28）：v1 池选已替代其展示功能。
    # （档位分组渲染的旧实现已删除；需还原见 git 历史，勿在此堆积注释代码。）

    # v1 池选（2026-08-28）：按优先级规则排序的详细列表，关键列展示。
    # 排序规则：榜上优先 → 涨幅升序 → 回调核心 → 排名升序 → 新面孔。
    def _cb_core_pullback_ok(sym: str) -> bool:
        kl = breakout_kmap.get(sym)
        if not kl or len(kl) < 20:
            return False
        h20 = max(b[1] for b in kl[-20:])
        t1_close = kl[-1][2]
        if h20 <= 0 or t1_close <= 0:
            return False
        pb = t1_close / h20 - 1.0
        return CORE_PULLBACK_MIN <= pb <= CORE_PULLBACK_MAX

    _stg_map = {
        "rebound": "RBD",
        "momentum": "MOM",
        "new_face": "NEW",
        "known_new_face": "kNF",
        "short_term": "ST",
        "pool_pick": "池选",
    }
    main_rows: list[MainRow] = []
    try:
        _scored_rows = []
        # 减仓类纪律标签（卖出信号）：有这些标签的票从主表过滤掉
        for e in main_recs:
            sym = e["symbol"]
            # 检查减仓类纪律标签
            _fc = fresh_candidate(e)
            if _fc and _fc.tactic_tags and any(t in TACTICS_SELL_TAGS for t in _fc.tactic_tags):
                continue  # 有减仓类标签，跳过
            # 涨幅键与展示列同源（entry_display_quote）：live 0.00% 合法不被 `or` 吞。
            chg = entry_display_quote(e)[0]
            # 不追涨过滤（2026-09-04 用户决策）：今日实时涨幅超过阈值的票不进主表
            # （纯显示层，不改评分/落库；回马枪/核心低吸区不受影响）。
            if chg > DISPLAY_MAX_TODAY_PCT:
                continue
            _fresh_c = _fc
            accum_val = None
            if _fresh_c and _fresh_c.kline:
                accum_val = _fresh_c.kline.accumulated_pct
            if accum_val is None:
                accum_val = accum_map.get(sym)
            score = e.get("score", 0)
            is_core = bool(e.get("_core_stock"))
            # 统一复合评分（2026-09-08）：取代原 5 元组排序（榜上优先→涨幅→核心→排名→新面孔）
            cs = composite_score(e, conn, accum_map=accum_map)
            tier = composite_tier(e, conn, accum_map=accum_map)
            _scored_rows.append((tier, cs, e, is_core, accum_val, score))
        # 统一排序：档位升序 → composite_score 降序 → 类别优先级（composite_score 内已含 cat_base）
        _scored_rows.sort(key=lambda x: (x[0], -x[1], CAT_DISPLAY_PRIORITY.get(x[2].get("category", ""), 99)))
        # 逐行解析为 MainRow（排序在上面的元组里完成，此处只做展示字段定型）。
        # 2026-08-29：候选（_fresh_c）必须逐行重算——构建循环里的 _fresh_c 只保留末行，
        # 跨行复用会把上一只票的行情安到本行。
        for _tier, _cs, _e, _ic, _av, _sc in _scored_rows:
            _fresh_c = fresh_candidate(_e)
            _rk_disp = _e.get("live_rank") or _e.get("rank")
            if _rk_disp is None and _fresh_c:
                _rk_disp = _fresh_c.stock.rank
            _rk_val = _rk_disp if isinstance(_rk_disp, (int, float)) and _rk_disp > 0 else None
            _pct_row, _cur_row = entry_display_quote(_e)
            main_rows.append(
                MainRow(
                    entry=_e,
                    rank=_rk_val,
                    accum=_av,
                    score=_sc or 0,
                    composite_score=_cs,
                    core=_ic,
                    cat_label=_stg_map.get(_e["category"], "?"),
                    pct=_pct_row,
                    current=_cur_row,
                    sector=_entry_sector(_e),
                )
            )
    except EXTERNAL_FAILURES as _e:
        # 2026-08-29：原为 `except Exception: pass`——渲染循环里任何 KeyError/TypeError
        # 都会让整张榜单静默截断，用户只看到"票变少了"而无从察觉。收窄到数据类异常
        # 并显式告警（代码 bug 则冒泡到主循环记录完整 traceback）。
        warnings.append(f"v1 池选构建中断（数据缺失）: {type(_e).__name__}: {_e}")

    # v2 池选区（双跑同屏，2026-09-02）已于 2026-09-14 按用户决策**隐藏**：终端与
    # 飞书两处展示区均已移除，故这里也不再构建 pool_rows / pool_total。
    # ⚠ 注意：pool_pick_recs 本身**仍要保留** —— 它是终选参考区合池输入之一
    # （见下方 final_pick 调用），删掉它会改变终选结果。此处只去掉展示结构。
    # （沿革：原实现按「排名升序 → 低吸标签优先 → 涨幅降序」排序后截前
    #  V2_POOL_DISPLAY_TOP 行。需复原见 git 历史。）

    # 动态推荐（2026-08-27）：根据近端主表档次日表现自动判断 regime，弱市下把
    # momentum/known_new_face/new_face 类🎯 从推荐序列剔除、优先 核心低吸/回马枪/
    # rebound🎯/弱转强；强市则正常优先级。纯展示行，不改排序/评分/落库。
    # 市况信号与头部 _market_env_tag / 飞书 env_tag 同源（统一 _regime_weak）；
    # weak 由调用方传入时复用（避免 Display 头/体重复查询），None 时自算一次。
    if weak is None:
        try:
            weak = _regime_weak(conn)
        except EXTERNAL_FAILURES as _e:
            # 编程错误（KeyError/TypeError 等）不再被吞——冒泡到主循环记录完整 traceback；
            # 此处仅承接数据类异常并按强市 fail-open。
            warnings.append(f"regime 判定中断（数据缺失，按强市处理）: {type(_e).__name__}: {_e}")
            weak = False
    _weak = weak
    try:
        _adj = _adjusted_picks(today_recs, nextday_mark, conn, flow_pct_map, weak=_weak)
    except EXTERNAL_FAILURES as _e:
        # 2026-08-29：原为裸 except Exception（`pi-lens-ignore` 绕过告警）——会把
        # _adjusted_picks 内的 KeyError/TypeError 静默吞成「无动态推荐」，用户无法
        # 区分「本就无推荐」与「推荐逻辑崩了」。收窄到数据类异常并显式告警。
        warnings.append(f"动态推荐计算中断（数据缺失）: {type(_e).__name__}: {_e}")
        _adj = None

    # 显示门（核心低吸）：原为「主区条数 ≤ COMEBACK_DISPLAY_MIN_MAIN 或弱市 regime 时
    # 展示核心方向低吸区」。2026-09-14 按用户决策**隐藏该展示区**，故 show_core_dip 门
    # 与对应字段一并移除。
    # ⚠ core_dips 本身仍在算：它是终选参考区合池输入之一（见下方 final_pick 调用），
    # 且 core_dips 排序仍按 _core_dip_entry_quality 保持原口径，只是不再单独成区渲染。
    # 回马枪区（comeback）虽无展示区，其排序结果同样进入终选合池，故一并保留。
    _show_comeback = bool(comeback_recs) and (len(main_recs) <= COMEBACK_DISPLAY_MIN_MAIN or bool(_weak))
    # 2026-08-24：回马枪区内按资金流优先（ranking.comeback_sort_key 单源，与 today_report
    # 回马枪小节同源防漂移）——▲▲回流可取在前、▼▼背离回避劣后，次键评分。
    _comeback_sorted = sorted(comeback_recs, key=lambda x: comeback_sort_key(x, flow_pct_map))
    core_dips: list[RecommendationRow] = list(core_dip_recs)
    core_dips.sort(key=_core_dip_entry_quality)

    # 次日大涨高概率规则（纯 DB-only 计算，不改 score / 不进综合排序）
    # conn 此时已非 None（函数入口对 conn is None 提前返回 None）
    _rule_result = scan_rule(conn)

    # 决策层（2026-09-04 ~ 2026-09-14）已按用户决策**整体删除**：短名单/空仓判定、
    # decision_picks 落库、终端与飞书「今日决策」区块、decision_lines 注入参数全部移除。
    # 仅保留 scanner.decision.market_gate（择时门），由终选参考区用于标注
    # 「门开」/「门关·仅观察参考」。需复原见 git 历史。

    # 终选参考区（2026-09-05 升级）：v1+v2+回马/低吸 合池 → 次日大涨概率终选 ≤2 只
    # + 落选理由 + 周期标签（概率排序单源 scanner.nextday_prob，去相关在 final_pick）。
    # 纯计算无落库，fail-open 不阻断展示主流程（评级单源在 scanner.final_pick）。
    _final_pick_lines: list[str] | None = None
    if FINAL_PICK_ENABLED:
        try:
            from scanner.final_pick import final_pick_lines as _build_final

            _final_pick_lines = _build_final(
                conn,
                main_recs + pool_pick_recs + _comeback_sorted + core_dips,
                accum_map,
                flow_pct_map,
                nextday_mark,
            )
        except EXTERNAL_FAILURES as _fpx:
            warnings.append(f"终选区构建失败: {type(_fpx).__name__}: {_fpx}")

    return ScanView(
        main_rows=main_rows,
        comeback_rows=_comeback_sorted[:COMEBACK_DISPLAY_MAX],
        nextday_mark=nextday_mark,
        breakout_mark=breakout_mark,
        flow_pct_map=flow_pct_map,
        last_ranks=last_ranks or {},
        adj_picks=_adj,
        weak=_weak,
        show_comeback=_show_comeback,
        warnings=warnings,
        rule_result=_rule_result,
        final_pick_lines=_final_pick_lines,
        beauty_mark=beauty_mark,
        hot_rows=hot_rows,
        flow_filtered=flow_filtered,
    )
