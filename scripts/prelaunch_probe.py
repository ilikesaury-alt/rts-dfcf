"""scripts/prelaunch_probe.py — 启动前形态的**样本外可提取性探针**（只读 scanner.db，不联网）。

## 回答什么问题

给定一个「T-1 收盘时的形态条件」，它在**次日**到底还有没有区分度？以及这个
区分度是**真信号**还是**在 train 窗拟合出来的噪声**？

这个问题每次有人拿着"我盯到一只票启动前长这样"时被重新问一遍。本脚本把
答案变成一条命令，避免在单案例上过度解读（见文末「本脚本存在的理由」）。

## 方法（刻意做成不可能自我欺骗的三段式）

1. **总体**：创业板全票的每个 T-1 收盘点（不是"推荐过的票"，不是"涨停票"）。
   n≈3.4 万，比"涨停复盘 20 只"高三个数量级。
2. **时间切分**：`--split` 之前的日期为 TRAIN，之后为 TEST。条件只在 TRAIN 上看，
   同一条件原封不动到 TEST 上复核。**只看 TRAIN 会系统性高估** —— 本项目实测
   见过 TRAIN +5.0pp → TEST +0.5pp 的塌方。
3. **MDE 门**：按 AGENTS.md 的口径，MDE 由「观测到的配对差值」的 bootstrap 标准误
   得出（改动无效果时退化为 0）。这里对每个条件做**按日配对** bootstrap：
   先把每个交易日的（条件组均值 − 基准组均值）聚成日序列，再对日序列 bootstrap。
   报告 `MDE = 2.8 × SE`（项目实测的 2.3pp 量级取整），判定看 TEST 的 Δ 是否 ≥ MDE。

## 判定与退出码（与 scanner/rule_validate.py 对齐）

    0  TEST 窗 Δ ≥ MDE 且方向为正 —— 样本外支持
    1  证据不足（TEST 窗翻转太少 / |Δ| < MDE / 两窗方向不一致）
    2  TEST 窗 Δ ≤ −MDE —— 样本外显著变差（该条件有害）

## 用法

    python scripts/prelaunch_probe.py                    # 默认切分 + 全部候选
    python scripts/prelaunch_probe.py --split 2026-08-01
    python scripts/prelaunch_probe.py --target 301190    # 只看某票的 T-1 落在哪一档
    python scripts/prelaunch_probe.py --json
    python scripts/prelaunch_probe.py --min-n 20        # 抬高单条件的最小 n（默认 30）

## ⚠ 本脚本**不产出**「该怎么做」的结论

它只如实报数。阈值改动另受 `python -m scanner.rule_validate` 门约束
（三个评估器都看不见展示层形态标签，形态类改动只能靠本脚本 + 人工判断）。

## 本脚本存在的理由（2026-09-29）

拿 SZ301190（善水科技）2026-09-28 的 20cm 启动做了一次单案例复盘：它的 T-1
形态（阴线回抽 + 缩量 + 逼近20日高 + 5日涨 12%）在 TRAIN 窗 hit≥7% 13.0%、
TEST 窗 6.2%，基准 5.7% —— 即**这个形态本身劣于或等于基准**。该票的次日 +20%
不是形态给的，是 09-28 盘中放量封板给的。n=1 的形态观察不构成规则依据。
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import statistics as st
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scanner.config import DB_PATH, now_beijing  # noqa: E402
from scanner.trading_session import is_trading_day  # noqa: E402

#: 按日配对 bootstrap 下判定「TEST 窗 Δ 显著」所需的最小倍数。
#: 项目实测 MDE ≈ 2.3pp（rule_validate 文档记载），此处取 2.8 略严。
MDE_K = 2.8

#: bootstrap 总预算（次）。重采样次数按样本量自适应封顶：基准组 n≈3.4 万时，
#: 固定次数逐次 choice 要跑上亿次，脚本会卡死；CI 宽度对 500~2000 次已收敛。
BOOT_BUDGET = 2_000_000

#: 目标标签：次日 ≥ +7%（项目唯一口径，见 AGENTS.md「目标函数」）。
HIT_PCT = 7.0

#: 20 日窗口最少 bar 数。少于此则 MA20 / pos20 / bias20 不可信，直接剔除该事件。
MIN_BARS = 21

EXIT_OK, EXIT_INSUFFICIENT, EXIT_WORSE = 0, 1, 2


# ── 事件特征 ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Event:
    symbol: str
    date: str
    close: float
    accum5: float  # T-1 前 5 日累计涨幅（含 T-1）
    bias20: float  # T-1 收盘相对 MA20 的乖离
    pos20: float  # T-1 收盘在 20 日高低区间的位置 %
    near_hi: float  # T-1 收盘 / 20 日最高 × 100
    t1_volratio: float  # T-1 量 / 前 5 日均量
    t1_pct: float  # T-1 涨幅
    up10: int  # 近 10 日阳线数
    two_probe: bool  # 近 10 日内 ≥2 根涨幅 ≥6%（二次试盘）
    f1_close: float | None  # 次日收盘 / T-1 收盘 − 1（%）
    f1_open: float | None  # 次日收盘 / 次日开盘 − 1（%），可执行口径


def build_events(conn: sqlite3.Connection) -> list[Event]:
    """把 daily_kline 展开成「每个 T-1 收盘点」一个事件。

    fail-closed：MA20 窗口不足、非正价格、缺次日 bar 的点一律不产出事件 ——
    与 `_breakout_structure_ok` 同纪律，宁可少样本不给假数据。
    """
    raw: dict[str, list[tuple]] = defaultdict(list)
    for sym, dt, opn, close, high, vol, pct in conn.execute(
        "SELECT symbol, date, open, close, high, volume, percent FROM daily_kline ORDER BY symbol, date"
    ):
        try:
            o, c, h, v, p = float(opn), float(close), float(high), float(vol), float(pct)
        except (TypeError, ValueError):
            continue
        if c <= 0 or h <= 0 or v <= 0 or o <= 0:
            continue
        raw[sym].append((dt, o, c, h, v, p))

    events: list[Event] = []
    for sym, bars in raw.items():
        for i in range(MIN_BARS - 1, len(bars) - 1):
            dt, _o, close, _h, _v, t1_pct = bars[i]
            window = bars[i - 20 : i]
            closes = [b[2] for b in window]
            hi20 = max(b[3] for b in window)
            ma20 = sum(closes) / 20
            if hi20 <= 0 or ma20 <= 0:
                continue
            prev_vols = [b[4] for b in bars[i - 5 : i]]
            mean_vol = sum(prev_vols) / len(prev_vols)
            if mean_vol <= 0:
                continue
            nxt_open, nxt_close = bars[i + 1][1], bars[i + 1][2]
            if nxt_open <= 0:
                continue
            events.append(
                Event(
                    symbol=sym,
                    date=dt,
                    close=close,
                    accum5=(close / bars[i - 5][2] - 1) * 100,
                    bias20=(close / ma20 - 1) * 100,
                    pos20=(close - min(closes)) / (hi20 - min(closes)) * 100 if hi20 > min(closes) else 50.0,
                    near_hi=close / hi20 * 100,
                    t1_volratio=bars[i][4] / mean_vol,
                    t1_pct=t1_pct,
                    up10=sum(1 for b in bars[i - 10 : i] if b[5] > 0),
                    two_probe=sum(1 for b in bars[i - 12 : i - 2] if b[5] >= 6) >= 2,
                    f1_close=(nxt_close / close - 1) * 100,
                    f1_open=(nxt_close / nxt_open - 1) * 100,
                )
            )
    return events


# ── 候选条件（全部是「T-1 收盘即可复算」的量，无未来函数）──────────────────
@dataclass(frozen=True)
class Cond:
    key: str
    desc: str
    fn: Callable[[Event], bool]


CONDS: tuple[Cond, ...] = (
    Cond("baseline", "基准：全部 T-1 收盘点", lambda e: True),
    Cond("pullback", "T-1 阴线回抽（涨幅<0）", lambda e: e.t1_pct < 0),
    Cond(
        "pb_shrink",
        "阴线回抽 + T-1 缩量(≤0.9×前5均量)",
        lambda e: e.t1_pct < 0 and e.t1_volratio <= 0.9,
    ),
    Cond(
        "pb_shrink_nearhi",
        "… + 逼近20日高(≥92%)",
        lambda e: e.t1_pct < 0 and e.t1_volratio <= 0.9 and e.near_hi >= 92,
    ),
    Cond(
        "sl_goodwin",
        "【善水型】阴线回抽+缩量+近高+5日涨>8%",
        lambda e: e.t1_pct < 0 and e.t1_volratio <= 1.0 and e.near_hi >= 92 and e.accum5 > 8,
    ),
    Cond(
        "sl_probe",
        "【善水型+】…+二次试盘(近10日≥2根≥6%)",
        lambda e: e.t1_pct < 0 and e.t1_volratio <= 1.0 and e.near_hi >= 92 and e.accum5 > 8 and e.two_probe,
    ),
    Cond(
        "acc_b5",
        "前5日累计 ≤ +5%（⚡ 蓄势口径）",
        lambda e: e.accum5 <= 5.0,
    ),
    Cond(
        "acc_b5_pb10",
        "前5日累计 ≤ +5% + 回撤至20日高下 8~18%（= ⚡ 结构门）",
        lambda e: e.accum5 <= 5.0 and -18.0 <= (e.near_hi - 100) <= -8.0,
    ),
    Cond("bias_mid", "bias20 ∈ (0%,15%]（善水所在档）", lambda e: 0 < e.bias20 <= 15),
    Cond("near_hi", "逼近20日高(≥92%)", lambda e: e.near_hi >= 92),
    Cond("r5_8", "5日累计 > +8%", lambda e: e.accum5 > 8),
    Cond("up10_6", "近10日阳线 ≥6", lambda e: e.up10 >= 6),
)


# ── 统计 ────────────────────────────────────────────────────────────────────
def _bootstrap_hit_ci(vals: list[float], n_boot: int, seed: int) -> tuple[float, float]:
    """hit≥7% 比例的 95% CI（对 iid 样本 bootstrap，仅作量级参考）。

    次数按样本量自适应封顶（BOOT_BUDGET）：基准组 n≈3.4 万时，固定 n_boot 次重采样
    要跑上亿次抽样，整个脚本会卡死。CI 宽度对 500~2000 次已完全收敛，样本越大
    需要的次数越少。
    """
    if not vals:
        return (float("nan"), float("nan"))
    hits = [1.0 if v >= HIT_PCT else 0.0 for v in vals]
    n = len(hits)
    eff = max(500, min(n_boot, BOOT_BUDGET // max(1, n)))
    # bootstrap 只需可复现的伪随机数，非密码学用途
    rng = random.Random(seed)  # noqa: S311
    mean = sum(hits) / n
    # 用「二项抽样」代替逐次 choice：同分布，O(eff) 而非 O(eff×n)
    boots = sorted(rng.binomialvariate(n, mean) / n for _ in range(eff))
    return (boots[int(0.025 * eff)], boots[int(0.975 * eff)])


def _paired_daily_delta(sel: list[Event], base: list[Event], key: str) -> tuple[float, float, float, int]:
    """按日配对：逐交易日算 (sel组均值 − base组均值)，返回 (Δ, SE, 翻转天数, 有效日数)。

    SE 取**日均值的标准误** `sd(deltas)/√n_days`：日序列长度只有几十，直接用解析
    标准误即可，不必 bootstrap（结果与 bootstrap 同阶，且少一个随机源）。
    有效日数 < 5 时 SE 不可估，返回 nan —— 按 AGENTS.md，MDE 不可估就是不可估，
    不得报 0（报 0 会让任何改动都「通过」）。
    """
    f = (lambda e: e.f1_close) if key == "f1_close" else (lambda e: e.f1_open)
    by_day_sel: dict[str, list[float]] = defaultdict(list)
    by_day_base: dict[str, list[float]] = defaultdict(list)
    for e in sel:
        v = f(e)
        if v is not None:
            by_day_sel[e.date].append(v)
    for e in base:
        v = f(e)
        if v is not None:
            by_day_base[e.date].append(v)

    days = [d for d in by_day_sel if d in by_day_base]
    if len(days) < 5:
        return (float("nan"), float("nan"), float("nan"), len(days))
    deltas = [sum(by_day_sel[d]) / len(by_day_sel[d]) - sum(by_day_base[d]) / len(by_day_base[d]) for d in days]
    delta = sum(deltas) / len(deltas)
    # 翻转天数 = 该日两组均值真的分开了（方向与总体一致）
    flipped = sum(1 for x in deltas if abs(x) > 1e-9)
    n = len(deltas)
    var = sum((x - delta) ** 2 for x in deltas) / (n - 1)
    se = (var / n) ** 0.5
    return (delta, se, float(flipped), n)


def _cell(value: float | None, width: int, suffix: str = "") -> str:
    """统一表格单元格的 nan 处理 —— MDE 不可估时必须显示 n/a，不得显示 0。"""
    if value is None:
        return f"{'n/a':>{width}}"
    try:
        fv = float(value)
    except (TypeError, ValueError):
        return f"{'n/a':>{width}}"
    if fv != fv:
        return f"{'n/a':>{width}}"
    return f"{fv:{width}.2f}{suffix}"


def evaluate(events: list[Event], split: str, min_n: int, n_boot: int) -> list[dict]:
    train = [e for e in events if e.date < split]
    test = [e for e in events if e.date >= split]
    rows: list[dict] = []
    for cond in CONDS:
        sel_tr = [e for e in train if cond.fn(e)]
        sel_te = [e for e in test if cond.fn(e)]
        row: dict = {
            "key": cond.key,
            "desc": cond.desc,
            "train_n": len(sel_tr),
            "test_n": len(sel_te),
            "min_n": min_n,
        }
        for window, sel in (("train", sel_tr), ("test", sel_te)):
            vals = [e.f1_close for e in sel if e.f1_close is not None]
            if not vals:
                row[f"{window}_hit"] = None
                continue
            lo, hi = _bootstrap_hit_ci(vals, n_boot, seed=7)
            row[f"{window}_hit"] = sum(1 for v in vals if v >= HIT_PCT) / len(vals) * 100
            row[f"{window}_mean"] = st.mean(vals)
            row[f"{window}_median"] = st.median(vals)
            row[f"{window}_ci"] = [lo, hi]
        for window, sel, base in (("train", sel_tr, train), ("test", sel_te, test)):
            if cond.key == "baseline":
                continue  # 基准与自身配对，Δ 恒为 0
            d, se, flip, ndays = _paired_daily_delta(sel, base, "f1_close")
            row[f"{window}_delta"] = d
            row[f"{window}_mde"] = se * MDE_K
            row[f"{window}_flip"] = flip
            row[f"{window}_days"] = ndays
        rows.append(row)
    return rows


def _num(row: dict, key: str) -> float | None:
    """取 row 里的数值，缺失或 nan 一律归 None（调用方必须显式处理 None）。"""
    v = row.get(key)
    if v is None:
        return None
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return None
    return None if fv != fv else fv


def verdict(row: dict) -> tuple[int, str]:
    """按 AGENTS.md 口径判 TEST 窗。"""
    if row["key"] == "baseline":
        return (EXIT_OK, "基准行，不参与判定")
    min_n = _num(row, "min_n")
    test_n = _num(row, "test_n")
    if min_n is None or test_n is None:
        return (EXIT_INSUFFICIENT, "row 缺 min_n/test_n")
    if test_n < min_n:
        return (EXIT_INSUFFICIENT, f"TEST n={int(test_n)} < min_n={int(min_n)}")
    d = _num(row, "test_delta")
    mde = _num(row, "test_mde")
    if d is None or mde is None:
        return (EXIT_INSUFFICIENT, "TEST 配对 Δ 或 MDE 不可估（有效交易日 <5）")
    if d >= mde:
        return (EXIT_OK, f"Δ={d:+.2f}pp ≥ MDE={mde:.2f}pp")
    if d <= -mde:
        return (EXIT_WORSE, f"Δ={d:+.2f}pp ≤ −MDE={mde:.2f}pp（该条件有害）")
    tr = _num(row, "train_delta")
    sign_flip = tr is not None and tr * d < 0
    tail = "；TRAIN/TEST 方向相反，疑似拟合" if sign_flip else ""
    return (EXIT_INSUFFICIENT, f"|Δ|={abs(d):.2f}pp < MDE={mde:.2f}pp{tail}")


# ── 数据体检 ────────────────────────────────────────────────────────────────
def data_health(conn: sqlite3.Connection) -> list[str]:
    """查 daily_kline 里的**整日空洞**——缺口会静默污染所有 20 日窗口。"""
    problems: list[str] = []
    rows = conn.execute(
        "SELECT DISTINCT date FROM daily_kline WHERE date >= ? ORDER BY date",
        ((now_beijing().date() - timedelta(days=45)).isoformat(),),
    ).fetchall()
    have = {r[0] for r in rows}
    cursor = date.fromisoformat(min(have)) if have else now_beijing().date()
    end = now_beijing().date()
    while cursor <= end:
        if is_trading_day(cursor) and cursor.isoformat() not in have:
            n = conn.execute(
                "SELECT COUNT(DISTINCT symbol) FROM daily_kline WHERE date < ?",
                (cursor.isoformat(),),
            ).fetchone()[0]
            problems.append(
                f"{cursor.isoformat()} 是交易日但 daily_kline 无任何 bar"
                f"（当日应有 ≤{n} 只票）→ 该日之后的 20 日窗口全部少一根，"
                f"跑 python backfill_kline.py（收盘后）补全"
            )
        cursor += timedelta(days=1)
    return problems


# ── 单票对照 ────────────────────────────────────────────────────────────────
def show_target(events: list[Event], target: str) -> list[str]:
    key = target.upper().replace("SZ", "").replace("SH", "").replace("BJ", "")
    out: list[str] = []
    hits = [e for e in events if key in e.symbol]
    if not hits:
        return [f"daily_kline 里没有 {target} 的 K 线"]
    for e in hits[-6:]:
        out.append(
            f"  {e.symbol} T-1={e.date} 收{e.close:7.2f} | "
            f"5日累{e.accum5:+6.2f}% bias20 {e.bias20:+6.2f}% 近高{e.near_hi:5.1f}% | "
            f"T-1量比 {e.t1_volratio:.2f}x ({e.t1_pct:+.2f}%) 二次试盘={e.two_probe} | "
            f"次日 {e.f1_close:+.2f}%"
        )
        for cond in CONDS:
            if cond.key != "baseline" and cond.fn(e):
                out.append(f"      命中条件: {cond.key}  {cond.desc}")
    return out


# ── 报告 ────────────────────────────────────────────────────────────────────
def render(rows: list[dict], health: list[str], split: str, n_events: int, target: list[str]) -> str:
    L: list[str] = []
    L.append("=" * 78)
    L.append("  启动前形态 · 样本外可提取性探针")
    L.append(f"  事件 n={n_events}  切分 split={split}  目标=次日≥{HIT_PCT:.0f}%  MDE倍数={MDE_K}")
    L.append("=" * 78)

    if health:
        L.append("\n【数据体检】⚠ 发现 daily_kline 整日空洞，结论建立在此之上，务必先补全：")
        for p in health:
            L.append(f"  ⚠ {p}")
    else:
        L.append("\n【数据体检】✓ 近 45 日无整日空洞")

    head = (
        f"{'条件':<34}{'TRAIN n':>8}{'hit%':>7}{'Δpp':>8}{'MDE':>8} │{'TEST n':>8}{'hit%':>7}{'Δpp':>8}{'MDE':>8}  判定"
    )
    L.append("")
    L.append(head)
    L.append("─" * 104)
    for r in rows:
        code, why = verdict(r)
        mark = {EXIT_OK: "✓支持", EXIT_INSUFFICIENT: "·不足", EXIT_WORSE: "✗变差"}[code]
        if r["key"] == "baseline":
            mark = "  基准"
        L.append(
            f"{r['desc'][:34]:<34}"
            f"{r['train_n']:>8}{_cell(r.get('train_hit'), 7, '%')}"
            f"{_cell(r.get('train_delta'), 8)}{_cell(r.get('train_mde'), 8)} │"
            f"{r['test_n']:>8}{_cell(r.get('test_hit'), 7, '%')}"
            f"{_cell(r.get('test_delta'), 8)}{_cell(r.get('test_mde'), 8)}  {mark}"
        )
        L.append(f"{'':<34}{'':>8}{'':>7}{'':>8}{'':>8} │{'':>8}{'':>7}{'':>8}{'':>8}  └ {why}")

    if target:
        L.append("\n【单票 T-1 对照】")
        L.extend(target)

    L.append("\n" + "─" * 100)
    L.append("  读法：只有 TEST 栏 Δ ≥ MDE 才算「样本外支持」；TRAIN 好看而 TEST 塌回基准 =")
    L.append("        在 train 窗拟合出来的噪声。本脚本不产出「该怎么做」的结论。")
    return "\n".join(L)


def main() -> int:
    # win32 控制台默认 GBK，⚠/✓ 等字符会抛 UnicodeEncodeError（与 label_audit.py 同因）
    _reconf = getattr(sys.stdout, "reconfigure", None)
    if callable(_reconf):
        _reconf(encoding="utf-8")
    ap = argparse.ArgumentParser(description="启动前形态的样本外可提取性探针（只读，不联网）")
    ap.add_argument("--split", default="2026-08-01", help="TRAIN/TEST 切分日（含当日归 TEST）")
    ap.add_argument("--min-n", type=int, default=30, help="单个条件在 TEST 窗的最小 n")
    ap.add_argument("--boot", type=int, default=2000, help="bootstrap 次数")
    ap.add_argument("--target", help="对照某票，如 301190 / SZ301190")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    try:
        health = data_health(conn)
        events = build_events(conn)
    finally:
        conn.close()
    events.sort(key=lambda e: (e.date, e.symbol))
    rows = evaluate(events, args.split, args.min_n, args.boot)
    target = show_target(events, args.target) if args.target else []

    if args.json:
        print(
            json.dumps(
                {
                    "events": len(events),
                    "split": args.split,
                    "data_health": health,
                    "rows": rows,
                    "target": target,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(render(rows, health, args.split, len(events), target))

    judged = [verdict(r)[0] for r in rows if r["key"] != "baseline"]
    if not judged or all(c == EXIT_INSUFFICIENT for c in judged):
        return EXIT_INSUFFICIENT
    return EXIT_WORSE if EXIT_WORSE in judged and EXIT_OK not in judged else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
