"""--supervise 熔断回归测试。

锁定两个不变量：
1. 连续短崩溃（uptime < SUPERVISE_CRASH_BASELINE_SECONDS）→ 累计达阈值后熔断 return 1；
2. 长跑后偶发崩溃（uptime >= 基线）→ consecutive_crashes 清零、永不熔断，继续容忍重启。

通过 mock subprocess.Popen + time.time/sleep 避免真实拉起子进程与退避等待。
"""

import subprocess
import time

import unified_scanner as us


class _FakeProc:
    def __init__(self, code):
        self._code = code

    def poll(self):
        return self._code

    def kill(self):
        pass

    def wait(self, timeout=None):
        pass


class _BreakLoop(Exception):
    """测试用：在指定重启轮次后跳出 _supervise 的无限循环。"""


def test_circuit_breaks_after_consecutive_short_crashes(monkeypatch):
    crash_codes = [1, 1, 1, 1, 1]
    idx = {"i": 0}

    def fake_popen(cmd):
        c = crash_codes[idx["i"]]
        idx["i"] += 1
        return _FakeProc(c)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    # uptime 恒为 0（start 与 uptime 取同一时刻）→ 每次都判为"短崩溃"
    monkeypatch.setattr(time, "time", lambda: 1000.0)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    rc = us._supervise(interval=60, no_feishu=True)

    assert rc == 1  # 连续 5 次短崩溃 → 熔断
    # 恰好启动 N 次后停止，不浪费更多轮次污染
    assert idx["i"] == us.SUPERVISE_MAX_CONSECUTIVE_CRASHES


def test_no_circuit_break_on_normal_exit(monkeypatch):
    idx = {"i": 0}

    def fake_popen(cmd):
        idx["i"] += 1
        return _FakeProc(0)  # 正常退出码

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(time, "time", lambda: 1000.0)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    rc = us._supervise(interval=60, no_feishu=True)

    assert rc == 0  # 正常退出，不重启
    assert idx["i"] == 1  # 只启动了 1 次


def test_long_running_crash_not_circuit_broken(monkeypatch):
    calls = {"n": 0}

    def fake_popen(cmd):
        calls["n"] += 1
        return _FakeProc(1)  # 每次都崩溃

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    # 每次 time.time 调用返回单调递增大值 → 每轮 uptime 恒 >> 基线（长跑后崩溃）
    tc = {"n": 0}

    def fake_time():
        tc["n"] += 1
        return 1000.0 + 10000.0 * tc["n"]

    monkeypatch.setattr(time, "time", fake_time)

    def fake_sleep(s):
        if calls["n"] >= 6:
            raise _BreakLoop()  # 第 6 轮重启前跳出，验证前 5 轮未熔断

    monkeypatch.setattr(time, "sleep", fake_sleep)

    try:
        us._supervise(interval=60, no_feishu=True)
    except _BreakLoop:
        pass

    # 已重启 5 轮且未熔断（否则会在第 5 次 return 1，calls 停在 5）
    assert calls["n"] == 6


def test_should_restart_contract():
    assert us._should_restart(0) is False  # 正常/Ctrl+C 不重启
    assert us._should_restart(1) is True   # 非 0 视为崩溃需重启
    assert us._should_restart(-9) is True  # 假死强杀同样重启
