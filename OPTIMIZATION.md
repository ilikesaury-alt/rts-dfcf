# 选股策略优化分析

> 基于 `scanner/` 模块通读，按影响程度分层
> 标记 ✅ 的为已实施，⬜ 的为待讨论

---

## 一、逻辑缺陷（可能影响选股准确性）

### 1. ✅ `accumulated` 索引偏移

**文件**: `analysis.py:21`, `:109`, `:182`

三个分析函数统一用 `pcts[-6:-1]` 取"5日累计"，跳过了最近一个完整交易日。
改为 `pcts[-5:]`，取最近5个完整交易日。

---

### 2. ⬜ Volume surge 检测滞后一天

`vol_ratio = volumes[-1] / avg_vol` 用的是 **昨天** 的日K线成交量。
如果今天放量突破，日K线数据里看不到，要等到明天。

**方案待讨论**: 从分时API提取今日实时累计成交量，计算 `live_vol_ratio`。

---

### 3. ✅ V_shape_reversal 用 `today_pct` 替代 `pcts[-1]`

**文件**: `analysis.py:36-40`

原条件用 `pcts[-1] > 3`（昨天日K涨幅），改为 `today_pct > 3`（今日实时涨幅），V反确认应该看当天。

---

### 4. ✅ `get_symbol_appearances` 日期口径统一

**文件**: `database.py:89-96`

新增 `AND date < ?` 条件排除今天，删除 `orchestrator.py` 中的 `if a['date'] < today` 手动过滤。

---

### 5. ✅ 弱形态过滤增加累计跌幅保护

**文件**: `analysis.py:18`, `:106`

原条件 `down_days >= 3 and sum < 5` 误杀浅调企稳形态。
新增 `sum > -5`，只有真跌多的弱形态才过滤。

---

## 二、策略精细化空间

### 6. ⬜ Old Face 历史强度用聚合指标

现在 `any(percent >= 4)` 一刀切。

**方案待讨论**: 最近N天内最大涨幅 ≥ 4%，或至少2次上榜且平均涨幅 ≥ 2.5%。

---

### 7. ✅ 市值过滤收紧

**文件**: `config.py:13`

`MAX_MARKET_CAP = 500亿` → **200亿**，与 小而美 加分标准对齐。

---

### 8. ⬜ 板块分类用 API 行业数据

**方案待讨论**: 从雪球批量行情 API `industry` 字段获取实际行业数据，替代名称关键词匹配。

---

### 9. ✅ CSV 日志全量写入

**文件**: `logging.py:17`

去掉 `[:5]` 截断，改为 `(new_faces + momentum + old_faces)`，全部候选写入CSV。

---

## 三、性能与健壮性

### 10. ✅ 去掉个股市值单只回退

**文件**: `api.py`

去掉批量API失败后的逐个请求回退（原代码太慢），只保留批量请求 + 容错。

---

### 11. ✅ 市场市值缓存 TTL 延长

**文件**: `api.py`

30s → **300s**，市值一天内变化极小，没必要高频刷新。

---

### 12. ✅ 排名趋势覆盖全量 GEM

**文件**: `orchestrator.py:145`

从 `update_rank_history({c.stock.symbol: c.stock.rank for c in all_cats})` 改为 `{s.symbol: s.rank for s in gem_stocks}`，使排名变化箭头覆盖所有监控股。
