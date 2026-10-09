"""scripts/offboard_nextday.py — B 段「榜外异动」次日表现的**离线追踪报告**（只读 scanner.db）。

为什么需要这个脚本
------------------
B 段（`scanner/offboard_watch.py`）上线时把「逐日落库 + `next_day_pct` 回填」写成硬前置，
标签侧由 `run_offboard_watch` 每轮自动推进（`backfill_next_day`）—— 但**读侧一直没有**：
终端/飞书只展示当轮信号，没有任何出口回答「B 段信号次日到底表现如何」。
于是一个反直觉的数字只能靠人手翻库才能发现，而人手翻库的结果不会有人复查。

本脚本补的就是这个读侧：一条命令输出样本覆盖、分布、分层、特征分桶、**基准对照**、
以及「结论不可下」的量化理由。

用法
----
    python scripts/offboard_nextday.py              # 全部有标签样本
    python scripts/offboard_nextday.py --days 20    # 只看近 20 个信号日
    python scripts/offboard_nextday.py --tier T2    # 只看某一层
    python scripts/offboard_nextday.py --json       # 机器可消费

⚠ 本脚本**不产出结论**，只报数（与 `scripts/push_gate_impact.py` 同一纪律）
--------------------------------------------------------------------
报告里没有「好/坏」「该收紧/该放宽」「建议阈值」任何字样。理由：
  · B 段的阈值归属 `scanner/config_hot_watch.py`，改它们要走
    `python -m scanner.rule_validate` —— 而 `rule_validate` 的三个评估器
    **都看不见** `offboard_watch`（`--set` 会被可见性硬校验拦在退出码 3），
    即 B 段是验证门的盲区，靠样本外统计裁决这条路在这里**不存在**；
  · 因此 B 段唯一可用的判据就是 observe-first 的逐日表，而「够不够格下结论」
    是报告必须自己交代的事（见 MDE/配对天数段）—— 一旦这里给出建议，它就成了
    项目的第二个结论源。

口径（务必与回填写入侧一致，否则两者对不上）
----------------------------------------
`next_day_pct` = **信号日收盘 → 次一交易日收盘**的收益 %，由
`offboard_watch.backfill_next_day` 回填。入场价用收盘而非信号捕获时的盘中价，
见报告末尾「测量口径自检」的实测漂移。

⚠ 「基准对照」不是干净的市场基准
------------------------------
对照取自 `daily_kline` 的横截面（同一信号日 → 次日 的连续 bar 收益）。
`daily_kline` 的既定语义是**榜单衍生池**（榜上票为主），与 B 段的「榜外票」
是两个总体 —— 所以对照只能回答「B 段是否比榜上票池更差/更好」，
**不能**回答「B 段是否有超额」。要答后者需要全创业板横截面基准，本项目没有。
这一条也写在报告输出里，避免读数被当成市场中性收益。
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import statistics as st
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# win32 控制台默认 GBK，报告里的 ⚠ / ∞ / ≥ 都不是 GBK 字符 ⇒ 直接 print 会抛
# UnicodeEncodeError（**崩在打印中途**，前面的统计已经算完却看不到）。
# getattr 模式：sys.stdout 静态类型是 TextIO（无 reconfigure 属性），运行时的
# TextIOWrapper 才有；pythonw 下 stdout 为 None（同 unified_scanner / pool_filter_scan）。
_reconfigure = getattr(sys.stdout, "reconfigure", None)
if callable(_reconfigure):
    _reconfigure(encoding="utf-8")

DB = Path(__file__).resolve().parent.parent / "scanner.db"

# 命中口径复用系统唯一目标函数（AGENTS.md「目标函数」：次日 ≥7%）。
# 其余档位只为看清分布形状，不是新增判据。
HIT_LEVELS: tuple[float, ...] = (0.0, 3.0, 5.0, 7.0, 9.9)

# 给「按日配对 bootstrap」的最少配对天数。低于它就**不出区间**（见 paired_ci）。
# 取 3：2 天的 bootstrap 只有 3 种重采样组合，区间会退化成少数几个离散值，
# 看似有区间实则不可用；3 仍是「仅供察觉量级」的量级，不足以支撑任何阈值动作
# （项目口径要求 D ≥ 47 个交易日才能检出 10pp，见 offboard_watch.docstring）。
MIN_PAIRED_DAYS = 3

# 特征分桶的边界（用于事后重放阈值，非阈值建议）
BUCKETS: dict[str, tuple[tuple[float, float], ...]] = {
    "percent": ((-99.0, 2.0), (2.0, 3.5), (3.5, 5.0), (5.0, 99.0)),
    "vol_ratio": ((0.0, 2.0), (2.0, 3.0), (3.0, 5.0), (5.0, 10.0), (10.0, 1e9)),
    "main_pct": ((-99.0, 0.0), (0.0, 3.0), (3.0, 10.0), (10.0, 99.0)),
    "accum_5d": ((-99.0, 0.0), (0.0, 5.0), (5.0, 10.0), (10.0, 99.0)),
}
BUCKET_LABELS = {
    "percent": "信号日涨幅",
    "vol_ratio": "量比",
    "main_pct": "主力净占比",
    "accum_5d": "5日累计",
}


@dataclass
class Row:
    """`offboard_launch_log` 的一个信号行（只带报告用得到的字段）。"""

    date: str
    symbol: str
    name: str
    tier: str
    percent: float | None
    accum_5d: float | None
    vol_ratio: float | None
    main_pct: float | None
    price: float | None
    next_day_pct: float | None


@dataclass
class Summary:
    """一组样本的次日表现摘要（全部指标都由 `vals` 直接算出，不含判断）。"""

    n: int = 0
    mean: float | None = None
    median: float | None = None
    lo: float | None = None
    hi: float | None = None
    hits: dict[float, int] = field(default_factory=dict)

    @classmethod
    def of(cls, vals: list[float]) -> "Summary":
        if not vals:
            return cls()
        return cls(
            n=len(vals),
            mean=st.mean(vals),
            median=st.median(vals),
            lo=min(vals),
            hi=max(vals),
            hits={lv: sum(1 for v in vals if v >= lv) for lv in HIT_LEVELS},
        )

    def hit_pct(self, lv: float) -> float | None:
        if not self.n or lv not in self.hits:
            return None
        return self.hits[lv] / self.n * 100.0


# ── 取数 ────────────────────────────────────────────────────────────────────


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def load_rows(conn: sqlite3.Connection, days: int | None, tier: str | None) -> tuple[list[Row], list[str], list[str]]:
    """读 `offboard_launch_log` → (有标签行, 全部信号日, 尚无标签的信号日)。

    有标签与无标签**分开返回**：无标签的信号日不是「表现差」，而是「标签还没回填」
    （回填要等次一交易日的 bar，且当日信号永远无法回填）—— 报告必须能把两者区分开，
    否则「命中率」会被无标签样本的处理方式悄悄改变：算作 0% 会系统性压低命中率，
    不算则样本量虚高。任何一种混法都是错的。
    """
    if not _has_table(conn, "offboard_launch_log"):
        return [], [], []

    cols = "date, symbol, name, tier, percent, accum_5d, vol_ratio, main_pct, price, next_day_pct"
    sql = f"SELECT {cols} FROM offboard_launch_log"  # noqa: S608 - 列名是上方常量
    params: list = []
    if days:
        sql += " WHERE date IN (SELECT DISTINCT date FROM offboard_launch_log ORDER BY date DESC LIMIT ?)"
        params.append(days)
    if tier:
        sql += f"{' AND' if days else ' WHERE'} tier = ?"
        params.append(tier)

    rows = [
        Row(
            date=r[0],
            symbol=r[1],
            name=r[2] or r[1],
            tier=r[3],
            percent=r[4],
            accum_5d=r[5],
            vol_ratio=r[6],
            main_pct=r[7],
            price=r[8],
            next_day_pct=r[9],
        )
        for r in conn.execute(sql, params)
    ]
    sig_days = sorted({r.date for r in rows})
    labeled = [r for r in rows if r.next_day_pct is not None]
    unlabeled_days = sorted({r.date for r in rows if r.next_day_pct is None})
    return labeled, sig_days, unlabeled_days


def load_baseline(conn: sqlite3.Connection, sig_days: list[str]) -> dict[str, list[float]]:
    """对照总体：`daily_kline` 在同一「信号日 → 次一交易日」窗口的横截面收益。

    ⚠ 这是**榜上票池**（`daily_kline` 的既定语义），不是全创业板 —— 只能用于
    「B 段 vs 榜上票池」的相对比较，不能当作市场中性基准（见模块 docstring）。

    「次一交易日」按**连续两根 bar**取，而不是「日历日 +1」—— 与
    `offboard_watch.backfill_next_day` 的防呆 1 同口径（停牌/跳空会缺 bar，
    按日历取会把长假后的多日收益算成「次日」一天）。
    """
    if not sig_days or not _has_table(conn, "daily_kline"):
        return {}
    bars: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for sym, d, close in conn.execute(
        "SELECT symbol, date, close FROM daily_kline WHERE date >= ? ORDER BY symbol, date",
        (min(sig_days),),
    ):
        if close:
            bars[sym].append((d, close))

    wanted = set(sig_days)
    out: dict[str, list[float]] = defaultdict(list)
    for series in bars.values():
        for i in range(len(series) - 1):
            d0, c0 = series[i]
            d1, c1 = series[i + 1]
            if d0 in wanted and c1 > 0:
                out[d0].append((c1 / c0 - 1) * 100.0)
    return dict(out)


def capture_drift(conn: sqlite3.Connection, rows: list[Row]) -> list[float]:
    """信号**捕获时**的盘中价 → 信号**当日收盘**的漂移 %（测量口径自检）。

    为什么要量它：`next_day_pct` 的入场价是信号日**收盘**，而信号是盘中某刻产出的
    （`first_time` 实测落在 09~14 时）。两者之间的漂移会混进「次日收益」—— 若一只票
    在信号后当日又涨 8%，它会被记成「次日 0% = 没跟上」。漂移接近 0 才说明这条口径
    可用；偏离大则所有绝对数都要打折看。

    取数用 `fetch_date > date` 的**后一天**缓存（后一天抓回来的 signal-day bar 才是
    真实收盘；当日缓存里那根是盘中 bar，拿它比等于自己和自己比）。
    """
    if not _has_table(conn, "offboard_kline_cache"):
        return []
    out: list[float] = []
    for r in rows:
        if not r.price:
            continue
        row = conn.execute(
            "SELECT payload_json FROM offboard_kline_cache WHERE symbol=? AND fetch_date > ? "
            "ORDER BY fetch_date LIMIT 1",
            (r.symbol, r.date),
        ).fetchone()
        if not row:
            continue
        try:
            bars = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            continue
        bar = next((b for b in bars if isinstance(b, dict) and b.get("date") == r.date), None)
        close = (bar or {}).get("close")
        if close and r.price and close > 0:
            out.append((close / r.price - 1) * 100.0)
    return out


# ── 统计 ────────────────────────────────────────────────────────────────────


def paired_ci(
    a_by_day: dict[str, list[float]],
    b_by_day: dict[str, list[float]],
    iters: int = 20000,
    seed: int = 20261009,
    min_days: int = MIN_PAIRED_DAYS,
) -> tuple[float, float, float, int] | None:
    """按日配对 bootstrap：两总体在同一批交易日上的均值差 (a - b) 的 95% CI。

    配对单位是**交易日**（不是信号行）—— 同一天的横截面样本共享同一段市场行情，
    行级 bootstrap 会把相关样本当独立样本，CI 假窄。

    返回 (点估计, 下界, 上界, 参与配对的天数)；配对天数 < `min_days` 时返回 None。

    🔴 为什么配对天数不足就**不给区间**（`min_days`）
    ------------------------------------------------
    bootstrap 的重采样单位是「逐日差值」这一个序列，长度 = 配对天数。n=1 时
    所有重采样都只有那一个样本 ⇒ CI 退化成 `[d, d]`（宽度 0），**看上去**
    是「区间不含 0 ⇒ 极显著」，实则是「样本量为一、只是方差无从估计」。
    本脚本开发时真踩到：`--days 3` 打出 `均值差 -7.39%  95% CI [-7.39%, -7.39%]` ——
    一个宽度为 0 的区间若被读成显著，就是把「无信息」报成了「强证据」，
    比不给数字更有害。

    反过来，天数够多时这个区间会宽到能自己说明「样本量不足」—— 那是它该有的样子，
    所以这里**只在天数不足时压制**，不做「太宽就不给」的二次过滤：后者会把
    「证据不足」与「效应为零」混为一谈，而这两者在项目纪律下必须能被区分。
    """
    diffs = _paired_diffs(a_by_day, b_by_day)
    if len(diffs) < min_days:
        return None
    point = st.mean(diffs)
    rng = random.Random(seed)  # noqa: S311 - 数值可复现的配对 bootstrap，无安全用途
    n = len(diffs)
    boots = sorted(st.mean(rng.choices(diffs, k=n)) for _ in range(iters))
    lo, hi = boots[int(0.025 * iters)], boots[int(0.975 * iters) - 1]
    return point, lo, hi, n


def _paired_diffs(a_by_day: dict[str, list[float]], b_by_day: dict[str, list[float]]) -> list[float]:
    """逐日 (均值_a - 均值_b)，只取两侧都有非空样本的交易日。方向是 a - b。"""
    return [
        st.mean(a_by_day[d]) - st.mean(b_by_day[d])
        for d in sorted(set(a_by_day) & set(b_by_day))
        if a_by_day[d] and b_by_day[d]
    ]


def paired_day_count(a_by_day: dict[str, list[float]], b_by_day: dict[str, list[float]]) -> int:
    """可配对天数（`a` 与 `b` 都有非空样本的交易日个数）。

    与 `_paired_diffs` 同源同过滤 —— 两者若各写一套过滤条件，报告会打出
    「不出区间」却不给出到底差几天，而那个天数才是可诊断的信息。
    """
    return len(_paired_diffs(a_by_day, b_by_day))


def _fmt(v: float | None, width: int = 7, suffix: str = "%") -> str:
    return "n/a".rjust(width) if v is None else f"{v:>{width}.2f}{suffix}"


def _hit_cell(s: dict, lv: float) -> str:
    """命中单元（dict 版 `Summary.hit_pct`）：`N 只 (x.x%)`。"""
    if not s.get("n"):
        return "—"
    return f"{s['hits'].get(lv, 0)} 只 ({s['hits'].get(lv, 0) / s['n'] * 100:.1f}%)"


# ── 报告 ────────────────────────────────────────────────────────────────────


def build_report(conn: sqlite3.Connection, days: int | None, tier: str | None) -> dict:
    labeled, sig_days, unlabeled_days = load_rows(conn, days, tier)
    vals = [r.next_day_pct for r in labeled if r.next_day_pct is not None]
    overall = Summary.of(vals)

    by_tier: dict[str, Summary] = {}
    for t in ("T1", "T2"):
        if tier and t != tier:
            continue
        by_tier[t] = Summary.of([r.next_day_pct for r in labeled if r.tier == t and r.next_day_pct is not None])

    a_by_day: dict[str, list[float]] = defaultdict(list)
    for r in labeled:
        if r.next_day_pct is not None:
            a_by_day[r.date].append(r.next_day_pct)
    b_by_day = load_baseline(conn, sig_days)
    base_vals = [x for v in b_by_day.values() for x in v]

    by_day: dict[str, dict] = {}
    for d in sorted(a_by_day):
        s = Summary.of(a_by_day[d])
        base = Summary.of(b_by_day.get(d, []))
        by_day[d] = {
            "n": s.n,
            "mean": s.mean,
            "median": s.median,
            "hit7": s.hit_pct(7.0),
            "baseline_n": base.n,
            "baseline_mean": base.mean,
            "baseline_hit7": base.hit_pct(7.0),
        }

    buckets: dict[str, list[dict]] = {}
    for key, edges in BUCKETS.items():
        items = []
        for lo, hi in edges:
            sub = [
                r.next_day_pct
                for r in labeled
                if r.next_day_pct is not None and getattr(r, key) is not None and lo <= getattr(r, key) < hi
            ]
            if sub:
                s = Summary.of(sub)
                items.append(
                    {
                        "lo": lo,
                        "hi": hi,
                        "n": s.n,
                        "mean": s.mean,
                        "median": s.median,
                        "hit7": s.hit_pct(7.0),
                    }
                )
        buckets[key] = items

    drift = capture_drift(conn, labeled)
    return {
        "tier_filter": tier,
        "days_filter": days,
        "signal_days": sig_days,
        "labeled_rows": len(labeled),
        "unlabeled_days": unlabeled_days,
        "overall": vars(overall),
        "by_tier": {k: vars(v) for k, v in by_tier.items()},
        "by_day": by_day,
        "buckets": buckets,
        "baseline": {
            "source": "daily_kline 榜上票池横截面（非市场基准）",
            "n": len(base_vals),
            "mean": st.mean(base_vals) if base_vals else None,
            "median": st.median(base_vals) if base_vals else None,
            "hit7": Summary.of(base_vals).hit_pct(7.0),
            "paired_days": paired_day_count(a_by_day, b_by_day),
            "paired_diff": paired_ci(a_by_day, b_by_day),
        },
        "capture_drift": {
            "n": len(drift),
            "median": st.median(drift) if drift else None,
            "abs_ge3_pct": (sum(1 for x in drift if abs(x) >= 3) / len(drift) * 100.0) if drift else None,
            "abs_ge5_pct": (sum(1 for x in drift if abs(x) >= 5) / len(drift) * 100.0) if drift else None,
        },
    }


def print_report(rep: dict) -> None:
    sig = rep["signal_days"]
    ov = rep["overall"]
    scope = f"· {rep['tier_filter']}" if rep["tier_filter"] else ""
    print("=" * 78)
    print(f"榜外异动（B 段）次日表现追踪 {scope} — 只报数，不给结论")
    print("=" * 78)

    span = f"（{sig[0]} .. {sig[-1]}）" if sig else ""
    print(
        f"\n【样本】信号日 {len(sig)} 个{span} | 有次日标签 {rep['labeled_rows']} 行"
        f" | 口径 = 信号日收盘 → 次一交易日收盘"
    )
    if rep["unlabeled_days"]:
        print(
            f"  尚无标签的信号日（{len(rep['unlabeled_days'])} 个，标签还没回填，"
            f"不是表现差）：{' '.join(rep['unlabeled_days'])}"
        )

    print(
        f"\n【分布】n={ov['n']}  均值 {_fmt(ov['mean'])}  中位 {_fmt(ov['median'])}"
        f"  区间 {_fmt(ov['lo'])} .. {_fmt(ov['hi'])}"
    )
    print(f"{'阈值':>10}{'命中':>8}{'占比':>10}")
    for lv in HIT_LEVELS:
        c = ov["hits"].get(lv, 0)
        pct = f"{c / ov['n'] * 100:.1f}%" if ov["n"] else "n/a"
        print(f"{'>= ' + format(lv, '.1f') + '%':>10}{c:>8}{pct:>10}")

    if rep["by_tier"]:
        print("\n【按层】")
        print(f"{'层':<6}{'n':>6}{'均值':>10}{'中位':>10}{'>=3%':>17}{'>=7%':>17}")
        for t, s in rep["by_tier"].items():
            if not s["n"]:
                print(f"{t:<6}{0:>6}{'—':>10}{'—':>10}{'—':>17}{'—':>17}")
                continue
            print(
                f"{t:<6}{s['n']:>6}{_fmt(s['mean'], 9)}{_fmt(s['median'], 9)}"
                f"{_hit_cell(s, 3.0):>17}{_hit_cell(s, 7.0):>17}"
            )

    print("\n【按信号日】")
    print(f"{'date':<12}{'n':>5}{'均值':>9}{'中位':>9}{'>=7%':>8} | {'对照n':>6}{'对照均值':>9}{'对照>=7%':>9}")
    for d, s in rep["by_day"].items():
        print(
            f"{d:<12}{s['n']:>5}{_fmt(s['mean'], 8)}{_fmt(s['median'], 9)}{_fmt(s['hit7'], 7)} | "
            f"{s['baseline_n']:>6}{_fmt(s['baseline_mean'], 8)}{_fmt(s['baseline_hit7'], 9)}"
        )

    print("\n【特征分桶】信号时取值 → 次日表现（用于事后重放阈值，不含阈值建议）")
    for key, items in rep["buckets"].items():
        if not items:
            continue
        print(f"  {BUCKET_LABELS[key]}:")
        for it in items:
            hi_s = "∞" if it["hi"] >= 1e9 else format(it["hi"], ".1f")
            print(
                f"    [{format(it['lo'], '.1f'):>6},{hi_s:>6}) : n={it['n']:>4}"
                f"  均值 {_fmt(it['mean'], 8)}  >=7% {_fmt(it['hit7'], 7)}"
            )

    b = rep["baseline"]
    print(
        f"\n【对照】{b['source']}  n={b['n']}  均值 {_fmt(b['mean'])}  中位 {_fmt(b['median'])}  >=7% {_fmt(b['hit7'])}"
    )
    print("  ⚠ 榜上票池 ≠ 全创业板 ⇒ 这是相对比较，不能读成「相对市场」的超额")
    pd = b["paired_diff"]
    nd = b["paired_days"]
    if pd:
        point, lo, hi, _ = pd
        print(
            f"  按日配对 bootstrap（{nd} 个交易日配对）：B 段 − 对照 均值差 "
            f"{point:+.2f}%  95% CI [{lo:+.2f}%, {hi:+.2f}%]"
        )
        print("  （CI 跨 0 = 与对照不可区分；CI 宽度由天数决定，不是由样本行数决定）")
    else:
        print(
            f"  按日配对 bootstrap：**不出区间**（可配对 {nd} 个交易日，"
            f"需 ≥{MIN_PAIRED_DAYS}）—— 配对天数不足时区间会塌缩成宽度 0 的点，"
        )
        print("  看上去像「显著」、实则是「无信息」，比不给数字更有害")

    d = rep["capture_drift"]
    print(
        f"\n【测量口径自检】信号捕获价 → 信号日收盘 漂移  n={d['n']}"
        f"  中位 {_fmt(d['median'])}  |漂移|>=3% 占 {_fmt(d['abs_ge3_pct'])}"
        f"  |漂移|>=5% 占 {_fmt(d['abs_ge5_pct'])}"
    )
    print("  入场价用收盘而非捕获时盘中价 —— 上面的漂移小，则该口径可用")

    print("\n【为什么不给结论】B 段是 rule_validate 三个评估器的盲区（--set 会被可见性")
    print("  硬校验拦在退出码 3），样本外裁决通道不存在；改动阈值依据写在 config_hot_watch.py。")
    print("=" * 78)


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="榜外异动（B 段）次日表现追踪（只读 scanner.db）")
    ap.add_argument("--days", type=int, default=None, help="只看近 N 个信号日")
    ap.add_argument("--tier", choices=["T1", "T2"], default=None, help="只看某一层")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    if not DB.exists():
        print(f"找不到 {DB}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        if not _has_table(conn, "offboard_launch_log"):
            print("scanner.db 里没有 offboard_launch_log —— 该表自 v8 迁移（2026-09-18）才存在", file=sys.stderr)
            return 1
        rep = build_report(conn, args.days, args.tier)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print_report(rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
