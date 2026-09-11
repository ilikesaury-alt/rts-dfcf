"""沪深飙升榜「极有可能大涨」独立观察区（2026-09-11 自 rts-xueqiu 合入）。

与主线（创业板 + 次日大涨 next_day 口径）**完全解耦**的第二观察维度，回答的是
不同问题：主线问「哪只明天可能大涨」，本区问「哪只今天正在启动、极可能继续大涨」。

三条硬性边界（改动前请先读，避免把两个口径混起来）
------------------------------------------------
1. **样本面更宽**：主线 `filter_gem_stocks` 只留创业板（300/301）；本区覆盖沪深
   主板 + 创业板个股（SH 600/601/603/605、SZ 000/001/002/003/300/301），
   剔除科创板(688)、北交所(8/4)、ETF/基金(15/16/5x)、港股、ST/退市。
2. **不进主线任何链路**：结果不写 `recommendations`、不参与复合评分/档位/🎯 画像、
   不进飞书主卡片。落库仅为本区自己的连击跟踪（`hot_watch_hits`）。
3. **口径独立**：按「榜单热度跃升 + 当日动能 + 量能」加权（rank_change 35 /
   percent 25 / price 15 / 量能 25），与 next_day 校准无关于是本区不做
   `--rescore` 回放、不参与 portfolio_backtest。

数据源与补全分工（2026-09-11 实测，勿互换）
------------------------------------------
- 榜单：`hot_stock/new_list.json`（= `api.fetch_biaosheng`，同一接口同一排序键
  `order_by=rank_change`），**直接复用主轮已抓的榜单**，不再单独发一次请求。
- 批量补全 `batch/quote.json`：有 volume/amount/turnover_rate/market_capital/
  last_close，**无** volume_ratio/limit_up/limit_down（实测恒 None）。2 请求/100 票。
- 单票 detail `quote.json?extend=detail`：额外有 volume_ratio/limit_up/limit_down，
  1 请求/票 —— 故只对最终前 `HOT_DETAIL_TOP` 名调用。
- 涨跌停价：batch 不给，由 `last_close` 按板块幅度推算（见 `limit_prices`），
  保证硬排除不依赖可选的 detail 补拉（补拉失败时排除口径不变）。

失败纪律：只捕获 `EXTERNAL_FAILURES`（网络/超时/JSON 脏值），编程错误冒泡；
本区任何异常由 `run_hot_watch` 外层兜住，主循环不受影响。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Sequence

from scanner.config import (
    HOT_DETAIL_TOP,
    HOT_DISPLAY_TOP,
    HOT_ENRICH_LIMIT,
    HOT_LIMIT_DOWN_TOLERANCE,
    HOT_LIMIT_PCT_GEM,
    HOT_LIMIT_PCT_MAIN,
    HOT_LIMIT_UP_NEAR,
    HOT_MAX_MARKET_CAP,
    HOT_MAX_PERCENT,
    HOT_MIN_PERCENT,
    HOT_PRICE_DECAY_TO,
    HOT_PRICE_IDEAL_HIGH,
    HOT_PRICE_IDEAL_LOW,
    HOT_RANK_CHANGE_CAP,
    HOT_STREAK_RESET_DAYS,
    HOT_TR_FULL,
    HOT_VOLUME_NO_DATA,
    HOT_VOLUME_SINGLE_FACTOR,
    HOT_VR_FULL,
    HOT_VR_WEIGHT,
    HOT_W_PERCENT,
    HOT_W_PRICE,
    HOT_W_RANK_CHANGE,
    HOT_W_VOLUME,
    now_beijing,
)
from scanner.utils import EXTERNAL_FAILURES, is_gem, is_st, to_float

logger = logging.getLogger(__name__)

# 本区样本面白名单（代码 6 位前缀）。与主线 is_gem 不同：这里要的是「沪深主板+创业板」，
# 且必须显式排除科创板/北交所/ETF —— 白名单而非黑名单，新板块默认不入选。
_HOT_MAIN_PREFIXES = ("600", "601", "603", "605", "000", "001", "002", "003")
_HOT_GEM_PREFIXES = ("300", "301")


@dataclass
class HotCandidate:
    """榜单字段 + 行情补全字段合并后的候选（纯内存，不落 recommendations）。"""

    symbol: str
    code: str
    name: str
    exchange: str
    current: float
    percent: float
    rank_change: int
    rank: int
    volume: float = 0.0  # 成交量（股）
    amount: float = 0.0  # 成交额（元）
    market_capital: float = 0.0
    float_market_capital: float = 0.0
    turnover_rate: float = 0.0
    volume_ratio: float = 0.0
    limit_up: float = 0.0
    limit_down: float = 0.0
    status: int = 1
    score: float = 0.0
    streak: int = 1
    reasons: list[str] = field(default_factory=list)


# ── 样本面与涨跌停价 ────────────────────────────────────────────────────────


def is_hot_universe(exchange: str, code: str) -> bool:
    """是否属于本区样本面：沪深主板 + 创业板个股。

    code 传 6 位代码或带前缀 symbol 均可（`_strip_code` 兼容）。
    白名单判定 —— 科创板(688)/北交所(8,4)/ETF(15,16,5x)/港股一律 False。
    """
    if exchange not in ("SH", "SZ"):
        return False
    c = _strip_code(code)
    if len(c) != 6 or not c.isdigit():
        return False
    if exchange == "SZ":
        return c.startswith(_HOT_MAIN_PREFIXES[4:] + _HOT_GEM_PREFIXES)
    return c.startswith(_HOT_MAIN_PREFIXES[:4])


def _strip_code(code: str) -> str:
    """去掉 SH/SZ/BJ 交易所前缀，取 6 位纯代码。"""
    if len(code) > 2 and code[:2] in ("SH", "SZ", "BJ"):
        return code[2:]
    return code


def limit_pct_for(code: str) -> float:
    """该代码的当日涨跌幅限制（%）：创业板 20%，主板 10%。

    本区样本面已剔除科创板与 ST，故只需两档。ST 为 ±5% 但已被 is_st 过滤。
    """
    return HOT_LIMIT_PCT_GEM if is_gem(code) else HOT_LIMIT_PCT_MAIN


def limit_prices(last_close: float, code: str) -> tuple[float, float]:
    """由昨收推算涨停价 / 跌停价（A 股规则：昨收 ×(1±limit%)，四舍五入到分）。

    batch 行情接口不返回 limit_up/limit_down，但返回 last_close —— 据此推算可与
    接口值逐分对齐，硬排除不依赖可选的 detail 补拉（补拉失败时口径不变）。
    last_close ≤ 0（脏值/缺失）时返回 (0.0, 0.0) —— 调用方按「无法判定」处理，
    不做涨跌停排除（fail-open，宁可放过也不误杀）。
    """
    if last_close <= 0 or not math.isfinite(last_close):
        return 0.0, 0.0
    pct = limit_pct_for(code)
    return round(last_close * (1 + pct / 100.0), 2), round(last_close * (1 - pct / 100.0), 2)


# ── 硬性排除 ────────────────────────────────────────────────────────────────


def hard_exclude(c: HotCandidate) -> str | None:
    """硬性排除（必要条件，任一命中即剔除）。返回排除原因，通过返回 None。

    与 rts-xueqiu 同口径，两处适配本项目：
      - ST 判定复用 utils.is_st（项目单一事实来源，不再本地实现 is_st_name）；
      - 涨跌停价由 last_close 推算（见 limit_prices），非接口直取。
    """
    if is_st(c.name):
        return "ST/退市风险股"
    if not is_hot_universe(c.exchange, c.code):
        return "非沪深个股(科创板/ETF/北交所/港股等)"
    if c.status != 1:
        return f"非正常交易状态(status={c.status})"
    if c.current <= 0:
        return "无有效报价"
    if c.volume <= 0:
        return "无成交量(停牌或未成交)"
    if c.limit_down > 0 and c.current <= c.limit_down * HOT_LIMIT_DOWN_TOLERANCE:
        return "已触及跌停"
    if c.percent <= HOT_MIN_PERCENT:
        return "当前非上涨状态"
    if c.limit_up > 0 and c.current >= c.limit_up * HOT_LIMIT_UP_NEAR:
        return f"已封涨停({c.percent:.2f}%), 追高性价比低"
    if c.percent > HOT_MAX_PERCENT:
        return f"涨幅过高({c.percent:.2f}%>{HOT_MAX_PERCENT:.0f}%)"
    if c.market_capital > 0 and c.market_capital > HOT_MAX_MARKET_CAP:
        return f"市值过大({c.market_capital / 1e8:.0f}亿>{HOT_MAX_MARKET_CAP / 1e8:.0f}亿)"
    return None


# ── 打分（合计 100）────────────────────────────────────────────────────────


def _log_norm(value: float, cap: float) -> float:
    """对数归一化到 [0,1]：压缩极值差距（rank_change 实测可达 9000+）。"""
    if value <= 0 or cap <= 0:
        return 0.0
    return min(1.0, math.log1p(value) / math.log1p(cap))


def score_rank_change(c: HotCandidate) -> float:
    return _log_norm(float(c.rank_change), HOT_RANK_CHANGE_CAP)


def score_percent(c: HotCandidate) -> float:
    """涨幅在 (0, HOT_MAX_PERCENT] 内递增：越接近上限动能越强。

    硬性排除已剔除 > 上限的标的，故以上限为满分基准。
    """
    if c.percent <= 0:
        return 0.0
    return min(c.percent / HOT_MAX_PERCENT, 1.0)


def score_price(c: HotCandidate) -> float:
    """价格特征：理想区间 [3,40] 满分（低价弹性好），超上限线性衰减，低于下限略降。"""
    price = c.current
    if price <= 0:
        return 0.0
    if HOT_PRICE_IDEAL_LOW <= price <= HOT_PRICE_IDEAL_HIGH:
        return 1.0
    if price < HOT_PRICE_IDEAL_LOW:
        return max(0.35, price / HOT_PRICE_IDEAL_LOW)
    return max(0.0, 1.0 - (price - HOT_PRICE_IDEAL_HIGH) / (HOT_PRICE_DECAY_TO - HOT_PRICE_IDEAL_HIGH))


def score_volume(c: HotCandidate) -> float:
    """量能活跃度：量比 + 换手率。

    量比在批量补全里缺失（batch 接口无该字段）时以换手率为主 —— 该回退路径是
    rts-xueqiu 原有语义，非本项目新增的降级。
    """
    vr = c.volume_ratio
    tr = c.turnover_rate
    vr_score = min(max(vr, 0.0) / HOT_VR_FULL, 1.0) if vr > 0 else 0.0
    tr_score = min(max(tr, 0.0) / HOT_TR_FULL, 1.0) if tr > 0 else 0.0
    if vr > 0 and tr > 0:
        return HOT_VR_WEIGHT * vr_score + (1 - HOT_VR_WEIGHT) * tr_score
    if tr > 0:
        return tr_score * HOT_VOLUME_SINGLE_FACTOR
    if vr > 0:
        return vr_score * HOT_VOLUME_SINGLE_FACTOR
    return HOT_VOLUME_NO_DATA


def compute_score(c: HotCandidate) -> float:
    return (
        HOT_W_RANK_CHANGE * score_rank_change(c)
        + HOT_W_PERCENT * score_percent(c)
        + HOT_W_PRICE * score_price(c)
        + HOT_W_VOLUME * score_volume(c)
    )


def build_reasons(c: HotCandidate) -> list[str]:
    """可读的筛选理由（JSON/日志消费，不影响排序）。"""
    rs: list[str] = []
    if c.rank_change >= 5000:
        rs.append(f"飙升榜排名暴升{c.rank_change}位, 热度断层领先")
    elif c.rank_change >= 2000:
        rs.append(f"排名大幅上升{c.rank_change}位")
    elif c.rank_change >= 800:
        rs.append(f"排名上升{c.rank_change}位")
    else:
        rs.append(f"排名小幅上升{c.rank_change}位")

    if c.percent >= 6:
        rs.append(f"涨幅{c.percent:.2f}%, 接近阈值上沿, 动能强")
    elif c.percent >= 3:
        rs.append(f"涨幅{c.percent:.2f}%, 稳步上行")
    else:
        rs.append(f"涨幅{c.percent:.2f}%, 小幅翻红(位置低较安全)")

    if c.current <= 10:
        rs.append(f"现价仅{c.current:.2f}元, 低价弹性大")
    elif c.current <= 40:
        rs.append(f"现价{c.current:.2f}元, 价格适中易拉升")
    else:
        rs.append(f"现价{c.current:.2f}元")

    if c.volume_ratio >= 3:
        rs.append(f"量比{c.volume_ratio:.2f}, 显著放量")
    elif c.volume_ratio >= 1.5:
        rs.append(f"量比{c.volume_ratio:.2f}, 温和放量")

    if c.turnover_rate >= 15:
        rs.append(f"换手{c.turnover_rate:.1f}%, 交投极为活跃")
    elif c.turnover_rate >= 7:
        rs.append(f"换手{c.turnover_rate:.1f}%, 活跃度良好")

    if c.market_capital > 0:
        cap_yi = c.market_capital / 1e8
        if cap_yi <= 80:
            rs.append(f"市值仅{cap_yi:.0f}亿, 小盘易炒作")
        elif cap_yi <= 150:
            rs.append(f"市值{cap_yi:.0f}亿, 中盘弹性尚可")
    return rs


# ── 候选构建 ────────────────────────────────────────────────────────────────


def _num(v: Any, default: float = 0.0) -> float:
    """安全转 float：None/字符串/NaN/inf → default（与 utils.to_float 同语义）。"""
    f = to_float(v, None)
    if f is None or not math.isfinite(f):
        return default
    return f


def prefilter_board(raw_items: Sequence[dict]) -> list[dict]:
    """榜单预筛：只用榜单自带字段剔除明显不合格者，减少后续补全请求量。

    与 rts-xueqiu 同策略（先预筛再补全）。注意 percent 上界**不在此处**截断：
    涨幅过高的判定依赖补全后的真实行情（榜单 percent 与 quote percent 偶有差异），
    留到 hard_exclude 统一判定，避免两处口径分叉。
    """
    prelim: list[dict] = []
    for it in raw_items:
        symbol = str(it.get("symbol") or "")
        name = str(it.get("name") or "")
        exch = str(it.get("exchange") or "")
        if is_st(name):
            continue
        if not is_hot_universe(exch, symbol):
            continue
        if _num(it.get("percent")) <= 0:
            continue
        prelim.append(it)
    # 按排名上升幅度倒序：补全额度有限时优先覆盖热度跃升最猛的
    prelim.sort(key=lambda x: _num(x.get("rank_change")), reverse=True)
    return prelim


def build_candidates(
    board_items: Sequence[dict],
    quotes: dict[str, dict],
) -> tuple[list[HotCandidate], list[HotCandidate]]:
    """合并榜单 + 补全行情 → 通过硬排除的候选（已按评分降序）。

    返回 (通过候选, 被排除候选)。被排除者仅用于调试/日志，不落库。
    """
    passed: list[HotCandidate] = []
    rejected: list[HotCandidate] = []

    for idx, it in enumerate(board_items, 1):
        symbol = str(it.get("symbol") or "")
        q = quotes.get(symbol)
        if not q:
            continue
        code = str(q.get("code") or _strip_code(symbol))
        last_close = _num(q.get("last_close"))
        limit_up, limit_down = limit_prices(last_close, code)
        c = HotCandidate(
            symbol=symbol,
            code=code,
            name=str(q.get("name") or it.get("name") or ""),
            exchange=str(q.get("exchange") or it.get("exchange") or ""),
            current=_num(q.get("current"), _num(it.get("current"))),
            percent=_num(q.get("percent"), _num(it.get("percent"))),
            rank_change=int(_num(it.get("rank_change"))),
            rank=int(_num(it.get("rank"), idx)),
            volume=_num(q.get("volume")),
            amount=_num(q.get("amount")),
            market_capital=_num(q.get("market_capital")),
            float_market_capital=_num(q.get("float_market_capital")),
            turnover_rate=_num(q.get("turnover_rate")),
            status=int(_num(q.get("status"), 1)),
            limit_up=limit_up,
            limit_down=limit_down,
        )
        reason = hard_exclude(c)
        if reason:
            rejected.append(c)
            continue
        c.score = compute_score(c)
        c.reasons = build_reasons(c)
        passed.append(c)

    passed.sort(key=lambda x: (-x.score, -x.rank_change))
    return passed, rejected


# ── 连击跟踪（DB 持久化，替代 rts-xueqiu 的 history.json）─────────────────────


def _next_round(conn) -> int:
    """取并递增全局轮次号（hot_watch_meta 表）。"""
    row = conn.execute("SELECT value FROM hot_watch_meta WHERE key='round_no'").fetchone()
    cur = int(row[0]) if row and str(row[0]).isdigit() else 0
    nxt = cur + 1
    conn.execute(
        "INSERT INTO hot_watch_meta(key, value) VALUES('round_no', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(nxt),),
    )
    return nxt


def update_streaks(conn, hits: Sequence[HotCandidate], round_no: int) -> None:
    """更新连续命中轮数：本轮命中且上轮也命中 → +1；否则重置为 1。

    本轮未命中的存量记录 streak 归零（保留记录供观察），与 rts-xueqiu 同语义。
    清理超过 HOT_STREAK_RESET_DAYS 天未再命中的记录，防表无限增长。
    """
    now = now_beijing().isoformat(timespec="seconds")
    hit_syms = set()

    for c in hits:
        hit_syms.add(c.symbol)
        row = conn.execute("SELECT streak, last_round FROM hot_watch_hits WHERE symbol=?", (c.symbol,)).fetchone()
        prev_streak = int(row[0] or 0) if row else 0
        prev_round = int(row[1] or 0) if row else 0
        # 上一轮也命中 → 连击累加；否则（新面孔 / 中间断过轮）重新从 1 起算
        streak = prev_streak + 1 if prev_round == round_no - 1 else 1
        c.streak = streak
        conn.execute(
            """INSERT INTO hot_watch_hits
               (symbol, name, streak, last_round, last_seen, last_percent, last_price, last_score)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol) DO UPDATE SET
                 name=excluded.name, streak=excluded.streak, last_round=excluded.last_round,
                 last_seen=excluded.last_seen, last_percent=excluded.last_percent,
                 last_price=excluded.last_price, last_score=excluded.last_score""",
            (
                c.symbol,
                c.name,
                streak,
                round_no,
                now,
                round(c.percent, 2),
                round(c.current, 2),
                round(c.score, 2),
            ),
        )

    if hit_syms:
        marks = ",".join("?" * len(hit_syms))
        conn.execute(
            f"UPDATE hot_watch_hits SET streak=0 WHERE symbol NOT IN ({marks})",  # noqa: S608
            tuple(hit_syms),
        )
    else:
        conn.execute("UPDATE hot_watch_hits SET streak=0")


def _prune_stale(conn) -> None:
    """清理长期未命中的记录（streak=0 且 last_seen 早于 HOT_STREAK_RESET_DAYS 天前）。

    last_seen 为 ISO 字符串（`YYYY-MM-DDTHH:MM:SS`），取前 10 位即日期，
    直接与截止日期字符串比较（ISO 日期字典序 == 时间序）。
    """
    from datetime import timedelta

    cutoff = (now_beijing() - timedelta(days=HOT_STREAK_RESET_DAYS)).strftime("%Y-%m-%d")
    conn.execute(
        "DELETE FROM hot_watch_hits WHERE streak=0 AND COALESCE(substr(last_seen,1,10),'') <> '' "
        "AND substr(last_seen,1,10) < ?",
        (cutoff,),
    )


# ── 单轮主流程 ──────────────────────────────────────────────────────────────


def run_hot_watch(
    adapter,
    conn,
    board_items: Sequence[dict],
    top_n: int = HOT_DISPLAY_TOP,
) -> list[HotCandidate]:
    """跑一轮本区筛选（含补全/排除/打分/连击落库）。返回待展示的 TOP N 候选。

    依赖注入 `board_items`：复用主循环**已抓取**的飙升榜（`adapter.fetch_biaosheng()`
    与本区同源同排序键），本区不再单独发榜单请求。

    fail-open 边界：
      - 榜单为空 / 补全全失败 → 返回 []（终端本区留空，不告警噪音）；
      - detail 补拉失败 → 量比留 0（表格显示 —），排除与排序不受影响；
      - 连击落库失败 → 本轮仍返回结果（连击退化为 1，不丢主功能）。
    异常由调用方（unified_scanner 交易分支）兜底，本函数不吞编程错误。
    """
    if not board_items:
        return []

    prelim = prefilter_board(board_items)
    targets = prelim[:HOT_ENRICH_LIMIT]
    if not targets:
        return []

    symbols = [str(t.get("symbol") or "") for t in targets if t.get("symbol")]
    fetch_batch = getattr(adapter, "fetch_hot_quotes_batch", None)
    if fetch_batch is None:
        return []
    try:
        quotes = fetch_batch(symbols)
    except EXTERNAL_FAILURES as e:
        logger.warning("hot_watch 批量补全失败，本区留空: %s", e)
        return []
    if not quotes:
        return []

    passed, _rejected = build_candidates(targets, quotes)
    if not passed:
        # 仍推进轮次并清零连击：否则「全员被排除」的一轮不会打断上一轮的连击链，
        # 停牌/急跌导致的空榜会被误读为「持续重点」。
        _safe_persist(conn, [])
        return []

    # 仅对最终前 N 名补拉 detail（拿量比 / 真实涨跌停价）：1 请求/票，成本可控。
    if HOT_DETAIL_TOP > 0:
        fetch_detail = getattr(adapter, "fetch_hot_quote_detail", None)
        for c in passed[:HOT_DETAIL_TOP]:
            if fetch_detail is None:
                break
            try:
                detail = fetch_detail(c.symbol)
            except EXTERNAL_FAILURES as e:
                logger.warning("hot_watch detail 补全失败 %s: %s", c.symbol, e)
                continue
            if not detail:
                continue
            c.volume_ratio = _num(detail.get("volume_ratio"))
            # 真实涨跌停价可用时覆盖推算值（仅用于展示口径校准，不回过头重判）
            real_up = _num(detail.get("limit_up"))
            real_down = _num(detail.get("limit_down"))
            if real_up > 0:
                c.limit_up = real_up
            if real_down > 0:
                c.limit_down = real_down

    top = passed[:top_n]
    # 连击以「全部通过者」为基数统计（非仅 TOP N）——否则跌出前 N 但仍在结果中的票
    # 会被误判为「本轮未命中」而清零。update_streaks 会写回 passed 的 streak。
    _safe_persist(conn, passed)
    return top


def _safe_persist(conn, all_hits: Sequence[HotCandidate]) -> None:
    """连击落库（失败只告警，不影响本轮结果返回）。

    update_streaks 直接写回 all_hits 各元素的 streak 字段，故调用方随后读
    top（all_hits 前缀切片）即可拿到正确的连击数，无需二次映射。
    """
    if conn is None:
        return
    try:
        round_no = _next_round(conn)
        update_streaks(conn, all_hits, round_no)
        _prune_stale(conn)
        conn.commit()
    except EXTERNAL_FAILURES as e:
        logger.warning("hot_watch 连击落库失败（本轮结果不受影响）: %s", e)


# ── 独立运行 CLI（python -m scanner.hot_watch）───────────────────────────────
# 主循环里本区随扫描自动跑；此入口用于**离线自检与临时查看**：
#   - --offline-demo 不联网，用内置样本跑通「预筛→补全→排除→打分→排序→渲染」全链路
#   - 在线模式读真实榜单，但**连击落库走内存库**（不污染生产 scanner.db）
# 渲染复用 display.render_hot_watch_standalone（惰性导入：常规 import 本模块不拉起
# 重量级 display 依赖链）。


def _demo_case(
    symbol, code, name, exch, current, percent, last_close, cap, tr=8.0, vol=1e7, amount=1e8, status=1, rc=500, rank=1
):
    """构造一对（榜单条目, 补全行情）—— 覆盖各硬排除分支的离线自检样本。"""
    board = {
        "symbol": symbol,
        "name": name,
        "percent": percent,
        "current": current,
        "rank_change": rc,
        "rank": rank,
        "exchange": exch,
    }
    quote = {
        "symbol": symbol,
        "code": code,
        "name": name,
        "exchange": exch,
        "status": status,
        "current": current,
        "percent": percent,
        "chg": round(current - last_close, 3),
        "volume": vol,
        "amount": amount,
        "turnover_rate": tr,
        "market_capital": cap,
        "float_market_capital": cap * 0.9,
        "last_close": last_close,
        "high": current * 1.01,
        "low": current * 0.99,
    }
    return board, quote


# 期望结果标注在 expect 列：pass=应通过 / 其余为应命中的排除原因关键词
_DEMO_CASES = [
    # —— 应通过 ——
    (
        "pass",
        _demo_case(
            "SZ002443",
            "002443",
            "金洲管道",
            "SZ",
            11.81,
            5.73,
            11.17,
            6.1e9,
            tr=8.6,
            vol=4.491e7,
            amount=5.15e8,
            rc=493,
        ),
    ),
    (
        "pass",
        _demo_case(
            "SZ300862",
            "300862",
            "蓝盾光电",
            "SZ",
            50.10,
            5.76,
            47.37,
            9.249e9,
            tr=18.18,
            vol=2.7532e7,
            amount=1.3569e9,
            rc=1257,
        ),
    ),
    (
        "pass",
        _demo_case(
            "SH605006", "605006", "山东玻纤", "SH", 18.40, 6.24, 17.32, 8.9e9, tr=7.8, vol=2.9e7, amount=5.3e8, rc=6445
        ),
    ),
    # —— 应被排除 ——
    ("ST", _demo_case("SZ002514", "002514", "*ST宝馨", "SZ", 2.65, 9.96, 2.41, 2.4e9, rc=4463)),
    ("非沪深", _demo_case("SH688260", "688260", "昀冢科技", "SH", 106.58, 15.1, 92.60, 1.3e10, rc=3713)),
    ("非沪深", _demo_case("01810", "01810", "小米集团-W", "HK", 26.44, 2.01, 25.92, 6.6e11, rc=6656)),
    ("非沪深", _demo_case("SZ159516", "159516", "半导体ETF", "SZ", 0.652, 2.10, 0.639, 1.1e10, rc=4050)),
    (
        "非正常交易状态",
        _demo_case(
            "SH600000", "600000", "浦发银行", "SH", 9.80, 0.50, 9.75, 2.9e11, status=0, vol=0, amount=0, tr=0.0, rc=900
        ),
    ),
    ("非上涨", _demo_case("SH601899", "601899", "紫金矿业", "SH", 32.23, -5.51, 34.11, 8.5e11, rc=11001)),
    ("涨幅过高", _demo_case("SZ301176", "301176", "逸豪新材", "SZ", 61.06, 7.50, 56.80, 1.03e10, rc=4432)),
    ("已封涨停", _demo_case("SZ002201", "002201", "九鼎新材", "SZ", 11.09, 10.02, 10.08, 5.8e9, rc=4705)),
    ("市值过大", _demo_case("SH600519", "600519", "贵州茅台", "SH", 1277.01, 0.50, 1270.65, 1.6e12, rc=5979)),
]


def run_offline_demo(top_n: int, emit_json: bool) -> int:
    """离线自检：不联网，用内置样本跑通全链路并逐条核对期望结果。"""
    board = [b for _, (b, _q) in _DEMO_CASES]
    quotes = {q["symbol"]: q for _, (_b, q) in _DEMO_CASES}

    # 注意：**故意不先过 prefilter_board** —— 预筛会提前丢掉 ST/非沪深/非上涨三类，
    # 那些样本就走不到 hard_exclude，排除分支无法被逐条验证（会显示"缺失"）。
    # 真实链路里预筛只是省请求量，最终判定仍以 hard_exclude 为准，故自检对全量
    # 样本直接跑 build_candidates；下面另打印预筛存活数作为对照。
    pre_n = len(prefilter_board(board))
    passed, rejected = build_candidates(board, quotes)
    passed_syms = {c.symbol for c in passed}
    rejected_map = {c.symbol: hard_exclude(c) for c in rejected}

    print("【离线自检】不联网，内置样本逐条核对")
    print(f"{'代码':<8}{'名称':<12}{'期望':<16}{'实际':<10}结果")
    print("-" * 60)
    ok = True
    for expect, (b, _q) in _DEMO_CASES:
        sym, name = b["symbol"], b["name"]
        code = _q["code"]
        if expect == "pass":
            actual = "通过" if sym in passed_syms else (rejected_map.get(sym) or "缺失")
            good = sym in passed_syms
        else:
            actual = rejected_map.get(sym) or ("通过" if sym in passed_syms else "缺失")
            good = sym not in passed_syms and expect in (actual or "")
        ok = ok and good
        print(f"{code:<8}{name:<12}{expect:<16}{actual:<10}{'OK' if good else 'FAIL'}")

    print("-" * 60)
    print(f"通过 {len(passed)} 只 / 排除 {len(rejected)} 只 / 样本 {len(_DEMO_CASES)} 条")
    print(f"（真实链路中预筛先滤掉一部分 → 仅 {pre_n} 条会进入行情补全，省请求量）")

    if emit_json:
        print(_rows_to_json(passed[:top_n]))
    else:
        from scanner.display import render_hot_watch_standalone

        render_hot_watch_standalone(passed[:top_n])
    return 0 if ok else 1


def _rows_to_json(rows) -> str:
    """紧凑 JSON 输出（便于程序消费/排查）。"""
    import json

    return json.dumps(
        [
            {
                "code": c.code,
                "symbol": c.symbol,
                "name": c.name,
                "current": round(c.current, 3),
                "percent": round(c.percent, 2),
                "rank_change": c.rank_change,
                "volume": round(c.volume),
                "amount": round(c.amount),
                "turnover_rate": round(c.turnover_rate, 2),
                "market_cap_yi": round(c.market_capital / 1e8, 2),
                "score": round(c.score, 2),
                "streak": c.streak,
                "reasons": c.reasons,
            }
            for c in rows
        ],
        ensure_ascii=False,
        indent=2,
    )


def _cli_conn():
    """CLI 专用内存库：跑连击逻辑但不落生产 scanner.db。"""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE hot_watch_hits (
            symbol TEXT PRIMARY KEY, name TEXT NOT NULL,
            streak INTEGER NOT NULL DEFAULT 0, last_round INTEGER NOT NULL DEFAULT 0,
            last_seen TEXT, last_percent REAL, last_price REAL, last_score REAL)
    """)
    conn.execute("CREATE TABLE hot_watch_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.commit()
    return conn


def main(argv: "list[str] | None" = None) -> int:
    import argparse

    # 先声明：下面 argparse 的 default 就要读这些全局名，global 必须先于首次使用。
    # hard_exclude / compute_score 均引用模块级全局名，故 main 内重新赋值即生效，
    # 无需给核心函数加参数（保持既有签名与单测不动）。
    global HOT_MAX_PERCENT, HOT_MAX_MARKET_CAP, HOT_ENRICH_LIMIT

    p = argparse.ArgumentParser(
        prog="python -m scanner.hot_watch",
        description="沪深飙升·极有可能大涨（独立区·独立运行；主循环内已自动执行，此入口用于自检）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--top", type=int, default=HOT_DISPLAY_TOP, help="展示前 N 只")
    p.add_argument("--max-cap", type=float, default=HOT_MAX_MARKET_CAP / 1e8, help="市值上限(亿元)")
    p.add_argument("--max-percent", type=float, default=HOT_MAX_PERCENT, help="涨幅上限(%%)")
    p.add_argument("--enrich", type=int, default=HOT_ENRICH_LIMIT, help="补全候选上限")
    p.add_argument("--json", action="store_true", help="额外输出 JSON")
    p.add_argument("--offline-demo", action="store_true", help="离线自检：内置样本，不联网")
    p.add_argument("--verbose", action="store_true", help="调试日志")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="[%(levelname)s] %(message)s",
    )

    # 覆盖阈值（全局名已在函数开头声明）
    HOT_MAX_PERCENT = args.max_percent
    HOT_MAX_MARKET_CAP = args.max_cap * 1e8
    HOT_ENRICH_LIMIT = args.enrich

    if args.offline_demo:
        return run_offline_demo(args.top, args.json)

    from scanner.data_source import get_adapter

    adapter = get_adapter()
    board = adapter.fetch_biaosheng(100)
    if not board:
        print("  [!] 飙升榜为空（熔断中/网络异常），无法运行")
        return 1
    print(f"  榜单 {len(board)} 条 | 数据源 {adapter.name}")

    rows = run_hot_watch(adapter, _cli_conn(), board, top_n=args.top)
    if not rows:
        print("  本轮无标的通过筛选（可能全被硬排除）")
        return 0

    if args.json:
        print(_rows_to_json(rows))
    else:
        from scanner.display import render_hot_watch_standalone

        render_hot_watch_standalone(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
