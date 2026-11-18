# 网页版（启动台 + 面板 v7 + 契约 v3）

> 2026-11-17。三件事一起落地：**启动台**（页面先起、从页面拉扫描器）、
> **契约 2→3**（网页补齐终端的两块区）、**面板视觉返修**（右栏挤爆 / 公式墙 / 0 行空块）。
> 原型见同目录 `prototype.html` / `prototype-launcher.html`（静态稿，仅设计参考）。

## 一、工作流程

```
① 起启动台（常驻，任何时刻都能打开）
   python -m scanner.web_launcher          # 或双击 start-web.bat
        │  绑 127.0.0.1:8901
        ▼
② 浏览器 http://127.0.0.1:8901/
   GET /api/console/health   → 拿一次性 token（响应头 + data 双下发）
   GET /api/console/status   → 状态由「单实例锁 + 面板 /api/meta」现算
        │
        ├─ 未运行 → 点「启动扫描」
        │    POST /api/console/start
        │      ① 锁被占？→ 409 ALREADY_RUNNING（**不杀进程**，只提示）
        │      ② spawn [当前解释器, unified_scanner.py, (可选附加参数)]
        │         env: RTS_LOCAL_PANEL=1 · RTS_TASKS=1 · PYTHONUNBUFFERED/IOENCODING
        │         从不注入 --no-lock（单实例锁是唯一互斥保障）
        │      ③ 返回 202 {state:"starting"}，页面每 1.5s 轮询状态
        │
        └─ 运行中 → 面板**就地**嵌进同一页（iframe，src = 8899）
             ├ 用户视角：打开一个页面 → 点启动 → 等一会 → 数据就在这刷
             ├ 「在新标签打开」/「停止扫描」在顶部细状态栏
             └ 用 iframe 而不是反代：面板仍是它自己的源（Origin 自洽、动作面零改动）

③ 面板（8899，扫描器进程内）  每轮：scan → build_scan_view → 终端/飞书
                              → cache_latest(serialize_view(...)) → 前端 60s 轮询
④ 停止：POST /api/console/stop → terminate(→kill) → 状态回 idle
```

**为什么不反代**：动作面要求 `Origin` 逐字等于 `127.0.0.1:<面板口>`（`tasks_server._allowed_hosts`）。
启动台若反代一切，浏览器 Origin 会变成 8901 而面板口是 8899 ⇒ 必然 403；要修就得把 8901
也加进白名单 = 给「能触发本机命令」的面再开一个入口。跳转方案让两个页面各自同源，**安全面零改动**。

**端口为什么不能合并**：启动台 `8901` / 面板 `8899` / 动作面 `8900`。面板口刻意不动 ——
它与 `tasks_server` 的 Origin 白名单、`tests/test_panel_web_cols.py` 的既有约定是绑定的。

## 二、契约 v3（补齐两块区）

| 键 | 内容 | 来源 |
|---|---|---|
| `regions.onboard` | 榜内异动段（仅 T1） | `ScanView.onboard_rows`（本就有，之前没进载荷） |
| `regions.crosssection` | `≡` 横截面层的结构化投影 | `crosssection.snapshot_payload()`（新增，与终端文本块同源） |
| `regions.cols.onboard` | = `COLS_HOT`（同表头） | `view.model.COLS_HOT` |
| `regions.cols.crosssection` | 面板专属列 | `crosssection.COLS_CS`（新增） |

`SCHEMA_VERSION` **三处同批改**：`scanner/panel_serialize.py` / `rts-panel-cloud/worker/src/serialize.ts`
/ `worker/test/api.test.ts`。云端 `payload` 是 `{regions, ctx}` 原样 JSON ⇒ **无需改表结构**。

⚠ 两个新键在前端都是**可选**：重启回放会灌入旧的 schema=2 快照（m021），缺键一律按空处理。
这条路径有实测（盘后启动扫描器，面板先显示 20 天前的旧快照，不白屏）。

## 三、面板 v7 修了什么

| 硬伤 | 之前 | 现在 |
|---|---|---|
| 右栏挤爆 | 1032px 下右栏 ~200px，`飞书过门 — 下一张卡会推什么` 竖排折断、页脚被裁 | ≤1180px 去掉分栏，过门整块落到主区下方同一条滚动流（`useNarrow`） |
| 公式当正文 | 每段把 `排序=…` 整段公式铺在标题下 | 收成「口径 ▾」，点开才铺原文（原文逐字保留，守卫仍查源码原串） |
| 0 行占整块 | `v1 回捞 0 行` 一整个空区块 | 默认收起成一行头，点开才看空态说明（`foldWhenEmpty`） |
| 缺两块区 | 无榜内异动、无横截面 | 三段并列 + `≡` 区块（持续 / 新进 两组） |

## 三·五、启动器为什么用 .vbs（而不是 .bat）

`.bat` 由 `cmd.exe` 执行，**必然**闪一个控制台窗口（`start /b` 也闪）。`start-web.vbs`
用 `WScript.Shell.Run(cmd, 0, False)`：第 2 参 `0` = 隐藏窗口、第 3 参 `False` = 不等待，
且解释器用 `pythonw.exe`（无控制台的 CPython）⇒ 双击后**没有任何窗口**，
启动台自己在就绪后调 `webbrowser.open` 打开浏览器。

`.bat` 保留：需要看启动日志时用它。两者行为相同，只差窗口。

⚠ 三个双击专属的坑（2026-11-17 实测，都已修）：

1. **`.vbs` 必须存成 UTF-16LE + BOM**。WSH 只认 ANSI 或 UTF-16LE；UTF-8（无 BOM）
   会被当 ANSI 读 ⇒ 中文注释直接触发 `(14, 1) 语法错误`，双击「没反应」。
   已用 `write_bytes(text.encode("utf-16"))` 固化。
2. **端口必须独占绑定**（`allow_reuse_address = False`）。Windows 上 `SO_REUSEADDR`
   允许两个进程**同时**绑同一口 ⇒ 第二次双击静默起了第二个启动台，停止按钮随机失灵。
   关掉后重复启动会得到 `WinError 10048`，`main()` 转成「打开已有页面」并退出 0（幂等）。
3. **`pythonw` 下没有控制台**（`sys.stderr is None`）⇒ 任何异常都静默消失。
   `_run_guarded()` 把 traceback 追加到 `logs/web_launcher_crash.log`；
   双击没反应时先看这个文件。

## 四、验证

```bash
python -m pytest tests/test_web_launcher.py tests/test_crosssection_payload.py \
                  tests/test_panel_serialize.py tests/test_panel_contract.py \
                  tests/test_panel_web_cols.py tests/test_panel_web_hygiene.py -q
ruff check . && python -m mypy scanner/
cd rts-panel-cloud/web && npx tsc --noEmit && npx vite build
```

端到端（真起 launcher → 真拉扫描器 → 真停止）：`logs/_smoke_launcher.py`（一次性脚本，
不入库；`logs/` 已 gitignore）。

## 五、踩过的坑（都已在代码里注释）

1. **`start()` 持锁调 `state()`** → 不可重入锁自死锁，页面永远「starting」。已拆成
   `state()` / `_state_locked()`。
2. **urllib 吃 Windows 系统代理**（本机 Clash `127.0.0.1:7890` 写在注册表）⇒ 就绪探针
   被路由进代理，永远探不到 `ready`。已显式 `ProxyHandler({})`。**回环永不走代理**。
3. **GBK 控制台 + emoji** ⇒ `python -m scanner.web_launcher` 一启动就
   `UnicodeEncodeError`。已在 `main()` 里 `reconfigure(encoding="utf-8")`（与
   `unified_scanner.py` 同款）。
4. **盘后不会有新轮次**：扫描器打印 `🌙 非交易时段 | 下次交易 …` 就睡到次日 09:30，
   面板只回放旧快照。截图/验收要看新区块得用 `logs/_preview_v3.py`（构造载荷）。
5. **拒绝请求要先排干 body**：四层校验在读书之前就 403，服务端关连接而客户端还在写
   body ⇒ Windows 抛 `ConnectionAbortedError (10053)`，客户端看到「连接被重置」而不是
   那枚 403。单跑不撞、跑全量才撞（回归测试
   `test_rejected_post_still_returns_clean_403_with_body_in_flight` 送 2KB body 放大窗口）。
   ⚠ `scanner/tasks_server.py` 有**同款**潜在问题（多轮 400/403 路径不排干 body），
   本次刻意未动（不在改动范围内），留作跟进项。