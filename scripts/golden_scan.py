#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""`orchestrator.scan_with_raw` 的黄金样本（golden master）工具 —— 第 3 步的前置安全网。

## 为什么需要它

`scan_with_raw`（507 行 / 圈复杂度 95）是项目的**主数据通路**，每轮扫描都走它。
但 `tests/test_orchestrator.py` 对它**本身零覆盖**——那个文件测的是 `score_stock` /
`fetch_all_klines` / `parallel_fetch` / `_update_excluded_marks` 等**辅助函数**，
没有一个用例调用 `scan_with_raw`。

于是出现一个死结：

- 想拆它（第 3 步，A 组收益最高）→ 需要证明「拆完输出逐字段一致」；
- 想证明 → 需要一个可复现的对比基准；
- 要基准 → 得先能**离线、确定性地**跑一遍它。

本工具就是那把尺子：从 `scanner.db` 重建某一天的真实榜单输入，用离线桩 adapter
跑一次 `scan_with_raw`，把 `ScanResult` 规范化成 JSON 快照；改代码前后各跑一次，
逐字段对比。

## 怎么做到确定性（三个钉子）

1. **钉死时间**：`now_beijing()` 被 30 个模块使用（`today`、`first_seen`、
   `compute_time_bonus`、K 线 TTL 全依赖它）。把**所有已加载的 `scanner.*` 模块**
   上的 `now_beijing` 覆盖为固定时刻——沿用 `rule_validate` 的「override 必须传播」
   教训：只改 `scanner.config.now_beijing` 对快照式导入的消费方无效。
2. **钉死网络**：离线桩 adapter，全部方法返回确定值（不联网）。
   另外把三个**函数内部 import 后调用**的外部数据源
   （`market_extra.collect_market_extra` / `fundamentals.collect_fund_risk` /
   `concept.compute_driving_concepts`）替换为确定性桩——它们在 `scan_with_raw`
   里是 `try/except EXTERNAL_FAILURES` 包裹的 fail-open 分支，不钉死会真的联网。
3. **钉死数据库**：把 `scanner.db` 复制到临时文件再跑，落库不污染生产库；
   K 线走 DB 缓存（`fetch_all_klines` 先查 `daily_kline`），所以用的是**真实历史数据**。

## 用法

    python scripts/golden_scan.py                      # 跑最新交易日并与基线对比
    python scripts/golden_scan.py --write               # （重新）生成基线
    python scripts/golden_scan.py --date 2026-09-10     # 指定日期
    python scripts/golden_scan.py --json                # 机器可读输出
    python scripts/golden_scan.py --diff                # 打印首个不一致字段的上下文

## 退出码

    0 = 与基线逐字段一致（或 --write 成功）
    1 = 与基线不一致 —— **这就是「等价变换被破坏」的信号**
    2 = 运行失败（无样本 / 异常）
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

if sys.platform == "win32":
    _r = getattr(sys.stdout, "reconfigure", None)
    if callable(_r):
        _r(encoding="utf-8")

BASELINE_DIR = _PROJECT_ROOT / "scripts" / "golden"
DEFAULT_AT = "14:30"  # 固定盘中时刻：compute_time_bonus / 分时兜底分支都吃它


# ── 1. 从 DB 重建真实榜单输入 ──


def load_raw(conn: sqlite3.Connection, date: str) -> list[dict]:
    """用 appearances + daily_kline 收盘价，重建某日的 biaosheng 原始输入。

    `filter_gem_stocks` 需要的字段：symbol / code / name / percent / current /
    value / rank_change / rank / turnover_rate / source_tag。
    `appearances` 存的是「上榜快照」，缺 current（现价）——从当日 K 线收盘价取，
    否则 MAX_STOCK_PRICE 硬门会因 current=0 整体跳过（`s.current > 0 and ...`），
    黄金样本就少覆盖一条真实分支。
    """
    closes = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT symbol, close FROM daily_kline WHERE date = ?", (date,)
        ).fetchall()
    }
    rows = conn.execute(
        "SELECT symbol, name, rank, percent, value FROM appearances "
        "WHERE date = ? ORDER BY rank",
        (date,),
    ).fetchall()
    raw: list[dict] = []
    for symbol, name, rank, percent, value in rows:
        code = symbol[2:] if len(symbol) > 6 else symbol  # SZ300561 → 300561
        raw.append({
            "symbol": symbol,
            "code": code,
            "name": name or "",
            "percent": float(percent or 0.0),
            "current": float(closes.get(symbol) or 0.0),
            "value": float(value or 0.0),
            "rank_change": 0,
            "rank": int(rank) if rank is not None else 0,
            "turnover_rate": 0.0,
            "source_tag": "golden",
        })
    return raw


# ── 2. 离线桩 adapter ──


class OfflineAdapter:
    """确定性 adapter：不联网，全部返回固定/空值。

    覆盖 `scan_with_raw` 直接调用的 4 个方法，以及 `kline_fetch` / `intraday_fetch`
    间接用到的 `fetch_kline` / `fetch_minute`。返回空 → 走各自的 DB 缓存 / fail-open
    分支（这些分支**本来就该被黄金样本覆盖**，它们正是历史上静默 bug 的高发区）。
    """

    name = "golden-offline"

    def fetch_market_caps_batch(self, symbols):  # noqa: D102
        return {}

    def fetch_market_index(self):  # noqa: D102
        return 0.0

    def get_market_index_meta(self):  # noqa: D102
        return (0.0, None, "golden-offline")

    def fetch_kline(self, symbol, days):  # noqa: D102
        return []

    def fetch_minute(self, symbol):  # noqa: D102
        return []


# ── 3. 钉死时间（override 必须传播）──


def pin_now(date: str, at: str) -> dt.datetime:
    """把所有已加载的 scanner.* 模块的 now_beijing 覆盖为固定时刻。

    只改 `scanner.config.now_beijing` 是无效的——30 个模块里绝大多数是
    `from scanner.config import now_beijing`（快照式导入，值已绑进各自命名空间）。
    这与 `rule_validate._patch_consumers` 处理的是同一个陷阱。
    """
    hh, mm = (int(x) for x in at.split(":"))
    fixed = dt.datetime.fromisoformat(date).replace(hour=hh, minute=mm, second=0, microsecond=0)
    patched = 0
    for name, mod in list(sys.modules.items()):
        if not name.startswith("scanner") or mod is None:
            continue
        if hasattr(mod, "now_beijing"):
            # 属性名固定但**目标模块是动态的**（遍历 sys.modules），这正是 setattr 的用途
            setattr(mod, "now_beijing", lambda _f=fixed: _f)  # noqa: B010
            patched += 1
    return fixed


def stub_external_sources() -> None:
    """把外部数据源替换为确定性桩，并**断网**，保证可复现。

    两层保险：

    1. 替换 `scan_with_raw` 里**函数内 import** 后调用的三个数据源
       （`market_extra` / `fundamentals` / `concept`）——只改模块属性即可，
       因为它们的 import 发生在调用时。
    2. 装一个 **requests 总闸**：任何 `requests.get/post` 直接抛
       `ConnectionError`。`utils.EXTERNAL_FAILURES` 含 `RequestException`，
       所以各处的 fail-open 分支会正常接住——**既不联网，又把降级路径真实跑了一遍**。

    第 2 层是必需的：第 1 层漏掉了 `core_themes.find_core_theme_dips` →
    `concept.fetch_stock_boards` 这条链（实测 09-09 的样本真的打到了
    emweb.securities.eastmoney.com）。与其逐个补漏，不如把网断掉。
    """
    import requests

    import scanner.concept as concept_mod
    import scanner.fundamentals as fundamentals_mod
    import scanner.market_extra as market_extra_mod

    market_extra_mod.collect_market_extra = lambda conn, symbols, **kw: {}
    fundamentals_mod.collect_fund_risk = lambda conn, symbols, **kw: {}
    concept_mod.compute_driving_concepts = lambda conn, symbols, surge_pool=None, **kw: {}

    def _no_net(*_a, **_kw):
        raise requests.exceptions.ConnectionError("golden_scan: 网络已禁用（离线可复现模式）")

    requests.get = _no_net  # type: ignore[assignment]
    requests.post = _no_net  # type: ignore[assignment]
    requests.put = _no_net  # type: ignore[assignment]
    requests.Session.request = (  # type: ignore[assignment]
        lambda self, method, *a, **kw: _no_net()
    )


# ── 4. ScanResult 规范化 ──


def _canon(obj: Any, _depth: int = 0) -> Any:
    """递归规范化为可比较的 JSON 结构（dataclass → dict，float 定点化，集合排序）。"""
    if _depth > 12:
        return "<deep>"
    if obj is None or isinstance(obj, (bool, str, int)):
        return obj
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):  # NaN / inf
            return f"<{obj}>"
        return round(obj, 6)
    if isinstance(obj, (list, tuple)):
        return [_canon(x, _depth + 1) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _canon(v, _depth + 1) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
    if isinstance(obj, (set, frozenset)):
        return sorted(_canon(x, _depth + 1) for x in obj)
    if hasattr(obj, "__dataclass_fields__"):
        import dataclasses

        return {
            f.name: _canon(getattr(obj, f.name), _depth + 1)
            for f in dataclasses.fields(obj)
            if f.name != "_candidate"  # 反向引用，递归陷阱
        }
    if hasattr(obj, "__dict__"):
        return {k: _canon(v, _depth + 1) for k, v in sorted(vars(obj).items()) if k != "_candidate"}
    return repr(obj)


def canonicalize(result: Any) -> dict:
    """ScanResult → 规范化 dict（各桶按 symbol 排序，消除列表顺序噪声）。"""
    out = _canon(result)
    for key in ("new_faces", "momentum", "rebound", "short_term", "comeback", "pool_picks"):
        if isinstance(out.get(key), list):
            out[key] = sorted(out[key], key=lambda c: str((c or {}).get("stock", {}).get("symbol", "")))
    return out


# 空桶 = 该代码路径**没有被黄金样本走到** → 拆它时这个基线保护不了你。
# 实测（2026-09-13）：comeback 在所有可用日期恒为 0（evaluate_comeback 依赖
# adapter 实时行情，离线模式下必然失败）；new_face / momentum 多数日期为 0~1
# （真实数据使然——多数票此前已上榜，不构成 new_face）。
COVERAGE_BUCKETS = ("new_faces", "momentum", "rebound", "short_term", "comeback", "pool_picks")


def _coverage_warnings(summary: dict) -> list[str]:
    empty = [b for b in COVERAGE_BUCKETS if not summary.get(b)]
    warn = []
    if empty:
        warn.append(
            f"空桶 {empty} —— 这些路径本次没走到，基线**保护不到**它们。"
            f"拆这部分时本工具无法证明等价，需另补针对性单测。"
        )
    if len(empty) == len(COVERAGE_BUCKETS):
        warn.append("全部桶为空：样本没有产出任何候选，基线基本无效。")
    return warn


def _summary(result: Any) -> dict:
    """人类可读摘要（先看这个，再看逐字段 diff）。"""
    return {
        "new_faces": len(getattr(result, "new_faces", []) or []),
        "momentum": len(getattr(result, "momentum", []) or []),
        "rebound": len(getattr(result, "rebound", []) or []),
        "short_term": len(getattr(result, "short_term", []) or []),
        "comeback": len(getattr(result, "comeback", []) or []),
        "pool_picks": len(getattr(result, "pool_picks", []) or []),
        "gem_stocks": len(getattr(result, "gem_stocks", []) or []),
        "filtered_large_cap": getattr(result, "filtered_large_cap", 0),
        "quotes": len(getattr(result, "current_quotes", {}) or {}),
    }


# ── 5. 主流程 ──


def run_once(db_path: Path, date: str, at: str, keep_db: Path | None = None) -> tuple[Any, dict]:
    """离线跑一次 scan_with_raw，返回 (ScanResult, 元信息)。"""
    tmp_db = Path(tempfile.mkdtemp(prefix="golden_")) / "scanner.db"
    shutil.copy2(db_path, tmp_db)

    # DB 路径由 config.DB_PATH 在**导入时**从环境变量读定，且 init_db() 不接受参数
    # —— 所以必须在首次 import scanner.* 之前设定，否则会连到生产库。
    os.environ["RTS_DB_PATH"] = str(tmp_db)

    from scanner.database import init_db
    from scanner.orchestrator import scan_with_raw

    conn = init_db()
    try:
        raw = load_raw(conn, date)
        if not raw:
            raise SystemExit(f"[中止] {date} 在 appearances 里没有样本")
        pin_now(date, at)
        stub_external_sources()
        result = scan_with_raw(raw, conn, OfflineAdapter())
        meta = {
            "date": date,
            "at": at,
            "raw_n": len(raw),
            "raw_sha256": hashlib.sha256(
                json.dumps(raw, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()[:16],
            "db_sha256": hashlib.sha256(db_path.read_bytes()).hexdigest()[:16],
        }
        if keep_db is not None:
            shutil.copy2(tmp_db, keep_db)
        return result, meta
    finally:
        conn.close()
        shutil.rmtree(tmp_db.parent, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="scan_with_raw 黄金样本（第 3 步等价变换的安全网）")
    p.add_argument("--date", default=None, help="榜单日期（默认取 appearances 最新日）")
    p.add_argument("--at", default=DEFAULT_AT, help=f"钉死的盘中时刻（默认 {DEFAULT_AT}）")
    p.add_argument("--db", default=str(_PROJECT_ROOT / "scanner.db"))
    p.add_argument("--write", action="store_true", help="（重新）生成基线快照")
    p.add_argument("--json", action="store_true")
    p.add_argument("--diff", action="store_true", help="打印首个不一致字段的上下文")
    p.add_argument("--keep-db", default=None, help="保留本次跑的临时库（调试用）")
    return p


def _find_first_diff(a: Any, b: Any, path: str = "") -> tuple[str, Any, Any] | None:
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            got = _find_first_diff(a.get(k), b.get(k), f"{path}.{k}" if path else k)
            if got:
                return got
        return None
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return (f"{path}[len]", len(a), len(b))
        for i, (x, y) in enumerate(zip(a, b, strict=False)):
            got = _find_first_diff(x, y, f"{path}[{i}]")
            if got:
                return got
        return None
    if a != b:
        return (path, a, b)
    return None


def main() -> int:
    args = build_parser().parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[中止] 找不到数据库：{db_path}")
        return 2

    if args.date is None:
        ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        try:
            args.date = ro.execute("SELECT MAX(date) FROM appearances").fetchone()[0]
        finally:
            ro.close()
        if not args.date:
            print("[中止] appearances 为空")
            return 2

    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    baseline_path = BASELINE_DIR / f"scan_result_{args.date}_{args.at.replace(':', '')}.json"

    try:
        result, meta = run_once(
            db_path, args.date, args.at,
            keep_db=Path(args.keep_db) if args.keep_db else None,
        )
    except SystemExit as e:
        print(str(e))
        return 2
    except Exception as e:  # noqa: BLE001 - 黄金样本工具要能看到真实异常
        print(f"[中止] 运行失败：{type(e).__name__}: {e}")
        return 2

    current = {"meta": meta, "summary": _summary(result), "result": canonicalize(result)}

    if args.write:
        baseline_path.write_text(
            json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        print(f"[写入] 基线 {baseline_path}")
        print(f"       样本 {meta['raw_n']} 只 / 榜单 {meta['date']} {meta['at']} / "
              f"raw_sha256={meta['raw_sha256']}")
        print(f"       摘要 {current['summary']}")
        for w in _coverage_warnings(current["summary"]):
            print(f"       ⚠ {w}")
        return 0

    if not baseline_path.exists():
        print(f"[中止] 基线不存在：{baseline_path}")
        print(f"       先跑：python scripts/golden_scan.py --date {args.date} --write")
        return 2

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

    if args.json:
        same = baseline.get("result") == current["result"]
        print(json.dumps({
            "same": same,
            "baseline_summary": baseline.get("summary"),
            "current_summary": current["summary"],
            "meta": meta,
            "baseline_meta": baseline.get("meta"),
        }, ensure_ascii=False, indent=2))
        return 0 if same else 1

    print("=" * 78)
    print(f"scan_with_raw 黄金样本对比 · 榜单 {args.date} {args.at}")
    print("=" * 78)
    print(f"  输入：{meta['raw_n']} 只（raw_sha256={meta['raw_sha256']}）")
    if baseline.get("meta", {}).get("raw_sha256") != meta["raw_sha256"]:
        print("  ⚠ 输入指纹与基线不同（DB 或重建逻辑变了）—— 此时对比结果不可信，"
              "应先用 --write 重建基线")
    print(f"  基线摘要 {baseline.get('summary')}")
    print(f"  本次摘要 {current['summary']}")
    for w in _coverage_warnings(current["summary"]):
        print(f"  ⚠ {w}")

    if baseline.get("result") == current["result"]:
        print("\n  ✅ 逐字段一致（等价变换未被破坏）")
        return 0

    print("\n  ❌ 与基线不一致")
    if baseline.get("summary") != current["summary"]:
        print("     —— 摘要层就不同，先核对各桶数量")
    got = _find_first_diff(baseline.get("result"), current["result"])
    if got:
        path, a, b = got
        print(f"     首个不一致字段：{path}")
        sa, sb = json.dumps(a, ensure_ascii=False)[:300], json.dumps(b, ensure_ascii=False)[:300]
        print(f"       基线：{sa}")
        print(f"       本次：{sb}")
    if args.diff:
        print("\n  —— 完整 diff（基线 vs 本次）——")
        _print_diff(baseline.get("result"), current["result"])
    return 1


def _print_diff(a: Any, b: Any, path: str = "", depth: int = 0, limit: list[int] | None = None) -> None:
    if limit is None:
        limit = [40]
    if limit[0] <= 0:
        return
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            _print_diff(a.get(k), b.get(k), f"{path}.{k}" if path else k, depth + 1, limit)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            print(f"  {path}[len]: {len(a)} → {len(b)}")
            limit[0] -= 1
        for i, (x, y) in enumerate(zip(a, b, strict=False)):
            _print_diff(x, y, f"{path}[{i}]", depth + 1, limit)
    elif a != b:
        print(f"  {path}: {json.dumps(a, ensure_ascii=False)[:160]} → "
              f"{json.dumps(b, ensure_ascii=False)[:160]}")
        limit[0] -= 1


if __name__ == "__main__":
    raise SystemExit(main())
