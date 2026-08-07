"""组合级回测引擎。

与 scanner.backtest（信号归因型：胜率 / rank-IC）互补：本模块模拟"真实资金滚动"，
给出组合层面的可执行性指标，回答"按此策略实盘资金跑下来收益 / 回撤 / 风险调整收益多少"。

模拟口径（均已在注释中标明，便于审计与后续调参）：
- 信号来源：recommendations 表（按 category 过滤，排除已废弃类别）。
- 买入：信号日(rec_date) + buy_delay 个交易日的**开盘价**（默认 1，避开前视、
        且天然满足 A 股 T+1 不能当日卖的约束）。
- 持有：hold_days 个交易日（默认 3，匹配用户「持有 2-3 天卖出」的真实操作），
        在到期日**收盘**卖出。
- 成本（A 股真实费率）：
    * 佣金：成交额的万 2.5，单笔最低 5 元（买/卖双向）。
    * 印花税：仅卖出，成交额的 0.05%（2023-08 减半后）。
    * 滑点：默认双边各 0.1%（买入加、卖出减），可配置。
- 仓位：每个买入槽位初始投入 initial_capital / max_positions（等权），
        总持仓数 ≤ max_positions（满仓时全部等权，未满仓留现金缓冲）。
- 基准：同等条件下「买入全部当日信号（无评分筛选）」等权组合，用于隔离
        "评分是否带来超额收益"。
- 多策略对比：对每个现役 category 单独跑一遍，再与「综合(all)」「基准」并列比较。

已知局限（已在结论文档 quant-library-comparison.md #2 对齐）：
- 无真实指数（沪深300 / 创业板指）数据，基准为样本内代理，非市场基准；
  若未来注入指数日线，仅需替换 `_benchmark` 信号集，其余不变。
- 简化：允许非整数股（组合评估级，未做 100 股整手约束）；买入日缺开盘数据则跳过该信号。

本模块不进入实时扫描路径，仅通过 `python -m scanner.portfolio_backtest` 运行。
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from scanner.config import DB_PATH
from scanner.trading_session import is_trading_day


# ── 组合回测使用的现役策略类别（排除已废弃的 old_face / early_momentum / pullback 离线）──
PORTFOLIO_CATEGORIES: set[str] = {
    "new_face",
    "known_new_face",
    "momentum",
    "rebound",
    "short_term",
    "comeback",
}

# A 股成本默认参数
DEFAULT_COMMISSION = 0.00025      # 万 2.5
DEFAULT_MIN_COMMISSION = 5.0      # 单笔最低 5 元
DEFAULT_STAMP_DUTY = 0.0005       # 卖出印花税 0.05%
DEFAULT_SLIPPAGE = 0.001          # 双边各 0.1%


@dataclass
class PBConfig:
    """组合回测配置。"""
    start: str | None = None            # 回测起始日（推荐日口径，含）；None=数据最早
    end: str | None = None              # 回测结束日（推荐日口径，含）；None=数据最晚
    days: int = 0                       # 仅用最近 N 天推荐（相对最晚推荐日）；0=全部
    hold_days: int = 3                  # 持有交易日数
    buy_delay: int = 1                  # 信号日到买入日的交易日的偏移（默认 1）
    max_positions: int = 10            # 最大同时持仓数（= 等权槽位数）
    initial_capital: float = 1_000_000.0
    commission: float = DEFAULT_COMMISSION
    min_commission: float = DEFAULT_MIN_COMMISSION
    stamp_duty: float = DEFAULT_STAMP_DUTY
    slippage: float = DEFAULT_SLIPPAGE
    category: str | None = None        # 仅跑该类别；None=综合(全部现役类别)
    no_skill: bool = False              # True=基准模式：不按 score 筛选，买入全部当日信号


@dataclass
class Signal:
    rec_date: str
    symbol: str
    name: str
    category: str
    score: int
    buy_date: str | None = None        # 实际买入日（交易日历内）；None=无法执行
    buy_index: int = -1                 # 买入日在日历中的下标
    exit_index: int = -1                # 卖出日在日历中的下标


@dataclass
class Position:
    symbol: str
    name: str
    category: str
    score: int
    entry_index: int
    entry_date: str
    entry_open: float
    shares: float
    entry_cost: float                  # 含买入成本的投入（用于计算净收益）
    exit_index: int


@dataclass
class Trade:
    symbol: str
    name: str
    category: str
    score: int
    buy_date: str
    buy_price: float
    sell_date: str
    sell_price: float
    hold_days: int
    ret_pct: float                     # 该笔净收益率（含成本）


@dataclass
class BacktestResult:
    label: str
    config: PBConfig
    nav: list[tuple[str, float]] = field(default_factory=list)   # [(date, equity)]
    trades: list[Trade] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    skipped_no_open: int = 0           # 因买入日缺开盘数据而跳过的信号数
    n_signals: int = 0
    active_start: int = 0              # 指标计算窗口起点(NAV 下标)；剔除空仓期初/期末平值
    active_end: int = 0                # 指标计算窗口终点(NAV 下标)


# ── 交易日历 / 价格加载 ─────────────────────────────────────────────────────

def _build_calendar(conn: sqlite3.Connection, start: str, end: str) -> list[str]:
    """构建 [start, end] 内的真实交易日历（基于 is_trading_day，不依赖 kline 完整度）。"""
    d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    cal: list[str] = []
    # 安全上限：防 holidays 数据损坏导致死循环
    guard = 0
    max_iter = (end_d - d).days * 3 + 365
    while d <= end_d and guard < max_iter:
        if is_trading_day(d):
            cal.append(d.isoformat())
        d += timedelta(days=1)
        guard += 1
    return cal


def _load_prices(conn: sqlite3.Connection, symbols: set[str]) -> dict[str, dict[str, tuple[float, float]]]:
    """{symbol: {date: (open, close)}}。"""
    prices: dict[str, dict[str, tuple[float, float]]] = {s: {} for s in symbols}
    for sym in symbols:
        rows = conn.execute(
            "SELECT date, open, close FROM daily_kline WHERE symbol=? ORDER BY date",
            (sym,),
        ).fetchall()
        d: dict[str, tuple[float, float]] = {}
        for dt, o, c in rows:
            if o is not None and c is not None:
                d[dt] = (o, c)
        prices[sym] = d
    return prices


def _nth_trading_day_after(d: date, n: int) -> date | None:
    """返回 d 之后第 n 个交易日（不含 d）。

    若节假日数据异常导致跳过非交易日时超过安全上限，返回 None（调用方据此跳过该信号）。
    """
    cursor = d
    max_iter = max(n * 10, 365)
    for _ in range(n):
        cursor += timedelta(days=1)
        while not is_trading_day(cursor):
            cursor += timedelta(days=1)
            max_iter -= 1
            if max_iter <= 0:
                return None
    return cursor


# ── 信号加载 ────────────────────────────────────────────────────────────────

def _load_signals(conn: sqlite3.Connection, cfg: PBConfig, calendar: list[str],
                  cal_index: dict[str, int], cal_end: str) -> list[Signal]:
    """从 recommendations 加载信号，并计算买入日 / 卖出日在日历中的下标。"""
    rows = conn.execute(
        "SELECT date, symbol, name, category, score FROM recommendations"
    ).fetchall()

    # 推荐日范围（用于 --days / 默认窗口）
    rec_dates = [r[0] for r in rows]
    max_rec = max(rec_dates)
    min_rec = min(rec_dates)
    end_date = cfg.end or max_rec
    if cfg.days > 0:
        start_date = (date.fromisoformat(max_rec) - timedelta(days=cfg.days)).isoformat()
    else:
        start_date = cfg.start or min_rec

    signals: list[Signal] = []
    for rec_date, sym, name, cat, score in rows:
        # 类别过滤
        if cfg.category and cat != cfg.category:
            continue
        if (not cfg.category) and cat not in PORTFOLIO_CATEGORIES:
            continue
        # 日期窗口过滤（推荐日口径）
        if not (start_date <= rec_date <= end_date):
            continue
        sig = Signal(rec_date=rec_date, symbol=sym, name=name, category=cat, score=score)
        # 计算买入日
        buy_d = _nth_trading_day_after(date.fromisoformat(rec_date), cfg.buy_delay)
        if buy_d is None or buy_d.isoformat() > cal_end:
            continue  # 买入日超过行情范围，无法执行
        buy_str = buy_d.isoformat()
        if buy_str not in cal_index:
            continue  # 买入日不在交易日历内（行情缺口），跳过
        sig.buy_date = buy_str
        sig.buy_index = cal_index[buy_str]
        exit_idx = sig.buy_index + cfg.hold_days
        if exit_idx >= len(calendar):
            exit_idx = len(calendar) - 1
        sig.exit_index = exit_idx
        signals.append(sig)
    return signals


# ── 核心模拟 ────────────────────────────────────────────────────────────────

def _buy_cost(notional: float, cfg: PBConfig) -> float:
    """买入总成本（含佣金，最低 5 元）。notional = 股数 * 开盘价 * (1+滑点)。"""
    commission = max(cfg.min_commission, notional * cfg.commission)
    return notional + commission


def _sell_proceeds(notional: float, cfg: PBConfig) -> float:
    """卖出净所得（扣佣金 + 印花税）。notional = 股数 * 收盘价 * (1-滑点)。"""
    commission = max(cfg.min_commission, notional * cfg.commission)
    stamp = notional * cfg.stamp_duty
    return notional - commission - stamp


def run_backtest(conn: sqlite3.Connection, cfg: PBConfig) -> BacktestResult:
    """执行组合回测，返回含 NAV 序列、逐笔交易与指标的结果。"""
    label = cfg.category or ("基准(无筛选)" if cfg.no_skill else "综合(all)")
    result = BacktestResult(label=label, config=cfg)

    # 1) 构建交易日历（覆盖全部 kline 日期范围，确保买入/卖出日有行情可查）
    min_date, max_date = conn.execute(
        "SELECT MIN(date), MAX(date) FROM daily_kline"
    ).fetchone()
    calendar = _build_calendar(conn, min_date, max_date)
    if not calendar:
        return result
    cal_index = {d: i for i, d in enumerate(calendar)}
    cal_end = calendar[-1]

    # 2) 加载信号并按买入日分组
    signals = _load_signals(conn, cfg, calendar, cal_index, cal_end)
    result.n_signals = len(signals)
    by_buy: dict[int, list[Signal]] = defaultdict(list)
    for s in signals:
        by_buy[s.buy_index].append(s)
    symbols = {s.symbol for s in signals}
    prices = _load_prices(conn, symbols)

    # 3) 模拟状态
    cash = cfg.initial_capital
    positions: list[Position] = []
    per_slot = cfg.initial_capital / cfg.max_positions

    last_close: dict[str, float] = {}   # 每个标的最近已知收盘价（缺失日向前填充）

    nav: list[tuple[str, float]] = []
    cash_series: list[float] = []       # 每日收盘后现金（用于暴露度计算）

    first_active_idx: int | None = None
    last_active_idx: int | None = None

    for i, today in enumerate(calendar):
        # ---- (a) 开盘买入：当日买入信号（按 score 或原始顺序选前 max_positions 个空位）----
        day_signals = by_buy.get(i, [])
        if day_signals:
            if not cfg.no_skill:
                day_signals = sorted(day_signals, key=lambda s: -s.score)
            for sig in day_signals:
                if len(positions) >= cfg.max_positions:
                    break
                pd = prices.get(sig.symbol, {})
                if today not in pd:
                    result.skipped_no_open += 1
                    continue
                open_p, close_p = pd[today]
                if open_p <= 0:
                    result.skipped_no_open += 1
                    continue
                invest = min(per_slot, cash)
                if invest <= 0:
                    break
                buy_notional = invest / (1 + cfg.slippage)   # 含滑点的名义成交额
                shares = buy_notional / open_p
                cost = _buy_cost(buy_notional, cfg)
                if cost > cash:
                    # 现金不足以覆盖含佣成本，按可用现金微调
                    affordable = (cash - cfg.min_commission) / (1 + cfg.slippage)
                    if affordable <= 0:
                        break
                    shares = affordable / open_p
                    cost = _buy_cost(affordable, cfg)
                cash -= cost
                positions.append(Position(
                    symbol=sig.symbol, name=sig.name, category=sig.category,
                    score=sig.score, entry_index=i, entry_date=today,
                    entry_open=open_p, shares=shares, entry_cost=cost,
                    exit_index=sig.exit_index,
                ))
                last_close[sig.symbol] = close_p
                if first_active_idx is None:
                    first_active_idx = i
                last_active_idx = i

        # ---- (b) 收盘卖出：到期持仓 ----
        still_open: list[Position] = []
        for pos in positions:
            if i >= pos.exit_index:
                pd = prices.get(pos.symbol, {})
                if today in pd:
                    close_p = pd[today][1]
                else:
                    close_p = last_close.get(pos.symbol)
                if close_p is None or close_p <= 0:
                    # 卖出日无收盘数据：继续持有（下一日再尝试），不强行平仓
                    still_open.append(pos)
                    continue
                sell_notional = pos.shares * close_p * (1 - cfg.slippage)
                proceeds = _sell_proceeds(sell_notional, cfg)
                cash += proceeds
                ret = proceeds / pos.entry_cost - 1.0
                result.trades.append(Trade(
                    symbol=pos.symbol, name=pos.name, category=pos.category,
                    score=pos.score, buy_date=pos.entry_date, buy_price=pos.entry_open,
                    sell_date=today, sell_price=close_p,
                    hold_days=i - pos.entry_index, ret_pct=ret,
                ))
                last_close[pos.symbol] = close_p
                last_active_idx = i
            else:
                still_open.append(pos)
        positions = still_open
        if positions:
            last_active_idx = i

        # ---- (c) 收盘标记 NAV ----
        equity = cash
        for pos in positions:
            pd = prices.get(pos.symbol, {})
            if today in pd:
                cp = pd[today][1]
                last_close[pos.symbol] = cp
            else:
                cp = last_close.get(pos.symbol)
            if cp is not None:
                equity += pos.shares * cp
        nav.append((today, equity))
        cash_series.append(cash)

    result.nav = nav
    # 活跃窗口：首个买入日 → 末个卖出/持仓日；剔除空仓期初/期末平值，避免稀释年化与总收益
    s = 0 if first_active_idx is None else first_active_idx
    e = (len(nav) - 1) if last_active_idx is None else last_active_idx
    result.active_start = s
    result.active_end = e
    result.metrics = _compute_metrics(
        nav[s:e + 1], result.trades, cfg, cash_series[s:e + 1]
    )
    return result


# ── 指标计算 ────────────────────────────────────────────────────────────────

def _compute_metrics(nav: list[tuple[str, float]], trades: list[Trade],
                     cfg: PBConfig, cash_series: list[float]) -> dict:
    if len(nav) < 2:
        return {"error": "NAV 序列过短，无法计算指标"}
    values = [v for _, v in nav]
    v0, vlast = values[0], values[-1]
    n_days = len(values) - 1

    total_return = vlast / v0 - 1.0
    annualized = (vlast / v0) ** (252.0 / n_days) - 1.0 if n_days > 0 else 0.0

    rets = [values[i] / values[i - 1] - 1.0 for i in range(1, len(values))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    std = var ** 0.5
    sharpe = (mean / std) * (252 ** 0.5) if std > 1e-12 else 0.0

    # 最大回撤
    peak = values[0]
    max_dd = 0.0
    for v in values:
        if v > peak:
            peak = v
        dd = v / peak - 1.0
        if dd < max_dd:
            max_dd = dd

    # 逐笔交易统计
    n_trades = len(trades)
    wins = [t.ret_pct for t in trades if t.ret_pct > 0]
    losses = [-t.ret_pct for t in trades if t.ret_pct <= 0]
    win_rate = (len(wins) / n_trades) if n_trades else 0.0
    avg_trade = (sum(t.ret_pct for t in trades) / n_trades) if n_trades else 0.0
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    pl_ratio = (avg_win / avg_loss) if avg_loss > 0 else 0.0
    avg_hold = (sum(t.hold_days for t in trades) / n_trades) if n_trades else 0.0

    # 暴露度：每日收盘后「持仓市值 / 总权益」的均值（满仓旋转模型下接近 1，
    # 建仓期/空仓期会低于 1；反映策略实际资金占用水平）
    exposure_sum = 0.0
    for (_, eq), c in zip(nav, cash_series):
        if eq > 0:
            exposure_sum += (eq - c) / eq
    exposure = exposure_sum / len(nav) if nav else 0.0

    return {
        "initial_capital": cfg.initial_capital,
        "final_equity": vlast,
        "total_return": total_return,
        "annualized": annualized,
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "n_trades": n_trades,
        "win_rate": win_rate,
        "avg_trade_return": avg_trade,
        "profit_loss_ratio": pl_ratio,
        "avg_holding_days": avg_hold,
        "exposure": exposure,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
    }


# ── 报告输出 ────────────────────────────────────────────────────────────────

def print_report(result: BacktestResult) -> None:
    m = result.metrics
    cfg = result.config
    print("=" * 72)
    print(f"组合级回测报告  [{result.label}]")
    print("=" * 72)
    print(f"回测窗口(推荐日): start={cfg.start or '全期'}  end={cfg.end or '全期'}  "
          f"days={cfg.days or '全期'}")
    print(f"参数: hold_days={cfg.hold_days}  buy_delay={cfg.buy_delay}  "
          f"max_positions={cfg.max_positions}  initial={cfg.initial_capital:,.0f}")
    print(f"成本: 佣金={cfg.commission*10000:.2f}‱(最低{cfg.min_commission:.0f}元)  "
          f"印花税={cfg.stamp_duty*100:.3f}%  滑点={cfg.slippage*100:.2f}%(双边)")
    print(f"信号数={result.n_signals}  成交笔数={m.get('n_trades',0)}  "
          f"跳过(缺开盘)={result.skipped_no_open}")
    print("-" * 72)
    if "error" in m:
        print(f"  [!] {m['error']}")
        return
    print(f"初始资金      : {m['initial_capital']:>14,.0f}")
    print(f"期末权益      : {m['final_equity']:>14,.0f}")
    print(f"总收益        : {m['total_return']*100:>13.2f}%")
    print(f"年化收益      : {m['annualized']*100:>13.2f}%")
    print(f"Sharpe(年化)  : {m['sharpe']:>14.2f}")
    print(f"最大回撤      : {m['max_drawdown']*100:>13.2f}%")
    print(f"胜率          : {m['win_rate']*100:>13.2f}%")
    print(f"平均单笔收益  : {m['avg_trade_return']*100:>13.2f}%")
    print(f"盈亏比        : {m['profit_loss_ratio']:>14.2f}")
    print(f"平均持仓(日)  : {m['avg_holding_days']:>14.1f}")
    print(f"资金暴露度    : {m['exposure']*100:>13.2f}%")
    print(f"盈利均/亏损均 : {m['avg_win']*100:>11.2f}% / {m['avg_loss']*100:>9.2f}%")


def print_comparison(results: list[BacktestResult]) -> None:
    print("\n" + "=" * 92)
    print("多策略对比（参数见各报告；组合回测为等权满仓旋转模型）")
    print("=" * 92)
    header = (f"{'策略':<18}{'总收益':>9}{'年化':>9}{'Sharpe':>8}"
              f"{'最大回撤':>10}{'胜率':>8}{'笔数':>7}")
    print(header)
    print("-" * 92)
    for r in results:
        m = r.metrics
        if "error" in m:
            print(f"{r.label:<18}{'N/A':>9}")
            continue
        print(f"{r.label:<18}{m['total_return']*100:>8.2f}%{m['annualized']*100:>8.2f}%"
              f"{m['sharpe']:>8.2f}{m['max_drawdown']*100:>9.2f}%"
              f"{m['win_rate']*100:>7.1f}%{m['n_trades']:>7}")


def export_nav(result: BacktestResult, path: str) -> None:
    """导出 NAV 序列为 CSV（含每日收益与回撤）。仅导出活跃窗口，与报告指标口径一致。"""
    s, e = result.active_start, result.active_end
    nav_slice = result.nav[s:e + 1]
    values = [v for _, v in nav_slice]
    peak = values[0]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "equity", "daily_return", "drawdown"])
        for i, (d, v) in enumerate(nav_slice):
            dr = (v / values[i - 1] - 1.0) if i > 0 else 0.0
            if v > peak:
                peak = v
            dd = v / peak - 1.0
            w.writerow([d, f"{v:.2f}", f"{dr*100:.4f}", f"{dd*100:.4f}"])
    print(f"[导出] NAV 序列(活跃窗口)已写入 {path}")


# ── CLI ──────────────────────────────────────────────────────────────────────

def _run_one(conn: sqlite3.Connection, cfg: PBConfig) -> BacktestResult:
    return run_backtest(conn, cfg)


def main() -> None:
    parser = argparse.ArgumentParser(description="创业板扫描策略 组合级回测")
    parser.add_argument("--days", type=int, default=0, help="仅用最近 N 天推荐（0=全部历史）")
    parser.add_argument("--start", default=None, help="起始推荐日 YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="结束推荐日 YYYY-MM-DD")
    parser.add_argument("--category", default=None,
                        choices=sorted(PORTFOLIO_CATEGORIES),
                        help="仅跑该策略类别；省略=综合(all)")
    parser.add_argument("--hold-days", type=int, default=3, help="持有交易日数（默认3）")
    parser.add_argument("--buy-delay", type=int, default=1, help="信号日到买入日偏移（默认1）")
    parser.add_argument("--max-positions", type=int, default=10, help="最大持仓数/等权槽位")
    parser.add_argument("--initial", type=float, default=1_000_000.0, help="初始资金(元)")
    parser.add_argument("--commission", type=float, default=DEFAULT_COMMISSION)
    parser.add_argument("--stamp-duty", type=float, default=DEFAULT_STAMP_DUTY)
    parser.add_argument("--slippage", type=float, default=DEFAULT_SLIPPAGE)
    parser.add_argument("--compare", action="store_true",
                        help="多策略对比：各现役类别 + 综合 + 基准")
    parser.add_argument("--export", default=None, help="导出 NAV 序列 CSV 路径")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)

    base_cfg = PBConfig(
        days=args.days, start=args.start, end=args.end,
        hold_days=args.hold_days, buy_delay=args.buy_delay,
        max_positions=args.max_positions, initial_capital=args.initial,
        commission=args.commission, stamp_duty=args.stamp_duty,
        slippage=args.slippage,
    )

    if args.compare:
        results: list[BacktestResult] = []
        # 基准（无筛选）
        bench = _run_one(conn, replace(base_cfg, no_skill=True, category=None))
        results.append(bench)
        # 综合
        combined = _run_one(conn, replace(base_cfg, category=None))
        results.append(combined)
        # 各现役类别
        for cat in sorted(PORTFOLIO_CATEGORIES):
            r = _run_one(conn, replace(base_cfg, category=cat))
            results.append(r)
        for r in results:
            print_report(r)
        print_comparison(results)
        if args.export:
            export_nav(combined, args.export)
    else:
        cfg = replace(base_cfg, category=args.category)
        res = _run_one(conn, cfg)
        print_report(res)
        if args.export:
            export_nav(res, args.export)

    conn.close()


if __name__ == "__main__":
    main()
