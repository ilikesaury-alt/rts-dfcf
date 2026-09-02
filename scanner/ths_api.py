"""同花顺金融数据API 官方客户端（2026-08-23 接入）。

数据源：同花顺官方面向开发者/AI Agent 的金融数据服务（fuyao.aicubes.cn，
REST + X-api-key 鉴权）。接入前探测结论（scripts/ths_api_probe.py，2026-08-23 实测）：
- 快照实时（数据时间戳滞后 0s），单请求延迟 ~0.8s
- 历史K线 forward 复权 vs 本地雪球 daily_kline 对账 120/120 一致（容差 0.5%；
  个别票分红后 qfq 锚点微差 ~0.36%，故交叉验证须用相对容差而非逐分对齐）
- 软限流：无硬 4001 拒绝，10 并发下单请求延迟 760ms→2.7s（吞吐 ≈3.8 req/s）
  → 只用于低频场景（涨停池 TTL 300s / 数据健康抽验），不进盘中热路径

当前用途：
1. 涨停池/炸板池主源（market_extra.fetch_zt_pool，AKShare 降为兜底）——
   官方接口免 _bounded_call 兜底且字段更富（封单额/涨停原因/开板次数）
2. data_health 第一交叉验证源（新浪 qfq 降为回退参照）
3. 财务风险过滤主源（fundamentals，估值快照 pb_mrq<0 ⟺ 净资产为负，
   替代 pywencai 问财；问财降为兜底）
4. K线兜底适配器（data_source.ThsAdapter，替代 AKShare 东财→新浪双兜底链）

不提供分钟K/tick/换手率/量比/市值——盘中链路仍走雪球/东财。
"""
import logging
import os
import threading
from datetime import datetime, timedelta

import requests

from scanner.config import BEIJING_TZ
from scanner.models import KlineBar, make_kline_bar
from scanner.utils import to_float

logger = logging.getLogger(__name__)

BASE_URL = "https://fuyao.aicubes.cn"
REQUEST_TIMEOUT = (5, 15)  # 连接/读取双段超时（与项目 _request_with_retry 同风格）

_key_lock = threading.Lock()
_api_key: str | None = None


def get_api_key() -> str:
    """API Key：环境变量 HITHINK_FINANCE_API_KEY 优先，缺失回退项目根 .env。"""
    global _api_key
    if _api_key is not None:
        return _api_key
    with _key_lock:
        if _api_key is None:
            key = os.environ.get("HITHINK_FINANCE_API_KEY", "")
            if not key:
                key = _read_env_file_key()
            _api_key = key.strip()
    return _api_key


def _read_env_file_key() -> str:
    """从项目根 .env 读 Key（只读一次文件，不写入 os.environ 防泄漏）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, ".env")
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("HITHINK_FINANCE_API_KEY="):
                    val = line.split("=", 1)[1].strip()
                    if val:
                        return val
    except OSError:
        pass  # 无 .env 文件属正常（未配置 → 上层禁用 THS 源）
    return ""


def xq_to_ths(symbol: str) -> str:
    """xq 符号转 thscode：SZ300033 → 300033.SZ；已是 thscode 格式原样返回。"""
    s = (symbol or "").strip().upper()
    if "." in s or len(s) != 8:
        return s
    return f"{s[2:]}.{s[:2]}"


def _call(path: str, params: dict | None = None) -> dict | None:
    """单次 GET；网络失败/非 JSON 返回 None（调用方按失败降级，不重试不阻塞）。

    限流为软排队（实测无硬拒绝），requests 双段超时已足够兜住，无需 daemon 线程。
    """
    key = get_api_key()
    if not key:
        return None
    try:
        r = requests.get(f"{BASE_URL}{path}", params=params or {},
                         headers={"X-api-key": key}, timeout=REQUEST_TIMEOUT)
        body = r.json()
        return body if isinstance(body, dict) else None
    except Exception:  # noqa: BLE001  网络/解析失败 → None，调用方降级
        return None


def _items(body: dict | None) -> list:
    """统一解 ApiResponse 信封的 data.item；code!=0 视为失败返回空。"""
    if not isinstance(body, dict) or body.get("code") != 0:
        return []
    return ((body.get("data") or {}).get("item")) or []


def date_str_to_ms(date_str: str) -> int | None:
    """YYYYMMDD 或 YYYY-MM-DD → 北京时区当日零点毫秒戳；解析失败返回 None。

    公开给 market_extra 等模块使用（2026-08-24 审查：此前跨模块访问私有
    _date_to_ms）。tz-aware 统一北京时区，不依赖主机 TZ（2026-08-24 审查：
    naive timestamp/fromtimestamp 在非 CST 主机上 bar 日期标签会偏移一天，
    交叉验证对账将系统性误报）。
    """
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(date_str, fmt).replace(tzinfo=BEIJING_TZ)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return None


def _ms_to_date_str(date_ms: float) -> str:
    """毫秒戳 → 北京时区 YYYY-MM-DD（bar 日期标签统一口径）。"""
    return datetime.fromtimestamp(date_ms / 1000, tz=BEIJING_TZ).strftime("%Y-%m-%d")


# 私有名保留为别名（兼容既有内部调用）
_date_to_ms = date_str_to_ms


def _safe_int(v) -> int:
    return int(to_float(v) or 0)


def _safe_float(v) -> float:
    return float(to_float(v) or 0.0)


def fetch_limit_up_pool(date_ms: int | None = None,
                        include_break: bool = True) -> dict[str, dict] | None:
    """涨停池（官方主源），返回与 market_extra.fetch_zt_pool 相同契约：

        {6位代码: {lianban, zt_stat, fengban_amt, zhaban, industry}}

    - lianban=continue_day_cnt、fengban_amt=seal_money；
      zhaban 取自炸板池 limit-break-pool 的 open_times（多一次请求，TTL 级低频
      可接受；炸板池失败时兜 0 不影响涨停主数据）；
      industry 填 THS 涨停原因 limit_up_reason（语义最近的既有字段）。
    - code!=0 / 网络失败返回 None（区别于"非交易日合法空表"的 {}，
      调用方据此决定是否降级 AKShare 兜底）。
    """
    params: dict = {"size": 200}
    if date_ms:
        params["date_ms"] = date_ms
    body = _call("/api/a-share/special-data/limit-up-pool", params)
    if body is None or body.get("code") != 0:
        return None
    # 信封 code=0 即成功：空 item 是"非交易日合法空表"，不是失败
    items = _items(body)
    break_map: dict[str, int] = {}
    if include_break:
        break_map = _fetch_break_open_times(date_ms)
    result: dict[str, dict] = {}
    for it in items:
        code = str(it.get("ticker") or "").strip()
        if not code:
            continue
        result[code] = {
            "lianban": _safe_int(it.get("continue_day_cnt")),
            "zt_stat": str(it.get("continue_day_text") or ""),
            "fengban_amt": _safe_float(it.get("seal_money")),
            "zhaban": break_map.get(code, 0),
            "industry": str(it.get("limit_up_reason") or ""),
        }
    return result


def _fetch_break_open_times(date_ms: int | None = None) -> dict[str, int]:
    """炸板池 {6位代码: 开板次数}；失败返回 {}（zhaban 兜 0，不影响涨停主数据）。"""
    params: dict = {"size": 200}
    if date_ms:
        params["date_ms"] = date_ms
    out: dict[str, int] = {}
    for it in _items(_call("/api/a-share/special-data/limit-break-pool", params)):
        code = str(it.get("ticker") or "").strip()
        if code:
            out[code] = _safe_int(it.get("open_times"))
    return out


def fetch_kline_closes(symbol: str, start_date: str, end_date: str,
                       adjust: str = "forward") -> dict[str, float] | None:
    """历史日K收盘序列 {YYYY-MM-DD: close}；失败返回 None。

    data_health 交叉验证用：symbol 为 xq 格式（SZ300033），
    start/end 为 YYYY-MM-DD（end 含当日）。
    """
    try:
        start_ms = int(datetime.strptime(start_date, "%Y-%m-%d")
                       .replace(tzinfo=BEIJING_TZ).timestamp() * 1000)
        end_ms = (int(datetime.strptime(end_date, "%Y-%m-%d")
                      .replace(tzinfo=BEIJING_TZ).timestamp() * 1000)
                  + int(timedelta(days=1).total_seconds() * 1000) - 1)
    except ValueError:
        return None
    body = _call("/api/a-share/prices/historical", {
        "thscode": xq_to_ths(symbol), "interval": "1d",
        "start": start_ms, "end": end_ms, "adjust": adjust})
    if not isinstance(body, dict) or body.get("code") != 0:
        return None
    out: dict[str, float] = {}
    for bar in ((body.get("data") or {}).get("item")) or []:
        raw = bar.get("date_ms")
        close = to_float(bar.get("close_price"), None)
        if raw is None or close is None or close <= 0:
            continue  # 与 KlineBar 契约同族：脏 bar 剔除
        d = _ms_to_date_str(raw)
        out[d] = close
    return out


def fetch_kline_bars(symbol: str, days: int = 15,
                     adjust: str = "forward") -> list[KlineBar] | None:
    """近 N 交易日日K（KlineBar 契约，data_source.ThsAdapter 兜底用）；失败返回 None。

    THS 无涨跌幅列 → percent 由收盘价推算（首根 0，与原新浪兜底同款处理）。
    多拉一倍天数确保剔除周末/节假日后窗口足够（与旧 AKShare 兜底同策略）。
    """
    end = datetime.now(tz=BEIJING_TZ)
    start = end - timedelta(days=days * 2)
    body = _call("/api/a-share/prices/historical", {
        "thscode": xq_to_ths(symbol), "interval": "1d",
        "start": int(start.timestamp() * 1000),
        "end": int(end.timestamp() * 1000) - 1,
        "adjust": adjust})
    if not isinstance(body, dict) or body.get("code") != 0:
        return None
    bars: list[KlineBar] = []
    prev_close: float | None = None
    for raw in ((body.get("data") or {}).get("item")) or []:
        date_ms = raw.get("date_ms")
        if date_ms is None:
            continue
        date_str = _ms_to_date_str(date_ms)
        close = to_float(raw.get("close_price"), None)
        percent = 0.0
        if close is not None and prev_close:
            percent = (close / prev_close - 1.0) * 100.0
        bar = make_kline_bar({
            "date": date_str,
            "open": raw.get("open_price"),
            "high": raw.get("high_price"),
            "low": raw.get("low_price"),
            "close": raw.get("close_price"),
            "volume": raw.get("volume"),
            "percent": percent,
        })
        if bar is not None:
            bar["timestamp"] = date_ms
            bars.append(bar)
            prev_close = bar["close"]
    return bars if bars else None


def fetch_gem_codes() -> list[str] | None:
    """创业板代码表（300xxx 且 SZ），返回 6 位代码升序列表；失败返回 None。

    单次请求（limit=10000 覆盖全 A 股 ~5400 只），fundamentals 财务风险过滤
    用它圈定估值快照的查询范围（扫描器只做创业板，无需全市场）。
    """
    body = _call("/api/meta/tickers/list",
                 {"asset_type": "a-share", "limit": 10000, "offset": 0})
    if not isinstance(body, dict) or body.get("code") != 0:
        return None
    items = ((body.get("data") or {}).get("item")) or []
    codes = set()
    for it in items:
        t = str(it.get("ticker") or "").strip()
        if t.startswith("30") and it.get("exchange") == "SZ":
            codes.add(t)
    return sorted(codes)


def _to_thscode(code: str) -> str:
    """6 位裸代码 → 完整 thscode（300001 → 300001.SZ）；已带后缀原样返回。"""
    s = (code or "").strip().upper()
    if "." in s:
        return s
    if s.startswith(("6", "9")):
        return f"{s}.SH"
    if s.startswith(("4", "8")):
        return f"{s}.BJ"
    return f"{s}.SZ"


def fetch_valuations(thscodes: list[str]) -> dict[str, float] | None:
    """估值快照（单批 ≤100 只）：{6位代码: pb_mrq}；失败返回 None。

    入参接受 6 位裸代码或完整 thscode（服务端要求完整 thscode，内部自动转换）。
    fundamentals 财务风险过滤主源：pb_mrq<0 ⟺ 每股净资产为负（资不抵债），
    文档明确负值原样返回；pb_mrq=null（未披露/停牌）不含在结果中——无法判定
    即不算命中（与问财只返回命中集合同语义，fail-open 不误杀）。
    """
    if not thscodes:
        return {}
    body = _call("/api/a-share/valuations/snapshot",
                 {"thscodes": ",".join(_to_thscode(c) for c in thscodes)})
    if not isinstance(body, dict) or body.get("code") != 0:
        return None
    out: dict[str, float] = {}
    for it in _items(body):
        t = str(it.get("ticker") or "").strip()
        pb = to_float(it.get("pb_mrq"), None)
        if t and pb is not None:
            out[t] = pb
    return out
