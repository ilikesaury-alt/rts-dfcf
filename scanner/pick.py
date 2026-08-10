"""今日选股建议：从综合排序候选里挑出值得买的 2 只。

背景（2026-08-10 调研）：综合排序 score 是"热度放大器"，跨类别混排后
直接取前 2 历史 3 日累计 -3.1%，比随机（-2.4%）还差。真正有区分度的是
**类别** + **市场环境**，而非分数：

  - rebound（超跌反弹）：全期 cum_3d +7.59% / 胜率 76% —— 唯一稳定强正效
  - momentum（动量）：全期 +2.33%，但近 30 天弱市转 -3.36% —— 弱势最脆弱
  - short_term（超短次日）：全期 +0.40%，有 rebound 的日子 +1.35% vs 无 -1.52%
  - new_face / known_new_face：负期望（-1.58% / -0.44%）
  - pullback：已下线（-7.15%）；comeback：样本不足不参与

选股规则（数据驱动，见模块 docstring 与 pick 函数注释）：
  1. 预过滤：剔除负期望类别（new_face/kNF/pullback/comeback）、硬风险标签、
     劣后档（资金净流出）；剔除重复 symbol。
  2. 分层：rebound > short_term > momentum。momentum 仅在市场非弱势时放行
     （近 30 天弱市 -3.36%，弱势追动量 = 送钱）。
  3. 目标 2 只，不同板块优先（板块普涨日同板块票同涨同跌，等于没分散）。
  4. 输出含理由：为什么选这 2 只 / 为什么排除某类别，供展示层渲染。
"""
from __future__ import annotations

from scanner.config import RISK_FLAGS_HARD_FILTER
from scanner.sector import classify_sector

# 选股优先级（类别层，非 score 层）：数据依据见模块 docstring
PICK_PRIORITY = ("rebound", "short_term", "momentum")

# 负期望类别：综合排序可展示，但选股建议不纳入
PICK_EXCLUDED_CATEGORIES = {"new_face", "known_new_face", "pullback", "comeback"}


def _entry_sector(entry: dict) -> str:
    """板块判定：候选推动概念 > 候选分类板块 > DB concept > 名称关键词。"""
    c = entry.get("_candidate")
    if c is not None:
        if getattr(c, "driving_concept", ""):
            return c.driving_concept
        if getattr(c, "sector", ""):
            return c.sector
    db_concept = (entry.get("concept") or "").strip()
    if db_concept:
        return db_concept
    return classify_sector(entry.get("name", ""))


def _entry_is_hard_risk(entry: dict) -> bool:
    """候选命中硬过滤风险标签（主力出货/趋势破位）→ 不可买。"""
    c = entry.get("_candidate")
    if c is None:
        return False
    return bool(set(getattr(c, "risk_flags", []) or []) & RISK_FLAGS_HARD_FILTER)


def _entry_is_outflow(entry: dict) -> bool:
    """劣后档：主力净流出 ≤ -5%（资金流出，出货嫌疑）。无数据不判劣后。"""
    c = entry.get("_candidate")
    if c is None:
        return False
    dims = getattr(c.kline, "dimensions", None) if c.kline else None
    ff = (dims or {}).get("fund_flow_main_pct")
    if ff is None:
        return False
    return ff <= -5.0


def _market_weak(entry: dict | None) -> bool:
    """市场环境：从候选 dims 读 market_env_bonus（<0 即弱市）。"""
    if entry is None:
        return False
    c = entry.get("_candidate")
    if c is None or not c.kline:
        return False
    return (c.kline.dimensions.get("market_env_bonus") or 0) < 0


def build_pick_suggestion(entries: list[dict], target: int = 2) -> dict:
    """从今日推荐 entries（已关联 _candidate）里挑建议买入的 target 只。

    返回 {"picks": [entry...], "reasons": [str...]}。picks 最多 target 只，
    不同板块优先；选不足时放宽同板块。reasons 说明选择/排除逻辑。
    """
    reasons: list[str] = []
    # 1) 预过滤
    seen_syms: set[str] = set()
    pool: list[dict] = []
    for e in entries:
        sym = e.get("symbol")
        if not sym or sym in seen_syms:
            continue
        seen_syms.add(sym)
        cat = e.get("category", "")
        if cat in PICK_EXCLUDED_CATEGORIES:
            continue
        if _entry_is_hard_risk(e):
            reasons.append(f"排除 {e.get('name','')}：命中硬风险标签")
            continue
        if _entry_is_outflow(e):
            reasons.append(f"排除 {e.get('name','')}：主力净流出(劣后档)")
            continue
        pool.append(e)
    if not pool:
        return {"picks": [], "reasons": reasons or ["今日无可买候选（全部被过滤或为负期望类别）"]}

    # 2) 弱市屏蔽 momentum
    weak = _market_weak(pool[0])
    allowed = [e for e in pool if not (weak and e.get("category") == "momentum")]
    skipped_mom = weak and len(allowed) < len(pool)
    if skipped_mom:
        reasons.append("市场弱势：跳过 momentum（近30天弱市 cum_3d -3.36% 转负）")

    # 3) 分层：类别优先级 > 类内 score 降序；板块去重优先
    def _pick_key(e):
        cat = e.get("category", "")
        prio = PICK_PRIORITY.index(cat) if cat in PICK_PRIORITY else len(PICK_PRIORITY)
        return (prio, -int(e.get("score", 0) or 0))

    allowed.sort(key=_pick_key)

    picks: list[dict] = []
    used_sectors: set[str] = set()
    # 第一轮：跨板块去重
    for e in allowed:
        if len(picks) >= target:
            break
        sec = _entry_sector(e)
        if sec and sec != "其他" and sec in used_sectors:
            continue
        picks.append(e)
        if sec:
            used_sectors.add(sec)
    # 第二轮：不足则放宽同板块（按剩余优先级补齐）
    if len(picks) < target:
        for e in allowed:
            if len(picks) >= target:
                break
            if e in picks:
                continue
            picks.append(e)

    # 4) 理由
    if not picks:
        return {"picks": [], "reasons": reasons or ["无可选候选"]}
    cat_names = {
        "rebound": "超跌反弹", "short_term": "超短次日", "momentum": "动量",
    }
    if len(picks) < target:
        reasons.append(f"候选池仅 {len(picks)} 只可买，不足 {target} 只")
    picked_desc = "、".join(
        f"{e.get('name','')}[{cat_names.get(e.get('category',''), e.get('category',''))}]"
        for e in picks
    )
    reasons.append(f"建议: {picked_desc}")
    return {"picks": picks, "reasons": reasons}
