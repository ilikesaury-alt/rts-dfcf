# rts-dfcf-max Web 面板 · 实现级设计文档

| 元信息 | 值 |
|---|---|
| 文档类型 | **实现级设计文档**（design-doc-gen，15 步） |
| 上游输入 | `docs/ui-panel-requirements.md` **v2.1**（已定稿，含评审决策 R1~R4、难点 D1~D8、里程碑 M0~M5） |
| 目标项目 | `D:\everything\rts-dfcf-max` + 新建云端独立仓库（R3） |
| 编制日期 | 2026-09-29 |
| 状态 | **v1.1 复核修订**（2026-09-29 技术复核，修 10 处，见 §1.3） |

---

## 1. 文档说明

### 1.1 设计原则

1. **业务与适配层分离**：`scanner/` 只新增 `panel_serialize`（序列化）与 `panel_reporter`（传输），
   **不改动 `run_scanner` 的任何既有逻辑**，只在其尾部追加一次调用。
2. **返回值即 API**：云端接口的 JSON 结构 = 本地序列化结构的镜像，单一契约文件（§5）双向约束。
3. **风险前置**：M0 只验证「跨网数据通路」这一最大不确定性，不通不进 M1。
4. **旁路隔离**：面板任何环节（序列化、网络、云端、鉴权）失败都不得影响扫描主链路。

### 1.2 与需求文档的对应

本文档只写「怎么实现」，不重复「做什么」。每章末尾标注其落地的需求编号（FR/G/D/NFR）。
需求侧未覆盖而设计侧新发现的问题，集中列在附录 Open Questions。

### 1.3 技术复核修订记录（2026-09-29，v1 → v1.1）

| # | 问题 | 修正 |
|---|---|---|
| 1 | §4.1 把 `SCHEMA_VERSION` 放进 `vars` —— Cloudflare **`vars` 一律字符串**，`2 !== "2"` 会让**每轮上报都被 400** | 常量移入 `serialize.ts`（代码），比较用 `Number(p.schema)`；`Env` 不再含 `SCHEMA_VERSION` |
| 2 | §3.8/§4.3/§4.4 用 `ORDER BY seq DESC` 取最新轮 —— 扫描机重启后 **seq 重排**，面板会读到旧轮冒充最新 | 一律改 `ORDER BY date DESC, time DESC`；`seq` 降级为**展示用轮号，不作排序键** |
| 3 | §3.5 历史日保留 SQL `time NOT IN (SELECT MAX(time) ... GROUP BY date)` **跨日误判**（不同日的 `MAX(time)` 相同则漏删） | 改 `(date,time) NOT IN (SELECT date, MAX(time) ... GROUP BY date)` |
| 4 | §3.8 文字「每 30s 查询 + 15s 心跳」与代码（每 15s 查询一次）矛盾；注释「40×30s≈20min」与预算表「144 次/日 = 每 10min」矛盾 | 统一为「每 15s 一次 `SELECT seq`（查询即心跳），40 次 = **10min**」，40×15s=600s 与 144 次/日对齐 |
| 5 | §3.2 注释称「常数时间比较」而代码是 `!==`，且本节反模式自己禁止 `==` 比较 | 补 `safeEqual()`（等长 XOR 累积）实现，代码与注释一致 |
| 6 | §4.2 `stmts.length > 21` 恒不触发（前一行已 `.slice(0,20)`）—— 死检查 | 改为先截断、响应带 `stocksStored` 计数，**不拒绝整轮**（避免上报器配额 bug 导致整轮丢失） |
| 7 | D2 域拓扑未定：读接口若同时挂在 `*.workers.dev` 暴露 → **完全绕过 Access** | 定为**同域** `panel.<域名>/api/*` + `wrangler.jsonc "workers_dev": false`（§3.2 新增第 6 层、§11.1） |
| 8 | `pending_stocks` 的组装机制全文缺失（谁算「新出现」、K 线从哪来、重启后如何收敛） | §3.1 补「增量收集器」段 + 进程内 `sent` 表 |
| 9 | §4.4 `GET /api/meta/config` 标 0 查询，实际要读最新 payload 才拿得到 `ctx.config` | 改 1 次查询 |
| 10 | §13.1 M1 判据漏了需求侧 M1 的「滞后 N 轮提示」 | 补入（M1 用 `/api/meta` 轮询实现，M3 换 SSE 驱动） |

---

## 2. 总体设计

### 2.1 架构总览（四层）

```text
┌─ 浏览器层 ─────────────────────────────────────────────────┐
│  React 19 + Vite 构建产物（Pages 托管）                      │
│  App → { 状态条 StatusBar / 五区块 RegionTable / 详情页 /     │
│           日历 / ECharts K线 }                              │
│  数据入口：src/api/client.ts（唯一收口，含 EventSource 封装） │
└───────────────▲───────────────────────────────────────┬────┘
                │ ② HTTPS + Access(GitHub SSO) 会话       │ ① SSE 事件
┌─ 服务层（Cloudflare Workers · cf-panel-api · Hono）───────▼────┐
│  index.ts     装配：CORS / 错误信封 / 路由挂载                 │
│  auth.ts      ① 读接口校验 Cf-Access-Jwt-Assertion（可选加固）  │
│               ② /api/ingest 校验 Bearer INGEST_SECRET          │
│  routes/ingest.ts   上报写入（schema 版本校验 + 幂等 upsert）    │
│  routes/regions.ts  五区块快照读（单行大字段，1 次查询）         │
│  routes/stock.ts    K线/出现史/评分分解                         │
│  routes/history.ts  按日快照 + 有快照日期列表                   │
│  routes/meta.ts     轮次/滞后/时段/配置快照                     │
│  routes/stream.ts   SSE（**自轮询 D1 + 生命周期上限**，见 D8）   │
│  cron.ts      scheduled：保留策略（R1=60 轮）批量 DELETE         │
└───────────────┬───────────────────────────────────────────┘
                │ ③ D1 binding（batch / prepare / bind）
┌─ 数据层 ──────▼───────────────────────────────────────────┐
│  D1 cf-panel-db：snapshots（幂等主键 date+time）             │
│                  kline_cache（主键 symbol+kline_date）        │
└───────────────▲───────────────────────────────────────────┘
                │ ④ POST /api/ingest（Bearer INGEST_SECRET）
┌─ 本地数据源层（Windows · 本仓库）─────────────────────────────┐
│  unified_scanner.run_scanner                                 │
│    → build_scan_view → render_terminal → push_feishu         │
│    → ★ panel_reporter.report(view)   ← 新增的唯一挂点         │
│         panel_serialize.serialize_view（≤50ms，调用线程内）     │
│         threading.Thread(daemon=True) → requests.Session      │
│              重试 2 次(1s/3s) → 失败丢弃 + logs/panel_report.log│
└──────────────────────────────────────────────────────────────┘
```

**启动命令**

```bash
# 本地（本仓库）
python unified_scanner.py 60 --no-feishu     # 面板随主循环自动上报（需 .env 配置）
python unified_scanner.py 60 --no-panel      # 关闭上报

# 云端（新仓库 rts-panel-cloud）
pnpm install
pnpm dev:worker        # wrangler dev --local（本地 D1）
pnpm dev:web           # vite（proxy → 127.0.0.1:8787）
pnpm db:migrate:remote # 首次
pnpm deploy:worker && pnpm deploy:web
```

### 2.2 模块划分与合并/拆分理由

| 模块 | 位置 | 职责 | 拆分/合并理由 |
|---|---|---|---|
| `panel_serialize.py` | 本仓库 | `ScanView → dict` | **独立**：D3/D7 的唯一收口，必须是纯函数可单测，不能混进传输逻辑 |
| `panel_reporter.py` | 本仓库 | 线程池上报 + 重试 + 日志 | **独立**：唯一持有网络与密钥的模块，隔离后 `panel_serialize` 可零网络测试 |
| `worker/routes/*.ts` | 云端仓 | 每资源一文件 | 按资源拆，**不按层拆**（避免 N 层目录但每层只有 3 行） |
| `worker/routes/stream.ts` | 云端仓 | SSE 单独一文件 | **独立**：唯一长连接路由，其生命周期/查询预算逻辑与普通请求差异大 |
| `worker/cron.ts` | 云端仓 | 保留策略 | **独立**：触发方式不同（`scheduled` 而非 `fetch`），权限面不同 |
| 前端 `api/client.ts` | 云端仓 | 所有 HTTP + EventSource | **合并**：一个收口，禁止组件裸 `fetch`（D7 反模式） |
| 前端 `components/RegionTable.tsx` | 云端仓 | 五区块表格 | **合并**：五区块仅列 spec 与数据源不同，抽一个组件吃 `COLS_*` 投影，避免 5 份近似代码 |

**不拆**：不引入状态管理库（Zustand/Redux）——面板状态只有「当前快照 + 滞后 + 路由」，
`useState` + Context 足够；不引入 SSR 框架（纯静态导出即可）。

### 2.3 目录结构（文件级 + 职责）

```text
仓库 A：D:\everything\rts-dfcf-max（本仓库，仅新增 2 文件 + 1 .env 键）
  scanner/
    panel_serialize.py     # 纯函数：ScanView → dict；含列 spec 投影导出 COLS_* → [{key,label,...}]
    panel_reporter.py      # report(view) 入口 / _send 后台线程 / _retry / _log
  tests/
    test_panel_serialize.py   # tuple 键往返、_candidate 剥离、列 key 序列 == COLS_*、体积上限
    test_panel_reporter.py    # 失败不上抛、重试次数、后台线程不阻塞（monkeypatch requests）
  .env                      # RTS_PANEL_URL / RTS_PANEL_SECRET（新增 2 键，gitignored）

仓库 B：rts-panel-cloud（新建，R3 独立仓库）
  worker/
    wrangler.jsonc          # D1 绑定 + triggers.crons + vars + observability
    migrations/0001_init.sql  # snapshots / kline_cache（id 不改名不删）
    src/
      index.ts              # new Hono<{Bindings:Env}>()；挂 CORS/错误信封/全部路由
      env.ts                # Env 类型（DB / INGEST_SECRET / WEB_ORIGINS / SCHEMA_VERSION）
      auth.ts               # bearerGuard(c) 与 accessGuard(c)
      error.ts              # AppError(code,status,message) + toEnvelope()
      serialize.ts          # 载荷 TS 类型 + SCHEMA_VERSION 常量（与 Python 侧对齐）
      routes/{ingest,regions,stock,history,meta,stream}.ts
      cron.ts               # scheduled handler：保留策略（60 轮 / 10 交易日）
    package.json  tsconfig.json  .dev.vars.example
  web/
    package.json  vite.config.ts  index.html
    src/
      main.tsx  App.tsx  styles.css
      api/client.ts         # apiGet/apiSend/subscribeSSE（唯一网络收口）
      api/types.ts          # 与 worker/src/serialize.ts 对齐的类型
      components/{StatusBar,RegionTable,GatePanel,RowDetail}.tsx
      pages/{Dashboard,Stock,History}.tsx
      lib/cols.ts           # 消费上报的 colSpec 投影
  tests/                    # worker 侧：vitest + @cloudflare/vitest-pool-workers
  docs/api.md  docs/deploy.md
```

### 2.4 技术选型决策（DR）

| # | 决策点 | 选型 | 理由 | 备选（被否原因） |
|---|---|---|---|---|
| DR-1 | 上报传输 | **HTTP POST + 幂等 upsert**，Fire-and-forget | 扫描是生产者，面板是旁路；单向、无状态、失败可丢 | ① WebSocket 长连（断线即丢数据、需服务端持有本地连接）② 隧道（v1.5 备选，数据不出本地但面板只能看活的扫描机） |
| DR-2 | 上报线程模型 | **`threading.Thread(daemon=True)` 每轮一线程 + `requests.Session` 复用** | daemon 线程不阻塞进程退出（A.3 佐证）；60s 一轮不会堆积（线程生命周期 ≤28s < 60s） | ① 线程池（多一层抽象，无收益）② asyncio+httpx（**R4 禁止新增 httpx**，且主循环是同步代码） |
| DR-3 | 云端存储 | **D1 双表** | 需按 `date` 查、幂等 upsert、LIMIT；免费 500MB，预算 26MB（§FR-C3） | R2（无 SQL）、KV（无查询）、Hyperdrive+PG（两张表犯不上，A.7） |
| DR-4 | 幂等 | **`PRIMARY KEY(date,time)` + `ON CONFLICT DO UPDATE`** | 重试/重放天然幂等，免去「先查后写」的竞态 | ① 只用 `seq`（重装/换机后 seq 会重排）② 先 SELECT 再 INSERT（非原子，D1 无显式事务） |
| DR-5 | 实时通道 | **SSE：D1 自轮询 + 生命周期上限 40 次轮询（约 10min）后主动断开** | 详见 D8 —— 这是 Step 2 调研推翻原假设后的修正 | ① Durable Objects 广播（要额外资源与代码，本期量级不值）② 纯轮询（省连接但每轮多 1 次请求，同预算） |
| DR-6 | 保留策略 | **`scheduled` cron 每小时批量 DELETE（每批 ≤1000 行）** | 官方要求大 DELETE 分批（A.5）；cron 免费 5 个够用 | ① 写入时顺带删（写路径变重、超 10ms CPU）② 靠 TTL（D1 无 TTL） |
| DR-7 | 前端框架 | **React 19 + Vite + ECharts，表格自研组件** | 需求 A.4 已定；行数 30~62 不需重型表格库 | ① AG Grid（企业版收费、过重）② Vue（团队栈已定 React） |
| DR-8 | 版本锁定 | Python 侧 `requirements.lock` 不动（**零新增依赖，R4**）；云端 `pnpm-lock.yaml` 入库 | 云端依赖变化快，锁文件是唯一防线 | — |
| DR-9 | 详情页数据源 | **K 线增量上报 + `kline_cache` 缓存，缺失 → 404 `NOT_IN_REPORT`** | 需求 FR-C2 规则 5/6：全量上报打穿体积与 CPU | ① 云端外网补拉（**第二数据通路，G5 禁止**）② 面板反向查本地（本地无公网入口） |

---

## 3. 核心机制设计（逐攻坚点 D1~D8）

### 3.1 D1 ★★★ 运行态数据的跨网上报

**问题回顾**：`hot_rows/hist_rows/offboard_rows/new_symbols/weak/rank_map` 只存在于
`run_scanner` 局部变量，DB 无法复原；云端隔着可能失败的公网。

**设计（可抄）**

```python
# scanner/panel_serialize.py —— 纯函数，零 I/O
import json, dataclasses
from scanner.view.model import ScanView, COLS_POOL, COLS_HOT, COLS_HIST

SCHEMA_VERSION = 2

def _marks(m: dict | None) -> dict[str, str]:
    """D3 攻坚点：tuple 键 → 'sym|cat' 字符串。
    json.dumps 遇 tuple 键抛 TypeError: keys must be str, int, float, bool or None。"""
    if not m:
        return {}
    return {"|".join(k) if isinstance(k, tuple) else str(k): v for k, v in m.items()}

def _flat_row(row) -> dict:
    """D3：dataclass + TypedDict 混合体 → 纯标量 dict。
    关键：entry 里的 _candidate 是活对象，必须剥离，否则 asdict 会递归爆炸。"""
    d = dataclasses.asdict(row) if dataclasses.is_dataclass(row) else dict(row)
    entry = d.pop("entry", None) or {}
    entry.pop("_candidate", None)                       # ← 剥离
    out = dict(entry)
    out.update({k: v for k, v in d.items() if k != "entry"})
    return out

def serialize_view(view: ScanView, *, extra: dict | None = None) -> dict:
    # 排序已在 build_scan_view 完成（FR-V2：云端不得重排），这里只做结构转换
    return {
        "schema": SCHEMA_VERSION,
        "seq": extra.get("seq", 0),
        "date": extra["date"], "time": extra["time"],
        "durationMs": extra.get("durationMs", 0),
        "regions": {
            "main":     [_flat_row(r) for r in view.main_rows],
            "hist":     [_flat_row(r) for r in view.hist_rows],
            "hot":      [_flat_row(r) for r in view.hot_rows],
            "offboard": [_flat_row(r) for r in view.offboard_rows],
            "gate": _gate(view),                          # 见 3.6
            "cols": _cols(),                              # 见 3.7（COLS_* 投影，静态 ~1KB）
        },
        "ctx": {
            "marketIdxPct": view.market_idx_pct, "weak": view.weak,
            "flowFiltered": view.flow_filtered,
            "warnings": view.warnings or [], "ruleResult": view.rule_result,
        },
        "stocks": extra.get("stocks", {}),                # 增量，见 3.1.1
    }
```

```python
# scanner/panel_reporter.py —— 唯一持有网络与密钥的模块
import json, logging, threading, time, requests

_log = logging.getLogger("panel")
_sess = requests.Session()                      # DR-2：连接复用
_BACKOFF = (1, 3)                               # FR-C1：退避 1s/3s
_TIMEOUT = (3, 8)                               # FR-C1：连接 3s / 总 8s

def report(view, *, url: str, secret: str, **extra) -> None:
    """主循环调用点。**任何异常都不上抛**（G3）。"""
    try:
        t0 = time.perf_counter()
        body = json.dumps(serialize_view(view, extra=extra), ensure_ascii=False).encode()
        ms = (time.perf_counter() - t0) * 1000
        if ms > 50:
            _log.warning("serialize %.1fms > 50ms budget", ms)
    except Exception:                            # 序列化失败同样不打断扫描
        _log.exception("panel: serialize failed, round dropped")
        return
    # DR-2：daemon 线程，进程退出不被阻塞；60s 轮间隔 > 28s 最坏线程时长，不堆积
    threading.Thread(target=_send, args=(url, secret, body), daemon=True).start()

def _send(url, secret, body) -> None:
    for attempt in range(len(_BACKOFF) + 1):     # 首次 + 2 次重试
        try:
            r = _sess.post(url, data=body, timeout=_TIMEOUT,
                           headers={"Authorization": f"Bearer {secret}",
                                    "Content-Type": "application/json"})
            if r.status_code < 300:
                _log.info("panel: ok %dB", len(body)); return
            if r.status_code in (400, 401):      # 4xx 重试无意义（契约/密钥错）
                _log.error("panel: %s %s", r.status_code, r.text[:200]); return
        except Exception as exc:                 # 网络失败 → 继续重试
            _log.warning("panel: attempt %d failed: %r", attempt, exc)
        if attempt < len(_BACKOFF):
            time.sleep(_BACKOFF[attempt])        # 1s, 3s
    _log.error("panel: round dropped after %d attempts", len(_BACKOFF) + 1)
```

**主循环挂点（唯一改动）** —— `unified_scanner.py` 在 `view = display(...)` 与 `push_feishu(...)`
之后追加：

```python
if panel_enabled:                                # 由 .env 判定，未配置则完全不导入
    panel_reporter.report(view, url=RTS_PANEL_URL, secret=RTS_PANEL_SECRET,
                          seq=round_seq, date=today, time=now,
                          durationMs=elapsed_ms, stocks=pending_stocks)
```

**开关口径（三者齐全，取自需求 FR-C1，勿再新增第四个）**

| 开关 | 语义 | 位置 |
|---|---|---|
| `RTS_PANEL_URL` / `RTS_PANEL_SECRET` 未配置 | **静默不启动**，连 `panel_reporter` 都不导入 | `.env` |
| `RTS_PANEL=0` | 即使配了 URL 也关闭上报 | `.env` |
| `--no-panel` | 命令行关闭，优先级最高 | CLI 参数 |

**`pending_stocks` 的增量收集器（FR-C2 规则 5，本节补全机制）**

问题：云端 `kline_cache` 何时「过期」，本地不知道（它在 D1 里）；重启后进程记忆丢失。

设计：`panel_reporter` 持有一张**进程内** `sent: dict[symbol, kline_last_date]`：

1. 每轮候选 = `(本轮四区块去重后的票)` 中满足任一条件者：
   ① `symbol not in sent`（首次/重启后）；② `daily_kline[symbol] 的最新日期 > sent[symbol]`
   （新交易日，该票 K 线已前进）。
2. 候选按 **`isNewEntry`（新票）优先 → 区块序 → 出现次数** 排序，**取前 20**（FR-C2 上限）；
   K 线从本地 `scanner.db` 的 `daily_kline` 切近 120 根，**无 K 线的票跳过、不阻塞本轮**。
3. 上报**成功**（HTTP <300）才更新 `sent`；失败/丢弃则下轮重新进入候选 → 天然自愈。
4. **重启后收敛**：50 票 ÷ 20/轮 ≈ **3 轮**收敛到全量，期间详情页未命中的票由
   `404 NOT_IN_REPORT` 文案兜底（§4.4），不视为错误。
5. 配额与体积探针（§3.3）是同一处代码：先按上述规则截断到 20，再测 `len(body)`。

> 云端**不做**「按 `kline_date` 判断过期」——那要求上报器读云端状态，会把旁路变成双向。

**多路线择一（M0 按 A→B→C 验证）**

| 路线 | 内容 | 前置 | 择一顺序 |
|---|---|---|---|
| **A（选定）** | 每轮上报（上文） | 无 | M0 默认 |
| B | 每 N 轮上报（N=5） | A 的载荷超时不可接受时 | 降低 4/5 请求数，代价：面板滞后 5min |
| C | 轮次打包：内存攒 5 轮一次性 POST | B 仍超时 | 复杂度上升，仅在带宽极差时启用 |

调研佐证已把风险下调：D1 `ON CONFLICT` 幂等 + 官方 `batch` 原子（A.1）+ requests 必须显式 timeout（A.3）。

**生命周期/边界**

- 线程生命周期 ≤ `8+1+8+3+8 = 28s` < 轮间隔 60s → **不堆积**（超时是上限，正常 ~200ms）
- 进程退出：daemon 线程被直接丢弃 → 最多丢 1 轮，下轮自愈
- `RTS_PANEL` 未配置 → `panel_enabled=False`，**连 `panel_reporter` 都不导入**（验收 1「无新依赖」）

**反模式（本节禁做）**：`report()` 内 `raise`；在主线程 `time.sleep` 等网络；
把 `requests` 换成 `httpx`（R4）；把序列化放进后台线程（那样 50ms 预算就测不到）。

---

### 3.2 D2 ★★★ 公网化的安全面

**问题回顾**：上报是开放写入端点，密钥泄露 = 别人能往面板灌假数据（污染决策依据）。

**设计**

```ts
// worker/src/auth.ts
function safeEqual(a: string, b: string): boolean {   // 常数时间比较（长度差异只暴露长度本身）
  const ab = new TextEncoder().encode(a), bb = new TextEncoder().encode(b);
  if (ab.length !== bb.length) return false;
  let diff = 0;
  for (let i = 0; i < ab.length; i++) diff |= ab[i] ^ bb[i];
  return diff === 0;
}
export function bearerGuard(c): Response | null {          // 用于 /api/ingest
  const got = c.req.header("Authorization") ?? "";
  if (!safeEqual(got, `Bearer ${c.env.INGEST_SECRET}`))
    return c.json(err("UNAUTHORIZED", "bad ingest secret"), 401);
  return null;
}
// 注意：/api/ingest **不在** Access 应用覆盖范围内（Access 只挂 GET 路由），
// 否则上报器需要维护 Service Token，违反 R2 的「本期不启用 Service Token」。
```

| 层 | 手段 | 覆盖 | 失败表现 |
|---|---|---|---|
| 1 | Access **GitHub SSO**（R2）挂在 `panel.<域名>` 应用，**路径 = `/*` 但排除 `/api/ingest`** | 全部 GET | 未登录 → Access 拦截页 |
| 2 | `INGEST_SECRET` Bearer（`wrangler secret put`），`safeEqual` 比较 | 上报写入 | 401 |
| 3 | WAF 自定义规则：`/api/ingest` 且非 POST → Block；速率 ≤ 6/分钟 | 抗重放/扫描 | 403 |
| 4 | 载荷白名单：只接受 FR-C2 声明的字段，多余字段丢弃 | 防塞脏数据 | 忽略 |
| 5 | 日志纪律：`INGEST_SECRET`/本地 `RTS_PANEL_SECRET` **禁止进入日志**，日志只记状态码与字节数 | 防泄露 | — |
| 6 | **域拓扑收敛（复核修 7）**：API 走**同域** `panel.<域名>/api/*`（Worker `routes` 承接，前端 `fetch('/api/...')` 无跨域），且 `wrangler.jsonc` 设 `"workers_dev": false` 关掉 `*.workers.dev` 旁路 | 堵住「读接口在 workers.dev 上绕过 Access」 | 404/Access 拦截 |

> **第 6 层为什么必须**：Worker 默认同时在 `<name>.<sub>.workers.dev` 暴露；若只在
> `panel.<域名>` 挂 Access，那么 `GET https://cf-panel-api.<sub>.workers.dev/api/regions`
> **不经过任何鉴权**（前端不会去访问它，但扫描器、爬虫、Googlebot 会）。
> 同域后 `WEB_ORIGINS` 仅用于本地 dev（`http://localhost:5173`），生产留空。

**多路线**：A = 上述 5 层（选定）；B = A + Service Token 双保险（M2 后按需启用，
Open Question #3）；C = 自建 session（否决：要写登录页/过期/暴力破解防护）。

**边界**：Access 覆盖不到 SSE 的重连？——`EventSource` 会带 Access cookie，仍在门内。
**密钥轮换**：`wrangler secret put` 覆盖 → 本地 `.env` 同步改 → 重启扫描器，
中间窗口上报失败**只丢轮次不降级**（下轮自愈）。

**反模式**：把 `INGEST_SECRET` 放进 `wrangler.jsonc` 的 `vars`（**vars 可被读取，只有 secret 加密**）；
用 `==` 比较密钥（时序侧信道，虽难利用但无成本规避）。

---

### 3.3 D3 ★★ 序列化的隐性类型

**问题回顾**：`tuple` 键、`TypedDict`、`_candidate` 活对象。

**设计**：见 §3.1 的 `_marks()` / `_flat_row()`。补充三点落地细节：

1. **往返单测**（必须）：`json.dumps(serialize_view(v))` 不抛 + `json.loads` 后键集合与
   `COLS_*` 投影一致。
2. **`score_breakdown`**：`entry` 中是 JSON 字符串 → 序列化时 `json.loads` 一次转 dict，
   云端存原样，前端不再解析（避免两端各解析一次）。
3. **体积探针**：序列化后 `len(body)` > 400KB 记 WARNING 并**截断 `stocks`**（新出现优先，
   FR-C2 规则 5），而非整轮丢弃。

**反模式**：`dataclasses.asdict(view)` 一把梭（会把 `_candidate` 一起递归）；
对 `breakout_mark` 用 `{str(k): v for ...}`（生成 `"(300319, 'MOM')"` 这种不可逆键）。

---

### 3.4 D4 ★★ 云端 CPU 10ms / 单行 2MB / 每调用 50 查询

**问题回顾**：`/api/ingest` 要 `JSON.parse` ≤400KB + `db.batch()`；10ms 是 Free 硬墙（`Error 1102`）。

**设计预算与手段**

| 手段 | 数值 | 依据 |
|---|---|---|
| 载荷分片：`regions+ctx` ≤200KB，`stocks` ≤200KB | 总 ≤400KB | 单行 2MB → 留 5 倍 |
| `JSON.parse` 只解析一次，`payload` 字段存 `JSON.stringify` 后的子串 | CPU ≈1~3ms | A.4：平均 Worker 2.2ms，重活 10~20ms |
| 每请求 D1 查询 ≤10（ingest 1~2、regions 1、stock 2、meta 1~3） | 远低于 50 | A.2 |
| `regions` 不返回 `stocks`；`stock` 独立端点 | 列表页不碰 K 线 | FR-D1 |
| `db.batch()` 每批 ≤20 语句 | 单次 30s SQL 上限内 | A.1/A.5 |

**超限表现**：`Error 1102` → Workers Logs `exceededCpu` → 告警；连续 3 天触顶 →
升 Paid（30s CPU）或按 §2.2 拆 `ingest` 为独立 Worker（service binding 内部调用，不走公网）。

**反模式**：在 Worker 里 `JSON.parse` 两次（读字段 + 存字段各一次）；对同一请求
串行 `await` 超过 6 个未完成的连接（Free 并发连接上限 6）。

---

### 3.5 D5 ★★ 存储预算与保留策略

**问题回顾**：1440 轮/日 × 300KB ≈ 432MB/日，单日逼近 500MB。

**设计**

```sql
-- 今日：只留 time 最大的 60 行（R1）
DELETE FROM snapshots WHERE date = ?1 AND time NOT IN (
  SELECT time FROM snapshots WHERE date = ?1 ORDER BY time DESC LIMIT 60
) LIMIT 1000;                      -- 官方要求大 DELETE 分批（A.5），循环执行直到 changes=0
-- 历史日：每 date 只留最后一轮
-- ⚠ 复核修 3：必须带 date 一起比。只写 time NOT IN (SELECT MAX(time) ... GROUP BY date)
--   会跨日误判 —— 若 A 日的 MAX(time) 恰等于 B 日某行的 time，B 日该行漏删。
DELETE FROM snapshots WHERE date < ?1
  AND (date, time) NOT IN (
    SELECT date, MAX(time) FROM snapshots WHERE date < ?1 GROUP BY date
  ) LIMIT 1000;
-- 超 10 交易日：整日删
DELETE FROM snapshots WHERE date < ?2 LIMIT 1000;
DELETE FROM kline_cache WHERE kline_date < ?2 LIMIT 1000;
```

`cron.ts`（`scheduled`，每小时第 7 分钟，错开整点）循环执行，直到 `meta.changes === 0`
或达到 **20 次循环**（防死循环；单次 cron 墙钟上限 15min，20×批量远低于）。

**边界**：`date` 用交易日历判断「10 交易日」——**当前按自然日近似**（Open Question #4）。

**反模式**：单条 `DELETE` 拉全表（超 30s SQL 限制）；在 `fetch` 处理器里顺带清理（吃 CPU）。

---

### 3.6 D6 ★★ 四端一致性（终端 / 飞书 / 云端全量 / 云端门内）

**设计**：`_gate(view)` 在**本地**调 `apply_push_gate(view)`（与飞书卡片同一函数、同一份结果），
随载荷上报；云端只做展示，**不重新过门**（否则阈值改动会造成两端不一致）。

```python
def _gate(view) -> dict:
    from scanner.push_gate import apply_push_gate
    g = apply_push_gate(view)
    return {"main": g.main, "hist": g.hist, "hot": g.hot, "offboard": g.offboard,
            "stats": dataclasses.asdict(g.stats)}          # 7 个字段全带（FR-V3）
```

前端两个开关：`全量（同终端）`（默认）/ `门内（同飞书）`，切换只是**换数据源**，
标题固定串 `◆ 飞书过门 · 下一张卡会推什么（非已推内容）` 由组件写死（验收 8 断言此串）。

**反模式**：云端按 `config_push` 阈值重新算门（`config_push` 改了云端不同步 → 两端不一致）；
把门内集合当默认视图（用户会误读为「已推送」）。

---

### 3.7 D7 ★★ 列头单源化

**设计**：`panel_serialize.py` 导出列投影，随 `/api/regions` 一并下发：

```python
def col_spec(cols: tuple) -> list[dict]:
    """COLS_* → [{key,label,align,width}]，剥离 ANSI 宽度逻辑。

    ⚠ 实施校正（2026-09-29 T0.1，与原设计不同）：`model.py` 的 `COLS_POOL/COLS_HOT/
    COLS_HIST` **不是** 带属性的对象，而是 `(label, width, align)` 三元组，且被
    `render.py` 以 `COLS_POOL[7][1]` 位置下标取宽度。改成具名对象会连带改渲染层，
    超出「不越界」纪律（不得改 run_scanner 既有逻辑/渲染口径）⇒ **key 取 label**
    （列名在本仓是唯一且稳定的标识；`#` 列的 label 就是 key `＃`→`#`）。
    因此单测断言是 `keys(col_spec(COLS_POOL)) == [c[0] for c in COLS_POOL]`，
    而非原文的 `[c.key for c in ...]`。`unit/digits` 字段前端暂不需要，**不**臆造。
    """
    return [{"key": c[0], "label": c[0], "align": c[2], "width": c[1]} for c in cols]
```

前端 `lib/cols.ts` 只认 `key`，**任何列名字符串不得硬编码在组件里**。
单测断言 `keys(col_spec(COLS_POOL)) == [c.key for c in COLS_POOL]`（D7 验证手段）。

---

### 3.8 D8 ★★ SSE 在 Workers 上的可用性 —— **调研推翻原设计**

**问题回顾**：原设计「SSE 长连接内自轮询 D1」。

**Step 2 调研发现的新约束（改变了设计）**

| 事实 | 来源 | 影响 |
|---|---|---|
| D1 **每次 Worker 调用 ≤50 次查询（Free）** | A.2 官方 Limits | 1 个 SSE 调用内若每 10s 轮询一次 D1，**10 分钟就 60 次 > 50** → 触顶 |
| HTTP 请求**无时长硬限制**，但 **100s 无事件 → 524** | A.6/A.7 | 必须 ≤100s 发事件 |
| Free 档 **100,000 请求/日**，1 连接 = 1 请求 | A.4 | 重连要受控 |

**设计（修正后）**：SSE = **每 15s 一次 D1 `SELECT seq`（查询即心跳）+ 生命上限 40 次（= 10min）后主动 `close`**

> 复核修 4：原稿写「15s 心跳 + 每 30s 查询」，但代码是每循环一次查询一次 sleep —— 两者只能留一个。
> 统一为**查询即心跳**：40 × 15s = 600s = 10min，既满足「≤100s 必须发字节」（15s，6.7 倍边际），
> 又使 D1 查询 = 40 次/连接（< 50 上限，留 10 次余量），并精确对上预算表「每 10min 重连 = 144 次/日」。
> **轮询间隔与上限是耦合常量**：改间隔必须同步改上限（`min(40, 45 / interval_s)`），进 `env.ts` 常量表。

```ts
// worker/src/routes/stream.ts
export async function stream(c, env) {
  const POLL_INTERVAL = 15_000;   // = 心跳间隔（复核修 4：单常量，勿拆两个）
  const MAX_POLLS = 40;           // 40 × 15s = 10min < 50 查询上限（留 10 次余量）
  let polls = 0;
  const gen = async function* () {
    let last = "";
    while (polls < MAX_POLLS) {
      const r = await env.DB.prepare(
        "SELECT seq,date,time FROM snapshots ORDER BY date DESC, time DESC LIMIT 1").first();  // 复核修 2：不按 seq
      const cur = r ? `${r.date} ${r.time}` : "";
      if (cur && cur !== last) { last = cur;
        yield `event: round\ndata: ${JSON.stringify(r)}\n\n`; polls++; }
      else { yield `: ping ${Date.now()}\n\n`; polls++; }   // 查询结果未变也必须发字节（15s << 100s→524）
      await sleep(POLL_INTERVAL);
    }
    yield `event: bye\ndata: {"reason":"lifecycle"}\n\n`;    // 主动收口，避免 524
  };
  return new Response(iteratorToSSE(gen()), {
    headers: { "Content-Type": "text/event-stream",
               "Cache-Control": "no-cache", "Connection": "keep-alive" } });
}
```

前端 `client.ts`：`EventSource` 收到 `bye` → 主动 `close()` 并 **2s 后重建连接**（而非等自动重连）；
普通断线交给浏览器指数退避。重连后**先 `GET /api/regions` 全量对齐**再等增量（FR-R1）。

**多路线择一（M3 按 A→C→B 验证）**

| 路线 | 内容 | 触发条件 |
|---|---|---|
| **A（选定）** | 自轮询 + 生命上限 | 默认；请求预算 144 次/日（§9.2 预算已含） |
| C | 退化为 15s 轮询 `/api/regions` | A 出现 524 或连接被掐（预算 5760 次/日，仍充裕） |
| B | Durable Objects 广播 | 真要多用户毫秒级推送时（成本与复杂度上升） |

**边界**：Access cookie 过期时 SSE 重连被拦截页挡住 → 前端检测到 `403`/非 SSE 内容即
停订阅并提示重新登录（错误表 E7）。

**反模式**：SSE 连接内无限轮询 D1；用 `setInterval` 在 Worker 里发心跳而不 `yield`（不发就不会有字节出去）。

---

## 4. 接口详细设计（schema 与校验代码）

> 对应需求的 FR-C*/FR-V*/FR-R*/FR-D*。所有处理器共享 `error.ts` 的信封。

### 4.1 通用层

```ts
// worker/src/error.ts
export class AppError extends Error {
  constructor(public code: string, message: string, public status = 400) { super(message); }
}
export const err = (code: string, message: string) => ({ ok: false, error: { code, message } });
export const ok  = (data: unknown) => ({ ok: true, data });
```

```ts
// worker/src/serialize.ts —— 契约常量（复核修 1：**不要**放进 wrangler vars）
// Cloudflare 的 vars 一律是字符串，放进去比较会变成 `2 !== "2"` → 每轮 400。
export const SCHEMA_VERSION = 2;   // 与 scanner/panel_serialize.py 的 SCHEMA_VERSION 同值

// worker/src/env.ts —— 绑定的唯一真源（改绑定必须同步这里）
export type Env = {
  DB: D1Database;
  INGEST_SECRET: string;        // wrangler secret（加密，非 vars）
  WEB_ORIGINS: string;          // CORS 白名单，逗号分隔；生产同域后仅 dev 用
};
```

### 4.2 `POST /api/ingest`（写，鉴权）

```ts
// routes/ingest.ts
import { AppError, ok } from "../error";
import { SCHEMA_VERSION } from "../serialize";

const STOCK_QUOTA = 20;   // FR-C2 规则 5：每轮 ≤20 票（≈160KB）

export async function ingest(c: any) {
  const guard = bearerGuard(c); if (guard) return guard;           // D2 第 2 层

  const p = await c.req.json();
  // 设计注释：契约校验用**白名单 + 显式规则**，不用 zod —— 避免为 8 个字段
  // 引入 schema 库把 CPU 从 ~2ms 推到 10ms 边界（D4）。字段级错误一次返回全部。
  const bad: string[] = [];
  if (Number(p.schema) !== SCHEMA_VERSION) throw new AppError("SCHEMA_MISMATCH",   // 复核修 1：Number() 对齐
      `expect ${SCHEMA_VERSION} got ${p.schema}`, 400);             // 云端不识别 → 400（FR-C2）
  for (const k of ["date", "time", "seq", "durationMs", "regions", "ctx"])
    if (p[k] === undefined) bad.push(k);
  if (bad.length) throw new AppError("BAD_REQUEST", `missing: ${bad.join(",")}`, 400);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(p.date)) throw new AppError("BAD_REQUEST", "bad date", 400);
  if (!/^\d{2}:\d{2}:\d{2}$/.test(p.time)) throw new AppError("BAD_REQUEST", "bad time", 400);

  // 复核修 6：先取全量再截断 —— 原稿 `.slice(0,20)` 后又检查 `>21` 恒不触发（死检查）。
  // 超配额**截断而非拒绝**：拒绝会让上报器一个配额 bug 丢掉整轮 regions。
  const allStocks = Object.entries<any>(p.stocks ?? {})
    .filter(([sym]) => /^\d{6}$/.test(sym));                        // 白名单丢弃脏键
  const accepted = allStocks.slice(0, STOCK_QUOTA);

  const stmts = [
    c.env.DB.prepare(`INSERT INTO snapshots(date,time,seq,duration_ms,schema,payload)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(date,time) DO UPDATE SET
          seq=excluded.seq, duration_ms=excluded.duration_ms,
          schema=excluded.schema, payload=excluded.payload,
          ingested_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')`)        // DR-4 幂等
      .bind(p.date, p.time, p.seq, p.durationMs, p.schema,
            JSON.stringify({ regions: p.regions, ctx: p.ctx })),     // stocks 不进 snapshots
  ];
  for (const [sym, s] of accepted) {
    stmts.push(c.env.DB.prepare(
      `INSERT INTO kline_cache(symbol,kline_date,kline,appearances) VALUES(?,?,?,?)
       ON CONFLICT(symbol,kline_date) DO UPDATE SET
         kline=excluded.kline, appearances=excluded.appearances,
         updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')`)
      .bind(sym, s.klineDate ?? p.date, JSON.stringify(s.kline),
            JSON.stringify(s.appearances ?? [])));
  }
  await c.env.DB.batch(stmts);                                       // 原子，A.5
  return c.json(ok({ seq: p.seq, date: p.date, time: p.time,
                     stocksStored: accepted.length,
                     stocksDropped: allStocks.length - accepted.length }), 201);
}
```

### 4.3 `GET /api/regions`（读，Access 后）

```ts
export async function regions(c: any) {
  const row = await c.env.DB.prepare(
    // 复核修 2：按 (date,time) 取最新，不用 seq —— 重启后 seq 重排会读到旧轮
    `SELECT date,time,seq,payload FROM snapshots ORDER BY date DESC, time DESC LIMIT 1`).first();
  if (!row) throw new AppError("NO_SNAPSHOT", "尚无任何上报", 404);
  const { regions, ctx } = JSON.parse(row.payload);
  return c.json(ok({ date: row.date, time: row.time, seq: row.seq, regions, ctx }));
}   // 设计注释：1 次查询；不返回 stocks（D4）；seq 仅展示
```

### 4.4 其余端点规格

| 端点 | 参数（类型/必填/说明） | 查询数 | 关键规则 |
|---|---|---|---|
| `GET /api/stock/{symbol}/kline` | `symbol` string(6) 必填 `^\d{6}$`；`days` int 否 1~120 | 1 | 超 `days>120` → 400 `RANGE_EXCEEDED`；无行 → 404 `NOT_IN_REPORT`（文案「本轮上报未包含该票」，**不外网补拉**） |
| `GET /api/history?date=` | `date` string 必填 `YYYY-MM-DD` | 1 | 取该日 `MAX(time)` 一行；无 → 404 `NO_SNAPSHOT` |
| `GET /api/meta/dates` | 无 | 1 | `SELECT DISTINCT date ORDER BY DESC` |
| `GET /api/meta` | 无 | 1~3 | 最新快照 `seq/date/time/ingested_at` + `config` 快照（取最新轮一律 `ORDER BY date DESC, time DESC`） |
| `GET /api/meta/config` | 无 | 1 | 从最新 `payload` 的 `ctx.config` 读（复核修 9：原标 0 查询有误），前端可与 regions 同请求复用 |
| `GET /api/history/rounds?date=` | `date` 必填 | 1 | P2，`SELECT time,seq WHERE date=? ORDER BY time` |
| `GET /api/stream` | 无 | ≤40（全生命周期） | 见 §3.8 |

**全部端点**：错误返回 `{ok:false,error:{code,message}}`；读端点在 Access 之后（4.1 的
`auth.ts` 可选加固 `accessGuard`）；**无任何写端点除 `/api/ingest` 外**（NFR 只读）。

---

## 5. 数据结构汇总

### 5.1 契约（两端必须一致，`SCHEMA_VERSION = 2`）

```ts
// worker/src/serialize.ts  ↔  scanner/panel_serialize.py（字段名逐一对齐）
export type GateStats = { total:number; passed:number; tierA:number; tierB:number;
                          tierCFallback:number; vetoed:number; droppedNoFallback:number };
export type Row = Record<string, unknown>;        // MainRow/Hot/Hist/Offboard 扁平标量
export type ColSpec = { key:string; label:string; align:string; width:number };  // 见 §3.7 实施校正
export type Regions = { main:Row[]; hist:Row[]; hot:Row[]; offboard:Row[];
                        gate:{ main:Row[]; stats:GateStats }; cols?: Record<string, ColSpec[]>;
                        // 实施补充（2026-09-29 T0.1）：行尾标记 map 随载荷下发。
                        // ScanView.breakout_mark / beauty_mark 的键是 (symbol, category)
                        // **tuple** —— json.dumps 遇 tuple 键直接抛 TypeError（D3 的核心
                        // 陷阱），故必须经 _marks() 转成 "sym|cat" 字符串键。
                        marks?: { breakout: Record<string, boolean|string>;
                                  beauty:   Record<string, string> } };
export type Ctx = { marketIdxPct:number|null; weak:boolean|null; flowFiltered:number;
                    warnings:string[]; ruleResult?:string; config?: Record<string, unknown> };
export type Payload = { schema:number; seq:number; date:string; time:string;
                        durationMs:number; regions:Regions; ctx:Ctx;
                        stocks: Record<string, { klineDate:string; kline:number[][];
                                                 appearances:Row[] }> };
// 复核修 2：`seq` = 本地进程内单调轮号，**仅供展示**（状态条、幂等覆盖列），
// 任何「取最新一轮」的查询一律 ORDER BY date DESC, time DESC。
// 复核补充：§3.1 的 `serialize_view` 必须把 §3.7 的 col_spec 结果填进
// `regions.cols`（静态 ~1KB，随载荷存 D1），否则前端拿不到列定义 —— §3.7 与 §5.1 在此对齐。
```

### 5.2 D1 表（`migrations/0001_init.sql`）

```sql
CREATE TABLE snapshots (
  date TEXT NOT NULL, time TEXT NOT NULL, seq INTEGER NOT NULL,
  duration_ms INTEGER NOT NULL DEFAULT 0, schema INTEGER NOT NULL,
  payload TEXT NOT NULL,      -- {regions,ctx}，目标 ≤200KB（单行硬限 2MB）
  ingested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  PRIMARY KEY (date, time));
CREATE INDEX idx_snapshots_date_seq ON snapshots (date, seq DESC);

CREATE TABLE kline_cache (
  symbol TEXT NOT NULL, kline_date TEXT NOT NULL,
  kline TEXT NOT NULL, appearances TEXT,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  PRIMARY KEY (symbol, kline_date));
```

### 5.3 本地 Python 结构

| 结构 | 来源 | 处理 |
|---|---|---|
| `ScanView`（dataclass） | `scanner/view/model.py:570` | `serialize_view` 转 dict |
| `MainRow`（dataclass，含 `entry: RecommendationRow` TypedDict） | `model.py:547` | `_flat_row` 剥离 `_candidate` |
| `HotCandidate/HistCandidate/OffboardCandidate` | `hot_watch.py:99` 等 | `dataclasses.asdict` |
| `COLS_POOL/COLS_HOT/COLS_HIST` | `model.py:444/477/530` | `col_spec` 投影 → 随载荷下发 |
| `GateResult/GateStats` | `push_gate.py:148/122` | 本地算好上报（D6） |

**注入方式**：列 spec 不是后端硬编码，而是**随 `/api/regions` 下发** → 前端词表与
运行时数据天然一致（与上游需求 D7 同源思路）。

---

## 6. 关键流程时序（ASCII）

### 6.1 正常一轮（端到端 ≤70s）

```text
扫描器            panel_reporter     Workers/ingest      D1          浏览器(Eventsrc)   /regions
  │ build_scan_view    │                 │                │                │              │
  ├─render_terminal ──►│                 │                │                │              │
  ├─push_feishu ──────►│                 │                │                │              │
  ├─report(view) ─────►│ 序列化 ≤50ms     │                │                │              │
  │                    ├─thread(daemon)──►│ 鉴权 401?      │                │              │
  │  (主循环继续 sleep) │                 ├─schema校验      │                │              │
  │                    │                 ├─batch upsert ──►│ 写 snapshots   │              │
  │                    │                 │                ├─(30s后 poll)──►│ event:round  │
  │                    │                 │                │                ├─GET ────────►│
  │                    │                 │                │                │◄─五区块 JSON──┤
  │                    │                 │                │                └─局部重渲      │
  │                    │ ◄── 201 ok ─────┘                │                │              │
```

### 6.2 上报失败（G3 旁路隔离）

```text
panel_reporter          主循环               日志
  ├─POST ──► 超时/5xx
  ├─重试1 (1s backoff) ──► 仍失败
  ├─重试2 (3s backoff) ──► 仍失败
  └─丢弃本轮，记 logs/panel_report.log        │ 主循环不受影响，继续 build 下一轮
                                              │ （面板侧：状态条 seq 不再前进 → 滞后 N 轮转黄）
```

### 6.3 SSE 生命周期（DR-5/D8）

```text
浏览器 EventSource          stream.ts                D1
  ├─GET /api/stream ───────►│                          │
  │                         ├─poll#1 SELECT(date,time)►│
  │◄── event: round ────────┤◄─ {date,time}            │
  │                         ├─ sleep 15s               │
  │                         ├─poll#2（未变 → : ping）   │
  │   …40 次轮询 = 40×15s = 10min（D1 查询 40 次 < 50）…
  │◄── event: bye ──────────┤  主动 close（避 524）     │
  ├─close() + 2s 后重建 ────►│                          │
  ├─重建后先 GET /api/regions 全量对齐 ─────────────────┤
```

### 6.4 保留策略（D5）

```text
cron(scheduled, 每小时:07)  → cron.ts
   ├─循环1: DELETE 今日第61轮起  (LIMIT 1000) ──► meta.changes
   ├─循环2: DELETE 历史日非最后一轮              ──► changes
   ├─循环3: DELETE 超10交易日整日 + kline_cache   ──► changes
   └─直到 changes==0 或 20 次循环 → 记 Workers Logs
```

---

## 7. UI 设计

### 7.1 布局（桌面优先，1440×900 基准）

```text
┌──────────────────────────────────────────────────────────────────────┐
│ 状态条（固定顶部，h=36px）                                             │
│  ◉ 14:32:05  seq 18231  间隔60s  交易中  滞后0轮  入库14:32:11  ⚙全量│
├───────────────────────────────────────────────┬──────────────────────┤
│ ◆ v1 池选（默认序，新票●）                     │ ◆ 飞书过门            │
│ #|代码|名称|涨幅|5日累计|现价|排名|板块|评分|策略 │  通过12·剔除28        │
│ 1|300319|麦捷科技|+4.2%|...                    │  tierA3/tierB6/...    │
│ ...                                           │  [全量] [门内] ◀      │
├───────────────────────────────────────────────┤                      │
│ ◆ v1 回捞                                     │  （右侧为固定侧栏，    │
├───────────────────────────────────────────────┤   高度跟随内容）       │
│ ◆ 沪深飙升·极有可能大涨                        │                       │
│   A 段（榜内）…                                │                       │
│   B 段（榜外）…                                │                       │
├───────────────────────────────────────────────┴──────────────────────┤
│ 页脚：数据源 = 本地扫描器上报 · 只读 · 不构成投资建议（G5）              │
└──────────────────────────────────────────────────────────────────────┘
  点任意行 ──► 详情页 /stock/300319：[K线+成交量] [评分分解] [出现史]
  顶部日历 ──► /history?date=2026-09-25（仅可选日期，FR-D2）
```

### 7.2 区块卡片规格表

| 元素 | 尺寸/行为 | 必备内容 |
|---|---|---|
| 状态条 | 固定，`position:sticky` | 轮次时间、seq、间隔、时段、**滞后 N 轮**、入库时间、全量/门内开关 |
| 区块表 | 宽度 100%，列数由 `colSpec` 决定 | 表头（`colSpec.label`）、行（key 取值）、空态 `—（本轮无数据）` |
| 行 | 悬停高亮、点击进详情 | `●` 新票标记（`isNewEntry`）、红涨绿跌数字 |
| 过门面板 | 右侧固定栏 | 标题固定串 + `gate.stats` 7 字段 + 两态开关（默认全量） |
| 滞后提示 | 状态条变黄 `#f5a623` | `数据滞后 N 轮`（>1 时出现，FR-V1/S5） |
| 滚动条/密度 | 行高 28px，紧凑 | 单屏尽量容纳 60 行不翻页 |

### 7.3 字段 → 控件映射表

| 数据 | 控件 | 数据源 | 快捷项 |
|---|---|---|---|
| `regions.*` 五区块 | `<RegionTable cols={...}>` | `GET /api/regions` | 表头点击 → 临时排序（评分/涨幅/量比，FR-V2） |
| `gate.stats` | `<GatePanel>` + 两态 Segmented | 同上（随载荷） | 切换只换数据源，不改标题 |
| 轮次/滞后 | `<StatusBar>` | `GET /api/meta` + `event:round` | 点 seq → 跳 `/history` |
| K 线 | `<ECharts candlestick + bar>` | `GET /api/stock/{s}/kline` | `days` 切换 60/120 |
| 评分分解 | 三列表格 | `regions.main[].entry.scoreBreakdown` | 非 v1 票显示「无评分分解」 |
| 出现史 | 紧凑列表 | `kline_cache.appearances` | — |
| 日期 | 日历（禁用无快照日） | `GET /api/meta/dates` | 今日/上一交易日 |

**ECharts 关键配置**（A.8 调研）：双 `grid`（上 K 线、下成交量），`xAxis[0]/[1]` 与
`yAxis[0]/[1]` **各自都要配**且 `axisLabel.show=false` 给下方；`candlestick.data` 为
`[open, close, low, high]`（**注意顺序不是 OHLC**）；颜色 `color:'#ef232a'`（涨）、
`color0:'#14b143'`（跌），成交量 `itemStyle.color` 用回调按当日涨跌取同色。

---

## 8. 错误处理设计（四列表）

| # | 场景 | 检测点 | 处理 | 用户/日志所见 |
|---|---|---|---|---|
| E1 | 本地序列化失败（tuple 键等） | `report()` try | 丢本轮 + `_log.exception` | 扫描照常；`panel_report.log` 有 traceback |
| E2 | 上报网络超时/5xx | `_send()` | 重试 1s/3s → 丢弃 | 日志 3 条 attempt；面板滞后 N 轮转黄 |
| E3 | 密钥错误 401 | `bearerGuard` | 立即返回 401 **不重试** | 本地日志 `401`；需改 `.env` + 重启 |
| E4 | `schema` 版本不符 400 | `ingest` | 返回 `SCHEMA_MISMATCH` | 本地记 error，**不丢弃但也不重试**（需升级代码） |
| E5 | 载荷超 400KB | 序列化体积探针 | 截断 `stocks`（新出现优先）+ WARNING | 日志 `payload 512KB > 400KB, stocks truncated` |
| E6 | D1 `overloaded` | `DB.batch` 抛错 | 返回 502 + Workers Logs 告警 | 面板暂时读到旧数据 |
| E7 | Access cookie 过期导致 SSE 403 | `EventSource` `onerror` + 检查 `content-type` | 停订阅、显示「请重新登录」 | 覆盖页重登后自动重建 |
| E8 | SSE 100s 无事件 → 524 | 15s/30s 心跳 + 生命上限 | 主动 `bye` 收口 | 浏览器自动重连（正常路径） |
| E9 | `symbol` 非法/超 `days` | `stock.ts` 正则 | 400 `BAD_REQUEST` / `RANGE_EXCEEDED` | 错误信封，前端 toast |
| E10 | 票不在 `kline_cache` | 查询无行 | 404 `NOT_IN_REPORT` | 「本轮上报未包含该票」（**不补拉**） |
| E11 | `date` 无快照 | `history` 无行 | 404 `NO_SNAPSHOT` | 日历该日不可选 + 提示 |
| E12 | CPU 10ms 触顶 | 平台 `Error 1102` | Workers Logs `exceededCpu` | 连续 3 天 → 升 Paid 或拆 Worker（D4） |
| E13 | 保留任务死循环 | cron 循环计数 | 20 次上限后记日志退出 | 下轮 cron 再试 |
| E14 | 云端整体不可用 | 前端超时 | 保留最后一次快照 + 滞后计数 | 状态条红 + 「云端无响应」，**不白屏** |

---

## 9. 性能设计（预算表）

| 路径 | 预算 | 达标手段 | 验证方式 |
|---|---|---|---|
| 本地序列化 | **≤50ms/轮**（G3） | 纯 dict 操作、`json.dumps` 一次 | `panel_serialize` 内 `time.perf_counter` + WARNING |
| 上报网络（后台） | ≤8s，均值 ~200ms | `(3,8)` 超时、Session 复用 | 日志耗时统计 |
| 端到端 | **≤70s**（G2） | 上报在 `render` 后立即发 | M0 实测 3 轮 |
| 载荷体积 | **≤400KB**（regions 200 + stocks 200） | 增量 K 线 ≤20 票 | 序列化体积探针 |
| Worker CPU | **p95 <5ms**（限 10ms） | 单次 parse、≤10 查询、无 schema 库 | Workers Logs 10 轮压测 |
| API 响应 | P95 ≤300ms | 每请求 ≤3 次查询 + 索引 | 简单计时压测 |
| 面板首屏 | ≤1.5s（G4） | 静态资源 CDN、regions 单请求、骨架屏 | Lighthouse |
| K 线渲染 | ≤1.5s（含 120 根） | ECharts 增量 setOption | 手测 |
| D1 存储 | **26MB**（红线 100MB / 上限 500MB） | 60 轮保留 + 批量 DELETE | cron 后 `SELECT count(*)` + 用量面板 |
| 日请求 | **≈10,400/100,000（10.4%）** | SSE 生命上限受控重连 | Cloudflare Analytics |
| SSE 连接 | ≤40 次 D1 查询/生命周期 | §3.8 轮询上限 | 单连接全程日志 |

---

## 10. 测试设计

| 层 | 用例类型 | 代表用例 |
|---|---|---|
| 单测（pytest） | 结构确定性 | `test_panel_serialize.py::test_tuple_mark_keys_roundtrip`：`{(sym,cat):v}` → `"sym|cat"` → 读回一致 |
| 单测 | 类型剥离 | `test_flat_row_drops_candidate`：载荷中**不存在** `_candidate` 键（防 `asdict` 递归炸） |
| 单测 | 列单源（D7） | `test_col_keys_match_cols_*`：`keys(col_spec(COLS_POOL)) == [c.key for c in COLS_POOL]` |
| 单测 | 体积（D5/D4） | `test_payload_under_400kb`：造 150 票样本 → `stocks` 被截断到 ≤20 且**新出现票保留** |
| 单测 | 失败隔离（G3） | `test_report_never_raises`：monkeypatch `requests.post` 抛 `Exception` → `report()` 正常返回、主线程继续 |
| 单测 | 重试次数 | `test_retry_2_times_with_backoff`：断言调用 3 次、`sleep` 收到 `(1,3)`（**避免空断言**：显式 `assert call_count == 3`） |
| 契约测试 | 两端一致 | 同一 payload 分别喂 Python `serialize_view` 与 TS `Payload` 类型解析 → 字段集合相等 |
| 集成（vitest-pool-workers） | 幂等 | 同 `date+time` POST 两次 → `SELECT count(*)==1`，`seq` 取新值 |
| 集成 | 鉴权（D2） | 错密钥 → 401；`schema=1` → 400 `SCHEMA_MISMATCH` |
| 集成 | 保留策略 | 插入 100 今日行 + 5 历史日 → 跑 cron → 今日剩 60、历史日各 1（**R1 断言**） |
| 集成 | 保留策略跨日边界（复核修 3） | 两日 `MAX(time)` 相同 → cron 后**非末轮行删净**（旧写法会漏删） |
| 集成 | 最新轮不按 seq（复核修 2） | 插入 `date/time` 更新但 `seq` 更小的行 → `/api/regions` 必须返回该新行 |
| 集成 | schema 常量类型（复核修 1） | body 里 `"schema": 2` 字面量 POST → 201（若误放 vars 会 `2 !== "2"` → 400） |
| 集成 | stocks 超配额（复核修 6） | POST 25 票 → 201 且 `stocksStored=20, stocksDropped=5`，**整轮不丢** |
| 集成 | SSE | 连接内 D1 查询计数 ≤40；收到 `bye` 后连接关闭 |
| 端到端 | 三端对齐（M0 核心） | 连续 3 轮：`curl /api/regions` 的行数/首行 symbol/顺序 == 终端输出 |
| 端到端 | 断网隔离 | 断网 10min → 扫描日志无异常、`panel_report.log` 有失败、扫描轮次继续 |
| 端到端 | 鉴权 | 无 GitHub SSO 会话访问 `/api/regions` → 拿不到数据（Access 拦截页） |
| 可视回归 | 渲染 | 空区块显示 `—`、红涨绿跌 hex 值、过门标题含固定串（验收 8 断言） |
| 泄漏检查 | 生命周期 | SSE 断开 50 次 → 云端无悬挂连接（Workers Logs 并发数归零） |

> 运行：`pytest tests/test_panel_*.py`；云端 `pnpm -r test`；
> 全量按仓库 `Verification order`：`ruff check` → `mypy` → `pytest tests/`。

---

## 11. 部署与运行

### 11.1 一次性准备

```bash
# 云端（新仓库 rts-panel-cloud）
pnpm create wrangler@latest cf-panel-api        # 或手写 wrangler.jsonc（骨架可参考 cf-stack-starter）
# wrangler.jsonc 必须含（复核修 7，缺一即暴露读接口）：
#   "workers_dev": false,                                  ← 关掉 *.workers.dev 旁路
#   "routes": [{ "pattern": "panel.<域名>/*", "zone_name": "<域名>" }]   ← 同域，Access 一处覆盖
wrangler d1 create cf-panel-db                  # 记 database_id → 填 wrangler.jsonc
wrangler d1 migrations apply cf-panel-db --local
wrangler d1 migrations apply cf-panel-db --remote
wrangler secret put INGEST_SECRET               # ← 高熵值，别复用任何其它密钥
wrangler deploy                                  # 只在 https://panel.<域名> 可达

# Zero Trust 控制台
Access → Applications → Add：域名 panel.<域名>（CNAME → Pages/Worker route）
  Self-hosted / 路径 /*，排除 /api/ingest
  Authentication = GitHub（R2 定稿）→ 仅勾自己的 GitHub 账号

# 本地（本仓库）
cat >> .env <<'EOF'
RTS_PANEL_URL=https://panel.<域名>/api/ingest   # 同域路径，非 workers.dev（复核修 7）
RTS_PANEL_SECRET=<与 INGEST_SECRET 相同>
EOF
python unified_scanner.py 60                    # 面板自动开始上报
```

### 11.2 部署顺序（M0 → M5）

`D1 建库+迁移` → `deploy worker（ingest 已带密钥）` → **M0 三轮对齐验证** →
`deploy web` → `挂 Access(GitHub SSO)` → `开 cron 保留` → `配 SSE` → `接详情/历史`。

### 11.3 离线/降级预案

| 情形 | 预案 |
|---|---|
| 本地断网 | 上报丢轮，终端+飞书不受影响；恢复后面板自动追平（验收 3/4） |
| 云端挂 | 面板显示最后快照 + 滞后计数（E14），本地照常 |
| `RTS_PANEL=0` | 连模块都不导入，与当前版本行为一致（验收 1） |
| 超出 Free 档 | 触顶表现与处置见 §9/D4/D8（`Error 1027`/`1102`/524） |
| 回滚 | 本地：删 `.env` 两键即可；云端：`wrangler rollback` + Access 摘除 |

### 11.4 版本锁定

Python：**零新增依赖**（R4，`requirements.lock` 不动）。
云端：`pnpm-lock.yaml` + `wrangler.jsonc` 的 `compatibility_date: "2026-09-01"` 入库；
`hono`/`wrangler`/`react`/`echarts` 版本在 lock 中钉死，升级走单独 PR。

---

## 12. 难点调研资料与分析（D1~D8 逐节）★ 分水岭

### D1 跨网上报

- **资料**：`requests` 官方文档 Session/timeout（https://requests.readthedocs.io/en/master/user/advanced/）
  —— *timeout 不会限制总时长，只限制等待响应数据的时间；必须显式设置，否则可能永久阻塞*；
  Python `threading` 官方文档（https://docs.python.org/3/library/threading.html）；
  daemon 线程分析（https://blog.miguelgrinberg.com/post/how-to-kill-a-python-thread）
  —— *daemon 线程不阻塞主进程退出，适合 fire-and-forget*；D1 FAQ（**大 UPDATE/DELETE 必须分批**）。
- **对设计的帮助**：① 定了 `(3,8)` 双段超时（只设一个 timeout 会在连接成功但不回包时挂住）；
  ② 定了 daemon 线程 + 每轮一线程（线程不可被强杀，故必须靠**短超时**控制生命周期，
  28s < 60s 保证不堆积）；③ 保留任务改成分批 DELETE。

### D2 公网安全面

- **资料**：Cloudflare 官方「Validate JWTs」（https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/）
  —— *Access 在前时，Worker 读 `Cf-Access-Jwt-Assertion` 即可，需校验 audience*；
  Workers + Access（https://developers.cloudflare.com/workers/configuration/cloudflare-access/）
  —— *无需显式校验，依赖 header 是标准简单路径*；
  GitHub SSO 教程（YouTube, GitHub SSO & OTP for Workers）确认可用 GitHub 作 IdP。
- **对设计的帮助**：① 定了「Access 挂读 + Bearer 挂写」的**双通道**，上报器不必维护 Service Token（R2）；
  ② 明确 `accessGuard` 属**可选加固**而非必需（默认信任 Access 已拦），避免 M2 范围膨胀；
  ③ 确认 GitHub SSO 可直接用作 IdP，不需额外建邮箱名单。

### D3 序列化隐性类型

- **资料**：Python `json.dumps` 键类型限制（keys must be str/int/float/bool/None，**tuple 会 TypeError**）；
  `dataclasses.asdict` 递归语义（官方文档：递归转换 dataclass 字段与 dict/list/tuple）。
- **对设计的帮助**：确认 `_marks()` 必须手写键转换；确认 `_candidate` 必须**先剥离再 asdict**
  （否则活对象被递归）。两者都进了单测（§10 前两条）。

### D4 CPU 10ms / 50 查询 / 2MB

- **资料**：Workers Limits 官方页（2026-09）：**CPU Free 10ms、Paid 默认 30s 可调到 300s**、
  内存 128MB、并发待响应连接 6、子请求 50/次、*平均 Worker 2.2ms，重活（鉴权/SSR/大载荷）10~20ms*；
  D1 Limits 官方页（2026-04）：**每调用 50 查询（Free）**、单行 2MB、单 SQL 100KB、
  bound 参数 100、单库 500MB、*单库单线程，吞吐 ≈ 1/查询耗时*。
- **对设计的帮助**：① 400KB 载荷（2MB 的 1/5）+ 单次 parse；② 每请求 ≤10 查询（50 的 1/5）；
  ③ **发现「重活 10~20ms」直接超 Free 上限 → 决定不用 zod 这类 schema 库**（§4.2 注释）；
  ④ 明确持续触顶的升级路径（升 Paid / 拆 Worker）。

### D5 存储预算与保留

- **资料**：D1 FAQ —— *大规模 UPDATE/DELETE 必须分批，单条改百万行会超执行限制*；
  D1 单库 500MB（Free）且**该上限不可提升**。
- **对设计的帮助**：60 轮保留策略落成「`LIMIT 1000` + 循环到 `changes==0` + 20 次上限」；
  把 432MB/日 的原始增长压到 26MB（5.2%），给 G6 留出 4 倍红线余量。

### D6 四端一致性

- **资料**：项目内不变式（AGENTS.md：终端全量 / 飞书过门是**故意打破**的对应关系）；
  `scanner/push_gate.py:319` 与 `feishu.py:502` 共用 `apply_push_gate`。
- **对设计的帮助**：门在**本地算好随载荷上报**，云端零逻辑 → 天然与飞书同源；
  省掉云端配置 `config_push`（那会是第 4 份阈值拷贝）。

### D7 列头单源化

- **资料**：`scanner/view/model.py` 的 `COLS_*` 列 spec 含对齐/宽度语义；终端渲染带 ANSI 码。
- **对设计的帮助**：加 `col_spec()` 投影层，列定义**随 API 下发**而非前端硬编码 →
  改列只改一处，且有单测断言 key 序列一致。

### D8 SSE 可用性 —— **本次调研改变了原设计**

- **资料**：
  1. Cloudflare Agents 官方「HTTP and Server-Sent Events」
     （https://developers.cloudflare.com/agents/runtime/communication/http-sse/）
     —— ***"No timeout limits — Workers have no effective limit on SSE response duration"***
  2. Workers Limits：***HTTP 请求无时长硬限制***，客户端保持连接即可继续流式
  3. 社区实测（Cloudflare 论坛）—— ***"Cloudflare will return a 524 if no events are sent for 100 seconds"***；
     另有报告称需 Durable Objects 才能稳长连
  4. **D1 Limits：每次 Worker 调用 ≤50 次查询（Free）**
  5. EventSource 官方运行时 API（Workers 支持）
- **对设计的帮助**：
  ① 事实 1+2 → SSE **可行**，不必上 Durable Objects；
  ② 事实 3 → **15s 心跳**（6 倍安全边际）；
  ③ **事实 4 是原设计的盲点**：长连接内自轮询 D1 会在 10 分钟撞 50 次上限 →
  **新增「生命周期上限 40 次轮询 + 主动 `bye` 收口」**（§3.8），
  同时把预算表的「每 10min 重连一次 = 144 次/日」从假设变成**设计保证**；
  ④ 事实 3 也决定前端必须处理 `bye` 后主动重建（而非只靠浏览器退避）。

---

## 13. 里程碑 → 设计验证映射 + 需求追溯矩阵

### 13.1 里程碑映射

| 里程碑 | 验证的设计点 | 对应章节 | 通过判据 |
|---|---|---|---|
| **M0 上报通路** | **D1 全部 + D3 + DR-1/2/4** | §3.1 §3.3 §4.2 §6.1 §6.2 | 3 轮三端逐条一致；序列化 ≤50ms；拔网线扫描无异常；幂等重放 1 行 |
| M1 静态渲染 | D7 + DR-7 + §7 UI | §3.7 §7 §5.1 | 列 key 序列一致；空区块 `—`；红涨绿跌；**滞后 N 轮提示**（复核修 10：M1 用 `/api/meta` 轮询实现，M3 换 SSE 驱动） |
| M2 鉴权公网 | **D2 全部** | §3.2 §11.1 | 无 SSO 拿不到数据；错密钥 401；`git log -S` 无密钥 |
| M3 SSE+状态条 | **D8 全部（含生命周期上限）** | §3.8 §6.3 §7.2 | 单连接 D1 查询 ≤40；10min 无 524；断线 10s 对齐 |
| M4 个股详情 | DR-9 + FR-D1 | §4.4 §7.3 | 非法 400、缺票 404 文案、K线+分解+出现史 |
| M5 历史+过门+收尾 | **D5 + D6 + 预算** | §3.5 §3.6 §9 | 今日仅 60 行、历史日各 1；过门标题断言；<100MB |

### 13.2 需求追溯矩阵

| 需求 | 设计落点 |
|---|---|
| G1 五区渲染 | §4.3 `/regions`、§5.1 `Regions`、§7.1 布局 |
| G2 ≤70s | §6.1 时序、§9 性能表 |
| G3 零侵入+失败隔离 | §3.1 `report()`、DR-2、§6.2、验收 1/3 |
| G4 详情 ≤1.5s | §4.4 stock 端点、§7.3 ECharts、§9 |
| G5 无第二结论源 | §3.6（门本地算）、DR-9（缺票不补拉）、§7.1 页脚 |
| G6 成本 | §9 请求/存储预算、§3.5、§3.8 |
| G7 公网不失守 | §3.2 五层、§11.1 Access 配置 |
| FR-C1 上报器 | §3.1 完整代码 |
| FR-C2 载荷契约 | §5.1 TS 类型 ↔ Python `serialize_view`、§4.2 校验 |
| FR-C3 D1 表+保留 | §5.2、§3.5、cron §6.4 |
| FR-V1~V3 渲染/排序/过门 | §3.6 §3.7 §7 |
| FR-R1~R2 SSE/状态条 | §3.8 §6.3 §7.2 |
| FR-D1~D2 详情/历史 | §4.4 §7.3 |
| FR-S1 只读元信息 | §4.4（无写端点，`meta/config` 0 查询） |
| NFR 全 | §8 错误表、§9 性能表、§10 测试表、§11 部署 |
| R1 60 轮 | §3.5 SQL、§10 保留策略断言、M5 |
| R2 GitHub SSO | §3.2、§11.1、M2 |
| R3 独立仓库 | §2.3 目录、§11 |
| R4 复用 requests | DR-2、§11.4（零新增依赖） |

---

## 附录 · Open Questions（未决项，实现前需拍板）

1. **SSE 自轮询的查询预算**：40 次是按「每 30s 轮询 + 心跳」估的，若改 15s 轮询则上限要降到 20。
   → M3 实测后定死常量（建议进 `env.ts` 常量表，不散落）。
2. **`ctx.config` 快照字段集**：`REFRESH_INTERVAL/MIN_SCORE/PUSH_TIER_A_MIN/OVERHEAT_ACCUM_MAX`
   之外还要不要带（如 `CATEGORY_HIT_RATE` 表）？带多了载荷涨，带少了页面要另开接口。
3. **Access 是否覆盖 `/api/ingest`（Service Token 双保险）**：R2 定为本期不启用，
   但若 WAF 限速规则配不好，B 路线要能在 1 天内启用 —— 代码上是否预留 `serviceTokenGuard` 钩子？
4. **「10 交易日」按自然日近似**是否可接受？严格实现需引入交易日历（项目已有 `scanner/holidays.py`，
   但云端拿不到）→ 方案：本地把「交易日列表」随 `ctx` 上报，或接受自然日近似。
5. ~~**当日 60 轮的排序键**~~ **已定（复核修 2/3）**：`seq` 是进程内轮号，重启后重排，
   **一律不作排序键**；所有「最新一轮」与保留策略改用 `(date, time)`（`HH:MM:SS` 字典序 = 时间序），
   `seq` 仅展示与幂等覆盖列。跨日保留必须写 `(date,time) NOT IN (...)`。
6. **Windows 代理干扰**：本地 `requests` 是否需要 `trust_env=False`？若扫描机走系统代理，
   上报可能被代理拦 → 建议 `Session.trust_env = False`（直连），M0 验证。
7. **前端是否要 PWA/离线缓存**：Out 未写明，本期不做，但移动端场景（S1）可能后续需要。

---

*编制：design-doc-gen（15 步）· 上游 `docs/ui-panel-requirements.md` v2.1 · 2026-09-29*
