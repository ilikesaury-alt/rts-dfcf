# legacy/ — 已下线的离线验证 / 概率子系统快照

归档于 2026-11-04。这些文件**不参与主程序、不进 CI、不计覆盖率、不在 ruff/mypy 扫描范围**。

## 为什么归档

逐条核对当前终端主链路（`unified_scanner.py → scan_with_raw → build_scan_view → display → push_feishu`）
后确认：下面这批模块**没有任何生产 import**。它们服务的 `_p` 概率排序、OR 常数校准、
样本外验证门、P&L 回测，都建立在已被删除的评分/终选链上；终端 v1 池选的排序键
（新票 → 类别优先级 → 榜位 → 主力净占比 → 形态）和「类别·先验」列都不读它们。

典型症状：`test_nextday_calib.py::test_snapshot_is_fresh` 按日历变红（快照 49 天未更新），
但它守的 `nextday_prob.py` 常数没有任何活消费方 —— 是「常红的守卫等于没有守卫」的实例，
而不是真实漂移信号。

## 目录

```
legacy/scanner/   nextday_prob / nextday_calib(+json) / rule_validate /
                  historical_rescan / portfolio_backtest / walkforward /
                  triple_barrier / model_bucket
legacy/scripts/   nextday_calib_ab / rerank_backtest / rerank_llm_backtest /
                  rule_change_tracker
legacy/tests/     上述模块的配套测试（10 个）
```

## 刻意没归档

- `scanner/nextday_attribution.py`：`prevday_perf.py` 与 4 个复盘脚本仍在用它的
  `DEFAULT_THRESHOLD` / `hit_stats`。
- `scanner/ai_digest/rerank.py`：AGENTS.md 指定的 Phase 2 证据载体。
- `scripts/offboard_nextday.py`（含 `--repair-next-day`）、`scripts/shadow_feature_backtest.py`：
  B 段运维与唯一裁决依据，仍在用。
- `scripts/replay_source_swap.py`：AGENTS.md 记录的 `scan_with_raw` 离线回放人工比对入口。
- `scripts/panel_m0_check.py`：终端三段段标题的守卫。

## 怎么复现 / 复原

文件是**冻结快照**，不是可安装包：内部仍写着 `from scanner.X import ...` 的原路径，
直接 `PYTHONPATH=legacy` 跑不通。要复现某次历史结论：

1. 用 `git show <归档前 commit>:scanner/xxx.py` 看原文件；或
2. `git mv legacy/<相对路径> <原路径>` 把需要的几个文件搬回原位（按需补齐相互依赖），
   跑完再搬回来。

归档前 commit：`8c6b4a3`（`feat(view): v1 池选改显类别先验 + 确定性矛盾提示，下线 AI 排序`）。

⚠ 归档代码里的口径/证据数字是**当时**的，不要拿它当当前生产口径。当前唯一口径见
`AGENTS.md`「目标函数」：次日 ≥7% hit 率，效率是前提（单只票 ≤ 2 分钟）。
