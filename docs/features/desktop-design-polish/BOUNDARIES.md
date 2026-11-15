# 桌面看板视觉重做 · 改造边界审计

> 性质：**纯研究**，不改任何生产代码、不安装依赖、不跑构建/测试。结论来自实际阅读当前工作树（`git status --short` 初始为空）的源码与测试；行号以本次读取时的文件为准。
> 路径缩写：`WEB = rts-panel-cloud/web/src`；`DESKTOP = desktop/`；`ROOT = 仓库根`。
> 同目录另有并行产出的 `DESIGN.md`（设计规格）与 `preview.html`（高保真预览），**本文件不隶属于它们、也不评价其结论**；本文件只回答「哪些能改、哪些会红」。

---

## 0. 总览（先给结论）

| 区域 | 自由度 | 一句话 |
|---|---|---|
| `WEB/index.css`（`@theme` token + 纯 CSS 类） | **高** | 无 pytest 读取它；改配色须同步 `pages/Stock.tsx` 的 Canvas 字面值（纪律，不是测试） |
| `WEB/App.tsx` 布局骨架 / 新增展示组件 | **高但有文案雷区** | 必须保留 3 个区块标题全串、2 个 `export const …Subtitle` 模板的形状与逐字内容；`web/src` 全域禁 9 个结论词 |
| `WEB/components/**` 表现层 | **中** | 禁 11 个列名字面量（整行注释不豁免）、禁结论词、禁裸 `fetch`、禁红线标签 |
| `WEB/lib/cols.ts` | **低** | 3 个 `// #region COLS:*` 块的键序、`HOT_HIGHLIGHT_STREAK = 3`、`POOL_CELLS` 内容被锁 |
| `WEB/api/client.ts` / `api/types.ts` | **低** | 唯一网络收口 / 三端镜像之一；`types.ts` 未被 pytest 直接读取，但被 `tsc`、worker 契约注释约束 |
| `DESKTOP/main.js` | **中** | 14 条结构守卫锁的是**扫描器拉起、env、路径、生命周期**的字面量；`BrowserWindow` 选项（`frame`/`icon`/`minWidth`/…）**无测试直接锁** |
| `DESKTOP/package.json` `build` | **中低** | `extraResources` 的 `to`/`filter` 4 条、`scripts.dist` 前缀被锁；新增图标资源需评估是否进 `files` |
| Python 侧（`scanner/local_panel_server.py` 等） | **无需动** | 纯表现层改动可做到**零 Python 改动**；前提是不改 URL/端口/SPA 路由形态/契约 |

三个最容易踩的「必然变红」：① 在 `components/**` 代码里写出任何列名（含 `评分`）；② 改/删 `App.tsx` 的 3 个 `TITLES.*.full` 或 2 个段标题模板；③ 在 `web/src` 任何 `.ts/.tsx` 写出 `建议/短名单/综合判断/买入/卖出/持仓/目标价/止盈/止损`。详见 §1。

---

## 1. 守卫清单：逐条断言到底锁了什么

### 1.1 `tests/test_panel_web_hygiene.py`（6 个测试，137 行）

公共设置（来源同文件）：

- `WEB_SRC = ROOT/rts-panel-cloud/web/src`（L16）；`pytestmark` 在该目录不存在时整体 skip（L18-21）。
- `_ts_files()`（L34-41）：`WEB_SRC.rglob("*")` 中 `suffix in (".ts", ".tsx")` 且文件名**不**以 `.test.ts` / `.test.tsx` 结尾（L30-31 注释说明：必须同时扫 `.tsx`，否则组件层守卫失明）。注意：它**递归**扫整个 `web/src`，不局限于 `components/`；也不扫 `.css`/`.html`（L29、L38）。
- `_code_only(text)`（L43-52）：先 `re.sub(r"/\*.*?\*/", "", text, flags=re.S)` 去**块注释/JSDoc**，再 `re.sub(r"^\s*//.*$", "", …, flags=re.M)` 去**整行**行注释（只认行首 `//`；行尾 `// …` 不算注释；`https://` 不会被误删）。即：**注释里写禁令不算违规，但行尾注释会算进代码**。
- `_FETCH_RE = re.compile(r"(?<![\w.])fetch\s*\(")`（L24）：要求 `fetch` 前**不是**单词字符或 `.`；因此 `obj.fetch(`、`myfetch(` 不匹配；`fetch (`（有空格）匹配；`window.fetch(` 被排除（前面是 `.`）。
- `_SUFFIXES = (".ts", ".tsx")`（L29）。

| 测试（函数 / 行） | 锁的具体东西 | 必然变红的改动 |
|---|---|---|
| `test_web_src_exists` L54 | `WEB_SRC.exists()`；`_ts_files()` 非空（防空转） | 删除/改名整个 `web/src`；清空所有 `.ts/.tsx` |
| `test_guard_actually_scans_component_files` L60 | 扫描集合里必须有文件名 `App.tsx` 与 `client.ts`（按 `p.name` 比对，L65-66） | 把 `App.tsx` 或 `client.ts` **改名**（`App.tsx` → `Dashboard.tsx` 会红）；删除它们；把它们移出 `WEB_SRC` |
| `test_no_bare_fetch_outside_client` L72 | 遍历 `_ts_files()`，**跳过** `p.name == "client.ts"`（L76-77），对每个文件去注释后逐行搜 `_FETCH_RE`（L78-81），要求 0 命中 | 在 `web/src` 下**任意** `.ts/.tsx`（`App.tsx`、新增组件、`pages/`、`lib/`、除 `*.test.ts(x)` 外的工具文件）写裸 `fetch(`；包括新加的展示组件里直接拉数据。**判定方式摘要**：按文件名而非路径豁免——任何叫 `client.ts` 的文件都被跳过，任何不叫 `client.ts` 的文件都不豁免；`fetch` 必须是「独立标识符 + 可选空白 + `(`」；`window.fetch(...)` / `axios.get(...)` 不被这条正则抓到（但见下一条） |
| `test_client_is_the_only_network_module` L85 | 遍历 `_ts_files()`，跳过 `client.ts` **和** `types.ts`（L88）；其余文件去注释后不得含字符串 `XMLHttpRequest`（L90） | 任何非 `client.ts`/`types.ts` 的 `.ts/.tsx` 代码里出现 `XMLHttpRequest`（含作为字符串常量/类型名）；注释里出现不红 |
| `test_table_components_have_no_hardcoded_column_labels` L94 | `comps = (WEB_SRC/"components").rglob("*")` 下**所有** `.ts/.tsx` 文件（L102-103，含 `ui/` 子目录；`components/` 不存在时 `comps=[]` → 空转通过）；去注释后搜 11 个词（L104）：`涨幅`、`5日累计`、`现价`、`板块`、`评分`、`策略`、`量比`、`换手%`、`市值(亿)`、`连击`、`排名上升`。**没有按文件白名单、没有按词白名单、没有例外路径**（`components/ui/tabs.tsx` 同样被扫）；`_code_only` 只去整行 `//` 与块注释，**行尾注释不豁免** | 在 `components/**/*.ts(x)` 的**可执行代码或行尾注释**里出现上述任一词：例如给空态写「暂无评分」、给按钮 `title="按涨幅排序"`。当前 `components/RegionTable.tsx:11`、`Sidebar.tsx:8/10`、`GatePanel.tsx:17` 的命中都在块/整行注释中，故不违规。**注意**：`App.tsx` 不在 `components/` 下，不受本条约束（它可以写列名，且 L52/L55 确实写了 `评分/涨幅/量比/排名上升`）；9 个结论词另由 §1.2 守卫管 |
| `test_ai_label_not_hardcoded_in_web` L120 | 常量 `_AI_LABEL = "仅解读，非投资建议"`（L115）；`WORKER_SRC = …/worker/src`（L117）；要求 `worker/src/routes/explain.ts` 存在且含该串（L125-126）；再扫 `_ts_files()`，任何文件去注释后含该串即违规（L132-136） | 前端任何 `.ts/.tsx` 代码里写出「仅解读，非投资建议」；删/改服务端 `explain.ts` 里的 `AI_LABEL`；删 `web/src` 全部 `.ts/.tsx`（防空转 L130） |

> **明确回答**：「components/** 不得出现列名字面量」检查的是 `WEB_SRC/components` **递归**下全部 `.ts/.tsx`（`rglob("*")` + suffix 过滤），词表是上列 11 个；**没有任何白名单**（只有「去注释」这一文本预处理）；`App.tsx`、`lib/cols.ts`、`pages/Stock.tsx` 不在其扫描面内。`lib/cols.ts` 里大量列名是**被允许且被要求**的（见 §1.2）。

> **明确回答**：「除 client.ts 外 0 处裸 fetch」是按 `_ts_files()` 全量递归扫 `WEB_SRC` 的 `.ts/.tsx`，**用 `_code_only` 去注释后**对每行跑 `(?<![\w.])fetch\s*\(`；豁免条件是 `p.name == "client.ts"`（按文件名、不限目录），测试文件 `*.test.ts(x)` 不在扫描集合里；`XMLHttpRequest` 另有独立守卫且额外豁免 `types.ts`。没有「允许某组件用 fetch」的白名单。

### 1.2 `tests/test_panel_web_cols.py`（6 个测试函数，138 行；其中 1 个参数化为 3 例）

公共设置：`WEB_SRC` 同上（L27）；`_read(name)` 用 `assert (WEB_SRC/name).is_file()`（L37-41）；`_code_only` 同 hygiene（L44-46）。导入 Python 真源：`from scanner.view.model import COLS_HIST, COLS_HOT, COLS_POOL, offboard_subtitle` 与 `hot_subtitle as py_hot_subtitle`（L23-24）。

| 测试（函数 / 行） | 锁的具体字符串 / 正则 / 结构 | 必然变红的改动 |
|---|---|---|
| `test_col_projection_covers_every_terminal_column` L64（`@pytest.mark.parametrize(("region","cols"))` L57-63：pool/hot/hist） | `_table_keys()`（L48-53）先 `re.search(rf"// #region COLS:{region}\n(.*?)// #endregion", ts, re.S)`，再 `re.findall(r'^\s{2}"([^"]+)":', m.group(1), re.M)` 取**恰好 2 空格缩进、双引号键**；比对 `got == [c[0] for c in COLS_*]`（L68-71）。即锁：`WEB/src/lib/cols.ts` 里 3 个 `// #region COLS:pool|hot|hist` → `// #endregion` 区块，以及每块内**两空格缩进**的 `"列名":` 键的**顺序与全集**。Python 真源见 `scanner/view/model.py:525-541`（POOL，9 列）、`:563-588`（HOT，16 列）、`:681-695`（HIST，10 列） | 改/删 region 标记（`// #region COLS:pool` 的换行位置很关键：正则要求标记后紧跟 `\n`）；把键改成单引号、改缩进、重排、增删任何列；在 Python 侧改 `COLS_*` 而不同步 `cols.ts`；两空格缩进变成 4 空格 |
| `test_hot_highlight_streak_matches_python_source` L73 | `re.search(r"export const HOT_HIGHLIGHT_STREAK = (\d+);", ts)` 取整（L77-79），要求 `== scanner.config_hot_watch.HOT_HIGHLIGHT_STREAK`（当前 `=3`，`scanner/config_hot_watch.py:46`；前端 `WEB/lib/cols.ts:67`） | 把 `export const HOT_HIGHLIGHT_STREAK = 3;` 改名/改成 `as const`/改成表达式/改数值；单改 Python 侧阈值而不改前端 |
| `test_segment_subtitles_are_verbatim_from_python_source` L90 | `_ts_template(ts, name)`（L81-85）用 `re.search(rf"export const {name} = \(n: number\): string =>\n?\s*`([^`]*)`;", ts)` 取反引号模板正文；断言 `template.replace("${n}", "7") == py_hot_subtitle(7)` 与 `== offboard_subtitle(7)`（L91-92）。即 `WEB/App.tsx` 必须导出**这两个名字、这个签名形状** `hotSubtitle` / `offboardSubtitle`，且模板正文逐字等于 Python 函数输出。Python 真源 `scanner/view/model.py:597-619` / `:622-654`。逐字串（当前）：<br>`（榜内创业板·当日动能+热度跃升（已涨）·排序=评分↓(排名上升35/涨幅25/价格15/量能25)·同分比排名上升·量比=上报原值·${n} 只）`<br>`（榜外创业板·非榜单来源·层级 T1=量先动·价未动 / T2=启动首日·排序=T2 启动首日→T1 量先动→量比→主力净占比·开盘 5 分内不产出·量比按市场前置系数去偏·${n} 只·观察段·未回测）`（`WEB/App.tsx:51-55`） | 改任何字（含把「5 分」改成常量插值 `${OFFBOARD…}`——那样 `.replace("${n}","7")` 后仍留 `${…}`，不等于 Python 串）；改 `export const` → `function`、改参数名/类型/返回类型标注、改成单行箭头但丢 `` `…`; `` 形状；把模板拆成多个拼接字符串；单改 Python 的 `hot_subtitle`/`offboard_subtitle` |
| `test_offboard_subtitle_keeps_both_tier_glossary_and_sort_text` L97 | 对 `offboardSubtitle` 模板正文额外断言 4 个子串（L101-103）：`层级 T1=量先动·价未动 / T2=启动首日`、`排序=T2 启动首日→T1 量先动`、`未回测`、`观察段` | 删词表、删排序键层序、删证据强度词（即使上一条因 Python 同步修改仍绿，本条独立拦住） |
| `test_region_titles_match_terminal_wording` L110 | 对 `WEB/App.tsx` 与 `scanner/view/render.py` **双向**断言 3 个完整串（L113-116）：<br>`◆ v1 池选 — 新票优先·类别优先·排名升序·资金流降序`<br>`◆ v1 回捞`<br>`◆ 沪深飙升 · 极有可能大涨`<br>（前端在 `App.tsx:44-48` 的 `TITLES.*.full`；终端 `render.py:404/512/732` 的 f-string 中含这些串） | 从 `App.tsx` 删/改任一完整串（把串放在别的常量里也行，只要源码里还出现；但 `RegionCard` 依赖 `TITLES.*.full` 传入）；或改 `render.py` 里的对应串；把长标题改成运行时拼接/去掉 `◆ ` 前缀会红 |
| `test_no_verdict_wording_in_web_src` L126 | `_VERDICT_WORDS = ("建议","短名单","综合判断","买入","卖出","持仓","目标价","止盈","止损")`（L123）；遍历 `WEB_SRC.rglob("*")` 全部 `.ts/.tsx`（跳过 `*.test.ts(x)`，L128-132），去注释后逐词搜（L133-136） | **`web/src` 全域**（不只 `components/`）任何 `.ts/.tsx` 代码出现这 9 个词之一：例如新空态写「暂无建议」、按钮写「买入」、注释写成行尾注释（不豁免）、给 `title="目标价..."`；`App.tsx:10-11` 与 `GatePanel.tsx:17`、`RegionTable.tsx:11` 里这些词都在**块注释/整行注释**内，故当前不违规 |

> **明确回答「段标题/连击阈值/常量逐字相等」**：
> - 段标题：锁 `App.tsx` 的 `export const hotSubtitle/offboardSubtitle` 的**模板正文**逐字等于 Python `scanner/view/model.py::hot_subtitle(7)/offboard_subtitle(7)`，另加 4 个独立子串（词表 / 层序 / `未回测` / `观察段`）。任何重措辞、把 `${n}` 换成别的占位符、改签名形状都红。
> - 连击阈值：锁 `lib/cols.ts` 的 `export const HOT_HIGHLIGHT_STREAK = <整数>;` 与 `scanner.config_hot_watch.HOT_HIGHLIGHT_STREAK`（=3）同值。
> - **已知手抄副本 `OFFBOARD_OPENING_SILENCE_MIN`**：Python 侧真源是 `scanner/config_hot_watch.py:109` 的 `OFFBOARD_OPENING_SILENCE_MIN = 5`（经 `scanner/config.py:29` `from scanner.config_hot_watch import *` 再导出，`view/model.py:9` 导入，`view/model.py:652` 插入 `f"·开盘 {…} 分内不产出…"`）。前端 `App.tsx:55` **手写死**了 `开盘 5 分内不产出`，没有 import、没有常量名。因此：若 Python 把 5 改成别的值，`test_segment_subtitles_are_verbatim_from_python_source` **会红**（Python 输出变了而前端模板没变）；「前端自己把 5 改成 15」也会红（与 Python 输出不等）。真正缺口是：`test_panel_web_cols.py` **并不校验该数字来自哪个常量**，也不会在「Python 改值 + 前端跟着手抄改值」时预警语义漂移。`docs/features/desktop-ui-modernize/HANDOFF.md:57` 也记录了这个缺口（该文档写「=15」，与当前 `config_hot_watch.py:109` 的 **5** 不符——**文档已过时，以代码为准**；`_UNADJUSTED = 15` 仍存在，`config_hot_watch.py:115`，`tests/test_offboard_watch.py:1903` 断言它 ==15）。消除手抄副本需要把值随 `colSpec`/`ctx` 下发 ⇒ 动 `SCHEMA_VERSION`，属独立契约任务（见 §4）。
> - 另有一个同类守卫在别处：`tests/test_prior_column.py:175-185` 的 `test_panel_web_projection_matches_python_columns` 用 `Path("rts-panel-cloud/web/src/lib/cols.ts").read_text()`（**相对当前工作目录**，非 `parents[1]`，故必须从仓库根跑），`split("const POOL_CELLS")[1].split("};")[0]` 后要求每个 `COLS_POOL` 列名以 `"<列名>":` 出现，且断言 `'"评分":' not in pool_block`（L185）。⇒ **`lib/cols.ts` 的 `POOL_CELLS` 块内不得重新加回「评分」列**，且 `const POOL_CELLS` 这个标识符、`};` 结束形状被锁。

> **明确回答：`SCHEMA_VERSION` 与三端镜像文件如何被检查**
> - `scanner/panel_serialize.py:32` `SCHEMA_VERSION = 2`，`:182` 写进 `"schema"`；`rts-panel-cloud/worker/src/serialize.ts:9` `export const SCHEMA_VERSION = 2;`；`rts-panel-cloud/web/src/api/types.ts:77` 的 `Payload.schema: number`（**不写 2**，`:4-6` 注释自称「手抄镜像」）。
> - `tests/test_panel_contract.py:43-45` `_ts_const(_ts("serialize.ts"), "SCHEMA_VERSION") == SCHEMA_VERSION == 2`：`_ts_const` 用 `re.search(rf"export const {name}\s*=\s*(\d+)\s*;", src)`（L36-40）读**云端 worker** 源码的文本；Python 侧用 import 取运行时常量。**`web/src/api/types.ts` 不在该断言里**。
> - `tests/test_panel_contract.py:48-53` 另查 `worker/wrangler.jsonc`（去掉以 `//` 开头的整行注释后）不得出现 `SCHEMA_VERSION`。
> - `tests/test_panel_serialize.py:274-276` 独立断言 `SCHEMA_VERSION == 2`；`rts-panel-cloud/worker/test/api.test.ts:254-256` `expect(SCHEMA_VERSION).toBe(2)`（运行时）。
> - 三端**字段集**检查：`test_panel_contract.py:62-79`（`gate.stats` 7 字段名逐个在 `serialize.ts` 里 `re.search(rf"\b{f}\b", ts)`）、`:82-90`（`serialize_view` 的 `regions` 每个键出现在 `serialize.ts`）、`:93-100`（payload 顶层每个键出现在 `serialize.ts`）。注意这些是**「本地字段 ⊆ 云端源码文本」的单向包含**，不是与 `web/src/api/types.ts` 比对；`types.ts` 的 `GateStats`/`Regions`/`Payload` 靠 `pnpm typecheck` 与人工注释维护，**没有 pytest 逐字段读 `types.ts`**。`tests/test_local_panel.py:264-270` 另用真实 HTTP 校验 `regions.cols.*` 的 `key` 序列 == `COLS_*` 列名序列（数据面，不读前端源码）。
> - 其它契约断言（与视觉重做无关但仍会红）：`test_panel_contract.py:56-59`（`STOCK_QUOTA` 双端 20）、`:103-120`（`worker/src/routes/regions.ts`/`meta.ts` 的每条 `ORDER BY` 必须含 `date DESC`、`time DESC` 且不含 `seq`）、`:123-126`（`BODY_LIMIT=400_000`、`STOCK_KLINE_DAYS=120`）、`:129-144`（`marks` 键为 `"sym|cat"` 而非 Python tuple 字面量）。`pytestmark`（L26-29）在 `worker/src/serialize.ts` 不存在时整体 skip。

> **明确回答：是否有测试锁死 `index.css` 的 CSS 变量名或 `Stock.tsx` 的 `ASHARE_THEME` 字面值**
> **没有。** 全仓 `tests/*.py` 与 `rts-panel-cloud/*/test/*.ts` 均无读取 `WEB/index.css`、`styles.css`、`--color-up`/`--up`/`--flat`、`ASHARE_THEME` 的断言（见 §1.1 的 `_ts_files()`：只扫 `.ts/.tsx`；`test_panel_web_cols.py:128-132` 同样只扫 `.ts/.tsx`）。`grep` 结果显示 `--color-up/--color-down/--color-flat` 仅存在于 `WEB/index.css:330-332`（`@theme`），`ASHARE_THEME` 的 6 个颜色字面量仅在 `WEB/pages/Stock.tsx:29-37`（`#ef232a` / `#14b143`）。**但存在非测试的设计纪律**：`WEB/index.css:10-16` 头注释明确写「涨跌色变量名原样保留」「改这里要同步 `pages/Stock.tsx` 的 `ASHARE_THEME`」；`WEB/lib/cols.ts:19-20` 也把 `#ef232a/#14b143` 写进注释；`WEB/pages/Stock.tsx:25-28` 说明 Canvas 认不得 CSS 变量所以要单独持有同一组字面值。⇒ 改 `@theme` 的 `--color-up/down/flat`（或 `--color-bg/card/border/text/text-dim/lag-warn/err/ring/mark-new`、`--spacing-*`）**不会让守卫变红**，但必须**同批**改 `Stock.tsx:31-36` 才能保持 A 股红涨绿跌口径一致；Tailwind v4 的 `@theme` 名与工具类一一对应（`index.css:14-15`，`vite.config.ts:19` 的 `@tailwindcss/vite`），改名 `--color-card` 会让 `bg-card`/`border-border`/`text-text-dim` 等类失效（编译不报错，视觉静默退化）。

### 1.3 `tests/test_panel_contract.py`（9 个测试，144 行）

见 §1.2「SCHEMA_VERSION」段。补充：`CLOUD_ROOT = ROOT/rts-panel-cloud/worker/src`（L24）；`pytestmark` 在 `serialize.ts` 缺失时 skip（L26-29）；`_ts(name)`（L31-32）只读 worker 源码。**该文件完全不读 `web/src`**，所以纯 `web/src` 表现层改动不会触发它；但若视觉重做顺手把 `worker/src/serialize.ts`、`routes/ingest.ts`、`routes/regions.ts`、`routes/meta.ts`、`wrangler.jsonc` 或 `scanner/panel_serialize.py` 改了，上表任一条都可能红。

### 1.4 `tests/test_local_panel.py`（28 个测试函数 / 参数化前，550 行）

真起 `ThreadingHTTPServer`（fixture `server` L33-39 用 `lps.start_server(0, static_dir=None)`），用 `urllib` 打真 HTTP。与视觉重做相关的是**后端行为契约**，不是前端源码：

| 组 | 测试（行） | 锁的东西 | 视觉改动会红吗 |
|---|---|---|---|
| 安全/启动 | `test_bind_host_is_loopback` L107：`lps.BIND_HOST == "127.0.0.1"`、`server.server_address[0]`、`/api/health` 200 + `{"ok":True,"data":{"alive":True}}`；`test_start_server_is_idempotent` L115；`test_handle_error_*` L122/L140（logger `propagate is False`、连接级异常不进 stdout/stderr、其它异常 WARNING + `exc_info`） | `scanner/local_panel_server.py` 的常量与 `handle_error` | 不会（不改 Python 时） |
| 静态托管 | `test_static_route_404s_when_dist_missing` L164（无 `web/dist` 时 `/` 404 + `NOT_FOUND`，`/api/health` 仍 200）；`test_static_route_blocks_path_traversal` L181（`/../secret.txt`、`/../../secret.txt`、`/C:/secret.txt` 必须 404 且不含 `TOPSECRET`；`/..%5Csecret.txt`、`/%2e%2e/secret.txt` 不得泄露；`/index.html` 200） | `_static()` 的 `normpath + startswith(root_abs + os.sep)` 守卫（`scanner/local_panel_server.py:251-274`） | 不会；但**若为了新路由去改 `_static` 的 SPA 回退**就可能红 |
| API 契约 | `test_unknown_api_route_is_not_found` L210；`test_meta_not_ready_before_first_round` L218（200 + 精确 `{"ready":False,"date":None,"time":None,"seq":None}`）；`test_regions_before_first_round_is_no_snapshot` L225（404 `NO_SNAPSHOT`）；`test_regions_replays_same_payload_as_serialize_view` L235（`date/time/seq/main/ctx` 逐项等于 `serialize_view` 输出，且 `main` 3 行、首行 `SZ300319`）；`test_regions_response_has_no_stocks_key` L254（顶层与 `regions` 都不得有 `stocks`）；`test_regions_cols_keys_match_col_specs` L264（`cols.pool/hot/hist` 的 `key` 序列 == `COLS_*`）；`test_meta_reports_cached_round` L273 | `local_panel_server._regions/_meta/_route` 与 `serialize_view` | 不会，除非改 Python 或改 `COLS_*` |
| 详情页 | `test_stock_kline_shape_and_breakdown` L335（`kline` 元组 `[date,open,close,low,high,volume]`，首行 `[10.0,11.0,9.5,12.0,1000.0]`；`scoreBreakdown`；必须真连过库）；`test_stock_without_kline_is_not_in_report` L353；`test_stock_without_breakdown_returns_null` L362；`test_bad_symbol_is_rejected` L378（非法 symbol 400 且 `opened == []`） | `build_stock_payload` / `_SYMBOL_RE` | 不会；但 `WEB/pages/Stock.tsx:42-53` 的 `toKLineData` 依赖这个元组顺序（`close` 在 `low/high` 前），改前端映射会画错蜡烛但**测试不红** |
| 缓存/迁移/开关 | `test_cache_latest_roundtrip_and_replace` L390；`test_persist_and_load_latest_roundtrip` L409（按 `(date,time)` 取最新，不是 `seq`）；`test_persist_prunes_to_recent_rounds` L424；`test_persist_on_missing_table_is_fail_open` L445；`test_m021_view_snapshot_is_registered_and_applies` L454（迁移 id 在表里、`view_snapshot` 列集）；`test_local_panel_disabled_by_default` L482、`test_local_panel_falsey_values_stay_off` L490、`test_local_panel_truthy_values_enable` L499、`test_local_panel_port_override_and_fallback` L507、`test_local_switch_is_independent_of_cloud_switch` L519、`test_local_panel_module_not_imported_when_disabled` L532（关闭时 `scanner.local_panel_server`/`local_view_cache` 不在 `sys.modules`，`unified_scanner` 无 `start_server` 属性） | Python 模块级行为 | 不会 |

⇒ **对纯 `web/src` 表现层改动，`test_local_panel.py` 是「旁观者」**：它不读任何前端文件。它的价值是提醒：`/api/*` 路径、`{ok:true,data}` 信封、`regions.cols` 列名、`meta.ready=false` / `NO_SNAPSHOT` / `NETWORK` 这些**状态语义**是前端 `StatusBar`/`useSnapshot`/`Stock.tsx` 的输入（`WEB/api/client.ts:117-127`、`WEB/lib/useSnapshot.ts:44-62`、`WEB/components/StatusBar.tsx:44-53`），重做空态/多态必须继续消费这些码，不能另造一套。

### 1.5 `tests/test_desktop_shell.py`（14 个测试，261 行）

公共：`DESKTOP = ROOT/desktop`（L28）；`pytestmark` 在 `main.js` 缺失时 skip（L30-33）；`_main_js()` 全文读 `desktop/main.js`（L36-37）；`_code_only` 去块注释 + 行首 `//`（L40-43）；`_package_json()` 解析 `desktop/package.json`（L46-47）。**全部是源码文本结构断言**（文件头 L1-18 已说明「JS 侧没有 pytest 能跑的东西」）。

| 测试（行） | 锁的字面量 / 正则 | 必然变红的改动 |
|---|---|---|
| `test_desktop_binds_loopback_only` L53 | `'const HOST = "127.0.0.1";'` 必须在；`_code_only(code)` 中不得含 `0.0.0.0` | 改 `main.js:43` 的 `HOST` 常量写法（空格/单引号/模板串都红）；代码里出现 `0.0.0.0` |
| `test_scanner_is_started_with_local_panel_switch_only` L64 | `'env.RTS_LOCAL_PANEL = "1";'`（L66，对应 `main.js:114`）；`'\"unified_scanner.py\"'` 全文出现（L67；当前 `main.js:137` 的 `path.join(cwd, "unified_scanner.py")` 与 `main.js:5` 头注释**各有一处**，且本条用的是 `_main_js()` 全文而非 `_code_only`，故注释也能满足）；`re.search(r"execFile\(\s*python,\s*\[(.*?)\]", code, re.S)`（L70）必须命中，且捕获的 argv 内不得有 `--no-panel`（L72） | 把两处 `"unified_scanner.py"` 字面量都删/改写（只删代码那处仍因注释而绿）；把 `execFile(python, [...])` 改成 `spawn`/把第一个参数改名/把 `[` 换成变量数组；给 argv 加 `--no-panel`（`extraArgs()` 是运行时拼接，不在静态 argv 里，故不受影响） |
| `test_python_env_is_scrubbed_of_system_interpreter_vars` L75 | 5 个 `"PYTHON…"` 等字符串：`"PYTHONHOME"/"PYTHONPATH"/"PYTHONUSERBASE"/"VIRTUAL_ENV"/"CONDA_PREFIX"`（L77-78，`main.js:49-57`）；`"delete env[key]"`（L79，`main.js:113`）；`'env.PYTHONNOUSERSITE = "1";'`（L82，`main.js:122`）；`desktop/fetch-python.mjs` 里也要有同一行（L83-84） | 精简 `SCRUBBED_ENV` 数组、把 delete 写成 `for…of` 之外形状、改 `PYTHONNOUSERSITE` 赋值写法；改 `fetch-python.mjs` 的这一行 |
| `test_packaged_python_comes_from_resources_path` L87 | `"app.isPackaged"`、`"process.resourcesPath"` 全文出现；`re.search(r'path\.join\(process\.resourcesPath, \"python\"', code)`（L91，`main.js:73`）；`"RTS_PYTHON"`（L92） | 把 `findPython()` 的打包分支改成 `path.resolve`/模板串；删 `RTS_PYTHON` 覆盖口 |
| `test_writable_state_goes_to_user_data_without_overwriting` L98 | `'app.getPath("userData")'`（L100）；`"RTS_DB_PATH"`、`"RTS_LOG_DIR"`（L101）；`re.search(r"if \(!fs\.existsSync\(dbPath\)\) \{(.*?)\n  \}", code, re.S)`（L103）且块内含 `copyFileSync`（L105）；`"mkdirSync(logDir"`（L106）；两串 `env.RTS_DB_PATH || state.dbPath` / `env.RTS_LOG_DIR || state.logDir`（L108-109） | 改 `preparePackagedState()` 的 `if` 形状/缩进、改 `fs.mkdirSync(logDir` 调用、改 `||` 表达式写法（如改成 `??`） |
| `test_node_side_never_reads_sqlite_directly` L112 | `main.js` 不含 `"better-sqlite3"`、不含 `'require("node:sqlite")'`；且含 `"copyFileSync"`（L115-116） | 引入任何 Node 侧 sqlite 依赖；删掉种子拷贝 |
| `test_packaged_env_file_points_at_user_data` L122 | `"RTS_ENV_FILE"`；`"env.RTS_ENV_FILE || state.envFile"`；`'path.join(userData, ".env")'`（L129-131，`main.js:100/129`） | 改 `.env` 落点或覆盖表达式 |
| `test_env_file_override_is_honored_by_entry` L134 | **真跑子进程**：`subprocess.run([sys.executable,"-c","import unified_scanner, os; print('PROBE=' + str(os.environ.get('RTS_PROBE_FROM_ENV_FILE')))"], cwd=ROOT, env={...,"RTS_ENV_FILE": tmp/custom.env})`，断言 `PROBE=yes`（L140-156） | 改 `unified_scanner.py` 的 `load_dotenv` 读取口；此测试与视觉无关但**会随 Python 环境变化** |
| `test_closing_window_kills_python_child` L162 | `'app.on("window-all-closed"'`、`'app.on("before-quit"'`、`"stopScanner()"`（L164-166）；`re.search(r"function stopScanner\(\)[\s\S]{0,400}proc\.kill\(\)", code)`（L167） | 改这两个事件名、把 `proc.kill()` 移出 `stopScanner` 400 字符窗口、把 `stopScanner` 改成箭头函数 |
| `test_second_instance_does_not_start_second_scanner` L170 | `"requestSingleInstanceLock"`、`"second-instance"`；`code.split("requestSingleInstanceLock")[1]` 里必须有 `"app.quit()"`（L172-174） | 删单实例锁、把 `app.quit()` 移到锁之前、改事件名 |
| `test_extra_resources_bundle_python_and_repo_but_not_secrets` L180 | 读 `package.json["build"]["extraResources"]`，按 `entry["to"]` 建字典；要求 `to == "python"` 与 `to == "repo"` 都存在（L183）；`repo` 的 `filter` 数组必须含 `"!.env"`、`"!scanner.db-wal"`、`"!scanner.db.bak*"`、`"!desktop/**"`（L184-188） | 删/改这 4 个 filter 字符串；删/改 `to` 键；把 `extraResources` 改成对象 |
| `test_package_scripts_are_self_contained` L191 | `scripts["dist"].startswith("node prepare.mjs")`；`scripts["dist"]` 含 `"electron-builder"`；`scripts` 含键 `"fetch-python"`（L193-195） | 把 `dist` 脚本前缀改成 `pnpm …`/加 `&&` 前缀等（当前 `desktop/package.json:9-10` 是 `node prepare.mjs && electron-builder …`） |
| `test_prepare_rejects_python_that_cannot_import_entry` L201 | `desktop/prepare.mjs` 文本含 `"import unified_scanner"`、`"PYTHONNOUSERSITE"`、`"requirements.lock"` 或 `"requirements"`（L208-210） | 简化 `prepare.mjs` 的真 import 检查（`:50`）、删 `PYTHONNOUSERSITE`（`:47`）、删报错文案里的 `requirements` 字样 |
| `test_entry_module_level_imports_are_declared_in_requirements` L213 | AST 解析 `ROOT/unified_scanner.py` 的**模块级** `Import`/`ImportFrom`（L226-232），排除 stdlib 与 `{scanner,tests,scripts}`（L234-235）；别名表 `{"dotenv":"python-dotenv"}`（L239）；`requirements.txt` 与 `requirements.lock` 逐行规范化（去注释、`-` 行、按 `[=<>!~]` 切版本、小写、`_`→`-`）；每个第三方模块必须在**两个文件**都出现（L254-258）；额外锚点 `"python-dotenv" in declared["requirements.lock"]`（L261） | 给 `unified_scanner.py` 新增模块级第三方 import 而不改两个 requirements；删 `python-dotenv==1.0.1`（`requirements.lock:18`） |

**对窗口壳的直接结论**：`BrowserWindow` 的 `width/height/minWidth/minHeight/backgroundColor/title/autoHideMenuBar/webPreferences`、`frame`、`titleBarStyle`、`icon`、`Menu`、`Tray`、窗口位置记忆——**没有任何一条 pytest 读这些字段/选项**（`_main_js()` 是整文读，但所有断言都列在上表）。改 `createWindow()`（`main.js:267-310`）的窗口选项，只要**不碰**上表的字面量与结构，守卫不会红。风险来自运行时/打包（§3）。

### 1.6 其它与 web/desktop 相关、会读前端源码或契约的测试（本次实际 grep 到的全部）

| 文件 | 断言 | 对视觉重做的意义 |
|---|---|---|
| `tests/test_prior_column.py:175-185` | 见 §1.2 末尾：读 `rts-panel-cloud/web/src/lib/cols.ts`（**相对 cwd**），锁 `const POOL_CELLS` 块与「无评分列」 | 别动 `POOL_CELLS`；从仓库根跑 pytest |
| `tests/test_panel_serialize.py:274-276, 279-298` | `SCHEMA_VERSION == 2`；payload 顶层/区域/gate 字段集精确相等（`:281-284`） | 纯表现层不触发；动契约即红 |
| `tests/test_ai_digest.py:133-149` | `test_ts_slot_vocabulary_is_covered_by_python_guard`：`skipif(not TS_GUARD.exists())`，`TS_GUARD = ROOT/rts-panel-cloud/worker/src/explain/guard.ts`（`:42`）；解析双引号槽位 `re.findall(r'"([^"\\\n]{2,})"', ts_src)`，要求 ≥30 条（防空转），且每条都在 `scanner/ai_digest/*.py` 中以 `"<槽位>"` 出现（`:143-149`） | 只读 worker 侧 `guard.ts`，**不读 `web/src`**；视觉重做不触发 |
| `rts-panel-cloud/web/src/api/client.test.ts`（vitest，133 行） | 桩替 `fetch`；锁 `apiGet` 相对路径 `"/api/regions"`（`:40-44`）、404 `NO_SNAPSHOT`/400 `BAD_REQUEST`/`NOT_IN_REPORT` 文案、非 JSON → `BAD_RESPONSE`、缺 `ok` → `BAD_RESPONSE`、网络错 → `NETWORK`、超时/取消、`apiSend` POST 且无 `Authorization`（`:34-132`） | 若重做时改 `api/client.ts` 的请求/信封解析即红；**纯表现层不触发** |
| `rts-panel-cloud/worker/test/api.test.ts:253-273` | `expect(SCHEMA_VERSION).toBe(2)`；`regions.cols` 原样下发 | 只在跑 worker vitest 时相关 |
| `rts-panel-cloud/worker/test/stock.test.ts:1-6` | 仅在**注释**里声明契约来源是 `web/src/api/types.ts::KlineResponse`；没有读文件 | 无自动校验 |

未发现任何测试读取 `WEB/index.css`、`WEB/pages/Stock.tsx` 的 `ASHARE_THEME`、`desktop/main.js` 的 BrowserWindow 选项、`desktop/pnpm-workspace.yaml` 或 `web/pnpm-workspace.yaml` 的 `allowBuilds`。

---

## 2. 可自由改动区

### 2.1 放心改（无 pytest 读取，仅需守住 §1 的文案/结构雷区）

| 文件 / 职责 | 可改内容 | 必须保留 / 注意 |
|---|---|---|
| `WEB/index.css`（355 行） | `@theme`（L328-355）全部 token 的**值**；`.region-*` / `.gate-*` / `.sort-head` / `.table-scroll` / `.region-table` / `.sticky-col` / `.tone-*` / `.cell-*` / `.mark-*` / `.row-clickable` / `.region-empty` / `.kv-total` 的**规则内容**；新增纯 CSS 类/动画/字体/间距 | ① `@theme` **变量名**别随意改（`--color-card` ↔ `bg-card` 等工具类一一对应，`index.css:14-15`）；② 改 `--color-up/down/flat` 值必须同步 `WEB/pages/Stock.tsx:31-36` 的 `ASHARE_THEME`（`index.css:10-13` 的纪律）；③ `bg-bg/text-text/border-border` 等由 Tailwind v4 生成，改名会静默失效 |
| `WEB/App.tsx` 布局骨架（L277-377） | 外层 `div` 的 class、栅格/分栏尺寸、`aside` 宽度、折叠按钮、`<footer>` 文案与结构、空态 `<main>`（L361-368）的视觉呈现、新增 wrapper / 品牌头 / 间隔 | ① `TITLES.*.full` 3 个完整串（L45-47）；② `hotSubtitle`/`offboardSubtitle` 的 export 形状与逐字模板（L51-55）；③ `MAIN_SCROLL_SELECTOR`、`scrollMainTo`、`useActiveSection` 依赖的 DOM 契约（见 §2.2）；④ `PANEL_GROUP_ID = "rts-panel-main"`（L221，可改但会丢已存的 localStorage 布局；`App.tsx:220` 注释）；⑤ `web/src` 全域 9 个结论词禁令（`test_panel_web_cols.py:123-138`） |
| 新增展示型组件（`WEB/components/**`） | 新增卡片头/徽章/骨架屏/空态插画/分隔线/图例等**纯渲染**组件；可自由用 Tailwind 类与 `cn()` | ① 代码里不得出现 11 个列名字面量（`test_panel_web_hygiene.py:94-112`）——包括行尾注释；② 不得出现 9 个结论词（`test_panel_web_cols.py:126-138`）；③ 不得出现裸 `fetch(`、`XMLHttpRequest`、`"仅解读，非投资建议"`（§1.1）；④ 新增 `.ts/.tsx` 会被自动纳入 `_ts_files()` 扫描，**没有白名单** |
| `WEB/components/StatusBar.tsx`（81 行） | `<header>` 的 class、`role`、文案排版、徽章、颜色、密度；`lagRounds`/`sessionLabel` 的**纯函数实现**也可改（无 vitest 覆盖） | 建议保留消费 `meta/snapshot/error/intervalMs/now` 这 5 个 props 的语义（L36-42、`App.tsx:279`）；`本地服务无响应 · 显示最后一份快照` / `等待首轮上报` 等状态文案对应 `useSnapshot.ts:44-62` 与 `test_local_panel.py:218-231` 的后端语义，改名要同步设计说明 |
| `WEB/components/Sidebar.tsx`（84 行） | `NAV_ITEMS` 的 `hint`/`label` 呈现、按钮样式、徽章样式、折叠态 | `NAV_ITEMS` 的 `key`（`pool/hist/hot/gate`）与顺序被 `App.tsx:83/94/106`、`counts`（`App.tsx:257-265`）、`scrollMainTo`（`App.tsx:127`）、`GatePanel.tsx:67` 的 `anchor-gate` 依赖；`label` 别写 11 个列名/9 个结论词（本文件头注释 L6-11 已自警） |
| `WEB/components/RegionCard.tsx`（138 行） | 卡片/头部/段标题的 DOM、`EMPTY_TEXT` 的**视觉包装**（徽章/图标）、`临时排序`/`还原顺序` 按钮样式、`frozen` 传参 | ① `id={`anchor-${anchorId}`}`（L92）必须保留；② 空态文案 `—（本轮无数据）`（L20）是设计规定，且 `RegionTable.tsx:31` 有同一句（两份手抄，无测试锁死，但改一个不改另一个会视觉不一致）；③ `fullTitle.startsWith("◆ ") ? slice(2)`（L88）依赖 `TITLES.*.full` 的 `◆ ` 前缀；④ `region-card`/`region-title`/`region-count` 等类名被 `index.css` 与 `GatePanel.tsx:68` 复用 |
| `WEB/components/RegionTable.tsx`（162 行） | `<table>` 结构、`<th>/<td>` 样式、排序箭头、`marks-cell` 呈现、`frozen` 行为 | ① 列头**必须**来自 `spec.label`（L105-111），不得写死列名；② `className="region-table"`、`table-scroll`、`sticky-col`、`cell-l/r/c`、`tone-*`、`cell-strong`、`marks-head/marks-cell`、`mark-*`、`row-clickable`、`region-empty` 被 `index.css:26-159` 结构性选择器依赖（L39-40 注释说明刻意不用工具类）；③ `stockHref`/`onNavigate` 的 hash 路由（L80、`lib/router.ts:29-31`） |
| `WEB/components/GatePanel.tsx`（143 行） | 账目区布局、`Tabs` 样式、行按钮样式、分组标题呈现 | ① `id="anchor-gate"`（L67）与 `App.tsx:151/252-275` 的右栏高亮逻辑配对；② `TITLE`（L28）与 `render.py` 过门区标题同源（本文件注释 L27 说明，**无 pytest 读前端这一串**，但属设计纪律）；③ `gate.stats` 七字段（L38-44、L74-106）须继续原样显示（`test_panel_contract.py:62-79` 锁的是后端/云端字段名）；④ `region-card`/`gate-stat*`/`gate-row` 类名被 CSS 依赖 |
| `WEB/pages/Stock.tsx` 表现层（300 行） | 分区卡片、表头、空态、AI 解读区布局、`CHART_HEIGHT` | ① `ASHARE_THEME` 6 个色值随 `@theme` 同步；② `toKLineData`（L42-53）的元组解构顺序 `[date,open,close,low,high,volume]` 由 `test_local_panel.py:335-350` 的后端形状决定；③ `data.label` 必须渲染服务端下发值（L257，§1.1 红线标签守卫）；④ `errorText` 的 `NOT_IN_REPORT`/`BAD_REQUEST`/`NETWORK` 等码对应 `client.ts` 与 `test_local_panel.py:353-387` |
| `WEB/index.html`（12 行） | `<title>`、`meta viewport`、`<body>` 静态壳 | `#root` id（`main.tsx:14-15` 硬查）；`<script type="module" src="/src/main.tsx">` 由 Vite 构建替换；`meta name="viewport" content="width=1440"`（L5）与窗口 1440×900（`main.js:269-270`）同一假设 |
| `WEB/vite.config.ts` / `tsconfig.json` | `build.outDir`、`sourcemap`、插件顺序、alias | `outDir: "dist"`（L43）被 `scanner/local_panel_server.py:71` 的 `DEFAULT_STATIC_DIR` 与 `desktop/prepare.mjs:29` 写死；`resolve.alias "@"`（L23-25）与 `tsconfig.json:17-19` 的 `paths` 必须同步（L22 注释） |
| `DESKTOP/main.js` 的 `createWindow()` BrowserWindow 选项（L267-281） | `width/height`、`minWidth/minHeight`、`backgroundColor`、`title`、`autoHideMenuBar`、`frame`、`titleBarStyle`、`icon`、新增 `webPreferences` 展示相关项 | 见 §3；**不得**破坏 §1.5 的字面量；`webPreferences.contextIsolation/nodeIntegration/sandbox`（L276-280）虽无测试，属安全基线，建议别动 |
| `DESKTOP/main.js` 的 `errorPage()`（L229-265） | 内联 HTML/CSS 的视觉（错误页不是 `web/src`，不受任何前端守卫） | 保留 `escapeHtml`（防日志注入）与 `data:text/html;charset=utf-8,` 的 `loadURL` 形态（L264、L293/301/303） |

### 2.2 必须保留的挂载点 / 属性（不是测试锁的，是运行时算法与 CSS 依赖）

| 依赖点 | 位置 | 谁依赖 | 丢了会怎样（无测试会红） |
|---|---|---|---|
| `data-scroll-root="main"` | `App.tsx:339` | `MAIN_SCROLL_SELECTOR`（`App.tsx:124`）、`scrollMainTo`（:129）、`useActiveSection`（:173） | 侧栏点击不滚动、滚动高亮永久停在首项（`App.tsx:131-145` 注释列了 4 个 Electron/Chromium 实测坑） |
| `data-scroll-root="side"` | `App.tsx:347` | 目前仅注释/结构标记（`App.tsx:150-161` 说明右栏是另一滚动容器）；无读取代码 | 低风险，但改名前确认没有新代码依赖 |
| `id="anchor-<key>"`：`anchor-pool` / `anchor-hist` / `anchor-hot` / `anchor-gate` | `RegionCard.tsx:92`（前 3 个由 `App.tsx:83/94/106` 传 `NAV_ITEMS[i].key`）、`GatePanel.tsx:67` | `scrollMainTo`（`App.tsx:127`）、`useActiveSection`（:176）、`Sidebar` 的 4 个 key（`Sidebar.tsx:34-39`） | 导航跳转/高亮静默失效 |
| `id=PANEL_GROUP_ID`（`"rts-panel-main"`）与 `<Panel id="main-table">` / `id="side-stats"` | `App.tsx:221/330/338/344` | `useDefaultLayout({id})`（L240）写 localStorage；`PanelGroup` 的 `defaultLayout/onLayoutChanged`（L332-333） | 拖拽宽度不持久化；改 `id` 会让老用户已存布局失效（不报错） |
| `className="region-table"` + `.table-scroll` + `.sticky-col` | `RegionTable.tsx:84-85/94/140`；CSS `index.css:27-93` | 结构性选择器 `thead th` / `tbody tr` / `position: sticky`（`index.css:21-24` 注释：刻意不用工具类） | 表头不吸顶、首列不冻结、横向滚动错位 |
| `region-card` / `region-head/title/count/fulltitle/sub/segtitle/sortnote/sortreset` / `region-empty` / `sort-head` / `sort-arrow` / `sort-active` / `gate-stat*` / `gate-row` / `kv-total` / `row-clickable` / `cell-l/r/c` / `tone-up/down` / `cell-strong` / `mark*` / `marks-head/cell` | `index.css:26-326`；组件见上表 | 纯 CSS，无类型检查；改名 = 视觉静默退化 |
| `#root` | `index.html:9` | `main.tsx:14-15`（`if (!el) throw`） | 白屏 |
| hash 路由前缀 `#/stock/` / `#/` | `lib/router.ts:17/29/33`；`RegionTable.tsx:80`、`GatePanel.tsx:62`、`Stock.tsx:109` | 详情页导航；`router.ts:8-10` 注释：hash 路由**不需要** Python SPA 回退 | 若改 history 路由才需要 `local_panel_server._static` 的回退（它已实现，L261-265），但那是 Python 改动 |
| `/api/*` 相对路径 + `{ok:true,data}` 信封 | `api/client.ts:117-127`、`:45-90`；`useSnapshot.ts:44-62` | `test_local_panel.py` 全套、`client.test.ts` | 改 baseURL/信封会同时打红 Python 与 vitest |

---

## 3. Electron 壳打磨可行性

读过的文件：`DESKTOP/main.js`（340 行）、`DESKTOP/package.json`（66 行）、`DESKTOP/prepare.mjs`（75 行）、`DESKTOP/pnpm-workspace.yaml`（11 行）、`DESKTOP/fetch-python.mjs`（仅被 `test_desktop_shell.py:83-84` 检查一行）、`scanner/local_panel_server.py`（317 行）、`WEB/lib/router.ts`、`WEB/components/StatusBar.tsx`、图标资源全仓扫描。

### 3.1 现状事实

- `createWindow()`（`main.js:267-281`）已有：`width:1440, height:900, minWidth:900, minHeight:600, backgroundColor:"#0f1115", title:"创业板飙升榜 · 桌面看板", autoHideMenuBar:false`，`webPreferences:{contextIsolation:true, nodeIntegration:false, sandbox:true}`。**未设** `frame` / `titleBarStyle` / `icon` / `x,y` / `show` / `Menu` / `Tray` / `preload` / `ipcMain`。
- 已有 `app.requestSingleInstanceLock()` + `second-instance` + `app.quit()`（`main.js:316-325`），且被 `test_desktop_shell.py:170-174` 锁死。
- 已有 `window-all-closed → app.quit()` 与 `before-quit → stopScanner()`（`main.js:329-338`），被 `test_desktop_shell.py:162-167` 锁死。
- `webContents.setWindowOpenHandler` 把外链交给 `shell.openExternal`（`main.js:285-289`）。
- 生产态加载 `http://127.0.0.1:<port>/`（`main.js:299`），页面由 Python `local_panel_server._static()` 从 `web/dist` 同源托管（`scanner/local_panel_server.py:14-16, 249-274`）；hash 路由 `lib/router.ts:8-10` 明确不需 SPA 回退。
- `build.files = ["main.js","package.json"]`（`package.json:19-22`）；`extraResources` 带 `resources/python → python` 和 `../ → repo`（含 4 条被锁 filter，`:23-55`）；`electron-builder` 的 `win.target: ["dir"]`（`:56-60`）。**没有 `icon` 配置项，没有 `asar` 显式配置**。
- **图标资源**：`desktop/` 下非 Python 运行时目录里**没有任何**应用图标。`find` 只命中 `desktop/resources/python/Lib/idlelib/Icons/idle*.ico|png`（IDLE 自带，属 Python 标准库资产，不适合当产品图标）与 `desktop/resources/python/tcl/tk8.6/demos/images/*.png`（Tk 演示图）。`docs/features/desktop-ui-modernize/panel-*.png` 是**界面截图**，不是图标；`desktop/dist/win-unpacked/resources/repo/docs/...` 是旧构建产物里的同一批截图副本。`electron-builder` 在无 `build.win.icon` 时会用 Electron 默认图标（**推测**，未在打包态验证；`docs/desktop/IMPLEMENTATION.md:174-177` 记录打包目标只做 Windows）。

### 3.2 逐项结论

| 改动 | 结论 | 理由 / 配套改动 / 风险 |
|---|---|---|
| `minWidth` / `minHeight`（已有 900×600，`main.js:271-272`） | **可独立做** | 无测试读这两个值；但 `App.tsx:278` 用 `h-dvh + flex`、`:338/344` 的 `Panel minSize={45}/{18}`、`index.html:5` 的 `width=1440` 与 `docs/features/desktop-ui-modernize/HANDOFF.md:56`「≤900px 未实测」共同构成事实下限；调小需真机验窄屏（**未验证**），调大不改前端即可 |
| `backgroundColor`（已有 `#0f1115`） | **可独立做** | 与 `WEB/index.css:335` 的 `--color-bg: #0f1115`、`main.js:247` 错误页、`main.js:273` 三者是手抄同值；改一处建议三处同改，避免首帧闪白/错误页色差。无测试锁 |
| `width/height` 初始值（1440×900） | **可独立做** | 与 `index.html:5` `viewport width=1440` 是软耦合（Electron 不使用该 meta 定窗口）；无测试锁 |
| `icon`（BrowserWindow / `build.win.icon` / Tray） | **需要配套改动** | ① 当前**无可复用产品图标**（§3.1）；须新造 `.ico`（Windows 建议多尺寸 16/32/48/256）并放进 `desktop/`；② `build.files` 只含 `main.js`+`package.json`（`package.json:19-22`），若图标放在 `desktop/assets/`，**必须**把该路径加进 `build.files`（或 `extraResources` 并改 `main.js` 的取值路径）；`extraResources` 的 `from:"../"` 过滤器含 `"!desktop/**"`（`:41`，被 `test_desktop_shell.py:188` 锁死），所以**不能**指望 `repo` 那份带出 `desktop/` 下的图标；③ `electron-builder` 的 `win.icon` 与 `BrowserWindow.icon` 是两处配置，建议都设；④ `main.js` 里出现新 `path.join(...)` 不影响 §1.5 的正则（它的 `resourcesPath, "python"` 断言只匹配一处），但别动 `findPython()` |
| `frame: false` 自定义标题栏 | **需要配套改动，且本期建议放 P2 末尾 / 先做原型** | ① `main.js` 当前 `require("electron")` 只取 `{app, BrowserWindow, shell}`（`:30`），**没有** `ipcMain`、`Menu`、`Tray`、`nativeImage`；`webPreferences` **没有 `preload`**（`:276-280`），且 `sandbox:true` + `contextIsolation:true`。Windows 无边框窗口的拖拽靠渲染进程 CSS `-webkit-app-region: drag`（`WEB/index.css` 目前**完全没有**该声明，全仓 grep 无命中），最小化/最大化/关闭按钮必须经 IPC 到主进程 ⇒ 需要新增 `preload.js`（`contextBridge.exposeInMainWorld`）+ `ipcMain.on/handle` + `win.minimize()/maximize()/close()`；`sandbox:true` 下 preload 可用但只能用受限 API（**Electron 44 具体行为未验证**）；② `build.files` 要加 `preload.js`（`package.json:19-22`）；③ 自定义标题栏 DOM 应放 `WEB/App.tsx` 顶部，与现有 `StatusBar`（`h-9 sticky top-0 z-10`，`StatusBar.tsx:55-58`）**双头部冲突**：要么把 `StatusBar` 并进标题栏（改 `role="banner"`/`aria`），要么在标题栏下保留它；`App.tsx:278` 的 `h-dvh` 需保证新头 + 状态条 + `<footer>` 高度链不断；④ `-webkit-app-region: drag` 覆盖到按钮/输入框会吃掉点击，须给交互元素 `no-drag`（**Electron/Chromium 行为未在本仓验证**）；⑤ 与 Python 同源托管**不冲突**（页面仍是 `http://127.0.0.1:<port>/`，`main.js:299`），与 hash 路由**不冲突**（`lib/router.ts:8-10`，`#/stock/...` 只是 fragment）；⑥ 无边框会失去系统窗口阴影/贴边吸附/Alt+Space 菜单（**推测**，未在 Electron 44 实测） |
| `titleBarStyle: 'hidden'`（macOS 语义） | **建议别碰** | `docs/desktop/IMPLEMENTATION.md:174-177` 明确「打包目标只做 Windows」、`findPython()` 的 win32 分支（`main.js:73-75`）、`package.json:56-60` 只有 `win`；该选项主要服务 macOS `hiddenInset`，Windows 上效果与 `frame:false` 不同且易出现「按钮还在但拖拽区不对」的半成品态（**未验证/推测**）。若真要跨平台，属另立任务 |
| 应用菜单去除（`Menu.setApplicationMenu(null)` 或 `autoHideMenuBar:true`） | **可独立做（低风险）** | `autoHideMenuBar` 已是显式 `false`（`main.js:275`），改成 `true` 或在 `app.whenReady()` 后 `Menu.setApplicationMenu(null)` 都不碰 §1.5 的任何断言；`require("electron")` 需加 `Menu`（`:30`）。注意 Windows 下 `frame:false` 时菜单栏本就不显示；若同时上自定义标题栏，二者合并做即可 |
| 系统托盘 `Tray` + 右键菜单 | **需要配套改动，且「最小化到托盘」建议别碰** | ① 需要图标（见 `icon` 行）+ `Tray`/`Menu`/`nativeImage` 引入 + `build.files` 覆盖；② **与生命周期守卫冲突**：`test_desktop_shell.py:162-167` 锁 `'app.on("window-all-closed"'` 且 `main.js:329-332` 的语义是「关窗 = 退出应用 ⇒ 一并收掉 Python，不留后台扫描器」；`docs/desktop/IMPLEMENTATION.md:94`（验收 7）与 `docs/desktop/CONTEXT.md:69` 也要求「关窗 kill 子进程、锁释放」。若做「关闭=隐藏到托盘、托盘退出才 quit」，必须保留 `window-all-closed` 里的 `app.quit()` 字面量（否则守卫红）或改成「最小化到托盘但不改关窗语义」；③ 托盘常驻意味着 Python 子进程常驻，会与终端扫描器的单实例「杀旧」语义（`docs/desktop/IMPLEMENTATION.md:132-134`）叠加复杂度；④ `second-instance` 现有逻辑只 `restore/focus`（`main.js:320-325`），托盘化后还要处理「从托盘恢复」；⑤ `errorPage` 路径（`main.js:293/301/303`）仍需在窗口里可见。⇒ **只加「显示/退出」的托盘图标、不改变关窗=退出** 是可做的；「最小化到托盘 + 后台常驻」本期别碰 |
| 单实例锁 | **已存在，别碰** | `main.js:316-325` + `test_desktop_shell.py:170-174`。可做的只是**打磨** `second-instance` 的行为（例如 `win.show()`），但 `app.quit()` 必须留在锁之后、事件名 `second-instance` 不能改 |
| 窗口尺寸/位置记忆 | **可独立做（但注意与 `userData` 纪律共存）** | 无测试锁。实现建议在 `app.getPath("userData")`（`main.js:90`，已被 `test_desktop_shell.py:100` 要求出现）下放一个 JSON（如 `window-state.json`），`BrowserWindow` 构造时读、`"close"`/`"move"`/`"resize"` 时写；`preparePackagedState()`（`main.js:88-101`）的 `if (!fs.existsSync(dbPath)) { … }` 正则（守卫 L103）**必须保持原样**，不要在新逻辑里另写一个形似 `if (!fs.existsSync(dbPath)) {` 的块以免正则误匹配/混淆；别用 `node:sqlite`/`better-sqlite3`（守卫 L115）；多显示器/窗口在屏幕外的兜底属**推测**，需实测。注意 `App.tsx:221` 的 `rts-panel-main` 已用 localStorage 记**分栏宽度**，两套持久化互不冲突 |
| 打包配置 `extraResources` / `build.files` | **多数不改；新增本地资源才改** | `extraResources` 的 `to`/4 条 filter 与 `scripts.dist` 前缀被 `test_desktop_shell.py:180-195` 锁死，别动。`build.files`（`package.json:19-22`）**未被测试逐项锁**（守卫只读 `extraResources` 与 `scripts`），所以加 `preload.js` / `icon.ico` 到 `files` 是**可做的**；但要确认 `electron-builder` 的 asar 打包下 `BrowserWindow.icon`/`Tray` 用 `__dirname` 相对路径能取到（**未验证**，建议用 `extraResources` + `process.resourcesPath` 或 `app.getAppPath()` 两条路各验一次）。`prepare.mjs:23-32` 只检查 `resources/python` 与 `web/dist/index.html`，**不检查新增图标/preload**，故新资源缺失时打包仍会「成功但运行时缺图」（`prepare.mjs:1-13` 的立意为「静默缺失前置检查」）⇒ 建议把新资源也加进 `CHECKS`，但这会动 `prepare.mjs`（`test_desktop_shell.py:201-210` 只要求 3 个字符串仍在，新增检查不冲突） |

**冲突面总结**：自定义标题栏与「Python 同源托管」「hash 路由」**没有协议层冲突**（`main.js:299` + `router.ts:8-10` + `local_panel_server.py:14-16`）；真正的冲突是 ① 与 `StatusBar` 争顶部空间/角色（`StatusBar.tsx:55-58` vs 新标题栏）；② 缺 preload/IPC 通道（`main.js:30/276-280` 现状）；③ 托盘与「关窗即退出杀 Python」的验收语义（`test_desktop_shell.py:162-167`）；④ 图标资源缺失与 `build.files` 白名单（`package.json:19-22`）。

---

## 4. 契约与构建的连带项：「零 Python 改动」是否成立

**结论：成立，且有条件。**

| 连带项 | 是否需要改 | 依据 |
|---|---|---|
| `scanner/local_panel_server.py` 静态托管 / SPA 回退 | **不需要** | `_static()`（`local_panel_server.py:249-274`）已按文件回放 `web/dist` 并对未知非 `/api` 路径回退 `index.html`（L261-265）；`DEFAULT_STATIC_DIR`（L71）固定指向 `rts-panel-cloud/web/dist`。`lib/router.ts:8-10` 明确 hash 路由**不需要** SPA 回退，现有回退只是为将来 history 路由预留。视觉重做只要仍 `pnpm build` 出 `dist/index.html`、仍用 `/api/*` 相对路径（`api/client.ts:117-127`），Python 零改动。⇒ **只有**改路由形态（hash→history）或新增 `/api` 端点/改 `dist` 输出目录时才需要动 Python，且会撞 `test_local_panel.py:164-207` |
| `desktop/prepare.mjs` 的 `web/dist/index.html` 前置检查 | **不需要改，但必须跑** | `CHECKS[1].path = REPO/rts-panel-cloud/web/dist/index.html`（`prepare.mjs:28-31`）；`test_desktop_shell.py:201-210` 锁它必须含 `import unified_scanner` / `PYTHONNOUSERSITE` / `requirements`。`dist/` 不入 git（`web/.gitignore` 的 `dist/`；`docs/features/desktop-ui-modernize/交付文档.md:120` 明确「打包前必须先跑一次 `pnpm build`」）。若新增图标/preload 资源，建议（非必须）把资源也加进 `CHECKS`（见 §3.2） |
| `web/pnpm-workspace.yaml` 的 `allowBuilds` | **不新增依赖 ⇒ 不动；新增依赖 ⇒ 看 postinstall** | 当前只有 `esbuild: true`（`web/pnpm-workspace.yaml:11-12`）；注释 L1-10 说明 pnpm≥10 拦 postinstall、pnpm 12 不再读 `package.json` 的 `pnpm` 字段、键名必须是 `allowBuilds`。`docs/features/desktop-ui-modernize/CONTEXT.md:50` 提示新增带 postinstall 的依赖要补键（举例 `@tailwindcss/oxide`/`lightningcss`），但 `交付文档.md:127` 实测 Tailwind v4 原生二进制**不需要**补键。⇒ 纯 CSS/组件改造不碰；若加图标库/动画库且带原生 postinstall，需 `pnpm approve-builds --all` 后核对本文件与 `pnpm-lock.yaml`。`desktop/pnpm-workspace.yaml:9-11` 另有 `electron`/`electron-winstaller` 两个键，窗口壳改造若**不新增 Electron 侧依赖**也不动 |
| `requirements.lock` / `requirements.txt` | **不动** | `test_desktop_shell.py:213-261` 把 `unified_scanner.py` 的模块级第三方 import 与这两个文件逐项比对，并硬锚 `python-dotenv`（`requirements.lock:18`）。视觉重做不新增 Python import ⇒ 无需改；**改了反而**可能因新增未登记包变红。注意 `test_desktop_shell.py:134-156` 的 `test_env_file_override_is_honored_by_entry` 会真起 Python 跑 `import unified_scanner`（timeout 300s），跑全量 pytest 时它需要本机 Python 依赖可用 |
| `scanner/panel_serialize.py` / `worker/src/serialize.ts` / `web/src/api/types.ts` 三端镜像 | **不动** | `SCHEMA_VERSION=2` 被 4 处锁死（§1.2）；`HANDOFF.md:57` 提议的「把 `OFFBOARD_OPENING_SILENCE_MIN` 随 `colSpec` 下发」会动 `Regions` 字段集 ⇒ 触发 `test_panel_contract.py:82-90`/`:93-100`、需升版本并三端同改（`docs/desktop/IMPLEMENTATION.md:122-127` 把「加一段就要 SCHEMA_VERSION 3」列为已知取舍）。**视觉重做不碰这条** |
| `web/dist` | **必须重新生成（不是源码改动）** | `pnpm build` 的输出；`test_local_panel.py:164` 断言缺 dist 时 `/` 404，属预期 fail-open；`prepare.mjs` 会检查它 |
| `desktop/package.json` / `main.js` | **P2 才动** | 见 §3；`extraResources`/`scripts` 的守卫字面量别碰 |

**「零 Python 改动」的精确定义**：`scanner/**`、`unified_scanner.py`、`tests/**`、`requirements*.txt|lock` 全部保持原样；只改 `rts-panel-cloud/web/src/**`（+ 可选 `web/index.html`、`web/pnpm-workspace.yaml` 当且仅当新增依赖）+ P2 的 `desktop/**`（+ 可选新图标/preload 资源）。此结论与上一轮 UI 现代化的实测一致：`docs/features/desktop-ui-modernize/HANDOFF.md:9` 写「零 `.py` 改动、零 `scanner/` 改动、零契约改动」，`:36-49` 的验证命令也是纯前端 + 纪律守卫 + `node prepare.mjs`。

**反例（会破零 Python）**：改 API 路径或信封、改 `regions` 字段集/加「榜内异动」段、改端口常量（`scanner.config.LOCAL_PANEL_PORT` ↔ `main.js:40-42` ↔ `vite.config.ts:37` 三处同步）、把 hash 路由换 history 且删掉/改写 `_static` 回退、给前端新增一个本地只读端点。

---

## 5. 落地分期建议

> 每期都**不改 Python / 不改契约 / 不新增后端端点**；命令均为建议，本次审计**未执行**任何一条。前端命令的工作目录是 `rts-panel-cloud/web`，pytest 与 `node prepare.mjs` 的工作目录见命令行注释。全量 pytest 的已知既有失败见 `docs/features/desktop-ui-modernize/CONTEXT.md:158`（6 个，已用 `git stash` 验证与前端无关）与 `docs/desktop/HANDOFF.md:76`（3 个 `test_data_health.py`）——**跑全量时先看基线，别把预存在失败算到本次头上**。

### P0 · 身份与层级（一眼可见）

- **目标**：品牌头/产品名、主次层级、设计 token 一次收敛；不动数据语义、不动窗口。
- **涉及文件**：`WEB/index.css`（`@theme` 值 + `.region-*`/`.gate-*` 视觉）、`WEB/App.tsx`（外层骨架 + `<footer>` + 顶部品牌区，L277-377）、`WEB/components/StatusBar.tsx`（表现层）、`WEB/components/Sidebar.tsx`、`RegionCard.tsx`、`GatePanel.tsx`、`RegionTable.tsx`（class/DOM 包装）、`WEB/pages/Stock.tsx`（`ASHARE_THEME` 同步 + 卡片样式）、`WEB/index.html`（`<title>`）。可选：新增纯展示组件 `WEB/components/Brand*`、`EmptyState.tsx`。
- **必须保留**：§2.2 全表；`TITLES` 3 串、`hotSubtitle/offboardSubtitle`、9 个结论词禁令、11 个列名词禁令、`data-scroll-root`、`anchor-*`、`#root`。
- **风险**：**中低**。最大静默风险是改 `@theme` 变量名导致 Tailwind 工具类全失效（`index.css:14-15`），以及 `ASHARE_THEME` 忘记同步造成 K 线颜色与表格不一致（`index.css:10-13`）。`components/**` 新文件会被 4 条 grep 守卫自动纳入扫描（§1.1），文案最容易踩雷。
- **验证命令**：
  ```bash
  # 前端（cd rts-panel-cloud/web）
  pnpm typecheck && pnpm test && pnpm build

  # 纪律守卫（仓库根；注意 test_prior_column.py 用相对路径读 cols.ts，必须从仓库根跑）
  python -m pytest tests/test_panel_web_hygiene.py tests/test_panel_web_cols.py tests/test_panel_contract.py tests/test_prior_column.py -q

  # 后端/壳回归（纯表现层也应全绿；仓库根）
  python -m pytest tests/test_local_panel.py tests/test_desktop_shell.py tests/test_panel_serialize.py -q

  # 打包前置门（cd desktop；要求上一步 pnpm build 已生成 web/dist/index.html）
  node prepare.mjs
  ```

### P1 · 空态与多态状态

- **目标**：`snapshot === null` / `meta.ready === false` / `error !== null`（`NETWORK`）/ `lag > 1` / 区块 0 行 / 详情页 `loading`、`error`、`NOT_IN_REPORT`、`NOT_FOUND` / AI 解读 `redlined` 的视觉分层（`useSnapshot.ts:22-29, 44-62`；`StatusBar.tsx:44-53`；`App.tsx:361-368`；`RegionCard.tsx:20,108-133`；`RegionTable.tsx:31,70-72`；`Stock.tsx:120-121, 137-138, 200-208, 253-258`；`GatePanel.tsx:115-117`）。
- **涉及文件**：`WEB/App.tsx`（空态 `<main>`）、`WEB/components/StatusBar.tsx`（状态徽章/色阶）、`WEB/components/RegionCard.tsx` + `RegionTable.tsx`（空态）、`WEB/components/GatePanel.tsx`（`—` 占位）、`WEB/pages/Stock.tsx`（各态）、`WEB/index.css`（新增状态类）、可新增 `WEB/components/StateBadge.tsx` / `Skeleton.tsx`。
- **必须保留**：后端状态码语义（`test_local_panel.py:218-231` 的 `ready:false` 200 / `NO_SNAPSHOT` 404 / `test_local_panel.py:353-387` 的 `NOT_IN_REPORT`/`BAD_REQUEST`）、`api/client.ts` 的错误分类（`client.test.ts:47-85`）、`StatusBar` 的 5 个 props、`data.label` 服务端单源（`test_panel_web_hygiene.py:120-136`）。**空态文案绝不能写 11 个列名或 9 个结论词**（例：`暂无建议` / `按涨幅…` 都会红）。`—（本轮无数据）` 在 `RegionCard.tsx:20` 与 `RegionTable.tsx:31` 是两份手抄，改要同改。
- **风险**：**中**。文案守卫是本期最高频红灯；其次是把「本地服务无响应 · 显示最后一份快照」误改成清空 `snapshot`（`useSnapshot.ts:55-60` 的 fail-open 语义，白屏回归）。
- **验证命令**：
  ```bash
  # cd rts-panel-cloud/web
  pnpm typecheck && pnpm test && pnpm build

  # 仓库根：四条 grep 守卫 + API 行为守卫（client.test.ts 由 pnpm test 覆盖）
  python -m pytest tests/test_panel_web_hygiene.py tests/test_panel_web_cols.py tests/test_local_panel.py tests/test_panel_contract.py -q
  python -m pytest tests/test_panel_serialize.py tests/test_prior_column.py -q

  # cd desktop
  node prepare.mjs

  # 手工（非自动守卫，建议真机）：起本地服务后断网/删 dist 验三态
  # RTS_LOCAL_PANEL=1 RTS_LOCAL_PANEL_PORT=8899 python unified_scanner.py 60 --no-feishu --no-lock
  ```
  端到端参考 `docs/features/desktop-ui-modernize/HANDOFF.md:41-48`（另开 shell 用 `electron <probe.js>` 断言 DOM）。

### P2 · 窗口壳与细节

- **目标**：先做零风险项，再考虑自定义标题栏。
  - P2a（**可独立做**）：`icon`（需先造图标并进 `build.files`）、`minWidth/minHeight`、`backgroundColor` 三处同色、`width/height` 初值、`autoHideMenuBar:true` 或 `Menu.setApplicationMenu(null)`、窗口尺寸/位置记忆（写 `userData/window-state.json`）、托盘「仅显示/退出」且**保持关窗=退出**。
  - P2b（**需配套、建议原型先行**）：`frame:false` + `preload.js`（`contextBridge`）+ `ipcMain` + `-webkit-app-region: drag/no-drag` + 把 `StatusBar` 与标题栏合并/分层 + `build.files` 加 `preload.js` + `prepare.mjs` 增补资源检查。
  - **建议别碰**：`titleBarStyle:'hidden'`（Windows-only 工程）、「最小化到托盘 + 后台常驻」（与 `test_desktop_shell.py:162-167` 的关窗 kill 语义冲突）、改 `extraResources` 的 4 条 filter / `scripts.dist` 前缀、动 `webPreferences` 的 `contextIsolation/nodeIntegration/sandbox`、改 `requestSingleInstanceLock` 结构。
- **涉及文件**：`DESKTOP/main.js`（`createWindow` L267-310、生命周期 L312-340）、`DESKTOP/package.json`（`build.files` L19-22、`win.icon`）、`DESKTOP/prepare.mjs`（可选新增 `CHECKS`）、新增 `DESKTOP/preload.js` / 图标资源；P2b 还要 `WEB/index.css`（`-webkit-app-region`、标题栏类）、`WEB/App.tsx`（顶部 DOM、`h-dvh` 高度链）、`WEB/components/StatusBar.tsx`（合并/降级为副行）。
- **必须保留**：§1.5 的 14 条字面量一个都别动；`HOST="127.0.0.1"`、`execFile(python, [...])`、`SCRUBBED_ENV`/`delete env[key]`/`PYTHONNOUSERSITE`、`preparePackagedState()` 的 `if (!fs.existsSync(dbPath)) {` 形状与 `|| state.*`、`window-all-closed`/`before-quit`/`stopScanner`/`proc.kill()`、`requestSingleInstanceLock` + `second-instance` + 锁后的 `app.quit()`、`extraResources` 的 `to: python/repo` 与 4 条 filter、`scripts.dist.startswith("node prepare.mjs")`；`test_prepare_rejects_python_that_cannot_import_entry` 的 3 个字符串。
- **风险**：**中高（P2b 高）**。守卫变红风险其实低（BrowserWindow 选项无断言），真正的风险是运行时/打包：`preload` 在 `sandbox:true` 下的可用性（**未验证**）、无边框拖拽区吞点击（**未验证**）、新资源没进 `build.files` 导致打包态缺图（`prepare.mjs` 现在不检查）、托盘与后台 Python 子进程残留、`StatusBar` 与新头部的 `sticky top-0 z-10`（`StatusBar.tsx:56`）叠加、`h-dvh` 高度链断裂导致分栏滚动失效（`App.tsx:336-341` 注释）。
- **验证命令**：
  ```bash
  # 前端（P2b 改了 WEB 才需要；cd rts-panel-cloud/web）
  pnpm typecheck && pnpm test && pnpm build

  # 壳结构守卫 + 打包前置门（仓库根 / cd desktop）
  python -m pytest tests/test_desktop_shell.py tests/test_local_panel.py -q
  cd desktop && node prepare.mjs
  # 可选：真打包（会产出 desktop/dist，属构建；本次审计未跑）
  # cd desktop && pnpm run dist

  # 纪律守卫回归（P2b 动了 App.tsx / index.css / 新组件，必须重跑；仓库根）
  python -m pytest tests/test_panel_web_hygiene.py tests/test_panel_web_cols.py tests/test_panel_contract.py tests/test_prior_column.py -q

  # 全量基线对照（仓库根；先看已知预存在失败，见本节开头）
  python -m pytest tests/ -q
  ```
  真机验收建议补一条「Electron 里点最小化/最大化/关闭后 `window-all-closed` 仍触发 `stopScanner()`」的手工检查（无自动测试覆盖；`docs/features/desktop-ui-modernize/HANDOFF.md:64-72` 把「打包态 `app.isPackaged` 行为」「分栏持久化刷新恢复」列为**未验证猜测**）。

### 分期共用门禁

无论哪一期，落地时**至少**跑：`pnpm typecheck && pnpm test && pnpm build`（`rts-panel-cloud/web`）→ `python -m pytest tests/test_panel_web_hygiene.py tests/test_panel_web_cols.py tests/test_panel_contract.py tests/test_prior_column.py tests/test_local_panel.py tests/test_desktop_shell.py -q`（仓库根）→ `node prepare.mjs`（`desktop`）。P2 追加真机 Electron 冒烟与（可选）`pnpm run dist`。

---

## 6. 未验证 / 推测（明确标注）

1. **未验证**：本次审计**没有跑**任何 pytest / pnpm / node / Electron / 打包命令；所有「不会变红」是基于阅读断言的**静态推断**，不是实测绿灯。
2. **未验证**：Electron 44 在 `sandbox:true` + `contextIsolation:true` 下 `preload.js` 的 `contextBridge` 行为、`-webkit-app-region: drag/no-drag` 与 Chromium 的点击吞没、`frame:false` 在 Windows 的窗口阴影/贴边行为（`docs/desktop/IMPLEMENTATION.md:148-150, 163-166` 只记录了 44 的 postinstall 与 DawnGraphiteCache 噪声）。
3. **推测**：`electron-builder` 无 `build.win.icon` 时回退 Electron 默认图标；`build.files` 加新资源后的 asar 路径取法（`__dirname` vs `process.resourcesPath`）；`titleBarStyle:'hidden'` 在 Windows 的实际表现。
4. **未验证**：窗口 `minWidth/minHeight` 调小后 ≤900px 的版面（`HANDOFF.md:56` 自承未实测）；`useDefaultLayout` 的刷新恢复（`HANDOFF.md:71`）；打包态 `app.isPackaged`（`HANDOFF.md:72`）。
5. **文档与代码不一致（已核实）**：`docs/features/desktop-ui-modernize/HANDOFF.md:57` 写 `OFFBOARD_OPENING_SILENCE_MIN（=15）`，而当前 `scanner/config_hot_watch.py:109` 是 **5**（`:115` 的 `_UNADJUSTED` 才是 15）；前端 `App.tsx:55` 手抄的是 `开盘 5 分内不产出`。以代码为准。
6. **未验证**：`test_prior_column.py:181` 用相对路径 `Path("rts-panel-cloud/web/src/lib/cols.ts")`，若 pytest 不在仓库根运行会 `FileNotFoundError`；本审计按「从仓库根跑」给命令。
7. 图标资源扫描范围：`desktop/`、`rts-panel-cloud/`（排除 `node_modules`/`dist`）与仓库根（排除 `.git`/`.alma-snapshots`/`node_modules`/`desktop/dist`）；未扫描用户主目录或系统图标库。

---

## 7. 交付回报（给发起人的三句话）

**最关键的 3 条「不能碰」**
1. `WEB/App.tsx:44-55` 的 3 个 `TITLES.*.full` 完整串 + `export const hotSubtitle/offboardSubtitle` 的逐字模板（含手抄的「开盘 5 分内不产出」）：`tests/test_panel_web_cols.py:90-116` 与 `:97-107` 双重锁死，且 Python 侧 `scanner/view/model.py` 是逐字真源。
2. `WEB/components/**` 的 11 个列名字面量（`涨幅/5日累计/现价/板块/评分/策略/量比/换手%/市值(亿)/连击/排名上升`）与 `web/src` 全域 9 个结论词（`建议/短名单/综合判断/买入/卖出/持仓/目标价/止盈/止损`）：`tests/test_panel_web_hygiene.py:94-112`、`tests/test_panel_web_cols.py:123-138`，**无文件白名单、行尾注释不豁免**。
3. `SCHEMA_VERSION = 2` 及三端镜像字段集（`scanner/panel_serialize.py:32/182`、`worker/src/serialize.ts:9`、`web/src/api/types.ts:77`）+ `lib/cols.ts` 的 3 个 `COLS:*` region 块与 `HOT_HIGHLIGHT_STREAK = 3`：`tests/test_panel_contract.py:43-100`、`test_panel_serialize.py:274-298`、`test_panel_web_cols.py:57-79`、`test_prior_column.py:175-185`、`worker/test/api.test.ts:254-256` 连锁锁死。

**最值得做的 2 项窗口壳打磨**
1. **图标 + 菜单收口 + 尺寸/背景色三处同色**（低风险、无守卫冲突）：造一个 `.ico` 并加进 `build.files`（`desktop/package.json:19-22`），设 `BrowserWindow.icon` 与 `build.win.icon`，`Menu.setApplicationMenu(null)`/`autoHideMenuBar:true`，把 `backgroundColor`（`main.js:273`）、`--color-bg`（`index.css:335`）、错误页底色（`main.js:247`）统一；顺带调 `minWidth/minHeight`。唯一配套是 `build.files` 与「图标现在不存在」。
2. **窗口尺寸/位置记忆到 `userData`**（`main.js:90` 已有 `app.getPath("userData")`）：与 `App.tsx:221` 已有的分栏 localStorage 持久化互补、不碰任何被锁字面量、不引入 IPC/preload；比 `frame:false` 自定义标题栏性价比高得多。

`frame:false` 排第三：技术上可做，但要同时补 preload/IPC/CSS 拖拽区/合并 `StatusBar`/打包资源，且与「关窗即退出杀 Python」的生命周期语义要小心隔离；建议 P2b 先做原型。

**「零 Python 改动」是否成立**：**成立**——只要改动限于 `rts-panel-cloud/web/src/**`（+ 可选 `web/index.html` / 新增依赖时的 `web/pnpm-workspace.yaml`）与 P2 的 `desktop/**`，且保持 `/api/*` 相对路径、`{ok,data}` 信封、hash 路由、`pnpm build` 产出 `web/dist/index.html`，`scanner/local_panel_server.py` 的静态托管与 SPA 回退（`:249-274`）已足够，`prepare.mjs:28-31` 只需**运行**不需修改，`requirements.lock`/`requirements.txt` 与三端契约都不动。破例条件见 §4 末尾反例（改 API/字段集/端口/路由形态）。
