from datetime import date, datetime, timedelta

from scanner.config import HOLIDAYS, MORNING_START, MORNING_END, AFTERNOON_START, AFTERNOON_END


def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    return d.isoformat() not in HOLIDAYS


def is_trading_time(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if not is_trading_day(now.date()):
        return False
    t = now.time()
    return (MORNING_START <= t <= MORNING_END) or (AFTERNOON_START <= t <= AFTERNOON_END)


def seconds_until_next_session(now: datetime | None = None) -> int:
    now = now or datetime.now()
    today = now.date()
    t = now.time()

    if is_trading_day(today):
        if t < MORNING_START:
            return int((datetime.combine(today, MORNING_START) - now).total_seconds())
        if MORNING_END < t < AFTERNOON_START:
            return int((datetime.combine(today, AFTERNOON_START) - now).total_seconds())
        if t > AFTERNOON_END:
            return _seconds_until_next_trading_day(now)
        return 0

    return _seconds_until_next_trading_day(now)


def _seconds_until_next_trading_day(now: datetime) -> int:
    cursor = now.date() + timedelta(days=1)
    while not is_trading_day(cursor):
        cursor += timedelta(days=1)
    return int((datetime.combine(cursor, MORNING_START) - now).total_seconds())


def next_session_label(now: datetime | None = None) -> str:
    now = now or datetime.now()
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
    while not is_trading_day(cursor):
        cursor += timedelta(days=1)
    return f"下次交易 {cursor.isoformat()} 09:30"
