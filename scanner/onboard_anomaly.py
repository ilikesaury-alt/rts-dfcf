"""沪深飙升区 **「榜内异动」段**（2026-09-30）：填 A 段与 B 段之间的中间档。

为什么要有这一段
----------------
「沪深飙升」区原有两段，中间缺一档：

    A 段（榜内飙升）  样本 = **榜内**   看「当日动能 + 热度跃升」 ⇒ 要求**已经涨了**
    B 段（榜外异动）  样本 = **榜外**   看「量先动·价未动 / 启动首日」 ⇒ 要求**没上榜**

于是「**刚上榜、涨幅还小、量已经动了**」这一档两侧都没覆盖：它已经涨（超过 A 段的
最低涨幅带才有竞争力）却还没启动到位（不到 B 段 T2 的 3.5% 启动线），同时它**在榜**
所以 B 段按定义把它排除了。本段只补这一档。

为什么只做 T1，不做 T2
---------------------
T2「启动首日」的下沿是 `MOMENTUM_LAUNCH_TODAY_MIN`(3.5%)，语义是「今天刚启动」；
而**榜内票按定义已经启动过**（它就是为启动而上榜的）⇒ T2 在榜内几乎不可能命中，
做了是死代码。故 `allowed_tiers=(T1,)`。这**不是**新增阈值或放宽口径，只是把
`classify_tier` 的两层窄化为一层，其余条件逐条照跑。

口径一律复用，本模块不新造任何一条判定
--------------------------------------
- 门槛 = `offboard_watch.offboard_gate`（通用 8 门 + 榜外专属门，原样复用）；
- 分层 = `offboard_watch.classify_tier` / `annotate`（含 5 日累计 / MA 非空头 /
  主力净占比 / 顶背离），仅以 `allowed_tiers` 关闭 T2；
- 排序 = `offboard_watch.sort_key`（层序 → 量比降序 → 主力净占比降序）；
- 样本面 = `hot_watch.is_hot_universe`（创业板 300/301，与 A/B 段同一实现）；
- K 线 = `daily_kline`（榜内票本就在该表内 —— 它是「榜单衍生池」），
  故**不需要**榜外那套 `offboard_kline_cache` 补取通道。

⚠ 与榜外段的唯一数据差异
----------------------
榜外票的日线**不在** `daily_kline` 里（B 段为此另建 `offboard_kline_cache`），
而榜内票**在**。所以本段的 K 线取数路径更短，但**判定阈值完全同源** ——
不要因为「取数更方便」就以为口径可以更松。

⚠ 尚未回测，且本区是 `rule_validate` 三个评估器的**盲区**
--------------------------------------------------------
`--set` 会被可见性硬校验拦在退出码 3（与榜外段同）。因此本段的观察纪律完全依赖
`onboard_anomaly_log` 逐日落库：**没有逐日表就没有标签，没有标签就没有任何阈值能被
证伪**，调阈值等于无标签调参。观察目标 D ≥ 47 交易日（按日 bootstrap 检出 10pp）。

⚠ 立场先说清楚（诚实义务，不是免责套话）
----------------------------------------
榜外段现有 324 条带次日标签的样本，**次日均值 −0.46%**。本段与它同源同门，
起点就是负的。本段**不声称**有预测力；它的唯一作用是把「已上��但还没启动到位」
这一档从视野里补出来，让观察面完整 —— 与 A/B 段同级，是观察段，不是信号段。
"""

from __future__ import annotations

import logging
from datetime import date as _date
from typing import Sequence

from scanner.config import (
    ONBOARD_ANOMALY_ENABLED,
    ONBOARD_DISPLAY_TOP,
    now_beijing,
)
from scanner.db.queries import (
    get_cached_klines,
    get_fund_flow_pct_map,
    get_market_extra_snapshot,
)
from scanner.display_gates import code_of
from scanner.hot_watch import is_hot_universe, is_st
from scanner.models import KlineBar
from scanner.offboard_watch import (
    T1,
    OffboardCandidate,
    annotate,
    offboard_gate,
    opening_silence_active,
    sort_key,
)
from scanner.utils import EXTERNAL_FAILURES, to_float

logger = logging.getLogger(__name__)


def _snapshot_vol_ratio_map(conn, symbols: Sequence[str]) -> dict[str, float]:
    """全市场 fund_flow 快照 → `{symbol: vol_ratio}`（缺失/无该键则不出现）。

    ## 为什么量比必须走这里，而不能走 `quotes`（2026-10-08 修复）

    本段原先从 `quotes`（生产路径 = `api.fetch_hot_quotes_batch`）读量比，但该函数
    只回传 `_HOT_QUOTE_FIELDS` 这 16 个字段，**其中没有 `volume_ratio`** ——
    雪球的 `batch/quote.json` 批量接口不提供量比，只有单票 `quote.json?extend=detail`
    才有（见 `api.fetch_hot_quote_detail` 的 docstring）。于是：

        q.get("volume_ratio") -> None -> to_float(None, 0.0) -> **恒为 0.0**

    而 `offboard_gate` 有 `volume_ratio < MOMENTUM_LAUNCH_VOL(1.5)` 硬门 ⇒
    **全段候选 100% 被拒 ⇒ 本段恒定产出 0 行**。实测 `onboard_anomaly_log`
    建表至今 0 行（2026-09-30 上线），确认这不是偶发而是结构性空转。

    之所以一直没被发现：`tests/test_onboard_anomaly.py` 的 fixture **手工塞了**
    `"volume_ratio": 2.0` 进 quotes dict，而真实生产通道永远不产出该键 ——
    守卫绿灯、线上空转（AGENTS.md「避免空断言」纪律要防的正是这个）。

    ## 修法与代价

    改从 `market_extra_cache` 的 fund_flow 快照取量比 —— **与 B 段同源同键**
    （`offboard_watch._snapshot_row_to_candidate` 读的也是 `payload["vol_ratio"]`），
    实测该快照覆盖全市场 1412 只创业板、100% 带 `vol_ratio`，无额外请求成本。

    ⚠ 与 B 段同款残留口径差：快照是「最近一次落库」的量比，可能滞后于实时价。
      本段其余字段（现价/涨幅）仍走**实时** `quotes`，故「量比旧、价新」两种
      时间戳并存。与 B 段一致，不假装没有 —— 彻底消除需为每票补 detail 接口
      （1 请求/票），成本不可接受。
    """
    if conn is None or not symbols:
        return {}
    try:
        snapshot = get_market_extra_snapshot(conn, "fund_flow")
    except EXTERNAL_FAILURES as e:
        logger.warning("榜内异动段量比快照取数失败（本段按无量比继续）: %s", e)
        return {}
    out: dict[str, float] = {}
    for sym in symbols:
        payload = snapshot.get(sym)
        if not payload:
            continue
        vr = to_float(payload.get("vol_ratio"), None)
        if vr is not None and vr > 0:
            out[sym] = vr
    return out


def build_onboard_candidates(
    board_items: Sequence[dict],
    quotes: dict[str, dict],
    conn=None,
    *,
    exclude_symbols: set[str] | None = None,
) -> tuple[list[OffboardCandidate], list[tuple[str, str]]]:
    """榜内创业板 → 通过 `offboard_gate` 的候选（按量比降序）。

    返回 (候选, 排除明细 [(symbol, 原因)])。明细只用于归因，不落库 —— 落库由
    `onboard_anomaly_log` 承担（只落**产出**行，与榜外段同款 observe-first 结构）。

    零成本过滤前置：代码前缀（创业板）、A 段已展示、今日已推荐 —— 先滤再跑门，
    理由同 `offboard_watch.build_candidates`（门里有多个浮点比较，全榜跑一遍是浪费）。

    `exclude_symbols` 语义**双义**，调用方须明确：
      · A 段已展示的 symbol（避免同屏重复）
      · 今日已推荐票（与 v1 池选区 / 回捞区互斥，同 v1 回捞区先例）

    ⚠ 量比来自 fund_flow 快照而非 `quotes`（2026-10-08 修复，理由见
      `_snapshot_vol_ratio_map`）：`quotes` 结构上无 `volume_ratio`，读它恒得 0.0，
      会让全段被 `offboard_gate` 的量比门拒掉 ⇒ 恒空。快照缺该票时量比留 0，
      该票同样会被门拒（fail-closed，与「数据不足不产出」一致）。
    """
    skip = exclude_symbols or set()
    out: list[OffboardCandidate] = []
    rejects: list[tuple[str, str]] = []

    # 量比一次性取数（快照覆盖全市场，1 次查询，与 B 段同源）。
    vr_map = _snapshot_vol_ratio_map(conn, [str(it.get("symbol") or "") for it in board_items])

    for it in board_items:
        symbol = str(it.get("symbol") or "")
        if not symbol or symbol in skip:
            if symbol:
                rejects.append((symbol, "已在A段展示或今日已推荐"))
            continue
        q = quotes.get(symbol)
        if not q:
            rejects.append((symbol, "无补全行情"))
            continue
        code = str(q.get("code") or "")
        if not code:
            rejects.append((symbol, "无代码"))
            continue
        if not code.startswith(("300", "301")):
            # 非创业板不进本段。与 offboard_gate 里的 is_hot_universe 同结论，
            # 这里前置是因为榜内快照含沪深主板，直接跑门会白跑浮点比较。
            continue
        if not is_hot_universe(str(q.get("exchange") or it.get("exchange") or ""), symbol):
            continue
        name = str(q.get("name") or it.get("name") or "")
        if is_st(name):
            continue

        c = OffboardCandidate(
            symbol=symbol,
            code=code,
            name=name,
            tier=T1,  # 占位；annotate 会按实际分层覆写（allowed_tiers 只放行 T1）
            current=to_float(q.get("current"), to_float(it.get("current"))) or 0.0,
            percent=to_float(q.get("percent"), to_float(it.get("percent"))) or 0.0,
            accum_5d=0.0,
            volume_ratio=vr_map.get(symbol, 0.0),
            main_pct=0.0,
            volume=to_float(q.get("volume"), 0.0) or 0.0,
            amount=to_float(q.get("amount"), 0.0) or 0.0,
            market_capital=to_float(q.get("market_capital"), 0.0) or 0.0,
            float_market_capital=to_float(q.get("float_market_capital"), 0.0) or 0.0,
            turnover_rate=to_float(q.get("turnover_rate"), 0.0) or 0.0,
            exchange=str(q.get("exchange") or it.get("exchange") or ""),
            status=int(to_float(q.get("status"), 1) or 1),
        )
        reason = offboard_gate(c)
        if reason:
            rejects.append((symbol, reason))
            continue
        out.append(c)

    # 主力净占比：与榜外段同一条数据源（全市场 fund_flow 快照），不在 quote 里。
    if conn is not None and out:
        try:
            flow_map = get_fund_flow_pct_map(conn, [c.symbol for c in out])
        except EXTERNAL_FAILURES as e:
            logger.warning("榜内异动段资金流取数失败（本段按无资金流继续）: %s", e)
            flow_map = {}
        for c in out:
            c.main_pct = flow_map.get(c.symbol, 0.0) or 0.0
            c.ff_pct = flow_map.get(c.symbol)

    out.sort(key=lambda x: -x.volume_ratio)  # 决定 K 线取数与分层顺序（量比现为快照真值）
    return out, rejects


def _board_is_chinext(it: dict) -> bool:
    """榜单行是不是创业板（300/301）——用于补行情前的前置过滤。

    与 `build_onboard_candidates` 里的判断同源（代码前缀 + `is_hot_universe`）。
    2026-10-08 修复 L4：原先用子串匹配（`"300" in code`），会把 002300 这类
    **代码中段恰好含 "300"** 的主板票误纳进预筛（白花一个请求位）；改为剥前缀后
    `startswith` 精确判定，与同文件 `build_onboard_candidates` 的口径一致。
    """
    code = code_of(str(it.get("code") or it.get("symbol") or ""))
    return code.startswith(("300", "301"))


def run_onboard_anomaly(
    conn,
    adapter,
    board_items: Sequence[dict],
    quotes: dict[str, dict] | None = None,
    *,
    exclude_symbols: set[str] | None = None,
    top_n: int = ONBOARD_DISPLAY_TOP,
    klines: dict[str, list | None] | None = None,
) -> list[OffboardCandidate]:
    """跑一轮榜内异动筛选，返回待展示的前 `top_n` 行（T1 独有）。

    `quotes` 显式传入时跳过补全（离线自检 / 单测路径）；否则走
    `adapter.fetch_hot_quotes_batch`（**不重发榜单请求**，只补行情）。同一批补全
    通道 A 段也在用（`run_hot_watch`）—— 本段**不复用 A 段那份**，因为 A 段的补全
    结果不外传（它在函数内部消费）；雪球批量行情 2 请求/100 票，重取一次的成本可
    接受，换来两段互不耦合（A 段改补全逻辑不会静默改到本段）。

    `klines` 显式传入时跳过 DB 取数（离线自检 / 单测路径）。

    fail-open 边界（与榜外段同向）：
      - 关掉开关 / 无 conn / 榜单空 / 开盘静默窗口内 → 返回 []，本段留空；
      - 补全失败 / 无行情 → 返回 []（本段留空，不告警噪音）；
      - K 线缺失 → **该票不产出**（fail-closed）：T1 的条件含「5 日累计 ∈ [0,15)」
        与「MA 非空头」两个**必须**成立项，验不了就不该报。
    编程错误不吞（由调用方主循环记录 traceback）。
    """
    if not ONBOARD_ANOMALY_ENABLED or conn is None or not board_items:
        return []
    if opening_silence_active():
        return []

    if quotes is None:
        fetch_batch = getattr(adapter, "fetch_hot_quotes_batch", None)
        if fetch_batch is None:
            return []
        # 零成本前置：**先剔掉非创业板与已排除的票**，再按剔完的集���补行情——
        # 否则会把沪深主板（榜内快照含主板）与已在 A 段/今日已推荐的票一并补回来，
        # 白花请求（batch 行情 2 请求/100 票）。
        skip = exclude_symbols or set()
        prelim = [
            it
            for it in board_items
            if str(it.get("symbol") or "") and str(it.get("symbol") or "") not in skip and _board_is_chinext(it)
        ]
        if not prelim:
            return []
        try:
            quotes = fetch_batch([str(it["symbol"]) for it in prelim])
        except EXTERNAL_FAILURES as e:
            logger.warning("榜内异动段行情补全失败（本段留空）: %s", e)
            return []
        if not quotes:
            return []

    cands, _rejects = build_onboard_candidates(board_items, quotes, conn, exclude_symbols=exclude_symbols)
    if not cands:
        return []

    if klines is None:
        try:
            # get_cached_klines 的值类型含 None（该 symbol 无有效 bar），故注解放宽到
            # list | None —— 交给 annotate 处理（它对 None 的语义是「数据不足不产出」）。
            fetched: dict[str, list[KlineBar] | None] = get_cached_klines(conn, [c.symbol for c in cands])
        except EXTERNAL_FAILURES as e:
            logger.warning("榜内异动段K线取数失败（本段留空）: %s", e)
            return []
        klines = {k: v for k, v in fetched.items() if v}

    today = now_beijing().date().isoformat()
    passed: list[OffboardCandidate] = []
    kl_map = klines or {}
    for c in cands:
        # allowed_tiers=(T1,) —— 本段只做 T1，理由见模块 docstring。
        if annotate(c, kl_map.get(c.symbol), today, allowed_tiers=(T1,)) is None:
            continue
        passed.append(c)

    if not passed:
        return []

    passed.sort(key=sort_key)
    persist_round(conn, passed, today)
    # 顺手回填历史日的次日收益（与榜外段 `run_offboard_watch` 同一位置、同一理由）。
    # 不放在这里的话，`onboard_anomaly_log.next_day_pct` 永远是 NULL ⇒ 本段
    # **一行标签都没有** ⇒ 按本仓纪律「没有标签就没有任何阈值能被证伪」，
    # 它连观察纪律都建立不起来（2026-10-08 修复：此前该回填无生产调用方）。
    # 无待回填行时只是一条索引查询，代价可忽略。
    try:
        backfill_next_day_pct(conn)
    except EXTERNAL_FAILURES as e:
        logger.warning("榜内异动段次日收益回填失败（不影响本轮）: %s", e)
    top = passed[:top_n]
    _attach_boards(top, conn)
    return top


def _attach_boards(rows: list[OffboardCandidate], conn) -> None:
    """板块列就地填充（与 A/B 段同一条回退链）。失败只让该列留空。"""
    if not rows:
        return
    try:
        from scanner.concept import attach_display_boards

        attach_display_boards(conn, rows, fetch=False)
    except EXTERNAL_FAILURES as e:
        logger.warning("榜内异动段板块列填充失败（本列留空）: %s", e)


# ── 逐日落库（observe-first 的唯一证据来源）───────────────────────────────────


def persist_round(conn, rows: Sequence[OffboardCandidate], today: str) -> None:
    """把本轮产出写入 `onboard_anomaly_log`（失败只告警，不影响本轮返回）。

    幂等：同 (date, symbol) 走 UPSERT —— 主循环每轮都跑，不去重会刷出大量重复行。
    """
    if conn is None or not rows:
        return
    stamp = now_beijing().isoformat()
    data = [
        (
            today,
            c.symbol,
            c.name,
            c.tier,
            c.percent,
            c.accum_5d,
            c.volume_ratio,
            c.main_pct,
            c.amount,
            c.float_market_capital,
            c.current,
            stamp,
        )
        for c in rows
    ]
    try:
        conn.executemany(
            "INSERT INTO onboard_anomaly_log "
            "(date,symbol,name,tier,percent,accum_5d,vol_ratio,main_pct,amount,"
            "float_cap,price,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(date,symbol) DO UPDATE SET "
            "name=excluded.name,tier=excluded.tier,percent=excluded.percent,"
            "accum_5d=excluded.accum_5d,vol_ratio=excluded.vol_ratio,"
            "main_pct=excluded.main_pct,amount=excluded.amount,"
            "float_cap=excluded.float_cap,price=excluded.price,updated=excluded.updated",
            data,
        )
        conn.commit()
    except EXTERNAL_FAILURES as e:
        # 2026-10-08 修复 L7：与全仓失败纪律对齐（原为 (sqlite3.Error, ValueError,
        # KeyError) —— KeyError 属「响应结构」类数据异常，EXTERNAL_FAILURES 已覆盖；
        # 收窄后编程错误（TypeError/AttributeError 等）照旧冒泡，不再被吞成一行告警）。
        logger.warning("榜内异动段落库失败（不影响本轮展示）: %s", e)


def backfill_next_day_pct(conn, signal_date: str | None = None) -> int:
    """回填 `onboard_anomaly_log.next_day_pct`（次日收益 %），返回**实际**回填行数。

    口径与 `offboard_launch_log.next_day_pct` 对齐：``(次一交易日收盘 / 信号日收盘 - 1) × 100``。

    ## 为什么重写（2026-10-08）

    原实现要求调用方传入 ``{symbol: (次日前收, 当日收盘)}``，然后拿
    **`now_beijing()` 当作 `WHERE date=?` 的值**去 UPDATE。三个问题：

    1. **它没有任何生产调用方** —— 全仓只有 `tests/test_onboard_anomaly.py` 调它。
       即 `next_day_pct` 永远不会被填 ⇒ 榜内异动段**永远没有标签** ⇒ 按本仓纪律
       （「没有逐日表就没有标签，没有标签就没有任何阈值能被证伪」）该段**无法回测**。
    2. **日期恒为今天**，所以只能更新「今天」的��。历史回放 / 隔日补数时，
       UPDATE 命中 0 行，但函数仍 `return len(rows)` 报成功 —— **谎报回填数**。
       `test_backfill_next_day_pct` 就是被这一点挡红的（写 `2026-09-30`、
       按今天查 ⇒ 0 行，却返回 1）。
    3. **无防呆**：没排除「下一根 bar 隔了整周」（停牌/长假），
       也没排除「信号日 = 今天」（那样永远找不到次日 bar）。

    ## 现实现：与榜外段 `offboard_watch.backfill_next_day` 同款

    改成**自驱动**：查待回填行 → 从 `daily_kline` 取日线 → 找次一交易日 bar →
    逐行 UPDATE。榜内票的日线本就在 `daily_kline`（本模块 docstring 已述），
    故不需要榜外那套 `offboard_kline_cache`。

    保留榜外段的两条防呆（docstring 见 `offboard_watch.backfill_next_day`）：
      1. 次一 bar 与信号日相差 ≤4 个自然日（覆盖周末），否则跳过；
      2. 信号日必须 `< today` —— 否则每轮都为当天信号去找「次日 bar」，
         既永远找不到、又反复翻当天的 K 线。

    `signal_date` 用于 CLI/回放指定某一天（与榜外段同名参数同义）。
    """
    if conn is None:
        return 0
    today = now_beijing().date().isoformat()
    sql = (
        "SELECT date, symbol FROM onboard_anomaly_log "  # noqa: S608 - 常量表名 + 占位符
        "WHERE next_day_pct IS NULL AND date < ?"
    )
    params: tuple = (today,)
    if signal_date:
        sql += " AND date = ?"
        params = (today, signal_date)
    try:
        rows = conn.execute(sql, params).fetchall()
    except EXTERNAL_FAILURES as e:
        logger.warning("榜内异动段回填查询失败: %s", e)
        return 0
    if not rows:
        return 0

    by_symbol: dict[str, list[str]] = {}
    for d, sym in rows:
        by_symbol.setdefault(sym, []).append(d)

    filled = 0
    now = now_beijing().isoformat(timespec="seconds")
    for sym, dates in by_symbol.items():
        bars = (get_cached_klines(conn, [sym]).get(sym)) or []
        # ⚠ `get_cached_klines` 已按 `make_kline_bar` 契约剔除脏 bar（close<=0 / 日期非法），
        #   故**信号日当天的 bar 可能根本不在 by_date 里** —— 必须用 `.get(d)` 而非
        #   `by_date[d]`，否则脏 bar 直接把回填打成 KeyError 崩掉（2026-10-08 实测）。
        by_date: dict[str, KlineBar] = {b["date"]: b for b in bars if b.get("date")}
        for d in dates:
            later = sorted(x for x in by_date if x > d)
            if not later:
                continue
            nxt = later[0]
            # 防呆 1：次一 bar 隔太久（停牌/长假）⇒ 不是「次日」，跳过。
            try:
                if (_date.fromisoformat(nxt) - _date.fromisoformat(d)).days > 4:
                    continue
            except ValueError:
                continue
            b0 = by_date.get(d)
            b1 = by_date.get(nxt)
            p0 = (to_float(b0.get("close"), 0.0) or 0.0) if b0 else 0.0
            p1 = (to_float(b1.get("close"), 0.0) or 0.0) if b1 else 0.0
            if p0 <= 0 or p1 <= 0:
                continue
            try:
                conn.execute(
                    "UPDATE onboard_anomaly_log SET next_day_pct=?, updated=? WHERE date=? AND symbol=?",
                    (round((p1 / p0 - 1.0) * 100.0, 2), now, d, sym),
                )
            except EXTERNAL_FAILURES as e:
                logger.warning("榜内异动段回填写入失败 %s %s: %s", d, sym, e)
                continue
            filled += 1
    if filled:
        try:
            conn.commit()
        except EXTERNAL_FAILURES as e:
            logger.warning("榜内异动段回填提交失败: %s", e)
    return filled


__all__ = [
    "build_onboard_candidates",
    "run_onboard_anomaly",
    "persist_round",
    "backfill_next_day_pct",
]
