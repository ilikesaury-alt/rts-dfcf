# rts-dfcf-max Web 面板 · 需求规格说明书（v2.1 · Cloudflare 版）

| 元信息 | 值 |
|---|---|
| 文档类型 | 需求规格说明书（comprehensive 模式） |
| 版本 | **v2.1（2026-09-29，复核定稿）** — v1 为「本地 FastAPI 读本地库」，已废弃 |
| 目标项目 | `D:\everything\rts-dfcf-max`（A 股创业板扫描器，Python 3.12，Windows 本地运行） |
| 目标形态 | **本地扫描器上报 → Cloudflare Workers/D1 → Pages 前端，公网可访问 + 鉴权** |
| 目标平台 | Cloudflare Free 档（Workers / D1 / Pages）+ 浏览器 |
| 关键变更 | 数据通路由「本地进程读 `scanner.db`」改为「**本地上报 → 云端只读**」；访问范围由 127.0.0.1 改为**公网 + Cloudflare Access** |
| 文档状态 | **已评审定稿**（评审决策见下方「评审定稿项」） |

### 评审定稿项（2026-09-29 用户批准，不得在实现中擅自改动）

| # | 决策 | 定稿 |
|---|---|---|
| R1 | 当日快照保留轮数 | **60 轮** |
| R2 | Access 认证方式 | **GitHub SSO**（不使用邮箱 OTP） |
| R3 | 云端仓库关系 | **独立仓库**，不并入 `cf-stack-starter` |
| R4 | HTTP 客户端 | **复用已有 `requests>=2.28`，不新增 `httpx`** |

---

## 一、项目概述

### 1.1 背景

项目当前只有两个输出出口：终端 ANSI（`scanner/view/render.py`）与飞书卡片（`scanner/feishu.py`），
均为快照式纯文本，无法承载表格排序、K 线可视化与跨日回看。v1 设计的本地 Web 面板
（FastAPI + 本地 SQLite）在评审时被改为**云端面板**，理由：需要在任何设备查看、不依赖扫描机常开。

**架构含义**：`scanner.db` 与 `ScanView` 留在本地，云端只保存**上报的只读副本**。
扫描器从「本机程序」变成「**云端面板的数据源**」，因此新增一条**跨网络、跨进程、可能失败**的数据通路
——这是 v2 相对 v1 的全部复杂度来源。

### 1.2 目标（可度量）

| # | 目标 | 度量指标 |
|---|---|---|
| G1 | 四区块 + 过门区在浏览器可读 | `v1 池选` / `v1 回捞` / `飙升A` / `榜外B` / `飞书过门` 五区全渲染，列头与 `COLS_*` 逐列一致 |
| G2 | 接近实时 | 上报完成到面板可见 **≤ REFRESH_INTERVAL + 10s**（默认 70s 内）。**注**：仅指成功路径；上报失败重试期间允许滞后，但必须由状态条显式标出（S5），不得静默 |
| G3 | **零侵入 + 失败隔离** | 上报耗时 **≤ 50ms/轮**（不含网络等待，网络走后台线程）；**网络失败/超时/云端 5xx 一律不上抛**，扫描行为与 v1 逐字节一致 |
| G4 | 可下钻 | 任意行进详情页：K 线 + 成交量 + `score_breakdown`，首屏 ≤ 1.5s |
| G5 | 不产生第二结论源 | 无短名单、无综合判断、无「建议持仓」；只展示已有证据 |
| G6 | 成本受控 | 全程落在 **Cloudflare Free 档**；请求量 ≤ 10 万/日 的 15%，D1 存储 ≤ 100 MB（上限 500 MB） |
| G7 | 公网不失守 | 所有读接口在 Cloudflare Access 之后；上报接口用独立密钥；无鉴权不可读任何数据 |

### 1.3 范围

**In（本期）**

1. 本地**上报器**（新模块）：每轮把 `ScanView` 序列化后 POST 到 Workers
2. Workers API：`/api/ingest`（写，带密钥）+ 只读查询接口 + SSE
3. Pages 前端：五区块表格、个股详情、历史按日、状态条
4. Cloudflare Access 鉴权 + 上报密钥管理
5. D1 表结构与保留策略（含存储预算）

**Out（明确不做）**

- ❌ 面板写回扫描器（不改配置、不触发重扫、不写 `scanner.db`）
- ❌ 交易下单、提醒、账号体系（Access 只管「谁能看」）
- ❌ 扫描器整体上云（依赖 `pywencai`/`akshare`/本地 kline 缓存与 60s 轮询，**在 Workers 免费档跑不起来**，论证见附录 A.5）
- ❌ 飞书/Resend 邮件告警（本期只做可视）
- ❌ 手机端精细适配

---

## 二、术语与缩写

| 术语 | 含义 |
|---|---|
| **ScanView** | 视图 dataclass（`scanner/view/model.py:570`），四区块数据唯一载体 |
| **MainRow** | `v1 池选` 行（`model.py:547`）：`entry/score/composite_score/cat_label/pct/current/sector/is_new_entry` |
| **四区块** | `v1 池选`、`v1 回捞`、`沪深飙升·极有可能大涨`（A 段榜内 + B 段榜外） |
| **飞书过门区** | `render._render_push_gate_region`，展示**下一张飞书卡会推什么**（非已推内容） |
| **push_gate** | 飞书严格过滤门（`scanner/push_gate.py:319`），**只作用于飞书**，终端与本面板默认都不套 |
| **上报（ingest）** | 本地扫描器每轮把序列化后的 `ScanView` POST 到 Workers `/api/ingest` |
| **快照（snapshot）** | 云端 `snapshots` 表中的一行 = 某轮的完整五区块 JSON |
| **seq** | 本地单调递增轮号，作快照主键与幂等键 |
| **SSE** | Server-Sent Events，服务器→浏览器单向推送，`EventSource` 自动重连 |
| **Access** | Cloudflare Zero Trust 的 Access：在 Worker 前做身份校验，未登录返回拦截页。**认证方式定稿为 GitHub SSO（R2）** |
| **Service Token** | Access 的机器对机器凭据。**本期不使用**（上报走 `INGEST_SECRET`），仅列为 R2 的备选加固 |
| **WAL** | SQLite 预写日志模式（本项目 `schema.py:50` 已开），本地多进程读写不互斥 |
| **类别先验** | `config_scoring.CATEGORY_HIT_RATE`，系统唯一口径（次日≥7% hit） |
| **kNF/MOM/NEW/RBD/ST** | known_new_face / momentum / new_face / rebound / short_term |
| **v2 池选（pool_pick）** | 已于 2026-09-28 退池，面板**不得显示**（占位行须显式标注「已退池」）。**注**：此处「v2」是项目内的池代号，与本文档版本号无关 |

---

## 三、系统总体架构

### 3.1 运行形态

```text
┌──────────────────────────────────────────────────────────────┐
│ 本机 Windows（数据源，行为不变）                               │
│  unified_scanner.py  ── 每 60s 一轮                           │
│   scan_with_raw → build_scan_view → render_terminal → 飞书    │
│   ★ 新增：panel_reporter（后台线程）                           │
│       序列化 ScanView → HTTP POST /api/ingest                 │
│       ├─ 成功：记 logs/panel_report.log                       │
│       └─ 失败：重试 2 次（退避 1s/3s）后丢弃 + 记日志（绝不阻塞）│
└──────────────────────────────┬───────────────────────────────┘
                               │ ① 上报（Bearer: INGEST_SECRET）
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ Cloudflare                                                    │
│  Workers  cf-panel-api（Hono）                                │
│   ├─ POST /api/ingest     ← 上报写入（**不属 Access 覆盖**，只凭 INGEST_SECRET）│
│   ├─ GET  /api/regions    当前轮五区块（D1 只读）              │
│   ├─ GET  /api/history    某日快照                            │
│   ├─ GET  /api/stock/*    个股 K 线/评分分解/出现史            │
│   ├─ GET  /api/meta       轮次/时段/健康/可用日期              │
│   └─ GET  /api/stream     SSE（15s 心跳，避开 100s→524）       │
│                                                                │
│  D1  cf-panel-db   snapshots / kline_cache                     │
│  Pages  前端静态资源（React + Vite + ECharts）                  │
│  Access（**GitHub SSO**）拦截全部 GET；/api/ingest 不在其覆盖内  │
└──────────────────────────────┬───────────────────────────────┘
                               │ ② HTTPS + Access 会话
                               ▼
                        浏览器（任意设备）
```

### 3.2 一轮的数据流

```text
本地轮完成（t=0）
  → 后台线程序列化（≤50ms）→ POST /api/ingest
      → Worker 解析 + D1 batch 写入（幂等键 date+time）
  → Worker 广播 SSE 事件 {seq,date,time}
      → 浏览器 EventSource 收到 → GET /api/regions → 局部重渲
（断线自动重连；心跳 15s；闭市降频为 meta 事件）
```

### 3.3 关键架构决策

| 决策点 | 选择 | 依据（调研，见附录 A） |
|---|---|---|
| 面板在哪跑 | **云端（Workers + D1 + Pages）** | 用户要求公网可访问；本地方案（v1）只在扫描机可看 |
| 数据怎么上去 | **每轮上报快照** | `build_scan_view` 的 `hot_rows/hist_rows/offboard_rows/new_symbols/weak` 是**内存态**，DB 里没有；不上报就无解（D1） |
| 上报失败怎么办 | **丢本轮 + 记日志，不阻塞扫描** | 扫描是生产者，面板是旁路消费者；遵循项目 fail-open 设计原则 |
| 鉴权 | **读：Access（GitHub SSO）；写：`INGEST_SECRET` Bearer** | Access 在 Worker 前拦截，Worker 无需自建登录/会话；`/api/ingest` **不属 Access 覆盖**，由独立密钥把关（R2） |
| 推送协议 | **SSE**（备选：15s 轮询） | Workers HTTP 请求**无时长硬限制**（官方 Limits），可流式；单向推送够用；WebSocket 不需要 |
| 前端托管 | **Pages（或 Workers Static Assets）** | 构建产物静态；与 `cf-stack-starter` 同构，可复用其 `wrangler.jsonc`/CORS/错误信封骨架 |
| 存储 | **D1 而非 R2/KV** | 需要按 `date` 查、幂等 upsert、`LIMIT` 分页；R2 只做可选冷备 |
| 重计算放哪 | **不上云**（本期内） | 扫描器本体依赖本地环境；Cloudflare Container/Hyperdrive 见附录 A.6，本期不引入 |

---

## 四、User Scenarios

### S1 · 移动端盯盘（P1）
**Why**：人在外面，扫描机不在手边。
**验收**：手机浏览器经 Access 登录后，70s 内看到最新五区块；断网恢复自动对齐。

### S2 · 盘后复盘（P1）
**验收**：日历选历史日，五区块完整复原，列头与终端一致；无快照日不可选。

### S3 · 单股深挖（P2）
**验收**：点行 → K 线 + 成交量 + 评分分解 + 出现史 ≤1.5s。

### S4 · 校验「下一张卡推什么」（P2）
**验收**：过门区显示 `通过/剔除` 计数，标题含「下一张卡会推什么（非已推内容）」。

### S5 · 上报中断的可感知性（P1，v2 新增）
**Why**：数据是上报来的，可能断。
**验收**：本地断网 3 轮后，面板状态条显示「数据滞后 N 轮」而非静默显示旧数据冒充最新。

---

## 五、功能需求

> 通用约束：**面板全程只读**；不新增结论；列头单源自 `scanner/view/model.py` 的 `COLS_*`；中文界面；
> 所有读接口在 Access 之后；所有错误返回 `{ok:false,error:{code,message}}`。

### FR-C1 本地上报器（新模块 `scanner/panel_reporter.py`）

| 项 | 值 |
|---|---|
| 触发 | `run_scanner` 每轮 `build_scan_view` 成功后（**紧跟 `render_terminal` 之后**，保证终端/飞书/云端三处同源） |
| 开关 | `RTS_PANEL=0` 关闭（默认开，但**未配置 URL/密钥时静默不启动**）；`--no-panel` CLI |
| 端点 | `RTS_PANEL_URL`（如 `https://cf-panel-api.<sub>.workers.dev/api/ingest`） |
| 密钥 | `RTS_PANEL_SECRET`（放 `.env`，**不进 git**；对应云端 `wrangler secret put INGEST_SECRET`） |
| 载荷 | 见 FR-C2；目标体积 **≤ 400 KB**（regions+ctx ≤200 KB + 增量 stocks ≤200 KB；D1 单行硬限 2 MB，留 5 倍余量） |
| 超时 | 连接 **3s**、总 **8s**；失败重试 **2 次**（退避 1s/3s），仍失败 → **丢弃本轮** |
| 性能 | 序列化 **≤ 50ms/轮**（G3）；网络 I/O 在**后台线程**，主循环不 `await` |
| 失败 | 任何异常**捕获后只写 `logs/panel_report.log`**，不向上抛（不触碰 `EXTERNAL_FAILURES` 之外的冒泡路径） |
| 幂等 | 同 `date+time` 重复上报 = 覆盖（D1 upsert），不产生重复行 |

### FR-C2 上报载荷（契约，本地与云端共同遵守）

```jsonc
{
  "seq": 18231,                    // 本地单调轮号
  "date": "2026-09-29", "time": "14:32:05",
  "durationMs": 4120,
  "schema": 2,                     // 载荷版本，云端不识别 → 400 并告警
  "regions": {                     // 五区块（已按服务端顺序排好）
    "main": [ /* MainRow 扁平化 */ ], "hist": [ ... ],
    "hot": [ ... ], "offboard": [ ... ],
    "gate": { "main": [...], "stats": { "total":40, "passed":12, "tierA":3,
              "tierB":6, "tierCFallback":3, "vetoed":18, "droppedNoFallback":10 } }
  },
  "ctx": { "marketIdxPct": 0.42, "weak": false, "flowFiltered": 7,
           "gemTotal": 128, "warnings": [ ... ] },
  "stocks": {                      // **增量**：只带新出现/过期的票（见下）
    "300319": { "kline": [[date,o,c,h,l,v], ...≤120根],
                "appearances": [ ... ] }
  }
}
```

序列化规则（**唯一收口在 `scanner/panel_serialize.py`**）：

1. `breakout_mark` / `beauty_mark` 的 `tuple[str,str]` 键 → `"sym|cat"` 字符串（`json.dumps` 会直接 `TypeError`）
2. `MainRow.entry._candidate` 是活对象 → **剥离**，只留标量；`score_breakdown` 解析后的 dict 原样带
3. 三类 Candidate 是 dataclass → `dataclasses.asdict`
4. 对外字段 **camelCase**；本地↔云端映射只在 `panel_serialize.py`，前端不得假设 snake_case
5. **`stocks` 必须是增量，不是全量**：四区块去重后 30~62 只/日（极端可到 150 只），
   若每轮全带 120 根 K 线 = 0.24~1.2 MB/轮，**直接打穿 400 KB 目标与 CPU 预算**。
   规则：只带 `本轮新出现` + `kline_cache 中该票日期已过期` 的票，**每轮 ≤ 20 票**（≈160 KB）；
   超出配额按「新出现优先」排序截断，其余票的 K 线由**历史轮次**已写入的 `kline_cache` 提供
6. **`breakdown` 不冗余上报**：它只对 `v1 池选` 票有意义（`HotCandidate` 无此字段），
   直接读 `regions.main[].entry.score_breakdown`；详情页对其它区块的票显示「无评分分解」

### FR-C3 云端 D1 表结构

```sql
CREATE TABLE snapshots (                 -- 一表一批，主键即幂等键
  date TEXT NOT NULL, time TEXT NOT NULL, seq INTEGER NOT NULL,
  duration_ms INTEGER NOT NULL DEFAULT 0, schema INTEGER NOT NULL,
  payload TEXT NOT NULL,                 -- FR-C2 的 regions+ctx（不含 stocks）
  ingested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  PRIMARY KEY (date, time)
);
CREATE INDEX idx_snapshots_date_seq ON snapshots (date, seq DESC);

CREATE TABLE kline_cache (               -- stocks 单独拆表：避免单行过大
  symbol TEXT NOT NULL,
  kline_date TEXT NOT NULL,              -- 该票最新一根 K 线日期（非快照日）
  kline TEXT NOT NULL,                   -- ≤120 根，约 8 KB
  appearances TEXT,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  PRIMARY KEY (symbol, kline_date)       -- 同票同日 upsert，不累积
);
```

- **D1 硬约束（官方 Limits，2026-04 版）**：单行/单字段 **2,000,000 bytes**、单条 SQL **100 KB**、
  bound 参数 **100/查询**、每次 Worker 调用 **50 次查询（Free）**、单库 **500 MB（Free）**
- 上报写入用 `db.batch([...])`，**每批 ≤ 20 条语句**（50 次调用上限的自我约束）
- **存储预算（G6）**：快照只存 `regions+ctx`（估 100~300 KB/轮）

| 项 | 估算 |
|---|---|
| 当日保留最近 **60 轮（R1）** × 300 KB | 18 MB |
| 历史日**只留最后一轮** × 10 交易日 | 3 MB |
| K 线（增量）60 票 × 10 日 × 8 KB | 4.8 MB |
| **合计** | **≈ 26 MB**，占 500 MB 上限的 **5.2%**（G6 的 100 MB 红线还有 4 倍余量） |

- 保留策略由云端 cron（每小时）执行，**口径（R1 = 60 轮）**：
  1. 每个 `date` **只保留 `time` 最大的最后一轮**（历史日）；
  2. **今日**只保留 `time` 最大的 **60 轮**，更早的删；
  3. `date` 超过 **10 个交易日** → 整日全删
  （`seq` 跳号不影响保留，保留只按 `time` 排序）
- 迁移仍走**版本化清单末尾追加**（若复用本地 `migrations.py` 风格；云端用 `migrations/000N_*.sql` 且 **id 不改名不删**）

### FR-V1 五区块表格渲染

| 区块 | 数据 | 列头（单源） |
|---|---|---|
| ◆ v1 池选 | `regions.main` | `COLS_POOL`：`#/代码/名称/涨幅/5日累计/现价/排名/板块/评分/策略` |
| ◆ v1 回捞 | `regions.hist` | `COLS_HIST`：`#/代码/名称/涨幅/5日累计/现价/量比/距v1/评分/上次v1桶` |
| ◆ 飙升 A 段 | `regions.hot` | `COLS_HOT`：`#/代码/名称/涨幅/5日累计/现价/排名上升/成交量/成交额/量比/换手%/市值(亿)/板块/评分/连击` |
| 同节 B 段 | `regions.offboard` | `COLS_HOT`（不适用列以 `—` 占位，**不改列头顺序**） |
| ◆ 飞书过门 | `regions.gate` | 复用区内列 |

- 空区块显示 `—（本轮无数据）`，**不折叠**（用户要区分「空」与「没加载」）
- `is_new_entry` 行首 `●`；配色 **红涨绿跌**（`#ef232a` / `#14b143`）
- **数据滞后指示**：`当前 seq` 与 `上一次已知 seq` 差 >1 → 状态条转黄显示 `数据滞后 N 轮`（S5）

### FR-V2 排序（严格复用，不新增）

- `v1 池选`：直接读上报时**已排好**的顺序（`assemble.py:419` 键链），**云端不得重新排序**
- A 段 `(-score,-rank_change)`；B 段 `offboard_watch.sort_key` —— 同理
- 允许对「评分/涨幅/量比」三列**客户端临时排序**，表头标注「临时排序」，刷新即还原

### FR-V3 飞书过门区（语义强约束）

- 标题固定：`◆ 飞书过门 · 下一张卡会推什么（非已推内容）`
- 必显 `gate.stats` 全部 7 个字段，含**剔除数**（对等「卡片强制打出剔除数」的不变式）
- 开关：`全量（同终端）` / `门内（同飞书）`，**默认全量**

### FR-R1 SSE 实时推送

| 项 | 值 |
|---|---|
| 端点 | `GET /api/stream`，`text/event-stream` |
| 事件 | `round`（`{seq,date,time,durationMs}`）、`meta`（时段/滞后）、`stale` |
| 心跳 | **每 15s 发 `: ping`** —— 官方无时长硬限制，但社区实测 **100s 无事件会被 524**，15s 有 6 倍安全边际 |
| 重连 | `EventSource` 自动重连；重连后**先 `GET /api/regions` 全量对齐**再等增量 |
| 降频 | 闭市时只发 `meta`（5min），不发 `round` |
| 预算 | 1 连接 = 1 次请求（事件不计次）；重连才计新请求 |

### FR-R2 状态条

`轮次时间 / seq / 间隔 60s / 时段（交易·午休·闭市）/ 数据滞后 N 轮 / 云端入库时间`
数据源 `GET /api/meta`（读 `snapshots` 最新行的 `seq`/`ingested_at`）。
**滞后算法**：前端持有本地 `ctx.seq`（每轮随快照更新），`滞后 = 本地已知 seq − 云端最新 seq`；
本地上报完全中断时改按时间估算：交易时段内 `ceil((now − 最新快照时间) / 60s)`。
两者都 >1 → 状态条转黄（FR-V1、S5）。

### FR-D1 个股详情（`/stock/{symbol}`）

| 模块 | 数据源 | 说明 |
|---|---|---|
| K 线 + 成交量 | `kline_cache.kline`（`/api/stock/{s}/kline?days=120`） | ECharts `candlestick` + volume 副图，红涨绿跌 |
| 评分分解 | **`regions.main[].entry.score_breakdown`**（随快照走，不在 `kline_cache`） | 三列表格：维度 / 取值 / 贡献分；**仅 `v1 池选` 票有**，其余显示「无评分分解」 |
| 出现史 | `kline_cache.appearances` | 日期、排名、涨幅 |

输入参数表：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `symbol` | string(6) | 是 | 校验 `^\d{6}$`，非法 → 400 `BAD_REQUEST` |
| `days` | int | 否 | 默认 120，范围 1~120（**云端只有 120 根**，超出 → 400 `RANGE_EXCEEDED`） |
| `date` | string | 否 | 默认最新快照日 |

- 票不在 `kline_cache` → **404 `NOT_IN_REPORT`**，文案：「本轮上报未包含该票」
  （**不静默、不去外网补拉**——补拉会产生第二数据通路）

### FR-D2 历史按日浏览

- `GET /api/meta/dates` → 有快照的日期数组（前端日历**只允许选这些**）
- `GET /api/history?date=YYYY-MM-DD` → 该日**最后一轮**快照；无 → 404 `NO_SNAPSHOT`
- `GET /api/history/rounds?date=` → 该日轮次列表（用于回放，P2）

### FR-S1 只读元信息

`GET /api/meta/config` 返回本地 `config` 生效阈值快照（`REFRESH_INTERVAL`、`MIN_SCORE`、
`PUSH_TIER_A_MIN`、`OVERHEAT_ACCUM_MAX`…），**无任何写接口**；由上报器随 `ctx` 附带。

---

## 六、非功能需求

| 类别 | 需求 |
|---|---|
| **可用性** | 上报失败**不影响**扫描（G3）；云端不可用时本地照常跑终端+飞书；面板显示「无数据/滞后」而非白屏 |
| **性能** | 面板首屏 ≤1.5s；端到端 ≤ 70s（G2）；序列化 ≤50ms；API P95 ≤300ms |
| **安全** | 所有读接口在 Access 后；`INGEST_SECRET` 走 `wrangler secret`；本地密钥在 `.env`（gitignored）；载荷**只含公开行情与评分**，不带 `.env`/飞书 webhook/账号信息 |
| **成本** | Free 档：请求数 ≤ 10 万/日 ×15%（G6）；CPU 10ms/请求内；D1 ≤100 MB |
| **可维护** | 列头/排序/阈值单源自 `scanner/`，云端零复制常量；载荷有 `schema` 版本号可灰度 |
| **可测试** | `tests/test_panel_serialize.py`、`tests/test_panel_api.py`（沿用 `tests/test_<module>.py`） |
| **可观测** | 本地 `logs/panel_report.log`（成功/失败/耗时）；云端 Workers Logs + `observability` |
| **降级** | `RTS_PANEL=0` 或未配 URL → 不上报，**与当前版本行为逐字节一致**；Access 挂了 → 本地不受影响 |
| **语言** | 界面中文（与终端/飞书一致） |

---

## 七、数据需求

### 7.1 数据字典

**ScanView（上报主体，字段同 v1）**

| 字段 | 类型 | 说明 |
|---|---|---|
| `main_rows` | `MainRow[]` | v1 池选（已排序） |
| `hist_rows` / `hot_rows` / `offboard_rows` | 各 Candidate 类型 | 回捞 / A 段 / B 段 |
| `flow_pct_map` | `map<symbol,float>` | 资金流占比 |
| `last_ranks` | `map<symbol,int>` | 上轮排名（画 `+N/-N`） |
| `weak` / `market_idx_pct` / `flow_filtered` | bool / float / int | 大盘语境 |
| `warnings` / `rule_result` | str | 体检与规则结果 |
| `breakout_mark` / `beauty_mark` | `map<"sym\|cat",str>` | **tuple 键 → 字符串** |

**MainRow**：`entry{symbol,name,category,score,date,time,percent,concept,first_time,accumulated_pct,trend,live_*}`、`rank`、`accum`、`score`、`composite_score`、`core`、`cat_label`、`pct`、`current`、`sector`、`is_new_entry`

**HotCandidate**：`symbol,code,name,exchange,current,percent,rank_change,rank,volume,amount,market_capital,float_market_capital,turnover_rate,volume_ratio,limit_up,limit_down,status,score,streak,reasons,ff_pct,beauty,sector`

**HistCandidate**：`symbol,code,name,current,percent,vol_ratio,rec_date,rec_days_ago,rec_category,rec_score,cum_pct,market_cap,ff_pct,beauty,score,reasons`

**OffboardCandidate**：同 Hot + `tier,main_pct`，无 `rank`

**K 线条目**：`[date, open, close, high, low, volume]`（ECharts `candlestick` 期望 `[o,c,l,h]`，转换在前端 `api/` 层完成）

### 7.2 存储方案对比（A 选定）

| | **A：D1 双表（snapshots + kline_cache）** | B：整包进 R2（每轮一个 JSON 对象） | C：v1 的本地表 |
|---|---|---|---|
| 查询 | SQL `date`/`seq` 索引、`LIMIT`、幂等 upsert | 需先 `list()` 再 `get()`，无条件查询 | 本地最简单，但**公网看不到** |
| 存储上限 | 500 MB（Free） | 免费额度更大，**但按对象计费与读取计费** | 500 MB 本地 |
| 成本 | 免费档内 | 对象数与读取次数计入 R2 免费额度 | — |
| 结论 | ✅ 结构化查询 + 免费 | ❌ 无 SQL、翻页麻烦 | ❌ 不满足公网需求 |

### 7.3 复用的本地查询（不上报的部分不进云端）

`scanner/db/queries.py` 的 `get_today_recommendations`、`get_symbol_appearances`、
`get_cached_klines`、`get_market_cap_cache`、`get_concepts_cache`、`get_fund_flow_pct_map`
——**在上报器侧消费**，组装进 FR-C2 载荷；云端不直连本地库。

---

## 八、难点与技术攻坚点（按难度排序）

### D1 ★★★ 运行态数据的跨网上报（最大不确定性）

- **问题**：`hot_rows / offboard_rows / hist_rows / new_symbols / weak / rank_map`
  只存在于 `run_scanner` 局部变量，**DB 无法复原**；面板又在云端，中间隔着**可能失败的公网**
- **攻坚**：
  1. **M0 验证**：每轮上报（FR-C1/C2），比对 Web 与终端逐条一致
  2. 失败策略**分级**：网络失败 → 丢本轮 + `seq` 保留（云端 seq 会跳，面板据此判滞后）
  3. 兜底：`RTS_PANEL=0` 或上报长期失败 → 面板状态条显示**「上报已关闭/中断」**，
     **不拿陈旧快照冒充最新**（S5 同源要求）
- **验证**：连续 3 轮 Web vs 终端行数/首行 symbol/顺序一致；拔网线 10 分钟后扫描无异常

### D2 ★★★ 公网化的安全面（v1 没有的新难点）

- **问题**：数据从「只在本机」变成「公网可读」；上报接口是**开放写入端点**，一旦密钥泄露，
  任何人都能往面板灌假数据（**污染的是你的决策依据，比泄露更危险**）
- **攻坚**：
  - 读：**Cloudflare Access + GitHub SSO（R2 定稿）**（免费档 ≤50 用户），Worker 依赖 `Cf-Access-Jwt-Assertion`
  - 写：`/api/ingest` **不属 Access 覆盖范围**，改用 `INGEST_SECRET`（Bearer）+ Cloudflare WAF 规则限来源；
    Service Token 仅作 R2 的备选加固，**本期不启用**
  - 密钥轮换：`wrangler secret put` 覆盖 + 本地 `.env` 同步，**禁止出现在日志**
  - 载荷**只含公开行情**（无隐私），但要在 FR-C2 层面禁止塞入任何 `.env` 内容
- **验证**：无 Access 会话访问 `/api/regions` → 403 拦截页；错密钥上报 → 401；`git log -S` 查密钥

### D3 ★★ 序列化的隐性类型（tuple 键 / TypedDict / `_candidate`）

- **问题**：`json.dumps` 遇 `tuple` 键直接 `TypeError`；`_candidate` 是活对象
- **攻坚**：手写 `panel_serialize.py` + 往返单测；**禁止**无脑 `asdict` 一把梭
- **验证**：`tests/test_panel_serialize.py` 用 `tests/test_feishu.py:88` 的 ScanView 桩

### D4 ★★ 云端 CPU 10ms / 单行 2MB / 每调用 50 查询

- **问题**：`/api/ingest` 要 `JSON.parse`（≤400 KB，含增量 stocks）+ `db.batch()`；`/api/regions` 要读大字段。
  10ms CPU 是 Free 档硬墙（`Error 1102`）
- **攻坚**：
  - 载荷 ≤400 KB（**增量 stocks ≤200 KB**，见 FR-C2 规则 5）、每批 ≤20 语句、**每请求 ≤10 次 D1 查询**（留余量）
  - `regions` 与 `stocks` **分接口**，列表页不解析 K 线
  - 超限表现：400/502 + Workers Logs 告警；持续触顶 → 升 Paid（30 s CPU）或拆 Worker
- **验证**：压测 10 轮上报，Workers Logs 看 CPU p95 < 5ms

### D5 ★★ 存储预算与保留策略

- **问题**：60s 一轮 = 全天约 1440 轮，每轮 ~300 KB → **一天 432 MB，单日即可逼近 500 MB 上限**
- **攻坚**：**当日保留最近 60 轮（R1）+ 历史日只留最后一轮**（§FR-C3 预算 ≈26 MB，占上限 5.2%）；
  cron 每小时 `DELETE`；`kline_cache` 按 `(symbol, kline_date)` upsert 不累积
- **验证**：造 30 天假数据，跑保留任务后 D1 用量 < 100 MB，且今日仅剩 60 行

### D6 ★★ 四端一致性（终端 / 飞书 / 云端全量 / 云端门内）

- **问题**：AGENTS 已明确「故意打破两端一一对应」，再加一端更易错位
- **攻坚**：Web 默认全量（同终端）+ 可切门内，**强制标注语义**（FR-V3）；列头单源（见 D7）
- **验证**：断言标题文案含「下一张…非已推」

### D7 ★★ 列头单源化（跨网络的第三份硬编码风险）

- **问题**：`COLS_*` 带 ANSI 宽度逻辑，不能直接给前端
- **攻坚**：`panel_serialize.py` 导出纯结构 `[{key,label,align,unit,digits}]`，云端/前端按 `key` 取值
- **验证**：单测断言 Web 列 key 序列 == `COLS_*` key 序列

### D8 ★ SSE 在 Workers 上的可用性

- **问题**：社区实测 **100s 无事件 → 524**；Free 档 10 万请求/日会被重连吃掉
- **攻坚**：15s 心跳（6 倍安全边际）；`EventSource` 退避重连；预算见 §九
- **兜底**：SSE 不稳时降级为 15s 轮询 `/api/regions`（1 次/15s ≈ 5760 次/日，仍充裕）

---

## 九、基础设施与资源

### 9.1 账号与资源（全部免费档）

| 资源 | 名称（建议） | 用途 |
|---|---|---|
| Workers | `cf-panel-api` | API + SSE（Hono） |
| D1 | `cf-panel-db` | `snapshots` / `kline_cache` |
| Pages | `cf-panel-web` | 前端静态资源 |
| Access | `panel.<你的域名>` 应用，**认证 = GitHub SSO（R2）** | 公网读鉴权 |
| 密钥 | `INGEST_SECRET`（wrangler secret）/ `RTS_PANEL_SECRET`（本地 `.env`） | 上报通道 |
| 可选 | R2 `cf-panel-archive` | 月度冷备导出 |

### 9.2 请求预算（G6，Free = 100,000/日）

| 来源 | 计算 | 每日 |
|---|---|---|
| 本地上报 | 1440 轮 × 1 | 1,440 |
| 每客户端 `/api/regions` | 1440 × 1 | 1,440 |
| SSE 连接（每 10min 重连一次） | 144 × 1 | 144 |
| 详情/历史浏览（估） | — | 200 |
| **单用户合计** | | **≈ 3,000** |
| **3 用户 + 上报** | 3,000×3 + 1,440 | **≈ 10,400（10.4%）** |
| 保留策略 cron | 24 × 2 | 48 |

→ **余量 9 倍**；若触顶（`Error 1027`）优先查是否有客户端死循环轮询。

### 9.3 依赖清单

**本地**（不新增任何依赖，R4）：**复用已有 `requests>=2.28`** 做上报 HTTP 客户端
（`requirements.txt` **零改动**；明确**不引入 `httpx`**）
**云端**：`hono`、`wrangler`（技术栈与 `cf-stack-starter` 同源，但**仓库独立，R3**）
**前端**：`react`、`react-dom`、`vite`、`echarts`（表格用原生 table 或 `@tanstack/react-table`）

### 9.4 仓库与目录落位（R3：独立仓库）

**两个仓库，边界 = 是否含 Python 上报侧：**

```text
仓库 A：D:\everything\rts-dfcf-max（本项目，已有）
  scanner/
    panel_serialize.py     # ScanView → JSON（D3/D7 唯一收口）
    panel_reporter.py      # 后台线程上报 + 重试 + 日志（FR-C1）
  tests/
    test_panel_serialize.py  test_panel_reporter.py
  logs/panel_report.log     # 本地上报日志（gitignored）
  .env                      # RTS_PANEL_URL / RTS_PANEL_SECRET（gitignored）

仓库 B：<新建，建议 rts-panel-cloud>（云端，独立于 cf-stack-starter）
  worker/   wrangler.jsonc  migrations/0001_init.sql  src/{index,ingest,api,stream}.ts
  web/      package.json  vite.config.ts  src/{api,components,pages}
  docs/     API 契约与部署手册
```

> 与 `cf-stack-starter` 的关系：**仅技术栈同源（Pages + Workers/Hono + D1 + 鉴权思路），
> 代码与仓库完全独立**（R3）。可参考其 `wrangler.jsonc` 骨架、Hono 错误信封、
> D1 迁移目录约定与 `docs/deploy.md` 纪律，但不合并、不作为依赖。

---

## 十、里程碑（风险前置）

| # | 里程碑 | 内容 | 验收要点 |
|---|---|---|---|
| **M0** | **打通上报通路（风险前置）** | `panel_serialize` + `panel_reporter` + `POST /api/ingest`（**已带 Bearer 密钥**，Access 尚未启用也不暴露写入）+ D1 双表 | 连续 3 轮：云端读回与终端**逐条一致**；序列化 ≤50ms；**拔网线扫描无异常**；幂等重放不产生重复行 |
| M1 | 面板静态渲染 | `/api/regions` + 前端五区块 + 静态托管 | 列头逐列一致、空区块显 `—`、红涨绿跌、滞后 N 轮提示 |
| M2 | **鉴权 + 公网** | Access（**GitHub SSO**）拦读 + `INGEST_SECRET` 拦写 + WAF | 未登录访问**被 Access 拦截页挡住、拿不到数据**；错密钥 401；`git log -S` 无密钥 |
| M3 | SSE + 状态条 | `/api/stream` + `EventSource` + 15s 心跳 | 断网 10s 恢复对齐；10 分钟无 524 |
| M4 | 个股详情 | K 线 + 评分分解 + 出现史 | 任意行可跳；`symbol` 非法 400；缺票 404 且文案准确 |
| M5 | 历史 + 过门 + 收尾 | 日历、FR-V3、保留策略、日志与预算核对 | 只列有快照日；今日仅剩 **60 轮**且总量 <100 MB；`RTS_PANEL=0` 与当前版本行为一致 |

> **M0 不过不进 M1** —— 跨网数据通路是本版唯一的技术悬崖。

---

## 十一、验收标准汇总（可演示脚本）

1. **GIVEN** `RTS_PANEL=0` **WHEN** 启动扫描 **THEN** 无上报、无新依赖，行为与当前版本一致
2. **GIVEN** 正常上报 **WHEN** 连续 3 轮 **THEN** 云端行数/首行 symbol/顺序与终端逐条一致
3. **GIVEN** 断网 10 分钟 **WHEN** 查看扫描日志 **THEN** 只有 `panel_report.log` 的失败记录，扫描照常推进
4. **GIVEN** 断网恢复 **WHEN** 面板刷新 **THEN** 补齐缺口，状态条滞后归 0
5. **GIVEN** 未登录 GitHub SSO **WHEN** 访问 `/api/regions` **THEN** 被 Access 拦截页挡住，数据不可读
6. **GIVEN** 错误 `INGEST_SECRET` **WHEN** 上报 **THEN** 401，且**云端数据不被污染**
7. **GIVEN** 正常轮次 **WHEN** 打开面板 **THEN** 五区块齐全、列头逐列一致、红涨绿跌
8. **GIVEN** 某轮 `passed/total=12/40` **WHEN** 看过门区 **THEN** 显示「通过 12 · 剔除 28」+「下一张卡会推什么（非已推内容）」
9. **GIVEN** 点击任意行 **WHEN** 进详情 **THEN** 1.5s 内 K 线+成交量+评分分解，各项之和=总分
10. **GIVEN** 运行 30 天 **WHEN** 查看 D1 用量 **THEN** <100 MB，**今日仅剩 60 轮**，历史日只留最后一轮

---

## 附录 A · 技术调研笔记（Research Notes）

### A.1 Cloudflare 官方限额（2026-09 抓取，权威）

**Workers Free**：请求 **100,000/日**（超限 `Error 1027`，可配 fail open/closed）、**CPU 10 ms/请求**、
内存 128 MB、子请求 50/次、并发待响应连接 6、启动 1 s、**HTTP 请求时长无硬限制**、
请求体上限 **100 MB**（Free 计划）、响应体无限制、Cron 5/账户。

**D1 Free**：单库 **500 MB**、账户 5 GB、**单行/单字段 2,000,000 bytes**、单条 SQL **100 KB**、
bound 参数 **100/查询**、**每 Worker 调用 50 次查询**、单条 SQL 30 s、单库单线程（吞吐≈1/查询耗时）。

来源：`developers.cloudflare.com/workers/platform/limits/`、`developers.cloudflare.com/d1/platform/limits/`。

### A.2 SSE 在 Workers 上的可行性

- 官方 Agents 文档：**"No timeout limits — Workers have no effective limit on SSE response duration"**
- Workers Limits：HTTP 请求**无时长硬限制**，客户端保持连接即可继续流式
- 但社区实测：**100 秒不发事件 → 524**；且有报告称 Free 档长连接存在历史限制
- 结论：**SSE 可行，必须 15s 心跳**；兜底方案是 15s 轮询（预算仍充裕）

### A.3 鉴权：Cloudflare Access vs 自建

- Access 在 **Worker 之前**拦截，Worker 只需读 `Cf-Access-Jwt-Assertion`（官方：
  "you do not need to perform explicit JWT validation"，但推荐校验 audience）
- 免费档适合 ≤50 用户的小团队/个人，支持邮箱 OTP、GitHub SSO、Service Token
- **本期定稿（R2）：GitHub SSO** —— 上报器不走 Access（用 `INGEST_SECRET`），
  只有人类浏览器读接口需要会话；GitHub 账号即身份，无需额外建邮箱名单
- 自建 session/JWT 要处理：登录页、cookie、过期、暴力破解 —— **本期不做**
- 备选加固：把 `/api/ingest` 也纳入 Access 并用 **Service Token**（M2 之后按需启用）

### A.4 前端与图表

- ECharts `candlestick` 数据格式 `[open, close, low, high]`；A 股**红涨绿跌**
  （`color:'#ef232a'` / `color0:'#14b143'`）；成交量副图要与 K 线同色联动，
  `grid`/`xAxis`/`yAxis`/`series` 四者都要设
- 表格：行数 30~62、列 10~14 → **原生 table 或 TanStack Table（MIT headless）**；
  AG Grid 社区版过重且企业版收费

### A.5 「扫描器整体上云」的不可行性论证

1. 运行时依赖 `pywencai`、`akshare`、`requests`、`wcwidth`、`psutil` —— Workers 只跑 JS/WASM，
   Python 无法直接运行（**必须重写整条数据链**）
2. `scanner.db` 是本地 SQLite（WAL），跨网络访问需要隧道或先全部迁 D1
3. 每 60s 一轮、每轮要拉榜单/批量行情/K 线 → 请求数远超 Free 10 万/日
4. `pywencai`/东财/雪球接口可能拒绝云 IP，且密钥暴露面变大
→ **结论**：本地扫描 + 云端只读展示是合理分工；确需服务端重计算再考虑 Container（见 A.6）

### A.6 与 `cf-stack-starter` 的关系（R3：独立仓库）

两者**技术栈同源**（Pages + Workers/Hono + D1 + 鉴权思路），但按评审决策**仓库完全独立**：

| | `cf-stack-starter` | 本项目云端仓库 |
|---|---|---|
| 定位 | 通用起手骨架（含 notes/upload/Resend 演示） | 面板专用 API + 前端 |
| 可借鉴 | `wrangler.jsonc` 骨架、Hono 错误信封、D1 迁移目录约定、`docs/deploy.md` 纪律 | — |
| 不可复用 | Access 规则、上报通道、SSE、保留策略、增量 K 线 | 这些是本项目独有能力 |
| 关系 | **不合并、不作为依赖、不互相引用** | — |

### A.7 其它候选（本期不用）

- **Hyperdrive + PostgreSQL**：需要跨表事务/复杂 JOIN 时才引入；本期只有两张表，D1 足够
- **Container**：分钟级/多核重活（若未来做批量回测、图像生成）；判断口径
  单次 > 30 s CPU 或需多核 → Container，否则留 Workers/Queues
- **Cloudflare Tunnel**：数据留在本地、只暴露入口的方案（v1.5 备选），本期已选「数据上云」

---

## 附录 B · 原始需求对照表

| 原始说法 | 对应章节 |
|---|---|
| 「给当前项目搭一个 UI」 | 全文 |
| 「形态 = 本地 Web 面板」（v1） | **已被 v2 覆盖** |
| 「使用 Cloudflare」「本地上报 → Workers/D1」「公网 + 鉴权」（v2） | 三、架构；FR-C*；FR-R*；六·安全；附录 A |
| 「comprehensive 模式」 | 一~十一章 + 附录 A/B/C |
| 评审决策 R1（当日 60 轮） | FR-C3 保留策略、D5、M5、验收 10 |
| 评审决策 R2（GitHub SSO） | 3.1、3.3、D2、A.3、9.1、M2、验收 5 |
| 评审决策 R3（独立仓库） | 9.4、A.6 |
| 评审决策 R4（复用 `requests`） | 9.3、A.5 |
| 隐含：不动扫描链路 | G3 / FR-C1（≤50ms、失败不上抛）/ 验收 1、3 |
| 隐含：不能产生第二结论源 | G5 / FR-V3 / FR-D1（缺票不外网补拉） |
| Cloudflare 材料中的 Container / Hyperdrive | 附录 A.7（本期不引入，留演进） |

## 附录 C · 参考链接

- Cloudflare Workers Limits（官方，2026-09）：https://developers.cloudflare.com/workers/platform/limits/
- Cloudflare D1 Limits（官方，2026-04）：https://developers.cloudflare.com/d1/platform/limits/
- Workers SSE / HTTP 流式：https://developers.cloudflare.com/agents/runtime/communication/http-sse/
- Cloudflare Workers + Access：https://developers.cloudflare.com/workers/configuration/cloudflare-access/
- EventSource（Workers 运行时 API）：https://developers.cloudflare.com/workers/runtime-apis/eventsource/
- Apache ECharts K 线示例：https://echarts.apache.org/examples/zh/index.html
- 项目内事实来源：`scanner/view/assemble.py:149`、`scanner/view/model.py:444/547/570`、
  `scanner/view/render.py:329/421/515/651`、`scanner/feishu.py:478`、`scanner/push_gate.py:319`、
  `scanner/db/schema.py:27-50`、`unified_scanner.py:339/569/664`

---

*编制：requirements-doc-gen（comprehensive 模式）· v2 · 2026-09-29*
