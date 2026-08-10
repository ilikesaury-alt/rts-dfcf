import os

import wcwidth

from scanner.config import (
    CAT_DISPLAY_PRIORITY,
    FUND_FLOW_MAIN_PCT_EXTREME,
    FUND_FLOW_MAIN_PCT_STRONG,
    FUND_FLOW_MAIN_PCT_WEAK,
    NEXTDAY_CAT_PRIORITY,
    NEXTDAY_SPIKE_MID_MAX,
    NEXTDAY_SPIKE_MID_MIN,
    NEXTDAY_SPIKE_SWEET_LOW,
    NEXTDAY_SPIKE_SWEET_MIN,
    RISK_FLAGS_DISPLAY_HARD,
    SUGGEST_BY_CAT,
    now_beijing,
)
from scanner.database import get_fund_flow_pct_map, get_prominence_map, get_today_recommendations
from scanner.models import Candidate
from scanner.orchestrator import _session_state
from scanner.sector import classify_sector

if os.name == "nt":
    import ctypes
    _kernel32 = ctypes.windll.kernel32
    _handle = _kernel32.GetStdHandle(-11)
    _mode = ctypes.c_uint32()
    _supports_ansi = (
        _kernel32.GetConsoleMode(_handle, ctypes.byref(_mode)) != 0
        and _kernel32.SetConsoleMode(_handle, _mode.value | 0x0004) != 0
    )
else:
    _supports_ansi = True

if _supports_ansi:
    ANSI = {
        "RED": "\033[91m", "YELLOW": "\033[93m", "GREEN": "\033[92m",
        "CYAN": "\033[96m", "BOLD": "\033[1m", "RESET": "\033[0m",
    }
else:
    ANSI = {"RED": "", "YELLOW": "", "GREEN": "", "CYAN": "", "BOLD": "", "RESET": ""}

# 类别展示标签/颜色：综合排序与回马枪独立区共用（提出模块级供 _print_priority_row 复用）。
CAT_LABEL = {
    "known_new_face": "kNF", "rebound": "RBD", "new_face": "NEW",
    "momentum": "MOM", "short_term": "ST", "pullback": "PB",
    "comeback": "CB",
}
CAT_COLOR = {
    "known_new_face": ANSI["GREEN"], "rebound": ANSI["CYAN"], "new_face": ANSI["GREEN"],
    "momentum": ANSI["YELLOW"], "short_term": ANSI["RED"], "pullback": ANSI["RED"],
    "comeback": ANSI["CYAN"],
}


def _vis_len(s: str) -> int:
    # wcwidth.wcwidth 对 ANSI 转义字符（如 \x1b）返回 -1（控制字符），
    # `-1 or 1` 在 Python 中返回 -1（truthy），导致宽度计算错误、列对齐错位。
    # 用 max(0, ...) 确保控制字符贡献 0 宽度。
    return sum(max(0, wcwidth.wcwidth(c)) for c in s)


def _pad(s: str, width: int, align: str = "l") -> str:
    pad = max(0, width - _vis_len(s))
    return f"{' ' * pad}{s}" if align == "r" else f"{s}{' ' * pad}"


def _trunc(s: str, width: int) -> str:
    """按可见宽度截断（中文全角按 2 列计），超长时尾部补 …。"""
    if _vis_len(s) <= width:
        return s
    out = ""
    for ch in s:
        if _vis_len(out) + max(0, wcwidth.wcwidth(ch)) > width - 1:
            break
        out += ch
    return out + "…"


def clear_screen():
    if os.name == "nt":
        os.system("cls")
    else:
        print("\033[2J\033[H", end="")


def pct_colored(pct: float | None, width: int = 8) -> str:
    if pct is None:
        pct = 0.0
    s = f"{pct:+.2f}%"
    if pct >= 9:
        c = ANSI["RED"]
    elif pct >= 5:
        c = ANSI["GREEN"]
    elif pct < 0:
        c = ANSI["YELLOW"]
    else:
        c = ""
    return f"{c}{s:>{width}}{ANSI['RESET']}" if c else f"{s:>{width}}"


def fund_flow_signal(main_pct: float | None) -> str:
    """主力净占比 → 强弱档位（与 enhancer 加分/资金流出标签阈值同源）。

    返回 strong_in / in / neutral / out / strong_out；无数据返回 ""。
    """
    if main_pct is None:
        return ""
    if main_pct >= FUND_FLOW_MAIN_PCT_EXTREME:
        return "strong_in"
    if main_pct >= FUND_FLOW_MAIN_PCT_STRONG:
        return "in"
    if main_pct <= -FUND_FLOW_MAIN_PCT_EXTREME:
        return "strong_out"
    if main_pct <= FUND_FLOW_MAIN_PCT_WEAK:
        return "out"
    return "neutral"


def split_risk_flags(risk_flags: list[str]) -> tuple[list[str], int]:
    """风险标签分级：返回 (硬信号列表, 软信号数量)。

    硬信号（RISK_FLAGS_DISPLAY_HARD：超买/主力出货/趋势破位）展开文字显示，
    软信号折叠成 +N 角标。display/feishu 共用，避免两处各自维护阈值集合。
    """
    hard = [f for f in risk_flags if f in RISK_FLAGS_DISPLAY_HARD]
    return hard, len(risk_flags) - len(hard)


def _render_prominence(prominence_labels: list[str] | None) -> str:
    """辨识度标签（↻ 等）渲染；空列表返回空串。返回段无前导空格，分隔由调用方处理。"""
    if not prominence_labels:
        return ""
    return f"{ANSI['CYAN']}│{' '.join(prominence_labels)}│{ANSI['RESET']}"


_FUND_FLOW_ICON = {
    "strong_in": f"{ANSI['GREEN']}▲▲{ANSI['RESET']}",
    "in": f"{ANSI['GREEN']}▲{ANSI['RESET']}",
    "neutral": f"{ANSI['YELLOW']}◇{ANSI['RESET']}",
    "out": f"{ANSI['RED']}▼{ANSI['RESET']}",
    "strong_out": f"{ANSI['RED']}▼▼{ANSI['RESET']}",
}


def _fund_flow_icon_str(ff_pct) -> str:
    """主力净占比 → 5 档图标（ANSI 着色）；无数据返回空串。"""
    if ff_pct is None:
        return ""
    return _FUND_FLOW_ICON.get(fund_flow_signal(float(ff_pct)), "")


def _entry_sector_display(entry: dict, c) -> str:
    """选股建议行的板块显示：候选推动概念 > 分类板块 > DB concept > 名称关键词。"""
    if c is not None:
        if getattr(c, "driving_concept", ""):
            return c.driving_concept
        if getattr(c, "sector", ""):
            return c.sector
    db_concept = (entry.get("concept") or "").strip()
    if db_concept:
        return db_concept
    return classify_sector(entry.get("name", ""))


def _market_extra_str(c: Candidate) -> str:
    """行情增强标记：主力资金流强弱图标 + 连板/炸板（无数据返回空串）。

    资金流用 fund_flow_signal 5 档图标替代原「资+x.x% ±xxx万」文本；
    展示型信息，追加在行尾可变区，不参与固定列对齐。
    """
    dims = c.kline.dimensions if c.kline else {}
    parts = []
    ff_pct = dims.get("fund_flow_main_pct")
    if ff_pct is not None:
        icon = _fund_flow_icon_str(ff_pct)
        if icon:
            parts.append(icon)
    zt_lb = dims.get("zt_lianban")
    zt_zb = dims.get("zt_zhaban")
    if zt_lb:
        if zt_zb:
            parts.append(f"{ANSI['YELLOW']}连{zt_lb}炸{zt_zb}{ANSI['RESET']}")
        else:
            parts.append(f"{ANSI['RED']}连{zt_lb}板{ANSI['RESET']}")
    return " ".join(parts) if parts else ""


def _market_env_tag() -> str:
    """大盘环境标签（中性/强势/弱势·谨慎），从候选池 dims 读 market_env_bonus。"""
    env_bonus = 0
    for c in _session_state.today_pool.values():
        if c.kline and c.kline.dimensions:
            env_bonus = c.kline.dimensions.get("market_env_bonus", 0) or 0
            break
    if env_bonus > 0:
        return f"{ANSI['GREEN']}[大盘强势]{ANSI['RESET']}"
    if env_bonus < 0:
        return f"{ANSI['RED']}[大盘弱势·谨慎]{ANSI['RESET']}"
    return "[大盘中性]"


def display(gem_total: int, interval: int, filtered_large_cap: int = 0,
            conn=None, live_quotes: dict[str, dict] | None = None,
            rank_map: dict[str, int] | None = None):
    """扫描主屏：头部摘要 + 综合排序总表（含回马枪/次日大涨/选股建议子区）。

    策略桶（新面孔/动量/反弹/回马枪/超短）2026-08-10 下线：与综合排序重复列同一批票、
    每桶重复列头；综合排序表已带类别标签，桶区信息不再单列。
    """
    clear_screen()
    now = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{'='*96}")
    print(f"  创业板飙升榜监控  ({now})")
    filter_info = f" | 过滤{filtered_large_cap}只" if filtered_large_cap else ""
    print(f"  创业板共 {gem_total} 只{filter_info} | 每{interval}s刷新 | {_market_env_tag()}")
    print(f"{'='*96}")
    display_priority(conn, live_quotes=live_quotes, rank_map=rank_map)


def _print_priority_row(entry: dict, i: int, flow_pct_map: dict) -> None:
    """综合排序单行的统一渲染（主表与回马枪独立区共用），避免两处复制大段渲染逻辑。

    flow_pct_map: {symbol: 主力净占比} DB 快照回退（候选缺失/扫描失败时仍显示资金流图标）。
    """
    c = entry["_candidate"]
    sector = classify_sector(entry["name"])
    # 标签/优先级/建议列统一用 entry["category"]（与排序口径一致），
    # 不用 c.category：双挂票的 today_pool 按 symbol 覆盖会拿到 short_term 候选，
    # 而 DB 保留的是最高分行的 category（可能是 new_face），两者不一致会导致
    # 排到 new_face 档却显示 ST 标签 + 回避建议的矛盾。
    cat = entry["category"]
    # 板块列优先级：推荐时落库的推动概念 > 当前池候选的推动概念 > 分类板块 > 名称关键词
    db_concept = (entry.get("concept") or "").strip()
    if db_concept:
        sector = db_concept
    elif c:
        if c.driving_concept:
            sector = c.driving_concept
        elif c.sector:
            sector = c.sector
    # 涨幅/现价/排名统一回退链：实时行情(live_quotes/rank_map) → 候选池当前扫描快照 →
    # appearances(DB) → 推荐时落库值。
    if entry.get("live_quote_available"):
        pct = entry.get("live_percent")
        if pct is None:
            pct = 0.0
        live_cur = entry.get("live_current", 0.0)
        live_rank = entry.get("live_rank")
    elif c:
        pct = c.stock.percent
        live_cur = c.stock.current
        live_rank = c.stock.rank
    else:
        # live_percent 可能为 0.0（合法 0.00% 涨幅），不能用 `or` 回退——
        # 否则 0.00% 的票会错误显示成推荐时落库的 percent（如 5.0%）。
        _lp = entry.get("live_percent")
        pct = _lp if _lp is not None else entry.get("percent", 0.0)
        live_cur = 0.0
        live_rank = entry.get("live_rank")
    # 实时行情不含 rank/current（batch/quote 无 rank 字段）时，回退候选快照。
    if not live_cur and c and c.stock.current:
        live_cur = c.stock.current
    if not live_rank and c and c.stock.rank:
        live_rank = c.stock.rank
    price_str = f"{live_cur:.2f}" if live_cur else "—"
    rank_str = f"{live_rank}" if live_rank else "—"
    label_display = f"{CAT_COLOR.get(cat, '')}{CAT_LABEL.get(cat, cat)}{ANSI['RESET']}"
    # 回马枪变体（反转/回踩）：策略桶下线后由此处单行保留，避免掉榜区丢失语义。
    if cat == "comeback":
        variant = ""
        if c:
            variant = getattr(c, "comeback_variant", "") or (
                c.kline.dimensions.get("comeback_variant", "") if c.kline else "")
        if not variant:
            trend = (entry.get("trend") or "")
            variant = trend.split("·")[0] if "·" in trend else ""
        if variant:
            label_display += f"{ANSI['CYAN']}·{variant}{ANSI['RESET']}"
    prom_labels = c.prominence_labels if c else (["↻"] if entry.get("_prominent") else [])
    prom_raw = _render_prominence(prom_labels)
    prom_str = f" {prom_raw}" if prom_raw else ""
    # 风险标记：掉榜/重启行 DB 无 risk_flags；候选行用与 _print_row 相同的分级渲染
    risk_str = ""
    if c and c.risk_flags:
        hard, soft_count = split_risk_flags(c.risk_flags)
        if hard:
            risk_str = f" {ANSI['RED']}⚠{'/'.join(hard)}{ANSI['RESET']}"
            if soft_count:
                risk_str += f"{ANSI['YELLOW']}+{soft_count}{ANSI['RESET']}"
        elif soft_count:
            risk_str = f" {ANSI['YELLOW']}⚠+{soft_count}{ANSI['RESET']}"
    # 建议列：按类别独立映射（与展示优先级解耦），SUGGEST_BY_CAT 含 ANSI 需用 _pad 对齐
    suggest_str = SUGGEST_BY_CAT.get(cat, "")
    first_time = entry.get("first_time", entry.get("time", ""))[:5]
    # 5日累计涨幅：优先用候选池的 kline 数据，否则用 DB 落库值
    accum_val = None
    if c and c.kline:
        accum_val = c.kline.accumulated_pct
    elif entry.get("accumulated_pct") is not None:
        accum_val = entry["accumulated_pct"]
    if accum_val is None:
        accum_str = "—"
    else:
        accum_str = f"{accum_val:+.2f}%"
    extra_str = ""
    if c:
        extra_str = _market_extra_str(c)
        ff_pct = c.kline.dimensions.get("fund_flow_main_pct") if c.kline else None
    else:
        ff_pct = None
    if ff_pct is None:
        # 扫描时无资金流维度（掉榜/拉取失败）回退 DB 快照图标
        icon = _fund_flow_icon_str(flow_pct_map.get(entry["symbol"]))
        if icon:
            extra_str = f"{extra_str} {icon}".strip() if extra_str else icon
    extra_suffix = f" {extra_str}" if extra_str else ""
    print(f"  {i:3d}  {entry['symbol']:<12} {_pad(entry['name'],10)} "
          f"{_pad(_trunc(sector,14),14)} {_pad(label_display,5,'r')} {entry['score']:4d} {pct_colored(pct)} "
          f"{accum_str:>8} {price_str:>7} {rank_str:>4} {_pad(first_time,6)} {_pad(suggest_str,6)}{prom_str}{risk_str}{extra_suffix}")


def _nextday_entry_percent(entry: dict) -> float:
    """推荐时刻盘中涨幅（用于次日大涨候选区筛形）。

    回退链：候选池当前扫描快照（最新）→ live_quotes（有实时覆盖）→ DB 落库 percent。
    次日大涨画像依据的是「推荐时刻涨幅带」，故优先用扫描快照的 percent
    （与 nextday_attribution 落库 percent 同源）；实时 live_percent 可能已随盘中
    涨跌漂移，仅在无候选/无落库时兜底。
    """
    c = entry.get("_candidate")
    if c and c.stock:
        return float(c.stock.percent)
    if entry.get("live_quote_available") and entry.get("live_percent") is not None:
        return float(entry["live_percent"])
    return float(entry.get("percent", 0.0))


def _in_nextday_sweet_band(percent: float) -> bool:
    """推荐时刻涨幅是否落在次日大涨甜蜜带（<2% 或 4~8%）。

    数据（scanner.nextday_attribution，next_day≥7%）：<1% hit 11.7%、1-2% hit 13.2%、
    4-6% hit 11.8%、6-8% hit 11.8%；2-4% 死区（6.2%）、8-10% 陷阱（7.5%，平均 -1.42%）。
    """
    return (NEXTDAY_SPIKE_SWEET_MIN <= percent < NEXTDAY_SPIKE_SWEET_LOW
            or NEXTDAY_SPIKE_MID_MIN <= percent < NEXTDAY_SPIKE_MID_MAX)


def _entry_prominent(entry: dict) -> bool:
    """辨识度（↻）：候选行用扫描时标签，掉榜/重启行用 DB appearances 计算的 _prominent。

    与 _print_priority_row 的 prom_labels 同口径，供次日大涨候选区排序复用。
    """
    c = entry.get("_candidate")
    if c:
        return bool(c.prominence_labels)
    return bool(entry.get("_prominent"))


def _nextday_spike_candidates(main_recs: list[dict]) -> list[dict]:
    """从综合排序主表中筛出「次日大涨画像」候选（display-only）。

    过滤条件（均来自 nextday_attribution 数据）：
      1. 推荐时刻涨幅在甜蜜带（低吸潜伏 <2% 或 中段启动 4~8%）；
      2. 排除 short_term 超买（死亡信号：hit 5% vs 非超买 10.5%）。
    排序：辨识度(↻)优先（2026-08-10：辨识度 hit 16~24% vs 非辨识度 6~10%，
    是当前最强单因子，复用已有 ↻ 标记）→ 类别优先级 → 评分降序。
    不改 score / 排序键 / 不落库——独立区纯展示观察窗口。
    """
    out = []
    for e in main_recs:
        if e["category"] not in NEXTDAY_CAT_PRIORITY:
            continue
        if not _in_nextday_sweet_band(_nextday_entry_percent(e)):
            continue
        c = e.get("_candidate")
        if c and c.kline and c.kline.dimensions:
            if (c.kline.dimensions.get("st_overbought_flag")
                    or c.kline.dimensions.get("mo_overbought_flag")
                    or c.kline.dimensions.get("v_st_overbought")
                    or c.kline.dimensions.get("v_mo_overbought")):
                continue  # 超买 = 次日大涨死亡信号
        out.append(e)
    out.sort(key=lambda x: (0 if _entry_prominent(x) else 1,
                            NEXTDAY_CAT_PRIORITY.get(x["category"], 99), -x["score"]))
    return out


def display_priority(conn=None, live_quotes: dict[str, dict] | None = None,
                     rank_map: dict[str, int] | None = None):
    """从本地数据库读取今日所有进入过推荐的票，按档位(辨识度/资金流)+展示优先级(CAT_DISPLAY_PRIORITY)+评分降序展示。

    live_quotes: {symbol: {percent, current}} 实时行情覆盖，优先于候选池和数据库数据。
    rank_map: {symbol: 飙升榜排名} 当前扫描的榜单排名，为掉榜/重启行补实时排名。
    排序键 = (档位, CAT_DISPLAY_PRIORITY, -score)：档0置前(辨识度或净流入≥5%) < 档1普通 <
    档2劣后(净流出≤-5%，覆盖辨识度)。档位只影响排序，不改评分列/不落库。
    劣后档（主力净流出 ≤ FUND_FLOW_MAIN_PCT_WEAK）直接不打印，被过滤出综合排序与回马枪区。
    """
    if conn is None:
        return

    today_recs = get_today_recommendations(conn)
    if not today_recs:
        return

    for entry in today_recs:
        pool_c = _session_state.today_pool.get(entry["symbol"])
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

    prom_syms = [e["symbol"] for e in today_recs if not e["_candidate"]]
    if prom_syms:
        prom_map = get_prominence_map(conn, prom_syms)
    else:
        prom_map = {}
    for entry in today_recs:
        if entry["_candidate"]:
            continue
        entry["_prominent"] = prom_map.get(entry["symbol"], False)

    # 资金流图标：从 market_extra_cache 直接读当日资金流，不依赖当前进程 today_pool。
    # 候选存在时优先用其扫描时的最新维度，否则（重启/掉榜/扫描时拉取失败）回退到 DB
    # 保存的全市场快照——避免综合排序大量行因进程重启丢失资金流图标。
    flow_pct_map = get_fund_flow_pct_map(conn, [e["symbol"] for e in today_recs])

    # 档位置顶（2026-08-06）：排序键 (档位, 类别优先级, -score)，跨类别全局生效。
    # 档0置前 = 辨识度(↻) 或 主力净流入 ≥ FUND_FLOW_MAIN_PCT_STRONG；档2劣后 = 净流出
    # ≤ FUND_FLOW_MAIN_PCT_WEAK（覆盖辨识度）；其余档1普通。只改排序，不改评分列/不落库。
    # 数据源与展示图标一致：资金流候选用扫描维度、否则回退 DB 快照 flow_pct_map；辨识度
    # 候选用扫描时标签、掉榜行用 prom_map——掉榜行 DB score 不含这些字段，展示层统一分档。
    def _sort_tier(entry):
        c = entry["_candidate"]
        ff = c.kline.dimensions.get("fund_flow_main_pct") if c and c.kline else None
        if ff is None:
            ff = flow_pct_map.get(entry["symbol"])
        prominent = bool(c.prominence_labels) if c else prom_map.get(entry["symbol"], False)
        # 档位阈值复用 fund_flow_signal 5 档映射（与图标/评分同源），避免独立比较漂移
        sig = fund_flow_signal(ff)
        if sig in ("out", "strong_out"):
            return 2
        if prominent or sig in ("in", "strong_in"):
            return 0
        return 1

    # 回马枪独立成区（2026-08-07 方案A）：comeback 是 off_list 掉榜跟踪票，语义与榜上票不同，
    # 从主排序表抽出放到末尾独立区块；主表只排榜上五类（rebound/known_new_face/new_face/
    # momentum/short_term，不含 comeback/pullback）。
    comeback_recs = [e for e in today_recs if e["category"] == "comeback"]
    main_recs = [e for e in today_recs if e["category"] != "comeback"]
    # 劣后档（主力净流出 ≤ FUND_FLOW_MAIN_PCT_WEAK）直接不打印，不进入排序与展示。
    main_recs = [e for e in main_recs if _sort_tier(e) != 2]

    # 2026-08-10: known_new_face 分数反指（回测分桶：低分档[18,37) cum_3d +5.58/64%胜率，
    # 高分档[77,98) -3.76/33%）——分区内 score 升序，把"低调二次上榜"的低分票排前，
    # 避免把最差的追高票顶在最前。其余类别仍降序。
    def _score_sort_key(entry):
        if entry["category"] == "known_new_face":
            return entry["score"]
        return -entry["score"]

    scored = sorted(main_recs, key=lambda x: (_sort_tier(x),
                                               CAT_DISPLAY_PRIORITY.get(x["category"], 99),
                                               _score_sort_key(x)))

    print(f"\n{ANSI['BOLD']}◆ 综合排序 — 今日上榜推荐{ANSI['RESET']}")
    hdr = (f"  {_pad('#',3,'r')} {_pad('代码',12)} {_pad('名称',10)} "
           f"{_pad('板块',14)} {_pad('策略',5)} {_pad('评分',4,'r')} {_pad('涨幅',8,'r')} "
           f"{_pad('5日累计',8,'r')} {_pad('现价',7,'r')} {_pad('排名',4,'r')} {_pad('时间',6)} {_pad('建议',6)}")
    print(hdr)
    for i, entry in enumerate(scored, 1):
        _print_priority_row(entry, i, flow_pct_map)
    print(f"  {'-'*92}")
    # 回马枪独立成区（方案A）：主表仅排榜上五类，comeback 抽到此处独立成区，
    # 仍按档位(tier)+评分排序，复用统一行渲染。comeback 为空则跳过（与旧行为一致）。
    if comeback_recs:
        cb_scored = [e for e in comeback_recs if _sort_tier(e) != 2]
        cb_scored = sorted(cb_scored, key=lambda x: (_sort_tier(x), -x["score"]))
        print(f"\n{ANSI['CYAN']}◆ 回马枪 — 掉榜跟踪/回调买点{ANSI['RESET']}")
        print(hdr)
        for ci, entry in enumerate(cb_scored, 1):
            _print_priority_row(entry, ci, flow_pct_map)
        print(f"  {'-'*92}")
    # 次日大涨候选独立区（2026-08-10）：display-only 观察窗口。
    # 依据 scanner.nextday_attribution：推荐时刻涨幅甜蜜带（<2% 低吸潜伏 / 4-8% 中段启动）
    # + 排除 short_term 超买死亡信号。只筛形展示，不改 score / 排序键 / 不落库。
    # 样本积累足够（nextday_attribution 归因稳定）后再考虑是否并入评分。
    nextday_cands = _nextday_spike_candidates(main_recs)
    if nextday_cands:
        print(f"\n{ANSI['GREEN']}◆ 次日大涨候选 — 低吸潜伏/中段启动（display-only 观察窗口，不改分）{ANSI['RESET']}")
        print(hdr)
        print(f"  {'-'*112}")
        for ni, entry in enumerate(nextday_cands, 1):
            _print_priority_row(entry, ni, flow_pct_map)
        print(f"  {'-'*92}")

    # 今日选股建议（2026-08-10）：跨类别 score 排序是反指（历史取前2 cum_3d -3.1%），
    # 选股按「类别+市场环境」而非分数（类别优先级 rebound>short_term>momentum，
    # 弱市回避动量，板块去重，排除负期望类别与硬风险/净流出）。置于末尾结论区。
    # 仅在进程内有真实候选关联时展示（today_pool 非空），避免在无候选上下文中
    # 输出干扰主表的额外行（display 测试契约：主表行数 = 推荐行数）。
    if _session_state.today_pool:
        from scanner.pick import build_pick_suggestion
        suggestion = build_pick_suggestion(today_recs)
        if suggestion["picks"]:
            print(f"\n{ANSI['BOLD']}{ANSI['CYAN']}◆ 今日选股建议（2只 · 类别>分数 · 弱市回避动量 · 板块去重）{ANSI['RESET']}")
            hdr_pick = (f"  {_pad('名称',10)} {_pad('板块',14)} "
                        f"{_pad('策略',5)} {_pad('评分',4,'r')} {_pad('涨幅',8,'r')}")
            print(hdr_pick)
            print(f"  {'-' * max(2, wcwidth.wcswidth(hdr_pick) - 2)}")
            for e in suggestion["picks"]:
                c = e.get("_candidate")
                cat = e.get("category", "")
                label = f"{CAT_COLOR.get(cat, '')}{CAT_LABEL.get(cat, cat)}{ANSI['RESET']}"
                pct = e.get("live_percent")
                if pct is None and c:
                    pct = c.stock.percent
                if pct is None:
                    pct = e.get("percent", 0.0)
                score = e.get("score", 0)
                sec = _entry_sector_display(e, c)
                print(f"  {_pad(e['name'],10)} {_pad(_trunc(sec,14),14)} "
                      f"{_pad(label,5,'r')} {score:4d} {pct_colored(pct)}")
            for r in suggestion["reasons"]:
                print(f"    {ANSI['YELLOW']}·{ANSI['RESET']} {r}")
        elif suggestion["reasons"]:
            print(f"\n{ANSI['YELLOW']}◆ 今日选股建议：无可选候选{ANSI['RESET']}")
            for r in suggestion["reasons"]:
                print(f"    · {r}")
