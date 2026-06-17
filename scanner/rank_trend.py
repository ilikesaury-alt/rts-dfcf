class RankTracker:
    def __init__(self):
        self._history: list[dict[str, int]] = []

    def update(self, current_ranks: dict[str, int]):
        self._history.append(current_ranks)
        if len(self._history) > 5:
            self._history.pop(0)

    def streak_score(self, symbol: str) -> int:
        if len(self._history) < 2:
            return 0
        ranks = []
        for snap in self._history:
            if symbol in snap:
                ranks.append(snap[symbol])
        if len(ranks) < 2:
            return 0
        recent = ranks[-1]
        prev = ranks[-2]
        diff = prev - recent
        score = 0
        if diff >= 5:
            score += 6
        elif diff >= 2:
            score += 3
        elif diff < -3:
            score -= 4
        if len(ranks) >= 3 and diff >= 2:
            score += 4
        return score


tracker = RankTracker()


def update_rank_history(current_ranks: dict[str, int]):
    tracker.update(current_ranks)


def rank_streak_score(symbol: str) -> int:
    return tracker.streak_score(symbol)
