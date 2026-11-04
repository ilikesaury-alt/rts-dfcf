"""三重屏障标签生成器（M2，2026-09-05）。

把 next_day>=7% 的单一固定水平二元标签升级为 López de Prado 三重屏障：
  - 上屏障：止盈 +NEXTDAY_HIT_THRESHOLD%（复用 next_day 靶点阈值，同源防漂移）
  - 下屏障：止损 TB_STOP_LOSS_PCT%（信号日收盘买入价下方）
  - 时间屏障：TB_HORIZON_DAYS 个交易日（对齐 cum_3d 校准口径）

输出四元组 (label, touch_date, touch_pct, ret_at_horizon) 落 triple_barrier_labels
表，供模型桶（v3）训练与持有期优化消费。旧 next_day 标签链路全部不动，本表并存。

口径（与回测/归因单源对齐）：
  - 买入价 = 信号日（T）收盘价（信号收盘后才产生，与 --buy-at close 的 cum_3d
    口径一致；T 日 bar 从 daily_kline 取，缺 T 日 bar 的样本跳过）；
  - 屏障触发用 T+1 起的 bar 的 high/low（日内先后不可判定 → 同日双触计 0，
    保守不猜）；
  - 样本口径与 backtest.load_attribution_rows 一致：excluded=0 + 同 (date, symbol)
    按 rowid 取最后一轮（跨类别同票同日也只留最后一轮，防重复加权——见
    load_attribution_rows 的 2.19x 虚高注释）。

用法：
    python -m scanner.triple_barrier              # 全量重建（幂等覆盖）
    python -m scanner.triple_barrier --days 30    # 仅重建最近 30 天样本
    python -m scanner.triple_barrier --report     # 新旧标签一致性对比
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from datetime import timedelta

from scanner.config import (
    DB_PATH,
    NEXTDAY_HIT_THRESHOLD,
    TB_HORIZON_DAYS,
    TB_STOP_LOSS_PCT,
    now_beijing,
)

if sys.platform == "win32":
    # getattr 模式（与 unified_scanner 同款）：TextIO 静态类型无 reconfigure
    _reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(_reconfigure):
        _reconfigure(encoding="utf-8")


def load_samples(conn: sqlite3.Connection, days: int = 0) -> list[sqlite3.Row]:
    """加载三重屏障样本（口径与 backtest.load_attribution_rows 一致）。

    excluded=0 + 同 (date, symbol) 按 rowid 取最后一轮（rowid 随轮次单调递增）。
    返回 Row（date/symbol/category/next_day_pct）。days>0 时仅取最近 N 天推荐。
    """
    conn.row_factory = sqlite3.Row
    # 静态 SQL + 可空 cutoff 参数（? IS NULL OR date >= ?）：无动态拼接，
    # cutoff=None 时等价全量（2026-09-05 整改：原动态拼 AND 子句触发静态
    # SQL 安全规则，且语义完全等价）
    cutoff = (now_beijing() - timedelta(days=days)).date().isoformat() if days > 0 else None
    # 字面量内联 + 参数化（占位符全由 "?" 组成，值经 tuple 传入）  # noqa: S608
    return conn.execute(
        "SELECT date, symbol, category, next_day_pct FROM ("
        "  SELECT date, symbol, category, next_day_pct,"
        "         ROW_NUMBER() OVER (PARTITION BY date, symbol ORDER BY rowid DESC) AS rn"
        "  FROM recommendations WHERE excluded = 0"
        ") WHERE rn = 1 AND (? IS NULL OR date >= ?) ORDER BY date, symbol",
        (cutoff, cutoff),
    ).fetchall()


def _load_kline_index(conn: sqlite3.Connection) -> dict[str, tuple[list[str], list[dict]]]:
    """一次性预载全部日线：{symbol: (dates 升序, bars)}，bar 为 date/high/low/close 字典。"""
    rows = conn.execute("SELECT symbol, date, high, low, close FROM daily_kline ORDER BY symbol, date").fetchall()
    out: dict[str, tuple[list[str], list[dict]]] = {}
    cur_sym: str | None = None
    dates: list[str] = []
    bars: list[dict] = []
    for sym, dt, hi, low, c in rows:
        if sym != cur_sym:
            if cur_sym is not None:
                out[cur_sym] = (dates, bars)
            cur_sym, dates, bars = sym, [], []
        dates.append(dt)
        bars.append({"date": dt, "high": hi, "low": low, "close": c})
    if cur_sym is not None:
        out[cur_sym] = (dates, bars)
    return out


def _evaluate_barrier(
    buy_price: float, future_bars: Sequence[dict], k: int
) -> tuple[int, str | None, float | None, float | None]:
    """对单样本模拟三重屏障。返回 (label, touch_date, touch_pct, ret_at_horizon)。

    - 上触：high >= buy * (1 + NEXTDAY_HIT_THRESHOLD/100)
    - 下触：low  <= buy * (1 + TB_STOP_LOSS_PCT/100)
    - 同一根 bar 双触 → label=0（日内先后不可判定，保守不猜）
    - 未触 → 时间屏障到期（第 k 根）label=0，ret_at_horizon = close/buy - 1
    - future_bars 不足 k 根 → 行情未齐，返回全 None（调用方跳过）
    """
    if len(future_bars) < k:
        return 0, None, None, None
    up_line = buy_price * (1 + NEXTDAY_HIT_THRESHOLD / 100.0)
    down_line = buy_price * (1 + TB_STOP_LOSS_PCT / 100.0)
    for bar in future_bars[:k]:
        hi = bar["high"]
        low = bar["low"]
        if hi is None or low is None:
            continue  # 脏 bar（缺 high/low）不触发，继续看下一根
        hit_up = hi >= up_line
        hit_down = low <= down_line
        if hit_up and hit_down:
            return 0, None, None, None  # 同日双触：日内先后不可判定
        if hit_up:
            return 1, bar["date"], (hi / buy_price - 1.0) * 100.0, None
        if hit_down:
            return -1, bar["date"], (low / buy_price - 1.0) * 100.0, None
    last = future_bars[k - 1]
    close_last = last["close"]
    if close_last is None or close_last <= 0:
        return 0, None, None, None
    return 0, None, None, (close_last / buy_price - 1.0) * 100.0


def build_labels(conn: sqlite3.Connection, days: int = 0) -> tuple[int, int]:
    """重建三重屏障标签（幂等）。返回 (写入数, 跳过数)。

    days>0 时只重建最近 N 天样本（DELETE 窗口后重插），其余保留；默认全量重建。
    """
    samples = load_samples(conn, days=days)
    if not samples:
        return 0, 0
    kline_idx = _load_kline_index(conn)

    payload: list[tuple] = []
    skipped = 0
    now = now_beijing().isoformat(timespec="seconds")
    for r in samples:
        rec_date, sym, cat = r["date"], r["symbol"], r["category"]
        entry = kline_idx.get(sym)
        if entry is None:
            skipped += 1
            continue
        dates, bars = entry
        # 找 T 日 bar（买价锚）与其后行情切片（二分，与 historical_rescan 同思路）
        try:
            t_idx = dates.index(rec_date)
        except ValueError:
            skipped += 1  # 信号日无行情（停牌等），线上同样拿不到买价
            continue
        buy_price = bars[t_idx]["close"]
        if buy_price is None or buy_price <= 0:
            skipped += 1
            continue
        label, touch_date, touch_pct, ret_at_h = _evaluate_barrier(buy_price, bars[t_idx + 1 :], TB_HORIZON_DAYS)
        if touch_date is None and ret_at_h is None:
            skipped += 1  # 后续行情不足 horizon（样本太新）或同日双触不可判定
            # 同日双触与行情不足都跳过？双触是有信息样本（高波动日），但 label 只能
            # 为 0 且 touch 字段缺失——保留会污染 label 分布，统一跳过由 report 计数。
            continue
        payload.append((rec_date, sym, cat, label, touch_date, touch_pct, ret_at_h, buy_price, now))

    with conn:
        # 静态 SQL + 可空 cutoff（? IS NULL OR date >= ?）：cutoff=None 等价全量清空，
        # 无动态拼接（与 load_samples 同款整改）
        cutoff = (now_beijing() - timedelta(days=days)).date().isoformat() if days > 0 else None
        conn.execute("DELETE FROM triple_barrier_labels WHERE ? IS NULL OR date >= ?", (cutoff, cutoff))
        conn.executemany(
            "INSERT OR REPLACE INTO triple_barrier_labels "
            "(date, symbol, category, label, touch_date, touch_pct, ret_at_horizon, buy_price, updated) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            payload,
        )
    return len(payload), skipped


def print_report(conn: sqlite3.Connection) -> None:
    """新旧标签一致性对比：next_day>=7% 旧标签 vs 三重屏障 label=+1。

    JOIN 必须带 category 并取最后一轮（2026-09-06 修复）：双挂票/池选会让同一
    (date,symbol) 存在多条不同 category 的推荐行，仅按 date+symbol 关联会把
    每条标签复制 2~5 份（实测 1689 → 3746，虚高 2.2 倍）。
    """
    rows = conn.execute(
        "SELECT t.category, t.label, t.ret_at_horizon, r.next_day_pct "
        "FROM triple_barrier_labels t JOIN recommendations r "
        "ON r.date = t.date AND r.symbol = t.symbol AND r.category = t.category "
        "WHERE (t.touch_date IS NOT NULL OR t.ret_at_horizon IS NOT NULL) "
        "AND r.rowid = (SELECT MAX(r2.rowid) FROM recommendations r2 "
        "               WHERE r2.date = t.date AND r2.symbol = t.symbol "
        "                 AND r2.category = t.category)"
    ).fetchall()
    if not rows:
        print("  [!] triple_barrier_labels 为空（先跑 python -m scanner.triple_barrier 生成）")
        return
    by_cat: dict[str, list[tuple]] = {}
    for cat, label, ret_h, ndp in rows:
        by_cat.setdefault(cat, []).append((label, ret_h, ndp))

    print("=" * 84)
    print(
        f"三重屏障标签一致性（上屏障=+{NEXTDAY_HIT_THRESHOLD}% / 下屏障={TB_STOP_LOSS_PCT}% / "
        f"时间={TB_HORIZON_DAYS}日，买价=信号日收盘）"
    )
    print("=" * 84)
    print(f"{'类别':<16}{'样本':>6}{'tb+1':>7}{'tb-1':>7}{'tb0':>7}{'nd hit':>9}{'一致率':>9}{'到期均收':>10}")
    for cat in sorted(by_cat):
        items = by_cat[cat]
        n = len(items)
        n_up = sum(1 for lb, _, _ in items if lb == 1)
        n_down = sum(1 for lb, _, _ in items if lb == -1)
        n_zero = n - n_up - n_down
        # 旧标签：next_day_pct >= NEXTDAY_HIT_THRESHOLD（与上游回填口径一致，>0 才有效）
        valid = [(lb, ret_h, ndp) for lb, ret_h, ndp in items if ndp is not None]
        nd_hit = sum(1 for _, _, ndp in valid if ndp >= NEXTDAY_HIT_THRESHOLD) / len(valid) * 100 if valid else 0.0
        # 一致 = 旧 hit 与 tb+1 同判定（tb 对 ndp 有效样本：label==1 ↔ ndp>=阈值）
        agree = (
            sum(1 for lb, _, ndp in valid if (lb == 1) == (ndp >= NEXTDAY_HIT_THRESHOLD)) / len(valid) * 100
            if valid
            else 0.0
        )
        rets = [ret_h for _, ret_h, _ in items if ret_h is not None]
        avg_h = sum(rets) / len(rets) if rets else 0.0
        print(f"{cat:<16}{n:>6}{n_up:>7}{n_down:>7}{n_zero:>7}{nd_hit:>8.1f}%{agree:>8.1f}%{avg_h:>9.2f}%")


def main() -> None:
    ap = argparse.ArgumentParser(description="三重屏障标签生成器（M2）")
    ap.add_argument("--days", type=int, default=0, help="仅重建最近 N 天推荐（0=全量）")
    ap.add_argument("--report", action="store_true", help="生成后输出新旧标签一致性报告")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    try:
        written, skipped = build_labels(conn, days=args.days)
        print(f"[三重屏障] 写入 {written} 条，跳过 {skipped} 条（缺买价/行情不足/同日双触）")
        if args.report:
            print_report(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
