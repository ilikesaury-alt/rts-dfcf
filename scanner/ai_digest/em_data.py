"""东财公开数据源的只读抓取（2026-11-03）—— 公告 / 分时资金流 / 龙虎榜。

这三个是「消息面 + 日内资金节奏 + 游资痕迹」三类硬数据，AI 调研缺了它们就只能看图说话：

| 数据 | 接口 | 为什么它有用 |
|---|---|---|
| **公告** | `np-anotice-stock.eastmoney.com/api/security/ann` | 公司层面的真实事件（业绩预告、重组、增减持）—— 唯一能回答「有没有实质消息」的数据源 |
| **分时资金流** | `push2his.eastmoney.com/.../fflow/kline/get`（`klt=101`） | 当日每分钟的累计净流入（主力/超大单/大单/中单/小单）—— 能看出「早盘冲高回落」「尾盘拉升」这类日内节奏 |
| **龙虎榜** | `datacenter-web.eastmoney.com/api/data/v1/get`（`RPT_DAILYBILLBOARD_DETAILSNEW`） | 上榜原因 + 买卖净额 + **D1/D2/D5/D10 后续涨幅** —— 游资参与度与「上榜后到底涨没涨」的硬证据 |

⚠ 三个接口都是**公开**的（无需 token），但**会变**：字段名/参数一改就静默返回空。
故本模块每个函数都返回**结构化 dict**（空就是空），由 `research.py` 的工具层负责说人话。

⚠ 分时资金流的 `klt` 必须是 **101**（当日分钟曲线）：`klt=1`（分时）返回 0 点 —— 实测。
⚠ 龙虎榜的 `TRADE_DATE` 是 `'2020-07-01 00:00:00'` 这种带时间戳的字符串，取日期要 split。
"""

from __future__ import annotations

from typing import Any

from scanner.utils import EXTERNAL_FAILURES

__all__ = ["fetch_announcements", "fetch_dragon_tiger", "fetch_intraday_fund_flow"]

_TIMEOUT = 15.0


def _requests():
    try:
        import requests
    except ImportError:  # pragma: no cover - 环境相关
        return None
    return requests


def fetch_announcements(symbol: str, limit: int = 8) -> list[dict[str, Any]]:
    """最近公告（标题 + 日期 + 类型）。东财按裸 6 位代码查（不带前缀）。"""
    code = symbol[2:] if len(symbol) == 8 and symbol[:2] in ("SH", "SZ", "BJ") else symbol
    req = _requests()
    if req is None:
        return []
    try:
        resp = req.get(
            "https://np-anotice-stock.eastmoney.com/api/security/ann",
            params={
                "page_size": max(1, min(limit, 30)),
                "page_index": 1,
                "ann_type": "A",
                "client_source": "web",
                "stock_list": code,
            },
            timeout=_TIMEOUT,
        )
        payload = resp.json()
    except EXTERNAL_FAILURES:
        return []
    except ValueError:  # 非 JSON（被网关拦了）
        return []
    items = (payload.get("data") or {}).get("list") or []
    out = []
    for it in items:
        cols = it.get("columns")
        # ⚠ `columns` 实测是**字符串列表**（如 ["公司公告"]），不是 dict 列表 ——
        #   当 dict 列表写会 AttributeError（2026-11-03 实测）。
        if isinstance(cols, list) and cols:
            first = cols[0]
            col_type = first.get("column_name", "") if isinstance(first, dict) else str(first)
        else:
            col_type = ""
        out.append(
            {
                "date": str(it.get("notice_date") or "")[:10],
                "title": str(it.get("title") or "").strip(),
                "type": str(col_type),
            }
        )
    return out


def fetch_intraday_fund_flow(symbol: str) -> list[dict[str, Any]]:
    """**日频**资金流序列（东财 fflow kline，`klt=101`）：近 121 个交易日的资金流。

    每点字段：`日期, 主力净流入, 小单, 中单, 大单, 超大单`（单位：元）。

    ⚠⚠ `klt=101` 是**日频**（实测：121 个点、日期跨 2026-05-07 ~ 11-02），**不是分时**。
    早先把它当「当日分时曲线」是错的 —— 分时资金流（`klt=1`）本环境取不到（0 点）。
    本函数因此定位为「多日资金流趋势」：主力在这 121 天里是净流入还是净流出、最近几天如何。
    """
    code = symbol[2:] if len(symbol) == 8 and symbol[:2] in ("SH", "SZ", "BJ") else symbol
    market = "1" if code.startswith(("6", "9")) else "0"  # 1=沪 0=深/北
    req = _requests()
    if req is None:
        return []
    try:
        resp = req.get(
            "https://push2his.eastmoney.com/api/qt/stock/fflow/kline/get",
            params={
                "secid": f"{market}.{code}",
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
                "klt": "101",
                "lmt": "0",
            },
            timeout=_TIMEOUT,
        )
        payload = resp.json()
    except EXTERNAL_FAILURES:
        return []
    except ValueError:
        return []
    klines = (payload.get("data") or {}).get("klines") or []
    out: list[dict[str, Any]] = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < 6:
            continue
        try:
            out.append(
                {
                    "date": parts[0],
                    "main": float(parts[1]),
                    "small": float(parts[2]),
                    "medium": float(parts[3]),
                    "large": float(parts[4]),
                    "xl": float(parts[5]),
                }
            )
        except ValueError:
            continue
    return out


def fetch_dragon_tiger(symbol: str, limit: int = 5) -> list[dict[str, Any]]:
    """龙虎榜记录：上榜日期、原因、买卖净额、成交占比、**D1/D2/D5/D10 后续涨幅**。

    ⚠ D* 字段是「上榜后第 N 个交易日收盘涨跌幅」—— 这是**该游资/该榜单在历史上到底
    涨没涨**的硬证据，比任何形态判断都实。
    """
    code = symbol[2:] if len(symbol) == 8 and symbol[:2] in ("SH", "SZ", "BJ") else symbol
    req = _requests()
    if req is None:
        return []
    try:
        resp = req.get(
            "https://datacenter-web.eastmoney.com/api/data/v1/get",
            params={
                "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
                "columns": "ALL",
                "filter": f'(SECURITY_CODE="{code}")',
                "pageNumber": 1,
                "pageSize": max(1, min(limit, 20)),
                "sortColumns": "TRADE_DATE",
                "sortTypes": -1,
                "source": "WEB",
                "client": "WEB",
            },
            timeout=_TIMEOUT,
        )
        payload = resp.json()
    except EXTERNAL_FAILURES:
        return []
    except ValueError:
        return []
    rows = (payload.get("result") or {}).get("data") or []
    out = []
    for r in rows:
        out.append(
            {
                "date": str(r.get("TRADE_DATE") or "")[:10],
                "reason": str(r.get("EXPLAIN") or "").strip(),
                "net": r.get("BILLBOARD_NET_AMT"),
                "amount": r.get("ACCUM_AMOUNT"),
                "deal_ratio": r.get("DEAL_AMOUNT_RATIO"),
                "turnover": r.get("TURNOVERRATE"),
                "close": r.get("CLOSE_PRICE"),
                "change": r.get("CHANGE_RATE"),
                "d1": r.get("D1_CLOSE_ADJCHRATE"),
                "d2": r.get("D2_CLOSE_ADJCHRATE"),
                "d5": r.get("D5_CLOSE_ADJCHRATE"),
                "d10": r.get("D10_CLOSE_ADJCHRATE"),
            }
        )
    return out
