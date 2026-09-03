"""redesign_gate 真过滤 smoke 验证（local-only，用完即删，不进 git）。

用 scanner.db 副本构造候选，验证：
1. L1 未过候选 → recommendations.excluded=1 + 被 get_today_recommendations 过滤。
2. L1 通过候选 → excluded=0，仍在推荐集。
3. L0 池窄（R5<25）→ 当日全部撤销（excluded=1）。
不落盘真实库、不 commit（进程退出即回滚临时副本）。
"""
import shutil
import sqlite3
import sys
import tempfile
import types

sys.path.insert(0, ".")
import scanner.redesign_gate as rg
from scanner.db.queries import get_today_recommendations
from scanner.models import Candidate, KlineSummary, StockInfo

# 固定扫描小时=10，避免尾盘规则干扰 L1 分类判定
rg.now_beijing = lambda: types.SimpleNamespace(hour=10)

TD = "2099-01-01"


def mk(symbol, category, pct, trend):
    k = KlineSummary(
        trend=trend, accumulated_pct=0.0, volume_ratio=1.0,
        bottom_confirmed=False, score=50, dimensions={},
    )
    s = StockInfo(
        symbol=symbol, name=symbol, code=symbol[-6:], percent=pct,
        current=10.0, value=1e8, rank_change=0, rank=5,
    )
    return Candidate(stock=s, category=category, score=50, reason="", kline=k)


def main():
    src = "scanner.db"
    tmpdir = tempfile.mkdtemp(prefix="redesign_gate_")
    tmp = f"{tmpdir}/verify.db"
    shutil.copy(src, tmp)
    conn = sqlite3.connect(tmp)
    conn.execute("DELETE FROM recommendations WHERE date=?", (TD,))
    conn.commit()

    # 构造 R5=30（≥25，不触发 L0）的通过候选 + 2 只不过关，隔离验证 L1。
    base = [
        (TD, "10:00", f"SZ30010{i}", f"过{i}", "rebound", 50, 3.0, "企稳回升")
        for i in range(30)
    ]
    base += [
        (TD, "10:00", "SZ399001", "动量", "momentum", 50, 3.0, "加速主升"),
        (TD, "10:00", "SZ399002", "过热", "rebound", 50, 12.0, "企稳回升"),
    ]
    conn.executemany(
        "INSERT INTO recommendations (date,time,symbol,name,category,score,percent,trend) "
        "VALUES (?,?,?,?,?,?,?,?)",
        base,
    )
    conn.commit()

    cands = [mk(f"SZ30010{i}", "rebound", 3.0, "企稳回升") for i in range(30)]
    cands += [
        mk("SZ399001", "momentum", 3.0, "加速主升"),
        mk("SZ399002", "rebound", 12.0, "企稳回升"),
    ]
    blocked, l0, r5 = rg.apply_redesign_gate(cands, conn, TD)
    print(f"L1 blocked={len(blocked)} (应为2: 动量族+过热) R5={r5} (应为30) l0={l0} (应为False)")

    excl = dict(
        conn.execute("SELECT symbol, excluded FROM recommendations WHERE date=?", (TD,)).fetchall()
    )
    print("excluded 状态样例:", {k: excl.get(k) for k in ("SZ300101", "SZ399001", "SZ399002")})
    shown = {r["symbol"] for r in get_today_recommendations(conn, as_of=TD)}
    print("展示集数量:", len(shown), "(应为30，含 SZ300101，不含 SZ399001/002)")

    assert excl["SZ300101"] == 0, "通过候选不应被 excluded"
    assert excl["SZ399001"] == 1, "动量族应被 excluded"
    assert excl["SZ399002"] == 1, "过热应被 excluded"
    assert "SZ399001" not in shown and "SZ399002" not in shown, "不过关候选不应出现在展示集"
    assert "SZ300101" in shown, "通过候选应在展示集"
    print("✅ L1 真过滤验证通过")

    # L0 池窄：仅 2 只通过候选（<25）
    conn.execute("DELETE FROM recommendations WHERE date=?", (TD,))
    conn.commit()
    conn.executemany(
        "INSERT INTO recommendations (date,time,symbol,name,category,score,percent,trend) VALUES (?,?,?,?,?,?,?,?)",
        [(TD, "10:00", f"SZ30020{i}", f"p{i}", "rebound", 50, 2.0, "企稳回升") for i in range(2)],
    )
    conn.commit()
    cands2 = [mk(f"SZ30020{i}", "rebound", 2.0, "企稳回升") for i in range(2)]
    blocked2, l0b, r5b = rg.apply_redesign_gate(cands2, conn, TD)
    print(f"L0: R5={r5b} (应为2) l0={l0b} (应为True，池窄)")
    excl2 = dict(conn.execute("SELECT symbol, excluded FROM recommendations WHERE date=?", (TD,)).fetchall())
    print("L0 后 excluded:", excl2, "(应全为1)")
    assert l0b is True
    assert all(v == 1 for v in excl2.values())
    print("✅ L0 池窄整体撤销验证通过")

    conn.close()
    print("ALL OK")


if __name__ == "__main__":
    main()
