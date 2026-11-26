"""横截面展示层：≡ 段的纯渲染 / 投影函数（从 crosssection.py 拆出，2026-11-25）。

只做展示与排序投影：不取数、不判定信号、不写库。画像串/列投影由终端、面板、
AI 素材三出口复用，任一端重拼即第二口径。阈值单源仍是 `scanner.config`。
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from scanner.config import (
    CS_DISPLAY_MAX_PERCENT,
    CS_DISPLAY_MIN_MAIN_NET,
    CS_PCT_BAND_MID,
    CS_PCT_BAND_TOP,
    CS_PERSIST_MIN,
    CS_POS_ACCEL,
    CS_POS_HIGH,
    CS_POS_LAUNCH,
    CS_STRONG_PERSIST,
)


def _consistency(speed_1: float | None, speed_3: float | None, speed_5: float | None) -> str:
    """三窗口方向一致性（**纯层内**：全部来自本层环形缓冲的 1/3/5 轮差分）。

    为什么要它：单窗口为正可能只是一次噪声尖刺；三个窗口同号才说明价格在
    **持续朝一个方向走**。这是本层唯一能自证的「信号真伪」判据 —— 不需要
    任何外部区域（榜单/资金流/B 段）的数据参与。

    返回 `全正` / `全负` / `混合`。
    """
    vals = [v for v in (speed_1, speed_3, speed_5) if isinstance(v, (int, float)) and math.isfinite(v)]
    if len(vals) < 3:
        return "混合"
    if all(v > 0 for v in vals):
        return "全正"
    if all(v < 0 for v in vals):
        return "全负"
    return "混合"

def _volume_price(amount_surge: float | None, speed_1: float | None) -> str:
    """量价配合（**纯层内**：`amount_surge` 与 `speed_1` 都是本层自算字段）。

    `配合` = 放量且价在动；`背离` = 明显放量但价格没跟（滞涨/派发嫌疑）；
    其余记 `—`（不足判断）。
    """
    if not isinstance(amount_surge, (int, float)) or not isinstance(speed_1, (int, float)):
        return "—"
    if not math.isfinite(amount_surge) or not math.isfinite(speed_1):
        return "—"
    if amount_surge > 2.0 and speed_1 <= 0:
        return "背离"
    if amount_surge > 1.0 and speed_1 > 0:
        return "配合"
    return "—"

#: 旧描述性分节的展示顺序（2026-11-19；档位分组上线后不进主展示，保留供兼容）。
#: 背离放最后：它是负向提示，不是候选。≡ 区现按 `PCT_BAND_ORDER` 分档。
SECTION_ORDER: tuple[str, ...] = ("低位·资金强", "持续走强", "高位加速", "量价背离")

def _pos_tier(percent: float | None) -> str:
    """当日涨幅 → 位置档（蓄势/启动/加速/高位；缺值 `—`）。阈值单源 `CS_POS_*`。

    回答「低位启动 vs 已冲高」：同是 p95+ 异动，+4% 刚起步和 +14% 已冲高的
    追入风险完全不同，但旧展示里这一信息淹没在数字列里。
    """
    if not isinstance(percent, (int, float)) or not math.isfinite(percent):
        return "—"
    if percent < CS_POS_LAUNCH:
        return "蓄势"
    if percent < CS_POS_ACCEL:
        return "启动"
    if percent < CS_POS_HIGH:
        return "加速"
    return "高位"

def _motion(consistency: str | None, volume_price: str | None) -> str:
    """一致性 × 量价 → 动向短语（画像串第三段；只压缩已知判据，不发明新含义）。

    优先级：背离 > 全正 > 全负 > 其余 —— 背离是风险提示必须先说；
    `量价齐升` = 三窗同向且放量被价格确认（旧「全正·配合」）；
    `放量滞涨` = 明显放量但价没跟（旧「背离」）；`一致回落` = 三窗同负
    （本层不预判方向，负向异动同样入围，如实描述）。
    """
    cons = consistency or "混合"
    vp = volume_price or "—"
    if vp == "背离":
        return "放量滞涨"
    if cons == "全正" and vp == "配合":
        return "量价齐升"
    if cons == "全正":
        return "稳步走强"
    if cons == "全负":
        return "一致回落"
    if vp == "配合":
        return "反复放量"
    return "动向不明"

def _fmt_money(value: float | None) -> str:
    """金额（元）→ 带符号的万/亿短语（`+4213万` / `-2.1亿` / `持平` / `—`）。

    净额与增量共用同一套单位换算，避免两处各写一份漂移（与 ai_digest 工具同纪律）。
    """
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return "—"
    if value == 0:
        return "持平"
    sign = "+" if value > 0 else "-"
    mag = abs(value)
    if mag >= 1e8:
        return f"{sign}{mag / 1e8:.1f}亿"
    return f"{sign}{mag / 1e4:.0f}万"

def _fmt_main_net(main_net: float | None) -> str:
    """主力净流入（元）→ 人类可读短语（万/亿；缺值 `资金—`）。"""
    if not isinstance(main_net, (int, float)) or not math.isfinite(main_net):
        return "资金—"
    return f"主力{_fmt_money(main_net)}"

def _fmt_main_delta(delta: float | None, rounds: int | None) -> str:
    """近 N 轮新增主力净额 → 画像可选尾段（`·近5轮+1800万`）；缺值返回空串。

    轮距随增量一起传（基准回退时不是固定 5），缺轮距则整段不显示 —— 不硬凑。
    """
    if (
        not isinstance(delta, (int, float))
        or not math.isfinite(delta)
        or not isinstance(rounds, (int, float))
        or not math.isfinite(rounds)
    ):
        return ""
    return f"·近{int(rounds)}轮{_fmt_money(delta)}"

def _fmt_flow_threshold(value: float) -> str:
    """展示资金闸阈值 → 人读单位（元/万/亿；与 `_fmt_main_net` 同纪律，别打 5000000 元）。"""
    if value >= 1e8:
        return f"{value / 1e8:g}亿"
    if value >= 1e4:
        return f"{value / 1e4:g}万"
    return f"{value:g}元"

def _flow_gate_note(hidden_flow: int) -> str:
    """展示资金闸的**人读说明**（终端 `≡` 块与网页版口径共用，前端不重拼）。

    文案由本函数单源生成：终端直接打印，面板经 `snapshot_payload.display_flow_note`
    原样渲染。任一端另写一份措辞即会漂移（阈值单位、fail-open 语义、剔除数）。
    """
    return (
        f"展示资金闸：主力净流入 > {_fmt_flow_threshold(CS_DISPLAY_MIN_MAIN_NET)} 才占位"
        f"（当日跌但流入保留；本轮剔 {hidden_flow} 只，仍落库，资金缺失照常展示）"
    )

def _pct_gate_note(hidden_pct: int) -> str:
    """展示涨幅闸的人读说明（终端/网页共用单源，与 `_flow_gate_note` 同纪律）。"""
    return (
        f"展示涨幅闸：当日涨幅 ≤ {CS_DISPLAY_MAX_PERCENT:g}% 才占位"
        f"（本轮剔 {hidden_pct} 只，仍落库，涨幅缺失照常展示）"
    )

def _fmt_main_pct(main_net: Any, amount: Any) -> str:
    """净额 ÷ 当日成交额（%）→ `(占额X.X%)`；分母缺失/非正时返回空串（不硬凑 0）。

    只做展示：净额给体量、占比给跨票可比强度。两者都是当日累计，口径同源、零新增请求。
    """
    if not isinstance(main_net, (int, float)) or not isinstance(amount, (int, float)):
        return ""
    if not math.isfinite(main_net) or not math.isfinite(amount) or amount <= 0:
        return ""
    return f"(占额{main_net / amount * 100:.1f}%)"

def profile_text(s: Mapping[str, Any]) -> str:
    """单条信号 → **中文画像串**（终端 `≡` 行、面板 `画像` 列、AI 素材三出口同源）。

    格式：`位置·持续N轮·动向·主力资金(占额X%)·近K轮新增净额`，如
    `启动·持续8轮·量价齐升·主力+4213万(占额6.8%)·近5轮+1800万`。
    尾段是可选的（增量或实际轮距缺失时不追加），保留旧四段画像的兼容性。
    全部由本层自算/快照自带字段派生（零新增请求、不读其它区域），改口径只改这里
    与 `_pos_tier`/`_motion`/`_fmt_main_net`/`_fmt_main_pct`/`_fmt_main_delta`
    —— 不在终端/前端/提示词里重拼。
    """
    persist = int(s.get("persist") or 1)
    return (
        f"{_pos_tier(s.get('percent'))}·持续{persist}轮"
        f"·{_motion(s.get('consistency'), s.get('volume_price'))}"
        f"·{_fmt_main_net(s.get('main_net'))}"
        f"{_fmt_main_pct(s.get('main_net'), s.get('amount'))}"
        f"{_fmt_main_delta(s.get('main_delta'), s.get('main_delta_rounds'))}"
    )

def _section(s: Mapping[str, Any]) -> str:
    """持续组内的**分节**（描述性归类，非推荐档位；展示顺序见 `SECTION_ORDER`）。

    判定优先级：低位·资金强 > 高位加速 > 量价背离 > 持续走强（默认）。
    每票只归一节。判据全部层内自算 + 快照自带 main_net，不读其它区域数据。
    """
    percent = s.get("percent")
    main_net = s.get("main_net")
    if (
        isinstance(percent, (int, float))
        and math.isfinite(percent)
        and percent < CS_POS_ACCEL
        and isinstance(main_net, (int, float))
        and math.isfinite(main_net)
        and main_net > 0
        and s.get("consistency") == "全正"
        and s.get("volume_price") == "配合"
    ):
        return "低位·资金强"
    if isinstance(percent, (int, float)) and math.isfinite(percent) and percent >= CS_POS_HIGH:
        return "高位加速"
    if s.get("volume_price") == "背离":
        return "量价背离"
    return "持续走强"

#: 分位档位（持续组内子分组），显示顺序：顶部 → 中部 → 边缘。
#: 阈值单源 `CS_PCT_BAND_*`（实测档位，见 config_crosssection docstring）。
PCT_BAND_ORDER: tuple[str, ...] = ("顶部", "中部", "边缘")


def _pct_band(s: Mapping[str, Any]) -> str:
    """当轮复合分位 → 档位名（顶部≥0.99 / 中部≥0.97 / 边缘）。

    数据依据：在持续组内 pct 仍有单调边际（顶部 hit 7.97% / 中部 6.14% / 边缘
    3.15%，去最强日），但**低 persist 时 pct 无区分度** ⇒ 只在持续组内分组，
    不另立展示组（不造第二结论源）。缺值归「边缘」（不可判 ≠ 很强）。
    """
    p = s.get("pct_rank")
    if not isinstance(p, (int, float)) or not math.isfinite(p):
        return "边缘"
    if p >= CS_PCT_BAND_TOP:
        return "顶部"
    if p >= CS_PCT_BAND_MID:
        return "中部"
    return "边缘"


def _quality_rank(s: Mapping[str, Any]) -> int:
    """持续组内的双维度质量序：**persist × pct 档**，越大越靠前。

    档位映射（实测命中序，见 config docstring）：
      双强(persist≥13 且 顶部) > 强持续(persist≥13) > 顶部 > 中部 > 边缘。
    只在持续组内生效（新进组 persist≤1，无持续维度，档位仅作顶部/中部/边缘）。
    """
    band = _pct_band(s)
    strong = int(s.get("persist") or 0) >= CS_STRONG_PERSIST
    if strong and band == "顶部":
        return 4
    if strong:
        return 3
    return {"顶部": 2, "中部": 1, "边缘": 0}[band]


def _rank_tier(s: Mapping[str, Any]) -> int:
    """展示质量层：全正·配合 > 全正·— > 全正·背离/混合·配合 > 其余。

    只看本层自算字段（consistency / volume_price），不引入外部区域数据。
    """
    cons = s.get("consistency")
    vp = s.get("volume_price")
    if cons == "全正" and vp == "配合":
        return 3
    if cons == "全正" and vp == "—":
        return 2
    if cons == "全正" and vp == "背离":
        return 1
    if cons == "混合" and vp == "配合":
        return 1
    return 0

#: 展示分组码（`signal_sort_key` 的首个分量，也是一一对应）：越大越靠前。
#: 终端 `≡` 块按它分两段展示；中间组（`_GROUP_MID`）**不展示**（见 config 注释）。
_GROUP_PERSIST = 2

_GROUP_MID = 1

_GROUP_NEW = 0

def _display_group(s: Mapping[str, Any]) -> int:
    """展示分组（**纯层内**：只用 `persist`，即本层自己的轮次计数器）。

    持续 = 当日累计出现 >=`CS_PERSIST_MIN` 轮；新进 = 当轮首现（persist <= 1）；
    其余（2 <= persist < CS_PERSIST_MIN）归中间组，**不展示、但仍参与排序**
    （要让 AI 素材与终端同源，就不能从排序里挖一个洞）。
    """
    persist = int(s.get("persist") or 0)
    if persist >= CS_PERSIST_MIN:
        return _GROUP_PERSIST
    if persist <= 1:
        return _GROUP_NEW
    return _GROUP_MID

def _display_pct_ok(s: Mapping[str, Any]) -> bool:
    """展示涨幅闸：只挡明确超过上限的票；缺值无法判 ⇒ fail-open 照常展示。
    与 `_display_flow_ok` 同纪律：只在两个展示出口调用，信号仍全部落库、排序键不变。
    """
    pct = s.get("percent")
    if not isinstance(pct, (int, float)) or not math.isfinite(pct):
        return True
    return float(pct) <= CS_DISPLAY_MAX_PERCENT

def _display_flow_ok(s: Mapping[str, Any]) -> bool:
    """展示层资金闸（2026-11-20）：只挡**明确**主力净流入不足的票。

    判据只有 `main_net > CS_DISPLAY_MIN_MAIN_NET`（默认 300 万，严格大于），
    **不看当日涨跌** —— 跌 8% 但主力净流入 >300 万的票照常留，涨 8% 但净流入
    不足 300 万的票不占展示位。快照没给
    `main_net` 时无法判资金 ⇒ fail-open 照常展示（不把「没数据」当「流出」）。
    只在 `snapshot_payload` / `format_terminal_block` 出口调用：信号仍全部落库、
    排序键不变，避免展示层变成第二结论源。
    """
    main_net = s.get("main_net")
    if not isinstance(main_net, (int, float)) or not math.isfinite(main_net):
        return True
    return float(main_net) > CS_DISPLAY_MIN_MAIN_NET

def signal_sort_key(s: Mapping[str, Any]) -> tuple[float, ...]:
    """终端 `≡` 块与 AI 素材**共用**的展示排序键（2026-11-17 改分组优先）。

    先分组（稳定锚点），再比质量/持续/强度：
      展示分组（持续 > 中间 > 新进）
      → 质量层(全正·配合 > 全正·— > 其余) → 当日轮次 → 横截面分位 → 额涌 → 量比。
    返回「越小越靠前」的元组（各键取负）。轮次/分位等缺值按 0 参与，
    与 `_zscore` 的缺值纪律一致：缺值是「未知」而非「很强」。

    ## 为什么分组提到质量层前面（2026-11-17）

    旧键是「质量层 → 轮次 → 分位…」，而 `全正·配合` 在 **1 轮窗口**里噪声极易
    达成 ⇒ 实测当日展示行相邻两轮重叠**中位数 1/8**（几乎整屏换新），人读不出重点
    （用户原话：「变化太快，眼花缭乱不知道咋选」）。稳定的票确实存在（当日 756 只
    里 180 只出现过 >=5 轮），但被单轮噪声顶下去了。
    分组后：持续组内部仍按质量→轮次→分位排（组内质量闸不变）。
    """
    return (
        -float(_display_group(s)),
        -float(_quality_rank(s)),
        -float(_rank_tier(s)),
        -float(s.get("persist") or 0),
        -float(s.get("pct_rank") or 0.0),
        -float(s.get("amount_surge") or 0.0),
        -float(s.get("vol_ratio") or 0.0),
    )

def _fmt_num(v: float | None, digits: int = 2, suffix: str = "") -> str:
    if v is None or not isinstance(v, (int, float)) or not math.isfinite(v):
        return "—"
    return f"{v:.{digits}f}{suffix}"

#: 本层**面向面板（web）的列投影**。终端 `≡` 块是自由文本（宽度靠 f-string 拼），
#: 故这张表只服务面板 —— 但字段名与 `_row()` 逐个对应，改一边要同改另一边。
#: 2026-11-19（方案二）：数字列撤出终端/面板（仍落库），改画像 + 分节；
#: `画像` 列吃 `profile_text`（Python 拼，前端不重拼——重拼即第二份口径）。
COLS_CS: tuple = (
    ("#", 3, "r"),
    ("代码", 12, "l"),
    ("名称", 10, "l"),
    ("现价", 8, "r"),
    ("涨幅", 8, "r"),
    ("轮次", 6, "r"),
    ("分节", 12, "l"),
    # 画像含可选「近K轮新增」尾段（2026-11-25）：实测最长 59ch（蓄势/持续12轮/亿级/
    # 两位数轮距），旧 34ch 会挤掉尾段。前端按 `spec.width}ch` 设 minWidth，单位即可见宽度。
    ("画像", 64, "l"),
)

def _signal_row(s: Mapping[str, Any]) -> dict[str, Any]:
    """单条信号 → 面板行（**纯标量**，键名与 `COLS_CS` 的单元格取值一一对应）。

    `profile`/`section` 由 Python 单源拼（`profile_text`/`_section`），前端原样渲染，
    **不在 TS 里重拼**（重拼即第二份口径）。数字列不再下发（仍落库可回查）。
    """
    return {
        "symbol": s.get("symbol"),
        "name": s.get("name"),
        "price": s.get("price"),
        "percent": s.get("percent"),
        "persist": int(s.get("persist") or 1),
        "profile": profile_text(s),
        "section": _pct_band(s),
    }
