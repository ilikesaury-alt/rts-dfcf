"""共享网络层单源（设计审查 P2-10，2026-08-20）。

收敛此前散落在 market_extra / fundamentals / concept / data_source 的重复实现：
- _bounded_call：daemon 线程 + join(timeout) 的限时网络调用包装（超时抛 TimeoutError，
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
EASTMONEY_UT_TOKEN = "b2884a393a59ad64002292a3e90d46a5"


def _bounded_call(fn: Callable[[], Any], timeout: float, label: str = "网络调用") -> Any:
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
