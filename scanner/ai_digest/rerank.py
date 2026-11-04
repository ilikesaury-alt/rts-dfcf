"""v1 池选 · listwise 精筛的**可复现载体**（2026-11-04 建，同日下线 CLI 入口）。

## 为什么它还在（而 CLI 入口已下线）

建它的原始目的是回答「今天池子里这十几只，我该优先看哪只」。**做完了，结论是
「不值得作为日常命令」**：
- Phase 1（`scripts/rerank_backtest.py`）：排序依据里**类别先验有效、分数无效**
  （类内 rank IC +0.027 vs −0.020）⇒ 「分数不进排序」这条被写进了生产
  （v1 池选删掉「评分」列、改显「类别·先验」列）。
- Phase 2（本模块 + `scripts/rerank_llm_backtest.py`）：**模型自己排序相对确定性排序
  没有显著增量**（ΔIC +0.0525，95% CI [−0.049,+0.148]，30 个历史日重建池）⇒
  「让 LLM 决定排第几」既花钱又不可复现，还多一份会漂移的第二结论源。

⇒ 落进生产的是**结论**（排序口径 + 先验入行 + 「先强资弱」矛盾提示），
不是这个工具。**CLI 入口已于 2026-11-04 下线**（`--mode rerank` 已移除），
本模块保留下来只为让 Phase 2 的结论**可复现**（脚本仍在用它组装素材与解析输出）。

## 模块内容（都是纯函数，零 I/O）

- `build_pool_materials` / `build_evidence_items` / `render_pool_table`：
  历史日池子 → 模型素材（与当时的 CLI 同款；证据行含类别先验、DB 实测、分数段说明）。
- `SYSTEM_PROMPT_RERANK` / `build_rerank_messages`：当时的提示词（**含「不给分数」与
  「排序依据」的明确口径**——这两条今天仍然是生产口径）。
- `parse_ranking`：listwise 输出的**覆盖度校验器**（漏票/重复/编池外票/代码笔误→
  名称回退）。这套解析经验今天仍然有用（任何 listwise 输出模式都需要它）。
- `deterministic_order`：**证据支持的确定性排序键**（先验 → 主力净占比 → 系统概率），
  供回放脚本与生产口径对照。

## 两条硬边界（沿用原纪律）

1. **不落库、不进系统输出**：本模块只组装素材与解析文本，不写 `recommendations`、
   不进终端主循环、不进飞书、不进回测。
2. **素材是唯一事实源**：模型只看得到 `build_pool_materials` 产出的行，数字全部来自
   DB / config 单源 / `nextday_prob`，没有自由输入；输出经**幻觉巡检**
   （`material.unverified_numbers`）+ **覆盖度检查**（`parse_ranking`）两道确定性闸。

类别中文说明（`CAT_GLOSS`）**只是文案助读**，不是口径定义 —— 口径以先验数字为准。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from scanner.ai_digest.material import NA, MaterialItem
from scanner.config import CATEGORY_HIT_RATE_DEFAULT

__all__ = [
    "CAT_GLOSS",
    "PoolMaterials",
    "RankingCheck",
    "SYSTEM_PROMPT_RERANK",
    "build_evidence_items",
    "build_pool_materials",
    "build_rerank_messages",
    "parse_ranking",
    "render_pool_block",
    "render_pool_items",
    "render_pool_table",
]

#: 类别中文助读（**文案，不是口径**）。口径以 `CATEGORY_HIT_RATE` 的数字为准。
CAT_GLOSS: dict[str, str] = {
    "rebound": "回调反弹",
    "known_new_face": "老面孔新迹象",
    "momentum": "动量延续",
    "new_face": "新面孔",
    "short_term": "短线启动",
    "core_dip": "核心低吸",
}

#: 一行一只票的字段表：`(素材键, 中文标签, 格式)`。
_POOL_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("name", "名称", "text"),
    ("category", "类别", "cat"),
    ("prior", "类别先验命中率", "pct1"),
    ("p", "系统校准次日概率", "pct2"),
    ("percent", "当前涨幅", "pct"),
    ("accum", "5日累计", "pct"),
    ("flow", "主力净占比", "pct"),
    ("rank", "飙升榜排名", "int"),
    ("sector", "板块", "text"),
    ("gate", "系统门判定", "text"),
)

#: 素材键 → 表格列名（表头与 `render_pool_table` 共用同一份，不写第二遍）。
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("name", "名称"),
    ("category", "类别"),
    ("prior", "先验"),
    ("p", "系统概率"),
    ("percent", "涨幅"),
    ("accum", "5日累计"),
    ("flow", "主力净占比"),
    ("rank", "榜位"),
    ("sector", "板块"),
    ("gate", "系统门"),
)


# ── 格式化 ────────────────────────────────────────────────────────────────────


def _num(v: object) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        f = float(v)
        return f if f == f and f not in (float("inf"), float("-inf")) else None
    if isinstance(v, str) and v.strip():
        try:
            f = float(v)
        except ValueError:
            return None
        return f if f == f and f not in (float("inf"), float("-inf")) else None
    return None


def _fmt(kind: str, v: object) -> str:
    if kind == "text":
        if isinstance(v, str) and v.strip():
            return v.strip()
        return NA
    if kind == "cat":
        s = v.strip() if isinstance(v, str) else ""
        if not s:
            return NA
        gloss = CAT_GLOSS.get(s)
        return f"{s}（{gloss}）" if gloss else s
    if kind == "bool":
        return "是" if v is True else ("否" if v is False else NA)
    n = _num(v)
    if n is None:
        return NA
    if kind == "pct":
        return f"{n:+.2f}%"
    if kind == "pct1":
        return f"{n * 100:.1f}%"
    if kind == "pct2":
        return f"{n * 100:.2f}%"
    if kind == "int":
        return f"{int(n)}"
    return f"{n}"


# ── 素材 ───────────────────────────────────────────────────────────────────────


def _measured_pair(cat: str, v: object) -> tuple[str, int, float]:
    """`(n, 命中率%)` → `(类别, n, 命中率)`（越界/形状不对一律降级为 0，不抛）。"""
    if isinstance(v, (tuple, list)) and len(v) >= 2:
        n = _num(v[0])
        hit = _num(v[1])
        return cat, int(n) if n is not None else 0, hit if hit is not None else 0.0
    return cat, 0, 0.0


def _bucket_triple(v: object) -> tuple[str, int, float]:
    """`(桶名, n, 命中率%)` → 同形状（越界/形状不对一律降级为 0，不抛）。"""
    if isinstance(v, (tuple, list)) and len(v) >= 3:
        name = str(v[0])
        n = _num(v[1])
        hit = _num(v[2])
        return name, int(n) if n is not None else 0, hit if hit is not None else 0.0
    return "", 0, 0.0


@dataclass(frozen=True)
class PoolMaterials:
    """一次精筛的素材。`items` 里既有全局证据行（`ev.` 前缀）也有票行（`st.` 前缀）。"""

    date: str
    items: tuple[MaterialItem, ...]
    symbols: tuple[str, ...]
    missing: tuple[str, ...]


def build_evidence_items(stats: Mapping[str, object]) -> tuple[MaterialItem, ...]:
    """全局证据行（DB 实测 + config 单源先验）—— 模型的排序依据都在这里。

    `stats` 结构见 `source.load_pool_label_stats` 与 `source.load_board_snapshot`。
    证据行只给**数字**，不给结论：先验/实测是历史命中估计，不是收益承诺。
    """
    items: list[MaterialItem] = []

    prior = stats.get("prior")
    measured = stats.get("measured")
    score_buckets = stats.get("score_buckets")
    board = stats.get("board")

    # ① 类别先验（唯一手抄源 = config_scoring.CATEGORY_HIT_RATE）
    if isinstance(prior, Mapping) and prior:
        seg = "、".join(
            f"{cat} {float(hit) * 100:.1f}%" for cat, hit in sorted(prior.items(), key=lambda kv: -float(kv[1]))
        )
        items.append(
            MaterialItem(
                id="ev.prior",
                label="类别先验命中率（系统唯一手抄源 · 排序依据；口径=次日涨幅≥7%）",
                value=f"{seg}；全体基线 {CATEGORY_HIT_RATE_DEFAULT * 100:.1f}%",
            )
        )

    # ② 本项目 DB 实测（逐行口径，与先验的去重口径不同 ⇒ 数值不等，只看 n）
    if isinstance(measured, Mapping) and measured:
        seg = "、".join(
            f"{cat} {hit:.1f}%（n={n}）" for cat, n, hit in (_measured_pair(cat, v) for cat, v in measured.items())
        )
        n_total = int(_num(stats.get("label_n")) or 0)
        items.append(
            MaterialItem(
                id="ev.measured",
                label="本项目库内逐行实测命中率（口径：命中=次日涨幅≥7%；与先验的去重口径不同，数值不可直接比；只用来看样本量 n）",
                value=f"{seg}；合计 {n_total} 条带标签样本",
            )
        )

    # ③ 分数为什么不给（实测证据，不是断言）
    if isinstance(score_buckets, Sequence) and score_buckets:
        seg = "、".join(f"{name} {hit:.1f}%（n={n}）" for name, n, hit in (_bucket_triple(v) for v in score_buckets))
        items.append(
            MaterialItem(
                id="ev.score",
                label="分数段实测（逐行口径；说明为什么本表不给分数）",
                value=f"{seg} ⇒ 分数与命中无单调关系，故排序不看分数",
            )
        )

    # ④ 飙升榜环境（榜位数据的时刻，避免两个时刻的数被当同一刻）
    if isinstance(board, Mapping) and board:
        parts = [f"榜单时刻 {board.get('time') or NA}"]
        if board.get("total") is not None:
            parts.append(f"共 {board['total']} 只")
        if board.get("up") is not None and board.get("down") is not None:
            parts.append(f"上涨 {board['up']} / 下跌 {board['down']}")
        med = _num(board.get("median_pct"))
        if med is not None:
            parts.append(f"中位涨幅 {med:+.2f}%")
        items.append(MaterialItem(id="ev.board", label="飙升榜环境", value="；".join(parts)))

    # ⑤ 目标口径（模型排序的「赢」是什么）
    items.append(
        MaterialItem(
            id="ev.target",
            label="排序目标口径",
            value="次日涨幅≥7%（系统唯一口径）。先验与系统概率都是这个口径下的历史命中估计，不是收益承诺。",
        )
    )
    return tuple(items)


#: **确定性排序键**（`--order deterministic`，默认）：先验 → 主力净占比 → 系统概率。
#:
#: 依据（2026-11-04 Phase 2 回放，30 个历史日重建池，逐日配对 bootstrap）：
#: - 「先验」是系统唯一口径的**类别**命中率（绝对水平），决定哪一类更值得看；
#: - 「主力净占比」是实测**类内区分力最强**的键（Phase 1 类内 IC +0.092，全键最高）；
#: - 「系统概率」作第三键（它已含先验，且在池内 **86% 的行上并列** ⇒ 只能当破并列的末位键）。
#:
#: ⚠ 不要改成「系统概率优先」：并列会让它在并列里任意排 ⇒ IC ≈ 0（实测 +0.0165、中位 −0.0485）。
#: ⚠ 「纯主力净占比」单键的实测 IC 更高（+0.1160），但那**只说明相对强弱**，
#:   不说明能达到「次日≥7%」（Phase 1 里 flow 的 top-3 命中率反而最差）⇒ 不作主键。
DETERMINISTIC_ORDER_KEYS: tuple[str, ...] = ("prior", "flow", "p")


def deterministic_order(rows: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    """按确定性键排序（先验 → 主力净占比 → 系统概率），缺失值排最后。

    纯函数、确定性（同样输入 → 同样输出）。返回**新列表**，不改原列表。
    """

    def _key(row: Mapping[str, object]) -> tuple[float, float, float, str]:
        def _num_or_min(v: object, floor: float = -1e9) -> float:
            n = _num(v)
            return n if n is not None else floor

        return (
            _num_or_min(row.get("prior")),
            _num_or_min(row.get("flow")),
            _num_or_min(row.get("p")),
            str(row.get("symbol") or ""),
        )

    return sorted(rows, key=_key, reverse=True)


def build_pool_materials(
    date: str,
    rows: Sequence[Mapping[str, object]],
    *,
    evidence: Sequence[MaterialItem] = (),
) -> PoolMaterials:
    """池行 → 素材（纯函数：同输入 → 字节级同输出，零 I/O、零时钟）。

    `rows` 的键见 `_POOL_FIELDS`。缺字段一律写「暂无数据」并登记进 `missing`
    （模型不得补）。行的顺序 = 终端 v1 池选顺序（调用方给什么就是什么，不在本模块重排）。
    """
    items: list[MaterialItem] = list(evidence)
    missing: list[str] = []
    symbols: list[str] = []

    for row in rows:
        symbol = str(row.get("symbol") or "").strip()
        if not symbol:
            continue
        symbols.append(symbol)
        for key, label, kind in _POOL_FIELDS:
            value = _fmt(kind, row.get(key))
            if value == NA:
                missing.append(f"{symbol}.{key}")
            items.append(MaterialItem(id=f"st.{symbol}.{key}", label=f"{symbol}·{label}", value=value))

    return PoolMaterials(date=date, items=tuple(items), symbols=tuple(symbols), missing=tuple(missing))


# ── 渲染 ───────────────────────────────────────────────────────────────────────


def render_pool_items(m: PoolMaterials) -> str:
    """素材的逐项原样视图（`--materials`）：与发给模型的内容同源，可逐行核对。"""
    return "\n".join(f"  {i.id} | {i.label} | {i.value}" for i in m.items)


def render_pool_table(m: PoolMaterials) -> str:
    """**模型实际看到的正文**：证据行 + 一行一只票的紧凑表。

    为什么用表格而不是逐项 `id|label|value`：listwise 重排要模型**横向比较**一整张表，
    逐项列表会把它读成流水账（llm_trade 的实测教训：喂极简快照，别喂全量数据）。
    但素材的逐项视图仍保留（`render_pool_items`）供人核对 —— 表格每格都能对回素材。
    """
    ev = [i for i in m.items if i.id.startswith("ev.")]
    per_symbol: dict[str, dict[str, str]] = {}
    for item in m.items:
        if not item.id.startswith("st."):
            continue
        _, symbol, key = item.id.split(".", 2)
        per_symbol.setdefault(symbol, {})[key] = item.value

    lines: list[str] = []
    if ev:
        lines.append("【全池证据】")
        for i in ev:
            lines.append(f"- {i.label}：{i.value}")
        lines.append("")
    header = " | ".join(["当前序", "代码", *[c[1] for c in _COLUMNS]])
    lines.append(f"【候选表】（共 {len(m.symbols)} 只，顺序 = 系统当前顺序）")
    lines.append(header)
    for idx, symbol in enumerate(m.symbols, start=1):
        cells = per_symbol.get(symbol, {})
        row = [str(idx), symbol, *[cells.get(key, NA) for key, _label in _COLUMNS]]
        lines.append(" | ".join(row))
    return "\n".join(lines)


def render_pool_block(m: PoolMaterials) -> str:
    """素材的**人可核验**视图：一行一只票（与终端同序），读者可逐格对照。"""
    per_symbol: dict[str, dict[str, str]] = {}
    for item in m.items:
        if not item.id.startswith("st."):
            continue
        _, symbol, key = item.id.split(".", 2)
        per_symbol.setdefault(symbol, {})[key] = item.value
    lines: list[str] = []
    for symbol in m.symbols:
        cells = per_symbol.get(symbol, {})
        parts = [
            cells.get("name", NA),
            cells.get("category", NA),
            f"先验{cells.get('prior', NA)}",
            f"系统概率{cells.get('p', NA)}",
            f"涨幅{cells.get('percent', NA)}",
            f"5日{cells.get('accum', NA)}",
            f"主力{cells.get('flow', NA)}",
            f"榜位{cells.get('rank', NA)}",
            cells.get("gate", NA),
        ]
        lines.append(f"  {symbol} " + " | ".join(parts))
    return "\n".join(lines)


# ── 提示词 ─────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT_RERANK = "\n".join(
    [
        "你是股票扫描器的「池内优先级排序器」。用户给你一张表：今日 v1 池选里的候选票。",
        "你的任务：给出**完整排序**（第 1 到第 N，每只票都要出现且只出现一次），",
        "并给每只票一句理由 —— 理由必须引用该行出现过的字段或数字。",
        "",
        "【排序依据（按优先级）】",
        "① 「系统概率」列：系统按历史标签校准出来的「次日涨幅≥7%」概率估计（参考值，非承诺）。",
        "② 「先验」列：该类别在本项目历史标签上的次日≥7% 命中率（系统唯一手抄源，见证据行）。",
        "③ 「主力净占比」「榜位」「板块」「5日累计」：用于同档位内排序，以及解释理由。",
        "⚠ 排序**不要用分数** —— 证据行里有实测：分数段与命中率无单调关系，本表因此不给分数。",
        "⚠ 「先验」行是排序依据；「实测」行是逐行口径的库内统计（与先验的去重口径不同，数值不等），",
        "   **只用它看样本量 n**，不许用它的数值改写先验。",
        "⚠ 「系统概率」是**参考量**：因子之间相关会系统性高估，别把它当成胜率承诺，",
        "   理由里写「系统概率 21.3%」这类事实即可，不要写成「胜率 79%」这种换算。",
        "",
        "【硬性规则】",
        "1. 池里每只票都必须出现在排序里，一只不落；不许出现表里没有的票。",
        "2. 只许使用表里与证据行里出现过的数值，一个数字都不许新增、估算、换算或求平均 ——",
        "   事后有机器核对，编了必被标出。",
        "3. 表里写「暂无数据」的项就当它缺失，不许猜、不许用常识补齐。",
        "4. 证据不足以区分时直说「这几只无法区分」，不要硬排；但排序仍要完整给出。",
        "5. 必须写风险：落在差先验桶 / 主力净流出 / 5日累计已高 / 系统概率明显低的票，",
        "   理由里要点明，不许只报喜。",
        "6. 禁止目标价、止损位、仓位建议（表撑不起这些数字，给了就是编造）。",
        "7. 禁止涨跌预测：不许出现「必涨」「大概率」「稳赚」「冲高」「会跌」这类词。",
        "8. 输出格式固定：每行一条，格式为「序号|代码|一句理由」，从第 1 条排到第 N 条；",
        "   理由里不要换行。最后可以加一行「总判断|……」，那一行不要写股票代码。",
        "9. 不要用 Markdown：不要列表符号、不要加粗、不要表格竖线以外的排版。",
    ]
)


def build_rerank_messages(m: PoolMaterials) -> list[dict[str, str]]:
    """素材 → 提示词消息（纯函数，便于测试断言「模型只看得到素材」）。"""
    user = "\n".join(
        [
            f"信号日：{m.date}",
            f"候选池共 {len(m.symbols)} 只。",
            "",
            render_pool_table(m),
        ]
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT_RERANK},
        {"role": "user", "content": user},
    ]


# ── 覆盖度校验（listwise 专属闸）────────────────────────────────────────────────

#: 排序行首：允许「第 3 名」「3.」「3、」「3|」「3)」等写法。
_RANK_LINE_RE = re.compile(r"^\s*(?:第\s*)?(\d{1,3})\s*(?:名|位|[.、,，|)）:：])")
#: 6 位代码（可选交易所前缀）。前后不许再接数字，避免把 20261104 之类的串切开。
_CODE_RE = re.compile(r"(?<!\d)((?:SZ|SH|BJ)?)(\d{6})(?!\d)", re.I)


@dataclass(frozen=True)
class RankingCheck:
    """覆盖度校验结果。`ok` = 一只不漏、一只不重、没编票。"""

    ordered: tuple[str, ...]
    missing: tuple[str, ...]
    unknown: tuple[str, ...]
    duplicates: tuple[str, ...]
    typos: tuple[str, ...]
    loose: bool

    @property
    def ok(self) -> bool:
        return not self.missing and not self.unknown and not self.duplicates


def _resolve(token: str, by_digits: Mapping[str, str]) -> str | None:
    """`300319` / `SZ300319` → 池内代码（裸码按数字部分匹配，容忍模型省前缀）。"""
    t = token.upper()
    if t in by_digits:
        return by_digits[t]
    return by_digits.get(t[2:] if len(t) == 8 else t)


def parse_ranking(text: str, symbols: Sequence[str], names: Mapping[str, str] | None = None) -> RankingCheck:
    """解析模型的排序输出 → 覆盖度校验（确定性，无模型参与）。

    `names`（可选）：`{代码: 名称}`。代码列认不出时**按名称回退匹配** ——
    实测踩到：模型把 `SZ301005` 写成 `300005`（代码列笔误），按代码匹配会把它判成
    「漏排」（误报，exit 5），而同一行里写着正确的名称「超捷股份」⇒ 按名称能认出来。
    名称回退只在**代码列认不出**时启用，不改变代码优先的顺序。

    判定规则（刻意保守，避免把格式差异误判成漏票）：
    - **行首带序号**的行才算一条排序（`第3名` / `3.` / `3、` / `3|` 都认）；
    - 每条的票 = **代码列**（`序号|代码|理由` 的第 2 段）解析出的池内票；代码列认不出时
      回退到整行里第一只池内票（模型没用竖线分隔也能解析）；
    - 代码列里的池外码：同句里出现池内票 ⇒ 判为**笔误**（软告警，不算编票）；
      同句里一只池内票都没有 ⇒ 判为**编池外票**（硬告警）；
    - 池里没被排到的 ⇒ `missing`；同一只票占了两条排序行 ⇒ `duplicates`。
    - **兜底**：一条序号行都没解析出来时，退化为「全文首次出现顺序」并置 `loose=True`
      （格式不合规不算错，但要在输出里如实标出「格式不规范」）。

    ⚠ 本模块只排**今日池**（CLI 拿不到历史日的视图快照）。要检验「排序质量」需另写回放
    脚本（参照 `scripts/shadow_feature_backtest.py` 的做法：用历史日标签算命中率）。
    """
    pool = list(dict.fromkeys(symbols))
    by_digits: dict[str, str] = {}
    for s in pool:
        by_digits[s] = s
        by_digits[s[2:] if len(s) == 8 else s] = s
    # 名称 → 代码（长名优先，避免「中国」这类前缀名先命中）。
    by_name: list[tuple[str, str]] = []
    if names:
        for sym, nm in names.items():
            if sym in pool and nm:
                by_name.append((str(nm), sym))
        by_name.sort(key=lambda kv: -len(kv[0]))

    ordered: list[str] = []
    seen: set[str] = set()
    unknown: list[str] = []
    duplicates: list[str] = []
    typos: list[str] = []

    def _resolve_digits(digits: str) -> str | None:
        return by_digits.get(digits.upper()) or by_digits.get(digits)

    def _resolve_by_name(line: str) -> str | None:
        """行内按**名称**回退匹配（代码列认不出时用）。"""
        for nm, sym in by_name:
            if nm in line:
                return sym
        return None

    def _scan(source: str, *, strict: bool) -> None:
        """strict=True：只认行首带序号的行，每行取**代码列**的票。

        strict=False（宽松兜底）：不要求序号，每行取**全部**池内票（按出现顺序）——
        模型写成一段话时，整段里出现的票就是它的排序。
        """
        for line in (source or "").splitlines():
            if strict and not _RANK_LINE_RE.match(line):
                continue
            line_tokens = [d for _p, d in _CODE_RE.findall(line)]
            if not line_tokens:
                continue
            hits: list[str] = []
            unresolved: list[str] = []
            for digits in line_tokens:
                r = _resolve_digits(digits)
                if r is None:
                    unresolved.append(digits)
                else:
                    hits.append(r)

            entry: str | None
            if strict:
                # 代码列（`|代码|` 的第 2 段）就是这一行的身份 —— 理由里提到的别的票
                # 不该抢走这一行（实测踩过：模型代码列写错、理由里引用了上一只）。
                parts = [p.strip() for p in line.split("|")]
                field_tokens = [d for _p, d in _CODE_RE.findall(parts[1])] if len(parts) >= 2 else []
                field_hit = next((r for r in (_resolve_digits(d) for d in field_tokens) if r), None)
                if field_hit is not None and field_hit in seen:
                    # 代码列指向一只**已排过**的票 —— 实测踩到：解释模式下模型会写
                    # 「3|SZ301536之后的利亚德|…」（代码写的是上一只，名字才是本只）
                    # ⇒ 改用名称回退（且必须指向一只**未排过**的票）。
                    alt = _resolve_by_name(line)
                    if alt is not None and alt not in seen:
                        field_hit = alt
                if field_hit is None and field_tokens:
                    # 代码列认不出 ⇒ 先试**名称回退**（模型写了名字但代码打错的情况）。
                    field_hit = _resolve_by_name(line)
                if field_hit is not None:
                    entry = field_hit
                elif field_tokens:
                    # 代码列有码但认不出 ⇒ 这一行的身份不明，不计入排序。
                    # 同句提到了池内票 ⇒ 判为**笔误**（软告警）；一只都没提 ⇒ 编的池外票。
                    if hits:
                        for d in field_tokens:
                            if d not in typos:
                                typos.append(d)
                    else:
                        for d in field_tokens:
                            if d not in unknown:
                                unknown.append(d)
                    continue
                else:
                    # 没竖线分隔（如「1. SZ300489 先验最高」）⇒ 回退到整行第一只池内票
                    if hits:
                        entry = hits[0]
                    else:
                        for d in unresolved:
                            if d not in unknown:
                                unknown.append(d)
                        continue
            else:
                # 宽松兜底：整行里的池内票按出现顺序全收（散文式输出）。
                if not hits:
                    for d in unresolved:
                        if d not in unknown:
                            unknown.append(d)
                    continue
                for sym in hits:
                    if sym in seen:
                        continue
                    seen.add(sym)
                    ordered.append(sym)
                continue

            if entry in seen:
                # 宽松模式下重复提及是散文常态（总结句常重复第一只），不算「重复排名」。
                if strict and entry not in duplicates:
                    duplicates.append(entry)
                continue
            seen.add(entry)
            ordered.append(entry)

    _scan(text or "", strict=True)
    loose = not ordered
    if loose:
        ordered = []
        seen = set()
        _scan(text or "", strict=False)

    missing = [s for s in pool if s not in seen]
    return RankingCheck(
        ordered=tuple(ordered),
        missing=tuple(missing),
        unknown=tuple(unknown),
        duplicates=tuple(duplicates),
        typos=tuple(typos),
        loose=loose,
    )
