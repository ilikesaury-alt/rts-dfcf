class RankTracker:
    def __init__(self):
        self._history: list[dict[str, int]] = []

    def reset(self):
        self._history.clear()

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

    def trajectory_score(self, symbol: str) -> int:
        if len(self._history) < 2:
            return 0
        ranks = []
        for snap in self._history:
            if symbol in snap:
                ranks.append(snap[symbol])
        if len(ranks) < 2:
            return 0
        improvements = 0
        for i in range(len(ranks) - 1):
            if ranks[i] - ranks[i + 1] > 0:
                improvements += 1
        if improvements >= len(ranks) - 1:
            return 8
        if improvements >= 3:
            return 6
        if improvements >= 2:
            return 4
        if improvements >= 1:
            return 2
        return -2


tracker = RankTracker()


def update_rank_history(current_ranks: dict[str, int]):
    tracker.update(current_ranks)


def rank_streak_score(symbol: str) -> int:
    return tracker.streak_score(symbol)


def rank_trajectory_score(symbol: str) -> int:
    return tracker.trajectory_score(symbol)
