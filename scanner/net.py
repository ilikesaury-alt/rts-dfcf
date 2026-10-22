"""共享网络层单源（设计审查 P2-10，2026-08-20）。

收敛此前散落在 market_extra / fundamentals / concept / data_source 的重复实现：
- bounded_call：daemon 线程 + join(timeout) 的限时网络调用包装（超时抛 TimeoutError，
  调用方按失败降级）。原 market_extra / fundamentals 各一份同构实现，仅超时文案不同，
  易漂移。
- EASTMONEY_HEADERS：直连东财 push2delay API 的请求头（UA + Referer + Accept），
  原 market_extra / concept 各定义一份完全相同的 _EM_HEADERS，data_source 一份子集。
  统一到此处单源，避免东财侧协议变更时多处漏改。

纯基础设施：只依赖标准库，不 import 其它 scanner 模块（避免环依赖）。
"""
import threading
from typing import Any, Callable

# 直连东财 push2delay API 的请求头（浏览器 UA + Referer + Accept）。
# 东财 clist/ulist 接口对请求头校验宽松，但 Referer/UA 缺失时部分接口返回空，
# 故集中一份完整 headers，market_extra / concept / data_source 共用。
EASTMONEY_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://emweb.securities.eastmoney.com/",
}

# 东财可达 host（push2 直连/代理在本机均不可达，push2delay 提供相同 API 且可达）。
EASTMONEY_PUSH2DELAY_HOST = "push2delay.eastmoney.com"

# 东财公开 ulist/clist 接口的固定 token（网页端同一值，非密钥）。
# 原 data_source / market_extra 各硬编码一份，收敛单源防漂移。
EASTMONEY_UT_TOKEN = "b2884a393a59ad64002292a3e90d46a5"  # noqa: S105 - 非密钥：东财网页端公开的固定参数

def eastmoney_no_proxy() -> dict[str, Any]:
    """直连东财（push2delay 等）用的显式「无代理」参数 —— **每次调用返回新 dict**。

    本机若设了系统代理（Windows 注册表 / HTTP(S)_PROXY），requests 默认把请求路由到
    代理；该代理午后健康度下降/间歇断连时，会把「本应直连」的调用打成
    `ProxyError: RemoteDisconnected` —— 2026-10-22 个股资金流「获取失败（0 只）」
    生产故障的根因。与 api.py:394 / panel_reporter.py:52 的 `Session.trust_env=False`
    同一意图（东财直连不经系统代理）。

    ⚠ 值必须是 None（不是删键、也不是空 dict）：requests 的
    `merge_environment_settings` 对环境代理走 `setdefault` —— 空 dict 会让 env 代理
    **合并回来**，显式 None 才真正对该 scheme 关闭代理。

    ⚠ 必须**每次新建**、不得复用模块级常量：`setdefault` 会就地写入 env 代理里
    `http`/`https` 之外的键（实测 `ftp`），共享对象会被永久污染；且 `_collect_fund_flow`
    在 6 线程池内并发调用，共享可变 dict 有竞态。typeshed 把 proxies 标注为
    `dict[str, str] | None`，故用 Any 承载 None 值。
    """
    return {"http": None, "https": None}


def bounded_call(fn: Callable[[], Any], timeout: float, label: str = "网络调用") -> Any:
    """带限时执行网络调用：超时抛 TimeoutError，调用方按失败降级。

    用 daemon 线程 + join(timeout) 而非 ThreadPoolExecutor，超时后线程在后台
    自然结束（daemon 不阻塞进程退出），主扫描循环不被外部 host 挂死。
    label 仅用于超时异常文案，标识被限时的调用来源。
    """
    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["error"] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        raise TimeoutError(f"{label} 超过 {timeout}s 已放弃")
    if "error" in box:
        raise box["error"]
    return box.get("value")
