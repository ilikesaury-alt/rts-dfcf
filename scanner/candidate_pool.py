from datetime import date, datetime, timedelta

from scanner.models import Candidate
from scanner.config import STALE_TIMEOUT_MINUTES


class ScanSession:
    """Encapsulates mutable scan state that was previously module-level globals."""

    def __init__(self):
        self.seen_today: set[str] = set()
        self.today_pool: dict[str, Candidate] = {}
        self.last_today: str = ""
        self.list_presence: dict[str, int] = {}

    def reset_if_new_day(self, today_str: str | None = None) -> bool:
        today_str = today_str or date.today().isoformat()
        if today_str != self.last_today:
            self.seen_today.clear()
            self.today_pool.clear()
            self.list_presence.clear()
            self.last_today = today_str
            return True
        return False

    def mark_seen(self, symbol: str) -> bool:
        was_first = symbol not in self.seen_today
        self.seen_today.add(symbol)
        return was_first

    def update_list_presence(self, current_symbols: set[str]):
        for sym in list(self.list_presence.keys()):
            if sym in current_symbols:
                self.list_presence[sym] += 1
            else:
                del self.list_presence[sym]
        for sym in current_symbols:
            if sym not in self.list_presence:
                self.list_presence[sym] = 1

    def get_list_streak(self, symbol: str) -> int:
        return self.list_presence.get(symbol, 0)

    def update_pool(self, candidates: list[Candidate], now: datetime | None = None):
        now = now or datetime.now()
        current_syms = {c.stock.symbol for c in candidates}

        for c in candidates:
            if c.stock.symbol in self.today_pool and not self.today_pool[c.stock.symbol].is_stale:
                c.first_seen = self.today_pool[c.stock.symbol].first_seen
            else:
                c.first_seen = now.strftime("%H:%M")
            self.today_pool[c.stock.symbol] = c

        for sym in list(self.today_pool.keys()):
            c = self.today_pool[sym]
            if sym not in current_syms and not c.is_stale:
                c.is_stale = True
                c.stale_since = now.strftime("%H:%M")

    def get_stale_candidates(self, now: datetime | None = None) -> list[Candidate]:
        now = now or datetime.now()
        today_str = date.today().isoformat()
        stale_cutoff = now - timedelta(minutes=STALE_TIMEOUT_MINUTES)
        result: list[Candidate] = []
        for sym, c in list(self.today_pool.items()):
            if c.is_stale:
                stale_dt = datetime.strptime(f"{today_str} {c.stale_since}", "%Y-%m-%d %H:%M")
                if stale_dt >= stale_cutoff:
                    result.append(c)
                else:
                    self.today_pool.pop(sym, None)
        result.sort(key=lambda c: -c.score)
        return result

    def update_stale_quotes(self, stale: list[Candidate], market_caps: dict[str, dict]):
        for c in stale:
            cap_data = market_caps.get(c.stock.symbol)
            if cap_data and cap_data.get("current"):
                c.stock.current = cap_data["current"]
                c.stock.percent = cap_data["percent"]
