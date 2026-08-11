"""数据源适配层。

抽象统一接口，支持雪球（主）+ AKShare（兜底）双数据源。
当雪球反爬封禁或 make_session 失败时，自动降级到 AKShare，避免单点依赖。

设计要点：
- AKShare 作为可选依赖（lazy import），未安装时自动降级为仅雪球模式
- adapter 输出格式与 api.py 1:1 对齐，下游无感知
- 飙升榜/热搜榜无 AKShare 对应接口，返回空列表让熔断+缓存兜底
- 符号格式由 adapter 内部转换（雪球 SZ300001 ↔ AKShare 300001）
"""
import logging
import math
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from scanner import api
from scanner.config import BEIJING_TZ, DATA_SOURCE
from scanner.models import KlineBar, make_kline_bar

logger = logging.getLogger(__name__)


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
    def fetch_hot_list(self, size: int = 100) -> list[dict]: ...
    def fetch_market_caps_batch(self, symbols: list[str]) -> dict[str, dict]: ...
    def fetch_market_index(self) -> float | None: ...


class XueqiuAdapter:
    """雪球数据源适配器（包装现有 api.py，零改动 api.py）。"""

    name = "xueqiu"

    def __init__(self):
        self._session = None

    def _get_session(self):
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

    def fetch_hot_list(self, size: int = 100) -> list[dict]:
        return api.fetch_xueqiu_hot_list(self._get_session(), size)

    def fetch_market_caps_batch(self, symbols: list[str]) -> dict[str, dict]:
        return api.fetch_market_caps_batch(self._get_session(), symbols)

    def fetch_market_index(self) -> float | None:
        return api.fetch_market_index(self._get_session())


def _as_float(v) -> float | None:
    """安全取 float：None/NaN/±inf/非法 → None（调用方按脏值处理）。

    Python json/DataFrame 均可能出现 NaN/inf（如东财涨停池/资金流停牌行），
    inf 与数值比较恒为真/假会绕过越界判断，与 NaN 同族，统一剔除。
    """
    try:
        f = float(v)
        return None if not math.isfinite(f) else f
    except (TypeError, ValueError):
        return None


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


class AkshareAdapter:
    """AKShare 数据源适配器（兜底）。

    AKShare 走东财/新浪公开接口，无 cookie 依赖，反爬强度低于雪球。
    飙升榜/热搜榜无语义对齐接口，返回空列表让熔断+缓存兜底。
    分钟数据不做兜底（字段差异大，intraday/opening_strength 已有 None 降级）。
    """

    name = "akshare"

    def __init__(self):
        self._ak = None

    def _get_ak(self):
        if self._ak is None:
            import akshare as ak  # lazy import
            self._ak = ak
        return self._ak

    def is_available(self) -> bool:
        try:
            self._get_ak()
            return True
        except ImportError:
            return False
        except Exception:
            return False

    def fetch_kline(self, symbol: str, days: int = 15) -> list[KlineBar] | None:
        """东财 stock_zh_a_hist 失败/空时降级到新浪 stock_zh_a_daily。

        东财 push2his 在本机间歇性不可达（连接被重置，2026-08-11 实测），
        单靠东财兜底等于没有兜底；新浪接口稳定（3/3），且返回完整 OHLCV。
        两份输出都统一走 make_kline_bar 契约，下游无感知。
        """
        ak = self._get_ak()
        code = _xq_to_ak(symbol)
        try:
            result = self._fetch_kline_em(ak, code, days)
            if result:
                return result
        except Exception as e:
            logger.warning("AKShare 东财K线获取失败 %s: %s，降级到新浪", symbol, e)
        try:
            return self._fetch_kline_sina(ak, code, days)
        except Exception as e:
            logger.warning("AKShare 新浪K线获取失败 %s: %s", symbol, e)
            return None

    def _fetch_kline_em(self, ak, code: str, days: int) -> list[KlineBar] | None:
        end = datetime.now(BEIJING_TZ)
        # 多拉一倍天数确保足够交易日（剔除周末/节假日）
        start = end - timedelta(days=days * 2)
        df = ak.stock_zh_a_hist(
            symbol=code, period="daily", adjust="qfq",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
        if df is None or df.empty:
            return None
        result = []
        for _, row in df.iterrows():
            date_str = str(row["日期"])[:10]
            dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=BEIJING_TZ)
            # 统一走 make_kline_bar 契约（数值强转 + close<=0/date 非法剔除），
            # 与雪球 fetch_kline 输出 1:1 对齐，下游无感知。
            bar = make_kline_bar({
                "date": date_str,
                "open": row["开盘"],
                "high": row["最高"],
                "low": row["最低"],
                "close": row["收盘"],
                "volume": row["成交量"],
                "percent": row["涨跌幅"],
            })
            if bar is not None:
                bar["timestamp"] = int(dt.timestamp() * 1000)
                result.append(bar)
        return result if result else None

    def _fetch_kline_sina(self, ak, code: str, days: int) -> list[KlineBar] | None:
        """新浪日线兜底：stock_zh_a_daily 返回全量 qfq，无涨跌幅列 → 由收盘价推算。"""
        market = "sh" if code.startswith("6") else ("bj" if code.startswith(("8", "4")) else "sz")
        df = ak.stock_zh_a_daily(symbol=f"{market}{code}", adjust="qfq")
        if df is None or df.empty:
            return None
        df = df.tail(days * 2)  # 多拉一倍，后续补拉/合并时窗口够用
        result = []
        prev_close: float | None = None
        for _, row in df.iterrows():
            date_str = str(row["date"])[:10]
            dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=BEIJING_TZ)
            close = _as_float(row["close"])
            percent = 0.0
            if close is not None and prev_close:
                percent = (close / prev_close - 1.0) * 100.0
            bar = make_kline_bar({
                "date": date_str,
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": close if close is not None else row["close"],
                "volume": row["volume"],
                "percent": percent,
            })
            if bar is not None:
                bar["timestamp"] = int(dt.timestamp() * 1000)
                result.append(bar)
                prev_close = close if close is not None else bar["close"]
        return result if result else None

    def fetch_biaosheng(self, size: int = 100) -> list[dict]:
        logger.warning("AKShare 无飙升榜对应接口，返回空列表（依赖雪球熔断缓存兜底）")
        return []

    def fetch_hot_list(self, size: int = 100) -> list[dict]:
        logger.warning("AKShare 无热搜榜对应接口，返回空列表")
        return []

    def fetch_market_caps_batch(self, symbols: list[str]) -> dict[str, dict]:
        if not symbols:
            return {}
        try:
            ak = self._get_ak()
            df = ak.stock_zh_a_spot_em()
            if df is None or df.empty:
                return {}
            wanted = {_xq_to_ak(s) for s in symbols}
            result: dict[str, dict] = {}
            for _, row in df.iterrows():
                code = str(row["代码"])
                if code not in wanted:
                    continue
                result[_ak_to_xq(code)] = {
                    "market_cap": _as_float(row.get("总市值")) or 0,
                    "circ_market_cap": _as_float(row.get("流通市值")) or 0,
                    "turnover_rate": _as_float(row.get("换手率")) or 0,
                    "current": _as_float(row.get("最新价")) or 0,
                    "percent": _as_float(row.get("涨跌幅")) or 0,
                }
            return result
        except Exception as e:
            logger.warning("AKShare 市值批量查询失败: %s", e)
            return {}

    def fetch_market_index(self) -> float | None:
        try:
            ak = self._get_ak()
            df = ak.stock_zh_index_spot_em()
            if df is None or df.empty:
                return None
            # 过滤创业板指 399006
            row = df[df["代码"] == "399006"]
            if row.empty:
                return None
            return _as_float(row.iloc[0].get("涨跌幅")) or 0
        except Exception as e:
            logger.warning("AKShare 大盘指数获取失败: %s", e)
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

    def is_available(self) -> bool:
        if self._primary.is_available():
            self._use_primary = True
            return True
        if self._secondary and self._secondary.is_available():
            logger.warning("主数据源 %s 不可用，降级到 %s",
                           self._primary.name, self._secondary.name)
            self._use_primary = False
            return True
        return False

    def _call(self, method: str, *args, **kwargs):
        if self._use_primary:
            try:
                return getattr(self._primary, method)(*args, **kwargs)
            except Exception as e:
                if self._secondary:
                    logger.warning("%s.%s 异常: %s，降级到 %s",
                                   self._primary.name, method, e, self._secondary.name)
                    return getattr(self._secondary, method)(*args, **kwargs)
                raise
        elif self._secondary:
            return getattr(self._secondary, method)(*args, **kwargs)
        raise RuntimeError("无可用数据源")

    def fetch_kline(self, symbol: str, days: int = 15) -> list[KlineBar] | None:
        """雪球 K 线失败时降级到 AKShare 补拉。

        api.fetch_kline 内部吞异常返回 None（网络失败/无数据），不会抛给
        _call，导致原 FallbackAdapter 的"仅异常降级"策略对 K 线形同死代码。
        这里显式处理：primary 返回 None 时视为失败，尝试 secondary 兜底。
        """
        if self._use_primary:
            try:
                result = self._primary.fetch_kline(symbol, days)
                if result is not None:
                    return result
                if self._secondary:
                    logger.warning("%s.fetch_kline 返回空，降级到 %s 补拉 %s",
                                   self._primary.name, self._secondary.name, symbol)
                    return self._secondary.fetch_kline(symbol, days)
                return None
            except Exception as e:
                if self._secondary:
                    logger.warning("%s.fetch_kline 异常: %s，降级到 %s",
                                   self._primary.name, e, self._secondary.name)
                    return self._secondary.fetch_kline(symbol, days)
                raise
        elif self._secondary:
            return self._secondary.fetch_kline(symbol, days)
        raise RuntimeError("无可用数据源")

    def fetch_biaosheng(self, size: int = 100) -> list[dict]:
        return self._call("fetch_biaosheng", size)

    def fetch_hot_list(self, size: int = 100) -> list[dict]:
        return self._call("fetch_hot_list", size)

    def fetch_market_caps_batch(self, symbols: list[str]) -> dict[str, dict]:
        return self._call("fetch_market_caps_batch", symbols)

    def fetch_market_index(self) -> float | None:
        return self._call("fetch_market_index")


_adapter_instance: DataSourceAdapter | None = None


def get_adapter() -> DataSourceAdapter:
    """获取数据源适配器单例。

    根据 config.DATA_SOURCE 决定模式：
    - "auto"（默认）：雪球优先 + AKShare 兜底
    - "xueqiu"：仅雪球（向后兼容）
    - "akshare"：仅 AKShare（调试/雪球全挂时强制）
    """
    global _adapter_instance
    if _adapter_instance is not None:
        return _adapter_instance

    mode = DATA_SOURCE.lower()
    if mode == "xueqiu":
        _adapter_instance = XueqiuAdapter()
    elif mode == "akshare":
        _adapter_instance = AkshareAdapter()
    else:  # auto
        xq = XueqiuAdapter()
        if xq.is_available():
            ak = AkshareAdapter()
            if ak.is_available():
                _adapter_instance = FallbackAdapter(xq, ak)
            else:
                _adapter_instance = xq
                logger.info("AKShare 不可用，仅使用雪球")
        else:
            logger.warning("雪球不可用，尝试 AKShare")
            ak = AkshareAdapter()
            if ak.is_available():
                _adapter_instance = ak
            else:
                raise RuntimeError("无可用数据源（雪球和 AKShare 均不可用）")

    logger.info("数据源已就绪: %s", _adapter_instance.name)
    return _adapter_instance


def reset_adapter():
    """重置单例（仅用于测试）。"""
    global _adapter_instance
    _adapter_instance = None
