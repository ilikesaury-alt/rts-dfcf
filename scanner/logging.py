import os
from datetime import date, datetime

from scanner.config import LOG_DIR
from scanner.models import Candidate


def log_results(new_faces: list[Candidate], old_faces: list[Candidate], momentum: list[Candidate]):
    os.makedirs(LOG_DIR, exist_ok=True)
    today = date.today().isoformat()
    log_file = os.path.join(LOG_DIR, f"scan_{today}.csv")
    is_new = not os.path.exists(log_file)
    with open(log_file, "a", encoding="utf-8") as f:
        if is_new:
            f.write("时间,分类,名称,代码,现价,涨幅,趋势,5日累计,量比,评分\n")
        now = datetime.now().strftime("%H:%M:%S")
        for c in (new_faces + momentum + old_faces):
            k = c.kline
            tag = {"new_face": "新", "momentum": "动量", "old_face": "旧"}.get(c.category, "?")
            f.write(f"{now},{tag},{c.stock.name},{c.stock.symbol},{c.stock.current:.2f},{c.stock.percent:+.2f}%,{k.trend if k else ''},{k.accumulated_pct if k else ''},{k.volume_ratio if k else ''},{c.score}\n")
