"""scanner/config_push.py — 飞书推送「严格过滤门」阈值（2026-09-28 新增）。

## 为什么有这组常量

用户反馈：**飞书推送信息量太大**。实测（`logs/feishu_push.log`，2026-09-28）
全天推 ~50 张卡，冷却期 `FEISHU_MIN_INTERVAL=300s` 几乎每轮都被「票集变化」击穿。

追根（不是飞书的问题，是取数的问题）：

    近 10 个交易日 v1 推荐池的类别构成（去重 symbol/day）
      pool_pick  621 只  92%   实测次日≥7% hit 3.9%
      core_dip   142 只  21%   实测 4.2%
      short_term  15 只   2%   实测 6.7%
      rebound / kNF / momentum / new_face   合计 3 只
    月度产出（recommendations 行数）：pool_pick 自 2026-09 起 0 → 1112，
    而 new_face 160→771→85→13→53、kNF 78→51→9→17、momentum 314→90→103→56 同步崩塌。

即：**量的暴涨来自 9 月上线的 pool_pick 桶，而它恰是全类别 hit 最低的一档。**
所以「更严格的过滤」不是加拍脑袋的分数线，而是把**唯一有实测证据的判别轴**
（类别先验 `CATEGORY_HIT_RATE`）提到推送层当门。

## 为什么判别轴只能是类别先验，不能是 score

同一份 DB（n=3543，取每票当日最后一轮，`hit = next_day_pct >= 7`）实测：

    score=118 → 4.2%    score=100 → 6.2%    score=54 → 21.1%
    score=50  → 5.6%    score=13  → 4.0%    score=0  →  0.0%

**score 与 hit 无单调关系**（噪声级）。而类别先验与实测高度吻合：

    rebound 17.9%(先验17.9%)  kNF 12.5%(12.7%)  new_face 9.8%(9.7%)
    momentum 10.4%(10.0%)     short_term 6.2%(6.2%)  core_dip 4.2%(6.5%)
    pool_pick 3.7%(2.1%)     comeback 2.8%

⇒ 过滤器的主键 = 类别先验；score 一律**不参与**（它连相关性都没有，拿它当门是
拿噪声冒充纪律）。这与 AGENTS.md「目标函数 = 次日≥7% hit」一致：过滤门必须
对着这个口径设，不能对着展示分设。

## 本模块的阈值全部是「门的位置」，不是「新的预测」

三档分级 + 一条兜底通道，理由见 `scanner/push_gate.py` 模块 docstring。
分级边界**不新造数字**：`PUSH_TIER_B_MIN` 直接取 `CATEGORY_HIT_RATE_DEFAULT`
（全体兜底 hit 率 0.078，即「不低于无差别基线」这一天然分界），
`PUSH_TIER_A_MIN` 取 0.10 —— 落在 momentum(10.0%) 之上、new_face(9.7%) 之下，
把「先验 ≥ 两倍基线」定为 A 档，**先验表一变，本门自动跟着动**。

⚠ 本门是**纯展示层**：不写库、不改 `excluded`、不落 `recommendations`、
不参与回测/归因样本口径（与 `view.assemble` 的资金流出硬门同一纪律）。
⚠ 与终端的关系：这是**用户 2026-09-28 明确决策**引入的**第二个出口**，
终端四区块**不套用本门**（终端仍是全量信息面）。代价是「终端有、飞书无」
的分叉，故卡片**必须**显式打出剔除数（`gate_note`），不允许静默少票。
"""

from __future__ import annotations

from scanner.config_core import _env_flag
from scanner.config_scoring import CATEGORY_HIT_RATE_DEFAULT

# ── 开关 ──
# 总闸：RTS_PUSH_GATE=0 可整体关掉本门，恢复「终端推什么飞书推什么」的旧行为
# （保留是为了可回滚，不是推荐用法 —— 用户要的就是更严）。
PUSH_GATE_ENABLED = _env_flag("RTS_PUSH_GATE", True)

# ── 档位边界（唯一的两个「数」）──
# A 档：先验 ≥ 2×基线。B 档：先验 ≥ 基线（= CATEGORY_HIT_RATE_DEFAULT，不另立同义常量）。
# C 档：先验 < 基线 —— 默认不推，仅在「榜单热度」达标时走兜底通道。
PUSH_TIER_B_MIN = CATEGORY_HIT_RATE_DEFAULT  # 0.078
PUSH_TIER_A_MIN = 0.10

# ── 兜底通道（C 档专用）──
# 高先验桶（rebound/kNF/momentum/new_face）近 10 个交易日几乎停产（合计 3 只），
# 若 C 档一刀切死，飞书会**长期静默** —— 那不是「更严格」，那是「不工作了」。
# 故留一条**显式降级**的兜底通道：C 档票只要有榜内热度证据就放行，
# 但卡片上**必须**标明这些是兜底档（gate_note 写明），不与 A/B 档混为一谈。
PUSH_FALLBACK_ENABLED = True
PUSH_FALLBACK_MAX_RANK = 40  # 榜内排名 ≤40（飙升榜主名单量级）即视为有热度
PUSH_FALLBACK_MIN_STREAK = 3  # 沪深飙升 A 段：连击 ≥N 轮（对齐 HOT_HIGHLIGHT_STREAK 语义）
PUSH_FALLBACK_MIN_RANK_RISE = 30  # A 段：排名上升 ≥N 名
PUSH_FALLBACK_MIN_VOL_RATIO = 2.0  # v1 回捞 B 段无榜内排名，用量比代替热度

# ── 卡片级闸 ──
# 至少这么多行通过才推一张卡 —— 1 行也推（用户要的是「精」不是「空」），
# 但 0 行绝不推空卡（沿用 view_has_content 的既有 empty 语义）。
PUSH_MIN_ROWS = 1
# 单卡最多几行（硬上限，防止某天 A 档集体爆发又变成长卡）。
PUSH_MAX_ROWS = 6
# 严格过滤后的最小推送间隔（秒）。旧值 FEISHU_MIN_INTERVAL=300 在「票集每轮都变」
# 的现实下等于没有 —— 提���到 900s（15 分钟），并配合「通过票集变化」才推。
PUSH_MIN_INTERVAL = 900

__all__ = [
    "PUSH_GATE_ENABLED",
    "PUSH_TIER_A_MIN",
    "PUSH_TIER_B_MIN",
    "PUSH_FALLBACK_ENABLED",
    "PUSH_FALLBACK_MAX_RANK",
    "PUSH_FALLBACK_MIN_STREAK",
    "PUSH_FALLBACK_MIN_RANK_RISE",
    "PUSH_FALLBACK_MIN_VOL_RATIO",
    "PUSH_MIN_ROWS",
    "PUSH_MAX_ROWS",
    "PUSH_MIN_INTERVAL",
]
