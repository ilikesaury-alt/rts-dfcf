"""数据真实性前置检查（2026-08-18 拓斯达脏数据事故后新增）。

背景：daily_kline 盘中残留的「未定稿今日 bar」（盘中价 + 部分量能）一旦收盘后无
覆盖，会静默污染 next_day_pct → 回测/归因/复盘全部口径。本地契约检查（make_kline_bar
的 close>0/NaN 剔除等）抓不到自洽脏数据——脏 bar 的 percent/量价内部一致，唯一
可靠的是**跨数据源交叉验证**（新浪 qfq 独立于雪球）。

用法（供回测/归因/复盘工具出报告前调用）：
    report = check_kline_health(conn, dates=dates)
    banner = health_banner(report)
    if banner: print(banner)
    if report.blocked: ...  # 中止，提示先跑 python repair_kline.py
"""
from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass, field

from scanner.utils import to_float

# 抽验最少对数：不足时只能警告不能阻断（避免小样本误判）
MIN_CHECKED = 5
# 阻断阈值：抽验样本中不符比例 ≥ 30% 判定数据疑似污染
BLOCK_RATIO = 0.3
# 容差：1 分钱以上视为不符（qfq 舍入噪声）
TOLERANCE = 0.011


@dataclass
class HealthReport:
    checked: int = 0
    mismatched: int = 0
    source_ok: bool = True
    samples: list[tuple] = field(default_factory=list)  # (symbol, date, db_close, ref_close)

    @property
    def ratio(self) -> float:
        return self.mismatched / self.checked if self.checked else 0.0

    @property
    def blocked(self) -> bool:
        """样本足够且不符比例超阈值 → 阻断出报告（数据疑似污染）。"""
        return self.source_ok and self.checked >= MIN_CHECKED and self.ratio >= BLOCK_RATIO


def _sina_close(symbol: str, date_str: str) -> float | None:
    """独立数据源（新浪 qfq）取指定日期收盘价；失败返回 None（fail-open）。"""
    try:
        import akshare as ak

        df = ak.stock_zh_a_daily(symbol=f"sz{symbol[2:]}", adjust="qfq")
        row = df[df["date"].astype(str) == date_str]
        if len(row):
            return float(row.iloc[0]["close"])
    except Exception:  # noqa: BLE001  网络/解析失败 → None，由调用方按 source_ok 处理
        pass
    return None


def check_kline_health(conn: sqlite3.Connection,
                       dates: list[str] | None = None,
                       sample_n: int = 10) -> HealthReport:
    """抽样交叉验证 daily_kline 与独立数据源（新浪 qfq）的一致性。

    dates 为 None 时取最近 10 个有数据的交易日。抽验样本按日期倒序取（近端优先，
    近端数据对回测结论影响最大）。结果含不符样本明细，供 health_banner 展示。
    """
    if dates is None:
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM daily_kline ORDER BY date DESC LIMIT 10"
        ).fetchall()]
    if not dates:
        return HealthReport()
    placeholders = ",".join("?" * len(dates))
    rows = conn.execute(
        f"SELECT symbol, date, close FROM daily_kline "
        f"WHERE date IN ({placeholders}) ORDER BY date DESC, symbol",
        tuple(dates),
    ).fetchall()
    if not rows:
        return HealthReport()
    random.seed(20260818)  # 固定种子，结果可复现
    sample = random.sample(rows, min(sample_n, len(rows)))

    report = HealthReport()
    for sym, d, db_close in sample:
        # 历史脏行 close 可能为 NULL/字符串/0/NaN（契约重构前遗留）：无法与独立源
        # 交叉验证，跳过该样本（与源不可达同语义），避免 abs() 对 None/str 抛
        # TypeError 崩溃整检查（此工具的目的正是处理脏数据，不能遇脏即崩）。
        db_close_f = to_float(db_close, None)
        if db_close_f is None or db_close_f <= 0:
            continue
        ref = _sina_close(sym, d)
        if ref is None:
            continue  # 源不可达/无该日数据 → 该样本不参与统计
        report.checked += 1
        if abs(db_close_f - ref) > TOLERANCE:
            report.mismatched += 1
            report.samples.append((sym, d, db_close_f, ref))
    if report.checked == 0:
        report.source_ok = False  # 全部样本源不可达，无法验证
    return report


def count_unfinalized_today(conn: sqlite3.Connection, date_str: str | None = None) -> int:
    """今日 bar 中 finalized=0（盘中未定稿快照）的数量。

    finalized 标记由 save_kline_to_db 写入：盘中写入的今日 bar 置 0，收盘定稿/
    收盘后写入置 1。计数 >0 说明盘中有残留快照（收盘定稿前属正常，之后仍 >0
    说明定稿机制没跑/失败）。
    """
    if date_str is None:
        from scanner.config import now_beijing

        date_str = now_beijing().date().isoformat()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM daily_kline WHERE date=? AND finalized=0",
            (date_str,),
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return 0  # 旧库无 finalized 列（未迁移）→ 无法判定，按 0 处理


def health_banner(report: HealthReport) -> str:
    """把 HealthReport 渲染成终端横幅；无异常返回空串。"""
    if not report.source_ok:
        return ("  ⚠ 数据健康检查：独立数据源（新浪）不可达，跳过交叉验证\n"
                "    （不影响报告；建议稍后跑 python repair_kline.py --dry-run 自查）")
    if report.mismatched == 0:
        return ""
    if report.blocked:
        lines = [
            f"  [数据健康检查] ❌ 抽验 {report.checked} 条，{report.mismatched} 条与独立源不符"
            f"（{report.ratio*100:.0f}%，阈值 {BLOCK_RATIO*100:.0f}%）——数据疑似污染！",
        ]
        for sym, d, dbc, ref in report.samples[:5]:
            lines.append(f"      {sym} {d}: DB={dbc} 独立源={ref}")
        lines.append("      先跑 python repair_kline.py 修复后重试；确属噪声可加 --force 强行出报告")
        return "\n".join(lines)
    return (f"  ⚠ 数据健康检查：抽验 {report.checked} 条，{report.mismatched} 条与独立源不符"
            f"（{report.ratio*100:.0f}%），低于阈值 {BLOCK_RATIO*100:.0f}%，报告继续但请注意数据质量")
