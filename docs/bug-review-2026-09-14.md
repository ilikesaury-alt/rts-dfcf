# 内部实现逻辑审查报告

日期：2026-09-14
范围：主通路（`orchestrator.scan_with_raw`）+ 决策核心链（`weights` / `analysis` / `enhancer` / `ranking` / `nextday_prob` / `final_pick`）
方法：静态阅读 + 对 `scanner.db`（4961 条 `excluded=0` 历史行，2026-05-28 → 2026-09-14）做反事实回算

## 结论摘要

| # | 严重度 | 问题 | 量化影响 |
|---|---|---|---|
| 1 | 严重 | `composite_tier`（实时主表档位）判别力不如被它取代的 `entry_tier`：**非单调** | 主表 2278 行；`entry_tier` 档0 的 415 行中 340 行（82%）被降档，而这批真实 hit 28.2% |
| 2 | 严重 | 实时主表丢失「涨幅带死区/陷阱」警示，丢掉的恰是最差的一批 | 命中警示 661 行，656 行（99.2%）被升档；这批 hit 6.2% vs 基线 11.4% |
| 5 | 严重 ✅已修 | `rule_validate` 的 MDE 在无效果时退化为 0，使「基线自检看 MDE」流程失效 | 报 0.0pp，真实 ≈ 2.3pp（相对基线 13.3% 即 ~17% 相对提升）；已改为打印 n/a + 翻转天数诊断 |
| 3 | 中 | `_fund_flow_norm` 把强流入映射为最高正分 +0.5，与项目已裁决口径冲突 | 382 行被加 +0.5；但当前样本不支持「反指」，实测效应量约 +0.1pp |
| 4 | 轻 | `composite_score` 死参数（`conn`/`accum_map` 未使用）+ 值域不符文档 + `rebound` 恒饱和 | 1513/4961 行 composite_score 为负；71 行 rebound 全部 = 10.0 |
| 6 | 观察 | 校准漂移守护当前是红的（退出码 1） | `OR_OUTFLOW` 漂移 42.6%、`pool_pick` base rate 44.9%，均未登记豁免 |
| — | 已记录 | 两套档位并存（文档已提，但**低估了规模**） | 实测已分叉 **4006/4961 = 80.7%** |

---

## 基线

- `pytest tests/` → **1473 passed, 13 skipped**（全绿，问题都在无测试覆盖的路径）
- 权重表一致性检查：4 张表 84 个 key 全部在 `analysis.py` 中被引用，无死键、无未定义键
- `git status` 干净，HEAD = `18a2b7b`

---

## 发现 1（严重）：`composite_tier` 的判别力不如它声称取代的 `entry_tier`

### 事实

`scanner/view/assemble.py:379` 用 `composite_tier` 决定实时主表排序，排序键是
`(tier, -composite_score, CAT_DISPLAY_PRIORITY)`（`assemble.py:382`）——**tier 是第一关键字**，
档位错一档，行序就错一段。

对主表类别（`new_face` / `known_new_face` / `momentum` / `short_term` / `rebound`，共 2278 行）
分别用两个函数算档位，再看各档真实次日表现：

| 档位 | `entry_tier`（旧，被取代） | `composite_tier`（现役实时主表） |
|---|---|---|
| 档0 | n=415 次日 **+1.866%** hit **26.3%** | n=138 次日 +1.463% hit 16.7% |
| 档1 | n=31 次日 +0.648% hit 12.9% | n=72 次日 **−0.458%** hit **6.9%** |
| 档2 | n=1189 次日 −0.560% hit 9.0% | n=1538 次日 **−0.045%** hit **13.2%** |
| 档3 | n=629 次日 −0.723% hit 6.0% | n=516 次日 −0.824% hit 5.2% |

**`entry_tier` 完美单调**（+1.87 → +0.65 → −0.56 → −0.72）。

**`composite_tier` 非单调：档1（hit 6.9%）比档2（hit 13.2%）更差。** 档位是升序主键，
这意味着实时主表会把期望收益更低的一批票排在更高位置。

### 交叉表：`entry_tier` → `composite_tier`

```
entry_tier\comp    档0    档1    档2    档3
档0                 59     16    340      0
档1                 32      0      0      0
档2                 28     35    615    522
档3                 20     22    585      4
```

`entry_tier` 判为档0 的 415 行（系统最强组，hit 26.3%）中，**340 行（82%）被
`composite_tier` 降到档2**——而这 340 行的真实表现是 **次日 +1.947% / hit 28.2%**，
是全场最好的一批。

### 根因

`composite_tier`（`ranking.py:784`）只保留了「过热」一个硬门：

```python
if accum is not None and accum >= OVERHEAT_ACCUM_MAX:
    return 3
cs = composite_score(...)
if cs >= COMPOSITE_TIER_THRESHOLDS[0]: return 0
...
```

`_warning_tier3_reasons` 的四项警示因子（超买 / 资金流出 / 小板块共振 / 涨幅带死区·陷阱）
**一项都没调用**；`is_nextday_marked` 的 🎯 画像也被显式忽略（函数签名的 `marked` 形参接收后不使用）。

函数 docstring 用一句话为这个删除做了辩护：

> 🎯 次日大涨画像（marked）降级为展示标记：composite 的 cat_base + rank_norm + fund_norm
> 已捕获相同底层信号（甜蜜带→cat_base 间接、非超买→tech_norm 间接）。

**这个辩护不成立**：`cat_base = COMPOSITE_CAT_BASE[category]` 是**按类别**取值的常量
（`config_scoring.py:271`），同一类别内 3% 进场和 5% 进场拿到完全相同的 `cat_base`，
**在类别内部没有任何涨幅带分辨力**。「甜蜜带→cat_base 间接」是错的。

---

## 发现 2（严重）：实时主表丢失「涨幅带死区/陷阱」警示，丢掉的恰是最差的一批

`_warning_tier3_reasons` 在真实数据上唯一实际命中的因子就是涨幅带（2-4% 死区 / 8-10% 陷阱）。

| 分组 | n | 次日均 | hit 率 |
|---|---|---|---|
| 主表全体（基线） | 2264 | −0.144% | 11.4% |
| 命中涨幅带警示的行 | 658 | **−0.685%** | **6.2%** |
| └ 其中被 `composite_tier` 升档（档≤2） | 656 | −0.691% | 6.2% |
| └ 其中仍判档3 | 2 | +1.395% | 0.0% |

命中警示的 661 行里 **656 行（99.2%）被升档**（`entry_tier` 会判档3）。这批行 hit 6.2%，
只有基线 11.4% 的一半——是明确的负向信号，却被排到了更高位置。

按 `composite_tier` 看这 656 行的落点：档0 51 行、档1 22 行、**档2 583 行**。
583 行的档2 分组真实表现是次日 −0.903% / hit 5.7%，**差于主表基线**。

而 `entry_tier` 的档3 分组（n=629）真实表现 −0.723% / hit 6.0%，确实是最差档——
**说明警示因子本身是有效的，被删掉的是有效的部分。**

---

## 发现 3（中）：`_fund_flow_norm` 把「强流入」映射为最高正分 +0.5，与项目已裁决的口径冲突

`ranking.py:719-738`：

```python
def _fund_flow_norm(entry):
    """...强流入正向加分已于 2026-08-10 下线（强流入组次日 -1.13% 反指），
    此处保留弱正向 +0.3 作为「有资金关注」信号（非强流入反指）。"""
    return {"strong_out": -0.5, "out": -0.2, "neutral": 0.1,
            "in": 0.3, "strong_in": 0.5}.get(sig, 0.1)
```

同一函数的 docstring 第二段说强流入加分已下线，代码却给了它 **+0.5（全部五档里的最大值）**。

`config_sources.py:51-53` 记录得更明确：

> 2026-08-10: 正向加分（原 `FUND_FLOW_BONUS_STRONG`）回测证实反指已删除——强流入(≥5%)组
> next_day 均 −1.13%（n=22）差于无数据基线 −0.85%……仅保留 `FUND_FLOW_BONUS_WEAK=-3`
> 流出扣分、「资金流出」标签。

`enhancer._apply_fund_flow_bonus`（`enhancer.py:441`）确实只保留了流出扣分，
**与 2026-08-10 的裁决一致**。所以现在项目里存在**两个互相矛盾的资金流评分器**。

### 但当前数据不支持「反指」这个结论

我用现有样本复算了（有 `fund_flow_main_pct` 的 1378 行）：

| 分组 | n | 次日均 | cum_3d 均 |
|---|---|---|---|
| `strong_in`（净占比 ≥ +5%） | 382 | **−0.774%** | −1.727% |
| `in` | 229 | −1.178% | −1.846% |
| 全样本（有资金流数据） | 1378 | −0.880% | — |

`strong_in` 反而**好于**全样本均值 0.1pp，不构成反指。2026-08-10 的结论基于 n=22，
现样本 n=382 已翻案。

**所以准确的结论不是「代码错了」，而是：**
1. 代码 / docstring / `config_sources.py` 三处口径互相矛盾，必须收口；
2. `+0.5` 是 composite 五档中的最大值，而实测效应量约 +0.1pp——**量级完全没有依据**；
3. `+0.5` 在档位阈值（6.0 / 4.0 / 2.0）下不是可忽略的抖动：对 `momentum`
   （`cat_base=2.6`）而言，+0.5 相当于到档1 距离的 36%。

---

## 发现 4（轻）：`composite_score` 的 `conn` / `accum_map` 是死参数，值域与文档不符

```python
def composite_score(entry, conn=None, accum_map=None) -> float:
```

实测函数体内 `conn` 与 `accum_map` 出现次数均为 **0**。两个形参只为了与
`composite_tier` 签名对称而存在，但调用方在传（`assemble.py:378`
`composite_score(e, conn, accum_map=accum_map)`），读者会误以为累计门槛参与了复合评分。

另外 docstring 称「统一复合评分 **[0, 10]**」，实测值域是 **[-4.95, 10.0]**，
4961 行中 **1513 行（30.5%）为负**（`min(10.0, raw)` 只封顶不封底，且 `cat_base`
本身可为负）。文档 `CORE-FLOW.md:207` 只写了「上限 10.0」，与 docstring 不一致。

### 附带：`rebound` 的复合评分恒等于上限

`COMPOSITE_CAT_BASE["rebound"] = 10.0`，而 `composite_score` 返回 `min(10.0, raw)`。
实测 71 行 rebound 的 composite_score **唯一值只有 1 个，全部等于 10.0**，
`composite_tier` 全部为 0。

后果：rebound 的另外四个分量（tech/rank/fund/dip）**被上限整体裁掉**，类内排序退化到
第三关键字 `CAT_DISPLAY_PRIORITY`——而它是**按类别**取值，对同一类别内所有行相同，
于是 rebound 行退化为 Python 稳定排序的插入序（DB 行序）。

影响面目前有限（rebound 日均 1~3 行），但这是设计缺陷：项目文档
（`ranking.py:414`）称 rebound 是「next_day 口径全场最强类别」，最强类别反而没有类内排序。

---

## 发现 5（严重，**已修复**）：MDE 在「无效果」时退化为 0，使文档指定的 MDE 自检流程失效

> **状态：2026-09-14 已修复并补测试**（`scanner/rule_validate.py` + `tests/test_rule_validate.py`）。
> 修复内容：新增 `mde_estimable` 标记 + `SE_ZERO_EPS` 容差，渲染层不可估时打印 `n/a` 并附原因；
> 新增「改动翻转了 X/N 个交易日」诊断（可检测性的真正来源）；同步修正 `AGENTS.md` 与模块 docstring
> 中"基线自检可看 MDE"的错误说法。全量测试 1478 passed，`ruff` / `mypy` 均通过。
> 下方保留原始问题描述作为修复依据。

`AGENTS.md` 与 `rule_validate.py:48` 都把「基线自检」指定为获知 MDE 的手段：

```
python -m scanner.rule_validate    # 基线自检：MDE 有多大？（不改任何东西）
```

实跑（无 override）输出：

```
  窗口         日数       基线      改动后        Δ             Δ 95%CI      单边p     MDE
  test       40    13.3%    13.3%    +0.0pp      [+0.0, +0.0]pp    1.000    0.0pp  ← 判定依据
```

**MDE 打印为 0.0pp。**

### 根因

`rule_validate.py:185`：

```python
se = math.sqrt(var)          # var 来自对「观测到的配对差值 deltas」做 bootstrap
return {..., "mde": 1.96 * se, ...}
```

MDE 是从**观测差值的标准误**估的。基线自检时没有改动 → 每天差值恒为 0 → `se = 0` → `MDE = 0.0pp`。
**MDE 只在改动已经产生效果之后才变成非零**——顺序完全颠倒。

### 实测：真实 MDE ≈ 2.3pp

| 调用 | test 窗 Δ | Δ 95%CI | MDE |
|---|---|---|---|
| 基线自检（无改动） | +0.0pp | [+0.0, +0.0] | **0.0pp** |
| `--set ...OR_MARKED=1.57`（微扰，未翻转 top-3） | +0.0pp | [+0.0, +0.0] | **0.0pp** |
| `--set ...OR_MARKED=5.0`（大扰动，翻转了 top-N） | +0.0pp | [−2.5, +2.5] | **2.3pp** |

注意中间那行：该扰动的 **rank-IC 确实变了**（+0.0130 → +0.0124），说明排序被改变，
但因为没翻转任何一天的 top-3 集合，MDE 仍报 0.0pp。

**真实可检测性下限 ≈ 2.3pp**（相对基线 13.3% 即约 17% 的相对提升）。

### 影响

文档写明「若 MDE 远大于你观察到的 Δ，那 Δ 无法与噪声区分，『指标变好』不构成上生产的理由」
——这是该工具最核心的一道判据。**MDE 恒报 0.0pp 时，这道判据永远不会触发**，
而 0.0pp 读起来像「任何微小改善都能被检出」，与事实（需 ~2.3pp）相反。
即：**工具最想防的那个失效模式（把噪声当信号），恰好被 MDE 的退化输出掩盖了。**

### 修复方向

MDE 应估**零假设下指标本身的可检测下限**，而非观测差值的标准误。可选：

- **安慰剂/置换法**：在每个交易日内随机置换「改动前/改动后」标签，重建 Δ 的零分布，取其 1.96×SE；
- **单样本法**：用基线指标在各 test 日的日级离散度，`MDE = 1.96 × sd(日基线指标) / sqrt(交易日数)`；
- 至少应在 `se == 0` 且 Δ ≡ 0 时**显式打印「MDE 不可估（改动无效果），请用置换法」**，
  而不是输出一个会被误读为 0 的数字。

---

## 观察 6：校准漂移守护当前是红的（退出码 1）

```
python -m scanner.nextday_calib    → 退出码 1
```

| 项 | 常数 | 实测 | 漂移 | 状态 |
|---|---|---|---|---|
| `OR_OUTFLOW` | 0.320 | 0.456 | **42.6%**（n=113/974） | DRIFT（未登记豁免） |
| `pool_pick` base rate | 0.0210 | 0.0304 | **44.9%** | DRIFT（未登记豁免） |
| `OR_MARKED` | 1.560 | 1.792 | 14.9% | ok（贴容差线，容差 15%） |
| `OR_BAND_SWEET_LOW` / `_DEAD` / `_MID` | — | — | 22.2% / 23.8% / 42.8% | 已知豁免 |

这不是新 bug——工具按设计正常工作（`AGENTS.md` 要求「漂移即退出码 1」，实测确实是 1）。
但它是**当前开放项**：两项超容差且未登记豁免，按项目纪律需在
`--write` 重写快照（须先过样本外验证）或补 `ACKNOWLEDGED_DRIFT` 之间二选一，
不允许长期红着。

顺带：`OR_BAND_*` 四项的「口径错位」（遗留拟合范围 = 全体样本，而模型只对
未标记且非 short_term 行生效）已于 2026-09-13 走过 §B1 样本外判定并裁决「不改」，
理由充分（换实测值后终选 top-3 hit 反而下降）。这一项处理得很干净，无需动作。

---

## 已记录但被低估的问题：两套档位并存

`docs/CORE-FLOW.md:219-220` 与 §已知问题 §4 已经写明：

> 展示排序已改用 `composite_tier`（2026-09-08），但 `_entry_tier` 仍被 `today_report`、
> `ranking_snapshot`、`walkforward`、`scripts/*` 使用……否则复盘口径与实时展示会分叉。

文档只说「会分叉」。**实测分叉已经发生，且规模很大：**

| 类别 | 总行数 | 两套判定不一致 | 分歧率 |
|---|---|---|---|
| comeback | 499 | 499 | 100.0% |
| short_term | 524 | 518 | 98.9% |
| pullback | 82 | 80 | 97.6% |
| core_dip | 414 | 400 | 96.6% |
| momentum | 504 | 471 | 93.5% |
| known_new_face | 141 | 114 | 80.9% |
| old_face | 1269 | 942 | 74.2% |
| new_face | 1038 | 686 | 66.1% |
| rebound | 71 | 32 | 45.1% |
| **合计** | **4961** | **4006** | **80.7%** |

关键后果：`ranking_snapshot`（收盘存证，`ranking_snapshot.py:50`）落库的是 **`entry_tier`**，
而当天终端实际按 **`composite_tier`** 排序。**存证记录与当时展示不一致**，
基于快照做的任何档位归因/回放，测的都不是用户当时看到的那套排序。

---

## 建议的验证与修复路径

项目已有现成工具，不要凭本报告的结论直接改：

1. **先跑样本外验证门**（改动必须过这道门）：
   ```
   python -m scanner.rule_validate --evaluator rescore --set scanner.ranking.COMPOSITE_CAT_BASE=...
   ```
   注意 `--set` 改的模块必须在评估器可见集合内，否则退出码 3。

2. **发现 1/2 的修复方向**（按代价从低到高）：
   - 低：给 `composite_tier` 补回 `_warning_tier3_reasons` 短路（等价于把旧级联的警示链
     搬到新函数里），保留 composite 作为档内排序键——即
     `tier = 3 if _warning_tier3_reasons(e) else composite_tier(...)`。
   - 中：把 🎯 画像恢复为档0 硬门（`marked → 0`），因为它实测 hit 26.3%，
     是全场最强单信号，当前被 82% 打散到档2。
   - 高：重新校准 `COMPOSITE_TIER_THRESHOLDS`，让档位在**主表类别内**单调。

3. **发现 4 的 `rebound` 饱和**：把 `cat_base` 与上限解耦（例如上限提到 11.0，
   或把 `cat_base` 降到 9.0 以下留出分量空间），否则该类内排序永久失效。

4. **发现 3**：先用 `python -m scanner.nextday_attribution` 确认 `strong_in` 的当期 IC，
   再决定是把 `+0.5` 下调到与效应量匹配的值，还是回退为 `0.0`。同时收口三处矛盾文案。

5. **收口双档位**：`ranking_snapshot` 应改记 `composite_tier`（或两者都记），
   否则存证与展示永久背离。

6. **发现 5（MDE 退化）建议最先修**，因为它是其余所有验证的前置条件：
   MDE 报 0.0pp 时，「Δ 能否与噪声区分」这道判据恒不触发。修好之后，
   上面 1~5 项的改动才有可信的判定依据。

7. **观察 6（校准红）**：按项目纪律二选一——`--write` 重写快照（须先过 §B1 样本外验证）
   或补 `ACKNOWLEDGED_DRIFT` 条目。注意 `OR_OUTFLOW` 是**降低**方向的常数
   （0.320 → 实测 0.456 意味着流出惩罚被高估），方向敏感，建议先跑验证再定。

---

## 已执行的既有自检（第二轮补做）

| 命令 | 结果 |
|---|---|
| `pytest tests/` | 1473 passed / 13 skipped |
| `python -m scanner.hot_watch --offline-demo` | 12/12 条全部符合预期，退出码 0 |
| `python -m scanner.rule_validate`（基线自检） | 退出码 1（证据不足=默认拒绝）；**MDE 报 0.0pp → 见发现 5** |
| `python -m scanner.nextday_calib` | 退出码 1（2 项超容差未豁免）→ 见观察 6 |
| 权重表 key ↔ 代码引用一致性（AST 差集） | 4 张表 84 键全部被引用，无死键/无未定义键 |

---

## 未覆盖范围（诚实声明）

- 本次未运行 `--run-smoke`（需真实 scanner.db 写权限 + 网络）与 `scripts/golden_scan.py`；
  发现 1/2/4/5 均为**纯函数 + 历史数据回算**结论，未触及 `scan_with_raw` 的等价性。
- 未审查 `comeback` / `core_themes` / `decision` / `trend_beauty` / `portfolio_backtest`
  的**内部实现**（仅读了 `hot_watch` 的离线自检结果与 `rule_validate` 的 MDE 计算路径）。
- 发现 1/2 的档位是**反事实回算**：`composite_tier` 自 2026-09-08 起才生效，
  历史行上算出的档位代表「若当时用新函数会怎样」。这不影响结论方向
  （`composite_tier` 是存储字段的纯函数），但样本区间跨越了函数上线日，严格评估
  应只看 2026-09-08 之后的行（该区间样本量小，故本报告用全区间）。
- **发现 2 未做按日配对 bootstrap 显著性检验**。分组差异（hit 6.2% vs 11.4%，n=658）
  量级明显，但正式改动前请用 `rule_validate` 的按日配对 bootstrap 复核
  （注意：需用 `--evaluator rescore` 才可见 `ranking` 模块的改动，且需先修发现 5 的 MDE）。
- 未核查数据源层（`api.py` / `data_source.py` / `ths_api.py`）的网络与解析逻辑。
