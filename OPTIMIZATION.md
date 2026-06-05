# 选股策略优化分析

> 基于 `scanner/` 模块通读，按影响程度分层

---

## 一、逻辑缺陷（可能影响选股准确性）

### 1. `accumulated` 索引偏移

**文件**: `analysis.py:21`, `:108`, `:186`

三个分析函数统一用 `pcts[-6:-1]` 取"5日累计"。K-line 来自已完成的交易日（不含今天），`pcts[-1]` 是最近一个完整交易日。`pcts[-6:-1]` **跳过了最近一天**，实际只统计了前5天中的4-5天。

**建议**: 改为 `pcts[-5:]`，取最近5个完整交易日。

---

### 2. Volume surge 检测滞后一天

**文件**: `analysis.py:26-29`, `:122-126`, `:193-197`

`vol_ratio = volumes[-1] / avg_vol` 用的是 **昨天** 的日K线成交量。如果今天放量突破，日K线数据里看不到，`volume_surge` 要到明天才会触发。而 `stock.percent`（今日实时涨幅）是新鲜的，两者口径不一致。

**建议**: 用分时API的实时成交量替代日K线数据，或至少加一条备注说明此滞后。

---

### 3. New Face v_shape_reversal 用 `pcts[-1]` 而非 `stock.percent`

**文件**: `analysis.py:42`

```python
v_shape_reversal = accumulated < -8 and volume_surge and today_pct > 2 and pcts[-1] > 3
```

`pcts[-1]` 是昨天日K涨幅，`today_pct` 才是今日实时涨幅。V反确认应该看今天而不是昨天。

**建议**: 将 `pcts[-1] > 3` 改为 `today_pct > 3`（即 `stock.percent > 3`），与 `today_pct > 2` 合并或取代。

---

### 4. `get_recent_symbols` vs `get_symbol_appearances` 日期口径不一致

**文件**: `database.py:68` vs `:90`

- `get_recent_symbols`: `date >= lookback AND date < today`（不含今天）
- `get_symbol_appearances`: `date >= lookback`（含今天）

靠 `orchestrator.py:55` 手动 `if a['date'] < today` 补漏，隐式约定容易引入Bug。

**建议**: 统一为含今天或不含今天的语义，去掉手动过滤。

---

### 5. New Face 弱形态过滤可能误杀

**文件**: `analysis.py:18-19`

```python
if not has_crash_day and down_days >= 3 and sum(recent_5_pcts) < 5 and today_pct < 5:
    return None
```

如果最近5个交易日有3天下跌但累计跌幅很小（如 `-0.5%, -1%, +2%, -0.3%, +3%`），今天涨4.9%，仍被过滤。但这是典型的 **底部企稳** 形态。

**建议**: 增加累计跌幅的绝对值条件（如 `sum(recent_5_pcts) > -5`），只有真正跌多的弱形态才过滤。

---

## 二、策略精细化空间

### 6. Old Face 历史强度用聚合指标而非单一阈值

**文件**: `orchestrator.py:59-62`

现在是 `any(percent >= 4)` 一刀切。

**建议**: 改为最近N天内：
- 最大涨幅 ≥ 4%，或
- 至少2次上榜且平均涨幅 ≥ 2.5%

---

### 7. 市值过滤偏松

**文件**: `config.py:13`

`MAX_MARKET_CAP = 500亿`，但 小而美 加分标准是 ≤100亿/≤50元。500亿的硬上限形同虚设。

**建议**: 降到 200亿 甚至 100亿，与 小而美 加分标准对齐。

---

### 8. 板块分类用关键词匹配太粗糙

**文件**: `sector.py`

"阳光" → 新能源，但实际上很多不相关公司名称含"阳光"。会错误触发板块集群加分。

**建议**: 从雪球批量行情 API 的 `industry` 字段获取实际行业数据，替代名称关键词匹配。

---

### 9. CSV 日志截断前5名

**文件**: `logging.py:17`

`new_faces[:5] + momentum[:5] + old_faces[:5]`，超过5只的类别后面的被静默丢弃。

**建议**: 改为全量写入，或可配置截断数。

---

## 三、性能与健壮性

### 10. Market cap 批量的单只回退

**文件**: `api.py:113-124`

批量API失败后逐个请求每只股票，在候选多时极慢。

**建议**: 去掉单只回退，或限制重试次数（最多3次）。

---

### 11. `_market_cap_cache` 30秒TTL刷新

**文件**: `api.py:79`

市值一天内变化极小，30秒太短。

**建议**: 延长到 300秒，或仅按需刷新（交易时段每30分钟一次）。

---

### 12. 排名趋势只记录推荐股

**文件**: `orchestrator.py:146`

`update_rank_history` 只接收 `all_cats`（被推荐的股票），未入选的股票排名变化不追踪。`display.py` 的 `↑↓` 标记仅限于推荐股。

**建议**: 将全量 GEM 股票排名传入，使排名趋势覆盖所有监控股。
