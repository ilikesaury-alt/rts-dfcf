"""用户可见标签登记簿 —— 展示层标签的单源元数据表（2026-09-28 新增）。

## 为什么有这个模块

仓库里 94 条用户可见标签分散在 10+ 个模块，出处分三类：本仓回测（33）、
通用规律（17）、**无任何依据（44）**。用户看到「稳★」「⚡」「▼」时，无从判断
它是「交易所制度事实」还是「某次拍脑袋的常量」。

本模块**不改变任何一条标签的判定**，只把展示层标签的元数据收敛成一张可审计的
表，每条给五件事：

  labels   用户可见字面量（终端 / 卡片 / 变体）
  rule     判定规则（可复现）
  source   出处分级 S1~S5 或 D（见 `SOURCES`）
  grade    证据等级 E3~E0（见 `GRADES`）
  counter  反误读：这条标签**不能**读成什么

## 四条硬规则（`tests/test_label_registry.py` 守护）

  G1  `source` 必须是 `SOURCES` 里的一级，`grade` 必须是 `GRADES` 里的一级；
  G2  `S5`（本仓拟合）⟹ `grade == "E0"` —— 本仓拟合只能作「未校准提示」，
      禁止当标签依据；`D`（系统定义）⟹ `grade == "E2"`；
  G3  `grade == "E0"`（即 `unverified`）⟹ `counter` 必须含披露关键词
      （`未校准` / `未回测` / `未验证` / `未样本外` / `未过样本外` / `尚未回测`）；
  G4  spec 声明的 `labels` 必须真的出现在 `surfaces` 指向的渲染模块里
      —— 防登记簿与实际渲染脱钩（登记了却没人画，或画了却没登记）。

`unverified` 是 `grade == "E0"` 的派生属性（by construction，不存在「标了 E0
却自称已校准」的中间态）。

## 边界（本模块不做什么）

**纯展示**。评分/排序/准入链路（`config_scoring.CATEGORY_HIT_RATE`、桶准入、
`V_*` 加分、`signals.fund_flow_signal`、`ranking._fund_flow_norm`）不在本表
登记，也不因本模块改变 —— 本模块只被 `view.render` 与 `feishu` 消费，
**没有任何评分模块 import 它**（反向依赖守卫见测试）。

## 一行图例

`legend_line(section)` 生成**一行**极简「反误读」图例。2026-09-16 曾按用户
决策移除双端的**三行长图例**（冗长）；2026-09-28 恢复一行，**同日**用户再次
决策「去掉显示读法」把它整体停用 —— 终端与飞书**都不再渲染**它。
本函数与本表的 `counter` 文案**原样保留**：scripts/label_audit.py 打印它，
测试仍守它单行、含披露关键词、与生产字面量一致；将来挂回渲染时，两端必须
同源调本函数（不要另起第二份文案）。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "DISCLOSURE_KEYWORDS",
    "GRADES",
    "LABEL_REGISTRY",
    "LEGEND_SECTIONS",
    "SOURCES",
    "LabelSpec",
    "audit",
    "legend_line",
]

# ── 出处分级 ────────────────────────────────────────────────────────────────
# S1~S4 是「外部可查」，S5 是「本仓自己拟的」，D 是「纯定义、不含经验断言」。
SOURCES: dict[str, str] = {
    "S1": "S1 制度/交易规则 —— 可外部复核（交易所涨跌幅、注册制等）",
    "S2": "S2 原始文献/作者定义（如 Wilder 1978 的 RSI/ADX 定义）",
    "S3": "S3 学术研究结论",
    "S4": "S4 业界通用惯例/工程约定",
    "S5": "S5 本仓数据裁决或拟合 —— 只可作『未校准提示』，禁止作标签依据",
    "D": "D 系统定义 —— 纯描述性标签，不含经验断言，因此无需外部依据",
}

# ── 证据等级 ────────────────────────────────────────────────────────────────
GRADES: dict[str, str] = {
    "E3": "E3 外部可复核的制度或标准定义",
    "E2": "E2 本仓构造/定义 —— 完全可复现且不含经验断言",
    "E1": "E1 文献或业界惯例支持，本仓未做样本外验证",
    "E0": "E0 仅本仓数据裁决或小样本 —— 必须标『未校准』",
}

# E0 的 counter 必须含其中之一 —— 否则「未校准」只是登记簿里的内部状态，
# 用户看不到，等于没标。
DISCLOSURE_KEYWORDS: tuple[str, ...] = (
    "未校准",
    "未回测",
    "未验证",
    "未样本外",
    "未过样本外",
    "未做样本外校准",
    "尚未回测",
)

# 一行图例覆盖的区块（终端与飞书同名）。
LEGEND_SECTIONS: tuple[str, ...] = ("pool", "hist", "hot")


@dataclass(frozen=True)
class LabelSpec:
    """一条用户可见标签的元数据。

    `labels` 为空 = 该 spec 描述的是**列/字段**而非字面标记（如回捞评分列），
    此时 G4 的「字面量必须出现在渲染模块」不适用。
    """

    id: str
    labels: tuple[str, ...]
    surfaces: tuple[str, ...]  # 渲染该标签的模块（相对仓库根），供 G4 核对
    where: str  # 出现位置（人读）
    rule: str  # 判定规则（可复现）
    source: str  # "S1: 出处正文"
    grade: str  # E3/E2/E1/E0
    counter: str  # 反误读：不能读成什么
    sections: tuple[str, ...] = ()  # 进哪些区块的一行图例

    @property
    def unverified(self) -> bool:
        """是否「未校准」—— by construction 等于 grade == E0。"""
        return self.grade == "E0"

    @property
    def source_code(self) -> str:
        return self.source.split(":", 1)[0].strip()


# ── 登记簿（顺序 = 图例里的出现顺序）────────────────────────────────────────
LABEL_REGISTRY: dict[str, LabelSpec] = {
    "fund_outflow": LabelSpec(
        id="fund_outflow",
        labels=("▼", "▼▼", "🔴", "🔴🔴"),
        surfaces=("scanner/view/model.py", "scanner/feishu.py"),
        where="四区块行尾（view.model._FUND_FLOW_ICON / _entry_row_suffix；飞书 _marks_tail_card）",
        rule="主力净占比 ff_pct：≤-8% → ▼▼/🔴🔴，≤-5% → ▼/🔴，其余不画（2026-09-28 正流入撤下）",
        source="S5: 本仓阈值（-8% 为展示层硬门、-5% 为语义提示档），无外部依据",
        grade="E0",
        counter="▼/🔴=主力净流出≥5%·本仓阈值未校准（≤-8% 走硬门，故不见 ▼▼）·流出≠次日必跌",
        sections=("pool", "hist", "hot"),
    ),
    "breakout_bolt": LabelSpec(
        id="breakout_bolt",
        labels=("⚡",),
        surfaces=("scanner/view/model.py",),
        where="v1 池选 行尾（view.model._entry_row_suffix 的 breakout_marked 分支）",
        rule="动量加速观察：截至 T-1 收盘前5日累计涨幅 > +20%（BREAKOUT_ACCUM_MIN）"
        "+ 类别门 new_face/kNF 或首推（⚡）/ 非首推 short_term（⚡R）·样本收集中·非排序因子",
        source="S5: 本仓样本外验证（2026-09-29 重设计；TEST 窗 n=914 次日≥7% 9.52% vs 基准 5.55%，"
        "Δ=+4.45pp ≥ MDE=3.14pp）。旧口径「缩量蓄势」经同法检验方向相反，已作废",
        grade="E0",
        counter="⚡=T-1已连涨加速·次日涨超7%概率9.5%（基准5.6%）·阈值未校准·"
        "非排序因子·亏超5%比例翻倍(4.9%→10.3%)·非买入信号",
        sections=("pool",),
    ),
    "guxing_archive": LabelSpec(
        id="guxing_archive",
        labels=("妖",),
        surfaces=("scanner/view/model.py",),
        where="v1 池选 行尾最末（view.model._entry_row_suffix 的 guxing 分支）",
        rule="妖股名单匹配：**静态名单**（config_scoring.GUXING_WATCHLIST，代码↔名称双键录入）"
        "内命中即打「妖」；代码自动归一（裸 6 位/SH/SZ 均支持），名称兼底"
        "（GUXING_MATCH_BY_NAME）。**无任何统计计算**",
        source="S5: 名单内容取自外部调研清单 F:\\downloads\\yaogu_list.json（56 只，"
        "2022~2024，字段 code/name/year/theme），用户 2026-09-30 指定为准；"
        "判定逻辑本身不含经验断言（纯名单成员测试）",
        grade="E0",
        counter="⚠ **不是买入信号**——「妖」只标「这票历史上当过妖股」，不说明下次会涨。"
        "**本标记未过样本外验证**：名单类推法 walk-forward 17 次异动命中 0 次。"
        "**名单随情绪周期整体换血**：公开研究按周期统计，2019 妖股 ∩ 2022 妖股 = ∅；"
        "本名单止于 2024，新妖进不来、掉队的不出去，属未校准静态快照，需定期人工复核。"
        "**板块覆盖不全**：v1 池选只监控创业板 300/301，名单 56 只里仅 13 只创业板票"
        "可能被标到，主板 000/001/002/003/600/601/603/605 的 43 只会永不显示。"
        "收录 ≠ 会涨；没收录 ≠ 不是妖股。·纯展示·不改排序/评分/落库/push_gate",
        sections=("pool",),
    ),
    "trend_steady": LabelSpec(
        id="trend_steady",
        labels=("稳", "稳★"),
        surfaces=("scanner/trend_beauty.py", "scanner/view/model.py", "scanner/feishu.py"),
        where="四区块行尾（trend_beauty.beauty_mark / display_gates.beauty_marks_daily）",
        rule="日线 6 硬门（MA 多头/5日斜率/无暴跌/回调可控/无长上影/距20日高回撤≤阈）全过 → 稳；再加分时 intraday_score≥2.5 → 稳★",
        source="S5: 6 门阈值全部为本仓常量（DAILY_BEAUTY_*），无外部依据",
        grade="E0",
        counter="稳/稳★=日线趋势稳·未做样本外校准（★需分时确认）·★只表示回撤更小·非更易大涨",
        sections=("pool", "hist", "hot"),
    ),
    "hist_score": LabelSpec(
        id="hist_score",
        labels=(),
        surfaces=("scanner/historical_watch.py",),
        where="v1 回捞 评分列（HistCandidate.score）",
        rule="回调深度/量能/时效 加权 50/30/20（HIST_W_*）",
        source="S5: 权重为启发式设定，未做样本外校准",
        grade="E0",
        counter="回捞评分=启发式50/30/20·未做样本外校准·分高≠更可能大涨",
        sections=("hist",),
    ),
    "hot_streak_star": LabelSpec(
        id="hot_streak_star",
        labels=("★",),
        surfaces=("scanner/view/render.py", "scanner/feishu.py"),
        where="沪深飙升 A 段「连击」列（streak ≥ HOT_HIGHLIGHT_STREAK 时前缀 ★）",
        rule="连续上榜轮数 ≥ HOT_HIGHLIGHT_STREAK(=3) → 连击列显示 ★{n}",
        source="D: 系统定义（描述性标签，不含经验断言）",
        grade="E2",
        counter="★=榜内连击≥3轮·非买入信号",
        sections=("hot",),
    ),
    "offboard_tier": LabelSpec(
        id="offboard_tier",
        labels=("T1", "T2"),
        surfaces=("scanner/offboard_watch.py",),
        where="榜外异动 B 段「评分」列（分层标记）",
        rule="涨幅带 × 量比 × 主力净占比 × MA 门 → T1（量先动·价未动）/ T2（启动首日）",
        source="S5: 分层阈值为榜上样本校准，域迁移到榜外不保证成立（模块 docstring 自述）",
        grade="E0",
        counter="T1/T2=榜外分层·尚未回测·非买卖强度",
        sections=("hot",),
    ),
    "risk_flags": LabelSpec(
        id="risk_flags",
        labels=("⚠",),
        surfaces=("scanner/view/model.py", "scanner/feishu.py"),
        where="v1 池选 行尾（_entry_row_suffix 的 risk_flags 分支）+ 飞书 _fmt_row",
        rule="validator 硬标签（超买/主力出货/趋势破位等）按 priority 取首个 + 软标签计数",
        source="S4: 超买与放量出货是技术分析通用风险惯例（RSI/KDJ 超买阈值见 scanner/indicators）",
        grade="E1",
        counter="⚠=风险提示·非买卖指令",
    ),
    "new_flag": LabelSpec(
        id="new_flag",
        labels=("新",),
        surfaces=("scanner/view/render.py", "scanner/feishu.py"),
        where="v1 池选 行首（MainRow.is_new_entry）",
        rule="扫描循环持有的跨轮票集差集（new_symbols），非 recommendations.time",
        source="D: 系统定义（描述性标签，不含经验断言）",
        grade="E2",
        counter="「新」=本轮新进池·非更优",
    ),
    "tactic_tags": LabelSpec(
        id="tactic_tags",
        labels=("⬇减仓", "⬇减半", "⬆加仓", "🔻勿接", "💰落袋"),
        surfaces=("scanner/config_tactics.py",),
        where="v1 池选 行尾（_entry_row_suffix 的 tactic_tags 分支）",
        rule="12 条盘中操作纪律（intraday_tactics.stock_actions），阈值集中在 TACTICS_*",
        source="S4: 纪律主题为技术分析通用惯例，具体阈值为本仓 TACTICS_* 参数",
        grade="E1",
        counter="纪律标签=盘中操作提示·非自动交易指令",
    ),
    "composite_score": LabelSpec(
        id="composite_score",
        labels=(),
        surfaces=("scanner/ranking.py",),
        where="**当前无渲染方**（assemble.py:390 核查：只写不读）",
        rule="cat_base + tech_norm + rank_norm + fund_norm + dip_bonus，封顶 10",
        source="S5: _fund_flow_norm 五档量级自述「未过样本外验证」",
        grade="E0",
        counter="评分含未校准成分（资金流归一 _fund_flow_norm）·当前只写不读、无渲染方",
    ),
}


def legend_line(section: str) -> str:
    """一行极简「反误读」图例（只写「不能读成什么」）。

    顺序 = 登记簿插入顺序（资金流 → ⚡ → 稳 → …）。
    ⚠ **当前两端都不渲染本函数的返回值**（2026-09-28 用户决策「去掉显示读法」），
    调用方只剩 scripts/label_audit.py；反向守卫见 tests/test_display.py。
    """
    parts = [s.counter for s in LABEL_REGISTRY.values() if section in s.sections]
    if not parts:
        return ""
    return "读法：" + "｜".join(parts)


def audit() -> list[str]:
    """跑一遍 G1~G4，返回违规描述列表（空 = 全过）。"""
    problems: list[str] = []
    for spec in LABEL_REGISTRY.values():
        tag = f"[{spec.id}]"
        if spec.source_code not in SOURCES:
            problems.append(f"{tag} source 分级未知：{spec.source_code!r}")
        if spec.grade not in GRADES:
            problems.append(f"{tag} 证据等级未知：{spec.grade!r}")
        if spec.source_code == "S5" and spec.grade != "E0":
            problems.append(f"{tag} S5（本仓拟合）必须落 E0，实际 {spec.grade}")
        if spec.source_code == "D" and spec.grade != "E2":
            problems.append(f"{tag} D（系统定义）必须落 E2，实际 {spec.grade}")
        if spec.unverified and not any(k in spec.counter for k in DISCLOSURE_KEYWORDS):
            problems.append(f"{tag} E0 的 counter 未含披露关键词：{spec.counter}")
        if not spec.counter.strip():
            problems.append(f"{tag} counter 为空")
        for sec in spec.sections:
            if sec not in LEGEND_SECTIONS:
                problems.append(f"{tag} 未知图例区块：{sec!r}")
        for label in spec.labels:
            if not any(_label_in_file(label, path) for path in spec.surfaces):
                problems.append(f"{tag} 字面量 {label!r} 未出现在声明的渲染模块 {spec.surfaces}")
    return problems


def _label_in_file(label: str, rel_path: str) -> bool:
    """字面量是否出现在指定模块源码里（G4）。"""
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(root, rel_path), encoding="utf-8") as fh:
            return label in fh.read()
    except OSError:
        return False


if __name__ == "__main__":  # pragma: no cover - 手工审计入口
    _problems = audit()
    for _spec in LABEL_REGISTRY.values():
        print(f"{_spec.id:<16} {_spec.grade}  {_spec.source_code}  {_spec.counter}")
    print()
    if _problems:
        print(f"发现 {len(_problems)} 处违规：")
        for _p in _problems:
            print(f"  - {_p}")
        raise SystemExit(1)
    print(f"登记簿 {len(LABEL_REGISTRY)} 条，元数据规则全过。")
