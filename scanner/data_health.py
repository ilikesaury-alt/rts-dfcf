"""数据真实性前置检查（2026-08-18 拓斯达脏数据事故后新增）。

背景：daily_kline 盘中残留的「未定稿今日 bar」（盘中价 + 部分量能）一旦收盘后无
覆盖，会静默污染 next_day_pct → 回测/归因/复盘全部口径。本地契约检查（make_kline_bar
的 close>0/NaN 剔除等）抓不到自洽脏数据——脏 bar 的 percent/量价内部一致，唯一
可靠的是**跨数据源交叉验证**（新浪 qfq 主参照 + 同花顺官方 API 回退参照，
均独立于雪球）。

用法（供回测/归因/复盘工具出报告前调用）：
    report = check_kline_health(conn, dates=dates)
    banner = health_banner(report)
    if banner: print(banner)
    if report.blocked: ...  # 中止，提示先跑 python repair_kline.py
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import sqlite3
from dataclasses import dataclass, field

import requests

from scanner.config import KLINE_DRIFT_FINGERPRINT_BARS, KLINE_DRIFT_MIN_BARS, KLINE_DRIFT_ROUND_DP, now_beijing
from scanner.utils import EXTERNAL_FAILURES, to_float

# 抽验最少对数：不足时只能警告不能阻断（避免小样本误判）
MIN_CHECKED = 5
# 阻断阈值：抽验样本中不符比例 ≥ 30% 判定数据疑似污染
BLOCK_RATIO = 0.3
# 容差：1 分钱以上视为不符（qfq 舍入噪声）
TOLERANCE = 0.011
# THS 官方源相对容差：THS forward 与雪球 qfq 锚点偶有细微差异（2026-08-23 探测
# 实测个别票分红后 ~0.36%），不能逐分对齐，用相对容差 0.5%
THS_TOLERANCE_REL = 0.005
# 大盘指数对账（2026-08-19）：扫描快照时点 vs 审计时点，同一天内允许的涨幅偏差(pp)。
# 隔日错位（如 -6.26% 崩盘被读成昨日 -0.93%）偏差 5.3pp 远超容差，必被命中。
INDEX_PCT_TOLERANCE = 0.5


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
    """回退数据源（新浪 qfq，经 akshare）取指定日期收盘价；失败返回 None（fail-open）。"""
    try:
        import akshare as ak

        df = ak.stock_zh_a_daily(symbol=f"sz{symbol[2:]}", adjust="qfq")
        row = df[df["date"].astype(str) == date_str]
        if len(row):
            return float(row.iloc[0]["close"])
    except EXTERNAL_FAILURES:  # noqa: BLE001  网络/解析失败 → None，由调用方按 source_ok 处理
        pass  # 外部依赖降级，非代码错误
    return None


def _ths_close(symbol: str, date_str: str) -> float | None:
    """主参照源（同花顺官方 API）指定日期收盘价；失败返回 None。

    2026-08-23 升为主参照：官方 REST 免爬虫解析与 akshare 版本耦合；
    新浪 qfq（akshare）降为回退。口径对账实测与本地雪球 qfq 一致
    （相对容差 THS_TOLERANCE_REL）。
    """
    from scanner import ths_api  # 惰性导入：无 Key 时零开销跳过

    closes = ths_api.fetch_kline_closes(symbol, date_str, date_str)
    if not closes:
        return None
    return closes.get(date_str)


def check_kline_health(conn: sqlite3.Connection, dates: list[str] | None = None, sample_n: int = 10) -> HealthReport:
    """抽样交叉验证 daily_kline 与独立数据源（新浪 qfq）的一致性。

    dates 为 None 时取最近 10 个有数据的交易日。抽验样本按日期倒序取（近端优先，
    近端数据对回测结论影响最大）。结果含不符样本明细，供 health_banner 展示。
    """
    if dates is None:
        dates = [
            r[0] for r in conn.execute("SELECT DISTINCT date FROM daily_kline ORDER BY date DESC LIMIT 10").fetchall()
        ]
    if not dates:
        return HealthReport()
    placeholders = ",".join("?" * len(dates))
    rows = conn.execute(
        f"SELECT symbol, date, close FROM daily_kline "  # noqa: S608 - 占位符由 ",".join("?" * n) 生成，值经参数化传入
        f"WHERE date IN ({placeholders}) ORDER BY date DESC, symbol",
        tuple(dates),
    ).fetchall()
    if not rows:
        return HealthReport()
    rng = random.Random(20260818)  # noqa: S311 - 仅用于确定性抽样（固定种子保证可复现），无安全用途
    sample = rng.sample(rows, min(sample_n, len(rows)))

    report = HealthReport()
    for sym, d, db_close in sample:
        # 历史脏行 close 可能为 NULL/字符串/0/NaN（契约重构前遗留）：无法与独立源
        # 交叉验证，跳过该样本（与源不可达同语义），避免 abs() 对 None/str 抛
        # TypeError 崩溃整检查（此工具的目的正是处理脏数据，不能遇脏即崩）。
        db_close_f = to_float(db_close, None)
        if db_close_f is None or db_close_f <= 0:
            continue
        # 主参照：同花顺官方 API（2026-08-23，免 akshare 爬虫依赖，相对容差——
        # THS forward 与雪球 qfq 锚点偶有 ~0.36% 微差，不能逐分对齐）；
        # 回退参照：新浪 qfq（akshare，绝对容差 1 分钱）。
        ref = _ths_close(sym, d)
        if ref is not None:
            tol = max(TOLERANCE, ref * THS_TOLERANCE_REL)
        else:
            ref = _sina_close(sym, d)
            tol = TOLERANCE
        if ref is None:
            continue  # 两源皆不可达/无该日数据 → 该样本不参与统计
        report.checked += 1
        if abs(db_close_f - ref) > tol:
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
    except sqlite3.OperationalError as e:
        if "no such column" in str(e):
            return 0  # 旧库无 finalized 列（未迁移）→ 无法判定，按 0 处理
        # database is locked 等其它 OperationalError 不应被吞成「无未定稿」
        # （2026-08-24 审查：那会让健康检查在锁竞争期静默漏报）
        raise


def _eastmoney_index_pct() -> float | None:
    """独立数据源（东财 push2delay，本机实测可达）取创业板指(399006)当前涨跌幅。

    与雪球异源（东财 vs 雪球），是唯一可靠地验证「扫描器当时读到的大盘涨幅是否属实」
    的手段——本地契约检查抓不到自洽错值（-0.93 与 -6.26 都是合法 float，只有跨源
    对比或 bar 日期证据能区分）。东财 f170 单位与项目大盘涨幅一致（百分比，-626 → -6.26%）。
    """
    try:
        url = "https://push2delay.eastmoney.com/api/qt/stock/get?secid=0.399006&fields=f43,f170"
        r = requests.get(url, timeout=(5, 10), headers={"User-Agent": "Mozilla/5.0"})
        data = (r.json() or {}).get("data") or {}
        pct = to_float(data.get("f170"), None)
        if pct is not None:
            return pct / 100.0
        return None
    except Exception:  # noqa: BLE001  网络/解析失败 → None，按 source_ok 处理
        return None


@dataclass
class IndexHealthReport:
    checked: int = 0
    stale_bar: bool = False  # 记录 bar 日期滞后（读到旧 bar = 涨幅可能非当日）
    mismatch: bool = False  # 同日内扫描涨幅与独立源偏差超容差
    recorded_pct: float | None = None
    recorded_bar: str | None = None
    recorded_time: str | None = None
    ref_pct: float | None = None
    source_ok: bool = True


def check_market_index_health(conn: sqlite3.Connection, date_str: str | None = None) -> IndexHealthReport:
    """对账大盘指数血缘日志（market_index_log）与独立源（东财）。

    背景（2026-08-19）：大盘标签曾因雪球 kline 接口 begin/count 语义错位，把当日
    -6.26% 崩盘读成昨日 -0.93%（展示"大盘中性"）而无痕——涨幅是瞬时值、不进
    daily_kline，daily_kline 交叉验证覆盖不到。此检查把「扫描器当时读到什么」变成
    可审计对象。

    三层检查：
    1. 有无当日记录（无记录 → 扫描没跑/落库失败，无法审计，source_ok=False）
    2. bar 日期滞后（bar_date < 被审计日期且扫描时间 ≥ 09:30 → 读到旧 bar）
    3. 涨幅与独立源偏差（仅当记录 bar 日期 == 被审计日期时对比；跨日记录读的是前一日
       收盘，与今日 spot 无对比意义，只靠第 2 层判断）

    盘中扫描时点与审计时点同一天内指数有自然波动，容差 INDEX_PCT_TOLERANCE=0.5pp。
    """
    from scanner.database import get_market_index_log  # 惰性导入避免与 database 循环依赖

    report = IndexHealthReport()
    rec = get_market_index_log(conn, date_str)
    if not rec or rec.get("index_pct") is None:
        report.source_ok = False
        return report
    report.recorded_pct = rec.get("index_pct")
    report.recorded_bar = rec.get("bar_date")
    report.recorded_time = rec.get("time")
    report.checked = 1
    if date_str is None:
        date_str = now_beijing().date().isoformat()
    # 交易日 09:30 后扫描应读到当日 bar（开盘前今日 bar 尚未生成，读到昨日属正常）
    if report.recorded_bar and report.recorded_bar < date_str and (report.recorded_time or "") >= "09:30":
        report.stale_bar = True
    ref = _eastmoney_index_pct()
    if ref is None:
        report.source_ok = False
        return report
    report.ref_pct = ref
    # 2026-08-29：recorded_pct 为 None（血缘表缺 pct 列值）时直接相减会抛 TypeError，
    # 让整个审计函数崩掉、横幅退化为"无法对账"。显式判空后再比对。
    if (
        report.recorded_bar == date_str
        and report.recorded_pct is not None
        and abs(report.recorded_pct - ref) > INDEX_PCT_TOLERANCE
    ):
        report.mismatch = True
    return report


def index_health_banner(report: IndexHealthReport) -> str:
    """把 IndexHealthReport 渲染成终端横幅；无异常返回空串。"""
    if report.checked == 0:
        return "  ⚠ 大盘指数审计：无当日血缘记录（扫描未跑/落库失败），无法对账"
    lines = []
    if report.stale_bar:
        lines.append(
            f"  ❌ 大盘指数审计：扫描读到旧 bar（{report.recorded_bar}，"
            f"时间 {report.recorded_time}）——涨幅非当日，大盘标签失真！"
        )
    if report.mismatch and report.recorded_pct is not None and report.ref_pct is not None:
        lines.append(
            f"  ❌ 大盘指数审计：扫描涨幅 {report.recorded_pct}% vs 独立源(东财) "
            f"{report.ref_pct}%（偏差 {abs(report.recorded_pct - report.ref_pct):.2f}pp，"
            f"容差 {INDEX_PCT_TOLERANCE}pp）——数据源口径异常！"
        )
    if not report.source_ok:
        lines.append("  ⚠ 大盘指数审计：独立源（东财）不可达或记录缺失，跳过涨幅对比")
    return "\n".join(lines)


def health_banner(report: HealthReport) -> str:
    """把 HealthReport 渲染成终端横幅；无异常返回空串。"""
    if not report.source_ok:
        return (
            "  ⚠ 数据健康检查：独立数据源（新浪/同花顺）均不可达，跳过交叉验证\n"
            "    （不影响报告；建议稍后跑 python repair_kline.py --dry-run 自查）"
        )
    if report.mismatched == 0:
        return ""
    if report.blocked:
        lines = [
            f"  [数据健康检查] ❌ 抽验 {report.checked} 条，{report.mismatched} 条与独立源不符"
            f"（{report.ratio * 100:.0f}%，阈值 {BLOCK_RATIO * 100:.0f}%）——数据疑似污染！",
        ]
        for sym, d, dbc, ref in report.samples[:5]:
            lines.append(f"      {sym} {d}: DB={dbc} 独立源={ref}")
        lines.append("      先跑 python repair_kline.py 修复后重试；确属噪声可加 --force 强行出报告")
        return "\n".join(lines)
    return (
        f"  ⚠ 数据健康检查：抽验 {report.checked} 条，{report.mismatched} 条与独立源不符"
        f"（{report.ratio * 100:.0f}%），低于阈值 {BLOCK_RATIO * 100:.0f}%，报告继续但请注意数据质量"
    )


# ── 复权漂移指纹监控（M1.2，2026-09-05；自 scanner/kline_drift.py 并入）──
#
# 背景：daily_kline 存雪球前复权（qfq）价——qfq 以最新 bar 为锚整体重算，
# 除权事件会**静默改写全部历史价格**（HTTP 不报错、无任何痕迹）。后果：
#   - --rescore 重扫 / 回测跨期不可复现（同一窗口两次读取数值不同）；
#   - accumulated_pct / 🎯 门槛（NEXTDAY_ACCUM_MIN）在除权日附近失真。
#
# 防御：收盘定稿后（unified_scanner 非交易分支）对**锚定历史窗口**做 SHA256
# 指纹比对——首次运行记录「截至昨日的最近 N 根 bar × 当时存在的 symbol 集合」，
# 此后每日用同一窗口重算哈希；不一致即前复权重算/数据回改，告警并更新指纹
# （同一变更只告警一次，审计走 logs/finalize.log）。
#
# 设计约束：
#   - symbol 集合取锚定时点快照（JSON 存行内）——后补历史票（repair_kline/
#     backfill_kline 补新票的历史）不污染哈希，只在锚定集合内部比对；
#   - volume 不参与哈希（复权重算只改价格；量能修正属另一类数据事件，
#     由 scan_quality_log 计数器覆盖，避免双通道误报混淆）；
#   - 窗口缺失（锚定票全被清理）标记为 kline_hash=''，只告警一次；
#   - fail-open：任何异常由调用方捕获告警，不阻断扫描主流程。


def _fingerprint_hash(
    conn: sqlite3.Connection, start_date: str, end_date: str, symbols: list[str]
) -> tuple[str | None, int]:
    """对固定窗口（date 范围 × symbol 集合）计算价格指纹。返回 (hash, bar 数)。

    哈希输入：symbol/date（顺序键）+ OHLC/percent（round 到 KLINE_DRIFT_ROUND_DP，
    非有限值按 0.0——与 api._num 口径一致）。window 为空 → (None, 0)。
    """
    if not symbols:
        return None, 0
    # 临时表 JOIN 代替动态 IN 占位符拼接：完全参数化，零字符串拼 SQL
    # （静态 SQL 安全规则不再命中，且大 symbol 集合下 sqlite 参数上限无虞）
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS fp_symbols (symbol TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM fp_symbols")
    conn.executemany("INSERT INTO fp_symbols (symbol) VALUES (?)", [(s,) for s in symbols])
    rows = conn.execute(
        "SELECT d.symbol, d.date, d.open, d.high, d.low, d.close, d.percent "
        "FROM daily_kline d JOIN fp_symbols f ON d.symbol = f.symbol "
        "WHERE d.date >= ? AND d.date <= ? ORDER BY d.symbol, d.date",
        (start_date, end_date),
    ).fetchall()
    conn.execute("DELETE FROM fp_symbols")
    h = hashlib.sha256()
    dp = KLINE_DRIFT_ROUND_DP
    for sym, dt, o, hi, low, c, pct in rows:
        parts = [str(sym), str(dt)]
        for x in (o, hi, low, c, pct):
            v = to_float(x, default=0.0)
            if not math.isfinite(v):
                v = 0.0
            parts.append(f"{round(v, dp):.{dp}f}")
        h.update(("|".join(parts) + "\n").encode("utf-8"))
    # 空窗口返回 None（而非空内容哈希）：调用方靠 None 识别「窗口数据缺失」
    return (h.hexdigest() if rows else None), len(rows)


def _init_fingerprint(conn: sqlite3.Connection, today: str) -> str | None:
    """首次运行：锚定「截至昨日的最近 N 根 × 当时存在的 symbol 集合」。

    N = min(可用历史，KLINE_DRIFT_FINGERPRINT_BARS)；历史不足 KLINE_DRIFT_MIN_BARS
    → 返回 None（不初始化，次日再试）。直接要求满 250 日会让新库长期处于
    无监控状态（真实库实测仅 ~70 交易日），降级为小窗口立即生效。
    """
    dates = [r[0] for r in conn.execute("SELECT DISTINCT date FROM daily_kline WHERE date < ? ORDER BY date", (today,))]
    if len(dates) < KLINE_DRIFT_MIN_BARS:
        return None  # 历史不足，窗口无法锚定（新库/清库后需积累）
    end_date = dates[-1]
    start_date = dates[-min(len(dates), KLINE_DRIFT_FINGERPRINT_BARS)]
    syms = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT symbol FROM daily_kline WHERE date >= ? AND date <= ? ORDER BY symbol",
            (start_date, end_date),
        )
    ]
    if not syms:
        return None
    h, n = _fingerprint_hash(conn, start_date, end_date, syms)
    now = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT OR REPLACE INTO kline_fingerprint "
        "(anchor_date, start_date, end_date, symbols, kline_hash, updated) VALUES (?,?,?,?,?,?)",
        (today, start_date, end_date, json.dumps(syms), h or "", now),
    )
    conn.commit()
    return f"[指纹] 复权漂移监控初始化：窗口 {start_date}~{end_date}（{len(syms)} 票 / {n} bars）"


def check_kline_fingerprint(conn: sqlite3.Connection) -> str | None:
    """每日一次指纹校验（每库单锚定行）。返回状态/告警行（None=无事件）。

    异常向上抛——调用方（unified_scanner._check_kline_fingerprint_once）fail-open。
    """
    today = now_beijing().date().isoformat()
    row = conn.execute(
        "SELECT anchor_date, start_date, end_date, symbols, kline_hash FROM kline_fingerprint LIMIT 1"
    ).fetchone()
    if row is None:
        return _init_fingerprint(conn, today)
    anchor, start_date, end_date, symbols_json, old_hash = row
    try:
        symbols: list[str] = json.loads(symbols_json or "[]")
    except (TypeError, ValueError):
        symbols = []
    new_hash, n = _fingerprint_hash(conn, start_date, end_date, symbols)
    now = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
    line: str | None = None
    if new_hash is None:
        # 窗口数据消失（锚定票全被清理/库重建，_fingerprint_hash 对空结果返回 None）：
        # 只告警一次（存 hash 置空哨兵），后续静默直至数据恢复。
        if old_hash:
            line = f"[指纹] 复权漂移监控：锚定窗口 {start_date}~{end_date} 数据缺失（{len(symbols)} 票现无 bar）"
        new_hash_stored = ""
    elif old_hash and old_hash != new_hash:
        line = (
            f"[指纹] ⚠ 复权漂移告警：窗口 {start_date}~{end_date}（{n} bars）历史价格被改写"
            f"（前复权重算/数据回改）——回测/rescore 跨期口径需复查"
        )
        new_hash_stored = new_hash
    else:
        new_hash_stored = new_hash or ""
    conn.execute(
        "UPDATE kline_fingerprint SET kline_hash = ?, updated = ? WHERE anchor_date = ?",
        (new_hash_stored, now, anchor),
    )
    conn.commit()
    return line
