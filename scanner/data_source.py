"""数据源适配层。

抽象统一接口，支持雪球（主）+ 同花顺官方 API（兜底）双数据源。
当雪球反爬封禁或 make_session 失败时，自动降级到 THS，避免单点依赖。

设计要点（2026-08-23 重构：兜底源 AKShare → THS 官方 API）：
- THS 兜底仅需配置 HITHINK_FINANCE_API_KEY（无 Key 时自动降级为仅雪球模式）
- adapter 输出格式与 api.py 1:1 对齐，下游无感知
- 飙升榜无语义对齐接口，返回空列表让熔断+缓存兜底
- 市值批量查询 THS 无字段，保留东财 push2delay 直连实现
- 大盘指数兜底保留 akshare 东财 spot 为可选路径（未安装返回 None 干净降级）
"""

import logging
import threading
from datetime import datetime
from typing import Protocol, runtime_checkable

import requests

from scanner import api
from scanner.config import BEIJING_TZ, DATA_SOURCE
from scanner.models import KlineBar
from scanner.net import EASTMONEY_HEADERS, EASTMONEY_UT_TOKEN
from scanner.utils import to_float

logger = logging.getLogger(__name__)

_ths_minute_warned = False  # THS 无分钟K 能力边界告警只打一次


@runtime_checkable
class DataSourceAdapter(Protocol):
    """数据源适配器统一接口。

    所有方法签名与 api.py 对应函数一致（去掉 session 参数），
    输出格式与 api.py 1:1 对齐，下游无感知。
    """

    name: str

    def is_available(self) -> bool: ...
    def fetch_kline(self, symbol: str, days: int = 15) -> list[KlineBar] | None: ...
    def fetch_biaosheng(self, size: int = 100) -> list[dict]: ...
    def fetch_market_caps_batch(self, symbols: list[str]) -> dict[str, dict]: ...
    def fetch_market_index(self) -> float | None: ...
    def get_market_index_meta(self) -> tuple[float | None, str | None, str]:
        """大盘指数取数的血缘元数据 (涨幅, bar日期, 数据源名)，供落库审计。

        bar 日期是「读到的是哪一天的数据」的权威证据——曾因 kline 接口 begin/count
        语义错位把当日 -6.26% 读成昨日 -0.93%（展示"大盘中性"）而无痕。涨幅本身
        是瞬时值，不落库就无法事后审计"当时读到了什么"。
        """
        return (None, None, self.name)

    def fetch_minute(self, symbol: str) -> list[dict] | None:
        """当日分时数据（分钟 bar 列表），无数据/不支持的源返回 None（降级为无分时信号）。

        AKShare 暂不提供分时兜底（字段差异大），返回 None 让 intraday/opening/live
        三个评分维度整体降级——与 api._fetch_minute_data 的 None 语义一致。
        """


class XueqiuAdapter:
    """雪球数据源适配器（包装现有 api.py，零改动 api.py）。"""

    name = "xueqiu"

    def __init__(self):
        self._session = None
        self._session_lock = threading.Lock()

    def _get_session(self):
        if self._session is None:
            # 2026-08-20 加固：懒初始化加锁——intraday_fetch 6 工作线程首次并发调用
            # 时若未加锁会双建 session（各线程拿到不同实例，cookie/自愈状态分叉）。
            with self._session_lock:
                if self._session is None:
                    self._session = api.make_session()
        return self._session

    def is_available(self) -> bool:
        try:
            self._get_session()
            return True
        except Exception as e:
            logger.warning("雪球 session 不可用: %s", e)
            return False

    def fetch_kline(self, symbol: str, days: int = 15) -> list[KlineBar] | None:
        return api.fetch_kline(self._get_session(), symbol, days)

    def fetch_biaosheng(self, size: int = 100) -> list[dict]:
        return api.fetch_biaosheng(self._get_session(), size)

    def fetch_market_caps_batch(self, symbols: list[str]) -> dict[str, dict]:
        return api.fetch_market_caps_batch(self._get_session(), symbols)

    def fetch_market_index(self) -> float | None:
        return api.fetch_market_index(self._get_session())

    def get_market_index_meta(self) -> tuple[float | None, str | None, str]:
        pct, bar_date = api.get_market_index_meta()
        return pct, bar_date, self.name

    def fetch_minute(self, symbol: str) -> list[dict] | None:
        return api._fetch_minute_data(self._get_session(), symbol)


def _as_float(v) -> float | None:
    """安全取 float：None/NaN/±inf/非法 → None（调用方按脏值处理，统一走 utils.to_float）。"""
    return to_float(v, None)


def _xq_to_ak(symbol: str) -> str:
    """雪球符号 → AKShare 符号（去市场前缀）。SZ300001 → 300001"""
    return symbol[2:] if symbol[:2] in ("SZ", "SH", "BJ") else symbol


def _ak_to_xq(code: str) -> str:
    """AKShare 符号 → 雪球符号（加市场前缀）。300001 → SZ300001"""
    if code.startswith(("SH", "SZ", "BJ", "sh", "sz", "bj")):
        return code.upper()
    if code.startswith("399"):
        return "SZ" + code
    if code.startswith("6"):
        return "SH" + code
    if code.startswith(("0", "3")):
        return "SZ" + code
    if code.startswith(("8", "4")):
        return "BJ" + code
    return code


class ThsAdapter:
    """同花顺官方 API 兜底适配器（2026-08-23 接入，替代 AKShare 双兜底链）。

    - K线兜底：官方 prices/historical（forward qfq），替代原 akshare
      东财 stock_zh_a_hist → 新浪 stock_zh_a_daily 双兜底（两个爬虫接口
      均曾间歇性不可达；官方 REST 更稳且免 pandas 解析）
    - 市值兜底：THS 无市值字段，保留东财 push2delay ulist 直连实现
    - 大盘指数兜底：akshare 东财 spot 降为可选路径（未安装返回 None）
    - 飙升榜无语义对齐接口，返回空列表让熔断+缓存兜底；
      分时不做兜底（字段差异大，intraday/opening_strength 已有 None 降级）

    可用性：配置了 HITHINK_FINANCE_API_KEY 即可用（不打网络探测）。
    THS 软限流（实测 ≈3.8 req/s）对兜底场景可接受：K线补拉本就逐票串行
    且上层有 KLINE_FETCH_DEADLINE 兜底。
    """

    name = "ths"

    def __init__(self):
        self._last_index_meta = (None, None, self.name)

    def is_available(self) -> bool:
        from scanner import ths_api

        return bool(ths_api.get_api_key())

    def fetch_kline(self, symbol: str, days: int = 15) -> list[KlineBar] | None:
        from scanner import ths_api

        try:
            result = ths_api.fetch_kline_bars(symbol, days)
            if result:
                return result
        except Exception as e:  # noqa: BLE001
            logger.warning("THS K线获取失败 %s: %s", symbol, e)
        return None

    def fetch_biaosheng(self, size: int = 100) -> list[dict]:
        logger.warning("THS 公开 API 无雪球飙升榜语义对齐接口，返回空列表（依赖雪球熔断缓存兜底）")
        return []

    def fetch_market_caps_batch(self, symbols: list[str]) -> dict[str, dict]:
        """市值批量查询：直连东财 push2delay（可达）ulist.np/get 按 secids 精确查。

        2026-08-19 修复：原走 akshare stock_zh_a_spot_em()（内部硬编码
        push2.eastmoney.com），本机直连/代理均不可达（ProxyError），兜底形同虚设。
        push2delay.eastmoney.com 提供相同 clist/ulist API 且可达（数据可能延迟，
        与资金流同款注，见 market_extra.py）；ulist.np/get 按 secids 只查请求的
        票（无需拉全市场 5292 只再过滤，更快更稳）。仅作雪球失败兜底。
        """
        if not symbols:
            return {}
        try:
            unique = sorted({_xq_to_ak(s) for s in symbols})
            # 东财 secids 前缀：0=SZ 深市(含300), 1=SH 沪市(含60), 2=BJ
            secids = ",".join(
                ("1." if c.startswith("6") else "2." if c.startswith("8") or c.startswith("4") else "0.") + c
                for c in unique
            )
            params = {
                "secids": secids,
                "fields": "f12,f14,f2,f3,f8,f20,f21",
                "fltt": "2",
                "invt": "2",
                "ut": EASTMONEY_UT_TOKEN,
            }
            headers = EASTMONEY_HEADERS
            resp = requests.get(
                "https://push2delay.eastmoney.com/api/qt/ulist.np/get",
                params=params,
                timeout=10,
                headers=headers,
            )
            data = resp.json().get("data") or {}
            rows = data.get("diff") or []
            result: dict[str, dict] = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                code = str(row.get("f12") or "").strip()
                if not code:
                    continue
                # 只保留请求的票（防御接口返回额外行）
                if code not in unique:
                    continue
                result[_ak_to_xq(code)] = {
                    "market_cap": _as_float(row.get("f20")) or 0,
                    "circ_market_cap": _as_float(row.get("f21")) or 0,
                    "turnover_rate": _as_float(row.get("f8")) or 0,
                    "current": _as_float(row.get("f2")) or 0,
                    "percent": _as_float(row.get("f3")) or 0,
                }
            return result
        except Exception as e:
            logger.warning("东财 push2delay 市值批量查询失败: %s", e)
            return {}

    def fetch_market_index(self) -> float | None:
        """大盘指数兜底：akshare 东财 spot（可选路径，未安装返回 None 干净降级）。

        THS 公开 API 暂无与雪球口径对齐的指数 spot 接口；akshare 未安装时
        本兜底静默失效——主源雪球 + data_health 东财对账仍覆盖该维度。
        """
        try:
            import akshare as ak  # lazy import，可选依赖
        except Exception:  # noqa: BLE001  未安装 → 兜底失效，干净降级
            logger.warning("akshare 未安装，THS 适配器的大盘指数兜底不可用")
            self._last_index_meta = (None, None, "none")
            return None
        try:
            df = ak.stock_zh_index_spot_em()
            if df is None or df.empty:
                self._last_index_meta = (None, None, "akshare")
                return None
            # 过滤创业板指 399006
            row = df[df["代码"] == "399006"]
            if row.empty:
                self._last_index_meta = (None, None, "akshare")
                return None
            pct = _as_float(row.iloc[0].get("涨跌幅")) or 0
            # 东财 spot 恒为当日实况 → bar 日期即今日（same-day 语义，区别于雪球 kline 旧 bar）
            today = datetime.now(BEIJING_TZ).date().isoformat()
            self._last_index_meta = (pct, today, "akshare")
            return pct
        except Exception as e:
            logger.warning("AKShare 大盘指数获取失败: %s", e)
            self._last_index_meta = (None, None, "akshare")
            return None

    def get_market_index_meta(self) -> tuple[float | None, str | None, str]:
        return getattr(self, "_last_index_meta", (None, None, self.name))

    def fetch_minute(self, symbol: str) -> list[dict] | None:
        # THS 无分钟K/tick 是恒定能力边界（非故障）：告警只打一次，避免停牌票多时
        # 每票每轮刷屏（2026-08-24 审查）。
        global _ths_minute_warned
        if not _ths_minute_warned:
            _ths_minute_warned = True
            logger.warning("THS 公开 API 无分钟K/tick，分时信号走主源/降级")
        return None


class FallbackAdapter:
    """组合适配器：primary 异常时降级到 secondary。

    降级策略：
    - is_available() 启动时决定主备（雪球优先）
    - 单个方法调用：primary 抛异常 → 降级到 secondary
    - 不基于返回值空判断降级（空列表/None 是合法结果，由各 adapter 内部容错处理）
    """

    name = "fallback"

    def __init__(self, primary: DataSourceAdapter, secondary: DataSourceAdapter | None = None):
        self._primary = primary
        self._secondary = secondary
        self._use_primary = True
        self._last_used_source: str | None = None  # 最近一次请求实际使用的源名

    def is_available(self) -> bool:
        if self._primary.is_available():
            self._use_primary = True
            return True
        if self._secondary and self._secondary.is_available():
            logger.warning("主数据源 %s 不可用，降级到 %s", self._primary.name, self._secondary.name)
            self._use_primary = False
            return True
        return False

    def _call(self, method: str, *args, **kwargs):
        """统一调用主/备数据源，覆盖"返回空"与"抛异常"两类失败。

        - 主数据源**抛异常**：降级 secondary（旧逻辑）。
        - 主数据源**返回 None 或空 dict {}**：同样视为失败，降级 secondary。
          这覆盖了 api.py 吞异常返 None/{} 的三个接口（kline / market_caps_batch /
          market_index）——旧实现需为每个接口手写 _call_with_none_fallback 分支，
          现已统一进本方法，三个 fetch_* 退化为单行委托。
        - 0.0（大盘平盘）是合法值，不触发兜底；空 list（K线无数据）同样是合法结果，
          不触发兜底。故判定只用 `is None` 与 `== {}`，不用笼统的 falsy。
        - 无 secondary 时：异常照旧上抛；None/{} 则原样返回（调用方干净降级）。
        """
        if self._use_primary:
            try:
                result = getattr(self._primary, method)(*args, **kwargs)
            except Exception as e:
                if self._secondary:
                    logger.warning("%s.%s 异常: %s，降级到 %s", self._primary.name, method, e, self._secondary.name)
                    self._last_used_source = self._secondary.name
                    return getattr(self._secondary, method)(*args, **kwargs)
                raise
            if result is None or result == {}:
                if self._secondary:
                    logger.warning("%s.%s 返回空，降级到 %s", self._primary.name, method, self._secondary.name)
                    self._last_used_source = self._secondary.name
                    return getattr(self._secondary, method)(*args, **kwargs)
                return result
            self._last_used_source = self._primary.name
            return result
        elif self._secondary:
            self._last_used_source = self._secondary.name
            return getattr(self._secondary, method)(*args, **kwargs)
        raise RuntimeError("无可用数据源")

    def fetch_kline(self, symbol: str, days: int = 15) -> list[KlineBar] | None:
        # api.fetch_kline 失败返 None（网络失败/无数据）→ _call 统一降级 secondary
        return self._call("fetch_kline", symbol, days)

    def fetch_biaosheng(self, size: int = 100) -> list[dict]:
        return self._call("fetch_biaosheng", size)

    def fetch_market_caps_batch(self, symbols: list[str]) -> dict[str, dict]:
        # api.fetch_market_caps_batch 失败返 {}（空 dict）→ _call 统一降级 secondary
        return self._call("fetch_market_caps_batch", symbols)

    def fetch_market_index(self) -> float | None:
        # api.fetch_market_index 失败返 None → _call 统一降级 secondary
        return self._call("fetch_market_index")

    def get_market_index_meta(self) -> tuple[float | None, str | None, str]:
        """委托实际生效的数据源返回指数血缘元数据。

        2026-08-24 第二轮审查：_use_primary 只在 is_available() 更新（启动后恒
        True），兜底成功的轮次按它推断会把 akshare/THS 数据记成 "xueqiu"——
        血缘日志 source 失真。以 _call 记录的最近一次实际使用源为准。
        """
        used = self._primary if self._use_primary else self._secondary
        last = self._last_used_source
        if last is not None:
            if last == self._primary.name:
                used = self._primary
            elif self._secondary is not None and last == self._secondary.name:
                used = self._secondary
        if used is not None and hasattr(used, "get_market_index_meta"):
            try:
                return used.get_market_index_meta()
            except Exception:  # noqa: BLE001
                pass
        return (None, None, used.name if used is not None else "unknown")

    def fetch_minute(self, symbol: str) -> list[dict] | None:
        # 主源返回 None 是合法降级值（开盘 <2 根 bar/停牌票/临时失败，api 层有负缓存），
        # 不走 _call——否则每只票每轮都转投 THS 再吃一次"无分钟K"告警（2026-08-24 审查）。
        try:
            return self._primary.fetch_minute(symbol)
        except Exception as e:
            if self._secondary:
                logger.warning("%s.fetch_minute 异常: %s，降级到 %s", self._primary.name, e, self._secondary.name)
                return self._secondary.fetch_minute(symbol)
            raise


_adapter_instance: DataSourceAdapter | None = None
_adapter_lock = threading.Lock()


def get_adapter() -> DataSourceAdapter:
    """获取数据源适配器单例。

    根据 config.DATA_SOURCE 决定模式：
    - "auto"（默认）：雪球优先 + THS 兜底
    - "xueqiu"：仅雪球（向后兼容）
    - "akshare"/"ths"：仅 THS 兜底源（调试/雪球全挂时强制；旧值 "akshare" 保留兼容）

    双检锁：is_available() 走真实网络探测（秒级），多线程同时首次调用会重复
    探测甚至构造多个适配器——锁保证只初始化一次（审查卫生项，原判「良性竞态」）。
    """
    global _adapter_instance
    if _adapter_instance is not None:
        return _adapter_instance
    with _adapter_lock:
        if _adapter_instance is not None:
            return _adapter_instance
        _adapter_instance = _build_adapter()
    logger.info("数据源已就绪: %s", _adapter_instance.name)
    return _adapter_instance


def _build_adapter() -> DataSourceAdapter:
    """按 DATA_SOURCE 模式构造适配器（仅 get_adapter 持锁调用）。"""
    mode = DATA_SOURCE.lower()
    if mode == "xueqiu":
        return XueqiuAdapter()
    if mode in ("akshare", "ths"):
        return ThsAdapter()
    # auto：雪球优先 + THS 兜底
    xq = XueqiuAdapter()
    ths = ThsAdapter()
    if xq.is_available():
        if ths.is_available():
            return FallbackAdapter(xq, ths)
        logger.info("THS API Key 未配置，仅使用雪球")
        return xq
    # 2026-08-24 审查修复：启动瞬间雪球探测失败（网络抖动/反爬窗口期一次即可）不再
    # 永久锁死为 THS-only——此前此处直接返回裸 ThsAdapter 且单例永不复探，长跑进程
    # 整天飙升榜返空（全天零候选）。现双源配置时恒构造 FallbackAdapter，由 _call
    # 逐请求降级承担故障切换，运行期雪球恢复即自动回主源。
    if ths.is_available():
        logger.warning("雪球启动探测不可用，恒构造 FallbackAdapter 由逐请求降级接管")
        return FallbackAdapter(xq, ths)
    raise RuntimeError("无可用数据源（雪球不可用且 THS_API_KEY 未配置）")


def reset_adapter():
    """重置单例（仅用于测试）。"""
    global _adapter_instance
    _adapter_instance = None
