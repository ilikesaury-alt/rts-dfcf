# LLM 金融 Agent 开源调研（2026-11-11）

> 目的：为 `scanner/ai_digest/`（A 股创业板扫描器 AI 模块）提供可落地到
> **工具调用 / 提示词 / 缓存 / 输出** 四层的改进借鉴。
>
> 现状基线（本仓库）：`agent.py`（research 循环，`max_rounds=8` / `max_calls=30`）、
> `research.py`（9 个真实数据工具，`_MAX_TOOL_CHARS=3000`，`_MAX_SYMBOLS_PER_CALL=20`）、
> `guard.py`（NFKC 归一化 + 动作/预测/比较槽位）、`material.py unverified_numbers`（幻觉巡检 = 正文数字 ⊆ 工具输出）。
> 目标：**60 秒扫描 / 单次分析 < 60 秒**。
>
> 所有链接已于 2026-11-11 逐条 curl 验证返回 HTTP 200。仓库 issue/PR 号均为真实编号。

---

## 一、LLM + 金融数据分析架构（多源交叉验证 / 延迟成本 / 防幻觉）

### 1.1 TradingAgents（TauricResearch）—— 多智能体「分析师并行 → 多空辩论 → 风控 → PM」流水线

- 仓库：https://github.com/TauricResearch/TradingAgents ｜ 文档站：https://tauricresearch.github.io/TradingAgents/
- 论文：https://arxiv.org/abs/2412.20138
- 中文落地拆解（含 A 股数据源对比）：https://tradings.cn/index.html

**它做了什么**
- 角色流水线：4 名分析师（基本面/情绪/新闻/技术）→ 多空研究员结构化辩论 → 交易员 → 风控团队（激进/稳健/中性）→ 组合经理终审。**分析师并发跑，各自用自己的工具，等所有报告到齐才进入辩论**（v0.5.2 起 "parallel analysts" —— 见文档站 News 段）。
- 双模型分层：`quick_think_llm`（分析师、研究员、辩论者、交易员）+ `deep_think_llm`（研究经理/组合经理），且**两档可用不同 provider**。CLI 环境变量 `TRADINGAGENTS_MAX_DEBATE_ROUNDS` / `TRADINGAGENTS_MAX_RISK_ROUNDS` 直接限轮数。
- 记忆与结算：`~/.tradingagents/memory/trading_memory.md` 每轮追加决策；后续 run 在分析师干活时**并行结算**已到期决策（真实收益 + 相对区域基准 alpha）并生成一段 reflection 回灌给 PM。
- **结构化输出**（v0.2.4）：Research Manager / Trader / Portfolio Manager 三个「下结论」的 agent 才用 structured output；分析师仍产出长报告。
- 缓存：`TRADINGAGENTS_CACHE_DIR` 下**按 ticker 一个 SQLite**（`checkpoints/<TICKER>.db`）做图状态 checkpoint。

**对我们可借鉴（四层映射）**
- **工具调用**：把 9 个工具在提示词里显式分组到「必须查」「按需查」两档（它把分析师并行 + 每个 agent 只拿自己的工具做到了极致）；我们 <60s 跑不了多 agent，但**可以一次 assistant turn 并发 3~4 个工具调用**（见 2.2）。
- **提示词**：学它的「按结论强度分层」——只有最终裁决才要求结构化输出，中间环节允许散文，避免每步都付 schema 约束的 token。
- **缓存**：学它 **per-symbol 分片 + 落盘**。我们用 SQLite，天然可以建 `ai_tool_cache(symbol, tool, trade_date, payload, fetched_at)`，跨 run 复用。
- **输出**：学它的「结论型 agent 走 structured output」——我们的 research 最终输出应落到带 `symbol/证据强弱/风险` 的固定 schema，而不是自由散文（当前 `RESEARCH_SYSTEM_PROMPT` 是自由文本 + 禁 Markdown 标题）。

### 1.2 TradingAgents 自带的反面教材（最值得读的一手证据）

这些 issue/PR 是**同一个项目踩过的坑**，比任何二手总结都具体：

- **#750 「Zero LLM API prompt cache hit rate」**：https://github.com/TauricResearch/TradingAgents/issues/750
  - 实测：单次 pipeline **16–22 次 LLM 调用，prompt cache 命中率 ≈ 0%**，在带缓存折扣的 provider 上多付 **~30–40%** 无谓开销。
  - 根因逐条列出：① 全代码库零缓存实现；② `current_date` / `instrument_context` / `tool_names` 被 `prompt.partial()` **注入到 system message**，前缀从第一个 token 就分叉（OpenAI 自动缓存要求 ≥1024 token 的**完全相同前缀**）；③ 每个分析节点后 `create_msg_delete()` **清空消息历史**；④ 辩论 agent 用 f-string 拼动态报告。
  - → **直接可抄：把易变值（日期、股票名、工具清单）从 system prompt 挪到 user message 尾部，让 system prompt 字节稳定。**
- **PR #1281 「Make debate and analyst prompts cache-friendly — measured 4.3x hit rate」**：https://github.com/TauricResearch/TradingAgents/pull/1281
  - 只做**顺序重排、不改一个字的指令**：共享上下文放最前 → 追加式辩论历史 → 角色专属指令放最后。实测命中率从个位数升到 28%+（文中给了一张 3 组对照表）。
  - → **可抄：我们的 `SYSTEM_PROMPT_ANALYZE` / `RESEARCH_SYSTEM_PROMPT` 把「工具清单 + 素材」放前，「本轮日期/票名」放最后。**
- **#1665 「Optional pre-screen to choose which tickers get the full multi-agent run」**：https://github.com/TauricResearch/TradingAgents/issues/1665
  - 提出者原话：全量跑一个 universe 太贵，需要**便宜的确定性筛选**先决定「哪几只值得全量分析」；并诚实标注「在我的 266 个周度 checkpoint、~6900 观测里，这个分数的 rank correlation 接近 0 —— 只当成本/triage 用，不当收益来源」。
  - → **这就是我们的两阶段（先粗筛再精查）的现成论据，且它明确警告：粗筛只省成本，不要吹它对收益有 alpha。**
- **#1625 「request (329068 tokens)」**：https://github.com/TauricResearch/TradingAgents/issues/1625 —— 打开某个新闻源后单请求冲到 **32.9 万 token**，超出本地模型 256k 上限，直接 400 报错。**工具输出无上限 = 必然炸上下文。** 我们的 `_MAX_TOOL_CHARS=3000` 正是对的方向，但要确认「截断要明说」这条在每一路都生效。
- **PR #1404 「debate gate: early stop when the bull/bear case is decision-ready」**：https://github.com/TauricResearch/TradingAgents/pull/1404 —— 用一个**廉价判定模型**（Jev）在每轮辩论后问一句「证据够判了吗」，够了就提前结束，省下一轮 2 次 LLM 调用。降级策略写得很清楚：没有 key → 不构造 gate；调用失败 → 返回 None → 走固定轮数。**这套「带优雅降级的提前停止」可以直接搬到我们的 research 循环。**
- **PR #320 「Physical URL Verification to Fact Checker」**：https://github.com/TauricResearch/TradingAgents/pull/320 —— 现有的 fact checker 只做**文本比对**，不验证引用的 URL 是否真实可达，于是产生「引用一个不存在的链接」这类幻觉；该 PR 加了一层真实 HTTP 校验，并配了反爬兜底（403 标 `VALID (Protected/403)` 而非报错）。
- **#141 「A weird data point in the report」**：https://github.com/TauricResearch/TradingAgents/issues/141 —— 报告写「SPY P/E 15.37」，真实值 27，追查发现数字来自某个**单一不靠谱数据源**。**幻觉不总来自模型，也来自上游源。**

### 1.3 ai-hedge-fund（virattt）—— 「工具层」做得最工程化的一个

- 仓库：https://github.com/virattt/ai-hedge-fund （6.4 万 star）
- v2 内核自述：https://github.com/virattt/ai-hedge-fund/blob/main/hedge_fund/README.md
- 关键源码（可直接读）：
  - 磁盘数据缓存：https://github.com/virattt/ai-hedge-fund/blob/main/hedge_fund/data/cached.py
  - Prompt 缓存：https://github.com/virattt/ai-hedge-fund/blob/main/hedge_fund/llm/cache.py
  - LLM agent 基类：https://github.com/virattt/ai-hedge-fund/blob/main/hedge_fund/signals/llm_agent.py
  - 选股器（point-in-time）：https://github.com/virattt/ai-hedge-fund/blob/main/hedge_fund/data/screener.py

**它做了什么**（全部来自上面源码，非宣传语）
- **两级缓存，职责分明：**
  - `data/cached.py`：包一层 `CachedDataClient`，把任何 `DataClient` 的响应按 JSON 落到 `~/.hedge-fund/cache/data/`。**分档 TTL**：已完成的历史行情 → 永久复用；「关于尚未结束的今天」（今日新闻、今日分钟线）→ **仅复用 600 秒**（`SAME_DAY_TTL = 600`），过期重拉；选股字段词表 → 86400 秒。**只有成功的响应才落缓存，错误继续向上抛（fail-loud）。**
  - `llm/cache.py`：**一 prompt 一文件**，key = `sha256(agent|model|system|user)`。文件注释写明它**故意同时是三样东西**：① 缓存（重跑未变快照的 backtest 成本 = $0）；② 持久化记录（每个 Signal 背后的**精确 prompt + 响应**，可回放可审计）；③ 调试轨迹（解析失败的原始响应留在盘上）。
- **失败合同（写死在 docstring 里）：** 数据层错误 **PROPAGATE**（fail loud，坏快照绝不能悄悄变中性观点）；LLM 调用/解析失败 → **ABSTAIN**（`Signal(value=0.0, metadata.abstained=True)`）。**「查不到」和「中性」是两种不同状态。**
- **缓存友好的细节**：日内 bar 的 prompt 里带的「今日至今」上下文是**分桶（bucketed）**的 —— 安静时段渲染出的 prompt 与上一根相同，因此**命中缓存、不花钱**。
- **防「模型凭记忆答题」**：`blind` 模式在回测时**扣掉 ticker、行业、日历日期、绝对金额**，市值只保留成 breakpoints 表里的百分位。
- **选股器 point-in-time**：`as_of` 只读 `filing_date <= as_of` 的最新财报，且最新报告期超过 180 天的公司直接剔除；**没有日期的筛选请求会被客户端拒绝**（因为在回测里就是 lookahead）。
- **「声明的方法不可用就换做法」**：`stock_price` 不给 `as_of` 用、不提供 volume/momentum 字段 —— 文档直接写明「已知限制」，而不是假装支持。

**对我们可借鉴（四层映射）**
- **缓存（最该抄）**：`SAME_DAY_TTL = 600` 这个常量正是我们的场景 —— **60 秒扫描里，同日盘中数据用短 TTL，收盘定稿数据用长 TTL/永久**。我们的 `daily_kline`「收盘定稿」与雪球实时 K 线正是同一条分界线，值得显式编码成一个 TTL 表。
- **工具调用**：fail-loud（数据层错误抛）+ abstain（LLM 失败给「弃权」）**双通道**。我们当前是「工具失败返回一句人话」，方向一致，但**没有「弃权」这个显式状态**——建议输出 schema 里加 `evidence: strong/medium/weak/insufficient`。
- **提示词**：`blind` 模式思路可复用 —— 我们给模型的确实是本地/实时数据，但**禁止它用「常识补齐」**这条已经从另一侧做了（SYSTEM_PROMPT_TRANSCRIBE 规则 2），可以再把「dcf/PE 这类没有的字段，模型不许自带」写进 research prompt。
- **输出**：`llm/cache.py` 把「精确 prompt + 响应 + 解析失败原文」三合一落盘 —— 我们已经有 `tool_output_blob`（幻觉巡检基准），把**最终 prompt 全文**也一起落盘，等于免费的审计与回放能力。

### 1.4 FinRobot（AI4Finance）—— 四层架构 + 财务思维链

- 仓库：https://github.com/AI4Finance-Foundation/FinRobot ｜ 论文：https://arxiv.org/abs/2405.14767
- README 实测结构（V1 equity research app）：**9 个 agent**（1 lead orchestrator + 5 个 pipeline 角色 `Data → Analysis → Modeling → Synthesis → Report` + 3 个辩论 `Bull ↔ Bear → Judge`）。
- **明确区分「模型」与「确定性计算」**：`32 pure-Python operators`（26 个估值/分析 + 6 个审计算子）、**7 providers with failover**（FMP / Finnhub / yfinance / SEC EDGAR / Adanos / NewsAggregator / FX）。
- **Skills = 56 个分析师 playbook**（股票研究/投行/PE/财富管理），按需加载，不是全塞进 prompt。
- 论文四层：① 财务 CoT（把复杂财务问题拆成逻辑序列）② 财务 LLM 算法层（按任务动态配置模型策略）③ LLMOps/DataOps 层 ④ 多源 LLM 基础模型层。

**可借鉴点**
- **工具调用**：**「算子归确定性代码，判断归模型」**——数字的换算/求和在 Python 里做完再喂给模型。**这正是我们 `_money()` 把「641890989.56」转成「6.42亿」的原因，做法一致，值得继续坚持并扩展到所有派生量。**
- **提示词**：provider failover + Skill 按需加载 → 我们的 9 个工具也可以按「本轮候选票画像」动态裁剪工具描述（比如没有公告源就直接不下发 `get_announcements`），省 schema token。
- **缓存**：「失败就换下一个 provider」的 fallback 链，可以用于雪球→东财的降级。

### 1.5 FinGPT（AI4Finance）—— 最完整的一手「数据层」论述

- 仓库：https://github.com/AI4Finance-Foundation/FinGPT ｜ RAG 模块：https://github.com/AI4Finance-Foundation/FinGPT/tree/master/fingpt/FinGPT_RAG
- 五层框架（README 原文）：① **Data source layer**（实时信息捕获，解决金融数据的时间敏感性）② **Data engineering layer**（实时 NLP 处理，直面金融数据的**高时间敏感 + 低信噪比**）③ LLMs layer（LoRA 微调以缓解金融数据高度动态）④ Task layer ⑤ Application layer。
- 成本论述很具体：训练 BloombergGPT 约 **$3M / 53 天**；**FinGPT 每次微调 < $300**（表里给了 1×RTX3090 上 FinGPT v3.3 加权 F1 与 **17.25 小时 / $17.25** 的行）。

**可借鉴点**
- 它的核心洞察「**金融数据 = 高时间敏感 + 低信噪比**」直接支持我们的分层缓存设计：**高时间敏感（盘中量比/主力净占比）短 TTL；低时间敏感（行业/概念归属、上市日期）长 TTL。**
- 微调不在我们射程内，但「**把结构化字段先工程化，再喂模型**」这条与 FinRobot 同源，是本轮调研里两个独立项目的一致结论。

### 1.6 中文 A 股同类项目（同场景，参考价值最高）

- **TradingAgents-AShare**：https://github.com/KylinMountain/TradingAgents-AShare
  - 14 个 agent，6 大分析师维度（基本面/情绪/新闻/技术/**宏观/主力资金**）→ 多空 Claim 驱动辩论 → 激进/稳健/中性三方风控 → PM 终审。
  - **Token 级流式 + 按 Round 分组的时间线**（前端可视化每个 agent 说了什么），**结构化研报按标+日期落库检索**，决策卡固定展示「方向/置信度/目标价/止损价」。
  - **定时分析复用持仓上下文，连续失败自动停用定时任务** —— 「失败要能自动熔断」这一条值得抄。SQLite + 单容器同时起 API 与调度器，与我们形态最接近。
- **PanWatch**：https://github.com/TNT-Likely/PanWatch ｜ 深度分析流程图：https://github.com/TNT-Likely/PanWatch/blob/main/docs/tradingagents-flow.en.md
  - 自述「**3–5 分钟内**给出九 agent 研报 + 完整推理链」，并**观测栈内建**：`agent_runs` 表 + `trace_id` 端到端 + **一个 LLM 调用 = 一个 `gen_ai` span**（符合 OpenTelemetry GenAI 语义约定，带 `gen_ai.system` / `request.model` / input/output token usage），**节点级成本上报**。
  - 集成手法值得抄：因为上游**没有公开的 toolkit 注入入口**，它 **monkeypatch `route_to_vendor`**，识别到 6 位 A 股代码就走 PanWatch 自己的 Provider（**复用已有缓存的 quote/kline**），否则放行 yfinance。并**只用于「取数」，不做决策**。
  - 数据快照用 `ContextVar` 而非模块级 dict 传递，原因是「深度分析跑在 asyncio worker 线程，模块级 dict 会让两只票并发时**互相覆盖数据**（报告里混入了另一只票的 K 线/新闻）」—— **并发下的数据串台，是我们一旦并行也会遇到的真实风险。**
  - `docs/tradingagents-flow.en.md` 明确写：**rating 解析失败的一律标「需人工复核」，不猜。**
- **A-Daily-TradingAgents**：https://github.com/cyecho-io/A-Daily-TradingAgents
  - 架构与我们最像（**默认 SQLite**，FastAPI）。核心是**「统一分析快照 `analysis_snapshot`」**：技术面 + 实时数据 + 市场上下文 + 机器偏向 + 风险标记**收敛到同一个结构**，评分、Prompt、AI 分析、DB 存储、前端展示**共用同一套事实来源**。
  - **双评分**（`entry_score` 买点质量 / `holding_score` 持仓延续与破位）+ `holding_state → HOLD/OBSERVE/REDUCE_ALERT/EXIT`。
  - **先机器评分、再 LLM 复核**，多 agent 输出解析成**结构化意见**后由统一决策引擎给出 `final_action / risk_level / consensus_level`。
  - → **「机器判断与 LLM 判断共用同一个快照结构」正是我们该做的：把 `research.py` 工具输出和 `material.py` 素材统一成一份 `AnalysisSnapshot`，评分/提示词/巡检/报告四处引用同一个对象。**
- **quantdash-ai-stock**：https://github.com/rancy777/quantdash-ai-stock
  - **代理层已支持：请求去重、缓存、失败冷却、并发上限、快照回退、最小请求间隔、轻量重试**（原文列表）。**Redis / 内存双模式缓存**；SQLite WAL / busy timeout / 自动事务控制。
  - **本地 stdio MCP Server 暴露结构化数据给外部 AI**，并强调「**先整理成结构化对象，再进入服务层和页面；不要把页面拼接结果当数据边界**」。
  - → 「失败冷却 + 并发上限 + 最小请求间隔」是**外呼治理三件套**，我们的雪球/东财外呼应当有同等保护（否则一次 research 循环 30 次工具调用会打爆上游）。
- **go-stock**：https://github.com/ArvinLovegood/go-stock（Go/Wails，A 股）
  - **150+ AI 数据工具 + React/PlanExecute/DeepAgents 三种 agent 模式 + 技能系统（按需加载）+ MCP 扩展**；内置「**AI 推荐个股按持有期计算实际收益并与沪深300对比，按模型/提示词/评级多维统计**」——**对 LLM 输出做按 prompt/模型的收益归因回测**，这是我们目前缺的一环。
- 另两个可备用：https://github.com/guangxiangdebizi/A-Scope-Research（MCP 协议 + 多智能体辩论，config.yaml 逐 agent 配 temperature/max_tokens）、https://github.com/WackyGem/Fleur（数据契约治理 + Dagster 回填 + 诚实的「毫秒级批量计算引擎」分层）。

---

## 二、延迟与成本优化

### 2.1 两阶段「先粗筛再精查」—— 一手证据

- **TradingAgents #1665**（上一节已引）：https://github.com/TauricResearch/TradingAgents/issues/1665
  - 这是**上游官方 issue 里被正式提出的两阶段方案**：一个小的**确定性** `screen.py` → 只吃已完成日线 → 算技术特征 + 启发式分数 → 排名 → 只对 top N 跑全量 graph。作者同时给出己方回测的**负面结论**（rank correlation ≈ 0），把它定位为 **cost control / triage，不是 alpha 来源**。
- **ai-hedge-fund `data/screener.py`**：https://github.com/virattt/ai-hedge-fund/blob/main/hedge_fund/data/screener.py
  - 把「筛选」做成**服务端一次调用**：`ScreenFilter{field, operator, value}` 列表 + `order_by` 排序；注释写明 **eligible set ~2000 只，排序在本地做，所以服务端从不截断（`SCREEN_LIMIT = 20_000`）**。
  - → **两阶段的正确形态：粗筛用「一条带条件的查询」打回一屏候选，精查才逐票调 LLM。** 我们已经是「扫描器先出候选池 → research 只对池子里的票调工具」，形态对了；缺的是**把粗筛结果显式写成一张可审计的表**（我们 v1 池选的「类别先验」其实已经是这个角色）。

### 2.2 并行工具调用（OpenAI parallel tool_calls）

- OpenAI 官方中文镜像文档（可访问，platform.openai.com 在本地 403）：https://developers.openai.ac.cn/api/docs/guides/function-calling
  - 原文：「在以 GPT-5 为起点且受支持的模型上……**模型可能会选择在单次会话中调用多个函数**。您可以通过将 `parallel_tool_calls` 设置为 `false` 来阻止这种情况，这将确保**最多只调用一个工具**。」
  - 同页 `strict: true` + `json_schema` + `additionalProperties: false` 的 `response_format` 用法（结构化输出）。
- 结构化输出总览：https://developers.openai.ac.cn/api/docs/guides/structured-outputs
- **对我们的意义**：我们现在是**串行**「模型调一次 → 我们执行 → 回灌 → 再调」。改成**一轮并发 3~4 个**（例如「本轮 8 只票 × 4 个工具」拆成 4 个 `symbols=[...]` 批量调用，一次 assistant turn 全发出）能把**往返次数**直接压下来 —— 这是 <60 秒里最省时间的一刀。`get_kline(symbols=[...])` 这种**批量参数**已经是现成的（我们工具就支持多代码 + `_MAX_SYMBOLS_PER_CALL=20`），**继续加大批量、减少轮次，比让模型多查几轮划算得多**。

### 2.3 提示词层减少无谓查询

- **Anthropic《Writing effective tools for agents》**：https://www.anthropic.com/engineering/writing-tools-for-agents （2025-09-11）
  - **「More tools don't always lead to better outcomes」**：不要只做 API 端点的一比一封装；**要合并**——把 `list_users + list_events + create_event` 合成一个 `schedule_event`；把 `get_customer_by_id + list_transactions + list_notes` 合成一个 `get_customer_context`，**一次调用把所有相关上下文编译好**，减少中间输出占用上下文。
  - **截断要「转向」**：「如果你选择截断响应，务必用有帮助的指令带动 agent」——🎯**直接鼓励 agent 采用更省 token 的策略，例如做很多次小而**针对性**的检索，而不是一次宽泛的检索**。错误响应也要 prompt-engineer 成「具体、可行动」的提示，而不是不透明错误码。
  - **实测 token 差异**：同一 Slack 工具，`detailed` 响应 **206 token** vs `concise` 响应 **72 token**——**约 1/3 的 token**；做法是给工具加一个 `response_format: enum{concise, detailed}` 参数，让 agent 自己选。
  - **响应格式（XML/JSON/Markdown）会显著影响 eval 表现，没有万金油**，要按自己的 eval 选。
  - **量化指标**：建议收集**每次工具调用的耗时、总调用次数、总 token 消耗、工具错误数**；「**大量冗余工具调用**」这个信号本身提示该调 pagination/token 上限，「大量参数错误」提示该改描述。
- **TradingAgents #750 / PR #1281**（见 1.2）：**把易变值从 system prompt 挪到最后 + 重排成「共享上下文 → 追加历史 → 角色指令」**，让 provider 自带的隐式前缀缓存自己生效，**不改一字指令**就把命中率从 ~10% 提到 ~28%。这是**零成本、零风险**的一刀。
- **LLMLingua**（微软，EMNLP'23 / ACL'24）：https://github.com/microsoft/LLMLingua —— 「压缩 prompt 与 KV-Cache，**最高 20x 压缩、性能损失极小**」。适合我们对**超长工具输出**（公告正文、龙虎榜长表）做压缩后再入 prompt。

### 2.4 提前停止（预算闸的进阶版）

- **TradingAgents PR #1404**（见 1.2）：每轮辩论后由**廉价判定模型**问「证据够判了吗」，够了提前结束，**省一轮 2 次 LLM 调用**；且**三层降级**（无 key 不构造 / 调用失败返回 None / 轮数上限永远优先）。
- → 我们的 `max_rounds=8 / max_calls=30` 是**硬闸**，建议再加一个**软闸**：模型连续两轮只调低收益工具（或调用数已达 N）就注入一句「请现在收敛给出结论」。**软闸比硬闸省时间，且不会把正常生成误杀成「服务不可用」。**

---

## 三、数据缓存层

### 3.1 值得直接照搬的分档 TTL：ai-hedge-fund

- `hedge_fund/data/cached.py`：https://github.com/virattt/ai-hedge-fund/blob/main/hedge_fund/data/cached.py
  - `SAME_DAY_TTL = 600`（未结束的当日数据：10 分钟）
  - `VOCABULARY_TTL = 86_400`（变化极少的字段词表：1 天）
  - 已结束日期的历史数据：**永久复用**
  - **只有成功响应才缓存；错误继续抛出（fail-loud preserved）**；`refresh=True` 才忽略已有条目。
  - **`live=True` 开关**：给「无日期的 latest 应答」（earnings、company facts）同样的 10 分钟，因为进程可能整天常驻、否则永远看不到新财报。**← 我们的常驻扫描器正是这个形状。**
- `hedge_fund/llm/cache.py`：https://github.com/virattt/ai-hedge-fund/blob/main/hedge_fund/llm/cache.py —— 一 prompt 一 JSON，key=`sha256(agent|model|system|user)`；**缓存 = 回放记录 = 调试轨迹**三合一。
- `signals/llm_agent.py`：https://github.com/virattt/ai-hedge-fund/blob/main/hedge_fund/signals/llm_agent.py —— **日内上下文分桶**，安静 bar 与上一根渲染出的 prompt 完全相同 → 白嫖缓存。

### 3.2 通用语义缓存（用于「同一个问题反复问」）

- **GPTCache**（Zilliz）：https://github.com/zilliztech/GPTCache ｜ 论文：https://aclanthology.org/2023.nlposs-1.24/
  - 自述 **「Slash Your LLM API Costs by 10x, Boost Speed by 100x」**。
  - 模块化：Embedding Generator → **Vector Store 相似检索** → **相似度评估函数** → **缓存逐出策略**；相似度阈值可配（文档示例 `Config(similarity_threshold=0.2)`），并可**关掉 embedding 退化成关键词匹配缓存**（`Disable embedding. This will turn GPTCache into a keyword-matching cache.`）。
  - 明确列出语义缓存的两个失效模式：**命中时的假阳性（false positive）与未命中时的假阴性**，并提供三个指标（含 **latency**）来做基准。
  - ⚠️ 官方 NOTE：**已不再为新 API/模型加适配**，建议直接用它的 `get`/`set` API。
- **对我们的判断**：我们的查询**高度结构化**（`get_kline(symbol, n)`、`get_fund_flow(symbols)`），**语义相似度缓存价值有限、假阳性风险还高**（把 A 票的量比返回给 B 票 = 直接幻觉）。**应该用确定性 key 缓存（symbol+tool+trade_date），而不是向量相似缓存。** GPTCache 值得借的是它的**「命中/未命中的假阳假阴」评估意识**，不是它的实现。

### 3.3 外呼治理（与缓存配套，缺一不可）

- **quantdash-ai-stock**：https://github.com/rancy777/quantdash-ai-stock —— 代理层已支持：**请求去重、缓存、失败冷却、并发上限、快照回退、最小请求间隔、轻量重试**；**Redis / 内存双模式缓存**；SQLite WAL / busy timeout / 自动事务控制。
- **PanWatch** 的 `toolkit_adapter.py`：https://github.com/TNT-Likely/PanWatch/blob/main/src/modules/automation/tradingagents/toolkit_adapter.py —— 命中 patch 时**直接返回 PanWatch 已拉好的数据**，避免「重复请求外部 API」。

### 3.4 Prompt 前缀缓存（provider 侧，几乎零成本）

- OpenAI 官方（可访问镜像）：https://developers.openai.ac.cn/api/docs/guides/prompt-caching
  - 原文：「复用的 Token 享受模型降低的缓存输入费率，**折扣最高可达 90%**」；**对支持的模型默认启用**；带「提示词缓存仪表盘」可看**读取命中率**。
  - 计费算例（原文）：缓存写入约 1.25×，**后续读取仅 0.1×**；「写入一次 + 完整复用一次 = 普通输入的 1.35×（无缓存两次是 2×）」；「**10 次请求中，1 写 + 9 读 = 2.15×，无缓存是 10×**」。
  - 支持**显式模式**（自己放 `prompt_cache_breakpoint`）与隐式模式。
- Anthropic prompt caching 文档（页面正文在本地因区域限制取不到，链接本身 200）：https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
- LiteLLM 的跨 provider 缓存配置文档（可访问）：https://docs.litellm.ai/docs/completion/prompt_caching
- → **要点：把「不变的 system prompt + 9 个工具 schema」放最前、把易变的素材放最后，让前缀稳定，就能白嫖 90% 输入折扣。** 这对我们这种**同一天同一套 system prompt 跑很多次**的场景收益极高。

---

## 四、输出层（报告简洁 / 两阶段总结）

### 4.1 「多轮交叉验证 → 第二轮单独总结」的两阶段模式

- **PanWatch 的工程实证**（最有说服力）：https://github.com/TNT-Likely/PanWatch/blob/main/docs/tradingagents-flow.en.md
  - 「四类分析师**并行**跑，然后**汇合**再进研究辩论」——**先并行取证、后单点收敛**，就是两阶段的形状；轮数可配。
  - **「TradingAgents 的评级映射到 PanWatch 的 buy/add/hold/reduce/sell；解析不出来的评分一律标记为『需人工复核』」** —— **输出解析必须有兜底状态，绝不硬猜。**
  - 决策日志**单独存储**，不回灌污染分析历史。
- **TradingAgents 的结构化输出分层**（v0.2.4，见 1.1）：**只有 Research Manager / Trader / Portfolio Manager 三个下结论的 agent 走 structured output**，中间环节不强制 schema。
- **ai-hedge-fund 的 Signal 契约**：https://github.com/virattt/ai-hedge-fund/blob/main/hedge_fund/signals/base.py
  - LLM agent 与量化模型**实现同一个接口**，统一输出 **`conviction ∈ [-1, +1]` + thesis**，且每个模型**自带 `horizon_days`**（价值投资者按年，盈利漂移按下一个财报前的周数）——**评分时按各自的时间尺度打分**。
  - → **输出 schema 的最小必要集：`symbol / direction / conviction / evidence_strength / horizon / risks[]`。**

### 4.2 保证报告简洁（schema + 字数约束）

- **结构化输出 + strict schema**（OpenAI 官方，`additionalProperties: false`）：https://developers.openai.ac.cn/api/docs/guides/structured-outputs —— **schema 本身就是字数与字段最硬的上限**：只定义你要的字段，模型就写不出第 11 段散文。
- **工具层用 `response_format` 控制详略**（Anthropic 实测 **206 → 72 token，约 1/3**）：https://www.anthropic.com/engineering/writing-tools-for-agents
- **LangChain 的分段总结策略**（概念与命名已标准化）：https://python.langchain.com/docs/how_to/summarize_chat/
  - `stuff`（全塞）／**`map-reduce`（分段并行总结 → 再合并）**／`refine`（串行迭代精炼）／`map-rethink`。
  - → **我们是 `map-reduce` 的天然适用者**：8 只票**各自并行**出一段（map），再来一次**只做收敛**的调用（reduce）。这正好是「第二轮单独总结」，且 map 阶段可以并发、reduce 阶段只跑一次。
- **我们的现状**：`BRIEF_INSTRUCTION` 已有「不要用 Markdown 标题/列表符号/加粗」这类格式约束。**下一刀应该是把字段锁进 schema**，而不是继续加自然语言禁令——**「禁止写长」永远不如「没有字段可以写长」有效。**

### 4.3 成本/延迟的量化基线（做决策时用）

- OpenAI prompt caching（见 3.4）：缓存读 **0.1×**；10 次请求 1写9读 = **2.15× vs 10×**。
- Anthropic 工具响应：**concise vs detailed ≈ 72 vs 206 token**。
- LLMLingua：**最高 20x 压缩，性能损失极小**。
- TradingAgents #750 实测：单次 pipeline **16–22 次 LLM 调用**；无缓存多付 **30–40%**。
- TradingAgents #1625 实测：工具输出不设限 → 单请求 **329,068 token** → 直接 400。
- PanWatch 自述：九 agent 研报 **3–5 分钟**。
- ai-hedge-fund `SAME_DAY_TTL = 600`（秒）。
- **我们的目标 60 秒 ≈ 上面最快的项目的 1/3~1/5**，**因此「多 agent 辩论」这条路在我们的时间预算内不可行**——必须走「**批量并行取证 → 单次收敛总结**」。

---

## 五、反面案例（LLM 金融 agent 的常见失败模式）

### 反面案例 1｜TradingAgents 的「0% 缓存命中 + 16~22 次调用」
- https://github.com/TauricResearch/TradingAgents/issues/750
- **失败模式：过度调用 + 结构上无法缓存。** 单次分析 16–22 次 LLM 调用，命中率 ≈ 0%，**白付 30–40% 溢价**。根因是「把 `current_date` 之类易变值注入 system message」「每个节点后清空消息历史」「辩论用 f-string 拼动态报告」。
- **教训**：**成本问题往往不是模型贵，而是 prompt 构造成本不可复用。** 修法（PR #1281）只是**重排顺序、一个字都不改**。

### 反面案例 2｜TradingAgents #1625「329,068 token 请求直接报错」
- https://github.com/TauricResearch/TradingAgents/issues/1625
- **失败模式：工具输出无上限 → 上下文爆炸 → 400。** 打开一个新闻源后单请求冲到 32.9 万 token，超出模型上限直接失败（v0.5/0.6/0.7.1 均复现）。
- **教训**：**每一个工具都必须有输出上限，且截断必须「明说」**（TradingAgents #750 的姊妹问题；我们 `_MAX_TOOL_CHARS` + `_note_truncated` 的对齐方向正确，别退回去）。

### 反面案例 3｜TradingAgents #141「报告写 SPY P/E 15.37，真实值 27」
- https://github.com/TauricResearch/TradingAgents/issues/141
- **失败模式：幻觉数字 + 上游源不可信。** 报告里一个漂亮数字来自某个单一站点（fintel），与该指标真实口径相差近一倍。issue 里用户的原话是「**应该先搞清楚这些数字怎么算的，再放进分析**」。
- **教训**：**数字核对要同时管两头 —— 模型不许编（我们有 `unverified_numbers`），数据源本身也要可信/交叉（我们东财+雪球双源正好是优势，应把交叉校验显式化）。**
- 配套：**PR #320 物理 URL 校验**（https://github.com/TauricResearch/TradingAgents/pull/320）证明「文本比对」式的 fact check 会漏掉「引用一个不存在的链接」这类幻觉。

### 反面案例 4｜FinanceBench：GPT-4-Turbo + 检索，81% 答错或拒答
- 论文：https://arxiv.org/abs/2311.11944
- 原文：「**GPT-4-Turbo used with a retrieval system incorrectly answered or refused to answer 81% of questions**」；「用更长上下文窗口**能提升表现，但在企业场景不现实 —— 延迟增加**，且撑不住更大的财务文档」；「**所有被测模型都表现出弱点（如幻觉），限制了其企业可用性**」。
- **教训**：**「换更长的上下文」是官方点名的伪解法（延迟不可接受）。** 我们 <60 秒的约束下，唯一出路是**更准的检索（对的字段）+ 更短的上下文**，而不是塞更多。

### 反面案例 5（清单式）｜ai-hedge-fund 引用的 RAG/推理失败清单
- https://github.com/virattt/ai-hedge-fund/issues/491 （WFGY ProblemMap，被 RAGFlow / LlamaIndex / ToolUniverse(Harvard MIMS) 等采用；上游清单：https://github.com/onestardao/WFGY/blob/main/ProblemMap/README.md）
- 原文点名的金融场景失败模式（**逐条都能在我们身上找到对应风险**）：
  - **narratives that sound plausible but ignore part of the data** —— 听着合理但漏看了部分数据（← 我们 82 项素材、20 只票上限的截断，正是高发点）
  - **confusion between similar tickers or different time windows** —— 弄混相似代码或时间窗（← **PanWatch 就真的踩过**：模块级 dict 让两只票并发时**K 线/新闻互相覆盖**，报告里混入另一只票的数据）
  - **long explanations that drift away from the original hypothesis** —— 报告越写越长、偏离原始问题（← 我们的「报告冗长」）
  - **difficulty telling whether a bad suggestion is caused by retrieval, reasoning, or tooling** —— **分不清是取数、推理还是工具的问题** （← 缺端到端 trace）
  - 对应 WFGY 编号：No.1 hallucination / chunk drift、No.2 interpretation collapse、No.3 long reasoning chains、No.4 overconfident bluffing、No.8 black-box debugging

---

## 六、最值得抄的 3 个做法（对 A 股创业板 / 60 秒扫描 / 单次 <60 秒）

### 抄 1｜**「批量并发取证 → 单次收敛总结」替代多轮串行**（工具调用层）
- 依据：OpenAI 官方确认**单轮可并行发多个工具调用**（`parallel_tool_calls`，默认允许，设为 `false` 才是「最多一个」）——https://developers.openai.ac.cn/api/docs/guides/function-calling；我们的工具**本来就支持批量代码**（`_MAX_SYMBOLS_PER_CALL=20`）。
- **做法**：把「本轮 N 只票 × 4 类数据」压成 **1 轮 4 个并发批量调用**（k线 / 资金流 / 概念 / 市场概览），**取证与判断彻底分开**；再补一道**软闸**（调用数或轮数达阈值就注入「请收敛结论」，参考 TradingAgents #1404 的提前停止 + 优雅降级）。
- **为什么适合我们**：我们**没有** TradingAgents/PanWatch 的 3–5 分钟预算，<60 秒里**最大的时间开销是 LLM 往返次数**，不是工具本身。压往返次数是唯一能同时降延迟和降 token 的一刀，而且**不牺牲证据量**。

### 抄 2｜**分档 TTL + 确定性 key 的本地缓存**（缓存层）
- 依据：ai-hedge-fund `cached.py` 的 **`SAME_DAY_TTL = 600`**＋「已完成日期永久复用」＋「只有成功才缓存、错误继续抛出」＋ `live=True` 处理常驻进程——https://github.com/virattt/ai-hedge-fund/blob/main/hedge_fund/data/cached.py；配合 quantdash 的**请求去重 / 失败冷却 / 并发上限 / 最小请求间隔**——https://github.com/rancy777/quantdash-ai-stock。
- **做法**：在 SQLite 里加 `ai_tool_cache(key = symbol|tool|trade_date|args_hash, payload, fetched_at, ttl_class)`；**盘中量比/主力净占比走短 TTL（分钟级），收盘定稿 K 线/概念归属走长 TTL**；**不做向量相似缓存**（我们查询是结构化的，语义缓存会把 A 票数据返回给 B 票 = 制造幻觉）。
- **为什么适合我们**：60 秒窗口里同一批票**经常被重复问**（模型自我纠偏、或同日多次扫描），命中即 0 毫秒；且我们是 SQLite，**加一张表就落地**，不需要引入 Redis。
- **附赠**：这条**顺带解决了 prompt 前缀缓存**——把易变素材移到 prompt 尾部、system prompt 字节稳定，就能吃 provider 的**最高 90% 输入折扣**（https://developers.openai.ac.cn/api/docs/guides/prompt-caching），做法只是**重排顺序**（TradingAgents PR #1281 实测命中率 ~10% → ~28%）。

### 抄 3｜**统一快照 + 结论 schema + 三重闸（弃权/巡检/需人工复核）**（提示词 + 输出层）
- 依据：A-Daily-TradingAgents 的 **`analysis_snapshot`「评分/Prompt/AI 分析/DB/前端共用同一套事实来源」**——https://github.com/cyecho-io/A-Daily-TradingAgents；ai-hedge-fund 的 **fail-loud + abstain**（查不到 ≠ 中性）与 **`conviction ∈ [-1,1]` + `horizon_days`** 契约——https://github.com/virattt/ai-hedge-fund/blob/main/hedge_fund/signals/base.py；PanWatch 的 **「评级解析失败 → 标需人工复核」**——https://github.com/TNT-Likely/PanWatch/blob/main/docs/tradingagents-flow.en.md。
- **做法**：① 把 `research.py` 工具输出与 `material.py` 素材收敛成**一个 `AnalysisSnapshot` 对象**，评分/提示词/幻觉巡检/报告四处引用同一实例；② 最终输出走**固定 schema**（`symbol / direction / conviction / evidence_strength(strong|medium|weak|insufficient) / horizon / risks[]`）——**字段即字数上限**（Anthropic 实测 `concise` 比 `detailed` 省约 2/3 token）；③ 保留我们的 `unverified_numbers` 巡检，**再加「弃权」与「需人工复核」两个显式状态**。
- **为什么适合我们**：我们已有 `material.py`（幻觉巡检）与 `guard.py`（红线槽位）两道确定性闸，**架构方向已经对了**；缺的是**一个统一的快照对象**（现在四处各自组装，容易漂移）和**一个把长度写死进 schema 的输出契约**。这三样加起来**不加一次 LLM 调用**，纯属把已有资产结构化。

---

## 附：一句话结论

TradingAgents 证明了多 agent 辩论**能出成果但代价是 16–22 次调用、3–5 分钟、且 0% 缓存命中**（#750）；ai-hedge-fund 展示了**正确的工程底座**（分档 TTL 缓存、fail-loud+abstain、prompt 缓存、point-in-time）；PanWatch / A-Daily-TradingAgents 展示了**中文 A 股场景的落地细节**（并发数据串台、3–5 分钟、评级解析兜底、统一快照）。

**我们的 60 秒预算不允许抄它们的「架构」，只允许抄它们的「工程纪律」——即上面三条。**

---

### 全部链接（2026-11-11 逐条验证 200）

**架构**
1. https://github.com/TauricResearch/TradingAgents
2. https://tauricresearch.github.io/TradingAgents/
3. https://arxiv.org/abs/2412.20138
4. https://tradings.cn/index.html
5. https://github.com/virattt/ai-hedge-fund
6. https://github.com/virattt/ai-hedge-fund/blob/main/hedge_fund/README.md
7. https://github.com/virattt/ai-hedge-fund/blob/main/hedge_fund/data/cached.py
8. https://github.com/virattt/ai-hedge-fund/blob/main/hedge_fund/llm/cache.py
9. https://github.com/virattt/ai-hedge-fund/blob/main/hedge_fund/signals/llm_agent.py
10. https://github.com/virattt/ai-hedge-fund/blob/main/hedge_fund/signals/base.py
11. https://github.com/virattt/ai-hedge-fund/blob/main/hedge_fund/data/screener.py
12. https://github.com/AI4Finance-Foundation/FinRobot
13. https://arxiv.org/abs/2405.14767
14. https://github.com/AI4Finance-Foundation/FinGPT
15. https://github.com/AI4Finance-Foundation/FinGPT/tree/master/fingpt/FinGPT_RAG
16. https://github.com/hsliuping/TradingAgents-CN
17. https://github.com/KylinMountain/TradingAgents-AShare
18. https://github.com/TNT-Likely/PanWatch
19. https://github.com/TNT-Likely/PanWatch/blob/main/docs/tradingagents-flow.en.md
20. https://github.com/TNT-Likely/PanWatch/blob/main/src/modules/automation/tradingagents/toolkit_adapter.py
21. https://github.com/cyecho-io/A-Daily-TradingAgents
22. https://github.com/rancy777/quantdash-ai-stock
23. https://github.com/ArvinLovegood/go-stock
24. https://github.com/guangxiangdebizi/A-Scope-Research
25. https://github.com/WackyGem/Fleur

**成本 / 延迟 / 工具设计**
26. https://developers.openai.ac.cn/api/docs/guides/function-calling
27. https://developers.openai.ac.cn/api/docs/guides/structured-outputs
28. https://developers.openai.ac.cn/api/docs/guides/prompt-caching
29. https://www.anthropic.com/engineering/writing-tools-for-agents
30. https://docs.litellm.ai/docs/completion/prompt_caching
31. https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
32. https://github.com/microsoft/LLMLingua

**缓存**
33. https://github.com/zilliztech/GPTCache
34. https://aclanthology.org/2023.nlposs-1.24/

**输出层**
35. https://python.langchain.com/docs/how_to/summarize_chat/

**反面案例**
36. https://github.com/TauricResearch/TradingAgents/issues/750
37. https://github.com/TauricResearch/TradingAgents/pull/1281
38. https://github.com/TauricResearch/TradingAgents/issues/1665
39. https://github.com/TauricResearch/TradingAgents/issues/1625
40. https://github.com/TauricResearch/TradingAgents/pull/1404
41. https://github.com/TauricResearch/TradingAgents/pull/320
42. https://github.com/TauricResearch/TradingAgents/issues/141
43. https://github.com/virattt/ai-hedge-fund/issues/491
44. https://github.com/onestardao/WFGY/blob/main/ProblemMap/README.md
45. https://arxiv.org/abs/2311.11944
