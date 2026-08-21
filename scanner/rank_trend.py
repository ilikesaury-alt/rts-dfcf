"""榜单排名轨迹追踪（rank trajectory）。

tracker 是**进程级单例**（模块加载时构造一次，跨扫描持续存在），
持有每轮扫描的榜单排名快照，用于计算「排名轨迹分」（连续上榜/上升/下降）。
这是其设计目的——轨迹必须跨扫描累积，故刻意放在模块级而非每次扫描重建。

生命周期：
- 每个交易日扫描循环每轮调用 update_rank_history 追加当前排名；
- candidate_pool 在**交易日切换**时调用 tracker.reset() 清空（避免跨日轨迹串味）；
- enhancer 经 rank_trajectory_score 读取，单线程扫描循环下无并发竞争（非线程安全是
  有意的——扫描进程本身是串行的，无需加锁）。

外部统一经 get_rank_tracker() 取单例，避免散落 `from scanner.rank_trend import tracker`
直接持有可变全局对象（设计审查 P2-13）。
"""

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


def get_rank_tracker() -> RankTracker:
    """返回进程级 RankTracker 单例（显式持有者，P2-13）。

    扫描进程串行访问，无需加锁；外部统一经此函数取单例，而非直接 import tracker。
    """
    return tracker


def update_rank_history(current_ranks: dict[str, int]):
    tracker.update(current_ranks)


def rank_trajectory_score(symbol: str) -> int:
    return tracker.trajectory_score(symbol)
