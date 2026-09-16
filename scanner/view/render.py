from scanner.categories import CAT_LABEL
from scanner.config import (
    FUND_OUTFLOW_NET_PCT,
    HIST_LOOKBACK_DAYS,
    HOT_HIGHLIGHT_STREAK,
    MAX_MARKET_CAP,
    MAX_STOCK_PRICE,
    TOP40_THRESHOLD,
    now_beijing,
)
from scanner.models import Candidate, RecommendationRow

# 排序/画像纯逻辑单源在 scanner.ranking；display 只导入渲染所需子集。
# 此前的全量 re-export（供 scripts 的 display._entry_* 属性访问）已下线：
# 消费方直接 import scanner.ranking（scripts/review_tier_replay.py 已改）。
from scanner.ranking import (
    fresh_candidate,
)

# 走势美感标记（日线定准入、分时定级别）的判定单源在 scanner.trend_beauty，
# 经下方 `import *` 带入 _beauty_mark_for / beauty_mark，本模块不重复持有判定逻辑。
from scanner.utils import clear_screen, to_int
from scanner.view.assemble import *  # noqa: F401,F403
from scanner.view.model import *  # noqa: F401,F403

# ANSI 探测（_is_console / _supports_ansi）/ ANSI / CAT_COLOR / _ANSI_ESCAPE 的单源在
# scanner.view.model —— 上方两个 `import *` 已带入，本模块不再重复定义。
# 2026-09-14 去重：拆分脚本曾把这段 Windows 终端探测头原样复制进三个文件，后果有二：
#   ① SetConsoleMode 被重复调用三次（无害但无谓）；
#   ② `__all__` 由「AST 模块级名 ∩ dir()」推导，把仅 Windows 分支存在的
#      _kernel32/_handle/_mode 也写进了导出表 —— Linux/macOS 下
#      `from scanner.view.model import *` 会因 __all__ 缺名直接 AttributeError。
# CAT_LABEL 本模块在用（类别标签显示），故在上方显式 import；CAT_COLOR 仍取自 model。


__all__ = (
    "ANSI",
    "CAT_COLOR",
    "_ANSI_ESCAPE",
    "_fmt_hot_amount",
    "_fmt_hot_volume_hand",
    "_is_console",
    "_print_priority_row",
    "_render_hist_watch_region",
    "_render_hot_watch_region",
    "_supports_ansi",
    "_table_header",
    "_table_row",
    "_watch_tail_terminal",
    "display",
    "display_priority",
    "render_hist_watch_standalone",
    "render_hot_watch_standalone",
    "render_terminal",
)


def display(
    gem_total: int,
    interval: int,
    filtered_large_cap: int = 0,
    conn=None,
    live_quotes: dict[str, dict] | None = None,
    rank_map: dict[str, int] | None = None,
    today_pool: dict[str, Candidate] | None = None,
    last_ranks: dict[str, int] | None = None,
    hot_rows: list | None = None,
    hist_rows: list | None = None,
) -> "ScanView | None":
    """扫描主屏：头部摘要 + 展示视图（构建/渲染委托 display_priority）。

    ScanView 定义在本文件更下方（视图模型区），此处为前向引用故写成字符串注解；
    render_terminal / build_scan_view 均在类定义之后，无需引号。

    返回本轮 ScanView 供飞书复用（同一份选择，避免两端分叉与重复计算）；
    conn 为空或今日无推荐时返回 None。

    today_pool：本轮候选池快照（symbol → Candidate），由调用方（scan_with_raw 的
    ScanResult）传入，display 不直接访问 orchestrator 内部状态。
    last_ranks: 上一轮扫描的榜单排名 {symbol: rank}，供「排名」列显示变化（+N 升 / -N 降）。
    """
    clear_screen()
    now = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{'=' * 96}")
    print(f"  创业板飙升榜监控  ({now})")
    filter_info = f" | 过滤{filtered_large_cap}只" if filtered_large_cap else ""
    # 统一市况信号：与动态推荐 / 飞书 env_tag 同源（_regime_weak），避免同屏矛盾。
    weak = _regime_weak(conn) if conn is not None else False
    print(f"  创业板共 {gem_total} 只{filter_info} | 每{interval}s刷新 | {_market_env_tag(weak)}")
    print(f"{'=' * 96}")
    return display_priority(
        conn=conn,
        live_quotes=live_quotes,
        rank_map=rank_map,
        today_pool=today_pool,
        last_ranks=last_ranks,
        weak=weak,
        hot_rows=hot_rows,
        hist_rows=hist_rows,
    )


def _print_priority_row(
    entry: RecommendationRow | dict,
    i: int,
    flow_pct_map: dict,
    breakout_mark: bool = False,
    last_ranks: dict[str, int] | None = None,
) -> None:
    """综合排序单行的统一渲染（主表与回马枪独立区共用），避免两处复制大段渲染逻辑。

    ⚠ 2026-09-14 现状：本函数的两个展示消费方（核心方向低吸区、回马枪区）都已不再
    渲染 —— 低吸区按用户决策隐藏、回马枪区自 2026-09-03 起就无展示区。故当前**无生产
    调用方**（仅单测覆盖）。**有意保留**：它是 COLS_DETAIL 列规格的唯一渲染实现，
    删除会让「恢复低吸区」变成重写而非复原；且 `scripts/_verify_view_split.py` 把
    拆分时的函数清单当契约（缺一个 top-level def 即报错）。要彻底清掉请连带处理该脚本。

    flow_pct_map: {symbol: 主力净占比} DB 快照回退（候选缺失/扫描失败时仍显示资金流图标）。
    breakout_mark: 蓄势突破观察画像（⚡）——新面孔/首推或重上榜 short_term + 横盘缩量回调位
    （见 _is_breakout_setup / _is_relist_breakout_setup；2026-08-22 渲染合并为单一 ⚡，
    变体区分保留在判定函数供样本统计）。纯观察标记，不参与排序/评分/落库。
    视觉标记，不参与排序/评分/落库；行尾标记统一走 _entry_row_suffix（与优选池行同口径）。
    last_ranks: 上一轮扫描的榜单排名 {symbol: rank}，用于「排名」列展示雪球榜单排名变化
    （+N 升 / -N 降），与已下线策略桶的 _rank_delta_str 同口径；缺省 None 不显示变化。

    （原 `nextday_mark`（🎯）入参 2026-09-14 删除：全仓无任何调用方传入，且 🎯 行尾
    渲染自 2026-09-04 已停用 —— 纯哑参。🎯 的判定仍在 ranking.is_nextday_marked。）
    """
    c = entry.get("_candidate")
    # 标签/优先级列统一用 entry["category"]（与排序口径一致），
    # 不用 c.category：双挂票的 today_pool 按 symbol 覆盖会拿到 short_term 候选，
    # 而 DB 保留的是最高分行的 category（可能是 new_face），两者不一致会导致
    # 排到 new_face 档却显示 ST 标签 + 回避建议的矛盾。
    cat = entry["category"]
    # 板块列单源（_entry_sector）：与主表 MainRow.sector 同一实现。
    sector = _entry_sector(entry)
    # 涨幅/现价/排名统一回退链：实时行情(live_quotes/rank_map) → 候选池当前扫描快照 →
    # appearances(DB) → 推荐时落库值。
    # 候选可信性走 fresh_candidate 单源助手（2026-08-24 第二轮审查收口）：stale
    # 掉榜候选的冻结快照（仙乐健康案例：掉榜后仍显示上榜时的 rank 15）与双挂票
    # 类别错位候选都视同无候选，落 DB 回退链。
    _fresh_c = fresh_candidate(entry)
    # 涨幅/现价走 entry_display_quote 单源回退链（与优选池行/涨幅升序排序键同口径）。
    pct, live_cur = entry_display_quote(entry)
    live_rank = entry.get("live_rank")
    if not live_rank and _fresh_c and _fresh_c.stock.rank:
        live_rank = _fresh_c.stock.rank
    price_str = f"{live_cur:.2f}" if live_cur else "—"
    # 排名列：当前名次 + 较上一轮扫描的变化（+N 升 / -N 降），无上轮或不变化仅显名次。
    # 名次在雪球榜单前 TOP40_THRESHOLD 内时高亮（加粗 + 红色），TOP40 视为热度强势信号。
    if live_rank:
        delta_str = _rank_delta_str(entry["symbol"], live_rank, last_ranks or {})
        rank_num = f"{live_rank}"
        if 0 < live_rank <= TOP40_THRESHOLD:
            rank_num = f"{ANSI['BOLD']}{ANSI['RED']}{rank_num}{ANSI['RESET']}"
        rank_str = f"{rank_num}{delta_str}" if delta_str else rank_num
    else:
        rank_str = "—"
    label_display = f"{CAT_COLOR.get(cat, '')}{CAT_LABEL.get(cat, cat)}{ANSI['RESET']}"
    # 回马枪变体（反转/回踩）：策略桶下线后由此处单行保留，避免掉榜区丢失语义。
    if cat == "comeback":
        variant = ""
        if c:
            variant = getattr(c, "comeback_variant", "") or (
                c.kline.dimensions.get("comeback_variant", "") if c.kline else ""
            )
        if not variant:
            trend = entry.get("trend") or ""
            variant = trend.split("·")[0] if "·" in trend else ""
        if variant:
            label_display += f"{ANSI['CYAN']}·{variant}{ANSI['RESET']}"
    # 辨识度（↻）行内标记已下线（2026-08-22 标记精简）：回测证独立增量≈0、已退出排序，
    # 纯装饰性噪音；prominence 数据仍在 today_report 归因中使用，不受影响。
    first_time = str(entry.get("first_time") or entry.get("time") or "")[:5]
    # 5日累计涨幅：优先用候选池可信快照（fresh_candidate），否则用 DB 落库值
    accum_val = _fresh_c.kline.accumulated_pct if _fresh_c and _fresh_c.kline else entry.get("accumulated_pct")
    accum_str = "—" if accum_val is None else f"{accum_val:+.2f}%"
    # 行尾标记（风险/资金流/连板/⚡）走 _entry_row_suffix 单源，与优选池行同口径。
    tail = _entry_row_suffix(entry, flow_pct_map, breakout_marked=breakout_mark)
    # 板块普涨避雷行尾标记已下线（2026-08-17 用户反馈「太扎眼」）：小板块共振避雷
    # 结论保留于回测（cnt<15 票 hit 5.9-6.7%/cum_3d -2.2~-2.6 最差），但黄色长文本
    # 移除，避免干扰 🎯 档0 等主信号。
    # 核心股高亮（2026-08-19）：该票今日在核心方向低吸区（category=core_dip）→ 判定
    # 为核心股，名称加粗品红高亮（判定在 display_priority 预计算 _core_stock，主表与
    # 回马枪区共用本函数同规则）。纯展示层不改评分不落库。
    name_str = entry["name"]
    if entry.get("_core_stock"):
        name_str = f"{ANSI['BOLD']}{ANSI['MAGENTA']}{name_str}{ANSI['RESET']}"
    # 列宽/对齐统一走 COLS_DETAIL（与 _table_header 同源）。此前表头用 1 个空格
    # 分隔序号列、数据行用 2 个，导致「代码」列起整体右偏 1 列——现由同一 spec 推导。
    print(
        _table_row(
            [
                str(i),
                entry["symbol"],
                name_str,
                pct_colored(pct),
                accum_str,
                price_str,
                rank_str,
                _trunc(sector, COLS_DETAIL[7][1]),
                label_display,
                to_int(entry["score"]),
                first_time,
            ],
            COLS_DETAIL,
        )
        + tail
    )


def _table_header(spec: tuple) -> str:
    """按列 spec 生成表头（与 _table_row 同源，杜绝表头/行宽漂移）。"""
    return "  " + " ".join(_pad(title, width, align) for title, width, align in spec)


def _table_row(cells, spec: tuple) -> str:
    """按列 spec 拼一行（宽度/对齐与 _table_header 同源）。

    单元格可含 ANSI 色码：_pad 按可见宽度补位，已着色的单元格宽度达标时不额外补。
    """
    parts = [_pad(str(cell), width, align) for cell, (_, width, align) in zip(cells, spec, strict=True)]
    return "  " + " ".join(parts)


def _fmt_hot_volume_hand(volume: float) -> str:
    """成交量（股）→ 手（1 手 = 100 股），带中文单位。"""
    if volume <= 0:
        return "—"
    hands = volume / 100.0
    if hands >= 1e8:
        return f"{hands / 1e8:.2f}亿手"
    if hands >= 1e4:
        return f"{hands / 1e4:.2f}万手"
    return f"{hands:.0f}手"


def _fmt_hot_amount(amount: float) -> str:
    """成交额（元）→ 亿/万。"""
    if amount <= 0:
        return "—"
    if amount >= 1e8:
        return f"{amount / 1e8:.2f}亿"
    if amount >= 1e4:
        return f"{amount / 1e4:.1f}万"
    return f"{amount:.0f}"


def _watch_tail_terminal(ff_pct, beauty: str) -> str:
    """独立观察区（沪深飙升 / v1 回捞）的行尾标记：资金流 ▲▼ + 日线美感「美」。

    与主表 `_entry_row_suffix` 同一分工 —— 判定单源（`signals.fund_flow_signal` /
    `display_gates.beauty_marks_daily`），本层只负责成形（ANSI）。两个独立区共用本函数，
    免得「同一个 ▲ 在两个区各画一遍、其中一个少了个空格」。

    追加位置在定宽列**之外**：塞进列内会撑破 `COLS_HOT` / `COLS_HIST` 的对齐。

    结构性上限（不是 bug，两个区都有）：▼▼ 不可达（≤-8% 已被通用门剔除）；
    美★ 不可达（两区都不抓分时，`beauty_marks_daily` 只给日线档）。
    """
    tail = _fund_flow_icon_str(ff_pct)
    if tail:
        tail = f" {tail}"
    if beauty:
        tail += f" {ANSI['GREEN']}{beauty}{ANSI['RESET']}"
    return tail


def _render_hot_watch_region(rows) -> None:
    """渲染「沪深飙升·极有可能大涨」独立区（无结果时整区跳过，不留空表）。

    行元素为 scanner.hot_watch.HotCandidate（主循环内已算好并落连击），本函数只做
    渲染——与 render_terminal 的「只画不算」纪律一致。

    形参取行列表而非 ScanView：本区与主线数据完全无关，取 view 会让独立运行
    （`python -m scanner.hot_watch`）被迫构造一个满是空字段的 ScanView。
    """
    if not rows:
        return

    print(
        f"\n{ANSI['BOLD']}{ANSI['CYAN']}◆ 沪深飙升 · 极有可能大涨{ANSI['RESET']}"
        f"（创业板 · 当日动能+热度跃升 · 与上方主线口径独立）"
    )
    print(_table_header(COLS_HOT))
    for _hi, c in enumerate(rows, 1):
        # 连击 ≥ 阈值 → 「★重点关注」（跨轮连续命中的稳定性信号）。
        # 量比/市值/成交额可能因批量补全缺字段而为 0 → 显示 —（不伪造为 0.00）。
        _streak_str = f"{c.streak}"
        if c.streak >= HOT_HIGHLIGHT_STREAK:
            _streak_str = f"{ANSI['RED']}★{c.streak}{ANSI['RESET']}"
        print(
            _table_row(
                [
                    str(_hi),
                    c.code,
                    c.name,
                    f"{c.current:.2f}" if c.current else "—",
                    pct_colored(c.percent),
                    f"+{c.rank_change}",
                    _fmt_hot_volume_hand(c.volume),
                    _fmt_hot_amount(c.amount),
                    f"{c.volume_ratio:.2f}" if c.volume_ratio > 0 else "—",
                    f"{c.turnover_rate:.1f}" if c.turnover_rate > 0 else "—",
                    f"{c.market_capital / 1e8:.0f}" if c.market_capital > 0 else "—",
                    f"{c.score:.1f}",
                    _streak_str,
                ],
                COLS_HOT,
            )
            # 行尾标记（2026-09-16）：与 v1 回捞区/主表同源（_watch_tail_terminal），
            # 本区此前没有这一列 —— 标记应当是**跨展示区通用**的，不该只有回捞区有。
            + _watch_tail_terminal(c.ff_pct, c.beauty)
        )
    print(f"  {'-' * 92}")
    # print(
    #     f"  排序=评分(排名上升35/涨幅25/价格15/量能25) | "
    #     f"已剔除 ST·非创业板·停牌/无成交·价格>{MAX_STOCK_PRICE:.0f}元·市值>{HOT_MAX_MARKET_CAP / 1e8:.0f}亿·"
    #     f"涨停·涨幅>{HOT_MAX_PERCENT:.0f}%（通用风险门 + 本区专有，见 scanner/display_gates.py）| "
    #     f"连击≥{HOT_HIGHLIGHT_STREAK}轮标★"
    # )
    # 行尾标记图例（2026-09-16）：飞书卡片有一份同义图例（build_feishu_card 的飙升节脚注），
    # 两处须同步改 —— 守卫 tests/test_display.py::test_hot_legend_printed_on_both_surfaces。
    # if any((c.ff_pct is not None) or c.beauty for c in rows):
    #     print(
    #         f"  标记：{ANSI['GREEN']}▲▲/▲{ANSI['RESET']}=主力净流入(≥+8%/≥+5%)　"
    #         f"{ANSI['RED']}▼{ANSI['RESET']}=净流出(≤-5%；≤-8% 已被硬门剔除，故不出现 ▼▼)　"
    #         f"{ANSI['GREEN']}美{ANSI['RESET']}=日线趋势漂亮（尾部回撤更小·非更易大涨；"
    #         f"日线数据不足则不标；本区默认开日线美感门，无分时档故不出现美★）"
    #     )


def render_hot_watch_standalone(rows) -> None:
    """只渲染「沪深飙升·极有可能大涨」区（供 `python -m scanner.hot_watch` 独立运行）。

    与主循环的 render_terminal 共用同一个 _render_hot_watch_region，避免两套渲染
    逻辑分叉（独立区行宽/配色/脚注只此一份）。
    """
    _render_hot_watch_region(rows)


def _render_hist_watch_region(rows) -> None:
    """渲染「v1 回捞」独立区（无结果时整区跳过，不留空表）。

    行元素为 scanner.historical_watch.HistCandidate（主循环内已算好），本函数只做渲染
    —— 与 render_terminal 的「只画不算」纪律一致。形参取行列表而非 ScanView，理由同
    _render_hot_watch_region：独立运行（`python -m scanner.historical_watch`）不必构造
    一个满是空字段的 ScanView。

    脚注必须带「启发式·未做样本外校准」：本区排序键的可信度低于 nextday_prob 那条
    主线，不写清楚最自然的误读就是「评分高=更可能大涨」。
    """
    if not rows:
        return

    print(
        f"\n{ANSI['BOLD']}{ANSI['CYAN']}◆ v1 回捞{ANSI['RESET']}"
        f"（前 {HIST_LOOKBACK_DAYS} 个交易日进过 v1 · 今日回调到位 · 与上方口径独立）"
    )
    print(_table_header(COLS_HIST))
    for _hi, c in enumerate(rows, 1):
        # 行尾标记（2026-09-16）：资金流 ▲/▼ + 日线美感「美」，与飙升区共用
        # `_watch_tail_terminal`（判定单源 signals / display_gates，本层只成形）。
        # 本区结构上不会出现「美★」与「▼▼」，原因见 scanner/historical_watch
        # 与 scanner/display_gates 的模块 docstring。
        print(
            _table_row(
                [
                    str(_hi),
                    c.code,
                    c.name[:9],
                    f"{c.current:.2f}" if c.current else "—",
                    pct_colored(c.percent),
                    pct_colored(c.cum_pct) if c.cum_pct else "—",
                    f"{c.vol_ratio:.2f}" if c.vol_ratio > 0 else "—",
                    f"{c.rec_days_ago}日",
                    f"{c.score:.0f}",
                    c.rec_category,
                ],
                COLS_HIST,
            )
            + _watch_tail_terminal(c.ff_pct, c.beauty)
        )
    print(f"  {'-' * 92}")
    # print(
    #     f"  判据=今日回调 ≤{HIST_DIP_PCT:.0f}% 且 量比 ≥{HIST_MIN_VOL_RATIO:.1f}"
    #     f"（未缩量·有承接）| 距上次 v1 ≤{HIST_LOOKBACK_DAYS} 交易日 | 已剔除今日已推荐票"
    # )
    # 通用风险门与飙升区同一份实现（scanner/display_gates.py），故这里只列**本区参数**：
    # 市值上限 500 亿；其余（ST/非创业板/无报价/价格>200元/资金流出≤-8%）与另两区同值。
    # print(
    #     f"  通用风险门（与 v1 池选·沪深飙升同源）：ST·非创业板·无有效报价·价格>{MAX_STOCK_PRICE:.0f}元·"
    #     f"市值>{HIST_MAX_MARKET_CAP / 1e8:.0f}亿·资金流出≤{FUND_OUTFLOW_NET_PCT:.0f}%"
    # )
    # print(
    #     f"  排序=回调深度{HIST_W_DIP:.0f}+量能{HIST_W_VOL:.0f}+时效{HIST_W_RECENCY:.0f}"
    #     f"｜启发式排序·未做样本外校准（本区为观察窗口，非选股主线）"
    # )
    # 行尾标记图例（2026-09-16）：飞书卡片有一份同义图例（build_feishu_card 的回捞节脚注），
    # 两处须同步改 —— 守卫 tests/test_display.py::test_hist_legend_printed_on_both_surfaces。
    # 两档的分档语义必须在**本区就地**说清，否则最自然的读法都是错的：
    #   ▲▼ 只回答「-8% 以上这一段的强弱」（≤-8% 已被硬门剔除，故 ▼▼ 不可达）；
    #   美 表示「尾部回撤更小」，不是「更可能大涨」（trend_beauty 分档实测 hit 低于基线）。
    # print(
    #     f"  标记：{ANSI['GREEN']}▲▲/▲{ANSI['RESET']}=主力净流入(≥+8%/≥+5%)　"
    #     f"{ANSI['RED']}▼{ANSI['RESET']}=净流出(≤-5%；≤-8% 已被硬门剔除，故不出现 ▼▼)　"
    #     f"{ANSI['GREEN']}美{ANSI['RESET']}=日线趋势漂亮（尾部回撤更小·非更易大涨；本区无分时档，不会出现美★）"
    # )


def render_hist_watch_standalone(rows) -> None:
    """只渲染「v1 回捞」区（供 `python -m scanner.historical_watch` 独立运行）。

    与主循环的 render_terminal 共用同一个 _render_hist_watch_region，避免两套渲染
    逻辑分叉。
    """
    _render_hist_watch_region(rows)


def render_terminal(view: ScanView) -> None:
    """把 ScanView 渲染到终端（纯渲染：不读库、不重算标记）。

    与 build_scan_view 分离的收益：飞书卡片可复用同一视图，杜绝此前「终端读 DB
    当日累计推荐 / 飞书读本轮候选桶」的选择分叉（同一只票两边排位可能不一致）。
    """
    for _w in view.warnings:
        print(f"  [!] {_w}")

    # 展示层资金流出硬门（2026-09-14）：跨区域生效，故提示放在所有区块之前——
    # 否则用户只会看到"某些票不见了"却不知道为什么（过滤在 build_scan_view 一处完成）。
    if view.flow_filtered:
        print(
            f"  {ANSI['YELLOW']}▸ 资金流出已剔除 {view.flow_filtered} 只"
            f"（主力净占比 ≤ {FUND_OUTFLOW_NET_PCT:.0f}% · 全区域统一口径）{ANSI['RESET']}"
        )

    # 终选参考区（2026-09-08 起与决策层合并渲染；2026-09-14 决策层删除后本区独立）。
    # 市况门状态（scanner.decision.market_gate）决定标题措辞：门关时标注「仅观察参考」。
    if view.final_pick_lines:
        print("=" * 78)
        print("◆ 终选参考 — 若必须持仓买谁（空仓是合法输出）")
        _fp_header = view.final_pick_lines[0] if view.final_pick_lines else ""
        # 市况门状态由 final_pick 标题携带（「⚠大盘门关·仅观察参考」），不再从决策层行推断
        # ——决策层已于 2026-09-14 删除，标题是门状态的唯一可见来源。
        _gate_open = "大盘门关" not in _fp_header
        if _gate_open:
            print("  ── 终选参考（合池·次日概率排序）──")
        else:
            print("  ── 终选参考（门关·仅观察参考）──")
        for _fpl in view.final_pick_lines[1:]:
            print(f"  {_fpl}")
        print("=" * 78)

    # ── 主表 / v2 池选区共用行渲染（同列 spec，行尾标记与回马枪/低吸区同源）──
    def _emit_pool_table_row(view: ScanView, row: MainRow, idx: int) -> None:
        _e = row.entry
        _nm = _e["name"]
        _nm_disp = f"{ANSI['BOLD']}{ANSI['MAGENTA']}{_nm}{ANSI['RESET']}" if row.core else _nm
        _av_str = f"{row.accum:+.2f}%" if row.accum is not None else "—"
        _rk_val = str(row.rank) if row.rank is not None else "—"
        _sc_str = f"{row.score:.0f}" if row.score else "—"
        _cur_str = f"{row.current:.2f}" if row.current else "—"
        _sec = _trunc(row.sector, COLS_POOL[7][1])
        # 行尾标记与低吸区同源（_entry_row_suffix）：风险/资金流/⚡。
        # 💡低吸标签不再行尾展示（太杂乱，2026-09-03），仅作两段式排序依据（_v2_pool_sort_key）。
        _bolt = view.breakout_mark.get((_e["symbol"], _e["category"]), False)
        _suffix = _entry_row_suffix(
            _e,
            view.flow_pct_map,
            breakout_marked=_bolt,
            beauty=(view.beauty_mark or {}).get((_e["symbol"], _e["category"]), ""),
        )
        print(
            _table_row(
                [
                    str(idx),
                    _e["symbol"],
                    _nm_disp,
                    pct_colored(row.pct),
                    _av_str,
                    _cur_str,
                    _rk_val,
                    _sec,
                    _sc_str,
                    row.cat_label,
                ],
                COLS_POOL,
            )
            + _suffix
        )

    # ── v1 池选 ──
    print(f"  {ANSI['BOLD']}◆ v1 池选 — 榜上优先·涨幅升序·回调核心{ANSI['RESET']}")
    # 通用风险门清单（2026-09-16）：三个展示区共用一个实现（scanner/display_gates.py），
    # 故这里把「哪些门在起作用」显式打出来 —— 此前只有飙升/回捞两区写了脚注，
    # 主展示区什么都看不到，用户无从判断「这只票到底过没过风控」。
    # ⚠ 主线这批门**在扫描期施加**（candidates.filter_gem_stocks + pipeline.pool +
    # assemble 的资金流出过滤），展示期不再重复判一遍；飙升/回捞两区没有扫描链路，
    # 在各自取数时判。差别只在**何时判**，不在**判什么**。
    print(
        f"  {ANSI['YELLOW']}▸ 风险门（与沪深飙升 · v1 回捞 同源）：ST·非创业板·停牌/无成交·"
        f"价格>{MAX_STOCK_PRICE:.0f}元·市值>{MAX_MARKET_CAP / 1e8:.0f}亿·资金流出≤{FUND_OUTFLOW_NET_PCT:.0f}%{ANSI['RESET']}"
    )
    # 美感标记分档图例（2026-09-15）：仅在确有标记时打一行，避免常年占位。
    # ★ 必须就地解释成「回撤更小」——否则最自然的误读是「更可能大涨」，而数据不支持
    # （美★ 与 美 的 next_day hit 无正向区分度，只有尾部回撤有差别，见 trend_beauty docstring）。
    # 飞书卡片 build_feishu_card 有一份同义图例，两处须同步改（守卫见 test_display）。
    if any((view.beauty_mark or {}).values()):
        print(
            f"  {ANSI['GREEN']}美{ANSI['RESET']}=日线趋势漂亮　"
            f"{ANSI['GREEN']}美★{ANSI['RESET']}=分时亦漂亮（尾部回撤更小·非更易大涨）"
        )
    print(_table_header(COLS_POOL))
    for _si, row in enumerate(view.main_rows, 1):
        _emit_pool_table_row(view, row, _si)

    # ── ⚡ 蓄势突破观察（动态推荐区已按需求移除，2026-09-03；adj_picks 仍在 ScanView 保留供复用）──
    if any(view.breakout_mark.values()):
        print(
            f"  {ANSI['CYAN']}⚡ 蓄势突破观察{ANSI['RESET']}（缩量回调蓄势位·含新面孔/重上榜两变体"
            f"·样本收集中·非排序因子）"
        )

    # 2026-09-14 按用户决策隐藏的两个展示区（需复原见 git 历史）：
    #   ◆ v2 池选（2026-09-02 上线，双跑同屏）—— 池→排雷→低吸匹配。
    #   ◆ 核心方向低吸（2026-08-19 上线）—— 主线方向核心股回调参考。
    # 两者的数据仍参与终选参考区合池（见 assemble.build_scan_view），只是不再单独成区。

    # ── 沪深飙升·极有可能大涨 独立区（2026-09-11 自 rts-xueqiu 合入）──
    # 与上方所有区块口径不同且互不干扰：样本面为**创业板**（300/301，与主线一致；
    # 2026-09-11 合入时曾为沪深主板+创业板，后收窄到创业板，见 hot_watch.hard_exclude），
    # 口径为「当日 momentum + 榜单热度跃升」（主线为 next_day 次日大涨）。
    # 独立成区而非并入主线表：两者排序键、评分体系、样本面都不同，混排会让
    # 「为什么这两只票排在同一个榜里」无法解释。
    # ── v1 回捞 独立区（2026-09-16）──
    # 候选来自 recommendations（前 N 个交易日的 v1 产出），与上方 v1 池选的「今日在榜票」
    # 样本域**互斥**（默认剔除今日已推荐票）—— 并列为两区而不是合并成一区，正是因为
    # 同一只票不可能同时出现在两边，不存在「同屏两种结论」的风险。
    _render_hist_watch_region(view.hist_rows)

    _render_hot_watch_region(view.hot_rows)


def display_priority(
    conn=None,
    live_quotes: dict[str, dict] | None = None,
    rank_map: dict[str, int] | None = None,
    today_pool: dict[str, Candidate] | None = None,
    last_ranks: dict[str, int] | None = None,
    weak: bool | None = None,
    hot_rows: list | None = None,
    hist_rows: list | None = None,
) -> "ScanView | None":
    """构建展示视图并渲染到终端（build_scan_view + render_terminal 的便捷入口）。

    weak：市况信号（弱市布尔）。None 时由 build_scan_view 内部按 _regime_weak 自算；
    传入则复用（display 主屏已在打印头部前算过一次，避免重复查询）。
    返回 ScanView 供复用（display 主屏回传飞书 / 测试捕获输出后取数据两用）；
    无 conn 或今日无推荐时返回 None。
    """
    view = build_scan_view(
        conn=conn,
        live_quotes=live_quotes,
        rank_map=rank_map,
        today_pool=today_pool,
        last_ranks=last_ranks,
        weak=weak,
        hot_rows=hot_rows,
        hist_rows=hist_rows,
    )
    if view is None:
        return None
    render_terminal(view)
    return view
