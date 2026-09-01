import os

from scanner.config import LOG_DIR, now_beijing
from scanner.models import Candidate


def log_event(message: str) -> None:
    """通用事件追加到 logs/feishu_push.log（推送成功/失败/跳过等，供排查）。

    fail-open：目录创建或落盘失败各自静默吞掉，不阻断调用方。
    """
    log_path = os.path.join(LOG_DIR, "feishu_push.log")
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except OSError:
        return
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{now_beijing().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except OSError:
        pass


def log_results(new_faces: list[Candidate], momentum: list[Candidate]):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except OSError:
        return
    today = now_beijing().date().isoformat()
    log_file = os.path.join(LOG_DIR, f"scan_{today}.csv")
    try:
        is_new = not os.path.exists(log_file)
        with open(log_file, "a", encoding="utf-8") as f:
            if is_new:
                f.write("时间,分类,名称,代码,现价,涨幅,趋势,5日累计,量比,评分,分时评分\n")
            now = now_beijing().strftime("%H:%M:%S")
            for c in new_faces + momentum:
                if c.is_stale:
                    continue
                k = c.kline
                tag = {
                    "new_face": "新",
                    "known_new_face": "新",
                    "momentum": "动量",
                    "rebound": "反",
                    "short_term": "超短",
                    "pool_pick": "池选",
                }.get(c.category, "?")
                intra = f"{c.intraday_score:+.1f}" if c.intraday_score is not None and c.intraday_score != 0.0 else ""
                f.write(
                    f"{now},{tag},{c.stock.name},{c.stock.symbol},{c.stock.current:.2f},"
                    f"{c.stock.percent:+.2f}%,{k.trend if k else ''},"
                    f"{k.accumulated_pct if k else ''},{k.volume_ratio if k else ''},"
                    f"{c.score},{intra}\n"
                )
    except OSError:
        pass
