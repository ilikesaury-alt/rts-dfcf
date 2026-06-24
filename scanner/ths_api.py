import logging
import time

import requests

from scanner.config import REQUEST_TIMEOUT, THS_HEADERS

logger = logging.getLogger(__name__)

MARKET_MAP = {33: "SZ", 17: "SH"}


def make_ths_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update(THS_HEADERS)
    s.get("https://www.10jqka.com.cn/", timeout=REQUEST_TIMEOUT)
    return s


def fetch_ths_hot_list(session: requests.Session, size: int = 100,
                       max_retries: int = 3) -> list[dict]:
    url = (
        "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock"
        "?stock_type=a&type=hour&list_type=skyrocket"
    )
    last_exc = None
    for attempt in range(max_retries):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status_code") != 0:
                logger.warning("同花顺热榜返回异常: %s", data.get("status_msg"))
                return []
            raw_items = data.get("data", {}).get("stock_list", [])
            return [ths_normalize(item) for item in raw_items[:size]]
        except Exception as e:
            last_exc = e
            if attempt < max_retries - 1:
                delay = 1.0 * (2 ** attempt)
                logger.warning("同花顺热榜请求失败(第%d次), %.0f秒后重试: %s",
                               attempt + 1, delay, e)
                time.sleep(delay)
    logger.error("同花顺热榜获取失败(已重试%d次): %s", max_retries, last_exc)
    return []


def ths_normalize(item: dict) -> dict:
    code = item.get("code", "")
    if not code:
        logger.warning("同花顺数据缺少code字段: %s", item)
        return {}
    market = item.get("market", 33)
    prefix = MARKET_MAP.get(market, "SZ")
    symbol = f"{prefix}{code}"
    tag = item.get("tag") or {}
    return {
        "symbol": symbol,
        "name": item.get("name", ""),
        "code": code,
        "percent": item.get("rise_and_fall", 0),
        "current": 0.0,
        "value": 0,  # no trading amount equivalent
        "rank_change": item.get("hot_rank_chg", 0),
        "rank": item.get("order", 0),
        "concept_tags": tag.get("concept_tag", []),
        "popularity_tag": tag.get("popularity_tag", ""),
    }
