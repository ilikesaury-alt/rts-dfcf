"""行情增强数据源（涨停池 + 个股资金流/报价快照）。

数据源：
- 涨停池   主源同花顺官方 API（ths_api.py，2026-08-23 接入）；AKShare
           `stock_zt_pool_em(date)` 降为兜底。全市场 1 次请求/轮
- 个股资金流 自实现直连东财 ulist.np/get（push2delay.eastmoney.com）显式 secid
           批量拉取（2026-10-15 从 clist 全市场分页迁移：clist 被东财 WAF 按**路径**
           封锁——全部 host/http+https/直连+代理/GET+POST 均 TLS 握手后被断连，
           而同 host 的 ulist 接口全字段可用）。取数域 = 创业板全集
           （300000-302999）∪ register_fund_flow_codes 注册的榜内主板票，
           500 码/批、6 批并行 ~1.2s。host 可用 RTS_FUND_FLOW_HOST 覆盖。
           ⚠ 此处原记「数据可能延迟约15分钟」，2026-09-18 实测证伪：响应内 f124
           更新戳距本地时间仅 1~5s，且与雪球实时行情逐票一致（push2delay 只是域名，
           返回的是实时快照）。故本表可当**准实时快照**使用。
           同一响应行除资金流外还带报价字段（价/涨幅/量比/换手/量额/市值），
           见 _absorb —— 这是榜外票（不在 daily_kline 内）唯一的价量来源。
           （取数域收敛说明：原 clist 全市场 5305 只里非创业板、非榜单的 ~3000 只
           主板票**从无消费方**——B 段只筛创业板、主线候选全创业板、榜单票显式
           注册——那是「全市场分页」实现方式的副产品，不是需求。）

与 concept.py 相同的可靠性模式：
- lazy import akshare（仅涨停池兜底依赖；未安装时自动禁用，返回空）
- 进程内 TTL 缓存（按交易日键、线程安全、带上限淘汰）→ DB 缓存（当日 +
  盘中新鲜度）→ 拉取落库
- 拉取带限时/分页 deadline，失败缓存空结果短退避，绝不阻塞扫描循环
- 符号格式转换复用 data_source 的 ak_to_xq（300001 → SZ300001）
"""

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests as _requests

from scanner.config import (
    ENABLE_FUND_FLOW,
    ENABLE_ZT_POOL,
    FUND_FLOW_FETCH_TIMEOUT,
    FUND_FLOW_HOST,
    FUND_FLOW_PARTIAL_TTL_SEC,
    FUND_FLOW_TTL_SEC,
    ZT_POOL_FETCH_TIMEOUT,
    ZT_POOL_TTL_SEC,
    now_beijing,
)
from scanner.data_source import ak_to_xq
from scanner.database import get_market_extra_cache, save_market_extra_cache
from scanner.net import EASTMONEY_HEADERS, EASTMONEY_UT_TOKEN, bounded_call
from scanner.utils import EXTERNAL_FAILURES, _strip_exchange, to_float
from scanner.utils import cache_put as _cache_put

logger = logging.getLogger(__name__)

_ZT_POOL = "zt_pool"
_FUND_FLOW = "fund_flow"

# 东财 ulist.np/get API：显式 secid 批量，与 clist 同一字段码体系（响应行同构）。
# 字段码：f12=代码, f62=主力净流入净额, f184=主力净流入净占比, f66=超大单净流入净额
# 末段 f5/f6/f8/f10/f18/f20/f21 是**报价快照**：与资金流同一响应行返回，扩字段
# 不增加请求数。这是榜外票（不在 daily_kline）唯一的价量来源。
# f11(5分钟涨跌)/f22(涨速) 实测恒为 0，该接口不提供，故不请求。
_FUND_FLOW_FIELDS = "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f204,f205,f124,f5,f6,f8,f10,f18,f20,f21"
# 创业板全集（300000-302999）：is_gem = startswith("30") 的完整代码空间，新股自动
# 覆盖。ulist 对未发行/退市码静默跳过（实测 303001 不回行），空洞零成本。
_GEM_CODE_MIN, _GEM_CODE_MAX = 300000, 302999
# ulist 单批 secid 数：实测 500 稳定、1000 → 502 Bad Gateway（2026-10-15 探针，
# 疑似网关 URL 长度上限）；600/800 也通过，取 500 留余量。
_FUND_FLOW_BATCH_SIZE = 500

_ak = None
_AK_FAILED = object()  # akshare 导入失败的哨兵（与 None 区分：None = 未尝试）
_ak_lock = threading.Lock()


def _get_ak():
    """lazy import akshare；不可用返回 None（上层按空数据降级）。

    顺带禁用 tqdm 进度条（akshare 部分接口向 stderr 打印进度，
    在长跑模式下污染日志）。
    """
    global _ak
    # 快路径必须同时排除失败哨兵（2026-09-29 审查修复）：akshare 未安装时
    # _ak=_AK_FAILED，此前的 `if _ak is not None` 会把哨兵返回给上层，fetch_zt_pool
    # 只判 None → 对哨兵调 stock_zt_pool_em 抛 AttributeError 穿透 fail-soft。
    if _ak is not None and _ak is not _AK_FAILED:
        return _ak
    with _ak_lock:
        if _ak is None:
            try:
                os.environ.setdefault("TQDM_DISABLE", "1")
                import akshare  # noqa: PLC0415

                _ak = akshare
            except Exception as e:  # ImportError 等
                logger.warning("AKShare 不可用，涨停池数据禁用: %s", e)
                _ak = _AK_FAILED
    return _ak if _ak is not _AK_FAILED else None


_zt_cache: dict[str, tuple[dict, float]] = {}
_ff_cache: dict[str, tuple[dict, float]] = {}
_extra_lock = threading.Lock()
# 当日注册的非创业板拉取码（榜内主板票，A 段量比/榜内异动段读取依赖）。按日清零：
# 昨日榜单票今日掉榜后不再浪费请求位（快照里留有其最后值，读侧无 TTL 依赖）。
_ff_extra_codes: set[str] = set()
_ff_extra_codes_date = ""
# 最近一次资金流拉取是否超时（部分结果）。完整拉取时把全集快照全部
# 落库，保证重启/掉榜后任一 symbol 的资金流数据仍可读；部分结果只存候选，
# 避免把不完整数据当快照冻结。
_last_ff_partial: bool = False


def register_fund_flow_codes(codes) -> None:
    """注册额外的资金流拉取码（6 位代码或 xq 符号；榜内主板票等非创业板消费对象）。

    注册码并入下一次 fetch_fund_flow_rank 的取数域（创业板全集 ∪ 注册码）。
    创业板码注册为幂等（本就在全集内）。按交易日键清零：新一天的第一次注册
    丢弃昨日码集。入参自动归一化为 6 位纯代码（SZ300001 → 300001），
    无法归一化的条目丢弃。
    """
    norm = set()
    for s in codes or ():
        code = _strip_exchange(str(s or "").strip())
        if len(code) == 6 and code.isdigit():
            norm.add(code)
    if not norm:
        return
    global _ff_extra_codes_date
    with _extra_lock:
        today = _today_key()
        if _ff_extra_codes_date != today:
            _ff_extra_codes.clear()
            _ff_extra_codes_date = today
        _ff_extra_codes.update(norm)


def _registered_ff_codes() -> set[str]:
    """当日注册码快照（线程安全拷贝；跨日未清零时视为空）。"""
    with _extra_lock:
        return set(_ff_extra_codes) if _ff_extra_codes_date == _today_key() else set()


def _today_key() -> str:
    return now_beijing().date().strftime("%Y%m%d")


def _cache_hit(cache: dict, ttl: float, key: str) -> dict | None:
    with _extra_lock:
        entry = cache.get(key)
        if entry:
            # 第三条记录写入时的自定义 TTL（None 用调用方默认 ttl）
            eff_ttl = entry[2] if len(entry) > 2 and entry[2] is not None else ttl
            if time.time() - entry[1] < eff_ttl:
                return entry[0]
        return None


def _cache_put_all(cache: dict, data: dict, key: str, now: float | None = None, ttl: float | None = None):
    with _extra_lock:
        _cache_put(cache, key, (data, now if now is not None else time.time(), ttl))


def _fetch_zt_pool_ths(date_key: str) -> dict[str, dict] | None:
    """THS 官方涨停池（2026-08-23 主源）；未配置 Key / 接口失败返回 None → AKShare 兜底。"""
    from scanner import ths_api  # 惰性导入避免无 Key 场景的模块级开销

    if not ths_api.get_api_key():
        return None
    date_ms = ths_api.date_str_to_ms(date_key)
    return ths_api.fetch_limit_up_pool(date_ms=date_ms, include_break=True)


def fetch_zt_pool(today: str | None = None) -> dict[str, dict]:
    """拉取今日涨停池，返回 {6位代码: {lianban, zt_stat, fengban_amt, zhaban, industry}}。

    主源：同花顺官方 API（字段更富：封单额/涨停原因/开板次数；免 AKShare 的
    bounded_call 兜底）。THS 未配置 Key / 接口失败 → AKShare 兜底（原路径）。
    非交易日接口返回空表 → 空 dict。失败打印告警、缓存空结果短退避并返回 {}（软降级）。
    """
    key = today or _today_key()
    cached = _cache_hit(_zt_cache, ZT_POOL_TTL_SEC, key)
    if cached is not None:
        return cached
    ths_result = None
    try:
        ths_result = _fetch_zt_pool_ths(key)
    except Exception as e:  # noqa: BLE001  THS 层异常同样降级 AKShare
        logger.warning("THS 涨停池异常，降级 AKShare: %s", e)
    if ths_result is not None:
        _cache_put_all(_zt_cache, ths_result, key)
        return ths_result
    ak = _get_ak()
    if ak is None:
        return {}
    try:
        df = bounded_call(lambda: ak.stock_zt_pool_em(date=key), ZT_POOL_FETCH_TIMEOUT)
        result: dict[str, dict] = {}
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                code = str(row["代码"]).strip()
                if not code:
                    continue
                result[code] = {
                    "lianban": int(_num(row, "连板数") or 0),
                    "zt_stat": str(row.get("涨停统计") or ""),
                    "fengban_amt": float(_num(row, "封板资金") or 0),
                    "zhaban": int(_num(row, "炸板次数") or 0),
                    "industry": str(row.get("所属行业") or ""),
                }
        _cache_put_all(_zt_cache, result, key)
        return result
    except EXTERNAL_FAILURES as e:
        print(f"  [!] 涨停池获取失败: {e}")
        # 短退避：失败空结果只冻结 FUND_FLOW_PARTIAL_TTL_SEC(60s) 而非默认 300s——
        # 默认 TTL 会让失败在 5 分钟内零重试（2026-08-20 修复，此前注释声称"短退避"实际 300s）。
        _cache_put_all(_zt_cache, {}, key, ttl=FUND_FLOW_PARTIAL_TTL_SEC)
        return {}


def _gem_universe_codes() -> list[str]:
    """创业板全集 6 位码（is_gem 全空间，含未来新股；无效码 ulist 静默跳过）。"""
    return [str(c) for c in range(_GEM_CODE_MIN, _GEM_CODE_MAX + 1)]


def _secid(code: str) -> str:
    """6 位代码 → 东财 secid（沪市 6 开头前缀 1.，深市 00/30 前缀 0.）。

    本模块消费域只有创业板 + 沪深主板榜单票；北交所码（4/8/92 开头）不在域内，
    若被注册会得到错误前缀而被接口静默忽略（fail-open）。
    """
    return ("1." if code.startswith("6") else "0.") + code


def _collect_fund_flow(box: dict, deadline: float, codes: list[str] | None = None) -> dict:
    """按显式码批量拉取个股今日资金流（ulist.np/get），结果实时写入 box["value"]。

    codes 缺省 = 创业板全集（B 段榜外发现 + 主线候选的全量需求面；榜内主板票
    经 register_fund_flow_codes 并入）。切批 _FUND_FLOW_BATCH_SIZE=500，6 线程
    并行（全集 3000 码 → 6 批，实测 ~1.2s）。每批 timeout=10，批间检查 deadline，
    超时取消未开始任务并返回已收集部分。
    首批**同步**执行：主机不可达时快速失败，不对余下批次打请求（clist 时代首页
    同步的同款语义）；首批成功（即使 0 行，如非交易时段）才提交其余批次。
    返回 {6位代码: {main_net, main_pct, super_net, price, percent, volume, amount,
    turnover, vol_ratio, prev_close, total_cap, float_cap, name}} —— 前三个是资金流，
    其余是**同一响应行的报价快照**（不额外请求）。后者使榜外票获得当日价量，
    配合 PK(symbol,data_type,date) 的"当日最后一次写入即收盘值"语义，
    日积即成全集日频价量面板（daily_kline 只覆盖榜内 1022 只）。
    完成全部批次且无批级失败时置 box["done"]=True；任一批失败不置 done
    （外层按部分/失败路径短退避重试，不把缺批数据当完整快照缓存 300s）。
    """
    result: dict[str, dict] = {}
    if codes is None:
        codes = _gem_universe_codes()
    if not codes or time.time() > deadline:
        box["value"] = result
        return result
    url = f"https://{FUND_FLOW_HOST}/api/qt/ulist.np/get"
    batches = [codes[i : i + _FUND_FLOW_BATCH_SIZE] for i in range(0, len(codes), _FUND_FLOW_BATCH_SIZE)]

    # 批级失败原因（最多留 3 条）。_batch 吞掉异常后上层只看得到"0 只/部分"，
    # 无法区分网络不可达 / 代理拦截 / 接口无 data / 纯超时截断 —— 收集后随 box["err"] 传出。
    errs: list[str] = []
    err_lock = threading.Lock()

    def _note_err(msg: str) -> None:
        with err_lock:
            if len(errs) < 3:
                errs.append(msg)

    def _batch_once(idx: int, secids: list[str]):
        """单次请求：成功返回行列表；失败返回 (None, 原因)（不记 errs）。"""
        p = {
            "fltt": "2",
            "invt": "2",
            "np": "1",
            "ut": EASTMONEY_UT_TOKEN,
            "secids": ",".join(_secid(c) for c in secids),
            "fields": _FUND_FLOW_FIELDS,
        }
        try:
            resp = _requests.get(url, params=p, timeout=10, headers=EASTMONEY_HEADERS)
            j = resp.json()
            data = j.get("data") if isinstance(j, dict) else None
            if not isinstance(data, dict):
                return None, (f"批{idx} 响应无 data" if isinstance(j, dict) else f"批{idx} 响应非 JSON 对象")
            diff = data.get("diff")
            if isinstance(diff, dict):  # 防御：个别网关把 diff 返回成 {序号: 行}
                diff = list(diff.values())
            return diff or [], ""
        except EXTERNAL_FAILURES as e:
            return None, f"批{idx} {type(e).__name__}: {e}"

    def _batch(idx: int, secids: list[str]):
        # 单次重试（0.5s 退避）：push2 偶发瞬时断连（实测同参数连打 4 次三败一成），
        # 重试一次可吸收抖动；重试成功不记 errs（否则瞬时抖动会阻止 done 置位、
        # 每轮全量重拉）；仍失败才计入 errs。仅在 deadline 内重试。
        rows, err = _batch_once(idx, secids)
        if rows is None and time.time() + 0.5 < deadline:
            time.sleep(0.5)
            rows, err = _batch_once(idx, secids)
        if rows is None:
            _note_err(err)
        return rows

    def _absorb(diff):
        for row in diff:
            if not isinstance(row, dict):
                continue
            code = str(row.get("f12") or "").strip()
            if not code:
                continue
            result[code] = {
                "main_net": _num(row, "f62"),
                "main_pct": _num(row, "f184"),
                "super_net": _num(row, "f66"),
                # 报价快照（同一响应行，零额外请求）。用途：榜外票无 daily_kline，
                # 收盘后最后一次写入即当日收盘价/涨幅/量比/换手，可累积成
                # 全集日频价量面板，供日后验证"脱离榜单"的信号。
                "price": _num(row, "f2"),
                "percent": _num(row, "f3"),
                "volume": _num(row, "f5"),
                "amount": _num(row, "f6"),
                "turnover": _num(row, "f8"),
                "vol_ratio": _num(row, "f10"),
                "prev_close": _num(row, "f18"),
                "total_cap": _num(row, "f20"),
                "float_cap": _num(row, "f21"),
                # 名称（f14）本就在请求字段里、此前被丢弃。榜外票**不在榜单**，
                # 拿不到榜单侧的 name，这里不取就只能回落到 6 位代码（B 段展示用）。
                "name": str(row.get("f14") or "").strip(),
            }

    first = _batch(0, batches[0])
    if first is None:
        # 首批整体失败（_batch 吞异常/响应非 dict 时返回 None）不得当完整快照：
        # 空结果若被当完整快照按默认 300s TTL 缓存，全集资金流会静默缺失
        # （2026-09-29 审查修复的语义等价保留）。不置 done → 外层
        # fetch_fund_flow_rank 走 timed_out 失败路径（60s 短退避重试）。
        box["err"] = "；".join(errs) or "首批无数据"
        box["value"] = dict(result)
        return result
    _absorb(first)
    box["value"] = dict(result)  # 首批后即可读到部分结果

    completed_all = True
    if len(batches) > 1:
        # 显式 shutdown(wait=False, cancel_futures=True) 而非 with-exit 隐式 wait=True
        # （2026-08-24 审查，与 concept.py 同款）：deadline 到点后不等在跑的 ≤6 批
        # 排空（每批 timeout=10s），避免 daemon 线程多存活 ~10s、相邻两轮短暂重叠。
        pool = ThreadPoolExecutor(max_workers=6)
        try:
            futs = {pool.submit(_batch, idx, b): idx for idx, b in enumerate(batches[1:], start=1)}
            for fut in as_completed(futs):
                if time.time() > deadline:
                    for f in futs:
                        f.cancel()
                    completed_all = False
                    break
                _absorb(fut.result() or [])
                box["value"] = dict(result)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    if completed_all and not errs:
        box["done"] = True
    if errs:
        box["err"] = "；".join(errs)
    return result


def _fetch_fund_flow_bounded(timeout: float, codes: list[str] | None = None) -> tuple[dict, bool, str]:
    """daemon 线程内执行并行拉取，join(timeout) 到点即返回 (已收集部分, 是否超时, 失败原因)。

    不抛错：网络异常在 _batch 内被吞掉 → 空/部分结果；超时由调用方按部分结果处理。
    判定"部分"的依据不只是线程是否仍在运行（线程可能在 join 超时前恰好跑完
    内部 deadline），还须看 _collect_fund_flow 是否显式标记完成（box["done"]）。
    第三项是批级失败原因摘要（box["err"]），空串 = 没有批失败过（纯超时截断）。
    """
    box: dict = {"value": {}}

    def _run():
        try:
            _collect_fund_flow(box, time.time() + timeout, codes)
        except BaseException as e:  # noqa: BLE001
            box["error"] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if "error" in box:
        raise box["error"]
    timed_out = t.is_alive() or not box.get("done")
    return box.get("value", {}), timed_out, box.get("err", "")


def fetch_fund_flow_rank() -> dict[str, dict]:
    """拉取**创业板全集 ∪ 注册码**的个股今日资金流（东财 push2delay ulist），
    返回 {6位代码: {main_net, main_pct, super_net, ...报价快照}}。

    取数域（2026-10-15 从 clist 全市场分页迁移）：创业板全集（300000-302999，
    B 段榜外发现 + 主线候选的需求面）∪ register_fund_flow_codes 注册的榜内
    主板票（A 段量比/榜内异动段读取依赖）。clist 曾被东财 WAF 按路径封锁
    （2026-10-15 生产故障）；ulist 同 host 全字段可用，且拉取量从全市场 5300 只
    收敛到真实消费面 ~2300 只。

    进程缓存命中条件 = TTL 未过 **且** 注册码 ⊆ 上次拉取域：注册新码（榜单
    换血）会触发重拉补齐，保证 A 段主板票首轮即入快照。
    超时返回已收集部分（告警 + 只缓存 FUND_FLOW_PARTIAL_TTL_SEC，下一轮扫描
    重试补全缺批）；彻底失败打印告警、缓存空结果短退避并返回 {}（软降级）。
    """
    key = _today_key()
    registered = _registered_ff_codes()
    with _extra_lock:
        entry = _ff_cache.get(key)
    if entry:
        eff_ttl = entry[2] if len(entry) > 2 and entry[2] is not None else FUND_FLOW_TTL_SEC
        # 第 4 位 = 上次拉取域（注册码集）；缺位（旧条目）时用已取到的码集近似
        univ = entry[3] if len(entry) > 3 else set(entry[0])
        if time.time() - entry[1] < eff_ttl and registered <= univ:
            return entry[0]
    global _last_ff_partial
    codes = sorted(set(_gem_universe_codes()) | registered)
    try:
        result, timed_out, err = _fetch_fund_flow_bounded(FUND_FLOW_FETCH_TIMEOUT, codes)
        if timed_out:
            _last_ff_partial = True
            if result:
                print(f"  [!] 个股资金流拉取超时，已获取 {len(result)} 只（部分数据），下一轮扫描重试补全")
            else:
                # 0 只不是"部分数据"：首批请求失败（网络/代理/无 data）或线程连一批都没
                # 跑完。2026-10-12 前两者共用同一文案，把真故障读成"少几只、下轮补"。
                print(f"  [!] 个股资金流获取失败（0 只）{'：' + err if err else ''}，下一轮扫描重试")
            _cache_put(_ff_cache, key, (result, time.time(), FUND_FLOW_PARTIAL_TTL_SEC, set(codes)))
        else:
            _last_ff_partial = False
            # 批级失败但整体完成：此前 errs 随 done=True 静默丢弃，缺批数据被当完整
            # 快照缓存 300s 无从排查 —— 显式打出来（一次拉取至多一条）。
            if err:
                print(f"  [!] 个股资金流拉取完成但部分批次失败：{err}")
            _cache_put(_ff_cache, key, (result, time.time(), None, set(codes)))
        return result
    except EXTERNAL_FAILURES as e:
        _last_ff_partial = True
        print(f"  [!] 个股资金流获取失败: {e}")
        # 短退避：失败空结果只冻结 FUND_FLOW_PARTIAL_TTL_SEC(60s) 而非默认 300s
        # （2026-08-20 修复，此前注释声称"短退避"实际 300s 内零重试）。
        _cache_put(_ff_cache, key, ({}, time.time(), FUND_FLOW_PARTIAL_TTL_SEC, set(codes)))
        return {}


def _num(row, key) -> float:
    """单元格安全取值：None/NaN/±inf/不可解析字符串 → 0.0（统一走 utils.to_float）。"""
    try:
        v = row.get(key)
    except (AttributeError, TypeError):
        return 0.0
    if v is None:
        return 0.0
    return to_float(v) or 0.0


def _merge_from_db(
    conn, symbols: list[str], data_type: str, intraday_ttl_sec: int
) -> tuple[dict[str, dict], list[str]]:
    """DB 缓存命中补齐，返回 (result, missing)。

    intraday_ttl_sec：DB 条目超过该秒数视为过期（盘中刷新），过期条目与缺失
    一样落入 missing，交由拉取流程刷新 → 保证资金流/涨停池盘中持续更新而非
    冻结首次扫描快照。
    """
    if not symbols:
        return {}, []
    db = get_market_extra_cache(conn, symbols, data_type, intraday_ttl_sec)
    result: dict[str, dict] = {}
    missing: list[str] = []
    for sym in symbols:
        if sym in db and db[sym]:
            result[sym] = db[sym]
        else:
            missing.append(sym)
    return result, missing


def collect_market_extra(
    conn, symbols: list[str], include_zt: bool | None = None, include_flow: bool | None = None
) -> dict[str, dict]:
    """收集候选符号的行情增强数据。

    返回 {symbol: {"zt": {...}|None, "fund_flow": {...}|None}}。
    仅返回本次有数据（涨停池/资金流至少一类命中）的 symbol。
    流程：进程 TTL 缓存 → DB 缓存（当日 + 盘中新鲜度，过期重拉）→ 全集
    （创业板全集 ∪ 注册码）一次拉取并落库。所有异常均已 fail-soft，返回空 dict 不影响主扫描。
    """
    include_zt = ENABLE_ZT_POOL if include_zt is None else include_zt
    include_flow = ENABLE_FUND_FLOW if include_flow is None else include_flow
    if not symbols or (not include_zt and not include_flow):
        return {}
    uniq = list(dict.fromkeys(symbols))
    result: dict[str, dict] = {}

    if include_zt:
        db_map, missing_zt = _merge_from_db(conn, uniq, _ZT_POOL, ZT_POOL_TTL_SEC)
        for sym, payload in db_map.items():
            result.setdefault(sym, {})["zt"] = payload
        if missing_zt:
            fetched = fetch_zt_pool()
            if fetched:
                # 全市场涨停池映射回 xq 符号；未涨停票无数据，不写库（None 语义）
                mapped = {ak_to_xq(code): payload for code, payload in fetched.items()}
                save_map = {sym: mapped[sym] for sym in missing_zt if sym in mapped}
                if save_map:
                    save_market_extra_cache(conn, save_map, _ZT_POOL)
                for sym in missing_zt:
                    if sym in mapped:
                        result.setdefault(sym, {})["zt"] = mapped[sym]

    if include_flow:
        # 注册本轮票为拉取码（创业板本就在全集内，幂等；榜内主板票由此进入
        # 快照 —— A 段量比图与榜内异动段的读取依赖）。若进程缓存尚未覆盖新码
        # （榜单换血），fetch 内部会因「注册码 ⊄ 上次拉取域」重拉补齐。
        register_fund_flow_codes(_xq_code6(s) for s in uniq)
        db_flow, miss_flow = _merge_from_db(conn, uniq, _FUND_FLOW, FUND_FLOW_TTL_SEC)
        for sym, payload in db_flow.items():
            result.setdefault(sym, {})["fund_flow"] = payload
        if miss_flow:
            fetched = fetch_fund_flow_rank()
            if fetched:
                mapped = {ak_to_xq(code): payload for code, payload in fetched.items()}
                # _last_ff_partial（超时部分结果）时只存当前缺失候选，避免把不完整数据
                # 当快照冻结；否则是完整全集快照，全部落库——这样当日任一 symbol
                # （含已掉榜/重启前推荐过的票）都能在展示层读到资金流数据，
                # 而非只有当前候选有。
                save_map = {sym: mapped[sym] for sym in miss_flow if sym in mapped} if _last_ff_partial else mapped
                if save_map:
                    save_market_extra_cache(conn, save_map, _FUND_FLOW)
                for sym in miss_flow:
                    if sym in mapped:
                        result.setdefault(sym, {})["fund_flow"] = mapped[sym]

    return result


def _xq_code6(symbol: str) -> str:
    """xq 符号 → 6 位代码（SZ300001 → 300001）。语义同 utils._strip_exchange，不复制。"""
    return _strip_exchange(str(symbol or ""))


def reset_extra_cache():
    """重置进程内缓存（仅用于测试）。"""
    global _ak, _last_ff_partial, _ff_extra_codes_date
    with _extra_lock:
        _zt_cache.clear()
        _ff_cache.clear()
        _ff_extra_codes.clear()
        _ff_extra_codes_date = ""
    _ak = None
    _last_ff_partial = False
