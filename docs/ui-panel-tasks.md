# rts-dfcf-max Web 面板 — Tasks（Spec Kit /speckit.tasks）

> **Source**：`docs/ui-panel-requirements.md` v2.1（已定稿，R1~R4 锁定）+ `docs/ui-panel-design.md` **v1.1（2026-09-29 复核修订版）**
> **重新产出说明**：本 tasks 对齐 design **v1.1 的 10 处复核修正**（清单见 design §1.3）——
> 重点吸收：① `SCHEMA_VERSION` 不进 vars（T0.6/T0.7）② 最新轮一律按 `(date,time)` 排序、`seq` 仅展示（T0.8/T3.1/T5.1）
> ③ 跨日保留 SQL 补 `date`（T5.4）④ SSE 单常量 15s×40=10min（T3.1）⑤ `safeEqual`（T0.7）
> ⑥ stocks 超配额截断不拒绝（T0.7）⑦ `workers_dev:false` 同域（T2.2）⑧ `pending_stocks` 增量收集器（T4.2）
> ⑨ `meta/config` 1 次查询（T0.8）⑩ M1 补滞后提示（T1.5）。

## Phase 0 — M0 跨网上报通路（**最大不确定性，风险前置**）   (D1/D3 · FR-C1/FR-C2 · 验收 1/2/3/6)

> **本 Phase 是整个项目唯一的技术悬崖**：`hot_rows/hist_rows/offboard_rows/new_symbols/weak`
> 是 `run_scanner` 局部变量，DB 无法复原；跨网传输还隔着公网。**T0.9 不过 → 不进 Phase 1。**

- [x] **T0.1** 实现纯函数序列化模块：`ScanView → dict`，含 tuple 键转换、`_candidate` 剥离、`score_breakdown` 解析、`_gate`/`_cols` 投影、体积探针（>400KB 截断 `stocks`）   (D3/D6/D7 · FR-C2 规则 1~6)
  - 产出：`scanner/panel_serialize.py`
  - 落点：`design.md §3.1 §3.3 §3.6 §3.7 §5.1`（`serialize_view/_marks/_flat_row/_gate/col_spec` 可抄代码）
  - 验收：`json.dumps(serialize_view(v))` 不抛；`{(sym,cat):v}` → `"sym|cat"`；载荷中无 `_candidate` 键；`regions.cols == col_spec(COLS_POOL/HOT/HIST)`；`SCHEMA_VERSION == 2` 与 `serialize.ts` 同值
- [x] **T0.2** 序列化单测（本模块零网络零依赖，是最早能进 CI 的资产）   (D3/D7 · FR-C2)
  - 产出：`tests/test_panel_serialize.py`
  - 落点：`design.md §10` 前 4 行 + 复核修 6
  - 验收：4 类断言全绿 —— tuple 键往返 / `_candidate` 剥离 / 列 key 序列 == `[c.key for c in COLS_*]` / 150 票样本 → `stocks` 截到 ≤20 且**新票保留**
- [x] **T0.3** 实现上报器：`report()`（调用线程内序列化 ≤50ms + 计时告警）+ `_send`（daemon 线程、`Session` 复用、超时 `(3,8)`、退避 `(1,3)` 重试 2 次、4xx 不重试、`trust_env=False`）+ 三开关口径 + 增量收集器 `sent` 表骨架   (D1 · FR-C1 · OQ#6)
  - 产出：`scanner/panel_reporter.py`
  - 落点：`design.md §3.1`（`report/_send` 代码 + 开关口径表 + 增量收集器 5 条 + 反模式清单）
  - 验收：日志只记状态码与字节数（**不含密钥**）；任何异常只写 `logs/panel_report.log` 不上抛；线程最坏 28s < 60s
- [x] **T0.4** 上报器单测（失败隔离是 G3 的核心证据）   (D1 · G3 · 验收 1/3)
  - 产出：`tests/test_panel_reporter.py`
  - 落点：`design.md §10` 第 5~6 行
  - 验收：monkeypatch `requests.post` 抛异常 → `report()` 正常返回（显式 `assert`，禁空断言）；`assert call_count == 3`、`sleep` 收到 `(1,3)`；401 分支不重试；主线程不阻塞
- [x] **T0.5** 主循环挂点（**唯一改动**，紧跟 `render_terminal`/`push_feishu` 之后）+ `--no-panel` CLI   (G3 · FR-C1 · 验收 1)
  - 产出：`unified_scanner.py`（改动仅限：`panel_enabled` 判断、`report()` 调用、`--no-panel` 参数解析）
  - 落点：`design.md §3.1` 挂点代码 + 开关口径表
  - 验收：`RTS_PANEL_URL` 未配置 → **连模块都不导入**（`"scanner.panel_reporter" not in sys.modules`）；`RTS_PANEL=0` 与 `--no-panel` 都能关；`ruff check` → `mypy` → `pytest tests/` 全绿
- [x] **T0.6** 云端独立仓库脚手架（R3）+ D1 双表迁移 + 通用层（`error/env/serialize`）   (FR-C3 · D4 · 复核修 1/7)
  - 产出：`rts-panel-cloud/worker/wrangler.jsonc`、`worker/migrations/0001_init.sql`、`worker/src/{index,env,error,serialize}.ts`、`worker/package.json`、`worker/.dev.vars.example`
  - 落点：`design.md §2.3 §4.1 §5.2 §11.1`（**必须含** `"workers_dev": false` 与 `routes: panel.<域名>/*`）
  - 验收：`wrangler dev` 启动；`d1 migrations apply --local` 成功且 `sqlite_master` 有 `snapshots`/`kline_cache`；`SCHEMA_VERSION` 在 `serialize.ts` **代码**里（不在 vars）
- [x] **T0.7** `POST /api/ingest`：`bearerGuard`(safeEqual) + 白名单校验（不用 zod）+ `ON CONFLICT` 幂等 batch + stocks 截断   (D2/D4 · FR-C1/FR-C2 · 验收 6)
  - 产出：`rts-panel-cloud/worker/src/routes/ingest.ts`、`worker/src/auth.ts`
  - 落点：`design.md §4.2 §3.2`（`safeEqual`、`STOCK_QUOTA=20`、复核修 1/6）
  - 验收：集成测试 4 条 —— 同 `date+time` POST 两次 → `count(*)==1` 且 `seq` 取新值；错密钥 → 401；`schema=1` → 400 `SCHEMA_MISMATCH`；25 票 → 201 且 `stocksStored=20, stocksDropped=5`（整轮不丢）
- [x] **T0.8** 最小读端点 `GET /api/regions` + `GET /api/meta`   (FR-S1 · 复核修 2/9)
  - 产出：`rts-panel-cloud/worker/src/routes/regions.ts`、`worker/src/routes/meta.ts`
  - 落点：`design.md §4.3 §4.4`
  - 验收：查询一律 `ORDER BY date DESC, time DESC LIMIT 1`（**非 seq**）；集成测试：插入 `date/time` 更新但 `seq` 更小的行 → 必须返回该新行；`regions` 返回 1 次查询、不含 `stocks`
- [x] **T0.9** ★ **M0 验收门**（人工 + 脚本，逐条留证）   (D1 全部 · 验收 1/2/3/6)
  - 产出：`docs/m0-gate-evidence.md`（记录 3 轮比对结果、耗时、断网日志）
  - 落点：`design.md §13.1 M0`、`requirements §十 M0`
  - 验收（全中才放行）：**连续 3 轮**云端读回与终端**逐条一致**（行数 / 首行 symbol / 顺序）；序列化 **≤50ms**；拔网线 10min 扫描照常推进、失败只进 `panel_report.log`；同 `date+time` 重放不产生重复行；`git log -S "RTS_PANEL_SECRET"` 无密钥值
  - **状态（2026-09-29）：✅ 5/5 判据全部通过 → M0 放行，Phase 1 可启动。** 证据见 `docs/m0-gate-evidence.md`。
  - **方法上的关键修正**：判据 1/3 原本被记为「必须人工 + 需公网」，实际它们验的是
    **序列化 → ingest → D1 → regions 读回**这条数据链路，与公网/Access/WAF 无关（那是 M2）。
    改用本地 `wrangler dev` + 本地 D1，以 `scripts/panel_m0_check.py`（复用生产路径
    `build_scan_view` → `render_terminal` → `report()` → `GET /api/regions`）跑出
    **3/3 轮逐条一致**（v1 池选 23 行，行数/首行/顺序全等）。
  - **判据 3 的做法**：用 `netstat`+`taskkill` 杀掉 Worker 使端点真不可达（断网等效），
    验证 **失败隔离机制**（不抛 / 不阻塞 / 只进 `panel_report.log` / 日志无密钥）；
    未覆盖「连续 10 分钟」的时长维度，部署后重跑即可。
  - **实施中修掉一个真 bug**：`report()` 原把 `collect_stocks` 的 DB I/O 算进
    **序列化预算**（1ms 的序列化被报成 250~600ms，告警失效）。已拆成两段计时，
    预算只管序列化（实测 2.7~8.9ms），并加回归单测锁住口径。
  - **公网面（Cloudflare 边缘路由 / Access / WAF / `workers_dev` 旁路）不在 M0**，
    属 M2 判据；部署后复验只需 `python scripts/panel_m0_check.py --url https://panel.<域名> --rounds 3`。
  - **不过 → 不进 Phase 1**，转 design §3.1 路线 B/C（每 N 轮 / 轮次打包）

---

## Phase 1 — M1 面板静态渲染   (D7 · FR-V1/FR-V2 · DR-7 · 验收 7)

- [x] **T1.1** 前端工程骨架（React 19 + Vite，不引状态库、不引 SSR）
  - 产出：`rts-panel-cloud/web/package.json`、`web/vite.config.ts`、`web/index.html`、`web/src/{main.tsx,App.tsx,styles.css}`
  - 落点：`design.md §2.2 §2.3 §7.1`（不拆理由）
  - 验收：`pnpm dev` 起 5173，`/api` proxy → 127.0.0.1:8787；`pnpm-lock.yaml` 入库（DR-8）
  - **状态（2026-09-29）：已交付。** `tsc --noEmit` 干净；`pnpm build` 产物 70.24KB gzip；
    `/api/meta` 经代理返回真实 D1 数据（`{"ready":true,"seq":3,...}`）；
    `pnpm-lock.yaml`（37KB）未被 gitignore，入库即 DR-8 达成。
  - **环境冲突（非代码问题）**：本机 5173 被**另一个项目** `starlight-course` 占用，
    已用 5174 验证（配置里仍固定 5173 + `strictPort`，不静默跳端口 —— 跳端口会让代理目标
    对不上，掩盖配置错误）。要按 5173 起需先停掉那个 vite。
  - **pnpm 12 的坑（已踩）**：依赖 postinstall 被默认拦截，esbuild 二进制装不上；
    设置**不再**读 `package.json` 的 `"pnpm"` 字段，键名也由 `onlyBuiltDependencies`（数组）
    改为 `allowBuilds`（map），正确位置是 `web/pnpm-workspace.yaml`。已用
    `pnpm approve-builds --all` 落地（而非交互式，便于 CI 复现）。
- [ ] **T1.2** 唯一网络收口 `client.ts`（`apiGet/apiSend`，解析 `{ok,error}` 信封）
  - 产出：`rts-panel-cloud/web/src/api/client.ts`、`web/src/api/types.ts`
  - 落点：`design.md §2.2 §5.1 §4.4`
  - 验收：`web/src` 下除 `client.ts` 外 **0 处裸 `fetch(`**（CI grep 断言）；404/400 统一抛错不吞
- [ ] **T1.3** `colSpec` 消费层（列头单源化的前端半边）
  - 产出：`rts-panel-cloud/web/src/lib/cols.ts`
  - 落点：`design.md §3.7`（D7）
  - 验收：组件中**无列名/列标签字符串硬编码**（grep 断言）；列变化只改 `scanner/view/model.py` 即生效
- [ ] **T1.4** 五区块表格组件（合并实现，吃 `colSpec` 投影）
  - 产出：`rts-panel-cloud/web/src/components/RegionTable.tsx`
  - 落点：`design.md §2.2（合并理由）§7.1 §7.2`
  - 验收：列数 == `colSpec` 长度；空区块显 `—（本轮无数据）`；红涨 `#ef232a` / 绿跌 `#14b143`；行高 28px；点击行进详情（占位路由）
- [ ] **T1.5** 状态条 + **滞后 N 轮提示**（M1 走 `/api/meta` 轮询，M3 换 SSE）   (FR-R2 · S5 · 复核修 10)
  - 产出：`rts-panel-cloud/web/src/components/StatusBar.tsx`
  - 落点：`design.md §7.2`、`design.md §13.1 M1`
  - 验收：显示轮次时间/seq/间隔/时段/入库时间；滞后 >1 时变黄 `#f5a623` 且文案 `数据滞后 N 轮`；云端无响应 → 红 + 「云端无响应」且**不白屏**（E14）
- [ ] **T1.6** Dashboard 页 + 静态托管（Pages）
  - 产出：`rts-panel-cloud/web/src/pages/Dashboard.tsx`、部署配置
  - 落点：`design.md §7.1 §11.2`
  - 验收：首屏 ≤1.5s（Lighthouse）；五区块齐全、顺序与终端一致；页脚含「只读 · 不构成投资建议」（G5）
- [ ] **T1.7** ★ **M1 验收门**   (验收 7)
  - 产出：`docs/m1-gate-evidence.md`
  - 落点：`design.md §13.1 M1`
  - 验收：列头**逐列**一致（对比终端截图）、空区块 `—`、红涨绿跌、滞后 N 轮提示、列 key 序列 == `COLS_*`

---

## Phase 2 — M2 鉴权 + 公网   (D2 · R2 · 验收 5/6)

- [ ] **T2.1** Access 应用配置：GitHub SSO，路径 `/*` **排除 `/api/ingest`**
  - 产出：`rts-panel-cloud/docs/deploy.md`（步骤留档 + 截图位）
  - 落点：`design.md §3.2 第 1 层 §11.1`
  - 验收：未登录 `GET /api/regions` → **Access 拦截页**、响应体不含任何行数据；已登录可读（验收 5）
- [ ] **T2.2** **域拓扑收敛**（复核修 7）：`wrangler.jsonc` 设 `"workers_dev": false` + 同域 `routes`
  - 产出：`rts-panel-cloud/worker/wrangler.jsonc`
  - 落点：`design.md §3.2 第 6 层`、`§11.1`
  - 验收：`https://cf-panel-api.<sub>.workers.dev/api/regions` **不可达**（404/无服务）；`panel.<域名>/api/regions` 经 Access 可达；前端 `fetch('/api/...')` 无跨域（生产 `WEB_ORIGINS` 留空）
- [ ] **T2.3** WAF 规则（非 POST `/api/ingest` → Block；速率 ≤6/min）+ 密钥卫生检查
  - 产出：`rts-panel-cloud/docs/deploy.md`（WAF 规则文本）、CI 检查脚本
  - 落点：`design.md §3.2 第 3/5 层`
  - 验收：非 POST → 403；连打 7 次 → 限速命中；`git log -S` 全历史无密钥；`panel_report.log` 无密钥（抽 10 行）
- [ ] **T2.4** 密钥轮换流程演练（`wrangler secret put` → 改 `.env` → 重启）
  - 产出：`rts-panel-cloud/docs/deploy.md`（轮换章节）
  - 落点：`design.md §3.2 密钥轮换`
  - 验收：换钥后旧钥 401、新钥 201；轮换窗口内**只丢轮次不降级**，扫描无异常
- [ ] **T2.5** ★ **M2 验收门**
  - 产出：`docs/m2-gate-evidence.md`
  - 落点：`design.md §13.1 M2`
  - 验收：验收 5（未登录拿不到数据）+ 验收 6（错密钥 401 且**数据未被污染**）+ `workers_dev` 旁路已封 + git 历史无密钥

---

## Phase 3 — M3 SSE + 状态条实时化   (D8 · FR-R1/FR-R2)

- [ ] **T3.1** `/api/stream`：15s 单常量（查询即心跳）+ `MAX_POLLS=40`（=10min）+ 主动 `bye` 收口   (D8 · 复核修 4/2)
  - 产出：`rts-panel-cloud/worker/src/routes/stream.ts`
  - 落点：`design.md §3.8 §6.3`（修正后代码 + 常量耦合说明）
  - 验收：集成测试 `assert d1QueryCount <= 40`；单连接 10min 内必收到 `event: bye`；全程 **0 次 524**；轮询查询用 `ORDER BY date DESC, time DESC`
- [ ] **T3.2** `subscribeSSE` 客户端封装：`bye` → 主动 `close()` + 2s 重建；重连后**先全量对齐**再等增量   (FR-R1)
  - 产出：`rts-panel-cloud/web/src/api/client.ts`（`subscribeSSE`）
  - 落点：`design.md §3.8 前端行为 §6.3`
  - 验收：断网 10s 恢复 → **10s 内**数据对齐、滞后归 0（验收 4 / S1）；`bye` 与普通断线两条路径分别覆盖（单测 mock）
- [ ] **T3.3** 连接异常降级：403/非 SSE 内容 → 停订阅 + 「请重新登录」(E7)；云端超时 → 保留最后快照 (E14)
  - 产出：`rts-panel-cloud/web/src/api/client.ts`、`web/src/components/StatusBar.tsx`（状态位）
  - 落点：`design.md §8 E7/E14`
  - 验收：cookie 过期场景下不出现无限重连风暴（断言重连次数上限）；状态条文案与 §8 一致
- [ ] **T3.4** 预算核对（请求 + CPU）
  - 产出：`docs/m3-gate-evidence.md`（Cloudflare Analytics 截图 + Workers Logs）
  - 落点：`design.md §9 §3.8`
  - 验收：日请求 ≈10,400（≤100,000 的 15%）；Workers Logs CPU p95 <5ms（`exceededCpu` 0 次）
- [ ] **T3.5** ★ **M3 验收门**
  - 落点：`design.md §13.1 M3`
  - 验收：单连接 D1 查询 ≤40；**10 分钟无 524**；断线 10s 恢复对齐（设计判据三条全中）

---

## Phase 4 — M4 个股详情   (DR-9 · FR-D1 · 验收 9)

- [ ] **T4.1** `GET /api/stock/{symbol}/kline`：正则校验 + `days` 上限 + 缺票 404
  - 产出：`rts-panel-cloud/worker/src/routes/stock.ts`
  - 落点：`design.md §4.4 §8 E9/E10`
  - 验收：`symbol` 非 `^\d{6}$` → 400 `BAD_REQUEST`；`days>120` → 400 `RANGE_EXCEEDED`；无行 → 404 `NOT_IN_REPORT` 且文案「本轮上报未包含该票」；**零外网补拉**（G5）
- [ ] **T4.2** **增量收集器落地**（进程内 `sent` 表 + `daily_kline` 切片）   (FR-C2 规则 5 · 复核修 8)
  - 产出：`scanner/panel_reporter.py`（扩展 `pending_stocks` 组装）
  - 落点：`design.md §3.1「增量收集器」5 条`
  - 验收：每轮 ≤20 票、新票（`isNewEntry`）优先；无 K 线的票跳过且不阻塞；**上报成功才更新 `sent`**；重启后 3 轮内收敛（50÷20）—— 单测断言 `len(stocks) <= 20` 与重发逻辑
- [ ] **T4.3** 详情页：ECharts 双 grid（K 线 + 成交量）+ 评分分解 + 出现史
  - 产出：`rts-panel-cloud/web/src/pages/Stock.tsx`、`web/src/components/RowDetail.tsx`
  - 落点：`design.md §7.1 §7.3`（ECharts 关键配置：`[open,close,low,high]` 顺序、双 xAxis/yAxis 都要配）
  - 验收：点任意行 → **≤1.5s** 渲染完成（验收 9）；各项之和 == 总分；非 v1 票显「无评分分解」；`kline_cache` 缺票显 E10 文案
- [ ] **T4.4** ★ **M4 验收门**
  - 产出：`docs/m4-gate-evidence.md`
  - 落点：`design.md §13.1 M4`
  - 验收：任意行可跳；非法 400 / 缺票 404 文案准确；K 线 + 分解 + 出现史齐全；详情 1.5s

---

## Phase 5 — M5 历史 + 过门 + 保留策略收尾   (D5/D6 · FR-D2/FR-V3 · R1 · 验收 8/10)

- [ ] **T5.1** `GET /api/history?date=` + `GET /api/meta/dates` + `GET /api/history/rounds?date=`（P2）
  - 产出：`rts-panel-cloud/worker/src/routes/history.ts`（`meta.ts` 扩展 dates）
  - 落点：`design.md §4.4`
  - 验收：取该日 `MAX(time)`；无 → 404 `NO_SNAPSHOT`；`meta/dates` 只返回**有快照日**；每请求 ≤1 查询
- [ ] **T5.2** 日历 + 历史页（禁用无快照日）
  - 产出：`rts-panel-cloud/web/src/pages/History.tsx`
  - 落点：`design.md §7.1 §7.3`
  - 验收：无快照日不可点（S2）；选历史日 → 五区块完整复原、列头与终端一致
- [ ] **T5.3** 飞书过门区（本地 `_gate` 上报已随 T0.1；本任务做前端两态开关 + 固定标题）   (D6 · FR-V3 · 验收 8)
  - 产出：`rts-panel-cloud/web/src/components/GatePanel.tsx`
  - 落点：`design.md §3.6 §7.2`
  - 验收：标题**固定含**「下一张卡会推什么（非已推内容）」；`passed/total=12/40` → 显示 `通过 12 · 剔除 28` + `gate.stats` 7 字段；切「门内」只换数据源**不改标题**；云端**不重算门**（grep 断言无 `config_push`）
- [ ] **T5.4** 保留策略 `cron.ts`（R1=60 轮 + 历史日各 1 + 超 10 交易日整删；分批 `LIMIT 1000` 到 `changes==0`，上限 20 轮）   (D5 · 复核修 3)
  - 产出：`rts-panel-cloud/worker/src/cron.ts`、`worker/wrangler.jsonc`（`triggers.crons` 每小时 :07）
  - 落点：`design.md §3.5 §6.4 §8 E13`（修正后的 SQL：`(date,time) NOT IN (SELECT date, MAX(time) ...)`）
  - 验收：集成测试插 100 今日行 + 5 历史日 → 跑 cron → **今日剩 60、历史日各 1**（R1 断言）；**两日 `MAX(time)` 相同的构造**也删净（复核修 3 回归）；20 次循环上限生效；死循环 0
- [ ] **T5.5** 存储与预算终核
  - 产出：`docs/m5-gate-evidence.md`
  - 落点：`design.md §9`（26MB / 10.4% 两行）
  - 验收：`SELECT count(*)` 今日 == 60；D1 用量 <100MB（目标 26MB）；日请求 <20k；cron 后无残留超期行（验收 10）
- [ ] **T5.6** 降级与回滚演练
  - 产出：`docs/deploy.md`（回滚章节）
  - 落点：`design.md §11.3`
  - 验收：`RTS_PANEL=0` → 无上报、**行为与当前版本一致**（验收 1）；删 `.env` 两键即停；`wrangler rollback` 可回上一版；云端挂 → 面板保留最后快照不白屏
- [ ] **T5.7** ★ **M5 验收门**
  - 落点：`design.md §13.1 M5`
  - 验收：今日仅 60 轮、历史日各 1、总量 <100MB、过门标题断言通过、`RTS_PANEL=0` 行为一致

---

## Phase 6 — 集成验收（可演示脚本）   (验收标准 1~10 · NFR)

- [ ] **T6.1** 验收脚本：把需求 §十一 的 10 条 GIVEN/WHEN/THEN 落成可执行断言（无法自动化的标注人工步骤）
  - 产出：`tests/test_panel_acceptance.py`（本地侧 1/2/3/4/6）+ `rts-panel-cloud/tests/acceptance.md`（云端侧 5/7/8/9/10）
  - 落点：`requirements §十一`、`design.md §13.2 追溯矩阵`
  - 验收：10 条逐条有落点 —— 自动断言跑绿 / 人工项有签字栏；**禁止空断言**（AGENTS.md 测试纪律）
- [ ] **T6.2** 验证顺序全跑（本仓库 + 云端）
  - 产出：`docs/m6-gate-evidence.md`
  - 落点：`design.md §10 §11.4`、AGENTS.md `Verification order`
  - 验收：`ruff check` → `mypy scanner/` → `pytest tests/` → 云端 `pnpm -r test` 全绿；本面板**不改评分**，`python -m scanner.rule_validate` 跑一次留证（预期「证据不足/翻转 0 天」属正常，写进证据）
- [ ] **T6.3** 文档收口（契约与实现一致性抽查）
  - 产出：`rts-panel-cloud/docs/api.md`、`rts-panel-cloud/docs/deploy.md`、`docs/ui-panel-design.md`（状态 → v1.2 定稿）
  - 落点：`design.md §4 §11`
  - 验收：抽查 10 项一一对应（端点 7 个、`SCHEMA_VERSION`、常量 `STOCK_QUOTA/MAX_POLLS/POLL_INTERVAL`、表 2 张、开关 3 个、列 spec 来源）；文档与实现不符 = 不通过

---

## 依赖与并行

**Phase 顺序（严格串行门禁）**

`Phase 0 → 1 → 2 → 3 → 4 → 5 → 6`，其中 **Phase 0 是硬门**（T0.9 不过不进 Phase 1，需求 §十明文）。

**可并行的工作**

| 并行组 | 内容 | 说明 |
|---|---|---|
| **P0 内部** | 本地侧 `T0.1+T0.2+T0.3+T0.4` ⇄ 云端侧 `T0.6+T0.7+T0.8` | 共享前置 = **契约 §5.1 + `SCHEMA_VERSION=2` 先定**；两侧可同时开工 |
| **P1/P2 骨架** | `T1.1+T1.2`（前端工程，mock 数据）⇄ `T0.6` 之后的云端 | 不阻塞 M0 门禁 |
| **跨 Phase 早做** | `T4.2`（增量收集器，只依赖 `T0.3`） | 可与 Phase 2/3 并行；`T5.4`（cron，只依赖 `T0.6` 的表）同理 |
| **纯函数资产** | `panel_serialize.py` / `cols` 投影 / `cron.ts` SQL | 零网络、零 UI 依赖，最早进 CI |

**先后依赖（不可颠倒）**

- `T0.5` 依赖 `T0.3`（挂点要用 `report`）；`T0.9` 依赖 `T0.1~T0.8` 全部
- `T1.4/T1.6` 依赖 `T0.8`（要有 `/regions` 才有真数据）
- `T2.1`（Access）依赖 `T1.6`（先有域名/静态托管）
- `T3.2` 依赖 `T2.1`（SSE 重连带 Access cookie）；`T3.1` 依赖 `T0.8`（轮询要查表）
- `T4.1` 依赖 `T0.7`（`kline_cache` 有数据才有得读）
- `T5.1` 依赖 `T0.6`（表 + 索引）；`T5.4` 依赖 `T0.6`，**不依赖** Phase 1~4

**共享前置**

- 契约 `docs/ui-panel-design.md §5.1` + `SCHEMA_VERSION = 2`（双端同值，改契约必须同步两端 + 提版本）
- `rts-panel-cloud/docs/deploy.md`：Access/WAF/密钥轮换的唯一记录载体，`T2.1` 前先建文件

**解耦收益**

- `scanner/panel_serialize.py` 无网络、无云端依赖 → 可先行合并、独立 CI
- 云端路由按资源拆分（§2.2），`stream.ts`/`cron.ts` 因触发方式不同而独立 → 可单独 review 与回滚

---

## Definition of Done

**每个任务通用**

1. **有 `design.md §` 落点**：任务描述里能指出它实现的是设计的哪一节；若实现与设计不符，**先改设计再改代码**（禁止任务级决策漂移）。
2. **产出文件路径精确**：`scanner/*.py` / `tests/*.py` / `worker/src/**/*.ts` / `web/src/**`，评审按文件走。
3. **验收点可量化**：有数字（≤50ms、==60 行、≤40 次、1.5s）或有可执行断言；**禁止空断言**（依赖 mock 的必须 `assert` 调用发生）。
4. **不越界**：不引入 `httpx`（R4）、不改 `run_scanner` 既有逻辑（只加挂点）、不产生第二个结论源（G5）、不新增评分/阈值常数。

**分模块额外要求**

- **纯函数模块**（`panel_serialize.py`、`cols` 投影、`cron.ts` SQL、`ingest` 校验）：必须有单测，覆盖**边界与负面**（非法 symbol、超配额、跨日保留、schema 不符）。
- **契约双端**（`panel_serialize.py` ⇄ `serialize.ts`）：字段集相等的契约测试；`SCHEMA_VERSION` 不同值即失败。
- **上报器**：必须证明「异常不上抛」「不阻塞主循环」「日志无密钥」三条（T0.4 + T0.9 抽查）。
- **鉴权**：每个鉴权层都有对应的**绕过尝试**用例（401 / 拦截页 / workers_dev 404 / WAF 403）。
- **交付前**：`ruff check` → `mypy` → `pytest tests/` → 云端 `pnpm -r test`；涉及任何阈值/常数改动补跑 `python -m scanner.rule_validate` 并留证。
- **每个 Phase 有 evidence 文档**（`docs/mN-gate-evidence.md`），M0/…/M5 五道门的判据来自 `design.md §13.1`，缺 evidence 视为该 Phase 未完成。
