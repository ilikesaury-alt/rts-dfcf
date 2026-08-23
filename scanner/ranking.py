"""综合排序档位 / 🎯 次日大涨画像 / 涨幅带 / 超买 / 辨识度 等纯排序逻辑单源。

原位于 scanner.display（职责是终端渲染，不应拥有业务排序逻辑）；today_report /
prevday_perf / scripts 现统一从此处取数，消除层违规与对渲染层的耦合。
全部为纯函数：输入 entry(dict 或含 _candidate 的推荐行) + 可选 conn，输出档位/标记/
分类，不改评分、不落库、无 ANSI 依赖。校准依据见各函数 docstring。
"""

import math

from scanner.config import (
    BREAKOUT_ACCUM_MAX,
    BREAKOUT_PULLBACK_MAX,
    BREAKOUT_PULLBACK_MIN,
    BREAKOUT_T1_VOL_RATIO,
    CAT_DISPLAY_PRIORITY,
    NEXTDAY_ACCUM_MIN,
    NEXTDAY_CAT_PRIORITY,
    NEXTDAY_SPIKE_MID_MAX,
    NEXTDAY_SPIKE_MID_MIN,
    NEXTDAY_SPIKE_SWEET_LOW,
    NEXTDAY_SPIKE_SWEET_MIN,
    SECTOR_RESONANCE_WARN_MAX,
)
from scanner.utils import to_float


def _nextday_entry_percent(entry: dict) -> float:
    """推荐时刻盘中涨幅（用于次日大涨候选区筛形）。

    回退链：候选池当前扫描快照（最新）→ DB 落库 percent（推荐时刻口径）→
    live_quotes（仅落库缺失时兜底）。

    2026-08-21 审查修复（口径漂移）：掉榜行此前在 DB percent 之前就吃
    live_quotes——而 unified_scanner 会为今日曾推荐但已掉榜的票主动补拉实时行情，
    导致 🎯 甜蜜带 / _entry_band 涨幅带判定随盘中价格逐轮漂移（档位闪变），且偏离
    校准口径（nextday_attribution 落库的是推荐时刻 percent）。现掉榜行优先 DB
    落库值，live 仅在落库缺失时兜底。展示列的实时涨幅由 display 独立回退链负责，
      不受本函数影响。
    """
    c = entry.get("_candidate")
    if c and c.stock:
        return to_float(c.stock.percent, default=0.0)
    db_pct = entry.get("percent")
    if db_pct is not None:
        return to_float(db_pct, default=0.0)
    if entry.get("live_quote_available") and entry.get("live_percent") is not None:
        return to_float(entry["live_percent"], default=0.0)
    return 0.0


def _in_nextday_sweet_band(percent: float) -> bool:
    """推荐时刻涨幅是否落在次日大涨甜蜜带（<2% 或 4~8%）。

    数据（scanner.nextday_attribution，next_day≥7%）：<1% hit 11.7%、1-2% hit 13.2%、
    4-6% hit 11.8%、6-8% hit 11.8%；2-4% 死区（6.2%）、8-10% 陷阱（7.5%，平均 -1.42%）。
    """
    return (NEXTDAY_SPIKE_SWEET_MIN <= percent < NEXTDAY_SPIKE_SWEET_LOW
            or NEXTDAY_SPIKE_MID_MIN <= percent < NEXTDAY_SPIKE_MID_MAX)


def _replay_accum_from_rows(rows: list, rec_date: str) -> float | None:
    """从 daily_kline 原始行（单 symbol，未截断）回放推荐前 5 日累计（含推荐日）。

    rows: [(date, close, percent), ...]（已按该 symbol 过滤，未截断、未限 date）。
    返回含推荐日 5 日累计涨幅(%)，或 None（无数据/不足）。脏值（close 非有限正数/
    percent 非有限）统一清洗——与 _nextday_entry_accum 回退链同源，避免 TypeError
    穿透到 display 预计算崩溃。
    """
    valid: list[tuple[str, float, float]] = []
    for dt, close, pct in rows:
        try:
            cl = float(close)
            pc = float(pct or 0.0)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(cl) or cl <= 0:
            continue
        if not math.isfinite(pc):
            pc = 0.0
        valid.append((dt, cl, pc))
    within = [v for v in valid if v[0] <= rec_date]
    within.sort(key=lambda v: v[0], reverse=True)
    window = within[:6]
    if len(window) >= 6:
        base = window[5][1]
        return (window[0][1] - base) / base * 100.0
    if window:
        return sum(v[2] for v in window[:5])
    return None


def _nextday_entry_accum(entry: dict, conn=None) -> float | None:
    """推荐前 5 日累计涨幅（%），用于 🎯 判定；拿不到返回 None（不阻断）。

    口径：NEXTDAY_ACCUM_MIN=6.0 校准于「含推荐日」口径（5 日复利，含推荐日 bar）。
    - short_term 的 KlineSummary.accumulated_pct 本身含今日（策略语义）；
    - new_face/known_new_face/momentum/rebound 的 accumulated_pct 不含今日（历史口径，
      RPS/评分用），其「含今日」值由分析侧另存于 dimensions["accumulated_incl_today"]。

    回退链（与 _nextday_entry_percent 同构）：
      候选池 kline 的 accumulated_incl_today（扫描时刻，含今日 bar，最准）
      → 候选池 kline 的 accumulated_pct（short_term 含今日，可直接用）
      → daily_kline 回放（掉榜/重启行；date<=推荐日，含推荐日 bar，口径同校准）
      → DB 落库 accumulated_pct（仅回放无数据时兜底：short_term 含今日；nf/mom 为
        不含今日的历史口径，兜底值口径不符但优于 fail-open 误放行）。

    2026-08-17 修复（原口径错位）：此前候选行直接返回 c.kline.accumulated_pct，
    对 new_face/momentum/known_new_face 该值不含今日，而门槛校准于含推荐日口径——
    今日大涨票系统性被低估（漏标 🎯 / 丢档0置顶）、今日下跌票被高估（误标）。
    现候选行优先用 accumulated_incl_today 维度；掉榜行优先回放（含推荐日），
    DB 落库的历史口径值不再优先于回放。
    """
    c = entry.get("_candidate")
    if c and c.kline:
        incl = (c.kline.dimensions or {}).get("accumulated_incl_today")
        if incl is not None:
            return float(incl)
        if c.kline.accumulated_pct is not None:
            return float(c.kline.accumulated_pct)
    if conn is None:
        return None
    sym = entry.get("symbol")
    rec_date = entry.get("date")
    if not sym or not rec_date:
        return None
    try:
        rows = conn.execute(
            "SELECT date, close, percent FROM daily_kline WHERE symbol = ? AND date <= ? "
            "ORDER BY date DESC",
            (sym, rec_date[:10]),
        ).fetchall()
    except Exception:
        rows = []
    accum = _replay_accum_from_rows(rows, rec_date[:10])
    if accum is not None:
        return accum
    # 回放无数据（daily_kline 缺表/该票无历史）：兜底 DB 落库值
    db_acc = entry.get("accumulated_pct")
    return float(db_acc) if db_acc is not None else None


def build_accum_map(conn, entries: list[dict]) -> dict[str, float | None]:
    """预计算推荐前 5 日累计（含推荐日），单批次查询替代逐行 N+1 回放（P1-9）。

    返回 {symbol: accum_or_None}。候选行（有 _candidate 且维度含 accumulated_incl_today）
    直接取维度，不查 DB；掉榜/重启行批量 SELECT ... WHERE symbol IN (...) 一次取回，
    逐 symbol 经 _replay_accum_from_rows 回放。把 display / today_report 每轮渲染的
    N 次 daily_kline 查询降为 1 次（N = 掉榜/重启行数）。
    """
    result: dict[str, float | None] = {}
    dropped: list[dict] = []
    for e in entries:
        c = e.get("_candidate")
        if c and c.kline:
            incl = (c.kline.dimensions or {}).get("accumulated_incl_today")
            if incl is not None:
                result[e["symbol"]] = float(incl)
                continue
            if c.kline.accumulated_pct is not None:
                result[e["symbol"]] = float(c.kline.accumulated_pct)
                continue
        if e.get("symbol") and e.get("date"):
            dropped.append(e)
    if not dropped:
        return result
    syms = sorted({e["symbol"] for e in dropped})
    try:
        rows = conn.execute(
            "SELECT symbol, date, close, percent FROM daily_kline "
            "WHERE symbol IN ({})".format(",".join("?" * len(syms))),
            tuple(syms),
        ).fetchall()
    except Exception:
        rows = []
    by_sym: dict[str, list[tuple[str, float, float]]] = {}
    for sym, dt, close, pct in rows:
        by_sym.setdefault(sym, []).append((dt, close, pct))
    for e in dropped:
        sym = e["symbol"]
        rec_date = e.get("date") or ""
        accum = _replay_accum_from_rows(by_sym.get(sym, []), rec_date[:10])
        if accum is None:
            # 回放无数据：兜底 DB 落库 accumulated_pct（short_term 含今日；nf/mom 为
            # 不含今日历史口径，兜底值口径不符但优于 fail-open 误放行），与
            # _nextday_entry_accum 回退链同源。
            db_acc = e.get("accumulated_pct")
            accum = float(db_acc) if db_acc is not None else None
        result[sym] = accum
    return result


def _entry_dims(entry: dict) -> dict:
    """统一维度访问：候选行读 kline.dimensions（最新扫描），掉榜/重启行读 DB score_breakdown。

    2026-08-17 新增（配合 get_today_recommendations 返回 score_breakdown）：
    拿不到（entry 无 _candidate），现在统一经此函数读取，候选行优先（最新数据）。
    """
    c = entry.get("_candidate")
    if c and c.kline and c.kline.dimensions:
        return c.kline.dimensions
    sb = entry.get("score_breakdown")
    return sb if isinstance(sb, dict) else {}


def _entry_weak_to_strong(entry: dict) -> bool:
    """short_term 弱转强成立：st_weak_to_strong / v_st_weak 任一 >0。

    组合信号分析（2026-08-17，去重 1224 样本）：弱转强∩非超买 hit 15.8%（全类
    基准 10.0%）——short_term 次日大涨的最强单信号。
    """
    d = _entry_dims(entry)
    return bool(d.get("st_weak_to_strong") or d.get("v_st_weak"))



def _entry_overbought(entry: dict) -> bool:
    """超买死亡信号：候选 dims / 掉榜 score_breakdown 统一判定（_entry_dims）。

    数据（nextday_attribution）：short_term/动量超买 hit 5-8%（非超买 10.5%）。
    """
    d = _entry_dims(entry)
    return bool(d.get("st_overbought_flag") or d.get("mo_overbought_flag")
                or d.get("v_st_overbought") or d.get("v_mo_overbought"))


def _entry_band(entry: dict) -> str:
    """推荐时刻涨幅带分类（与 🎯 甜蜜带同源，nextday_attribution 口径）。

    sweet(0-2%/4-8% 甜蜜带) / down(<0) / dead(2-4% 死区，next_day hit 7.0%) /
    trap(8-10%：全量 9.0% 不差但被 short_term 拉高，momentum/new_face 分类别 hit 0%，
    由 _entry_tier 仅对非 short_term 生效)。
    """
    p = _nextday_entry_percent(entry)
    if p < 0:
        return "down"
    if 0.0 <= p < NEXTDAY_SPIKE_SWEET_LOW or NEXTDAY_SPIKE_MID_MIN <= p < NEXTDAY_SPIKE_MID_MAX:
        return "sweet"
    if p < NEXTDAY_SPIKE_MID_MIN:
        return "dead"
    return "trap"


def _entry_fund_flow_pct(entry: dict) -> float | None:
    """主力净占比（%），候选行读 dims、掉榜行读 score_breakdown；无数据返回 None。"""
    v = _entry_dims(entry).get("fund_flow_main_pct")
    return to_float(v, default=None) if v is not None else None


def _entry_sector_resonance(entry: dict) -> bool:
    """小板块共振：v_st_sector / v_pb_sector / v_nf_sector >0 且板块规模 count<SECTOR_RESONANCE_WARN_MAX。

    回测细分（2026-08-17）：板块共振整体 next_day hit 5.6% 全场最差（无共振 13.7%），
    但按规模分档差异大——cnt<5 hit 5.3%、cnt 5-14 hit 6.3%、cnt>=15 hit 12.9%（接近
    无共振 11.9%，大板块有持续资金）——只对小板块（cnt<15，局部抱团次日兑现）档位劣后。
    count 缺失按 0（小板块，保守）。2026-08-17 行尾 ⚠板块普涨 文本下线后，本函数
    仅服务 _entry_tier 档位劣后（排序），不渲染任何文本。
    """
    d = _entry_dims(entry)
    if not (d.get("v_st_sector") or d.get("v_pb_sector") or d.get("v_nf_sector")):
        return False
    cnt = (d.get("v_st_sector_count") or d.get("v_pb_sector_count")
           or d.get("v_nf_sector_count") or 0)
    return cnt < SECTOR_RESONANCE_WARN_MAX


def _entry_tier(entry: dict, conn=None, accum: float | None = None,
                marked: bool | None = None, accum_map: dict | None = None) -> int:
    """综合排序档位（2026-08-17 二值 → 4 级；2026-08-18 统一口径为「次日大涨」）。

    档0 = 🎯 次日大涨画像（数据最强，见 _is_nextday_marked，short_term 弱转强分型）
    档1 = 强信号：rebound（next_day 口径 hit 28.6%/+2.78%，全场最强类别）
    档2 = 普通：无警示（参考）
    档3 = 警示劣后：累计≥50% 过热 / 超买（hit 6.8%）/ 小板块共振 cnt<15（hit 5.6%）/
          2-4% 死区（hit 7.0%）/ momentum、new_face 的 8-10% 陷阱（hit 0%）/ 资金流出≤-8%。
          short_term 豁免涨幅带（规律在弱转强）；comeback 统一档2——除累计≥50%
          过热外不看任何警示因子（超买/资金流/板块共振/涨幅带，2026-08-18 设计）。

    2026-08-18 口径统一：全部档位判定因子均校准于 next_day（次日大涨≥7% hit 口径，
    scanner.nextday_attribution 1184 去重样本）。comeback 由档1 移除——其 6 维回踩买点
    信号是 cum_3d 语义（回踩企稳等 3 日修复），next_day 口径下 hit 仅 3.3% 全场最差，
    不再置顶，统一回档2（独立区补充参考）。

    纯排序层：不改评分不落库，档位只重排展示顺序（跨类别全局生效）。
    """
    if accum_map is not None:
        accum = accum_map.get(entry.get("symbol"))
    elif accum is None:
        accum = _nextday_entry_accum(entry, conn)
    # 过热妖股优先于一切：累计≥50% 即使命中 🎯 画像也劣后（精选区校准，hit 最低区）
    if accum is not None and accum >= 50:
        return 3
    if marked is None:
        marked = _is_nextday_marked(entry, conn, accum=accum, accum_map=accum_map)
    if marked:
        return 0
    cat = entry["category"]
    if cat == "rebound":
        return 1
    if cat == "comeback":
        return 2
    if _entry_overbought(entry):
        return 3
    ff = _entry_fund_flow_pct(entry)
    if ff is not None and ff <= -8:
        return 3
    # 小板块共振（cnt<15）档3 劣后：板块普涨日冲进去即接盘位（next_day hit 5.6% vs 无共振 13.7%）。
    # 仅非 🎯 票生效（🎯∩板块普涨 hit 12.2% 仍有效，太辰光案例）；不渲染文本，只排序。
    if _entry_sector_resonance(entry):
        return 3
    # 涨幅带劣后（next_day 口径，全量 1184 样本）：2-4% 死区 hit 7.0%（基准 9.7%）；
    # 8-10% 全量 9.0% 不差但被 short_term 拉高——momentum（n=18）与 new_face（n=26）
    # 在 8-10% 的 next_day hit 均为 0%，kNF 仅 2 条样本；short_term 豁免（8-10% 是其
    # 最差与最好并存的双峰带，weak_to_strong 子集可用）；rebound 已提前档1 返回。
    if cat != "short_term":
        if _entry_band(entry) in ("trap", "dead"):
            return 3
    return 2


def _is_nextday_marked(entry: dict, conn=None, accum: float | None = None,
                      accum_map: dict | None = None) -> bool:
    """次日大涨画像标记（🎯）：推荐时刻涨幅在甜蜜带 + 非超买死亡信号 + 5日累计门槛。

    2026-08-11：原「◆ 次日大涨候选」独立区与综合排序主表重合度 65%（实测当日
    主表 17 只中 11 只甜蜜带、两表排序几乎一致、辨识度因子空转），改为主表行尾
    标记，消除重复输出。筛形条件与独立区完全一致（nextday_attribution 口径）：
      1. 推荐时刻涨幅在甜蜜带（<2% 低吸潜伏 或 4~8% 中段启动）；
      2. 排除超买（short_term/动量死亡信号：hit 5% vs 非超买 10.5%）。
    2026-08-14 新增 3. 5 日累计 ≥ NEXTDAY_ACCUM_MIN（用户怕追高只选涨幅小/累计低的票，
    实测 0~3 平档 hit 仅 5.4% 全场最差、10~15 档 21.2% 最好——「累计低=安全」是反指；
    甜蜜带+累计≥6 使 hit 16.5%→20.0%）。rebound 豁免（超跌反弹，负累计天然，hit 33.3%）、
    short_term 豁免（其规律在超买/弱转强，不在此列）。累计缺失 fail-open 不阻断（见
    _nextday_entry_accum）。累计口径 = 校准口径（含推荐日 bar，_nextday_entry_accum
    优先取候选 kline 的 accumulated_incl_today 维度；2026-08-17 修复口径错位）。
    视觉标记 + 参与综合排序档位（_sort_tier 档0置顶），不改 score / 不落库。
    """
    if entry["category"] not in NEXTDAY_CAT_PRIORITY:
        return False
    cat = entry["category"]
    if cat == "short_term":
        # 2026-08-17 🎯 分型（组合信号分析，去重 1224 样本）：short_term 次日大涨规律
        # 在弱转强（弱转强∩非超买 hit 15.8%），甜蜜带对 short_term 反而负效（5.7% vs
        # 全类 8.5%）——原「甜蜜带+非超买」判定把 122 只甜蜜带 short_term 里仅 1/7 命中
        # 的侥幸票（太辰光式）顶进档0。改判定：short_term 要求弱转强 + 非超买，不再看
        # 甜蜜带。掉榜行经 score_breakdown 判定（_entry_weak_to_strong），缺数据不标。
        if not _entry_weak_to_strong(entry):
            return False
    elif not _in_nextday_sweet_band(_nextday_entry_percent(entry)):
        return False
    cat = entry["category"]
    if cat not in ("rebound", "short_term"):
        if accum_map is not None:
            accum = accum_map.get(entry.get("symbol"))
        elif accum is None:
            accum = _nextday_entry_accum(entry, conn)
        if accum is not None and accum < NEXTDAY_ACCUM_MIN:
            return False  # 有累计数据且不达门槛 → 不标；缺数据 fail-open 放行
    # 超买 = 次日大涨死亡信号：候选行读 dims，掉榜/重启行读 score_breakdown（统一 _entry_dims）。
    # 2026-08-17 修复：此前只查候选行，掉榜行（无 _candidate）直接放行——兆日科技
    # 案例（超买+累计74.7%妖股被误标 🎯）。掉榜行 score_breakdown 含 v_st_overbought 等字段。
    d = _entry_dims(entry)
    if (d.get("st_overbought_flag") or d.get("mo_overbought_flag")
            or d.get("v_st_overbought") or d.get("v_mo_overbought")):
        return False
    return True


# ── 蓄势突破观察画像（2026-08-21，纯展示层 ⚡ 标记）──
# 来源：历史涨停复盘（「推荐后当日封板」20 只 vs 全部推荐对照，见 config BREAKOUT_* 注释）。
# 定位：观察标记——不改排序、不改评分、不落库；样本达标后经 nextday_attribution
# 复盘再决定是否升级。判定全部基于 T-1 及更早的结构（推荐日盘中 bar 不完整不参与）。

def build_breakout_kline_map(conn, entries: list[dict]) -> dict[str, list[tuple[str, float, float, float]]]:
    """批量取蓄势突破判定所需 K 线（单查询防 N+1），返回 {symbol: [(date, high, close, volume), ...]}。

    仅保留 date < 推荐日的行（结构基准 = T-1 及更早；推荐日盘中 bar 未定稿不参与，
    且收盘后回放口径一致）。脏值清洗与 _replay_accum_from_rows 同族：close/high 非有限
    正数整行剔除，volume 非有限置 0。
    """
    syms = sorted({e["symbol"] for e in entries if e.get("symbol") and e.get("date")})
    if not syms:
        return {}
    try:
        rows = conn.execute(
            "SELECT symbol, date, high, close, volume FROM daily_kline "
            "WHERE symbol IN ({})".format(",".join("?" * len(syms))),
            tuple(syms),
        ).fetchall()
    except Exception:
        return {}
    by_sym: dict[str, list[tuple[str, float, float, float]]] = {}
    for sym, dt, high, close, vol in rows:
        try:
            h = float(high)
            cl = float(close)
            v = float(vol) if vol is not None else 0.0
        except (TypeError, ValueError):
            continue
        if not math.isfinite(h) or h <= 0 or not math.isfinite(cl) or cl <= 0:
            continue
        if not math.isfinite(v) or v < 0:
            v = 0.0
        by_sym.setdefault(sym, []).append((dt, h, cl, v))
    result: dict[str, list[tuple[str, float, float, float]]] = {}
    for e in entries:
        sym = e.get("symbol")
        rec_date = (e.get("date") or "")[:10]
        lst = sorted(by_sym.get(sym, []))
        result[sym] = [r for r in lst if r[0] < rec_date]
    return result


def _breakout_structure_ok(entry: dict, conn=None, accum: float | None = None,
                           accum_map: dict | None = None,
                           klines: list[tuple[str, float, float, float]] | None = None) -> bool:
    """蓄势结构共同条件（⚡ 与 ⚡R 变体共用，2026-08-22 自 _is_breakout_setup 抽出单源）：

      2. 前5日累计（含推荐日，复用 🎯 的 accum 口径链）≤ BREAKOUT_ACCUM_MAX——
         涨停前是横盘蓄势而非连涨加速；缺失不标（观察标记 fail-closed）；
      3. T-1 缩量：T-1 量 / 前5日均量 ≤ BREAKOUT_T1_VOL_RATIO；
      4. T-1 收盘距20日高点回撤 ∈ [BREAKOUT_PULLBACK_MIN, BREAKOUT_PULLBACK_MAX]；
      5. MA5>MA10>MA20（截至 T-1 收盘）。

    阈值见 config BREAKOUT_*（校准于涨停复盘 A 组中位数附近）。类别门由各画像自判。
    klines：build_breakout_kline_map 的单票切片（已滤 date<推荐日）；None 时退化为
    单票查询（conn 缺失则不标）。数据不足（<21 根）任一条件拿不到 → 不标。
    """
    if accum_map is not None:
        accum = accum_map.get(entry.get("symbol"))
    elif accum is None:
        accum = _nextday_entry_accum(entry, conn)
    if accum is None or accum > BREAKOUT_ACCUM_MAX:
        return False
    if klines is None:
        if conn is None or not entry.get("symbol") or not entry.get("date"):
            return False
        try:
            rows = conn.execute(
                "SELECT date, high, close, volume FROM daily_kline "
                "WHERE symbol = ? AND date < ? ORDER BY date",
                (entry["symbol"], entry["date"][:10]),
            ).fetchall()
        except Exception:
            return False
        cleaned: list[tuple[str, float, float, float]] = []
        for dt, high, close, vol in rows:
            try:
                h = float(high)
                cl = float(close)
                v = float(vol) if vol is not None else 0.0
            except (TypeError, ValueError):
                continue
            if not math.isfinite(h) or h <= 0 or not math.isfinite(cl) or cl <= 0:
                continue
            cleaned.append((dt, h, cl, v))
        klines = cleaned
    if len(klines) < 21:
        return False
    t1_close, t1_vol = klines[-1][2], klines[-1][3]
    prev_vols = [b[3] for b in klines[-6:-1]]
    mean_vol = sum(prev_vols) / len(prev_vols) if prev_vols else 0.0
    if mean_vol <= 0 or t1_vol <= 0:
        return False
    if t1_vol / mean_vol > BREAKOUT_T1_VOL_RATIO:
        return False
    h20 = max(b[1] for b in klines[-20:])
    pullback = (t1_close / h20 - 1.0) * 100.0
    if not (BREAKOUT_PULLBACK_MIN <= pullback <= BREAKOUT_PULLBACK_MAX):
        return False
    closes = [b[2] for b in klines]
    ma5 = sum(closes[-5:]) / 5.0
    ma10 = sum(closes[-10:]) / 10.0
    ma20 = sum(closes[-20:]) / 20.0
    return ma5 > ma10 > ma20


def _is_breakout_setup(entry: dict, conn=None, accum: float | None = None,
                       accum_map: dict | None = None,
                       klines: list[tuple[str, float, float, float]] | None = None) -> bool:
    """蓄势突破画像（⚡ 观察标记）：新面孔/首推 + 横盘缩量回调位 + MA 多头。

    条件：1. 类别门 new_face/known_new_face，或首推（dims.first_today_bonus>0）——
    涨停组 65% 为新面孔、首推占 61%；2~5 共用 _breakout_structure_ok。
    """
    d = _entry_dims(entry)
    if entry["category"] not in ("new_face", "known_new_face") \
            and not bool(d.get("first_today_bonus")):
        return False
    return _breakout_structure_ok(entry, conn, accum=accum,
                                  accum_map=accum_map, klines=klines)


def _is_relist_breakout_setup(entry: dict, conn=None, accum: float | None = None,
                              accum_map: dict | None = None,
                              klines: list[tuple[str, float, float, float]] | None = None) -> bool:
    """重上榜蓄势突破观察画像（⚡R 观察标记）：非首推 short_term + 横盘缩量回调位 + MA 多头。

    来源：2026-08-21 肯特股份案例（长期掉榜后重新上榜的 short_term，推荐日 +20% 涨停）
    ——命中原 ⚡ 画像条件 2~5 全部，仅被类别门（只认 new_face/kNF/首推）排除，即 AGENTS.md
    记录的已知局限正主。本变体把类别门换成「short_term 且**非**首推」：首推票已由 ⚡
    覆盖，两个标记按构造不相交、不会重复打点。结构条件 2~5 复用同一
    _breakout_structure_ok（同阈值同 fail-closed），保证两画像口径不漂移。
    纯展示层观察标记：不改排序/评分/落库；按开放假设清单 SOP 先积累样本，达标后经
    nextday_attribution 复盘再评估是否升级为排序因子或放宽更多类别（momentum 等）。
    """
    d = _entry_dims(entry)
    if entry.get("category") != "short_term" or d.get("first_today_bonus"):
        return False
    return _breakout_structure_ok(entry, conn, accum=accum,
                                  accum_map=accum_map, klines=klines)


# ── 排序组合层（2026-08-20 收敛单源）──
# display.py / today_report.py 此前各写一份排序键装配（tier_map 预计算 + 类别分流 +
# 分数键），且已实际分化：today_report 漏掉 known_new_face 分数反指升序特判（display
# 有，2026-08-10 加）。现把「同一行该排第几」的唯一逻辑收归此处，两处消费同一结果。

def score_sort_key(entry: dict) -> float:
    """分数键：known_new_face 分数反指（低分档 hit 更高）→ 升序在前；其余类别降序。

    回测分桶（2026-08-10）：kNF 低分档[18,37) cum_3d +5.58/64%胜率 vs 高分档[77,98)
    -3.76/33%，单调反指且低分档样本充足。统一入口避免展示端与报告端排序分化。
    """
    if entry["category"] == "known_new_face":
        return entry["score"]
    return -entry["score"]


def sort_main_entries(main_recs: list[dict], tier_map: dict[str, int]) -> list[dict]:
    """综合排序主表排序键 = (档位, 类别展示优先级, 分数键)。

    tier_map：{symbol: 档位(0..3)}，由调用方预计算（display 用 _entry_tier 统一预计算，
    today_report 用逐行 _entry_tier 结果）。类别优先级取 CAT_DISPLAY_PRIORITY（值越小越靠前，
    未知类别落 99）。档位只影响排序，不改评分/不落库。
    """
    return sorted(
        main_recs,
        key=lambda x: (
            tier_map.get(x["symbol"], 2),
            CAT_DISPLAY_PRIORITY.get(x["category"], 99),
            score_sort_key(x),
        ),
    )
