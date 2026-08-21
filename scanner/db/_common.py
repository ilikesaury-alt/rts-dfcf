"""database 内部共享 helper（P1-6 拆分）。

queries / dal 都要用交易日回溯（_n_trading_days_ago），放这里避免两个模块
互相 import 形成隐式耦合。下划线前缀 = 包内私有，外部不要直接 import。
"""
import logging
from datetime import date, timedelta

from scanner.config import now_beijing
from scanner.trading_session import is_trading_day

logger = logging.getLogger(__name__)


def _n_trading_days_ago(n: int, as_of: str | None = None) -> str:
    """as_of（含）之前第 n 个交易日；as_of 为 None 时锚定真实今日。

    as_of 用于历史回放（historical_rescan）：把「今天」挪到某个过去的交易日，
    使 is_new / 回溯窗口的判定与那一天的实时扫描完全一致。
    """
    cursor = date.fromisoformat(as_of) if as_of else now_beijing().date()
    trading_days = 0
    # 上限保护：避免节假日数据缺失/损坏时 is_trading_day 永远为 False 导致死循环
    max_iter = n * 3 + 30
    iters = 0
    while trading_days < n:
        cursor -= timedelta(days=1)
        iters += 1
        if iters > max_iter:
            logger.warning("_n_trading_days_ago(%d): max_iter=%d 触发, "
                           "回溯仅到达 %s (期望 ~%d 个交易日前), "
                           "节假日数据可能缺失", n, max_iter, cursor, n)
            break
        if is_trading_day(cursor):
            trading_days += 1
    return cursor.isoformat()
