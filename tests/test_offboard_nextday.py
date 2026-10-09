"""scripts/offboard_nextday.py 的守卫测试。

这个脚本是 B 段（榜外异动）次日表现的**唯一读侧**，所以守卫的不是「算得准」
（那依赖真实 DB，不在单测里做），而是三条**失效方式**：

1. **配对天数不足时必须不出 CI**（`paired_ci` 返回 None）——
   bootstrap 的重采样单位是「配对差值」这一个序列，长度 = 配对天数。n=1 时
   所有重采样都只有一个样本，CI 塌缩成宽度 0 的 `[d, d]`，打印出来像
   「区间不含 0 ⇒ 极显著」，实则是「样本量为一」。本脚本开发时真踩到过：
   `--days 3` 打出 `均值差 -7.39%  95% CI [-7.39%, -7.39%]`。
   若哪天有人把 `min_days` 调成 0，本文件会红。
2. **配对天数够时必须给区间，且区间包含点估计** —— 反向守卫，防止「永远不给
   数字」这种同样不可接受的行为（那会让读者以为无差异，而非样本不足）。
3. **`--tier` / `--days` 过滤真的生效** —— 空断言是这个项目反复踩的坑
   （`onboard_anomaly` 的 fixture 塞了生产拿不到的字段，守卫绿灯线上空转）；
   这里显式断言过滤后**只**剩目标层，且总行数确实变少。

另附一条「报告不得含结论措辞」的结构性守卫：脚本的 docstring 明确声明
「只报数、不给建议」，若哪天有人在输出里写进「建议 / 该收紧 / 该放宽」，
本文件会红 —— 那正是 AGENTS.md 反对的「第二个结论源」。
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "offboard_nextday.py"


def _load():
    spec = importlib.util.spec_from_file_location("offboard_nextday", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["offboard_nextday"] = mod
    spec.loader.exec_module(mod)
    return mod


ob = _load()


# ── 1. 配对天数不足 → 不出区间 ──────────────────────────────────────────────


def test_paired_ci_refuses_to_report_when_days_below_min():
    """n=1 时 CI 会塌缩成宽度 0 —— 必须返回 None，而不是把点估计当区间报出去。"""
    a = {"2026-10-08": [-1.0, -2.0]}
    b = {"2026-10-08": [5.0, 6.0]}
    assert ob.paired_ci(a, b) is None
    assert ob.paired_ci(a, b, min_days=2) is None
    # 只配对 2 天时同样拒绝（默认门槛 3）
    a2 = {"d1": [-1.0], "d2": [-2.0]}
    b2 = {"d1": [5.0], "d2": [6.0]}
    assert ob.paired_ci(a2, b2) is None
    assert ob.paired_day_count(a2, b2) == 2


def test_paired_ci_no_overlap_days_is_not_a_finding():
    """两个总体的信号日完全不重叠时不得给区间（配对 0 天 = 无从比较）。"""
    assert ob.paired_ci({"d1": [1.0]}, {"d2": [1.0]}) is None
    assert ob.paired_day_count({"d1": [1.0]}, {"d2": [1.0]}) == 0


def test_paired_day_count_ignores_empty_sides():
    """某侧当天为空样本（该日无信号 / 对照无 bar）时不计入配对天数。"""
    a = {"d1": [1.0], "d2": [], "d3": [2.0]}
    b = {"d1": [1.0], "d2": [1.0], "d3": []}
    assert ob.paired_day_count(a, b) == 1


# ── 2. 配对天数够 → 必须给区间，且区间含点估计 ──────────────────────────────


def test_paired_ci_gives_bracketing_interval_when_enough_days():
    """反向守卫：不能「永远不给数字」—— 天数够时必须给区间且 lo ≤ point ≤ hi。"""
    # a_d = -d, b_d = 2d ⇒ 逐日差值 = -3d，d=0..5 ⇒ [0,-3,-6,-9,-12,-15]，均值 -7.5
    a = {f"d{i}": [-1.0 * i] for i in range(6)}
    b = {f"d{i}": [2.0 * i] for i in range(6)}
    out = ob.paired_ci(a, b)
    assert out is not None, "配对 6 天却不出区间 —— 门槛设反了？"
    point, lo, hi, nd = out
    assert nd == 6
    assert lo <= point <= hi
    # 显式断言配对方向是 a - b 而非 b - a（反了会得到 +7.5，本守卫就是防这个）
    assert point == -7.5
    assert lo < point < hi, "6 个不同差值却给出零宽区间 —— bootstrap 退化了"


def test_paired_ci_is_deterministic():
    """固定 seed ⇒ 同一输入同一区间。否则报告数字每次跑都变，无法复查。"""
    a = {f"d{i}": [float(i), float(-i)] for i in range(8)}
    b = {f"d{i}": [1.0, -1.0] for i in range(8)}
    assert ob.paired_ci(a, b) == ob.paired_ci(a, b)


# ── 3. 过滤真的生效（避免空断言）────────────────────────────────────────────


def _memdb() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE offboard_launch_log ("
        "date TEXT, symbol TEXT, name TEXT, tier TEXT, percent REAL, accum_5d REAL,"
        "vol_ratio REAL, main_pct REAL, price REAL, next_day_pct REAL)"
    )
    return conn


def test_load_rows_tier_and_days_filters_take_effect():
    """显式断言过滤后只剩目标层，且总行数变少 —— 不写成「调用没报错」那种空断言。"""
    conn = _memdb()
    rows = [
        ("2026-10-08", "SZ300001", "甲", "T1", 2.0, 1.0, 2.0, 1.0, 10.0, -1.0),
        ("2026-10-08", "SZ300002", "乙", "T2", 4.0, 1.0, 2.0, 1.0, 10.0, 8.0),
        ("2026-10-09", "SZ300003", "丙", "T1", 2.5, 1.0, 2.0, 1.0, 10.0, None),
    ]
    conn.executemany(
        "INSERT INTO offboard_launch_log (date,symbol,name,tier,percent,accum_5d,"
        "vol_ratio,main_pct,price,next_day_pct) VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()

    all_rows, days, unlabeled = ob.load_rows(conn, None, None)
    assert len(all_rows) == 2, "有标签行应为 2（10-09 那行 next_day_pct 为 NULL）"
    assert days == ["2026-10-08", "2026-10-09"]
    assert unlabeled == ["2026-10-09"]

    t2_rows, _, _ = ob.load_rows(conn, None, "T2")
    assert len(t2_rows) == 1, "tier 过滤未生效"
    assert t2_rows[0].symbol == "SZ300002"

    one_day, one_days, _ = ob.load_rows(conn, 1, None)
    assert one_days == ["2026-10-09"], "--days 应只取最近 1 个信号日"
    assert one_day == [], "10-09 无标签 ⇒ 有标签行应为空"
    conn.close()


def test_unlabeled_rows_are_never_counted_as_performance():
    """无标签行（next_day_pct 为 NULL）不得混进分布 —— 否则命中率被系统性低估。

    反向也不成立：不能把无标签行当成 0% —— 那是把「还没回填」当成「没涨」。
    """
    conn = _memdb()
    conn.executemany(
        "INSERT INTO offboard_launch_log (date,symbol,name,tier,percent,accum_5d,"
        "vol_ratio,main_pct,price,next_day_pct) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("2026-10-08", "SZ300001", "甲", "T1", 2.0, 1.0, 2.0, 1.0, 10.0, -5.0),
            ("2026-10-08", "SZ300002", "乙", "T1", 2.0, 1.0, 2.0, 1.0, 10.0, None),
        ],
    )
    conn.commit()
    labeled, _, unlabeled = ob.load_rows(conn, None, None)
    assert len(labeled) == 1
    assert labeled[0].next_day_pct == -5.0
    assert unlabeled == ["2026-10-08"]
    rep = ob.build_report(conn, None, None)
    assert rep["overall"]["n"] == 1, "无标签行混进了分布统计"
    assert rep["overall"]["mean"] == -5.0
    conn.close()


def test_summary_of_empty_sample_is_all_none_not_crash():
    """空样本不能抛异常（`Summary.of([])` 是分桶/分层在零产出时的常态）。

    `hits` 空字典而非零填充是刻意的：消费方一律用 `.get(lv, 0)` 兜底，
    零填充会让「零样本」与「命中 0 只」在数据结构上无法区分 —— 那正是
    「没数据」被读成「表现差」的起点。
    """
    s = ob.Summary.of([])
    assert s.n == 0
    assert s.mean is None and s.median is None
    assert s.hits == {}
    assert s.hit_pct(7.0) is None


def test_summary_hit_counts_are_monotone_in_threshold():
    """更高阈值的命中数不可能更多 —— 命中率的单调性是报告正确性的底线。"""
    vals = [-3.0, -1.0, 0.5, 3.5, 7.2, 9.95]
    s = ob.Summary.of(vals)
    counts = [s.hits[lv] for lv in ob.HIT_LEVELS]
    assert counts == sorted(counts, reverse=True), f"命中数非单调递减：{counts}"
    assert s.hit_pct(7.0) == 2 / 6 * 100


# ── 4. 报告不得含结论措辞（AGENTS.md：不要第二个结论源）─────────────────────


def test_report_contains_no_verdict_wording():
    """**打印出去**的内容里不得出现可执行的结论措辞。

    本脚本存在的意义是**只报数**：B 段的阈值归属 `config_hot_watch.py`，
    改动依据必须走该文件 docstring 里写明的流程，而非由一个统计脚本顺口给出。

    两个设计要点（都来自本项目吃过的亏）：
    · 作用域限定在 `print_report` 的源码 —— 用 `inspect.getsource` 取**真正会被
      print 的那段**，而不是全文扫描：模块 docstring 里「不含阈值建议」这类
      **免责声明**本身就该含「建议」二字，全文扫描会把守卫变成噪音（本次首版
      就是这么写错的：docstring 被判红，而它是正确的）。
    · 禁用词用「动作词 + 动词」而非裸「建议」：同理，免责声明无法与建议区分。
    """
    import inspect
    import re

    src = inspect.getsource(ob.print_report)
    patterns = (
        r"建议[^，。\n]{0,4}(收紧|放宽|提高|降低|上调|下调|删除|关闭|关掉|调参)",
        r"(该|应该)(收紧|放宽|提高|降低|上调|下调)",
        r"推荐(买入|加仓|上)",
        r"(可以|值得)(上|买|加仓)",
        r"胜率(很高|不错|良好)",
    )
    for pat in patterns:
        m = re.search(pat, src)
        assert m is None, f"报告输出里出现结论措辞：{m.group(0)!r}（{pat}）"

    # 免责声明必须仍在（它是「不给结论」的实现方式之一，不是结论）
    assert "不含阈值建议" in src or "不含结论" in src or "不给结论" in src


def test_module_docstring_declares_no_conclusion_discipline():
    """守住「只报数」的**声明**本身 —— 有人删掉它时本文件会红。"""
    doc = ob.__doc__ or ""
    assert "不产出结论" in doc
    assert "rule_validate" in doc, "必须写明 B 段是 rule_validate 的盲区（不给结论的根因）"
