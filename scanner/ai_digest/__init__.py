"""榜外异动段（B 段）的 **AI 转述**（手动触发，2026-11-03）。

这个包回答的问题只有一个：终端上 `榜外异动` 那几行我一眼看不出是什么 —— 让模型把
**这一段的素材**转成中文，然后我自己核对。

四条硬边界（改前必读）
----------------------
1. **只转述，不产生第二结论源**。素材由 `material.build_materials` 从
   `offboard_launch_log` 只读确定性组装；模型没有别的输入，只能把素材改写成中文。
   输出**不落库、不进任何计算路径、不进终端主循环、不进飞书**。
2. **手动触发，不轮询**。刻意不挂进 `unified_scanner` 主循环：那边 60s 一轮，
   自动出网会让「终端刷新频率」= 「模型计费频率」，且主循环是单线程串行渲染，
   一次 20s 的外呼会把扫描节拍整体拖慢。这里只有人敲命令才出网。
3. **只针对 `榜外异动`（B 段）**。B 段结构上**没有**排名/连击/复合分（那是 A 段榜单
   专属量），素材面窄、字段少，是「全段一次性转述」成本可控的唯一一段。其余三区
   （v1 池选 / 回捞 / 榜内飙升）的跨区或整区转述**未开**：整区对比式描述会直接落进
   `docs/adr/0001-ai-explain-redline.md` 修订 1 登记的「隐含建议」逃逸区。
4. **红线守卫在 `guard.py`**，与本包同源，fail-closed：命中即整段替换为拒答文案。
   它与 worker 侧 `explain/guard.ts` 是**两份实现**，`tests/test_ai_digest.py`
   用「TS 词表逐条必须在 Python 侧存在」的漂移守卫咬住，改一侧不改另一侧会红。

用法
----
    python -m scanner.ai_digest                  # 最新信号日的榜外异动 → 转述
    python -m scanner.ai_digest --date 2026-11-03
    python -m scanner.ai_digest --materials      # 只打素材，不出网（无凭据也能跑）
    python -m scanner.ai_digest --demo           # 内置样例，零库零网（自检）
    python -m scanner.ai_digest --json           # 机器可消费

凭据：`MAGPIE_BASE_URL` / `MAGPIE_API_KEY` / `MAGPIE_MODEL`（放 `.env`，勿提交）。
CLI 会调 `load_dotenv()`（与 `unified_scanner.py` 同款），缺失时只依赖真实环境变量。

退出码：0 成功 / 1 未配置凭据 / 2 上游失败 / 3 触红线被拦（拒答文案已打）。
"""

from __future__ import annotations

# 本包刻意不在 __init__ 里 re-export 子模块：`from .material import ...` 会把
# `scanner.offboard_watch` 拉进来（排序键单源），而只想用 `guard.is_redlined` 的调用方
# 不该为此付这笔导入代价。子模块各自可 import：
#     from scanner.ai_digest.guard import is_redlined
#     from scanner.ai_digest.material import build_materials, render_materials_block
