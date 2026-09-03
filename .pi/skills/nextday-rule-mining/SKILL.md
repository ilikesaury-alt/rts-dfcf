---
name: nextday-rule-mining
description: 在 rts-dfcf-max 项目中挖掘"次日大涨"（next_day_pct>=7%）命中票共性并提炼可验证规则的完整方法论。当用户要求分析推荐命中票的共性、提炼规则、验证规则稳健性、或复盘某类策略的次日表现时使用。
---

# 次日大涨规则挖掘

从 `scanner.db` 的 `recommendations` 表挖掘"推荐后次日涨幅≥7%"命中票的共性，提炼规则并用样本内/样本外双窗口验证稳健性。

## 数据背景（先读这个）

- 表：`recommendations`，关键字段：`date, time, symbol, name, category, score, percent, trend, accumulated_pct, score_breakdown(JSON), next_day_pct, fwd_3d`
- **同一票同一天有大量盘中快照**（分钟级重复行），必须按 `(date, symbol)` 去重：取 `time` 最大的一行，偏好 `percent` 非空的行（core_dip/pool_pick 的 15:00 盘后行无 percent 但 trend/breakdown 未必全）
- `score_breakdown` 是 JSON，含 `fund_flow_main_pct, list_momentum_bonus, list_streak_bonus, market_cap_bonus, v_st_vol, turnover_bonus` 等特征
- **字段是陆续加的**：`fund_flow_main_pct` 约 2026-08-07 才开始记录；早期窗口做不了资金流规则验证。分析前先统计字段存在率
- `next_day_pct` 由盘后回填（backfill_kline），最新 1-2 天通常是 NULL
- `trend` 标签词表会随版本变化（2026-08 前后不同：早期多"震荡整理/企稳回升"，后期多"放量启动/回踩·到买点"）

## 操作流程

### 1. 写脚本文件，不要管道喂代码

⚠️ **PowerShell 管道（`@'...'@ | python -`）会把脚本里的中文字面量变乱码**（GBK 编码），导致 `trend in {"加速启动"}` 之类的集合匹配全部静默失败（匹配数为 0 但不报错）。
必须用 `write` 工具写成 `.py` 文件（UTF-8）再 `python 文件名` 执行。用完删除，或放入本 skill 的 scripts/ 目录。

脚本开头：

```python
import sys
reconfigure = getattr(sys.stdout, "reconfigure", None)
if reconfigure is not None:
    reconfigure(encoding="utf-8")
```

### 2. 加载 + 去重

直接用 [scripts/rule_eval.py](scripts/rule_eval.py)，它封装了加载、去重、命中/未命中特征对比、规则双窗口评估。自定义规则时复制它改 `RULES` 列表。

```bash
python .pi/skills/nextday-rule-mining/scripts/rule_eval.py                 # 默认规则集
python .pi/skills/nextday-rule-mining/scripts/rule_eval.py --since 2026-06-01  # 指定起点
```

### 3. 对比分析

- 命中组 `next_day_pct >= 7` vs 未命中组，比较**中位数**（分布偏斜，均值易被极端值带偏）
- 类别（category）、trend 标签按"命中率 = hit/(hit+miss)"排序看，**必须同时看样本量**，hit<5 的标签不下结论
- 当日涨幅分桶（<0 / 0-3 / 3-6 / 6-9 / ≥9）分别看命中率

### 4. 样本内/样本外双窗口验证（核心，不可省）

- 样本内：特征字段齐全的近期窗口（如 2026-08-01 起）
- 样本外：更早的窗口（如 2026-05-28 ~ 07-31）
- 规则只在样本内有效、样本外反转 → 判定过拟合，**放弃**（实例：`list_momentum_bonus>=12` 样本内 +6.9pp，样本外 -5.6pp）
- 样本外组合选出数 <30 条时标注"样本小，置信度低"

### 5. 输出结论的形式

- 每条信号给出：两窗口各自命中率、相对基线提升(pp)、样本量
- 明确区分"稳健（双窗口同向）"与"失效/样本不足"
- 最终规则用一句话概括 + 双窗口对照表
- 提醒：规则排序口径是 `next_day`（次日≥7%），回测默认 `--hold-days 3`，两者兑现率差异大

## 已知结论

见 [references/findings.md](references/findings.md)（按日期追加，新分析不要覆盖旧记录）。

## 历史教训

- PowerShell 管道中文乱码 → 集合匹配静默失败（见上）
- `--buy-at open` 回测被拒绝：信号收盘后才产生，验证 P&L 用 `--buy-at close`（见 AGENTS.md）
- 改权重/阈值不影响历史 `recommendations`（分数是冻结的），验证需 `portfolio_backtest --rescore`
