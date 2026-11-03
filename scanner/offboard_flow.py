"""B 段：N 日主力累计净流入（历史资金流序列）。

## 依据（2026-11-03 调研，样本外检验通过）

`scripts/shadow_feature_backtest.py` 在 `offboard_launch_log` 的 948 条已标注样本
（25 个交易日）上做了回溯检验，结论：

| 条件 | 参与日 | Δ命中 | 95% CI | 符号检验 |
|---|---|---|---|---|
| 主力 20 日累计净流入 > 0 | 25 | +1.74pp | [+0.69, +2.99] | 正 17 / 负 3，p=0.0002 |
| 主力 20 日累计 > 3000 万 | 25 | +3.21pp | [+1.40, +5.16] | 正 16 / 负 5 |
| 超大单 20 日累计 > 0 | 25 | +2.31pp | [+0.81, +4.04] | 正 17 / 负 3 |

**多重比较**：8 个候选族做 BH-FDR，主力 20 日 > 0 的 q=0.0006（显著）。
**jackknife**（逐日剔除）：Δ 范围 [+5.74, +6.64]，符号稳定。
**分段**：前半 +1.82pp / 后半 +1.06pp，两段同向。
**时间切分**（训练窗定阈值 → 测试窗验证）：零阈值条件在两窗都显著。
**窗口敏感性**：窗口 10~30 全部为正 ⇒ 不是挑窗口挑出来的。

### 已排除的伪信号（**别加**）

- 🔴 **龙虎榜**：bootstrap CI 看着显著（+9.19pp），但**逐日方向是正 8 / 负 9**
  （符号检验 p=0.815）⇒ 被少数几天拉起来的假信号。已否决。
- 🔴 **公告**：业绩预告/重组并购方向为**负**（利好出尽），但参与日只有 2~5 天，
  样本不足以下结论。
- ⚠ **分母效应**：阈值越高 Δ 越大的同时过滤后样本骤减（阈值 5000 万只剩 21.7%），
  Δ 会被小样本放大 ⇒ 生产阈值取**零阈值**（> 0），可调阈值只作参考。

### 前视偏差检验（关键）

回溯数据里的「信号日 bar」是**收盘后**的完整数据，而实时扫描时它只有盘中部分数据。
用**剔除信号日 bar** 的版本重测：主力 19 日累计 > 0 ⇒ Δ=+2.10pp，正 16 / 负 4
⇒ **结论不依赖信号日 bar** ⇒ 实时可用。

### 为什么用本地库而不是东财 fflow

东财 fflow `klt=101` 的 121 日日频序列与本地 `market_extra_cache` 的每日快照
**相关系数 r=+0.946、符号一致率 92.2%**，检验效果几乎相同（Δ +1.85 vs +1.74）。
⇒ 生产实现走**本地库**（`market_extra_cache` 每天已在写），**零新增网络请求**。

## 口径

- **窗口**：`OFFBOARD_FLOW_WINDOW` 个交易日（默认 10），**只含已收盘的交易日**
  （`date < 今日`）—— 见下方「前视偏差」一节。
- **序列来源**：`market_extra_cache` 中 `data_type='fund_flow'` 的 `main_net`，
  按 `date` 升序取最近 N 个**已收盘**交易日求和。
- **数据不足**：历史不足 N 天 ⇒ 返回 `None`（**不可判定**），
  由调用方 fail-open（放行），**不**当成「净流出」。
- **单位**：元（`main_net` 原生单位），对外接口换算成万元。

## 前视偏差（为什么剔除信号日）

B 段的信号是**盘中**产生的（实测 `offboard_launch_log.updated` 时刻集中在 09:45~10:32），
而 `market_extra_cache` 的当天那行会被当天后续的快照**持续覆盖**（INSERT OR REPLACE）
⇒ 回溯时读当天那行拿到的是「接近收盘」的值，而信号产生时并不存在这个信息。

⇒ 本模块一律只取 `date < 今日` 的行（已收盘交易日）。验证：剔除信号日后两个**独立**
数据源（本地库快照 / 东财 fflow 日频）给出一致结果 ——
本地库 Δ=+1.43pp CI[+0.33,+2.67] 正17/负4；东财 Δ=+1.64pp CI[+0.52,+2.87] 正17/负4
（符号检验 p=0.0009）。
"""

from __future__ import annotations

import json
import logging
import sqlite3

logger = logging.getLogger(__name__)

#: 参与累计的快照类型（与 `market_extra.save_market_extra_cache` 的 data_type 同源）
FLOW_DATA_TYPE = "fund_flow"


def load_flow_history(
    conn: sqlite3.Connection,
    symbols: list[str],
    *,
    window: int,
    as_of: str | None = None,
) -> dict[str, list[tuple[str, float]]]:
    """批量取每只票最近 `window` 个**已收盘**交易日的 (date, main_net) 序列。

    返回 {symbol: [(date, main_net), ...]}（按 date 升序）。
    查不到的 symbol 不在结果里（调用方按「数据不足 → fail-open」处理）。

    ⚠ **只取 `date < as_of`**（已收盘交易日）：当天那行会被当天后续快照覆盖成
    「接近收盘」的值，而 B 段信号是盘中产生的 ⇒ 含当天行 = 前视偏差。详见模块 docstring。
    """
    if not symbols or window <= 0:
        return {}
    uniq = list(dict.fromkeys(symbols))
    # 占位符数量由 len(uniq) 决定（非外部输入），但 ruff 的 S608 不认这个推导 ⇒
    # 改用**参数化 IN**（每个元素一个占位符），让检查器也能确认没有字符串拼接。
    placeholders = ",".join("?" * len(uniq))
    rows = conn.execute(
        f"SELECT symbol, date, payload_json FROM market_extra_cache "  # noqa: S608 - 占位符由 len(uniq) 决定
        f"WHERE data_type = ? AND symbol IN ({placeholders}) AND date < ? "
        f"ORDER BY symbol ASC, date DESC",
        (FLOW_DATA_TYPE, *uniq, as_of or _today()),
    ).fetchall()

    out: dict[str, list[tuple[str, float]]] = {}
    for sym, date, payload in rows:
        try:
            parsed = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(parsed, dict):
            continue
        net = parsed.get("main_net")
        if not isinstance(net, (int, float)):
            continue
        out.setdefault(sym, []).append((date, float(net)))
    # SQL 是 date DESC，取完反转成升序；并截断到 window 行。
    for sym in list(out):
        out[sym] = list(reversed(out[sym]))[-window:]
    return out


def sum_main_net(
    history: list[tuple[str, float]] | None,
    *,
    window: int,
) -> float | None:
    """序列 → N 日主力净额合计（元）。**历史不足 window 天返回 None**（不可判定）。

    ⚠ 不足即 None 而不是「用现有的天数求和」：那样窗口会随新股上市时间变短，
    等价于给新票套了更宽松的阈值 —— 与「历史不足不判定」是两种语义，不可混同。
    """
    if not history or len(history) < window:
        return None
    return sum(v for _, v in history)


def _today() -> str:
    from scanner.config import now_beijing

    return now_beijing().date().isoformat()
