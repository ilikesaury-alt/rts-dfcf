"""市场级状态采集（P1 基础设施，2026-09-03）。

为 L0 市场闸门提供**真实市场级特征**，替代当前依赖内生「池子宽度」的近似
（池宽是与本系统产出相关的内生变量，非独立市场信号）。

数据源优先级（遵循项目惯例：雪球 → 同花顺 → 东财 → 其他）：
- 三大指数涨跌幅（创业板指 / 上证 / 深证）：
  1. 雪球 batch quote（api.fetch_market_caps_batch，主源，本机验证可用）
  2. 东财 akshare spot（stock_zh_index_spot_em，可选依赖，未装则优雅降级 None）
  3. 腾讯财经实时行情（qt.gtimg.cn，「其他」兜底，本机验证可达）
- 涨停家数 / 炸板数 / 连板高度：
  1. 同花顺官方涨停池（ths_api.fetch_limit_up_pool，主源，本机验证可用）
  2. 东财 push2ex 涨停池（本机不可达，失败降级 None）

设计原则（与项目一致）：
- fail-open：单源失败不影响其他源 / 其他字段；全部失败则只写 date/fetched_at。
- 仅落库 + 提供读取接口，不改动评分 / 排序 / 展示逻辑。
- 独立 collect() 由主循环（传入 session 复用）或 CLI 调用。
  CLI：`python -m scanner.market_state --date 2026-09-03`
"""
import argparse
import sqlite3
import ssl
import sys
import urllib.request

from scanner.config import now_beijing
from scanner.utils import EXTERNAL_FAILURES

# 指数代码映射
_XQ_INDICES = {"cyb": "SZ399006", "sh": "SH000001", "sz": "SZ399001"}  # 雪球 / 腾讯格式
_AK_INDICES = {"cyb": "399006", "sh": "000001", "sz": "399001"}        # akshare 格式
_TENCENT_CODES = {"sh": "sh000001", "sz": "sz399001", "cyb": "sz399006"}

# 腾讯兜底用独立 opener：避免继承任何全局 opener 副作用（本项目 push2 系列直连本机不可达，
# 腾讯走系统代理可达）；ssl 忽略证书（与 scanner/api.py 同处理）。
ssl._create_default_https_context = ssl._create_unverified_context
_opener = urllib.request.build_opener()


def init_market_state(conn: sqlite3.Connection) -> None:
    """建 market_state 表（幂等）。"""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS market_state (
            date TEXT PRIMARY KEY,
            cyb_close REAL, cyb_pct REAL,
            sh_close REAL, sh_pct REAL,
            sz_close REAL, sz_pct REAL,
            limit_up INTEGER, limit_break INTEGER, limit_up_prev INTEGER,
            fetched_at TEXT
        )"""
    )


# ───────────────────────────────────────────────────────────────────────────
# 指数涨跌幅：雪球 → 东财(akshare) → 腾讯
# ───────────────────────────────────────────────────────────────────────────
def _fetch_indices_xueqiu(session=None) -> dict | None:
    """雪球层（主源）：batch quote 拿三大指数涨跌幅。失败返回 None。"""
    try:
        import scanner.api as api  # lazy import，避免顶层环依赖

        s = session or api.make_session()
        res = api.fetch_market_caps_batch(s, list(_XQ_INDICES.values()))
        if not res:
            return None
        out: dict[str, tuple[float, float]] = {}
        for key, xq in _XQ_INDICES.items():
            d = res.get(xq)
            if d and d.get("percent") is not None:
                out[key] = (d.get("current"), float(d["percent"]))
        return out or None
    except EXTERNAL_FAILURES:
        return None


def _fetch_indices_eastmoney() -> dict | None:
    """东财层（可选）：akshare 东财指数 spot。未安装 / 失败优雅降级 None。"""
    try:
        import akshare as ak  # lazy import，可选依赖

        df = ak.stock_zh_index_spot_em()
        if df is None or df.empty:
            return None
        out: dict[str, tuple[float, float]] = {}
        for key, code in _AK_INDICES.items():
            row = df[df["代码"] == code]
            if not row.empty:
                out[key] = (float(row.iloc[0]["最新价"]), float(row.iloc[0]["涨跌幅"]))
        return out or None
    except Exception:  # noqa: BLE001  未装 akshare / 网络失败 → 降级
        return None


def _http_text(url: str, encoding: str = "utf-8") -> str:
    """限时重试 GET，返回解码文本；失败抛 EXTERNAL_FAILURES 子类。"""
    import time

    last_err: Exception | None = None
    for _ in range(3):
        try:
            req = urllib.request.Request(  # noqa: S310 - URL 为模块内固定端点，非外部输入
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Referer": "https://stockapp.finance.qq.com/",
                },
            )
            with _opener.open(req, timeout=15) as resp:  # noqa: S310 - URL 为模块内固定端点，非外部输入
                return resp.read().decode(encoding, errors="ignore")
        except EXTERNAL_FAILURES as e:
            last_err = e
            time.sleep(0.5)
    if last_err is not None:
        raise last_err
    raise RuntimeError("market_state._http_text: no attempt made")


import re  # noqa: E402

_TENCENT_RE = re.compile(r'v_([\w]+)="([^"]*)"')


def _fetch_indices_tencent() -> dict | None:
    """其他兜底：腾讯财经实时行情（本机走代理可达）。盘中为实时、盘后为收盘涨跌幅。"""
    try:
        out: dict[str, tuple[float, float]] = {}
        for key, code in _TENCENT_CODES.items():
            text = _http_text(f"https://qt.gtimg.cn/q={code}", encoding="gbk")
            m = _TENCENT_RE.search(text)
            if not m:
                continue
            parts = m.group(2).split("~")
            if len(parts) < 5:
                continue
            cur = float(parts[3])
            prev = float(parts[4])
            if prev == 0:
                continue
            out[key] = (cur, (cur - prev) / prev * 100.0)
        return out or None
    except (EXTERNAL_FAILURES, ValueError):
        return None


def _fetch_indices(session=None) -> dict | None:
    """指数涨跌幅按优先级取数：雪球 → 东财 → 腾讯。返回 {key:(close,pct)}。"""
    for fn in (_fetch_indices_xueqiu, _fetch_indices_eastmoney, _fetch_indices_tencent):
        try:
            v = fn(session) if fn is _fetch_indices_xueqiu else fn()
            if v:
                return v
        except EXTERNAL_FAILURES:
            continue
    return None


# ───────────────────────────────────────────────────────────────────────────
# 涨停家数 / 炸板数 / 连板高度：同花顺 → 东财(push2ex)
# ───────────────────────────────────────────────────────────────────────────
def _fetch_limit_ths() -> tuple[int, int, int] | None:
    """同花顺层（主源）：官方涨停池。返回 (涨停家数, 炸板数, 连板高度)。失败 None。"""
    try:
        import scanner.ths_api as ths  # lazy import，避免顶层环依赖

        res = ths.fetch_limit_up_pool()
        if not res:
            return None
        zt = len(res)
        zbc = sum(1 for it in res.values() if it.get("zhaban"))
        max_lian = max((it.get("lianban") or 0) for it in res.values())
        return (zt, zbc, max_lian)
    except EXTERNAL_FAILURES:
        return None


def _fetch_limit_eastmoney() -> tuple[int, int, int] | None:
    """东财层（兜底）：push2ex 涨停池。本机不可达 → 失败返回 None。"""
    try:
        import time

        url = (
            "https://push2ex.eastmoney.com/getTopicZTPool?"
            "ut=7eea3edcaed734bea9cbfc24409ed989"
            f"&d={time.strftime('%Y-%m-%d')}&Pageindex=0&pagesize=200&sort=fbt:asc"
        )
        d = _http_text(url)
        import json

        data = json.loads(d).get("data") or {}
        if not data:
            return None
        return (data.get("ztc") or 0, data.get("zbc") or 0, data.get("lbc") or 0)
    except (EXTERNAL_FAILURES, ValueError):
        return None


def _fetch_limit_stats() -> tuple[int, int, int] | None:
    """涨停统计按优先级取数：同花顺 → 东财。失败返回 None。"""
    for fn in (_fetch_limit_ths, _fetch_limit_eastmoney):
        try:
            v = fn()
            if v:
                return v
        except EXTERNAL_FAILURES:
            continue
    return None


# ───────────────────────────────────────────────────────────────────────────
_COLS = [
    "date", "cyb_close", "cyb_pct", "sh_close", "sh_pct",
    "sz_close", "sz_pct", "limit_up", "limit_break", "limit_up_prev", "fetched_at",
]


def collect_market_state(conn: sqlite3.Connection, d: str | None = None, session=None) -> dict:
    """采集 d 日市场状态并 upsert。返回实际采集到的字段字典（供日志）。

    fail-open：单源失败不影响其余字段；全部失败则只写 date/fetched_at。
    session：可选雪球 session，主循环传入以复用，避免重复建连。
    """
    d = d or now_beijing().strftime("%Y-%m-%d")
    row: dict = {"date": d, "fetched_at": now_beijing().isoformat(timespec="seconds")}
    idx = _fetch_indices(session)
    if idx:
        for key, (close, pct) in idx.items():
            row[f"{key}_close"], row[f"{key}_pct"] = close, pct
    lim = _fetch_limit_stats()
    if lim:
        # limit_up_prev 列实际存「连板高度」（max continue_day_cnt），语义见建表注释
        row["limit_up"], row["limit_break"], row["limit_up_prev"] = lim
    vals = [row.get(c) for c in _COLS]
    placeholders = ",".join("?" * len(_COLS))
    conn.execute(
        f"INSERT OR REPLACE INTO market_state ({','.join(_COLS)}) VALUES ({placeholders})", vals  # noqa: S608 - _COLS 为模块受信常量白名单，值经 ? 参数化
    )
    conn.commit()
    return {k: v for k, v in row.items() if v is not None}


def get_market_state(conn: sqlite3.Connection, d: str) -> dict | None:
    """读取 d 日市场状态；无则返回 None。"""
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM market_state WHERE date=?", (d,)).fetchone()
    return dict(r) if r else None


def get_range(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    """读取 [start, end] 区间内市场状态（按 date 升序）。"""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM market_state WHERE date>=? AND date<=? ORDER BY date", (start, end)
    ).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    sys.path.insert(0, ".")
    ap = argparse.ArgumentParser(description="采集市场级状态到 market_state 表")
    ap.add_argument("--date", default=None, help="日期 YYYY-MM-DD，默认今天")
    args = ap.parse_args()
    c = sqlite3.connect("scanner.db")
    init_market_state(c)
    out = collect_market_state(c, args.date)
    print("collected:", out)
