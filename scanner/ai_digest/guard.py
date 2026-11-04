"""榜外异动 AI 转述的**红线守卫**（Python 侧，2026-11-03）。

这是 worker 侧 `explain/guard.ts` 的移植 + 一批**整段转述专属**的追加槽位。

## 为什么整段转述要额外加槽位

单票转述里「比较」没有合法的使用场景；而**一次转述 8 只票**时，模型最容易越界的方式
恰好是比较与取舍：「其中 X 更值得关注」「相比之下 Y 更强」「先看这两只」。这类话不带
任何买卖动词、不给目标价，`ACTION_SLOTS` 一条都拦不住 —— 但它已经是一个结论了。
ADR 0001 修订 1 把「隐含建议」诚实登记为词表挡不住的一类，本文件就是把这类里**可枚举
的那一半**（比较/取优/排序句式）补进槽位。

## 归一化 + 槽位（不做逐字正则）

`guard.ts` 的教训是实测得来的：逐字正则对**平凡变体完全不设防** —— 一个全角冒号
（`建议：买入`）、一个词内空格（`建 议买入`）、一句英文（`buy zone 17.8`）就能整体绕过
（当时 8/8 逃逸）。故这里同样先归一化再匹配：

1. 归一化：NFKC（全角→半角）、大写→小写、去全部空白（含全角空格）、去常见分隔符；
2. 槽位匹配：动作槽位 / 预测槽位 / **比较取优槽位**，按共现规则判定。

## 残余风险（诚实标注，不假装全覆盖）

能挡**已知的平凡变体**与已枚举的比较句式；挡不住槽位表外的说法（「拿住这个位置」
「有肉可吃」）、拼音/方言、以及不比较不排序但强烈暗示倾向的句子。故三层各司其职：
① 素材约束（模型只看得到确定性素材，无自由输入 —— 也是最强的一道）；
② 本词表（最后一道确定性闸）；③ 人可核验（素材逐行对照打印，读者能发现文案与数据不符）。

⚠ 与 `worker/src/explain/guard.ts` 是**两份实现**。改一侧必须同改另一侧：
`tests/test_ai_digest.py::test_ts_slot_vocabulary_is_covered_by_python_guard`
会解析 TS 侧的槽位字面量并逐条要求在 Python 侧存在，只改一侧即红。
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["REFUSAL", "is_redlined", "is_rerank_redlined", "normalize"]

_WS_RE = re.compile(r"[\s\u3000]+")
# 常见分隔符：让「建议：买入」与「建议买入」收敛成同一个串。
_PUNCT_RE = re.compile(r"[，,。.、;；:：!！?？'\"“”‘’()（）\[\]【】<>《》|]")

# ── 词表（**与 explain/guard.ts 同源的那部分**）────────────────────────────────

# 动作槽位：买卖 / 仓位 / 价格 / 止损止盈。命中任一即高危词。
ACTION_SLOTS: tuple[str, ...] = (
    "买入",
    "卖出",
    "加仓",
    "减仓",
    "建仓",
    "清仓",
    "补仓",
    "抄底",
    "追高",
    "抛售",
    "出货",
    "入场",
    "离场",
    "买点",
    "卖点",
    "介入",
    "布局",
    "持有",
    "加码",
    "目标价",
    "目标位",
    "合理价",
    "估值位",
    "价位",
    "止损",
    "止盈",
    "止损位",
    "止盈位",
    "仓位",
    "轻仓",
    "半仓",
    "重仓",
    "满仓",
    "空仓",
    "底仓",
    "buy",
    "sell",
    "long",
    "short",
    "stop",
    "targetprice",
)

# 建议类副词：与动作槽位**共现**才判红线（单独出现可能是描述）。
ADVICE_MARKERS: tuple[str, ...] = (
    "建议",
    "推荐",
    "应该",
    "可以买",
    "值得",
    "不妨",
    "务必",
    "建议立刻",
    "suggest",
    "recommend",
    "should",
)

# 预测类措辞：单独出现即拒（本项目不给预测）。
PREDICTION_SLOTS: tuple[str, ...] = (
    "大概率",
    "必涨",
    "稳赚",
    "翻倍",
    "看多",
    "看涨",
    "看跌",
    "会涨",
    "会跌",
    "将上涨",
    "将下跌",
    "预计涨",
    "预计跌",
    "冲高",
    "回调就是",
    "risk-reward",
    "upside",
    "downside",
)

# 强动作槽位：单独出现即拒（「持有」这类弱动作不在此列）。
STRONG_ACTION_SLOTS: tuple[str, ...] = (
    "买入",
    "卖出",
    "加仓",
    "减仓",
    "建仓",
    "清仓",
    "抄底",
    "追高",
    "抛售",
    "出货",
    "入场",
    "离场",
    "买点",
    "卖点",
    "介入",
    "加码",
    "目标价",
    "目标位",
    "合理价",
    "估值位",
    "价位",
    "止损",
    "止盈",
    "仓位",
    "轻仓",
    "半仓",
    "重仓",
    "满仓",
    "空仓",
    "底仓",
    "buy",
    "sell",
    "long",
    "short",
    "stop",
    "targetprice",
)

# ── 本包追加：整段转述专属（比较 / 取优 / 排序）────────────────────────────────
#
# 逐条说明为何「单独出现即拒」而不是走共现规则：这几种句式在「转述一段票」的语境里
# **没有合法用途**（转述只需要「谁在哪一层、量比多少、主占多少」），而它们的代价是
# 一句话就把 8 只票排出了优先级 —— 那正是本项目删掉的「综合判断」区块。
COMPARISON_SLOTS: tuple[str, ...] = (
    "更值得",
    "值得关注",
    "值得留意",
    "值得优先",
    "首选",
    "优于",
    "强于",
    "弱于",
    "好于",
    "差于",
    "不如",
    "相比之下",
    "相比",
    "排序",
    "排在前",
    "靠前",
    "优先看",
    "优先关注",
    "重点关注",
    "最值得",
    "更有吸引力",
    "确定性最高",
    "确定性较高",
)

# ── 拒答文案（用户可见；不含任何建议内容）──────────────────────────────────────

REFUSAL = (
    "该转述触及红线（买入/卖出、目标价、仓位、止损、涨跌预测，或对多只票做比较/排序/取舍），"
    "已按纪律整段拦下，不对外输出。本系统只转述数据，不构成任何投资建议。"
)


def normalize(text: str) -> str:
    """去空白 + 全角转半角 + 大写转小写 + 去常见分隔符（让同一意思的不同写法收敛）。"""
    s = unicodedata.normalize("NFKC", text).lower()
    s = _WS_RE.sub("", s)
    return _PUNCT_RE.sub("", s)


def is_redlined(text: str) -> bool:
    """是否触红线。判定顺序：归一化 → 预测 → 比较取优 → 动作（共现 / 强动作）。"""
    s = normalize(text)
    if not s:
        return False

    if any(p in s for p in PREDICTION_SLOTS):
        return True
    if any(p in s for p in COMPARISON_SLOTS):
        return True

    if not any(p in s for p in ACTION_SLOTS):
        return False
    if any(p in s for p in ADVICE_MARKERS):
        return True
    return any(p in s for p in STRONG_ACTION_SLOTS)


#: 从句切分（**最细粒度**：。；，、！？换行）—— 否定豁免的最小判定单位。
_CLAUSE_SPLIT_RE = re.compile(r"[。；;，,、！!？?\n\r]+")

#: 正式免责类否定词（出现即整句豁免）。
_FORMAL_NEGATIONS: tuple[str, ...] = (
    "不构成",
    "不是",
    "并非",
    "不作为",
    "不提供",
    "不代表",
    "不含",
    "不意味",
    "不做",
    "不预测",
    "不谈",
)

#: 指令类否定词（出现即整句豁免）。
_INSTRUCTION_NEGATIONS: tuple[str, ...] = (
    "不建议",
    "不要",
    "别",
    "勿",
    "不能",
    "不可",
    "不得",
    "无需",
    "无须",
    "不用",
    "不宜",
    "避免",
)


#: 英文动作槽位：**按词边界**匹配（归一化会删空格，词边界只能在原文上判）。
#: ⚠ 不做词边界就会误伤类别键：池里有 `short_term` 类别，英文 `short`（做空）会把它
#:   当成买卖动作 —— 实测真踩到了（2026-11-04：整份好输出被 `short_term` 里的 short 拦成拒答）。
#:   `\bshort\b` 不匹配 `short_term`（下划线是词字符）✓，但仍能拦住 `short at 16.9` ✓。
_ASCII_ACTION_SLOTS: tuple[str, ...] = ("buy", "sell", "long", "short", "stop", "targetprice")
_ASCII_ACTION_RES: tuple[re.Pattern[str], ...] = tuple(re.compile(rf"\b{s}\b", re.I) for s in _ASCII_ACTION_SLOTS)

#: 中文强动作槽位（归一化后匹配，能吃掉「建 议买入」这类带空格的写法）。
#: 只用于「动作词+风险」的名词短语识别（风险描述豁免）。
_CN_STRONG_ACTION_SLOTS: tuple[str, ...] = tuple(s for s in STRONG_ACTION_SLOTS if not s.isascii())

#: 「动作词 + 风险」= 风险**描述**（名词短语），不是指令 —— 豁免。
#: ⚠ 实测踩到（2026-11-04）：模型写「5日累计 +25.62% 已明显偏高，**追高风险**需正视」——
#:   「追高风险」是在说追高这件事的风险，不是叫用户追高，却让整份好输出被拦成拒答。
#:   同类还有「止损风险」「仓位风险」「抄底风险」—— 都是风险名，不是动作指令。
_RISK_PHRASE_RE = re.compile("(?:%s)风险" % "|".join(re.escape(s) for s in _CN_STRONG_ACTION_SLOTS))

#: **祈使型动作词**（在 rerank 输出里当动词用 = 指令）：
#: 「买入」「卖出」「加仓」「减仓」「建仓」「清仓」「补仓」「抄底」「出货」「抛售」「入场」「离场」
#: 「加码」「布局」「介入」「目标价」「止损」「止盈」「仓位」「轻仓」「半仓」「重仓」「满仓」「空仓」「底仓」
#: ⚠ **不含「追高」**：实测里「追高」几乎全是描述用法（「不追高」「无追高压力」「追高特征」
#:   「追高位阶」「追高段位风险」「追高证据劣势」）—— 把它当动词会误伤大量好输出
#:   （2026-11-04 实测：30 个测试日里 8 天的输出被它整段拦下）。
#:   「追高」若真成指令（「建议追高」），由**建议标记紧邻**规则拦。
_IMPERATIVE_ACTION_SLOTS: tuple[str, ...] = (
    "买入",
    "卖出",
    "加仓",
    "减仓",
    "建仓",
    "清仓",
    "补仓",
    "抄底",
    "出货",
    "抛售",
    "入场",
    "离场",
    "加码",
    "布局",
    "介入",
    "目标价",
    "止损",
    "止盈",
    "仓位",
    "轻仓",
    "半仓",
    "重仓",
    "满仓",
    "空仓",
    "底仓",
    "买点",
    "卖点",
)

#: **只在跟建议标记紧邻时才算指令**的词（其余一律按描述处理）。
#: 「追高」实例：实测里全是描述用法（「不追高」「追高特征」「追高位阶」…），
#: 但「建议追高」是真建议 ⇒ 单独走「建议标记紧邻」这一条。
_ADVICE_ONLY_SLOTS: tuple[str, ...] = ("追高",)

#: 建议类副词（与动作词**紧邻**才算指令；隔得远的如「止损位或仓位建议」是枚举描述）。
_ADVICE_MARKERS_NEAR: tuple[str, ...] = ("建议", "推荐", "应该", "可以", "不妨", "务必", "立刻", "立即")

#: 「动作词后 8 字内出现数字」= 报了个**具体价位/仓位**（实质建议）。
_DIGIT_AFTER_WINDOW = 8

#: 纯祈使从句（只有动作词（+数字））—— 典型如「买入。」「止损 16.9」。
_BARE_IMPERATIVE_RE = re.compile(
    r"^(?:" + "|".join(re.escape(s) for s in _IMPERATIVE_ACTION_SLOTS) + r")(?:[0-9.]+%?)?$"
)


#: 指向**未来**的标记（配合「时态模糊」预测词用）。
#: ⚠ 实测踩到（2026-11-04）：模型写「当日 +12.15% 大幅冲高」——「冲高」在这里是
#:   **描述今天已发生的事**，不是预测明天；而「必涨/看多」这类词无论时态都是断言。
_FUTURE_MARKERS: tuple[str, ...] = (
    "将",
    "会",
    "要",
    "预计",
    "预期",
    "明天",
    "明日",
    "次日",
    "后续",
    "接下来",
    "有望",
    "可能",
    "大概率",
    "恐",
    "料",
    "还能",
    "或许",
)

#: **时态模糊**的预测词：指向未来时才是预测，描述当日行情时不是。
_TENSE_AMBIGUOUS_PREDICTION_SLOTS: tuple[str, ...] = (
    "冲高",
    "回调就是",
    "upside",
    "downside",
    "risk-reward",
)


def _clause_is_negated(clause: str) -> bool:
    """从句是否含否定词（含则整句豁免红线检查）。"""
    return any(m in clause for m in _FORMAL_NEGATIONS) or any(m in clause for m in _INSTRUCTION_NEGATIONS)


def is_rerank_redlined(text: str) -> bool:
    """**池内排序模式**的红线：放行比较/排序，拦住预测与买卖动作。

    与 `is_redlined` 的唯一区别：**不查 `COMPARISON_SLOTS`** —— 「先看哪只」「排第几」
    「谁更强」正是排序模式的产出本身，用转述档的词表拦它等于把模式拦死。

    仍然拦的三类（对任何输出都成立）：
    - `PREDICTION_SLOTS`：涨跌预测（「必涨」「大概率」「冲高」…）；
    - `STRONG_ACTION_SLOTS`：买卖/仓位/目标价/止损（「买入」「加仓」「目标价」「仓位」…）；
    - `ACTION_SLOTS` 与 `ADVICE_MARKERS` 共现：「建议买入」这类组合。

    ⚠ **否定从句豁免**（2026-11-04 实测踩到）：模型爱在结尾写「以上不构成任何涨跌预测或
    仓位建议」—— 那是一句**免责声明**，却被「仓位」二字整个拦下（实测把一份好输出整段
    变成拒答）。故先按从句切分，**含否定词的从句整句跳过**再查槽位：
    - 切分粒度取最细（。；，、！？换行）—— 否则「不建议追高，但可以买入」里的
      「买入」会跟着前半句一起被豁免；
    - 否定词分「正式免责」（不构成/不是/并非/…）与「指令否定」（不建议/不要/别/勿/…），
      两类都豁免整句。
    ⚠ **按句式判指令**（2026-11-04 实测踩到第二次）：动作词在真实输出里绝大多数是
    **描述**（「不追高」「追高特征」「表内不足以支撑目标价」），当动词拦会误伤 27% 的好输出。
    故只拦：动作词后紧跟**具体数字**、建议副词**紧邻**动作词、**裸祈使句**。详见
    `_has_imperative_advice`。
    已知残余风险：整句里同时有免责与指令（如「不构成建议但可以买入」）时会被整句豁免 ——
    选这个方向是刻意的：宁可放过一句奇怪的措辞，也别把好输出整段拒答（守卫变噪声 = 没有守卫）。

    ⚠ 词表与 `guard.ts` 同源的那部分**一个字都没改**（漂移守卫 `test_ts_slot_vocabulary_is_
    covered_by_python_guard` 仍成立）；本函数只是**另一条判定路径**，不改词表。
    ⚠ 本路径与转述档的**四处行为差异**（都是实测踩出来的，不改词表）：
      ① 英文动作槽位按**词边界**匹配（`short_term` 里的 short 不算做空动作）；
      ② 含否定词的**从句**整句豁免（免责声明不误伤）；
      ③ 「动作词+风险」按**风险描述**豁免（「追高风险需正视」不是叫用户追高）；
      ④ 只拦**祈使/具体价位型**建议，描述性动作词放行。
    """
    for clause in _CLAUSE_SPLIT_RE.split(text or ""):
        if not clause.strip():
            continue
        if _clause_is_negated(clause):
            continue
        # 英文动作槽位：原文 + 词边界（`short_term` 不算 `short`）。
        if any(rx.search(clause) for rx in _ASCII_ACTION_RES):
            return True
        # 「动作词+风险」是风险描述，先剥掉再查槽位（否则「追高风险」被当指令）。
        s = normalize(_RISK_PHRASE_RE.sub("", clause))
        if not s:
            continue
        pred_hits = [p for p in PREDICTION_SLOTS if p in s]
        if pred_hits:
            # 时态模糊的词（冲高/回调就是/…）需指向**未来**才算预测；
            # 「必涨/看多/大概率」这类无论时态都是断言 ⇒ 直接拦。
            ambiguous = [p for p in pred_hits if p in _TENSE_AMBIGUOUS_PREDICTION_SLOTS]
            hard = [p for p in pred_hits if p not in _TENSE_AMBIGUOUS_PREDICTION_SLOTS]
            if hard or (ambiguous and any(m in clause for m in _FUTURE_MARKERS)):
                return True
        if _has_imperative_advice(s):
            return True
    return False


#: 仓位类词（描述仓位 ⇒ 本身就是仓位建议）。
_POSITION_SLOTS: tuple[str, ...] = ("仓位", "轻仓", "半仓", "重仓", "满仓", "空仓", "底仓")
#: 仓位建议的搭配词（「轻仓参与」「半仓操作」…）。
_POSITION_VERBS: tuple[str, ...] = ("参与", "操作", "持有", "建仓", "买入", "加仓", "减仓")


def _has_imperative_advice(s: str) -> bool:
    """归一化从句里是否含**祈使/具体价位型**的投资建议（描述用法放行）。

    为什么改成「按句式判」而不是「黑名单命中即拦」（2026-11-04 实测）：
    同一批动作词在真实模型输出里绝大多数是**描述**（「不追高」「无追高压力」「追高特征
    最明显」「表内不足以支撑目标价」「止损位或仓位建议」），当动词拦会误伤 27% 的好输出。
    故只拦三种**实质建议**：
    ① 动作词**后 8 字内出现数字** —— 报了具体价位/仓位（「目标价 18.5」「止损 16.9」）；
    ② 建议类副词**紧邻**动作词 —— 「建议买入」「可以加仓」；
    ③ 从句**只剩**动作词（可带数字）—— 裸祈使句「买入。」。
    其余描述性用法（「追高特征」「入场形态」「5日累计已高」）一律放行。
    """
    for slot in _IMPERATIVE_ACTION_SLOTS:
        pos = s.find(slot)
        while pos >= 0:
            tail = s[pos + len(slot) : pos + len(slot) + _DIGIT_AFTER_WINDOW]
            if any(ch.isdigit() for ch in tail):
                return True  # ① 具体价位/仓位
            head = s[max(0, pos - 4) : pos]
            if any(m in head for m in _ADVICE_MARKERS_NEAR):
                return True  # ② 建议紧邻动作词
            pos = s.find(slot, pos + 1)
    # ②' 只跟建议标记联动的词（「建议追高」拦；「不追高」「追高特征」放行）。
    for slot in _ADVICE_ONLY_SLOTS:
        pos = s.find(slot)
        while pos >= 0:
            head = s[max(0, pos - 4) : pos]
            if any(m in head for m in _ADVICE_MARKERS_NEAR):
                return True
            pos = s.find(slot, pos + 1)
    # ③ 裸祈使句
    if _BARE_IMPERATIVE_RE.match(s):
        return True
    # ④ **仓位型建议**：「轻仓参与」「加仓至半仓」「仓位 30%」——
    #    不带数字也构成仓位建议（动作词与仓位词相邻，或仓位词搭配「参与/操作/持有」）。
    for pos_word in _POSITION_SLOTS:
        pos = s.find(pos_word)
        while pos >= 0:
            tail = s[pos + len(pos_word) : pos + len(pos_word) + 6]
            head = s[max(0, pos - 6) : pos]
            if any(v in tail for v in _POSITION_VERBS) or any(v in head for v in _POSITION_VERBS):
                return True
            pos = s.find(pos_word, pos + 1)
    return False
