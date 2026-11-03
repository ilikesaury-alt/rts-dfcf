"""终端「一键复制」（`scanner/terminal_copy.py`）单测。

覆盖三件事：
1. **文本正确性**：ANSI 去掉、`\r` 进度行规整、采集到的内容 = 打印出去的内容。
2. **不越界**：采集只还原 stdout、不改别的；剪贴板失败只返回 False 不抛。
3. **热键行为**：只有 `c`（不分大小写）触发复制；`None`（无输入）继续等。

⚠ 每个用例都**显式断言调用发生**（AGENTS.md：禁空断言）。
"""

from __future__ import annotations

import sys

import pytest

from scanner import terminal_copy as tc


@pytest.fixture(autouse=True)
def _reset_latest():
    tc._set_latest("")
    yield
    tc._set_latest("")


# ── 文本处理 ────────────────────────────────────────────────────────────


def test_strip_ansi_removes_color_and_cursor_codes():
    raw = "\x1b[91m+9.99%\x1b[0m  \x1b[2J\x1b[H 沪深飙升"
    assert tc.strip_ansi(raw) == "+9.99%   沪深飙升"


def test_strip_ansi_normalizes_carriage_return_progress_line():
    assert tc.strip_ansi("⏳ 30s ...\r⏳ 25s ...") == "⏳ 30s ...\n⏳ 25s ..."


def test_strip_ansi_removes_osc_title_sequences():
    assert tc.strip_ansi("\x1b]0;title\x07正文") == "正文"


# ── 采集 ────────────────────────────────────────────────────────────────


def test_capture_stdout_records_printed_text():
    original = sys.stdout
    with tc.capture_stdout() as recorded:
        print("创业板飙升榜监控")
        print("  300001 特变电")
    assert sys.stdout is original  # 采集结束必须还原
    assert "创业板飙升榜监控" in tc.latest_panel_text()
    assert "300001 特变电" in tc.latest_panel_text()
    assert any("创业板飙升榜监控" in chunk for chunk in recorded)


def test_capture_stdout_strips_ansi_before_storing():
    with tc.capture_stdout():
        print("\x1b[92m绿色\x1b[0m")
    assert tc.latest_panel_text().strip() == "绿色"


def test_capture_stdout_restores_stdout_on_exception():
    original = sys.stdout
    with pytest.raises(ValueError), tc.capture_stdout():
        print("半截输出")
        raise ValueError("boom")
    assert sys.stdout is original
    assert "半截输出" in tc.latest_panel_text()


def test_capture_stdout_does_not_clobber_devnull_downgrade():
    """stdout 被降级成 devnull 后，采集退出**不得**把它还原回真实 stdout。"""
    import os

    original = sys.stdout
    with tc.capture_stdout():
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    assert sys.stdout is not original


# ── 清单抽取 ────────────────────────────────────────────────────────────


def test_extract_stock_list_keeps_name_and_code_only():
    text = (
        "  ◆ v1 池选 — 新票优先·类别优先·排名升序·资金流降序\n"
        "   #  代码         名称      涨幅     5日累计   现价   排名  板块     评分 策略\n"
        "   1  SZ300369    绿盟科技    +8.10%   +12.00%  23.50     7  半导体   86  新面孔\n"
        "   2  SZ300133    华策影视    +6.20%    +3.10%  11.20    12  传媒     72  动量\n"
    )
    assert tc.extract_stock_list(text) == "绿盟科技 300369\n华策影视 300133"


def test_extract_stock_list_handles_hot_rows_without_market_prefix():
    text = (
        "  — 榜内飙升 —\n"
        "   1  300369      绿盟科技   +9.90%   +15.00%  23.50   +3   120手   1.2亿  2.10  8.0   120  半导体  88.0   —   ★3\n"
    )
    assert tc.extract_stock_list(text) == "绿盟科技 300369"


def test_extract_stock_list_dedupes_and_ignores_non_row_lines():
    text = (
        "  创业板飙升榜监控  (2026-11-03 10:00:00)\n"
        "  ◆ 飞书过门（本轮 3 只通过严格过滤 · 下一张卡片将推此集合）\n"
        "    池选  绿盟科技 威尔高\n"
        "   1  SZ300369  绿盟科技  +8.10%\n"
        "   2  SZ300133  华策影视  +6.20%\n"
        "   3  300369     绿盟科技  +9.00%\n"  # 与第 1 行同一只票（不同区块）
    )
    assert tc.extract_stock_list(text) == "绿盟科技 300369\n华策影视 300133"


def test_extract_stock_list_empty_for_non_table_text():
    assert tc.extract_stock_list("  创业板飙升榜监控  (2026-11-03 10:00:00)\n") == ""


# ── 剪贴板 ──────────────────────────────────────────────────────────────


def test_copy_to_clipboard_returns_false_for_empty_text(monkeypatch):
    called = []
    monkeypatch.setattr(tc, "_copy_windows", lambda text: called.append(text) or True)
    assert tc.copy_to_clipboard("") is False
    assert called == []  # 空文本不该真去写剪贴板


def test_copy_to_clipboard_prefers_native_api(monkeypatch):
    fallback = []
    monkeypatch.setattr(tc, "_copy_windows", lambda text: True)
    monkeypatch.setattr(tc, "_copy_via_command", lambda text: fallback.append(text) or True)
    assert tc.copy_to_clipboard("面板") is True
    assert fallback == []  # 原生路径成功 ⇒ 不该再兜底


def test_copy_to_clipboard_falls_back_to_command(monkeypatch):
    monkeypatch.setattr(tc, "_copy_windows", lambda text: False)
    monkeypatch.setattr(tc, "_copy_via_command", lambda text: True)
    assert tc.copy_to_clipboard("面板") is True


def test_copy_to_clipboard_returns_false_when_all_paths_fail(monkeypatch):
    monkeypatch.setattr(tc, "_copy_windows", lambda text: False)
    monkeypatch.setattr(tc, "_copy_via_command", lambda text: False)
    assert tc.copy_to_clipboard("面板") is False


def test_copy_to_clipboard_survives_native_api_exception(monkeypatch):
    def _boom(text: str) -> bool:
        raise OSError("clipboard unavailable")

    monkeypatch.setattr(tc, "_copy_windows", _boom)
    monkeypatch.setattr(tc, "_copy_via_command", lambda text: True)
    assert tc.copy_to_clipboard("面板") is True  # 原生失败 ⇒ 走兜底，不抛


# ── 热键 ────────────────────────────────────────────────────────────────


def test_hotkey_loop_copies_on_c_key():
    keys = iter([None, "c"])
    copied = []

    def _read_key() -> str | None:
        try:
            return next(keys)
        except StopIteration:
            raise

    tc._hotkey_loop(_read_key, lambda: copied.append(True))
    assert copied == [True]


def test_hotkey_loop_ignores_other_keys():
    keys = iter(["x", "1", "C", "\x00"])
    copied = []

    def _read_key() -> str | None:
        try:
            return next(keys)
        except StopIteration:
            raise

    tc._hotkey_loop(_read_key, lambda: copied.append(True))
    assert copied == [True]  # 只有 'C'（大写）触发


def test_handle_copy_request_reports_when_nothing_captured(capsys, monkeypatch):
    monkeypatch.setattr(tc, "copy_to_clipboard", lambda text: True)
    tc._handle_copy_request()
    out = capsys.readouterr().out
    assert "还没有可复制" in out


def test_handle_copy_request_copies_names_and_codes_only(capsys, monkeypatch):
    tc._set_latest(
        "  1  SZ300369    绿盟科技    +8.10%   +12.00%   23.50   7   半导体   86   新面孔\n"
        "  2  SZ300133    华策影视    +6.20%    +3.10%   11.20  12   传媒     72   动量\n"
    )
    copied = []
    monkeypatch.setattr(tc, "copy_to_clipboard", lambda text: copied.append(text) or True)
    tc._handle_copy_request()
    assert copied == ["绿盟科技 300369\n华策影视 300133"]
    out = capsys.readouterr().out
    assert "已复制 2 只股票" in out


def test_handle_copy_request_reports_failure(capsys, monkeypatch):
    tc._set_latest("  1  300369  绿盟科技  +8.10%")
    monkeypatch.setattr(tc, "copy_to_clipboard", lambda text: False)
    tc._handle_copy_request()
    out = capsys.readouterr().out
    assert "复制失败" in out


@pytest.mark.skipif(sys.platform != "win32", reason="热键仅 Windows 实现")
def test_install_hotkey_requires_tty(monkeypatch):
    class _NoTty:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(sys, "stdin", _NoTty())
    assert tc.install_hotkey() is False
