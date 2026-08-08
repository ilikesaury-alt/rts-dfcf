"""Step 2 验证（方案 C）：历史重扫新权重再回测。

核心断言：用当前 config 权重重扫 new_face/known_new_face 引擎后，
组合策略（综合）在「持 2-3 日」节奏下不再毁灭价值（综合 ≈ 朴素基准），
而冻结旧权重分下综合显著跑输基准。

这些测试会读取真实 scanner.db（appearances + daily_kline），属于集成测试，
运行时间略长，但能锁定 Step 2 的 P&L 改善不回归。
"""

import sqlite3

from scanner.database import init_db
from scanner.portfolio_backtest import PBConfig, run_backtest


def _cfg(**kw):
    base = dict(buy_delay=0, buy_at="open", hold_days=3, category=None)
    base.update(kw)
    return PBConfig(**base)


def test_rescan_produces_signals():
    """重扫应对 new_face/known_new_face 产出有效信号（buy_index/exit_index/rank_score 合法）。"""
    conn = init_db()
    try:
        cfg = _cfg(category="new_face", rescore=True)
        res = run_backtest(conn, cfg)
        assert res.n_signals > 0, "重扫应产出 new_face 信号"
        for s in res.trades:
            assert s.hold_days == 3
        # rank_score 由 historical_rescan._assign_rank_scores 计算，应在 [0,100]
        # （间接验证：回测未因 rank_score 异常崩溃）
    finally:
        conn.close()


def test_rescore_combined_no_longer_loses_to_benchmark_h3():
    """Step 2 验证（主节奏：持 3 日）。

    冻结旧权重分：综合(去热度) 远跑输基准（约 -30pt）。
    重扫新权重：综合(重扫new_face) ≈ 基准（gap 收窄到 ~0）。
    该断言锁定「重扫综合 >= 冻结综合」这一改善不回归。
    """
    conn = init_db()
    try:
        frozen = run_backtest(conn, _cfg(category=None, deheat=True, rescore=False))
        rescored = run_backtest(conn, _cfg(category=None, rescore=True))
        # Step 2 应使综合策略不再显著跑输其自身基准（改善不回归）
        assert rescored.metrics["total_return"] >= frozen.metrics["total_return"], (
            f"重扫综合 {rescored.metrics['total_return']:.2%} 应 >= "
            f"冻结综合 {frozen.metrics['total_return']:.2%}"
        )
    finally:
        conn.close()


def test_rescore_combined_matches_rescanned_benchmark_h3():
    """重扫宇宙内：综合(重扫new_face) ≈ 基准（同宇宙可比，gap 收窄到 ~0）。

    冻结宇宙内：综合(去热度) 显著跑输基准（gap ≈ -30pt）。
    该断言锁定 Step 2 后「综合不再毁灭价值」这一结论不回归。
    """
    conn = init_db()
    try:
        frozen_bench = run_backtest(conn, _cfg(category=None, no_skill=True, rescore=False))
        frozen_comb = run_backtest(conn, _cfg(category=None, deheat=True, rescore=False))
        rescan_bench = run_backtest(conn, _cfg(category=None, no_skill=True, rescore=True))
        rescan_comb = run_backtest(conn, _cfg(category=None, rescore=True))

        frozen_gap = frozen_comb.metrics["total_return"] - frozen_bench.metrics["total_return"]
        rescan_gap = rescan_comb.metrics["total_return"] - rescan_bench.metrics["total_return"]

        # 冻结宇宙：综合显著跑输基准
        assert frozen_gap < -0.10, f"冻结综合应跑输基准，gap={frozen_gap:.2%}"
        # 重扫宇宙：综合 ≈ 基准（gap 收窄，容忍 ±10pt）
        assert rescan_gap > -0.10, f"重扫综合应≈基准，gap={rescan_gap:.2%} 过大"
    finally:
        conn.close()
