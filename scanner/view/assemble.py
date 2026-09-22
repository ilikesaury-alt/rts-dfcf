import statistics

from scanner.config import (
    CAT_DISPLAY_PRIORITY,
    CORE_DIP_CATEGORY,
    CORE_PULLBACK_MAX,
    CORE_PULLBACK_MIN,
    DISPLAY_MAX_TODAY_PCT,
    FUND_FLOW_HARD_FILTER_ENABLED,
    TACTICS_SELL_TAGS,
    TREND_MARK_ENABLED,
)
from scanner.config_scoring import MARKET_WEAK_THRESHOLD
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
    _dip_label_bonus,
    build_accum_map,
    build_breakout_kline_map,
    composite_score,
    entry_fund_flow_pct,
    fresh_candidate,
    is_fund_outflow,
)

# 走势美感标记判定单源在 scanner.trend_beauty（日线定准入、分时定级别）——经下方
# `from scanner.view.model import *` 带入 _beauty_mark_for，本模块不重复持有判定逻辑。
from scanner.utils import EXTERNAL_FAILURES
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
    "_is_console",
    "_market_suggestion_text",
    "_real_market_regime",
    "_regime_weak",
    "_supports_ansi",
    "build_scan_view",
)


def _real_market_regime(market_idx_pct: float | None) -> bool | None:
    """基于真实市场指数（创业板指）判定弱市。

    返回 True=弱市 / False=强市 / None=无数据（调用方 fail-open 按强市处理）。
    使用 enhancer 同源阈值：MARKET_STRONG_THRESHOLD / MARKET_WEAK_THRESHOLD。
    无数据时不判否（None → 按强市处理），避免无指数时误标「弱势」。
    """
    if market_idx_pct is None:
        return None
    return market_idx_pct < MARKET_WEAK_THRESHOLD


def _market_suggestion_text(weak: bool | None, market_idx_pct: float | None) -> str:
    """根据市况生成板块观察建议文本（纯展示，不参与评分/选股）。

    weak=True → 弱市（防御型板块）；weak=False → 强市（进攻型板块）；
    weak=None → 无指数数据，给通用建议。
    """
    if weak is True:
        return "弱市·防御优先：银行/医药/消费红利"
    if weak is False:
        return "强势·进攻优先：科技/AI/半导体/新能源"
    return "市况未知·均衡配置"


# 2026-09-21 删除：综合判断摘要（_build_summary + SUMMARY_* 观察线 5 个常量）。
# 它原先是终端最后一块「分区体检报告」（2026-09-17 重写），用户决策「终端只留四个
# 区块（v1 池选 / v1 回捞 / 沪深飙升 / 榜外异动）」后整块移除 —— 含 ScanView.summary
# 字段、render_terminal 的「◆ 综合判断」区块、feishu 侧本就没有该节（无需改）。
# ⚠ 被它带走的还有 chase_filtered / tactic_filtered 两个纯统计计数器（唯一用途是
#   摘要的「口径分歧」判定）；两个**过滤器本身**（不追涨 / 减仓标签）完整保留。
# 需复原见 git 历史。


def _regime_weak(conn, lookback=10):
    """近端主表档(非 comeback/core_dip)次日表现均值 < 0 → 弱市(regime 退潮)。
    纯展示层用：驱动头部市况标签（与飞书 env_tag 同源）。
    fail-open：无数据/查询异常时返回 False(按强市处理，不误删推荐)。

    ⚠ SQL 里的 `category NOT IN ('comeback','core_dip')` **必须保留 comeback**：
    这是对**历史 recommendations 行**的过滤（回马枪桶曾长期产出并落库），
    删掉它会把历史 comeback 行纳入近端均值 → 头部市况标签对历史日期的判定静默改变。
    与「回马枪桶 2026-09-16 删除」无关（该桶只是不再产出新行）。

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


# ── 动态推荐（regime 自适应序列）已于 2026-09-16 整体删除 ──
# 原 `_adjusted_picks(today_recs, nextday_mark, conn, flow_pct_map, ...)` 的排序语义
# **完全**由被删的两个特性构成：🎯 桶（marked ∩ rebound/short_term）与回马枪桶
# （comeback_sort_key）。两者删除后该函数没有可保留的语义——强行留下就得凭空发明
# 替代规则（那是新增行为，不是删除）。且它的渲染出口早在 2026-09-03 就已移除
# （render.py 只留注释），`ScanView.adj_picks` 字段同期一并删除。
# 需复原见 git 历史（2026-09-16 之前）——`_regime_weak` 仍保留，现只服务市况标签。


def build_scan_view(
    conn=None,
    live_quotes: dict[str, dict] | None = None,
    rank_map: dict[str, int] | None = None,
    today_pool: dict[str, Candidate] | None = None,
    last_ranks: dict[str, int] | None = None,
    weak: bool | None = None,
    hot_rows: list | None = None,
    offboard_rows: list | None = None,
    hist_rows: list | None = None,
    market_idx_pct: float | None = None,
    new_symbols: set[str] | None = None,
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
    new_symbols: 本轮**新进入**今日推荐池的票集（symbol）。由调用方（扫描循环）持有
    上一轮的票集快照做差集得到 —— 本函数**刻意不自行推导「新」**：
    `recommendations.time` 在「分数提高」时会被覆盖（dal.save_recommendations 的
    UPDATE 分支），故 `MIN(time)` / `first_time` 的真实语义是「最后一次提分时刻」而非
    首次出现；拿它判「新票」会把「老票提分」误标成新票（实测每轮 1~3 行，最大一轮
    12 行，全部落在这个误判口径里）。缺省 None / 空集 ⇒ 不标记任何新票，排序退化为
    原有键序 —— 收盘后无新票时即此状态，输出与加本功能之前**逐字节相同**。

    v1 池选排序键（2026-09-22 现行，沿革见下方主表注释块）：
    新票优先 → 类别展示优先级 → 榜单排名升序 → 资金流降序 → 低吸/突破标签加分。
    （2026-09-16~21 的版本在「新票优先」后还有第 2 键 tier（composite_tier 过热劣后/
    composite 分档），2026-09-22 按用户决策删除 —— 与类别键重合且 composite 部分
    无法向用户解释；**副作用**：accum≥50% 过热票不再被排序劣后。）
    ⚠ 「新票优先」是绝对置顶（用户 2026-09-21 明确选择）：一只本轮刚进池的票无论其余
    键怎么排都到第 1 行。选它的理由是主表是**观察名单**不是买入清单，而「新信息必须
    先看到」优先于「排序按质量读」；过热票仍带 ⚠超买 行尾标记。若要改回「桶内新票
    优先」，把 is_new 键从 x[0] 移到 x[1] 即可。
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
    # 在此处过滤 `today_recs` 一次，下游全部派生集合（main_recs 及其一切切片）自动继承
    # ——终端与飞书同源
    # （feishu 只读 build_scan_view 产出的同一份 ScanView），不会再出现「某区剔了、
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

    # P1-9（2026-08-20）：全部推荐一次性批量回放累计（build_accum_map 单查询）。
    # 2026-09-16：🎯 标记预计算（`nextday_mark` + 双挂票归一）随 🎯 画像删除。
    accum_map = build_accum_map(conn, today_recs)

    # v1 池选 = today_recs 去掉三个**非主表类别**。三者共同点是「历史行仍可能留在
    # today_recs 里」，故过滤必须显式列出，不能指望本轮候选桶天然不含：
    #   comeback  —— 回马枪桶已于 2026-09-16 删除，只剩历史行；
    #   core_dip  —— 核心低吸（DIP）：其展示区 2026-09-14 隐藏，唯一的合池消费方
    #                「终选参考区」2026-09-21 删除 ⇒ 现无任何消费方；
    #   pool_pick —— v2 池选：展示区 2026-09-14 隐藏，合池消费方同上 2026-09-21 删除。
    # ⚠ 两个「无消费方」的类别**仍从 main_recs 排除**：它们代表的是策略语义不同的桶，
    # 混进 v1 主表会同时破坏主表列语义与排序键（v1 档位序 vs v2 涨幅降序）。
    # RTS_PIPELINE 不再影响显示层。
    main_recs = [e for e in today_recs if e["category"] not in ("comeback", CORE_DIP_CATEGORY, V2_CATEGORY)]

    # 核心股高亮（2026-08-19）：综合排序/低吸列表里属于当前主线方向核心股的票，
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
        # 只算 v1 主表行 —— 标记的唯一渲染出口是 v1 池选行行尾（_entry_row_suffix）。
        _beauty_entries = main_recs
        _beauty_klines = get_cached_klines(conn, sorted({e["symbol"] for e in _beauty_entries}))
        for e in _beauty_entries:
            beauty_mark[(e["symbol"], e["category"])] = _beauty_mark_for(e, _beauty_klines.get(e["symbol"]))

    # 综合排序主表已隐藏（2026-08-28）：v1 池选已替代其展示功能。
    # （档位分组渲染的旧实现已删除；需还原见 git 历史，勿在此堆积注释代码。）

    # v1 池选（2026-08-28）：按优先级规则排序的详细列表，关键列展示。
    # 排序规则（2026-09-22 现行）：新票优先 → 类别展示优先级 → 榜单排名升序
    # → 资金流降序 → 低吸/突破标签加分。**涨幅不在键内**。
    # （沿革：2026-08-28 键为「榜上优先→涨幅升序→回调核心→排名升序→新面孔」，09-16 换键时
    #  漏改文档；2026-09-16~21 的键含第 2 键 tier（composite_tier 过热劣后/composite 分档），
    #  2026-09-22 按用户决策**删除该键** —— 它与类别键高度重合（kNF→tier1、MOM/NEW→tier2、
    #  ST→tier3）且 composite 部分无法向用户解释。**副作用**：accum≥50% 过热票不再被
    #  排序劣后，只靠行内「⚠ 超买/过热」展示标记提示 —— 需要恢复劣后时从 git 历史取回
    #  `composite_tier` 调用即可。）
    # 实测（2026-09-21 真实 scanner.db 17:28 那轮，25 行）：
    #   · 榜单排名升序**在榜票之间真实生效** —— rank_map 只覆盖当轮在榜票（当日 7 只），
    #     正是它把 rank17 的义翘神州排在 rank43 的威尔高之前，并让这两只排在资金流更优的
    #     ▲▲ 行之前（纯排名假设无法解释该顺序）。
    #   · 形态加分该轮全 0，属稀有 tie-breaker（故不入终端标题）。
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
    # ⚠ 2026-09-21：原先这里还有 chase_filtered / tactic_filtered 两个「风险剔除计数」
    # （唯一用途是综合判断摘要的「口径分歧」判定）。摘要删除后二者无消费方，故一并移除；
    # 下面循环里的**两个过滤器本身**（减仓标签 / 不追涨）照旧生效，只是不再计数。
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
            # （纯显示层，不改评分/落库；v2 池选区与核心低吸区早已停止渲染，不受影响）。
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
            # composite_score 本身**只写不读**：唯一去处是 MainRow.composite_score 字段，
            # 而全仓没有任何 `.composite_score` 读取（2026-09-21 核查）—— 即当前无渲染方。
            # 2026-09-22 起，它对排序的**最后一条影响路径**（经 composite_tier 折算成档位、
            # 充当第 2 排序键）也按用户决策删除 —— 现在它对顺序零影响，纯历史展示字段。
            cs = composite_score(e, conn, accum_map=accum_map)
            # ── v1 排序主键（2026-09-16 换键；2026-09-21 加「新票优先」居首；
            #    2026-09-22 删 tier 键，终成四键 + 形态加分）──
            # 新票优先(本轮新进池) → 类别展示优先级(策略语义,非历史hit) → 榜单排名升序
            # (实时热度) → 资金流降序(实时主力) → 低吸/突破标签加分(形态,稀有 tie-breaker)。
            # 「新」取自调用方传入的 new_symbols（跨轮票集差集）——**不读 DB 时间戳**，
            # 理由见本函数 docstring 的 new_symbols 段（time 会被提分覆盖）。
            # new_symbols 缺省/空 ⇒ 本键恒 1 ⇒ 键序完全还原，输出逐字节不变。
            _is_new_key = 0 if (new_symbols and sym in new_symbols) else 1
            _rk = e.get("live_rank") or e.get("rank")
            _rank_key = _rk if isinstance(_rk, (int, float)) and _rk > 0 else 99999
            _flow = entry_fund_flow_pct(e, flow_pct_map)
            _fund_key = _flow if _flow is not None else 0.0
            _dip_key = _dip_label_bonus(e)
            _cat_pri = CAT_DISPLAY_PRIORITY.get(e.get("category", ""), 99)
            _scored_rows.append(
                (_is_new_key, _cat_pri, _rank_key, -_fund_key, -_dip_key, e, is_core, accum_val, score, cs)
            )
        # 统一排序：新票优先 → 类别语义优先级 → 榜单排名升序 → 资金流降序 → 形态加分
        _scored_rows.sort(key=lambda x: (x[0], x[1], x[2], x[3], x[4]))
        # 逐行解析为 MainRow（排序在上面的元组里完成，此处只做展示字段定型）。
        # 2026-08-29：候选（_fresh_c）必须逐行重算——构建循环里的 _fresh_c 只保留末行，
        # 跨行复用会把上一只票的行情安到本行。
        for _ink, _cat_pri, _rank_key, _neg_fund, _neg_dip, _e, _ic, _av, _sc, _cs in _scored_rows:
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
                    is_new_entry=not _ink,
                )
            )
    except EXTERNAL_FAILURES as _e:
        # 2026-08-29：原为 `except Exception: pass`——渲染循环里任何 KeyError/TypeError
        # 都会让整张榜单静默截断，用户只看到"票变少了"而无从察觉。收窄到数据类异常
        # 并显式告警（代码 bug 则冒泡到主循环记录完整 traceback）。
        warnings.append(f"v1 池选构建中断（数据缺失）: {type(_e).__name__}: {_e}")

    # v2 池选区（双跑同屏，2026-09-02）已于 2026-09-14 按用户决策**隐藏**：终端与
    # 飞书两处展示区均已移除，故这里也不再构建 pool_rows / pool_total。
    # 2026-09-21：pool_pick 类别连合池消费方（终选参考区）一并消失 —— 该类别现在
    # 既不渲染、也不参与任何合池，只剩「从 main_recs 排除」这一处语义（见上方注释）。
    # （沿革：原实现按「排名升序 → 低吸标签优先 → 涨幅降序」排序后截前
    #  V2_POOL_DISPLAY_TOP 行。需复原见 git 历史。）

    # 市况信号：优先使用真实市场指数（创业板指 pct），无数据时回退到 DB 推荐历史。
    # weak 由调用方传入时复用（避免 Display 头/体重复查询），None 时自算一次。
    if weak is None:
        real_regime = _real_market_regime(market_idx_pct)
        if real_regime is not None:
            weak = real_regime
        else:
            try:
                weak = _regime_weak(conn)
            except EXTERNAL_FAILURES as _e:
                # 编程错误（KeyError/TypeError 等）不再被吞——冒泡到主循环记录完整 traceback；
                # 此处仅承接数据类异常并按强市 fail-open。
                warnings.append(f"regime 判定中断（数据缺失，按强市处理）: {type(_e).__name__}: {_e}")
                weak = False
    _weak = weak

    # 显示门（核心低吸）：原为「主区条数 ≤ COMEBACK_DISPLAY_MIN_MAIN 或弱市 regime 时
    # 展示核心方向低吸区」。2026-09-14 按用户决策**隐藏该展示区**，故 show_core_dip 门
    # 与对应字段一并移除；2026-09-21 连合池消费方（终选参考区）也删除 ⇒ core_dip 类别
    # 现在既不渲染、也不合池，只剩「从 main_recs 排除」这一处语义（见上方注释）。
    # 回马枪区（comeback）连同 `_show_comeback` / `_comeback_sorted` 于 2026-09-16 删除。
    # （`_core_dip_entry_quality` 排序键仍在 scanner.view.model，供需要按低吸质量排序的
    #  离线脚本复用；展示通路已不再调用它。）

    # 次日大涨高概率规则（纯 DB-only 计算，不改 score / 不进综合排序）
    # conn 此时已非 None（函数入口对 conn is None 提前返回 None）
    _rule_result = scan_rule(conn)

    # 决策层（2026-09-04 ~ 2026-09-14）与终选参考区（2026-09-04 ~ 2026-09-21）均已按
    # 用户决策**整体删除**：短名单/空仓判定、decision_picks 落库、终端与飞书对应的
    # 「今日决策」「终选参考」区块、decision_lines / final_pick_lines 注入参数，
    # 以及 scanner/decision.py、scanner/final_pick.py 两个模块全部移除。
    # 需复原见 git 历史。
    # ⚠ 展示通路现在**没有任何合池/短名单环节**：main_recs 直接渲染，
    #   四区（v1 池选 / v1 回捞 / 沪深飙升 / 榜外异动）各自独立、互不排名。

    # 综合判断摘要（2026-09-17 重写的那版「分区体检报告」）已于 2026-09-21 按用户决策
    # **整体删除** —— 连带 _build_summary 函数、SUMMARY_* 观察线常量、ScanView.summary
    # 字段、终端「◆ 综合判断」区块，以及只服务于它的 chase_filtered / tactic_filtered
    # 两个**纯统计计数器**（两个过滤器本身保留，见上方 v1 主表构建循环）。需复原见 git 历史。

    return ScanView(
        main_rows=main_rows,
        breakout_mark=breakout_mark,
        flow_pct_map=flow_pct_map,
        last_ranks=last_ranks or {},
        weak=_weak,
        warnings=warnings,
        rule_result=_rule_result,
        beauty_mark=beauty_mark,
        hot_rows=hot_rows,
        offboard_rows=offboard_rows,
        hist_rows=hist_rows,
        flow_filtered=flow_filtered,
        market_idx_pct=market_idx_pct,
    )
