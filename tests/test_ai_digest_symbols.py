"""`--symbols` 自选清单批量调研入口的测试（2026-11-04）。

不触网：代码规范化是纯函数；调研循环 mock `agent_mod.chat`。
"""

from __future__ import annotations

import pytest

from scanner.ai_digest.__main__ import (
    LABELS,
    _run_symbols,
    extract_symbols_from_text,
    normalize_user_symbols,
)

# ── 代码规范化 ────────────────────────────────────────────────────────────────


def test_normalize_accepts_bare_cnyb_codes_with_exchange_inference():
    assert normalize_user_symbols("300319,300666") == ["SZ300319", "SZ300666"]
    assert normalize_user_symbols("600519") == ["SH600519"]
    assert normalize_user_symbols("688981") == ["SH688981"]


def test_normalize_accepts_prefix_and_mixed_separators_case_insensitive():
    assert normalize_user_symbols("sz300319 SH600519、000001") == ["SZ300319", "SH600519", "SZ000001"]


def test_normalize_dedups_preserving_order():
    assert normalize_user_symbols("300319 SZ300319 300319") == ["SZ300319"]


@pytest.mark.parametrize(
    "bad",
    ["", "abc", "30031", "3003199", "BJ830799", "100319"],
)
def test_normalize_rejects_bad_tokens(bad):
    with pytest.raises(ValueError):
        normalize_user_symbols(bad)


def test_normalize_caps_at_50():
    codes = " ".join(f"300{i:03d}" for i in range(51))
    with pytest.raises(ValueError, match="50"):
        normalize_user_symbols(codes)
    assert len(normalize_user_symbols(" ".join(f"300{i:03d}" for i in range(50)))) == 50


# ── 提示词渲染容忍缺字段（agent 侧，被 --symbols 路径复用）──────────────────────


def test_candidate_lines_omit_missing_fields():
    from scanner.ai_digest.agent import _candidate_lines

    lines = _candidate_lines([{"symbol": "SZ300319", "name": "麦捷科技"}])
    assert lines == ["- SZ300319 麦捷科技"]
    assert "None" not in lines[0]

    lines2 = _candidate_lines([{"symbol": "SZ300770", "name": "新媒股份", "tier": "T2", "percent": 6.44}])
    assert lines2 == ["- SZ300770 新媒股份｜层 T2｜信号时涨幅 6.44%"]


# ── 自由文本抽码（剪贴板/文件/交互路径用）─────────────────────────────────────────


def test_extract_symbols_from_mixed_text():
    """名字/日期/涨跌幅混着的文本只抽出真代码，日期与零散数字被跳过。"""
    text = (
        "麦捷科技 300319 +6.44%\n新媒股份 SZ300770 现价 15.20 元\n日期 20261104，前值 601318.SH\n缺口的 830799 先跳过"
    )
    syms, skipped = extract_symbols_from_text(text)
    assert syms == ["SZ300319", "SZ300770", "SH601318"]
    assert "830799" in skipped and "202611" not in "".join(syms)


def test_extract_symbols_from_full_panel_only_reads_code_column():
    """终端按 c 复制的是整行表格；成交量「300000手」不能被读成股票代码。"""
    text = (
        "◆ v1 池选 — 新票优先·类别优先\n"
        "   #  代码         名称      涨幅     5日累计   现价   排名  板块     类别·先验\n"
        "   1  SZ300369    绿盟科技    +8.10%   +12.00%  23.50     7  半导体   RBD·17.9%\n"
        "◆ 沪深飙升 · 极有可能大涨（三段并列）\n"
        "   1  300133      华策影视  +2.10%    —     300000手   —   T2  —\n"
    )
    syms, skipped = extract_symbols_from_text(text)
    assert syms == ["SZ300369", "SZ300133"]
    assert skipped == []


def test_extract_symbols_still_scans_free_text_loosely():
    """无表格行的粘贴文本仍走宽松抽码（旧行为不变）。"""
    text = "麦捷科技 300319 +6.44%\n新媒股份 SZ300770 现价 15.20 元"
    assert extract_symbols_from_text(text)[0] == ["SZ300319", "SZ300770"]


def test_extract_dedups_and_returns_empty_pair_for_garbage():
    assert extract_symbols_from_text("没有任何代码") == ([], [])
    syms, _ = extract_symbols_from_text("300319 300319 SZ300319")
    assert syms == ["SZ300319"]


def test_extract_matches_normalize_for_pure_codes():
    """同一批代码，宽松抽码与严格解析结果一致（两条路径不漂移）。"""
    raw = "300319, SZ300770 600519"
    assert extract_symbols_from_text(raw)[0] == normalize_user_symbols(raw)


def test_run_symbols_file_source(monkeypatch, capsys, tmp_path):
    """--symbols-file：文件里混着名字也能抽出来。"""
    import argparse as _argparse

    import scanner.ai_digest.__main__ as cli
    import scanner.ai_digest.agent as agent_mod
    from scanner.db.schema import get_conn

    db = tmp_path / "scanner.db"
    prod = get_conn(str(db))
    prod.close()
    lst = tmp_path / "list.txt"
    lst.write_text("麦捷科技 300319\n新媒股份 SZ300770\n", encoding="utf-8")

    seen: dict = {}

    def fake_chat(cfg, messages, tools=None, timeout=None):
        seen["user"] = next(m for m in messages if m["role"] == "user")
        return {"content": "证据不足。", "tool_calls": []}

    monkeypatch.setattr("scanner.ai_digest.__main__._load_env", lambda: None)
    monkeypatch.setenv("MAGPIE_BASE_URL", "https://magpie.test/v1")
    monkeypatch.setenv("MAGPIE_API_KEY", "k-test")
    monkeypatch.setenv("MAGPIE_MODEL", "m-test")
    monkeypatch.setattr(agent_mod, "chat", fake_chat)
    monkeypatch.setattr(cli, "resolve_db_path", lambda: str(db))
    monkeypatch.setattr(cli, "_resolve_names", lambda syms: {s: s for s in syms})

    args = _argparse.Namespace(
        symbols=None,
        symbols_file=str(lst),
        clipboard=False,
        out=None,
        mode="research",
        max_rounds=8,
        max_calls=30,
        json=False,
    )
    assert _run_symbols(args) == 0
    assert "SZ300319" in seen["user"]["content"] and "SZ300770" in seen["user"]["content"]
    assert "清单文件" in capsys.readouterr().err


def test_run_symbols_over_limit_exits_3(monkeypatch, capsys):
    import argparse as _argparse

    args = _argparse.Namespace(
        symbols=",".join(f"300{i:03d}" for i in range(51)),
        symbols_file=None,
        clipboard=False,
        out=None,
        limit=None,
        mode="research",
        max_rounds=8,
        max_calls=30,
        json=False,
    )
    monkeypatch.setattr("scanner.ai_digest.__main__._load_env", lambda: None)
    assert _run_symbols(args) == 3
    assert "最多" in capsys.readouterr().out


def test_pick_py_maps_bare_codes_and_no_args(monkeypatch, tmp_path):
    """pick.py：裸代码 → --symbols；无参 → 交互模式；标志透传。"""
    import pick

    assert pick._build_argv(["300319", "300666"]) == ["--symbols", "300319,300666"]
    assert pick._build_argv([]) == ["--symbols"]
    assert pick._build_argv(["300319", "--out", "r.txt"]) == ["--symbols", "300319", "--out", "r.txt"]
    assert pick._build_argv(["--clipboard"]) == ["--clipboard"]


def test_save_report_writes_utf8_file(tmp_path):
    from scanner.ai_digest.__main__ import _save_report
    from scanner.ai_digest.agent import ResearchResult

    result = ResearchResult(text="结论：第一只证据最强。", rounds=3, tool_calls=[{"name": "get_kline", "args": {}}])
    out = tmp_path / "report.txt"
    _save_report(str(out), ["SZ300319"], {"SZ300319": "麦捷科技"}, result, ["9.99"])
    text = out.read_text(encoding="utf-8")
    assert "麦捷科技" in text and "结论" in text and "9.99" in text


# ── 工具层不得静默截断（2026-11-04）──────────────────────────────────────────


def test_quote_tool_reports_dropped_symbols_instead_of_silent_truncation(monkeypatch):
    """超过 20 只时必须**明说**哪几只没查 —— 静默截断会被模型读成「都查过了」。"""
    from scanner.ai_digest.research import ResearchContext, _tool_get_realtime_quote

    syms = [f"SZ300{i:03d}" for i in range(25)]

    class _Ctx(ResearchContext):
        def get_session(self):
            return object()

    import scanner.api as api_mod

    monkeypatch.setattr(api_mod, "fetch_hot_quotes_batch", lambda s, syms: {x: {"current": 1.0} for x in syms})
    out = _tool_get_realtime_quote({"symbols": syms}, _Ctx(conn=None))
    assert "未查" in out
    assert "SZ300024" in out  # 第 25 只必须被点名


def test_quote_tool_within_limit_has_no_truncation_note(monkeypatch):
    from scanner.ai_digest.research import ResearchContext, _tool_get_realtime_quote

    syms = [f"SZ300{i:03d}" for i in range(10)]

    class _Ctx(ResearchContext):
        def get_session(self):
            return object()

    import scanner.api as api_mod

    monkeypatch.setattr(api_mod, "fetch_hot_quotes_batch", lambda s, syms: {x: {"current": 1.0} for x in syms})
    out = _tool_get_realtime_quote({"symbols": syms}, _Ctx(conn=None))
    assert "未查" not in out


def test_symbol_cap_is_50_and_limit_truncates(monkeypatch):
    """上限 50（模型能分批调工具）；--limit N 取前 N 只。"""
    import argparse as _argparse
    import tempfile

    import scanner.ai_digest.__main__ as cli
    import scanner.ai_digest.agent as agent_mod
    from scanner.ai_digest.__main__ import MAX_SYMBOLS
    from scanner.db.schema import get_conn

    assert MAX_SYMBOLS == 50

    seen: dict = {}

    def fake_chat(cfg, messages, tools=None, timeout=None):
        seen["user"] = next(m for m in messages if m["role"] == "user")
        return {"content": "证据不足。", "tool_calls": []}

    tmpdb = tempfile.mkdtemp() + "/s.db"
    prod = get_conn(tmpdb)
    prod.close()

    monkeypatch.setattr("scanner.ai_digest.__main__._load_env", lambda: None)
    monkeypatch.setenv("MAGPIE_BASE_URL", "https://magpie.test/v1")
    monkeypatch.setenv("MAGPIE_API_KEY", "k-test")
    monkeypatch.setenv("MAGPIE_MODEL", "m-test")
    monkeypatch.setattr(agent_mod, "chat", fake_chat)
    monkeypatch.setattr(cli, "resolve_db_path", lambda: tmpdb)
    monkeypatch.setattr(cli, "_resolve_names", lambda syms: {s: s for s in syms})

    codes = ",".join(f"300{i:03d}" for i in range(30))
    args = _argparse.Namespace(
        symbols=codes,
        symbols_file=None,
        clipboard=False,
        out=None,
        limit=12,
        mode="research",
        max_rounds=8,
        max_calls=30,
        json=False,
    )
    assert _run_symbols(args) == 0
    user = seen["user"]["content"]
    assert "SZ300011" in user
    assert "SZ300012" not in user  # 第 13 只被 --limit 砍掉


def test_run_symbols_rejects_non_research_mode(monkeypatch, capsys):
    import argparse as _argparse

    args = _argparse.Namespace(symbols="300319", mode="analyze", max_rounds=8, max_calls=30, json=False)
    monkeypatch.setattr("scanner.ai_digest.__main__._load_env", lambda: None)
    assert _run_symbols(args) == 3
    assert "--symbols 只支持" in capsys.readouterr().out


def test_run_symbols_without_credentials_exits_1(monkeypatch, capsys):
    import argparse as _argparse

    args = _argparse.Namespace(symbols="300319,300666", mode="research", max_rounds=8, max_calls=30, json=False)
    monkeypatch.setattr("scanner.ai_digest.__main__._load_env", lambda: None)

    for k in ("MAGPIE_BASE_URL", "MAGPIE_API_KEY", "MAGPIE_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MAGPIE_BASE_URL", "")  # 形式占位，确保走 magpie_config 的空分支
    monkeypatch.delenv("MAGPIE_BASE_URL")
    assert _run_symbols(args) == 1
    out = capsys.readouterr().out
    assert "MAGPIE_BASE_URL" in out


def test_run_symbols_happy_path_reports_and_flags_fabricated_numbers(monkeypatch, capsys, tmp_path):
    """happy path：mock chat 直出结论；结论里编一个工具输出里没有的数字 → exit 5。"""
    import argparse as _argparse

    import scanner.ai_digest.__main__ as cli
    import scanner.ai_digest.agent as agent_mod
    from scanner.db.schema import get_conn

    # 临时库（空库即可，工具会自己说「无数据」）
    db = tmp_path / "scanner.db"
    prod = get_conn(str(db))
    prod.close()

    def fake_chat(cfg, messages, tools=None, timeout=None):
        # 找到 user 提示词断言来源文案
        user_msg = next(m for m in messages if m["role"] == "user")
        assert "用户指定的自选清单" in user_msg["content"]
        assert "麦捷科技" in user_msg["content"]
        assert "层 None" not in user_msg["content"]
        return {"content": "结论：SZ300319 现价 9999.99 元遥遥领先。", "tool_calls": []}

    monkeypatch.setattr("scanner.ai_digest.__main__._load_env", lambda: None)
    monkeypatch.setenv("MAGPIE_BASE_URL", "https://magpie.test/v1")
    monkeypatch.setenv("MAGPIE_API_KEY", "k-test")
    monkeypatch.setenv("MAGPIE_MODEL", "m-test")
    monkeypatch.setattr(agent_mod, "chat", fake_chat)
    monkeypatch.setattr("scanner.ai_digest.agent.chat", fake_chat, raising=False)
    monkeypatch.setattr(cli, "resolve_db_path", lambda: str(db))
    monkeypatch.setattr(cli, "_resolve_names", lambda syms: {"SZ300319": "麦捷科技", "SZ300666": "深圳华强"})

    args = _argparse.Namespace(symbols="300319,300666", mode="research", max_rounds=8, max_calls=30, json=False)
    assert _run_symbols(args) == 5  # 9999.99 不在任何工具输出里 → 幻觉巡检拦截
    out = capsys.readouterr().out
    assert "自选清单 · AI 调研" in out
    assert LABELS["research"] in out
    assert "SZ300319 麦捷科技" in out
    assert "9999.99" in out  # 幻觉巡检要把它点出来


def test_run_symbols_json_output(monkeypatch, capsys, tmp_path):
    import argparse as _argparse
    import json as _json

    import scanner.ai_digest.__main__ as cli
    import scanner.ai_digest.agent as agent_mod
    from scanner.db.schema import get_conn

    db = tmp_path / "scanner.db"
    prod = get_conn(str(db))
    prod.close()

    monkeypatch.setattr("scanner.ai_digest.__main__._load_env", lambda: None)
    monkeypatch.setenv("MAGPIE_BASE_URL", "https://magpie.test/v1")
    monkeypatch.setenv("MAGPIE_API_KEY", "k-test")
    monkeypatch.setenv("MAGPIE_MODEL", "m-test")
    monkeypatch.setattr(
        agent_mod, "chat", lambda cfg, m, tools=None, timeout=None: {"content": "证据不足。", "tool_calls": []}
    )
    monkeypatch.setattr(cli, "resolve_db_path", lambda: str(db))
    monkeypatch.setattr(cli, "_resolve_names", lambda syms: {s: s for s in syms})

    args = _argparse.Namespace(symbols="300319", mode="research", max_rounds=8, max_calls=30, json=True)
    assert _run_symbols(args) == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["source"] == "symbols"
    assert payload["symbols"] == [{"symbol": "SZ300319", "name": "SZ300319"}]
