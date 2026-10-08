"""scanner/push_gate.py — 飞书推送严格过滤门（2026-09-28）。

## 定位

**纯展示层、纯函数、唯一出口 = 飞书卡片**。终端四区块**不套用**本门
（终端是全量信息面，飞书是精选推送面）—— 这是用户 2026-09-28 明确引入的
**第二个出口**，与 `feishu.build_feishu_card` 此前「与终端一一对应」的不变式
**故意分叉**。分叉的代价是「终端有、飞书无」，故本门**强制**在卡片上打出
剔除数（`gate_note`），不允许静默少票。

## 过滤方程式

    通过(row) = ¬否决(row) ∧ 分级(row) ≥ 兜底档

    分级(row) = CATEGORY_HIT_RATE.get(category, CATEGORY_HIT_RATE_DEFAULT)
        tier A : 分级 ≥ PUSH_TIER_A_MIN (0.10)          → 必推
        tier B : PUSH_TIER_B_MIN (0.078) ≤ 分级 < A      → 推
        tier C : 分级 < B                               → 仅走兜底通道

    兜底通道(C 档) = PUSH_FALLBACK_ENABLED ∧ 有榜内热度证据
        v1 池选   : rank ≤ PUSH_FALLBACK_MAX_RANK
        沪深飙升A : streak ≥ PUSH_FALLBACK_MIN_STREAK ∨ rank_change ≥ ...MIN_RANK_RISE
        v1 回捞  : vol_ratio ≥ PUSH_FALLBACK_MIN_VOL_RATIO   （该区无榜内排名）

    否决(row) = 资金流出(≤ FUND_OUTFLOW_NET_PCT)          # 风险，非预测
             ∨ 过热(5日累计 ≥ OVERHEAT_ACCUM_MAX)          # 复用现成常量
             ∨ 风险硬信号(risk_flags ∩ RISK_FLAGS_DISPLAY_HARD)

    卡片级 = 通过数 ≥ PUSH_MIN_ROWS
           ∧ 通过票集 ≠ 上次通过票集                       # 去重键改用「通过集」
           ∧ 距上次 ≥ PUSH_MIN_INTERVAL                    # 900s

## 三条纪律（改动本模块前必读）

1. **主键只能是类别先验，不能是 score。** 实测 n=3543：score 与次日≥7% hit
   **无单调关系**（score=118→4.2%，score=0→0.0%，score=54→21.1%，纯噪声）；
   类别先验与实测逐档吻合（rebound 17.9/17.9、kNF 12.5/12.7、pool_pick 3.7/2.1）。
   拿 score 当门 = 拿噪声冒充纪律。依据与数字见 `scanner/config_push.py` docstring。

2. **否决项全是「风险」不是「预测」。** 三个否决阈值**全部复用现成单源**
   （`FUND_OUTFLOW_NET_PCT` / `OVERHEAT_ACCUM_MAX` / `RISK_FLAGS_DISPLAY_HARD`），
   本模块**不新造任何风险阈值** —— 新造就会与上游漂移。

3. **fail-open。** 取不到的字段（rank=None、ff_pct=None、risk_flags 缺失）一律
   **不否决**，只影响分级与兜底判定；程序错误照旧冒泡（不裸 except Exception）。

## 与既有不变式的关系

- `_view_symbols`（去重键）改为**通过集**而非 `main_rows[:FEISHU_TOP_N]`：
  旧键取前 10 行，这 10 行每轮都在换 ⇒ `has_change` 恒 True ⇒
  `FEISHU_MIN_INTERVAL` 形同虚设，**这才是推送量的真正根因**（门只是第二道保险）。
- `view_has_content` 判空改为「通过集非空」：门全剔时返回 `empty`，不推空卡。
- 终端 `render_terminal` 与 `ScanView` 组装链路**零改动**。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from scanner.config import (
    CATEGORY_HIT_RATE,
    CATEGORY_HIT_RATE_DEFAULT,
    FUND_OUTFLOW_NET_PCT,
    OVERHEAT_ACCUM_MAX,
    PUSH_FALLBACK_ENABLED,
    PUSH_FALLBACK_MAX_RANK,
    PUSH_FALLBACK_MIN_RANK_RISE,
    PUSH_FALLBACK_MIN_STREAK,
    PUSH_FALLBACK_MIN_VOL_RATIO,
    PUSH_GATE_ENABLED,
    PUSH_MAX_ROWS,
    PUSH_TIER_A_MIN,
    PUSH_TIER_B_MIN,
    RISK_FLAGS_DISPLAY_HARD,
)

# is_fund_outflow（2026-10-08 修复 L2）：资金流出否决的单源判定 —— 与 assemble 的
# 展示层硬门同一条回退链（行内 dims/score_breakdown → flow_pct_map 快照）。此前本模块
# 只查 flow_pct_map，行内 dims 判出流出但快照缺行时会漏否决（终端剔了、推送放行的分叉）。
from scanner.ranking import is_fund_outflow
from scanner.utils import to_float

TIER_A = "A"
TIER_B = "B"
TIER_C = "C"


def classify(category: str | None) -> tuple[str, float]:
    """类别 → (档位, 先验 hit 率)。

    ⚠ **表外类别一律判 C 档**（需兜底证据），**不**用 `CATEGORY_HIT_RATE_DEFAULT`
    （0.078）给它们 B 档放行。原因（实测踩到）：`CATEGORY_HIT_RATE` 里
    `comeback` 被**删掉**了（config_scoring 注明「0.028，全场最差」），而
    `pullback`/`old_face`/`early_momentum` 同样不在表内 —— 若按「未知 → 默认值」
    放行，2026-09-14~16 的回捞区 64 只 `comeback`（实测 hit **2.8%**，n=506，
    全场最差）会**整批白送 B 档**。默认值的语义是「全体基线」，不是「这张票的先验」；
    拿它当通行证 = 把已知的最差类别洗成合格。fail-safe 方向：表外 → C 档 → 要热度证据。

    纯查表，不做任何再计算 —— `CATEGORY_HIT_RATE` 是先验唯一手抄源
    （`config_scoring`，守护见 `tests/test_category_priors.py`）。
    """
    if not category or category not in CATEGORY_HIT_RATE:
        return TIER_C, CATEGORY_HIT_RATE_DEFAULT
    hit = CATEGORY_HIT_RATE[category]
    if hit >= PUSH_TIER_A_MIN:
        return TIER_A, hit
    if hit >= PUSH_TIER_B_MIN:
        return TIER_B, hit
    return TIER_C, hit


def _veto_common(accum_5d, ff_pct, risk_flags) -> str | None:
    """三个否决项（全部复用上游单源阈值）。返回否决理由 | None（放行）。"""
    ff = to_float(ff_pct, default=None)
    if ff is not None and ff <= FUND_OUTFLOW_NET_PCT:
        return f"资金流出{ff:+.1f}%"
    acc = to_float(accum_5d, default=None)
    if acc is not None and acc >= OVERHEAT_ACCUM_MAX:
        return f"5日累计过热{acc:+.0f}%"
    hard = [f for f in (risk_flags or []) if f in RISK_FLAGS_DISPLAY_HARD]
    if hard:
        return "/".join(hard)
    return None


@dataclass
class GateStats:
    """一次过滤的账（卡片上要如实告知用户剔了多少、为什么）。"""

    total: int = 0
    passed: int = 0
    tier_a: int = 0
    tier_b: int = 0
    tier_c_fallback: int = 0
    vetoed: int = 0
    dropped_no_fallback: int = 0

    def add(self, other: GateStats) -> None:
        self.total += other.total
        self.passed += other.passed
        self.tier_a += other.tier_a
        self.tier_b += other.tier_b
        self.tier_c_fallback += other.tier_c_fallback
        self.vetoed += other.vetoed
        self.dropped_no_fallback += other.dropped_no_fallback

    @property
    def filtered(self) -> int:
        return self.total - self.passed


@dataclass
class GateResult:
    """过滤结果：`kept` 是各区通过的行（保持原顺序），`stats` 是账。"""

    main: list = field(default_factory=list)
    hist: list = field(default_factory=list)
    hot: list = field(default_factory=list)
    offboard: list = field(default_factory=list)
    stats: GateStats = field(default_factory=GateStats)

    @property
    def passed_total(self) -> int:
        return len(self.main) + len(self.hist) + len(self.hot) + len(self.offboard)

    def symbols(self) -> set[str]:
        """去重键：**通过集**（取代旧的 main_rows[:FEISHU_TOP_N]）。"""
        out: set[str] = set()
        for group in (self.main, self.hist, self.hot, self.offboard):
            for row in group:
                entry = getattr(row, "entry", None)
                sym = entry.get("symbol") if isinstance(entry, dict) else getattr(row, "symbol", None)
                if sym:
                    out.add(sym)
        return out

    def note(self) -> str:
        """卡片上的一行告知：剔了多少、为什么。**门开着就必须出这一行**（静默少票=分叉）。"""
        s = self.stats
        if s.filtered <= 0:
            return ""
        bits = [f"严格过滤 {s.passed}/{s.total} 只通过"]
        detail = []
        if s.tier_c_fallback:
            detail.append(f"兜底档 {s.tier_c_fallback}")
        if s.dropped_no_fallback:
            detail.append(f"类别先验不足且无榜内热度 {s.dropped_no_fallback}")
        if s.vetoed:
            detail.append(f"否决(过热/流出/风险) {s.vetoed}")
        if detail:
            bits.append("（" + "·".join(detail) + "）")
        return "".join(bits)


def _gate_one(stats: GateStats, category, *, accum_5d, ff_pct, risk_flags, fallback_ok) -> bool:
    """单行判定（纯函数，便于单测穷举）。`stats` 就地累加账。"""
    stats.total += 1
    veto = _veto_common(accum_5d, ff_pct, risk_flags)
    if veto:
        stats.vetoed += 1
        return False
    tier, _hit = classify(category)
    if tier == TIER_A:
        stats.tier_a += 1
        stats.passed += 1
        return True
    if tier == TIER_B:
        stats.tier_b += 1
        stats.passed += 1
        return True
    # C 档：仅兜底通道
    if PUSH_FALLBACK_ENABLED and fallback_ok:
        stats.tier_c_fallback += 1
        stats.passed += 1
        return True
    stats.dropped_no_fallback += 1
    return False


def _candidate_of(entry):
    """取推荐行上的 `_candidate`（**dict 键**，不是属性）。

    feishu._row_risk / _row_zt 一直用 `entry.get("_candidate")` —— 行对象是
    dict-like（RecommendationRow），`_candidate` 是渲染期注入的键。写成
    `getattr(entry, "_candidate", None)` 会**静默恒为 None** ⇒ 风险硬信号否决项
    形同虚设（tests/test_push_gate.py::test_risk_hard_flags_veto 抓到）。
    同时兼容属性式行对象，两种都不误判。
    """
    if isinstance(entry, dict):
        return entry.get("_candidate")
    return getattr(entry, "_candidate", None)


def gate_main_rows(view) -> tuple[list, GateStats]:
    """v1 池选：兜底条件 = 榜内排名 ≤ PUSH_FALLBACK_MAX_RANK（rank 缺失 → 不兜底）。

    资金流出否决（2026-10-08 修复 L2）：先走 `ranking.is_fund_outflow` 单源（与
    assemble 展示层硬门同一条回退链），命中即计否决并跳过 `_gate_one` —— `_gate_one`
    内部的 ff_pct 快照判定保留作第二道防线，但不会重复计数（能走到它说明单源未判流出）。

    过热否决输入（2026-10-08 修复 L3）：`accum_hist`（历史 5 日累计，排除今日）优先，
    缺失才回退 `row.accum` —— `accum` 对 short_term 行含今日（策略语义），直接与
    `OVERHEAT_ACCUM_MAX`（历史口径阈值）比较会系统性偏严一档。
    """
    stats = GateStats()
    kept = []
    flow_map = getattr(view, "flow_pct_map", None) or {}
    for row in getattr(view, "main_rows", None) or []:
        entry = row.entry
        # 单源否决（L2）：与 assemble 同一判定，含行内 dims 回退链。
        if is_fund_outflow(entry, flow_map):
            stats.total += 1
            stats.vetoed += 1
            continue
        cand = _candidate_of(entry)
        rank = to_float(row.rank, default=None)
        fb = rank is not None and rank <= PUSH_FALLBACK_MAX_RANK
        cat = entry.get("category") if isinstance(entry, dict) else getattr(entry, "category", None)
        accum_veto = row.accum_hist if getattr(row, "accum_hist", None) is not None else row.accum
        if _gate_one(
            stats,
            cat,
            accum_5d=accum_veto,
            ff_pct=flow_map.get(entry.get("symbol")),
            risk_flags=getattr(cand, "risk_flags", None),
            fallback_ok=fb,
        ):
            kept.append(row)
    return kept, stats


def gate_hot_rows(rows) -> tuple[list, GateStats]:
    """沪深飙升 A 段：兜底条件 = 连击 ≥3 轮 ∨ 排名上升 ≥30 名。

    该区无类别字段（口径是热度跃升，与主线 next_day 无关）⇒ **全部按 C 档处理**，
    只靠热度证据放行。这是有意的：A/B 档的类别先验对 hot_watch 不适用。
    """
    stats = GateStats()
    kept = []
    for c in rows or []:
        streak = to_float(getattr(c, "streak", None), default=0.0) or 0.0
        rise = to_float(getattr(c, "rank_change", None), default=0.0) or 0.0
        fb = streak >= PUSH_FALLBACK_MIN_STREAK or rise >= PUSH_FALLBACK_MIN_RANK_RISE
        if _gate_one(
            stats,
            None,  # hot 区无 category → 走 C 档
            accum_5d=getattr(c, "accum_5d", None),
            ff_pct=getattr(c, "ff_pct", None),
            risk_flags=None,
            fallback_ok=fb,
        ):
            kept.append(c)
    return kept, stats


def gate_offboard_rows(rows) -> tuple[list, GateStats]:
    """榜外异动 B 段：**无榜内排名与连击**（结构上缺失）⇒ 兜底只能用量比。

    该段标注为「观察段·未回测」（`view.model.offboard_subtitle`），证据强度本就
    弱于 A 段，故门最紧：无量比支撑一律不推。
    """
    stats = GateStats()
    kept = []
    for b in rows or []:
        vr = to_float(getattr(b, "volume_ratio", None), default=0.0) or 0.0
        if _gate_one(
            stats,
            None,
            accum_5d=getattr(b, "accum_5d", None),
            ff_pct=getattr(b, "ff_pct", None),
            risk_flags=None,
            fallback_ok=vr >= PUSH_FALLBACK_MIN_VOL_RATIO,
        ):
            kept.append(b)
    return kept, stats


def gate_hist_rows(rows) -> tuple[list, GateStats]:
    """v1 回捞：兜底条件 = 量比 ≥ PUSH_FALLBACK_MIN_VOL_RATIO（该区无榜内排名）。

    分级用**当时的 v1 桶**（`rec_category`）—— 那才是「它被系统认为是什么类型」
    的记录；用今天的 category 会得到 None → 恒 C 档。
    """
    stats = GateStats()
    kept = []
    for c in rows or []:
        vr = to_float(getattr(c, "vol_ratio", None), default=0.0) or 0.0
        if _gate_one(
            stats,
            getattr(c, "rec_category", None),
            accum_5d=getattr(c, "accum_5d", None),
            ff_pct=getattr(c, "ff_pct", None),
            risk_flags=None,
            fallback_ok=vr >= PUSH_FALLBACK_MIN_VOL_RATIO,
        ):
            kept.append(c)
    return kept, stats


def apply_push_gate(view) -> GateResult:
    """对一份 ScanView 跑完整过滤门 → GateResult（各区通过行 + 账）。

    `PUSH_GATE_ENABLED=False` 时**原样放行**（保持旧行为，可回滚）。
    """
    res = GateResult()
    if not PUSH_GATE_ENABLED:
        res.main = list(getattr(view, "main_rows", None) or [])
        res.hist = list(getattr(view, "hist_rows", None) or [])
        res.hot = list(getattr(view, "hot_rows", None) or [])
        res.offboard = list(getattr(view, "offboard_rows", None) or [])
        res.stats = GateStats(total=res.passed_total, passed=res.passed_total, tier_a=res.passed_total)
        return res

    res.main, s = gate_main_rows(view)
    res.stats.add(s)
    res.hist, s = gate_hist_rows(getattr(view, "hist_rows", None))
    res.stats.add(s)
    res.hot, s = gate_hot_rows(getattr(view, "hot_rows", None))
    res.stats.add(s)
    res.offboard, s = gate_offboard_rows(getattr(view, "offboard_rows", None))
    res.stats.add(s)

    # 单卡硬上限：按 A→B→兜底 的优先级截断，不是简单取前 N（否则高先验票会被
    # 排在后面的兜底票挤掉 —— 那等于把唯一的证据轴扔了）。
    for name in ("main", "hist", "hot", "offboard"):
        rows = getattr(res, name)
        if len(rows) > PUSH_MAX_ROWS:
            setattr(res, name, _prioritize(rows))
    return res


def _prioritize(rows: list) -> list:
    """超上限时的截断顺序：A 档 → B 档 → 兜底档，同档保原序（= 终端已排好的序）。"""
    order = {TIER_A: 0, TIER_B: 1, TIER_C: 2}
    keyed = []
    for i, row in enumerate(rows):
        cat = None
        if hasattr(row, "entry"):
            e = row.entry
            cat = e.get("category") if isinstance(e, dict) else getattr(e, "category", None)
        elif hasattr(row, "rec_category"):
            cat = row.rec_category
        tier, _hit = classify(cat)
        keyed.append((order[tier], i, row))
    keyed.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in keyed[:PUSH_MAX_ROWS]]


__all__ = [
    "GateResult",
    "GateStats",
    "TIER_A",
    "TIER_B",
    "TIER_C",
    "apply_push_gate",
    "classify",
    "gate_hist_rows",
    "gate_hot_rows",
    "gate_main_rows",
    "gate_offboard_rows",
]
