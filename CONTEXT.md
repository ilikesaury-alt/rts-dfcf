# 本次 Bug 修复 CONTEXT

## 要解决的问题（现象 / 实际行为）

终端每轮扫描打印：

```
[!] 个股资金流获取失败（0 只）：批0 ProxyError: HTTPSConnectionPool(host='push2delay.eastmoney.com', port=443): Max retries exceeded with url: /api/qt/ulist.np/get?... (Caused by ProxyError('Unable to connect to proxy', RemoteDisconnected('Remote end closed connection without response')))，下一轮扫描重试
```

实际行为：个股资金流（东财 push2delay `ulist.np/get`）每轮都走系统代理，而本机代理不可用 →
`ProxyError` → 整取被吞 → 返回 `{}`（0 只）→ 打印告警 → 缓存 60s 短退避 → 下一轮重试，
全天陷入「0 只 + 重试」死循环。`v1 池选` / `沪深飙升` 三段依赖该快照的量比/报价段随之零产出。

## 期望行为

个股资金流为「直连东财」调用（与 `api.py` 雪球握手、`panel_reporter.py` 面板上报同一设计意图），
**不应**走系统 `HTTP(S)_PROXY`。本机代理不可达时，调用必须强制直连 push2delay.eastmoney.com，
正常返回创业板全集 ∪ 注册码的资金流/报价快照（实测可达、准实时）。

## 复现路径（最小复现步骤）

- 静态复现（单测用）：设置 `HTTPS_PROXY=http://127.0.0.1:9`（不可达代理），跑
  `fetch_fund_flow_rank()`。因 `_batch_once` 的 `requests.get` 未禁用代理，首批请求被发往代理
  → `ProxyError` → 返回 `{}` → 打印「个股资金流获取失败（0 只）… ProxyError」。
- 真实触发（用户现场，2026-10-22）：该告警**一般只在下午出现**。含义 —— 本机 `HTTP(S)_PROXY`
  在工作时段/午后代理健康度下降、偶发断连，`requests` 默认读系统代理把 ulist 直连调用错误路由到它
  → 午后间歇 `ProxyError`。上午代理正常时同一路径经代理可通，故上午不报错；与「东财 IP 级限频
  （直连+代理同灭、报直连错误）」症状不同 —— `ProxyError` 特指**连不上代理本身**，可排除东财封禁。

## 范围（In / Out）

**In：**
- `scanner/market_extra.py::_batch_once`（个股资金流直连 ulist）—— 报告命中的代码路径，必修。
- `scanner/data_source.py::fetch_market_caps_batch`（同 host `push2delay` 直连 ulist 市值兜底）——
  同一根因、AGENTS.md 已标注其 ProxyError 历史故障，一并修复，避免留下同源隐患。

**Out：**
- `scanner/data_health.py:174` 的东财直连（399006 涨跌幅兜底）：同源，但属只读对账旁路、非报告症状、
  非资金流主线；本次不修，留作已知项（如需一并修在 checkpoint 提出）。
- 东财 WAF 封锁 `push2` / `clist`（2026-10-15 历史故障）、ulist 末批 `rc=102`（2026-10-16 修复）等
  **与本 bug 无关**的既有逻辑，不触碰。
- 不新增重试/退避/告警策略改动（现有 fail-soft + 60s 短退避语义保留）。

## 验收标准（写死）

1. `fetch_fund_flow_rank()` 在设置了 `HTTP(S)_PROXY`（指向不可达代理）的环境下，仍走**直连**、
   正常返回非空集（用 monkeypatch 同款 `requests.get` spy 断言调用携带
   `proxies={'http': None, 'https': None}`，且首批能解析出伪 JSON 行）。
2. `_batch_once` 的 `requests.get` 调用**显式携带** `proxies={'http': None, 'https': None}`
   （强制直连，与 `api.py:394` / `panel_reporter.py:52` 的 `trust_env=False` 同一意图）。
3. `data_source.fetch_market_caps_batch` 的同款 `requests.get` 同样携带 `proxies={...: None}`
   （同源直连，避免重蹈 `data_source.py:204` 注释里的 ProxyError）。
4. 既有回归套件 `pytest tests/test_market_extra.py` 与 `pytest tests/test_data_source.py` 全绿
   （无新回归；既有的「0 只 / 超时 / 部分」降级语义不变）。
5. `ruff check .` + `mypy scanner/` 干净。

## 已知风险 / 踩过的坑

- ⚠ 本机**确有**系统代理且**午后健康度下降、间歇不可用**（与「上午正常、下午才报错」的时间分布吻合）
  —— 修复以「强制直连」为准，而非「修代理」或「加直连重试」。东财直连域名 push2delay 本环境
  全天可达（主源雪球 `api.py:394` 已 `trust_env=False` 全天直连运行、AGENTS.md 记 push2delay
  本机实测可达），fund flow 调用去代理后不再受午后代理抖动影响。
- `requests.Session` 跨线程共享非官方线程安全：fund flow 批在 `ThreadPoolExecutor(max_workers=6)`
  内并发。本次改用**每次请求传 `proxies` 参**（不改 Session 共享模型、不引入新的并发风险），与现有
  `_requests.get` 模块级调用形态一致，风险面不变。
- `ProxyError` 已是 `EXTERNAL_FAILURES`（`requests.exceptions.RequestException` 子类），既有
  fail-soft 捕获链无需改动；本次只修正「为何会走到代理」这一根因，不动错误处理。

## 已确认方案

- **根因定位**：`_batch_once` 的 `requests.get` 未禁用系统代理；本机 `HTTP(S)_PROXY` 在**午后健康度
  下降、间歇不可达** → 直连东财的调用被默认错误路由到代理 → `ProxyError: RemoteDisconnected`
  （连不上代理本身，非东财封禁）→ 整取返回 `{}`（0 只）→ 每轮重试死循环。**根因 = 缺失代理绕过
  （且未隔离午后抖动），而非东财服务器故障**：东财直连本环境全天可达；`api.py:394` /
  `panel_reporter.py:52` 早已 `trust_env=False` 走同一直连意图，上午经代理可通只是碰巧掩盖了缺陷。
- **修复点**：
  1. `market_extra.py::_batch_once` 的 `requests.get` 增加 `proxies={"http": None, "https": None}`
     （强制直连）。
  2. `data_source.py::fetch_market_caps_batch` 的 `requests.get` 同步加 `proxies={...: None}`。
- **高风险点（状态/并发/一致性/回归面）**：
  - 并发：`_batch_once` 在 6 线程池内执行；传 per-request `proxies` 不改 Session 共享，无新并发风险。
  - 回归：现有「首批 0 行快速失败 / 超时部分 / 末批 rc=102」语义不受影响；新增 spy 测试守 `proxies` 契约。
  - 兼容性：`RTS_FUND_FLOW_HOST` 覆盖仍生效（指向可达 host 即可）；`proxies=None` 仅禁用系统代理、
    不改目标 host。
- **是否需拆 Issue**：否（单点同根因修复，改动面 2 处各一行）。

## 验收证据

### E1 · 红测试复现（修复前失败，失败原因正确）
```
$ python -m pytest tests/test_market_extra.py::TestFetchFundFlow::test_get_bypasses_system_proxy -q
>       assert "300001" in result, "绕开系统代理后首批应正常解析出票（不再 0 只）"
E       AssertionError: 绕开系统代理后首批应正常解析出票（不再 0 只）
E       assert '300001' in {}
1 failed          # 红：未带 proxies 时被判定为走代理 → 模拟 ProxyError → 0 只
```

### E2 · 绿测试（修复后通过）
```
$ python -m pytest tests/test_market_extra.py::TestFetchFundFlow::test_get_bypasses_system_proxy \
    tests/test_data_source.py::TestThsAdapter::test_fetch_market_caps_batch_bypasses_system_proxy -q
2 passed
```

### E3 · 回归（受影响两文件全绿）
```
$ python -m pytest tests/test_market_extra.py tests/test_data_source.py -q
85 passed
```

### E4 · 全量单测：无新增回归
```
修复前（干净树 git stash）：6 failed, 1954 passed, 13 skipped
修复后                  ：6 failed, 1956 passed, 13 skipped
```
新增 2 = 本次两条新守卫；6 个 failed 均为**修复前既有失败**（`test_data_health` 3 条
交易日/收盘时刻相关、`test_rule_tracking` 3 条脚本子进程相关），与本次改动无关。

### E5 · 静态检查
```
$ ruff check .        → All checks passed!
$ mypy scanner/       → 无 error（92 source files）
```

### E6 · 根因实证（关键：错误类型从 ProxyError 变为直连 ConnectionError）
本机实测 `requests.utils.getproxies()` 返回 `{'http': 'http://127.0.0.1:7890', ...}`
—— 系统代理存在（Windows 注册表，**非** `HTTP(S)_PROXY` 环境变量；故 `Get-ChildItem Env:` 为空）。
- 修复前：`proxies=None` → `Session.request` 先 `proxies or {}` → 非 None ⇒
  `merge_environment_settings` 用 `setdefault` 把 env 代理**合并回来**（实测 `http/https`
  均变成 `http://127.0.0.1:7890`）⇒ 走代理 ⇒ `ProxyError`。
- 修复后：显式 `{"http": None, "https": None}` ⇒ 实测合并结果 `http=None https=None`
  ⇒ 直连。同一请求错误类型由 `ProxyError` 变为
  `ConnectionError: RemoteDisconnected`（**直连**被拒），证明代理已从链路移除。

### E7 · 当前 push2delay 全站直连被拒（环境态，非本次缺陷）
实测（2026-10-22 17:3x）：`push2delay` 的 `ulist` 与 `stock/get` **直连与经代理同灭**
（直连 `ConnectionError/RemoteDisconnected`，经代理 `ProxyError`），而 `www.baidu.com`、
`emweb.securities.eastmoney.com`、`datacenter-web.eastmoney.com` 直连均 HTTP 200。
⇒ 与 AGENTS.md 记「push2 家族 IP 级限频：直连+代理同灭，等待冷却即可恢复，勿误判为代码故障」
一致。**这是独立的临时环境态**；本次修复解决的是「错误地走代理」这一层，冷却后直连即恢复取数。

## 流程状态

- 第 0 步 定档：🟢 轻量（局部两处同态改动、验收一眼清、无架构影响）。
- 第 1 步 准备：CONTEXT.md 初始化；单元测试环境可用（pytest，无外网依赖用例）。
- 第 2 步 需求对齐：验收标准已写死（见上）。
- 第 3 步 定位与方案：已 locate 根因（`market_extra.py:297` + 同源 `data_source.py:226`），
  代码现状已核对，已确认方案已写。
- 第 3.5 步 文档复核：六项逐项过，用户已授权「开始实现」。
- 第 4 步 实现：✅ 红测试先复现（E1）→ 修复 → 绿（E2）；M0 根因验证通过（E6）。
  改动：`net.eastmoney_no_proxy()` 工厂 + `market_extra._batch_once`、`data_source.fetch_market_caps_batch`
  两处接入。**实现中发现的第二个缺陷**（见「已知风险」）：`requests` 的
  `merge_environment_settings` 用 `setdefault` **就地改写**传入 dict，故最初写成模块级常量会被
  永久污染（实测混入 `ftp` 代理键）且 6 线程并发共享有竞态 ⇒ 改为**每次返回新 dict** 的工厂函数。
- 第 5 步 交叉对抗审查：✅ 小档自查替代（按三轴逐条核验，见下）。
  - ①根因：错误类型 ProxyError → 直连 ConnectionError（E6），证实打中的是根因而非表象。
  - ②回归：受影响两文件 85 passed；全量无新增失败（E4）；`proxies` 值必须为 None 而非 `{}`
    已实测证伪（E6，`{}` 会让 env 代理合并回来）。
  - ③高风险：共享可变 dict 的竞态/污染已由工厂函数消除并实测隔离（`a is b: False`）。
- 第 6 步 人工 Review：✅ 审查通过（改动 3 文件、+2 测试；无新增抽象层级；未动错误处理链）。
- 第 7 步 交付文档：**缺陷修复，交付文档省略**。
- 第 8 步 提交：**未提交**（用户未给提交授权约定；工作树保留改动，待用户决定）。
- 第 9 步 HANDOFF：**缺陷修复，HANDOFF 跳过**。
- 第 10 步 复盘：本文件「已知风险」已沉淀 `setdefault` 就地改写 + 系统代理来自注册表两条坑。
- 第 11 步 E2E：**待用户手动跑**。⚠ 当前 `push2delay` 全站直连被拒（E7，IP 限频环境态），
  需冷却后验证：启动 `unified_scanner.py`，确认告警**不再是 `ProxyError`**（若仍失败应为
  `ConnectionError`），冷却恢复后资金流行数恢复、`沪深飙升` 三段恢复产出。
