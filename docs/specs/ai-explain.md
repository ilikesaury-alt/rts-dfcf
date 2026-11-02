# Spec: AI 解读层（ai-explain）

> 按 ai-dev-flow 工作流沉淀。方案背景见 `docs/features/ai-explain/AI解读层-方案设计.md`；
> 本 Spec 以**代码现状基线核实后**的口径为准，与方案 v1 冲突处（素材字段）以本文件为准。
> 2026-11-02 拍板：P1 云端 web 面板 · P2 手工先验文案 · P3 Worker 内存缓存 · P4 详情页触发。
> **2026-11-02 M0 修订（v2）**：实测推翻「kline_cache 每轮有 ≤20 票」这一假设 ——
> `ingest.ts::pickStocks` 用 `/^\d{6}$/` 过滤，而真实 `symbol` 带前缀（`SZ301171`）
> ⇒ **全部被丢弃**，`kline_cache` 在生产环境恒为空。详见 §5 已验证第 6 条。

## 1. 问题

云端面板只给数字（评分/层/排名），阅读者要自己回想「这个类别是什么先验、出现史说明什么」。
需要一个红线内的 AI 自然语言解读：解释素材由服务端确定性组装，LLM 只转录，
**不产生第二结论源、不入库、不参与排序与过门**。

## 2. 范围

**做**

- **修 `ingest.ts::SYMBOL_RE` 缺陷**（M0 发现，前置阻塞）：正则改为接受带交易所前缀的 A 股代码，
  并补一条**用真实格式**（`SZ301171`）的守卫测试；否则 E1/E2 在生产恒 404
- worker 新增 `GET /api/stock/{symbol}/kline`（补齐 `Stock.tsx` 已依赖但**未实现**的端点，欠账）
- worker 新增 `GET /api/ai-explain?symbol=`：确定性素材组装 → magpie 转录（OpenAI 兼容 fetch）→ 红线词表扫描 → 带「仅解读，非投资建议」标签返回
  - ⚠ **GET 而非 POST**（审查 A / P2-3 修正）：面板 NFR 是只读，全仓唯一写端点是 `/api/ingest`，
    且 `web/src/api/client.ts` 明文写着「不新增端点给 apiSend 找用武之地」。本端点不写任何表 ⇒ 用 GET。
- web 详情页（`Stock.tsx`）新增「AI 解读」区块：固定标签 + 文本 + 素材对照（可折叠）
- 素材范围（**已按基线收窄**）：该票在最新快照 regions 各区块中的行字段（类别/层/评分/排名）+ `kline_cache.appearances` 出现史 + K 线摘要 + 手工维护的类别先验文案
- Worker 内存缓存（素材 hash 为 key）

**不做**

- Python 扫描器/上报链路/feishu/终端任何改动
- 评分分解、资金流的解读（**云端上报体没有这些字段**，见 §5 已验证——做就是编地基）
- 自由文本问答（请求只有 `{symbol}`，越界提问面在结构上不存在）
- 流式输出、D1 持久缓存、desktop/飞书入口（留二期）
- AI 输出写入 `recommendations` / 排序 / 过门（任何计算路径不读 AI 结果）

## 3. 验收标准

| # | 验收标准 | 判定方式 |
|---|---|---|
| 0 | `ingest` 接受真实格式 symbol（`SZ301171`/`SH600036`/`300001`），拒绝 `not-a-symbol`；`kline_cache` 真被写入 | worker vitest（**真实格式**键，不是 6 位数字） |
| 1 | `GET /api/stock/{symbol}/kline`：命中 `kline_cache` 返回 K线+出现史；未命中返回明确 404 文案 | worker vitest（内存 D1 mock） |
| 2 | symbol 不在最新快照 regions/kline_cache → ai-explain 返回明确 404 文案，**零模型调用** | vitest（mock fetch 断言未调用） |
| 3 | magpie secrets 未配置 → 面向用户文案，无堆栈泄露，零模型调用 | vitest |
| 4 | 每个成功响应必含 `label: "仅解读，非投资建议"`（服务端保证） | vitest |
| 5 | 红线词表：mock LLM 返回含「建议买入/卖出/目标价/仓位/止损」等 → 整体替换为拒答文案 | vitest（**含平凡变体 10 例**：全角冒号、词内空格、全英文、目标位/合理价/看多/轻仓等；见 `worker/test/guard.test.ts`） |
| 6 | 素材组装为纯函数：同一 D1 状态 → 素材对象字节级一致；缺字段输出「暂无数据」 | vitest 确定性断言 |
| 7 | 响应含素材数组，每段解释可对应素材 ID，素材值与 D1 快照一致 | vitest |
| 8 | AI 结果零写入任何表；重复请求同素材命中内存缓存（第二次零模型调用） | vitest |
| 8b | 频控：素材变化后同 symbol 再请求 → 429 `AI_RATE_LIMITED`，零模型调用（防刷爆付费调用） | vitest |
| 9 | 既有回归：worker vitest 全绿；`pytest tests/` 全量无新增失败（本应零改动） | CI / 本地跑 |
| 10 | web 详情页可点「AI 解读」→ 渲染标签+文本+素材对照；失败显示用户文案不白屏 | 线上 E2E（真实快照 + 真凭据，**拆给人手动跑**，前置清单见 §5） |
| 11 | 前端无密钥、无堆栈上屏 | 人工审查 + 构建产物 grep |

## 4. 关键决策

| 决策 | 理由 | 备选及放弃原因 | 记录位置 |
|---|---|---|---|
| LLM 只转录 + 双道守卫（素材确定性 + 输出词表），无自由输入 | 本项目唯一有证据口径是类别先验，LLM 不得成为第二结论源；无问答面则拦截用户输入的闸不需要 | 提示词自律（不可靠，health-track 已验证范式是剥夺能力而非祈求） | `docs/adr/0001-ai-explain-redline.md` |
| 词表守卫承诺**明文降级**（审查 A / P1-1） | 实测 8/8 平凡变体可绕过逐字正则 ⇒ 改为归一化+槽位；残余（槽位外说法/拼音/隐含建议）明文登记，不假装全覆盖 | 声称「守卫充分」（与实测不符，拒绝） | ADR 修订 1 |
| 素材**允许跨表拼装**，但**双侧轮次都披露**（审查 A / P2-2） | 强制同轮会使命中率极低（票掉出 20 票配额即无该轮 K 线）；改为「允许 + 必须披露快照轮次与 K 线交易日」 | 强制同轮（解读大面积不可用，拒绝）；隐去差异（用户会误读同刻数据，拒绝） | 本文件 §6 |
| 端点用 **GET**（审查 A / P2-3） | 面板只读纪律；全仓唯一写端点是 ingest，不为 apiSend 新增用武之地 | POST（违反 api/client.ts 明文纪律） | 本文件 §2 |
| 入口=云端 web 详情页（P1+P4） | D1 数据现成、有 Access；desktop 走 127.0.0.1 需另建 LLM 代理通路，改动面大 | desktop 壳 / 飞书卡片（触碰 Python，违反零改动） | 本文件 §4 |
| 先验解释=worker 内手工文案（P2） | 保住 Python 零改动；先验变化频率低 | 扩上报字段带实时统计（二期再评估） | 本文件 §4 |
| 缓存=Worker 内存（P3） | 解读量小（人工点击才产生），重启丢失可接受，省 D1 写入配额 | D1 持久表（多一张表迁移 + 「计算路径不读它」守卫成本） | 本文件 §4 |
| 先补 `/api/stock/{symbol}/kline` 欠账 | 详情页是它已声明的消费方，缺它则 P4 入口无处安放；独立可验收 | 把素材数据内嵌 ai-explain 返回（详情页欠账仍在，两件事耦合） | 本文件 §2 |

## 5. 假设与待确认

**已验证**（2026-11-02 代码基线核实）

- worker 路由仅 `ingest.ts` / `meta.ts` / `regions.ts`，**无** stock 读取端点 → 欠账成立
- D1 有 `kline_cache` 表（`symbol, kline_date, kline, appearances`），ingest 每轮**试图**写入 ≤20 只票（`STOCK_QUOTA=20`，幂等 upsert）
- **❌ 假设已推翻（v2）**：真实 `symbol` 是 `SZ301171` / `SH600036`（实测 `scanner.db` 的 `daily_kline`、`recommendations`），
  而 `ingest.ts::pickStocks` 的 `SYMBOL_RE = /^\d{6}$/` 只收 6 位纯数字 ⇒ **真实票全部被当作脏键丢弃**
  （`node` 实测：`SZ301171 → DROPPED`、`300001 → ACCEPTED`）⇒ 生产 `kline_cache` 恒为空。
- **❌ 既有测试假绿（v2）**：`test/api.test.ts` 的 stocks 用例一律用 `300000+i` / `"300001"` 这类 6 位键，
  与生产格式不符，故该缺陷**永远不会被测试发现**（与 AGENTS.md 记录的历史假绿同型）。
- `appearances` 行字段实测为 `date / time / category / score / percent`（`panel_reporter._appearances_for`）
- 上报体 `Stock = { klineDate, kline, appearances: Row[] }`（`serialize.ts:57`）——**无评分分解/资金流字段**
- `/api/regions` 返回 `{regions, ctx}`，不含 stocks；行字段（类别/层/评分/排名）在 regions 行内
- 鉴权：`bearerGuard`（ingest 写路径）；面板读路径走 Cloudflare Access + `workers_dev:false`
- web 有详情页 `pages/Stock.tsx`（hash 路由 `#/stock/{symbol}`），其 K线数据契约 `[date,open,close,low,high,volume]`

**假设（未经证实）**

- magpie 网关凭据可自备（用户持有）——若不成立，§3-10 线上 E2E 无法跑（单测不受影响）
- regions 行字段 + appearances + 手工先验文案足以组装「够解释」的素材——**M0 先验证**：拿一条真实 `kline_cache` 行 + 最新 regions payload 组一次素材人工过目，不够则回改本 Spec
  - **状态（v2）**：素材字段口径已按真实字段定稿（§2），但「真实 kline_cache 行」目前不存在
    （见 §5 推翻项）⇒ 该 M0 需在 `SYMBOL_RE` 修复后、有真实上报时才能完成

**待确认（需人工回答）**

- [ ] magpie `MAGPIE_BASE_URL / MAGPIE_API_KEY / MAGPIE_MODEL` 三值（部署期 `wrangler secret put`，勿发到对话/文档）——负责人：用户
- [ ] §3-10 线上 E2E 拆给人手动跑（需真凭据+真快照）——负责人：用户

## 6. 影响面

> ### 已知项（既有缺陷，**不在本次范围**，登记备查）
>
> 1. **`kline_cache` 无保留策略（既有）**：`wrangler.jsonc` 配了 `crons: ["7 * * * *"]`，
>    但 worker **无 scheduled handler**（`src/` 下无 `DELETE` 语句、无 `scheduled()` 导出）
>    ⇒ 表只增不减。此缺陷此前被「stocks 全被丢弃 ⇒ 表恒空」掩盖，
>    E0 修复后才会真增长。量化：约 20 行/交易日 × ~25KB/行 ⇒ 250 交易日 ≈ **122 MB**
>    （D1 免费版 5 GB 上限的约 12%）。**不紧急但需排期**（建议：保留最近 60 个交易日）。
> 2. **Access 应用配置在仓库外**：`wrangler.jsonc` 的 `workers_dev:false` + `routes` 覆盖
>    已保证两个新端点在 `panel.PANEL_DOMAIN/api/*` 域内，但「Access 应用覆盖 `/*`、
>    且仅排除 `/api/ingest`」无法从代码证实 ⇒ 部署时**必须人工核对**一次。
> 3. **无应用层鉴权兜底**：读端点不校验 `Cf-Access-Jwt-Assertion`，
>    完全依赖 Cloudflare Access。若 Access 被误配成路径白名单，读端点即裸奔。
> 4. **⛔ 「把 SYMBOL_RE 改回 /^\d{6}$/」不是回滚，是回归**（复核项 3）：
>    该正则被 ingest / stock / explain **三个端点共用**（`worker/src/symbol.ts`），改回去会同时
>    (i) 重演 `kline_cache` 恒空缺陷、(ii) 让两个读端点对带前缀 symbol 全返 400。
>    真正的止血手段是「只收紧写侧 + 补前缀一致性校验」（已于 2026-11-02 完成：`isSymbol`
>    现验形状**与**前缀-代码段一致性），已写入的坏键只能手动 DML 清理：
>    ```sql
>    -- 按交易所代码段规则批量清理前缀错配（6/9/5→SH，0/3/2/1→SZ，4/8/9→BJ）
>    DELETE FROM kline_cache
>    WHERE (symbol LIKE 'SH%' AND substr(symbol,3,1) NOT IN ('6','9','5'))
>       OR (symbol LIKE 'SZ%' AND substr(symbol,3,1) NOT IN ('0','3','2','1'))
>       OR (symbol LIKE 'BJ%' AND substr(symbol,3,1) NOT IN ('4','8','9'));
>    ```
> 5. **缓存与频控都只在单 isolate 内生效**（复核项 1–2）：`CACHE` / `LAST_CALL` 均为模块级 Map，
>    workerd 多 isolate / 多 colo 不共享，且 isolate 回收即清空。因此：
>    - 缓存只能当**成本优化的 best-effort**，**不可当成本上界**；
>    - 频控能挡真人连点（通常落同一 isolate），**挡不住脚本化/分布式刷**。
>    真限流需平台手段（WAF rate-limiting rule / Durable Object / D1 计数器），列入部署清单。
>    ⚠ 并发双打（TOCTOU）目前靠频控的**同步临界区**挡住（`LAST_CALL.set` 在 `await` 之前），
>    属「频控的副作用」而非缓存设计保证——改动该顺序会立即重开双付费窗口。
> 6. **隐私边界**（复核项 4）：外发内容仅单只股票的行情/信号素材（区块行白名单字段 + 评分分解
>    + 出现史 ≤20 条 + K 线摘要 + 手工先验文案）；`ctx`（warnings/ruleResult/config 等）
>    **不外发**；无任何用户身份/IP/账号信息。两个结构性边界：① 网关侧可推断「关注了哪些票」；
>    ② 自有评分口径属业务信息外流。

- **涉及文件**：
  - 新增 `rts-panel-cloud/worker/src/explain/{material.ts, llm.ts, guard.ts}`（纯函数优先）
  - 修改 `rts-panel-cloud/worker/src/index.ts`（路由注册，新增 2 个）
  - 修改 `rts-panel-cloud/web/src/pages/Stock.tsx`（新增解读区块）+ `api/{client.ts, types.ts}`
  - 新增 `worker/test/explain*.test.ts`
- **数据或接口变更**：D1 只读（新增 2 个读端点），无迁移、无写路径变化；ingest/regions/meta 契约不动
- **风险点**：
  - 权限：新端点必须落在 Access 覆盖域内（不走 bearerGuard），否则绕过 Access 读数据——实现时对照 `auth.ts` 复核
  - 一致性：素材允许跨表拼装（快照按轮 + K 线按票），但**必须双侧披露**：`generatedFrom` 同时给快照轮次与该票 K 线最新交易日（审查 A / P2-2）；禁止跨表拼装已改为「允许 + 披露」
  - 成本：词表扫描 + 缓存 + 详情页触发 + **同 symbol 30s 出网冷却** 四重限流（审查 A / P2-5 补入频控层）
