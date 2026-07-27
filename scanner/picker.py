"""今日首选选择器：从全量候选中按规则机械选出 top_n 只，减少用户选择迷茫。

这不是预测模型，只是把已在 STRATEGY.md 文档化的实操纪律机械应用：
1. 大盘弱势 → 仅 momentum 候选可见（弱市不抢反弹/超短）
2. 剔除带 超买/主力出货/趋势破位 风险标签的票（追高/派发/破位不碰）
3. 按确定性优先、评分次之排序（替代原策略优先级排序）
4. 分散约束：第二只不选与第一只同板块的票（避免双暴露）

样本仅 3 个月、A 股短线噪声大，本模块不会提高胜率，只减少 cognitive load。
"""

from dataclasses import dataclass

from scanner.config import (
    CONVICTION_STAR_THRESHOLDS,
    CONVICTION_WEIGHTS,
)
from scanner.models import Candidate

# 阻断性风险标签：触发即剔除出首选池
BLOCKING_RISK_TAGS = {"超买", "主力出货", "趋势破位"}


@dataclass
class PickResult:
    """单只首选的结果，包含候选、理由、确定性、信号和对比。"""
    candidate: Candidate
    reason: str
    conviction: int          # 1-5 星
    signals: list[str]       # 驱动信号列表
    vs_text: str = ""        # 与另一只的对比文字


def pick_top_candidates(
    all_candidates: list[Candidate],
    top_n: int = 2,
) -> list[PickResult]:
    """从全量候选中选出 top_n 只"今日首选"。

    排序逻辑：确定性优先 → 评分为辅（替代原策略优先级排序）。

    Args:
        all_candidates: 所有策略桶的候选并集（含双挂）
        top_n: 选几只，默认 2

    Returns:
        [PickResult, ...] 长度 <= top_n。无候选时返回空列表。
    """
    if not all_candidates:
        return []

    # 1. 大盘环境检测（遍历找第一个非零 market_env_bonus，避免依赖迭代顺序）
    market_bonus = _detect_market_bonus(all_candidates)
    weak_market = market_bonus < 0

    # 2. 过滤风险标签 + 弱市策略限制 + 按 symbol 去重
    seen_syms: set[str] = set()
    safe: list[Candidate] = []
    for c in all_candidates:
        if c.stock.symbol in seen_syms:
            continue
        seen_syms.add(c.stock.symbol)
        if set(c.risk_flags) & BLOCKING_RISK_TAGS:
            continue
        cat = _normalize_category(c.category)
        if weak_market and cat != "momentum":
            continue
        safe.append(c)

    if not safe:
        return []

    # 3. 按确定性优先 + 评分次之排序
    conv_map = {c.stock.symbol: _compute_conviction(c) for c in safe}
    safe.sort(key=lambda c: (-conv_map[c.stock.symbol], -c.score))

    # 4. 选 top_n，第二只避免同板块
    picks: list[PickResult] = []
    picked_sectors: set[str] = set()
    for c in safe:
        if len(picks) >= top_n:
            break
        if len(picks) >= 1 and c.sector and c.sector in picked_sectors:
            continue
        conv = conv_map[c.stock.symbol]
        signals = _build_signals(c, weak_market)
        picks.append(PickResult(
            candidate=c,
            reason=_build_reason(c, weak_market),
            conviction=conv,
            signals=signals,
        ))
        if c.sector:
            picked_sectors.add(c.sector)

    # 5. 兜底：因分散约束没选够时，放宽约束补齐
    if len(picks) < top_n:
        for c in safe:
            if len(picks) >= top_n:
                break
            if any(p.candidate.stock.symbol == c.stock.symbol for p in picks):
                continue
            conv = conv_map[c.stock.symbol]
            signals = _build_signals(c, weak_market)
            picks.append(PickResult(
                candidate=c,
                reason=_build_reason(c, weak_market),
                conviction=conv,
                signals=signals,
            ))

    # 6. 生成 vs 对比文字
    if len(picks) >= 2:
        picks[0].vs_text = _build_vs_text(picks[0], picks[1])
        picks[1].vs_text = _build_vs_text(picks[1], picks[0])

    return picks


def _detect_market_bonus(candidates: list[Candidate]) -> int:
    """遍历找第一个非零 market_env_bonus，不依赖迭代顺序。"""
    for c in candidates:
        if c.kline and c.kline.dimensions:
            val = c.kline.dimensions.get("market_env_bonus", 0) or 0
            if val != 0:
                return val
    return 0


def _normalize_category(cat: str) -> str:
    """known_new_face 归入 new_face 桶统一比较。"""
    return "new_face" if cat == "known_new_face" else cat


def _compute_conviction(c: Candidate) -> int:
    """计算确定性评分（1-5 星）。

    积分制：四个因子各自贡献原始分，求和后按阈值映射到 1-5 星。
    总分范围 0-70。
    """
    dims = c.kline.dimensions if c.kline else {}
    w = CONVICTION_WEIGHTS

    # 因子 1: 验证通过率 (pos_dims / max_dims → 0-25)
    pos = dims.get("_pos_dims", 0)
    mx = dims.get("_max_dims", 3)
    depth_score = (pos / mx * w["validation_depth"]) if mx > 0 else 0

    # 因子 2: 风险标签清晰度 (0标签=25, 1=12, 2+=0)
    n_risks = len(c.risk_flags)
    if n_risks == 0:
        risk_score = float(w["risk_clarity"])
    elif n_risks == 1:
        risk_score = w["risk_clarity"] / 2.0
    else:
        risk_score = 0.0

    # 因子 3: 实时量确认
    vol_score = float(w["volume_confirm"]) if c.live_vol_bonus > 0 else 0.0

    # 因子 4: 板块支撑
    sec_score = float(w["sector_support"]) if c.sector_bonus > 0 else 0.0

    # 求和 → 映射到 1-5 星
    raw = depth_score + risk_score + vol_score + sec_score
    stars = 1
    for threshold in CONVICTION_STAR_THRESHOLDS:
        if raw >= threshold:
            stars += 1
    return stars


def _build_signals(c: Candidate, weak_market: bool) -> list[str]:
    """提取 top 驱动信号（按贡献大小排序，最多 3 个）。"""
    signals: list[tuple[int, str]] = []

    if c.sector_bonus:
        signals.append((c.sector_bonus, f"板块共振+{c.sector_bonus}"))
    if c.live_vol_bonus:
        signals.append((c.live_vol_bonus, "放量确认"))
    if c.first_breakout_bonus:
        signals.append((c.first_breakout_bonus, "首次突破"))
    if c.gap_up_bonus:
        signals.append((c.gap_up_bonus, "跳空"))
    if c.list_momentum_bonus > 0:
        signals.append((c.list_momentum_bonus, "持续上榜"))
    if c.turnover_bonus > 0:
        signals.append((c.turnover_bonus, "换手健康"))
    if c.rps_bonus > 0:
        signals.append((c.rps_bonus, "相对强度"))

    # 验证维度信号
    dims = c.kline.dimensions if c.kline else {}
    pos = dims.get("_pos_dims", 0)
    mx = dims.get("_max_dims", 3)
    if pos > 0:
        signals.append((pos * 3, f"验证{pos}/{mx}维通过"))

    # 按贡献排序，取 top 3
    signals.sort(key=lambda x: -x[0])
    return [s[1] for s in signals[:3]]


def _build_vs_text(this: PickResult, other: PickResult) -> str:
    """生成与另一只的对比文字（共通事实比较）。"""
    parts: list[str] = []
    tc = this.candidate
    oc = other.candidate

    # 评分差
    diff = tc.score - oc.score
    if diff > 0:
        parts.append(f"高{diff}分")
    elif diff < 0:
        parts.append(f"低{-diff}分")

    # 确定性差
    if this.conviction > other.conviction:
        parts.append("确定性更高")
    elif this.conviction < other.conviction:
        parts.append("确定性较低")

    # 板块支撑对比
    if tc.sector_bonus and not oc.sector_bonus:
        parts.append("有板块共振")
    elif not tc.sector_bonus and oc.sector_bonus:
        parts.append("无板块共振")
    elif tc.sector_bonus and oc.sector_bonus:
        if tc.sector_bonus > oc.sector_bonus:
            parts.append("板块更强")

    # 量能对比
    if tc.live_vol_bonus and not oc.live_vol_bonus:
        parts.append("放量确认")
    elif not tc.live_vol_bonus and oc.live_vol_bonus:
        parts.append("量能偏弱")

    # 风险对比
    n_risk_this = len(tc.risk_flags)
    n_risk_other = len(oc.risk_flags)
    if n_risk_this == 0 and n_risk_other > 0:
        parts.append("无风险标签")
    elif n_risk_this > 0 and n_risk_other == 0:
        parts.append(f"有{len(tc.risk_flags)}个风险标签")
    elif n_risk_this > n_risk_other:
        parts.append("风险标签更多")

    return " | ".join(parts) if parts else ""


def _build_reason(c: Candidate, weak_market: bool) -> str:
    """生成一行人类可读的选择理由。"""
    cat_display = c.category if c.category != "known_new_face" else "new_face"
    parts = [f"{cat_display} {c.score}分"]
    if c.first_breakout_bonus:
        parts.append("首次突破")
    if c.sector_bonus:
        parts.append(f"板块共振+{c.sector_bonus}")
    if c.live_vol_bonus:
        parts.append("放量确认")
    if c.gap_up_bonus:
        parts.append("跳空")
    if weak_market:
        parts.append("弱市仅取动量")
    if not c.risk_flags:
        parts.append("无风险标签")
    return "+".join(parts)
