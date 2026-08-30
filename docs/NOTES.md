# Notes

历史记录、详细解释、变更日志等非关键参考信息。

## P&L 口径

排序/档位/🎯 画像全部校准于 `next_day`（次日≥7% hit），但组合回测默认 `--hold-days 3`。二者不是同一目标——例：rebound 次日 +1.30%/hit 17.9%（全场最优）却 3 日持有 −12.80%（全场最差）。改权重/阈值前先确认你在优化哪个口径，需要时加 `--hold-days 1` 单独验证次日逻辑能否覆盖交易成本。

## `--buy-at open` 被拒绝的原因

AGENTS 旧文档写的 `--buy-at open` 现已被前视偏差守卫挡下——信号收盘后才产生，无法以当日开盘价买入。必须用 `--buy-at close` 对齐 cum 口径。

## Fail-open 设计教训

2026-08-29：宽泛捕获曾把一个回归测试里的 `NameError` 吞成"通过"，该测试守护的 bug 实际从未被验证过。编程错误必须冒泡到 `unified_scanner` 主循环兜底（记录完整 traceback 后下一轮重试）。仅在资源清理（`conn.close()` / 文件句柄）场景可 `except sqlite3.Error: pass`。

## Feishu webhook 历史

`bb7d421` 曾提交过明文 token（可在 `git log -p -S "open.feishu.cn" -- scanner/config.py` 中还原）。该 bot 需轮换；历史清理由 `git filter-repo --replace-text` 单独排期（会改写历史，需协调后 force-push）。

## 测试记录

- 2026-08-29 实测：非 smoke 全绿（1023 passed / 14 skipped）。此前记录的 `test_data_source.py` / `test_market_extra.py` / `test_robustness.TestSuperviseLoop` 失败已不再复现（pandas 已安装）。若再次出现，先确认是环境问题还是真回归。
- 空转断言教训（2026-08-29）：`tests/test_comeback.py` 的 stale-kline 回归测试因 comprehension 里的 `NameError` 被上游吞掉，断言全部 vacuously pass 长达一年。
