from datetime import date, datetime, timedelta

from scanner.config import AFTERNOON_END, AFTERNOON_START, HOLIDAYS, MORNING_END, MORNING_START, now_beijing


def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    return d.isoformat() not in HOLIDAYS


def _nth_trading_day_after(d: date, n: int) -> date | None:
    """返回 d 之后第 n 个交易日（不含 d）。

    2026-08-20 收敛单源：此前 backtest / portfolio_backtest / historical_rescan 各抄一份，
    backtest 版在 max_iter 耗尽时静默返回非交易日（holidays.json 损坏会算错 next_day 收益）。
    统一在此：节假日数据异常导致跳过非交易日超过安全上限时返回 None，由调用方跳过该信号。
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


def is_trading_time(now: datetime | None = None) -> bool:
    now = now or now_beijing()
    if not is_trading_day(now.date()):
        return False
    t = now.time()
    return (MORNING_START <= t <= MORNING_END) or (AFTERNOON_START <= t <= AFTERNOON_END)


def trading_minutes_elapsed(now: datetime | None = None) -> int:
    """当日已开盘交易分钟数（收盘后=240，开盘前/非交易日=0）。

    09:30-11:30 → 1~120（首分钟计为 1，避免投影倍数跳变）；午休 11:30-13:00 → 120；
    13:00-15:00 → 120~240。用于把盘中部分量能投影为全天量能，消除早盘 vol_ratio 天然偏低偏置。
    """
    now = now or now_beijing()
    if not is_trading_day(now.date()):
        return 0
    t = now.time()
    if t < MORNING_START:
        return 0
    if t <= MORNING_END:
        return max(int((now - datetime.combine(now.date(), MORNING_START, now.tzinfo)).total_seconds() // 60), 1)
    if t < AFTERNOON_START:
        return 120
    if t <= AFTERNOON_END:
        return 120 + int((now - datetime.combine(now.date(), AFTERNOON_START, now.tzinfo)).total_seconds() // 60)
    return 240


def seconds_until_next_session(now: datetime | None = None) -> int:
    now = now or now_beijing()
    today = now.date()
    t = now.time()

    if is_trading_day(today):
        if t < MORNING_START:
            return int((datetime.combine(today, MORNING_START, now.tzinfo) - now).total_seconds())
        if MORNING_END < t < AFTERNOON_START:
            return int((datetime.combine(today, AFTERNOON_START, now.tzinfo) - now).total_seconds())
        if t > AFTERNOON_END:
            return _seconds_until_next_trading_day(now)
        return 0

    return _seconds_until_next_trading_day(now)


def _seconds_until_next_trading_day(now: datetime) -> int:
    cursor = now.date() + timedelta(days=1)
    max_iter = 365  # 安全上限：防止 holidays.json 损坏导致无限循环
    while not is_trading_day(cursor) and max_iter > 0:
        cursor += timedelta(days=1)
        max_iter -= 1
    return int((datetime.combine(cursor, MORNING_START, now.tzinfo) - now).total_seconds())


def next_session_label(now: datetime | None = None) -> str:
    now = now or now_beijing()
    t = now.time()
    today = now.date()

    if not is_trading_day(today):
        return _next_trading_day_label(now)

    if t < MORNING_START:
        return "今日开盘 09:30"
    if MORNING_END < t < AFTERNOON_START:
        return "下午开盘 13:00"
    if t > AFTERNOON_END:
        return _next_trading_day_label(now)
    return ""


def _next_trading_day_label(now: datetime) -> str:
    cursor = now.date() + timedelta(days=1)
    max_iter = 365  # 安全上限：防止 holidays.json 损坏导致无限循环
    while not is_trading_day(cursor) and max_iter > 0:
        cursor += timedelta(days=1)
        max_iter -= 1
    return f"下次交易 {cursor.isoformat()} 09:30"
