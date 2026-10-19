from datetime import datetime

from scanner.config import (
    EARLY_BONUS,
    LATE_BONUS,
    MARKET_ENV_STRONG,
    MARKET_ENV_WEAK,
    MAX_MARKET_CAP,
    MAX_STOCK_PRICE,
    MOMENTUM_MIN_SCORE,
    NEW_FACE_MIN_SCORE,
    REFRESH_INTERVAL,
    now_beijing,
)


class TestNowBeijing:
    def test_returns_datetime(self):
        assert isinstance(now_beijing(), datetime)


class TestConstants:
    def test_interval(self):
        assert isinstance(REFRESH_INTERVAL, int) and REFRESH_INTERVAL > 0

    def test_min_scores(self):
        assert all(isinstance(s, int) for s in [NEW_FACE_MIN_SCORE, MOMENTUM_MIN_SCORE])
        assert all(s > 0 for s in [NEW_FACE_MIN_SCORE, MOMENTUM_MIN_SCORE])

    def test_market_cap(self):
        assert MAX_MARKET_CAP > 0

    def test_stock_price(self):
        assert MAX_STOCK_PRICE > 0

    def test_time_bonus(self):
        assert isinstance(EARLY_BONUS, int)
        assert isinstance(LATE_BONUS, int)

    def test_market_env(self):
        assert MARKET_ENV_STRONG > 0
        assert MARKET_ENV_WEAK < 0


class TestLogDirEnvOverride:
    """`RTS_LOG_DIR` 可覆盖 `LOG_DIR`（桌面版打包用：安装态 `resources/` 只读）。

    与 `tests/test_database.py::TestDbPathEnvOverride` 同一形态、同一理由：常量在
    import 时求值，故必须 reload 才能观察环境变量生效；`LOG_DIR` 是扫描器**唯一**的
    运行时可写目录（CSV/锁/日志全在里面），改它等于是打包态能否启动的前提。
    """

    def test_env_override_takes_effect(self, tmp_path, monkeypatch):
        import importlib

        import scanner.config as cfgmod

        target = str(tmp_path / "logs-desktop")
        monkeypatch.setenv("RTS_LOG_DIR", target)
        reloaded = importlib.reload(cfgmod)
        assert target == reloaded.LOG_DIR, f"RTS_LOG_DIR 应覆盖 LOG_DIR，实际 {reloaded.LOG_DIR}"

        # 恢复原模块状态，避免污染其他测试（同 DB_PATH 那条的纪律）
        monkeypatch.delenv("RTS_LOG_DIR")
        importlib.reload(cfgmod)
        assert target != reloaded.LOG_DIR

    def test_absent_env_keeps_repo_default(self, monkeypatch):
        import importlib
        import os

        import scanner.config as cfgmod
        from scanner.config_core import BASE_DIR

        monkeypatch.delenv("RTS_LOG_DIR", raising=False)
        reloaded = importlib.reload(cfgmod)
        expected = os.path.join(BASE_DIR, "logs")
        assert expected == reloaded.LOG_DIR, "未设 RTS_LOG_DIR 时必须回落仓库根 logs/（改前行为）"
