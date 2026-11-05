"""B 段「AI 调研数据源」的历史回溯检验（2026-11-03）。

ruff: noqa: S311  # bootstrap 重采样用 random，非密码学用途


## 为什么需要它

用户在 2026-11-03 提出「把 AI 调研工具的数据源变成扫描器筛选条件」。本仓铁律是
observe-first + 样本外验证，但**等影子期要 30 个交易日，用户等不了**。于是先回答一个
更基本的问题：**这些数据源里，哪些能回溯到历史样本上做检验？**

| 数据源 | 可回溯? | 判据 |
|---|---|---|
| 公告（东财 `np-anotice-stock`） | ✅ | 每条带 `notice_date` ⇒ 可只取「信号日之前 N 日」的公告（不用当时不知道的信息） |
| 资金流趋势（东财 fflow `klt=101`） | ✅ | 121 个交易日的日频序列 ⇒ 按信号日切片取「信号日之前的 5/20 日累计」 |
| 龙虎榜（东财 `RPT_DAILYBILLBOARD_DETAILSNEW`） | ✅ | 全历史，带 `TRADE_DATE` |
| 分时（雪球 minute.json） | ❌ | **只有当日** ⇒ 只能前瞻性攒，无法回溯 |

## 口径（与 `rule_validate` / `scripts/offboard_nextday.py` 对齐）

- 样本 = `offboard_launch_log` 里 `next_day_pct` 非空的行（信号日收盘 → 次一交易日收盘）；
- 命中 = 次日 ≥7%（系统唯一目标函数）；
- **配对检验**：同一天内「命中的 Δ」按日等权，逐日配对 bootstrap 95% CI。
  这是唯一可信的口径 —— 不做时间切分比较（实测基线本身在窗口间漂移 4.3pp）。

## 它明确不做什么

- **不改任何阈值、不写生产表**：本脚本只读 `scanner.db`，采集结果写 `logs/` 下的 JSON；
- **不给结论**：只报 Δ 与 CI。「某条件 Δ>0 且 CI 不跨 0」才叫证据；
  全跨 0 就是「当前样本量检不出」——这时**不能**说「无效」，只能说「不可区分」。

用法：
    python scripts/shadow_feature_backtest.py                 # 全量（520 票 × 3 接口）
    python scripts/shadow_feature_backtest.py --limit-symbols 40   # 先跑小样本看耗时
    python scripts/shadow_feature_backtest.py --no-fetch      # 只用已缓存数据重算检验
"""

from __future__ import annotations

import argparse
import json
import random  # noqa: S311 - bootstrap 重采样，非密码学用途
import sqlite3
import statistics as st
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date as _date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scanner.ai_digest.em_data import fetch_announcements, fetch_dragon_tiger, fetch_intraday_fund_flow  # noqa: E402

CACHE = Path(__file__).resolve().parent.parent / "logs" / "shadow_feature_cache.json"

#: 公告关键词 → 事件类别（用来把「有公告」细分成「有利好公告」这种可检验的信号）
ANN_KEYWORDS = {
    "业绩预告": ("业绩预告", "预增", "预盈", "业绩快报"),
    "重组并购": ("重组", "收购", "并购", "重大资产", "股权转让", "要约"),
    "减持": ("减持", "清仓式减持"),
    "回购": ("回购", "增持"),
    "中标订单": ("中标", "重大合同", "框架协议"),
    "监管风险": ("问询", "关注函", "警示", "立案", "处罚", "退市"),
}


def _vol_ratio_expr(conn: sqlite3.Connection) -> str:
    """量比列表达式：**上报原值尺度**（跨 2026-11-05 的量比去偏改动可比）。

    2026-11-06 起 `offboard_launch_log.vol_ratio` 存的是**去偏值**（决策口径），
    上报原值另存 `vol_ratio_raw`（m024）⇒ 量比分桶必须用原值尺度，否则同一个桶
    在改动前后含义不同。旧行 `vol_ratio_raw` 为 NULL ⇒ 其 `vol_ratio` 本就是原值
    ⇒ coalesce 正确回退。老库没有该列 ⇒ 退回裸列。
    """
    try:
        conn.execute("SELECT vol_ratio_raw FROM offboard_launch_log LIMIT 1").fetchone()
    except sqlite3.Error:
        return "vol_ratio"
    return "coalesce(vol_ratio_raw, vol_ratio)"


def load_samples(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT date, symbol, tier, percent, accum_5d, "
        f"{_vol_ratio_expr(conn)} AS vol_ratio, "
        "main_pct, amount, float_cap, next_day_pct "
        "FROM offboard_launch_log WHERE next_day_pct IS NOT NULL ORDER BY date"
    ).fetchall()
    keys = ("date", "symbol", "tier", "percent", "accum_5d", "vol_ratio", "main_pct", "amount", "float_cap", "ndp")
    return [dict(zip(keys, r, strict=True)) for r in rows]


def collect(symbols: list[str], workers: int, cache: dict) -> dict:
    """按票采集三类可回溯数据（公告 / 资金流趋势 / 龙虎榜），带磁盘缓存。"""
    todo = [s for s in symbols if s not in cache]
    print(f"  需采集 {len(todo)} 只（已缓存 {len(symbols) - len(todo)}）", flush=True)
    done = 0
    t0 = time.time()

    def one(sym: str) -> tuple[str, dict]:
        return (
            sym,
            {
                "ann": fetch_announcements(sym, 50),
                "flow": fetch_intraday_fund_flow(sym),
                "dtb": fetch_dragon_tiger(sym, 20),
            },
        )

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(one, s): s for s in todo}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                k, v = fut.result()
            except Exception as e:  # noqa: BLE001 - 采集脚本：单票失败不该中断整批
                print(f"    [!] {sym} 采集异常 {type(e).__name__}", flush=True)
                continue
            cache[k] = v
            done += 1
            if done % 25 == 0:
                el = time.time() - t0
                print(
                    f"    {done}/{len(todo)}  用时 {el:.0f}s  预计剩余 {el / done * (len(todo) - done):.0f}s",
                    flush=True,
                )
    return cache


def _days_between(a: str, b: str) -> int | None:
    """`a - b` 的天数（YYYY-MM-DD）；解析失败返回 None。"""
    try:
        ya, ma, da = (int(x) for x in a.split("-")[:3])
        yb, mb, db = (int(x) for x in b.split("-")[:3])
        return (_date(ya, ma, da) - _date(yb, mb, db)).days
    except (ValueError, TypeError):
        return None


def build_features(sample: dict, data: dict) -> dict:
    """把采集到的数据 → 信号日**当时可知**的特征（严禁使用信号日之后的信息）。"""
    d = sample["date"]
    feats: dict[str, object] = {}

    # ① 公告：信号日**之前 10 日**内的公告（含当日），以及按事件类别细分
    ann_win, kinds = 0, set()
    for a in data.get("ann") or []:
        ad = str(a.get("date") or "")
        delta = _days_between(d, ad) if ad else None
        if delta is None:
            continue
        if 0 <= delta <= 10:  # 公告日 ≤ 信号日（负数 = 未来，剔除）
            ann_win += 1
            title = str(a.get("title") or "")
            for kind, kws in ANN_KEYWORDS.items():
                if any(k in title for k in kws):
                    kinds.add(kind)
    feats["ann_10d"] = ann_win
    for kind in ANN_KEYWORDS:
        feats[f"ann_{kind}"] = 1 if kind in kinds else 0

    # ② 资金流趋势：信号日**及之前** 5 / 20 个交易日的累计主力净额
    flow = [p for p in (data.get("flow") or []) if str(p.get("date") or "") <= d]
    for n in (5, 20):
        window = flow[-n:]
        feats[f"flow_main_{n}d"] = sum(p["main"] for p in window) if window else None
        feats[f"flow_xl_{n}d"] = sum(p["xl"] for p in window) if window else None
        feats[f"flow_pos_{n}d"] = sum(1 for p in window if p["main"] > 0) if window else None

    # ③ 龙虎榜：信号日**之前 20 日**是否上榜
    dtb_recent = [
        r
        for r in (data.get("dtb") or [])
        if (delta := _days_between(d, str(r.get("date") or ""))) is not None and 0 <= delta <= 20
    ]
    feats["dtb_20d"] = 1 if dtb_recent else 0
    feats["dtb_net_20d"] = sum(float(r.get("net") or 0) for r in dtb_recent) if dtb_recent else None

    return feats


def paired_ci(rows: list[dict], predicate, *, n_boot: int = 20000) -> dict:
    """按日配对 bootstrap：同日「过滤后命中率 − 当日全量命中率」。

    过滤后不足 2 只的交易日剔除（单只票的日命中率非 0 即 100，噪声压倒信号）。
    """
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["date"], []).append(r)

    def hit_rate(rs: list[dict]) -> float:
        return sum(1 for r in rs if r["ndp"] >= 7) / len(rs) * 100

    diffs = []
    for rs in by_day.values():
        sub = [r for r in rs if predicate(r)]
        if len(sub) < 2:
            continue
        diffs.append(hit_rate(sub) - hit_rate(rs))
    if not diffs:
        return {"days": 0, "delta": 0.0, "lo": 0.0, "hi": 0.0}
    random.seed(42)
    means = []
    for _ in range(n_boot):
        s = [random.choice(diffs) for _ in diffs]  # noqa: S311 - bootstrap 重采样
        means.append(st.mean(s))
    means.sort()
    return {
        "days": len(diffs),
        "delta": round(st.mean(diffs), 2),
        "lo": round(means[int(0.025 * n_boot)], 2),
        "hi": round(means[int(0.975 * n_boot)], 2),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="B 段 AI 调研数据源的历史回溯检验（只读，不改阈值）")
    ap.add_argument("--limit-symbols", type=int, default=0, help="只采前 N 只票（试跑用）")
    ap.add_argument("--workers", type=int, default=4, help="采集并发（东财有限频，默认 4）")
    ap.add_argument("--no-fetch", action="store_true", help="不联网，只用已有缓存重算检验")
    args = ap.parse_args(argv)

    if sys.platform == "win32":
        rec = getattr(sys.stdout, "reconfigure", None)
        if callable(rec):
            rec(encoding="utf-8")

    import scanner.config as cfg

    conn = sqlite3.connect(str(cfg.resolve_db_path()))
    samples = load_samples(conn)
    conn.close()
    symbols = sorted({s["symbol"] for s in samples})
    print(f"样本 {len(samples)} 行 / {len(symbols)} 只票 / {len({s['date'] for s in samples})} 个交易日")

    cache: dict = {}
    if CACHE.exists():
        try:
            cache = json.loads(CACHE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cache = {}
    if args.limit_symbols:
        symbols = symbols[: args.limit_symbols]
    if not args.no_fetch:
        print("采集（公告 50 条 / 资金流趋势 121 日 / 龙虎榜 20 条）：")
        cache = collect(symbols, args.workers, cache)
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        print(f"  缓存已写 {CACHE}（{len(cache)} 只）")

    rows = []
    for s in samples:
        data = cache.get(s["symbol"])
        if not data:
            continue
        r = dict(s)
        r.update(build_features(s, data))
        rows.append(r)
    print(f"\n可检验样本 {len(rows)} 行（有采集数据的票）")

    base = [r["ndp"] for r in rows]
    print(f"基线：命中 {sum(1 for v in base if v >= 7) / len(base) * 100:.1f}%（n={len(base)}）\n")

    print("=== 配对检验（按日等权，Δ = 过滤后命中率 − 当日全量命中率）===")
    tests = [
        ("有公告(10日内)", lambda r: r["ann_10d"] > 0),
        ("公告-业绩预告", lambda r: r["ann_业绩预告"] == 1),
        ("公告-重组并购", lambda r: r["ann_重组并购"] == 1),
        ("公告-减持", lambda r: r["ann_减持"] == 1),
        ("公告-中标订单", lambda r: r["ann_中标订单"] == 1),
        ("公告-监管风险", lambda r: r["ann_监管风险"] == 1),
        ("无任何公告(10日)", lambda r: r["ann_10d"] == 0),
        ("20日内上过龙虎榜", lambda r: r["dtb_20d"] == 1),
        ("主力5日净流入>0", lambda r: (r["flow_main_5d"] or 0) > 0),
        ("主力20日净流入>0", lambda r: (r["flow_main_20d"] or 0) > 0),
        ("主力20日净流入>5000万", lambda r: (r["flow_main_20d"] or 0) > 5e7),
        ("超大单20日净流入>0", lambda r: (r["flow_xl_20d"] or 0) > 0),
        ("20日净流入天数>=12", lambda r: (r["flow_pos_20d"] or 0) >= 12),
    ]
    out = []
    for name, fn in tests:
        res = paired_ci(rows, fn)
        if res["days"] == 0:
            print(f"  {name:22} 参与日 0（过滤后每天不足 2 只）")
            continue
        sig = "跨0=不可区分" if res["lo"] <= 0 <= res["hi"] else "★不跨0"
        print(
            f"  {name:22} 日={res['days']:3d}  Δ={res['delta']:+6.2f}pp  CI[{res['lo']:+.2f},{res['hi']:+.2f}]  {sig}"
        )
        out.append({"name": name, **res, "ci_crosses_zero": res["lo"] <= 0 <= res["hi"]})

    # 组合：公告 + 资金流（两条最有先验的方向）
    print("\n=== 组合 ===")
    combos = [
        ("有公告 且 主力5日净流入>0", lambda r: r["ann_10d"] > 0 and (r["flow_main_5d"] or 0) > 0),
        ("有公告 且 20日净流入>0", lambda r: r["ann_10d"] > 0 and (r["flow_main_20d"] or 0) > 0),
        ("重组/中标 且 主力5日>0", lambda r: (r["ann_重组并购"] or r["ann_中标订单"]) and (r["flow_main_5d"] or 0) > 0),
    ]
    for name, fn in combos:
        res = paired_ci(rows, fn)
        if res["days"] == 0:
            print(f"  {name:26} 参与日 0")
            continue
        sig = "跨0=不可区分" if res["lo"] <= 0 <= res["hi"] else "★不跨0"
        print(
            f"  {name:26} 日={res['days']:3d}  Δ={res['delta']:+6.2f}pp  CI[{res['lo']:+.2f},{res['hi']:+.2f}]  {sig}"
        )
        out.append({"name": name, **res, "ci_crosses_zero": res["lo"] <= 0 <= res["hi"]})

    report = Path(__file__).resolve().parent.parent / "logs" / "shadow_feature_backtest.json"
    report.write_text(
        json.dumps(
            {
                "samples": len(rows),
                "baseline_hit": round(sum(1 for v in base if v >= 7) / len(base) * 100, 2),
                "results": out,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n结果已写 {report}")
    print("⚠ 本脚本只报 Δ 与 CI，不下「该不该做筛选条件」的结论 —— 那是 rule_validate 的职责，而 B 段是它的盲区。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
