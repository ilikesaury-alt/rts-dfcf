"""分时兜底数据层（P2 从 orchestrator.py 抽出，2026-08-21）。

盘中 K 线补拉失败时，用分时数据构造今日 bar 的兜底逻辑（2026-08-14 网宿科技
案例引入）。只依赖通用件（models/trading_session/utils），不感知扫描流水线，
kline_fetch.fetch_all_klines 是唯一生产调用方。
"""

import threading
from datetime import date

from scanner.config import now_beijing
from scanner.models import KlineBar, StockInfo, make_kline_bar
from scanner.trading_session import is_trading_time

# 盘中 K 线补拉失败兜底（2026-08-14）：分时数据构造今日 bar 的限时（秒）。
# 主链路已由 KLINE_FETCH_DEADLINE 兜底，此兜底只对补拉失败的票追加一次分时拉取，
# 单独限时避免 60s 扫描循环被拖垮。
TODAY_BAR_MINUTE_TIMEOUT = 8.0


def build_today_bar_from_minute(adapter, stock: StockInfo, today: date) -> KlineBar | None:
    """盘中 K 线补拉失败时，用分时数据构造今日 bar（2026-08-14 网宿科技案例）。

    背景：盘中在榜票若 K 线补拉失败（API 超时/异常），回退旧缓存（无今日 bar）。
    此时 _compute_volume_metrics 把昨日量当今日量，量比恒 <1.0 → short_term
    量比硬门（validator.v_st_vol_gate）误杀放量启动票（网宿 10:56~14:14 在榜
    3 小时未被推荐即此根因）。分时接口独立于日线接口，补拉失败时往往仍可用，
    用当日累计量能构造今日 bar，使量比/涨幅基于真实今日盘面而非昨日。

    注意：构造 bar 只用于本轮评分（merge 进返回的 kline），不写 DB、不影响缓存，
    下轮 KLINE_REFRESH_TTL 过期后仍会正常补拉日线。失败返回 None（维持旧回退行为）。
    """
    try:
        items = adapter.fetch_minute(stock.symbol)
    except Exception as e:
        print(f"  [!] 今日bar分时兜底失败 {stock.symbol}: {e}")
        return None
    if not items:
        return None
    # 分时 item 结构：{timestamp, volume, current, avg_price, high, low, percent}
    try:
        total_vol = sum(float(i.get("volume") or 0) for i in items)
        if total_vol <= 0:
            return None
        current = float(items[-1].get("current") or 0)
        if current <= 0:
            current = float(items[-1].get("avg_price") or 0)
        if current <= 0:
            return None
        high = max(float(i.get("high") or 0) for i in items)
        low = min(float(i.get("low") or 0) for i in items)
        if high <= 0 or low <= 0:
            high = max(current, high)
            low = min(current, low) if low > 0 else current
        percent = float(items[-1].get("percent") or 0)
        if percent == 0 and stock.percent:
            percent = stock.percent
        return make_kline_bar({
            "date": today.isoformat(),
            "open": float(items[0].get("current") or current),
            "high": high,
            "low": low,
            "close": current,
            "volume": total_vol,
            "percent": percent,
        })
    except (TypeError, ValueError):
        return None


def merge_minute_today_bar(adapter, stock: StockInfo | None, today: date,
                           stale: list[KlineBar],
                           deadline: float | None = None) -> list[KlineBar] | None:
    """把分时构造的今日 bar 合并进旧缓存；失败/非盘中返回 None（维持原回退）。

    限时 TODAY_BAR_MINUTE_TIMEOUT 兜底：分时接口自身无 timeout，超时线程后台自然
    结束（daemon），主扫描循环不挂死。仅交易时段启用——收盘后缺今日 bar 属正常，
    不值得为每个回退票再发一次分时请求。

    deadline（2026-08-17 审查新增）：整个兜底阶段共享的总预算时间戳——单只 join(8s)
    限时存在但串行叠加无总量上限（API 故障时 N 只 × 8s 可拖垮单轮扫描），调用方
    kline_fetch.fetch_all_klines 用 MINUTE_FALLBACK_PHASE_DEADLINE 设好共享 deadline 传入，
    剩余时间不足即跳过该票（维持旧缓存回退），保证兜底阶段总量有界。
    """
    if not is_trading_time() or not stale or stock is None:
        return None
    timeout = TODAY_BAR_MINUTE_TIMEOUT
    if deadline is not None:
        remaining = deadline - now_beijing().timestamp()
        if remaining <= 0:
            return None
        timeout = min(timeout, remaining)
    box: dict = {}

    def _run():
        try:
            box["bar"] = build_today_bar_from_minute(adapter, stock, today)
        except BaseException:  # noqa: BLE001
            box["bar"] = None

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=timeout)
    bar = box.get("bar")
    if bar is None or t.is_alive():
        return None
    merged = {k["date"]: k for k in stale}
    merged[bar["date"]] = bar
    return sorted(merged.values(), key=lambda x: x["date"])
