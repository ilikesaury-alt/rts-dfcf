# 代码审查报告（2026-09-29）

范围：scanner/ 全部 87 个模块 + 根目录 12 个脚本，约 3.2 万行。所有「中」级发现均已对照调用方上下文人工核验。

## 总体评价

防御密度高于平均水平：数据入口单点强转（`_num`/`to_float`/`make_kline_bar`）、`EXTERNAL_FAILURES` 边界族、DB 层统一 WAL/busy_timeout、迁移账本+幂等、SQL 全参数化、除零守卫齐全。剩余风险集中在三类系统性盲区：

1. **AttributeError/TypeError 不在 `EXTERNAL_FAILURES`**（utils.py:17-23），但多处解析代码恰好可能抛这两类异常 → try 形同虚设，异常穿透降级路径直达主循环兜底（整轮扫描丢失）。
2. **`dict.get("data", {})` 对 JSON null 误用**（key 存在值为 null 时默认值不生效）→ `.get` 链上抛 AttributeError，api.py 3 处同族。
3. **异常处理器/失败路径里的副作用**（print 到已断的 stdout、失败被当完整快照）。

## 中危（9 项，均已核验）

| # | 位置 | 问题 | 影响 | 修复 |
|---|------|------|------|------|
| M1 | unified_scanner.py:624, 632 | 主循环 `except requests.RequestException` / `except Exception` 处理器内直接 `print(...)`。若 stdout 管道已断（tee 读端被杀、SSH 断连）且本轮首个 print 前就抛异常（最常见：fetch_biaosheng 网络异常），处理器内 print 再抛 BrokenPipeError——except 处理器内的异常不被同级 handler 捕获 | **守护进程直接终止**（唯一能杀死守护进程的路径），违背本文件「输出异常降级不崩溃」目标；627 行的 OSError handler 做法正确（先 `_log_exception` 再 `_silence_stdout`） | 两个处理器改为先 `_log_exception` 记文件、再 `_silence_stdout()` 后输出；或 print 包 try/except OSError |
| M2 | scanner/market_extra.py:76-77 | `_get_ak()` 快路径 `if _ak is not None: return _ak` 把失败哨兵 `False` 当模块返回（87 行的 `is not False` 收口只护慢路径）。akshare 未安装时 `_ak=False`，后续调用返回 `False`；fetch_zt_pool:151 只判 `ak is None` → 对 bool 调 `ak.stock_zt_pool_em` 抛 AttributeError（不在 EXTERNAL_FAILURES） | akshare 缺失环境下每轮 AttributeError 穿透 collect_market_extra 的 fail-soft 承诺直达主循环 → 行情增强（涨停池+资金流）功能全灭 | 快路径改 `if _ak is not None and _ak is not False: return _ak` |
| M3 | scanner/market_extra.py:243-251 | 首页拉取失败时 `_page(1)` 吞异常返回 `([], None)`（:212-213）→ `total = total or len(first)` 得 0 → `total_pages=1` → `box["done"]=True`。失败与「非交易日空表」不可区分 | 空结果被当**完整**快照按默认 300s TTL 缓存，无告警无短退避，违背模块声明的失败纪律 → TTL 内全市场资金流静默缺失，榜外异动区（B 段）空转 | `total is None and not first` 时按失败路径处理（走 `_last_ff_partial`/短退避分支），不置 `done=True` |
| M4 | scanner/api.py:248（同族 ：702） | `resp.json().get("data", {}).get("item", [])`：响应为 `{"data": null}` 时 `.get("data", {})` 返回 None → AttributeError。fetch_market_index 在 orchestrator.py:267 无本地 try；FallbackAdapter._call 也只捕 EXTERNAL_FAILURES | 雪球限流/反爬返回 null data 时整轮扫描丢失（主循环兜底救回但本轮无数据）。api.py:445 的 isinstance 防御是对齐范本 | 改 `(resp.json().get("data") or {}).get("item", [])` 同族写法 |
| M5 | scanner/api.py:330 | `fetch_kline` 的 `resp.json().get("data")` 在 try（325-329，只包 `_request_with_retry`）之外：非法 JSON（反爬 HTML/截断响应）抛 ValueError（在 EXTERNAL_FAILURES 内但**在此 try 外**），顶层非 dict 抛 AttributeError | DATA_SOURCE="xueqiu" 单源模式下 XueqiuAdapter.fetch_kline（data_source.py:105）无捕获，异常直传主循环，整轮丢失 | 解析段纳入 try，整体包 EXTERNAL_FAILURES 返回 None |
| M6 | scanner/concept.py:75 | `data.get("ssbk", [])`：`data = resp.json()`（:69）为非 dict（东财反爬返回 JSON 数组/字符串）时抛 AttributeError；try（66-72）只包请求与解析，此行在 try 外，线程池 `fut.result()`（:104）也只捕 EXTERNAL_FAILURES | 异常穿透 `_fetch_many` → `_collect_concepts` → orchestrator.py:344 的 try 接不住 → 整轮丢失 | `if not isinstance(data, dict): return []` |
| M7 | scanner/db/queries.py:100-111 | `get_cached_market_caps` 的两个 `conn.execute` 位于 try（113 行）之外，违背 docstring「兜底 fail-open 返回 {}」契约 | orchestrator.py:124 调用处无本地 try：库锁/磁盘等瞬时 sqlite3.Error 使整轮扫描丢失，而非仅市值兜底失效 | try 起点上移覆盖两个 execute，捕 sqlite3.Error 记 warning 返回 {} |
| M8 | scanner/db/migrations.py:490-495 | `run_migrations` 只捕 sqlite3.Error 做回滚；`m.up(conn)` 抛其他异常（迁移代码的 TypeError/KeyError）时跳过 rollback 且事务保持打开上抛（get_conn 为隐式事务模式） | 破坏「失败即回滚、下轮重试」保证：打开事务持有写锁，上层宽捕获后复用同一连接会阻塞主循环写入；半执行 DDL 可能被下次 commit 落库且账本未记 | 捕获放宽为 BaseException 执行 rollback 后再 raise |
| M9 | scanner/ranking.py:187-195 | `build_accum_map` 直接读 `e.get("_candidate")` 的 kline.dimensions，绕过 `fresh_candidate` 单源助手——与同文件 ：148/:236/:254 的既定不变量冲突（fresh_candidate docstring 明确：stale 掉榜快照与双挂票类别错位快照不得参与累计门槛/五日累计列） | 掉榜行消费冻结在掉榜时刻的 `accumulated_incl_today`（≠推荐时刻落库口径），双挂票吃错位类别 dims → 过热门槛（OVERHEAT_ACCUM_MAX）与 entry_tier/composite_tier 档位判定被污染 | 比照 `_nextday_entry_accum` 改为 `c = fresh_candidate(e)` 后再读维度 |

## 低危（按主题归组）

**解析/空值防御缺口**
- scanner/candidates.py:425,427-428：`cap_data.get("market_cap", 0)` 的默认值在 key 存在值为 None 时不生效 → `cmc > 0` 抛 TypeError。当前生产方保证非 None，属陈旧缓存路径的潜在雷。改 `or 0` 收口。
- scanner/api.py:680, 921：`_normalize_minute_item` 的 timestamp 未 `_num` 强转（volume/current 均已收敛），字符串 ts 使 analyze_minute_trend:921 的 `ts / 1000` 抛 TypeError 且不在任何捕获元组。
- scanner/analysis.py:261：`_vol_peak_ratio` 的 `max(window)` 空列表抛 ValueError（:262 已有同函数空防御，:261 漏）。当前调用方均有 len>=5 前置，属未来新增调用方的雷。

**口径/语义问题**
- scanner/walkforward.py:234, 191-193：过热判定直接用 DB 落库 `accumulated_pct`（不含今日口径），阈值 50 校准于含今日口径——walkforward 的过热因子检验存在系统性口径错位。
- scanner/feishu.py:640：去重键 `_view_symbols` 用 push_gate 全通过集，而卡片实际只画截断后的 `gate.main[:top_n]`——截断线外票的进出翻转 has_change 却不改变卡片内容 → 触发内容相同的多余推送。
- scanner/fundamentals.py:271-279：THS 增量拉取进行中且当前命中 0 时每轮落 pywencai 兜底，问财一旦成功按日级 TTL 缓存，把 THS 增量续传遮蔽一整天（两源口径混合）。
- scanner/walkforward.py:68-70：`--test 0`（或负数）时 `i += test_days` 不前进，while 死循环挂起。入口校验 test_days >= 1。
- today_report.py:265：`--top 0` 时 0 为 falsy 走 else 返回**全部**，语义与预期相反。改 `if top_n is not None`。
- diag_health.py:53,62：SQL 只命中最新一天，与「最近 5 个交易日」标签不符，诊断口径失真。

**资源/健壮性**
- scanner/backtest.py:386-387：`conn.row_factory = sqlite3.Row` 对连接**永久生效**，函数返回后同连接后续 fetch 拿到 Row（恒真值、不等于 tuple），影响 backtest.main 与 nextday_attribution 传入的 conn。入口保存原 row_factory，finally 恢复。
- scanner/backtest.py:814-822、scanner/portfolio_backtest.py:843-910：main 的 `conn.close()` 不在 try/finally 内，异常路径写事务悬挂、写锁延迟释放。
- backfill_kline.py:115、repair_kline.py:38：`sqlite3.connect` 未设 timeout（默认 5s），与 60s 一轮的常驻扫描器并发写 daily_kline 易撞锁；repair_kline.py:66-88 写入循环在 per-symbol try 之外，异常时整轮比对零提交作废。加 `timeout=15` 并把写入纳入 try。
- backfill_market_index.py:104：conn 无 close（一次性脚本，危害有限）。

**脚本入口**
- stock_report.py:32：`300319.SZ` 清洗后变 `300319.`，不匹配 `^\d{6}$` 落名称 LIKE 分支 → 误报「未找到」。先 `re.sub(r"\.(SZ|SH|BJ)$", "", ...)`。
- prevday_perf.py:201：`_render` 的 `dates[0]` 未判空（JSON 分支 ：478 有防护，文本分支漏）→ 新库/未回填时 IndexError。
- today_report.py:43：无条件 `reconfigure(encoding="utf-8")`，pythonw 下 import 阶段崩溃；项目其他入口均有 getattr 防护。
- diag_capture.py:34：`dates[0]/dates[-1]` 未判空；:19 与 diag_health.py:15 硬编码 `scanner.db` 不走 `RTS_DB_PATH` → 分支环境下静默读错库。
- query_summary.py:21、query_today.py:21：默认日期为日历日「昨天」无交易日回退 → 周一/节后恒空报告。无结果时回退 `MAX(date)`。

## 无问题确认（重点核验过）

scanner/db/dal.py、schema.py、data_source.py、matcher.py、validator.py、enhancer.py、push_gate.py、hot_watch.py、offboard_watch.py、view/render.py、view/model.py、core_themes.py、nextday_attribution.py、nextday_calib.py、historical_rescan.py、historical_watch.py、data_health.py、rule_validate.py、model_bucket.py、triple_barrier.py、single_instance.py（TOCTOU/PID 重用/锁残留均被设计正确规避）、orchestrator.py（本人精读：防护密度高，降级分支口径一致）。

## 最需要优先处理

1. **M1（unified_scanner.py:624,632）**——唯一能终止守护进程的路径，修复成本两行。
2. **系统性盲区 M4+M5+M6**——同一根因（EXTERNAL_FAILURES 不含 AttributeError/TypeError + null 解析族）。建议除逐点修复外，考虑在解析边界统一 `(x or {})` 惯用法，并在关键 adapter 入口补 AttributeError/TypeError 的显式测试。
3. **M2（market_extra._ak）**——一行修复，否则 akshare 缺失环境每轮必炸且功能全灭。
4. **M8（migrations 回滚）**——迁移失败是低频高损事件，回滚缺口会造成写锁阻塞主循环。
5. **M9（ranking.build_accum_map）**——静默口径污染，直接影响过热判定与档位，且违背项目自己 2026-08-24 收口的单源不变量。

---

## 修复记录（2026-09-29，同日完成）

M1-M9 全部修复，共改 7 个文件：unified_scanner.py、scanner/market_extra.py、scanner/api.py、scanner/concept.py、scanner/db/queries.py、scanner/db/migrations.py、scanner/ranking.py。低危项未动（待用户确认范围）。

验证：`ruff check` 通过；`pytest tests/` 1727 通过 / 13 跳过 / 2 失败——2 个失败（test_rule_tracking.py 的 test_doc_exists / test_self_consistent）在未修改的干净树上同样失败（缺 docs/rule-baseline-2026-09-11.md），为存量问题，与本次修复无关。目标模块 140 个测试全绿。

注：工作区另有本次会话之前遗留的未提交改动（scanner/view/render.py、tests/test_display.py），未触碰。
