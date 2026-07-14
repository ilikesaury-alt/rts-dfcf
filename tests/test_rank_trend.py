from scanner.rank_trend import RankTracker, rank_streak_score, rank_trajectory_score, update_rank_history


class TestRankTrackerReset:
    def test_reset_clears_history(self):
        t = RankTracker()
        t.update({"A": 10})
        t.update({"A": 5})
        t.reset()
        assert t.streak_score("A") == 0
        assert t.trajectory_score("A") == 0


class TestRankTrackerUpdate:
    def test_history_window_limit(self):
        t = RankTracker()
        for i in range(7):
            t.update({"A": 100 - i * 10})
        assert len(t._history) == 5
        assert t._history[0] == {"A": 80}
        assert t._history[-1] == {"A": 40}


class TestStreakScore:
    def test_insufficient_snapshots(self):
        t = RankTracker()
        t.update({"A": 50})
        assert t.streak_score("A") == 0

    def test_insufficient_ranks_for_symbol(self):
        t = RankTracker()
        t.update({"B": 50})
        t.update({"B": 40})
        assert t.streak_score("A") == 0

    def test_rank_improve_ge5(self):
        t = RankTracker()
        t.update({"A": 100})
        t.update({"A": 90})
        assert t.streak_score("A") == 6

    def test_rank_improve_2_to_4(self):
        t = RankTracker()
        t.update({"A": 100})
        t.update({"A": 97})
        assert t.streak_score("A") == 3

    def test_rank_decline_gt3(self):
        t = RankTracker()
        t.update({"A": 90})
        t.update({"A": 100})
        assert t.streak_score("A") == -4

    def test_rank_small_change_no_bonus(self):
        t = RankTracker()
        t.update({"A": 100})
        t.update({"A": 99})
        assert t.streak_score("A") == 0

    def test_streak_ge3_with_improve_ge2(self):
        t = RankTracker()
        t.update({"A": 100})
        t.update({"A": 97})
        t.update({"A": 94})
        assert t.streak_score("A") == 7

    def test_streak_ge3_with_large_improve(self):
        t = RankTracker()
        t.update({"A": 100})
        t.update({"A": 90})
        t.update({"A": 80})
        assert t.streak_score("A") == 10

    def test_streak_ge3_without_enough_improve(self):
        t = RankTracker()
        t.update({"A": 100})
        t.update({"A": 99})
        t.update({"A": 98})
        assert t.streak_score("A") == 0

    def test_only_2_snapshots_no_streak_bonus(self):
        t = RankTracker()
        t.update({"A": 100})
        t.update({"A": 97})
        assert t.streak_score("A") == 3


class TestTrajectoryScore:
    def test_insufficient_snapshots(self):
        t = RankTracker()
        t.update({"A": 50})
        assert t.trajectory_score("A") == 0

    def test_insufficient_ranks_for_symbol(self):
        t = RankTracker()
        t.update({"B": 50})
        t.update({"B": 40})
        assert t.trajectory_score("A") == 0

    def test_all_improvements(self):
        t = RankTracker()
        t.update({"A": 100})
        t.update({"A": 90})
        t.update({"A": 80})
        assert t.trajectory_score("A") == 8

    def test_improvements_ge3(self):
        t = RankTracker()
        t.update({"A": 100})
        t.update({"A": 95})
        t.update({"A": 90})
        t.update({"A": 85})
        assert t.trajectory_score("A") == 8

    def test_improvements_3_not_all(self):
        t = RankTracker()
        t.update({"A": 100})
        t.update({"A": 95})
        t.update({"A": 85})
        t.update({"A": 90})
        t.update({"A": 80})
        assert t.trajectory_score("A") == 6

    def test_improvements_2(self):
        t = RankTracker()
        t.update({"A": 100})
        t.update({"A": 95})
        t.update({"A": 90})
        t.update({"A": 95})
        assert t.trajectory_score("A") == 4

    def test_improvements_1(self):
        t = RankTracker()
        t.update({"A": 100})
        t.update({"A": 95})
        t.update({"A": 110})
        assert t.trajectory_score("A") == 2

    def test_no_improvements(self):
        t = RankTracker()
        t.update({"A": 80})
        t.update({"A": 90})
        t.update({"A": 100})
        assert t.trajectory_score("A") == -2

    def test_trajectory_2_snapshots_all_improve(self):
        t = RankTracker()
        t.update({"A": 100})
        t.update({"A": 90})
        assert t.trajectory_score("A") == 8


class TestModuleLevelFunctions:
    def test_update_and_streak(self):
        update_rank_history({"MODFUNC_A": 100})
        update_rank_history({"MODFUNC_A": 90})
        assert rank_streak_score("MODFUNC_A") == 6

    def test_update_and_trajectory(self):
        update_rank_history({"MODFUNC_B": 100})
        update_rank_history({"MODFUNC_B": 90})
        update_rank_history({"MODFUNC_B": 80})
        assert rank_trajectory_score("MODFUNC_B") == 8

    def test_streak_unknown_symbol(self):
        assert rank_streak_score("NONEXISTENT_X") == 0

    def test_trajectory_unknown_symbol(self):
        assert rank_trajectory_score("NONEXISTENT_Y") == 0
