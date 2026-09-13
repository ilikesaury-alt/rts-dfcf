import os
import re

from scanner.config import (
    HOT_HIGHLIGHT_STREAK,
    HOT_MAX_MARKET_CAP,
    HOT_MAX_PERCENT,
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

# 走势美感标记判定单源（与美感门同源；相对导入绕开 pyright 会话冻结快照的绝对名解析）
from scanner.utils import clear_screen, to_int
from scanner.view.assemble import *  # noqa: F401,F403
from scanner.view.model import *  # noqa: F401,F403

# ANSI SGR 转义序列（\x1b[...m：颜色/加粗/复位）。_vis_len 必须先剥离它们再量宽度，
# 否则 `[`、数字、`;`、`m` 等可打印字符各被 wcwidth 计 1 列，彩色文本被高估宽度，
# _pad 少补空格 → 实际渲染更窄 → 后续固定列整体错位。
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

if os.name == "nt":
    import ctypes

    _kernel32 = ctypes.windll.kernel32
    _handle = _kernel32.GetStdHandle(-11)
    _mode = ctypes.c_uint32()
    # 是否「真实 Windows conhost」：GetConsoleMode 仅对真实控制台成功；
    # pty/终端模拟器/重定向管道均失败（返回 0），但它们通常讲 ANSI/VT 协议。
    _is_console = _kernel32.GetConsoleMode(_handle, ctypes.byref(_mode)) != 0
    _supports_ansi = _is_console and _kernel32.SetConsoleMode(_handle, _mode.value | 0x0004) != 0
else:
    _is_console = False
    _supports_ansi = True

if _supports_ansi:
    ANSI = {
        "RED": "\033[91m",
        "YELLOW": "\033[93m",
        "GREEN": "\033[92m",
        "CYAN": "\033[96m",
        "MAGENTA": "\033[95m",
        "BOLD": "\033[1m",
        "RESET": "\033[0m",
    }
else:
    ANSI = {"RED": "", "YELLOW": "", "GREEN": "", "CYAN": "", "MAGENTA": "", "BOLD": "", "RESET": ""}

# 类别展示标签/颜色：综合排序与回马枪独立区共用（提出模块级供 _print_priority_row 复用）。
# 2026-08-20 收敛：CAT_LABEL / 颜色键统一来自 scanner/categories 注册表（单一事实来源），
# 颜色键经本模块 ANSI 字典解析为色码，避免与 config 循环依赖。
from scanner.categories import CAT_LABEL, CATEGORY_COLOR_KEYS  # noqa: E402

CAT_COLOR = {name: ANSI[key] for name, key in CATEGORY_COLOR_KEYS.items()}


__all__ = (
    "ANSI",
    "CAT_COLOR",
    "_ANSI_ESCAPE",
    "_fmt_hot_amount",
    "_fmt_hot_volume_hand",
    "_handle",
    "_is_console",
    "_kernel32",
    "_mode",
    "_print_priority_row",
    "_render_hot_watch_region",
    "_supports_ansi",
    "_table_header",
    "_table_row",
    "display",
    "display_priority",
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
    decision_lines: list[str] | None = None,
) -> "ScanView | None":
    """扫描主屏：头部摘要 + 展示视图（构建/渲染委托 display_priority）。

    ScanView 定义在本文件更下方（视图模型区），此处为前向引用故写成字符串注解；
    render_terminal / build_scan_view 均在类定义之后，无需引号。

    返回本轮 ScanView 供飞书复用（同一份选择，避免两端分叉与重复计算）；
    conn 为空或今日无推荐时返回 None。

    today_pool：本轮候选池快照（symbol → Candidate），由调用方（scan_with_raw 的
    ScanResult）传入，display 不直接访问 orchestrator 内部状态。
    last_ranks: 上一轮扫描的榜单排名 {symbol: rank}，供「排名」列显示变化（+N 升 / -N 降）。
    decision_lines：主循环已落库算好的决策层文本行（2026-09-13，落库移出视图层）。
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
        decision_lines=decision_lines,
    )
def _print_priority_row(
    entry: RecommendationRow | dict,
    i: int,
    flow_pct_map: dict,
    nextday_mark: bool = False,
    breakout_mark: bool = False,
    last_ranks: dict[str, int] | None = None,
) -> None:
    """综合排序单行的统一渲染（主表与回马枪独立区共用），避免两处复制大段渲染逻辑。

    flow_pct_map: {symbol: 主力净占比} DB 快照回退（候选缺失/扫描失败时仍显示资金流图标）。
    nextday_mark: 次日大涨画像（🎯）——推荐时刻涨幅甜蜜带 + 非超买（见 is_nextday_marked）。
    breakout_mark: 蓄势突破观察画像（⚡）——新面孔/首推或重上榜 short_term + 横盘缩量回调位
    （见 _is_breakout_setup / _is_relist_breakout_setup；2026-08-22 渲染合并为单一 ⚡，
    变体区分保留在判定函数供样本统计）。纯观察标记，不参与排序/评分/落库。
    视觉标记，不参与排序/评分/落库；行尾标记统一走 _entry_row_suffix（与优选池行同口径）。
    last_ranks: 上一轮扫描的榜单排名 {symbol: rank}，用于「排名」列展示雪球榜单排名变化
    （+N 升 / -N 降），与已下线策略桶的 _rank_delta_str 同口径；缺省 None 不显示变化。
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
    # 行尾标记（风险/资金流/连板/🎯/⚡）走 _entry_row_suffix 单源，与优选池行同口径。
    tail = _entry_row_suffix(entry, flow_pct_map, marked=nextday_mark, breakout_marked=breakout_mark)
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
        f"（沪深主板+创业板 · 当日动能+热度跃升 · 与上方主线口径独立）"
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
        )
    print(f"  {'-' * 92}")
    print(
        f"  排序=评分(排名上升35/涨幅25/价格15/量能25) | 已剔除涨停·涨幅>{HOT_MAX_PERCENT:.0f}%·"
        f"市值>{HOT_MAX_MARKET_CAP / 1e8:.0f}亿·ST·科创板/北交所/ETF | "
        f"连击≥{HOT_HIGHLIGHT_STREAK}轮标★"
    )


def render_hot_watch_standalone(rows) -> None:
    """只渲染「沪深飙升·极有可能大涨」区（供 `python -m scanner.hot_watch` 独立运行）。

    与主循环的 render_terminal 共用同一个 _render_hot_watch_region，避免两套渲染
    逻辑分叉（独立区行宽/配色/脚注只此一份）。
    """
    _render_hot_watch_region(rows)
def render_terminal(view: ScanView) -> None:
    """把 ScanView 渲染到终端（纯渲染：不读库、不重算标记）。

    与 build_scan_view 分离的收益：飞书卡片可复用同一视图，杜绝此前「终端读 DB
    当日累计推荐 / 飞书读本轮候选桶」的选择分叉（同一只票两边排位可能不一致）。
    """
    for _w in view.warnings:
        print(f"  [!] {_w}")

    # 今日决策 + 终选参考合并展示（2026-09-08）：一个区块用子标题区分
    # 「该不该买」+「必须持仓时买谁」——避免用户混淆两个区块的用途。
    if view.decision_lines or view.final_pick_lines:
        print("=" * 78)
        print("◆ 今日决策 — 该不该买 + 买谁（空仓是合法输出）")
        if view.decision_lines:
            print("  ── 市场门 ──")
            # decision_lines[0] 是原 header，跳过；从 gate_reason 行开始
            for _dl in view.decision_lines[1:]:
                print(f"  {_dl}")
            print("  ── 决策推荐 ──")
        if view.final_pick_lines:
            _fp_header = view.final_pick_lines[0] if view.final_pick_lines else ""
            _gate_open = any("允许开仓" in dl for dl in (view.decision_lines or []))
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
        # 行尾标记与低吸区同源（_entry_row_suffix）：风险/资金流/🎯/⚡。
        # 💡低吸标签不再行尾展示（太杂乱，2026-09-03），仅作两段式排序依据（_v2_pool_sort_key）。
        _marked = view.nextday_mark.get((_e["symbol"], _e["category"]), False)
        _bolt = view.breakout_mark.get((_e["symbol"], _e["category"]), False)
        _suffix = _entry_row_suffix(
            _e,
            view.flow_pct_map,
            marked=_marked,
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
    print(_table_header(COLS_POOL))
    for _si, row in enumerate(view.main_rows, 1):
        _emit_pool_table_row(view, row, _si)

    # ── v2 池选独立区（双跑同屏，2026-09-02）：pool→danger→低吸匹配输出 ──
    if view.pool_rows:
        _pool_top = len(view.pool_rows)
        _pool_cnt = f"（前{_pool_top}/共{view.pool_total}只）" if view.pool_total > _pool_top else ""
        print(
            f"\n{ANSI['BOLD']}◆ v2 池选 — 池→排雷→低吸匹配（排名升序→低吸标签优先→涨幅降序）{_pool_cnt}{ANSI['RESET']}"
        )
        print(_table_header(COLS_POOL))
        for _vi, row in enumerate(view.pool_rows, 1):
            _emit_pool_table_row(view, row, _vi)
        print(f"  {'-' * 92}")

    # ── ⚡ 蓄势突破观察（动态推荐区已按需求移除，2026-09-03；adj_picks 仍在 ScanView 保留供复用）──
    if any(view.breakout_mark.values()):
        print(
            f"  {ANSI['CYAN']}⚡ 蓄势突破观察{ANSI['RESET']}（缩量回调蓄势位·含新面孔/重上榜两变体"
            f"·样本收集中·非排序因子）"
        )

    # ── 核心方向低吸独立区（2026-08-19，scanner/core_themes.py）──
    # 大跌市中找「当前主线方向核心股低吸」参考。2026-08-19 起随扫描落库
    # category=core_dip（同 comeback 族），本区从今日 recommendations 读取。
    if view.show_core_dip:
        print(f"\n{ANSI['GREEN']}◆ 核心方向低吸 — 主线方向核心股回调参考（主区稀少·补充参考）{ANSI['RESET']}")
        print(_table_header(COLS_DETAIL))
        for di, entry in enumerate(view.core_dip_rows, 1):
            _print_priority_row(entry, di, view.flow_pct_map, last_ranks=view.last_ranks)
        print("  排序=今日波动（涨多/跌狠优先）→主力回流→回撤深→龙头强。")
        print(f"  {'-' * 92}")

    # ── 沪深飙升·极有可能大涨 独立区（2026-09-11 自 rts-xueqiu 合入）──
    # 与上方所有区块口径不同且互不干扰：样本面为沪深主板+创业板（主线只做创业板），
    # 口径为「当日 momentum + 榜单热度跃升」（主线为 next_day 次日大涨）。
    # 独立成区而非并入主线表：两者排序键、评分体系、样本面都不同，混排会让
    # 「为什么这只创业板票排在一只主板票后面」无法解释。
    _render_hot_watch_region(view.hot_rows)
def display_priority(
    conn=None,
    live_quotes: dict[str, dict] | None = None,
    rank_map: dict[str, int] | None = None,
    today_pool: dict[str, Candidate] | None = None,
    last_ranks: dict[str, int] | None = None,
    weak: bool | None = None,
    hot_rows: list | None = None,
    decision_lines: list[str] | None = None,
) -> "ScanView | None":
    """构建展示视图并渲染到终端（build_scan_view + render_terminal 的便捷入口）。

    weak：市况信号（弱市布尔）。None 时由 build_scan_view 内部按 _regime_weak 自算；
    传入则复用（display 主屏已在打印头部前算过一次，避免重复查询）。
    返回 ScanView 供复用（display 主屏回传飞书 / 测试捕获输出后取数据两用）；
    无 conn 或今日无推荐时返回 None。

    decision_lines：主循环已落库算好的决策层文本行，透传给 build_scan_view
    （2026-09-13：不传时 build_scan_view 会自己算一份纯的，不落库）。
    """
    view = build_scan_view(
        conn=conn,
        live_quotes=live_quotes,
        rank_map=rank_map,
        today_pool=today_pool,
        last_ranks=last_ranks,
        weak=weak,
        hot_rows=hot_rows,
        decision_lines=decision_lines,
    )
    if view is None:
        return None
    render_terminal(view)
    return view
