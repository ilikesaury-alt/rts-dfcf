#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""数据源替换实验：把「某日 v1 产出」当作飙升榜输入，跑一遍完整筛选管线。

回答的问题：**如果飙升榜推送的不是实时飙升票，而是昨天筛出来的 v1 票，
今天这套门会放行哪几只、拒掉哪几只、各死在哪个环节。**

做法（与 `scripts/golden_scan.py` 同源的离线回放机制，注入点在 `scan_with_raw` 的
第一个参数 `raw`——它就是飙升榜的原始输入）：

1. 从 `recommendations(from_date)` 取候选票（`--scope` 决定范围）
2. 用腾讯批量行情取 `to_date` 的实时行情，充当榜单的 percent / current / turnover_rate
   （飙升榜本身只给 关注度+涨幅+排名，价格字段由行情补全——与真实链路一致）
3. `rank` / `value` 优先沿用 from_date 的 `appearances` 真实值（人气值不编造），
   不在榜的票 value 记 0
4. 钉死 `now_beijing` 到 to_date，跑 `orchestrator.scan_with_raw(raw, conn, adapter)`
5. 归因五段漏斗：身份门 → 价格门 → 市值门 → 硬过滤（读临时库 scan_rejections）→ 入选桶

**安全**：生产库以 `mode=ro` 只读打开，`sqlite3.backup()` 复制到临时文件后
所有写入（appearances / pool_log / scan_rejections / scan_quality_log …）
全部落在临时库，生产 `scanner.db` 零改动。

**失真提示**（结果解读必读）：榜单宽度被压到 ~15 条（真实 ~100 条）⇒ 依赖榜宽的
逻辑（情绪分 `compute_surge_sentiment`、板块聚簇）会偏离；`session_state` 是全新进程
状态 ⇒「连续在榜天数」从 1 起算，与生产进程的累积态不同。

用法：
    python scripts/replay_source_swap.py                      # v1 五桶 → 今天
    python scripts/replay_source_swap.py --scope v1+pool
    python scripts/replay_source_swap.py --scope all
    python scripts/replay_source_swap.py --offline            # adapter 离线（K线走缓存）
    python scripts/replay_source_swap.py --json               # 机器可读

退出码：0 正常 / 1 数据不足 / 2 运行失败。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
from collections import Counter
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# v1 五桶（与 orchestrator 的 new_faces/momentum/rebound/short_term 同源）
SCOPE_V1 = ("momentum", "new_face", "known_new_face", "rebound", "short_term")
SCOPE_V1_POOL = SCOPE_V1 + ("pool_pick",)

DEFAULT_AT = "10:30"


# ── 1. 候选池（来自 from_date 的落库产出）──


def load_candidates(conn: sqlite3.Connection, from_date: str, scope: str, to_date: str | None = None) -> list[dict]:
    """从 recommendations 取候选票；scope=all 取当日全部落库（含 excluded）。

    scope=real 是**对照组**：直接取 to_date 当日真实上榜的票，即不替换数据源跑一遍，
    用来回答「v1 规则在真实榜宽下的产出量」——把回放实验的榜宽失真（15 条 vs ~100 条）
    与「这批票本身不行」两个原因区分开。
    """
    if scope == "real":
        if not to_date:
            return []
        rows = conn.execute(
            "SELECT DISTINCT symbol, name FROM appearances WHERE date = ? ORDER BY symbol",
            (to_date,),
        ).fetchall()
        return [{"symbol": s, "name": n or "", "categories": ["今日真榜"], "score": None} for s, n in rows]
    if scope == "all":
        rows = conn.execute(
            "SELECT symbol, name, category, score FROM recommendations WHERE date = ? ORDER BY score DESC",
            (from_date,),
        ).fetchall()
    else:
        cats = SCOPE_V1 if scope == "v1" else SCOPE_V1_POOL
        q = ",".join("?" * len(cats))
        rows = conn.execute(
            f"SELECT symbol, name, category, score FROM recommendations "
            f"WHERE date = ? AND excluded = 0 AND category IN ({q}) ORDER BY score DESC",
            (from_date, *cats),
        ).fetchall()
    out: list[dict] = []
    seen: set[str] = set()
    for symbol, name, category, score in rows:
        if symbol in seen:  # 同票多类别（如斯迪克在 momentum+short_term）只留一条，记全部来源
            for item in out:
                if item["symbol"] == symbol:
                    item["categories"].append(category)
            continue
        seen.add(symbol)
        out.append({"symbol": symbol, "name": name or "", "categories": [category], "score": score})
    return out


def fetch_quotes(symbols: list[str]) -> dict[str, dict]:
    """腾讯批量行情：一次请求拿现价/涨跌幅/换手/市值。失败返回空 dict（调用方降级）。"""
    import requests

    out: dict[str, dict] = {}
    for i in range(0, len(symbols), 50):
        chunk = symbols[i : i + 50]
        try:
            r = requests.get("http://qt.gtimg.cn/q=" + ",".join(s.lower() for s in chunk), timeout=10)
            r.encoding = "gbk"
        except Exception as e:  # noqa: BLE001 - 网络失败降级为空，不阻断回放
            print(f"  [!] 行情抓取失败（{type(e).__name__}）：{e}", file=sys.stderr)
            continue
        for line in r.text.split(";"):
            if "~" not in line or '="' not in line:
                continue
            f = line.split('="')[1].split("~")
            if len(f) < 46:
                continue
            code = f[2]
            market = "SH" if code.startswith("6") else "SZ"
            out[market + code] = {
                "name": f[1],
                "current": _f(f[3]),
                "prev_close": _f(f[4]),
                "percent": _f(f[32]),
                "turnover_rate": _f(f[38]),
                "circ_market_cap": _f(f[44]) * 1e8,  # 亿元 → 元
                "market_cap": _f(f[45]) * 1e8,
            }
    return out


def _f(v) -> float:
    try:
        x = float(v)
        return x if x == x else 0.0
    except (TypeError, ValueError):
        return 0.0


def fetch_quotes_cached(symbols: list[str], path: str | None) -> dict[str, dict]:
    """带快照缓存的行情抓取：文件里已有的票直接复用，只补抓缺失的，然后写回。

    为什么要缓存：盘中行情在分钟级漂移，如果两次对照运行（如"昨日 v1 池" vs
    "今日真榜"）各自现抓，同一只票拿到的涨幅就不同——那就无法判断结果差异是
    「数据源不同」还是「行情时点不同」造成的。缓存把两次运行钉在同一份行情快照上。
    """
    cache: dict[str, dict] = {}
    p = Path(path) if path else None
    if p and p.exists():
        try:
            cache = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
    missing = [s for s in symbols if s not in cache]
    if missing:
        cache.update(fetch_quotes(missing))
    if p:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return cache


def build_raw(cands: list[dict], quotes: dict[str, dict], conn: sqlite3.Connection, from_date: str) -> list[dict]:
    """构造飙升榜形态的原始输入。

    rank/value 沿用 from_date appearances 的真实榜单值（不编造人气值）；
    percent/current/turnover_rate 用 to_date 实时行情（榜单核心字段）。
    rank 缺失时按今日涨幅降序补（榜单按热度排，此处用涨幅代理并标注）。
    """
    hist = {
        r[0]: (r[1], r[2])
        for r in conn.execute("SELECT symbol, rank, value FROM appearances WHERE date = ?", (from_date,))
    }
    raw: list[dict] = []
    for c in cands:
        sym = c["symbol"]
        q = quotes.get(sym, {})
        hr = hist.get(sym)
        raw.append(
            {
                "symbol": sym,
                "code": sym[2:] if len(sym) > 6 else sym,
                "name": q.get("name") or c["name"],
                "percent": q.get("percent", 0.0),
                "current": q.get("current", 0.0),
                "value": float(hr[1] or 0.0) if hr else 0.0,
                "rank_change": 0,
                "rank": int(hr[0]) if hr else 0,
                "turnover_rate": q.get("turnover_rate", 0.0),
                "source_tag": "replay",
            }
        )
    # rank 缺失者按今日涨幅降序补齐排名（1 起）
    missing = [r for r in raw if not r["rank"]]
    for i, r in enumerate(sorted(missing, key=lambda x: -x["percent"]), 1):
        r["rank"] = i
    return raw


# ── 2. 离线确定性支撑（与 golden_scan 同款，避免跨脚本耦合这里自带一份）──


class OfflineAdapter:
    """确定性 adapter：不联网，全部返回空 → 走 DB 缓存 / fail-open 分支。"""

    name = "replay-offline"

    def fetch_market_caps_batch(self, symbols):
        return {}

    def fetch_market_index(self):
        return 0.0

    def get_market_index_meta(self):
        return (0.0, None, "replay-offline")

    def fetch_kline(self, symbol, days):
        return []

    def fetch_minute(self, symbol):
        return []


def pin_now(date: str, at: str):
    """把所有已加载 scanner.* 模块的 now_beijing 覆盖为固定时刻。

    只改 `scanner.config.now_beijing` 无效——绝大多数消费方是
    `from scanner.config import now_beijing`（快照式导入，值已绑进各自命名空间）。
    """
    import datetime as dt

    hh, mm = (int(x) for x in at.split(":"))
    fixed = dt.datetime.fromisoformat(date).replace(hour=hh, minute=mm, second=0, microsecond=0)
    for name, mod in list(sys.modules.items()):
        if name.startswith("scanner") and mod is not None and hasattr(mod, "now_beijing"):
            setattr(mod, "now_beijing", lambda _f=fixed: _f)  # noqa: B010
    return fixed


def stub_external_sources() -> None:
    """把外部数据源换成确定性桩 + 断网（fail-open 分支照常跑）。"""
    import requests

    import scanner.concept as concept_mod
    import scanner.fundamentals as fundamentals_mod
    import scanner.market_extra as market_extra_mod

    market_extra_mod.collect_market_extra = lambda conn, symbols, **kw: {}
    fundamentals_mod.collect_fund_risk = lambda conn, symbols, **kw: {}
    concept_mod.compute_driving_concepts = lambda conn, symbols, surge_pool=None, **kw: {}

    def _no_net(*_a, **_kw):
        raise requests.exceptions.ConnectionError("replay_source_swap: 离线模式已断网")

    requests.get = _no_net  # type: ignore[assignment]
    requests.post = _no_net  # type: ignore[assignment]
    requests.put = _no_net  # type: ignore[assignment]


def _backup_db(src: Path, dst: Path) -> None:
    """用 sqlite3.backup API 复制（WAL 库直接 cp 会拿到 checkpoint 前的旧页）。"""
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


# ── 3. 归因 ──


def classify(
    cands: list[dict],
    gems: set[str],
    buckets: dict[str, list[str]],
    rejects: dict[str, str],
    quotes: dict[str, dict],
    pool_info: dict[str, dict] | None = None,
) -> list[dict]:
    """把每只候选票归到漏斗的某一段。

    归因顺序即真实执行顺序：身份门 → 价格门 → 市值门 → v2 排雷 → v1/v2 硬过滤
    → 入选桶 → 过门未入选。**v2 池选本身不淘汰**（score 恒 0，只做语义标注），
    所以「入选 pool_picks」只代表进了池，不等于通过了选股逻辑——真正的强筛选
    是 v1 五桶（momentum/new_face/rebound/short_term）。
    """
    from scanner.config_categories import MAX_MARKET_CAP, MAX_STOCK_PRICE
    from scanner.utils import is_gem, is_hk_stock, is_st

    pool_info = pool_info or {}

    sym_to_bucket: dict[str, list[str]] = {}
    for b, syms in buckets.items():
        for s in syms:
            sym_to_bucket.setdefault(s, []).append(b)

    rows = []
    for c in cands:
        sym, code = c["symbol"], c["symbol"][2:] if len(c["symbol"]) > 6 else c["symbol"]
        q = quotes.get(sym, {})
        info = pool_info.get(sym, {})
        row = {
            "symbol": sym,
            "name": c["name"],
            "from_categories": c["categories"],
            "from_score": c["score"],
            "percent": q.get("percent"),
            "current": q.get("current"),
            "bias20": info.get("bias20"),
            "acc5": info.get("acc5"),
            "dangers": info.get("hard") or [],
        }
        if is_hk_stock(sym) or not is_gem(code) or is_st(c["name"]):
            row["stage"], row["detail"] = "身份门", "非创业板/ST/港股"
        elif q.get("current", 0) > MAX_STOCK_PRICE:
            row["stage"] = "价格门"
            row["detail"] = f"现价 {q.get('current')} > {MAX_STOCK_PRICE}"
        elif q.get("market_cap", 0) > MAX_MARKET_CAP:
            row["stage"] = "市值门"
            row["detail"] = f"总市值 {q.get('market_cap', 0) / 1e8:.0f}亿 > 500亿"
        elif row["dangers"]:
            row["stage"], row["detail"] = "v2排雷", "+".join(row["dangers"])
        elif sym in rejects:
            row["stage"], row["detail"] = "硬过滤", rejects[sym]
        elif sym in sym_to_bucket:
            row["stage"], row["detail"] = "入选", "+".join(sym_to_bucket[sym])
        elif sym in gems:
            row["stage"], row["detail"] = "过门未入选", "通过全部门禁但未进任何桶"
        else:
            row["stage"] = "未过门"
            # 细分（价格门 vs 市值门）依赖本行行情；行情快照缺失该票时无法区分
            # ——管线用的是 adapter 回填的 market_caps（雪球），与本地行情源可能不同源。
            row["detail"] = "不在过滤后候选池（价格/市值门，行情缺失时不可细分）"
        rows.append(row)

    # 排序：强筛选（v1 五桶）在前，其次池选，再次被拒；组内按涨幅降序
    order = {"入选": 0, "v2排雷": 1, "硬过滤": 2, "过门未入选": 3, "市值门": 4, "价格门": 5, "身份门": 6}
    rows.sort(
        key=lambda r: (
            order.get(r["stage"], 9),
            0 if r["stage"] == "入选" and r["detail"] != "pool_picks" else 1,
            -(r["percent"] if r["percent"] is not None else -999),
        )
    )
    return rows


# ── 4. 主流程 ──


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="把某日 v1 产出当作飙升榜输入再跑一遍筛选")
    p.add_argument("--from-date", default=None, help="候选池来源日（默认取 today 之前的最近落库日）")
    p.add_argument("--to-date", default=None, help="扫描日（默认今天）")
    p.add_argument("--at", default=DEFAULT_AT, help=f"钉死的盘中时刻（默认 {DEFAULT_AT}）")
    p.add_argument(
        "--scope",
        default="v1",
        choices=["v1", "v1+pool", "all", "real"],
        help="v1=昨日 v1 产出（默认）/ real=今日真实榜单（对照组）",
    )
    p.add_argument(
        "--with-v2",
        action="store_true",
        help="同时跑 v2 池管道（默认关闭：本实验只回答 v1 筛选规则）",
    )
    p.add_argument("--offline", action="store_true", help="adapter 用离线桩（K线/市值走缓存，不联网）")
    p.add_argument(
        "--no-quotes",
        action="store_true",
        help="不抓行情（涨幅/现价全 0，仅用于调试，结果不可用于判断）",
    )
    p.add_argument("--json", action="store_true")
    p.add_argument(
        "--quotes-file",
        default=None,
        help="行情快照文件：已有票复用、缺失补抓后写回（两次对照运行钉在同一份行情上）",
    )
    p.add_argument("--keep-db", default=None, help="保留本次临时库（调试）")
    p.add_argument("--db", default=str(_PROJECT_ROOT / "scanner.db"))
    return p


def main() -> int:
    args = build_parser().parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[中止] 找不到数据库：{db_path}")
        return 2

    import datetime as dt

    to_date = args.to_date or dt.date.today().isoformat()

    def log(*a, **kw):
        """--json 时把人类可读日志打到 stderr，保证 stdout 是纯 JSON（可被管道消费）。"""
        print(*a, file=sys.stderr if args.json else sys.stdout, **kw)

    ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        from_date = args.from_date
        if args.scope == "real":
            from_date = from_date or to_date  # 对照组：直接取当日上榜票
        if not from_date:
            row = ro.execute("SELECT MAX(date) FROM recommendations WHERE date < ?", (to_date,)).fetchone()
            from_date = row[0] if row else None
        if not from_date:
            print("[中止] 无法推断 from-date（recommendations 无同日历史）")
            return 1
        cands = load_candidates(ro, from_date, args.scope, to_date)
        if not cands:
            print(f"[中止] {from_date} 在 scope={args.scope} 下没有候选")
            return 1

        log(f"候选池：{from_date} 的 {args.scope} → {len(cands)} 只（去重后）")
        # 行情必须抓——它是榜单 raw 的核心字段（percent/current 驱动价格门、追涨门、
        # 动量桶）。早年离线模式把 quotes 置空 ⇒ 涨幅全 0 ⇒ 这几道门静默失效、
        # 结果虚高（15/15 全过），是一个会让人得出相反结论的假绿灯。
        quotes = {} if args.no_quotes else fetch_quotes_cached([c["symbol"] for c in cands], args.quotes_file)
        if args.no_quotes:
            log("  [!] --no-quotes：涨幅/现价全 0 ⇒ 价格门/追涨门/动量桶失效，结果仅供调试")
        if quotes:
            hit = sum(1 for c in cands if c["symbol"] in quotes)
            log(f"行情补全：{hit}/{len(cands)} 只（腾讯批量，{to_date} 实时）")
        raw = build_raw(cands, quotes, ro, from_date)
    finally:
        ro.close()

    tmp_dir = Path(tempfile.mkdtemp(prefix="replay_"))
    tmp_db = tmp_dir / "scanner.db"
    _backup_db(db_path, tmp_db)
    os.environ["RTS_DB_PATH"] = str(tmp_db)

    # 本实验只回答「v1 筛选规则」：默认关掉 v2 池管道。v2 槽位（pool_pick）本身
    # **不淘汰**（score 恒 0，只做语义标注），混进来会让「入选」虚高、归因串味；
    # 且双跑模式下同一票在 v1/v2 各有一个独立 Candidate 对象、各自打风险标签，
    # 把两边的分数并列展示会得到看似自相矛盾的结果（实测踩过：怡达股份 v1 域被
    # 「主力出货」硬过滤、v2 域却留在池选里）。
    # comeback / core_dip 同为附加桶，一并关闭——它们与前一日推荐池耦合，属另一问题。
    os.environ["RTS_ENABLE_POOL"] = "1" if args.with_v2 else "0"
    import scanner.orchestrator as _orch  # noqa: E402
    from scanner.data_source import get_adapter  # noqa: E402
    from scanner.database import init_db  # noqa: E402
    from scanner.models import ScanResult  # noqa: E402

    # ENABLE_COMEBACK / ENABLE_CORE_DIP 在 config_categories 里是硬编码常量，
    # 且 orchestrator 用 `from scanner.config import X` 快照式绑定 —— 必须打在
    # **orchestrator 的命名空间**上才生效（项目里踩过 4 次的同一个坑）。
    _orch.ENABLE_COMEBACK = False
    _orch.ENABLE_CORE_DIP = False
    scan_with_raw = _orch.scan_with_raw

    conn = init_db()
    # 临时库是从生产库整份复制的，其中 to_date 的 scan_rejections / pool_log 是
    # **生产进程当天写下的历史记录**。不清掉就会把历史归因当成本次回放的结果
    # （实测踩过：真榜组里「行云科技→趋势破位」其实是生产 09:39 写的，
    # 本次回放根本没算出这条）。清空当日两张审计表，保证归因只反映本次这一轮。
    conn.execute("DELETE FROM scan_rejections WHERE date = ?", (to_date,))
    conn.execute("DELETE FROM pool_log WHERE date = ?", (to_date,))
    conn.commit()
    adapter = OfflineAdapter() if args.offline else get_adapter()
    buf = io.StringIO()
    try:
        pin_now(to_date, args.at)
        if args.offline:
            stub_external_sources()
        with contextlib.redirect_stdout(buf):
            result: ScanResult = scan_with_raw(raw, conn, adapter)

        buckets = {
            "new_faces": [c.stock.symbol for c in result.new_faces],
            "momentum": [c.stock.symbol for c in result.momentum],
            "rebound": [c.stock.symbol for c in result.rebound],
            "short_term": [c.stock.symbol for c in result.short_term],
            "comeback": [c.stock.symbol for c in result.comeback],
            "pool_picks": [c.stock.symbol for c in result.pool_picks],
        }
        gems = {s.symbol for s in result.gem_stocks}
        rejects: dict[str, str] = {}
        for sym, reason in conn.execute("SELECT symbol, reason FROM scan_rejections WHERE date = ?", (to_date,)):
            rejects.setdefault(sym, reason or "（无原因）")

        # v2 首轮排雷不写 scan_rejections（被剔除的票压根没进候选），只在 pool_log
        # 的 danger_flags 留痕。v2 管道关闭时 pool_log 本次为空，不读（否则读到的是
        # 生产当天的旧记录，归因串味）。
        pool_info: dict[str, dict] = {}
        if args.with_v2:
            from scanner.danger import hard_flags

            for sym, dj, bias20, acc5 in conn.execute(
                "SELECT symbol, danger_flags, bias20, acc5 FROM pool_log WHERE date = ?",
                (to_date,),
            ):
                try:
                    flags = json.loads(dj) if dj else []
                except (TypeError, ValueError):
                    flags = []
                pool_info[sym] = {"hard": hard_flags(flags), "bias20": bias20, "acc5": acc5}

        scores = {}
        for c in (
            list(result.new_faces)
            + list(result.momentum)
            + list(result.rebound)
            + list(result.short_term)
            + list(result.pool_picks)
        ):
            scores.setdefault(c.stock.symbol, {})[c.category] = c.score

        rows = classify(cands, gems, buckets, rejects, quotes, pool_info)
        for r in rows:
            r["final_scores"] = scores.get(r["symbol"], {})
        for r in rows:
            r["final_scores"] = scores.get(r["symbol"], {})
    finally:
        conn.close()
        if args.keep_db:
            # 父目录不存在时 copy2 抛 FileNotFoundError，而它处在 finally 里会把
            # 已经算完的结果一起吞掉（实测踩过）——先建目录。
            keep = Path(args.keep_db)
            keep.parent.mkdir(parents=True, exist_ok=True)
            import shutil

            shutil.copy2(tmp_db, keep)
        import shutil as _sh

        _sh.rmtree(tmp_dir, ignore_errors=True)

    # ── 输出 ──
    if args.json:
        print(
            json.dumps(
                {
                    "from_date": from_date,
                    "to_date": to_date,
                    "at": args.at,
                    "scope": args.scope,
                    "offline": args.offline,
                    "raw_n": len(raw),
                    "raw": raw,
                    "buckets": buckets,
                    "gem_n": len(gems),
                    "filtered_large_cap": result.filtered_large_cap,
                    "rows": rows,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    v1_names = ("new_faces", "momentum", "rebound", "short_term")
    print(f"\n扫描日 {to_date} {args.at}（{'离线 adapter' if args.offline else '联网真实数据'}）")
    print(f"数据源：{from_date} 的 v1 产出 → 榜宽 {len(raw)} 条")
    print(f"过滤后候选池 {len(gems)} 只（市值门另拦 {result.filtered_large_cap} 只）")
    print(f"v1 四路引擎产出：{ {k: len(v) for k, v in buckets.items() if k in v1_names and v} or '全空' }")
    if buf.getvalue().strip():
        print("\n─── 管线原始输出（节选）───")
        for line in buf.getvalue().strip().splitlines()[-25:]:
            print("  " + line)
    print("\n─── 逐票去向 ───")
    print(f"{'票名':<10}{'代码':<9}{'涨幅':>8}{'bias20':>8}  {'源自':<20}{'去向':<10}{'最终评分':<20}明细")
    print("-" * 112)
    for r in rows:
        pct = f"{r['percent']:+.2f}%" if r["percent"] is not None else "—"
        b20 = f"{r['bias20']:+.1f}" if isinstance(r.get("bias20"), (int, float)) else "—"
        sc = "/".join(f"{k}={v}" for k, v in (r.get("final_scores") or {}).items())
        print(
            f"{r['name'][:9]:<10}{r['symbol']:<9}{pct:>8}{b20:>8}  "
            f"{'/'.join(r['from_categories'])[:19]:<20}{r['stage']:<10}{sc[:19]:<20}{r['detail']}"
        )
    print()
    print("去向分布：", dict(Counter(r["stage"] for r in rows)))
    passed = [r for r in rows if r["stage"] == "入选" and r["detail"] != "pool_picks"]
    print(
        "\n★ 通过 v1 筛选：",
        [
            f"{r['name']}({r['detail']}" + (f", {r['percent']:+.2f}%)" if r["percent"] is not None else ")")
            for r in passed
        ]
        or "无",
    )
    print(
        "✗ 被硬过滤（主力出货/趋势破位）：",
        [f"{r['name']}→{r['detail']}" for r in rows if r["stage"] == "硬过滤"] or "无",
    )
    print("○ 过门但四路引擎均未采纳：", [r["name"] for r in rows if r["stage"] == "过门未入选"] or "无")
    return 0


if __name__ == "__main__":
    sys.exit(main())
