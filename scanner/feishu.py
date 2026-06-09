from datetime import datetime

import requests

from scanner.config import FEISHU_WEBHOOK, FEISHU_KEYWORD
from scanner.models import Candidate


def push_feishu(new_faces: list[Candidate], momentum: list[Candidate], gem_total: int, conn=None):
    if not FEISHU_WEBHOOK:
        return

    now = datetime.now().strftime("%H:%M")
    all_c = new_faces + momentum

    sec_cnt: dict[str, int] = {}
    for c in all_c:
        if c.sector:
            sec_cnt[c.sector] = sec_cnt.get(c.sector, 0) + 1
    sec_hot = " ".join(f"{s}{n}" for s, n in sorted(sec_cnt.items(), key=lambda x: -x[1])[:2])

    lines = [f"{FEISHU_KEYWORD}",
             f"{now} 新{len(new_faces)}动{len(momentum)}" + (f" | {sec_hot}" if sec_hot else "")]

    if new_faces:
        lines.append(f"▎新")
        for c in new_faces:
            s = c.stock
            lines.append(f" {s.rank} {s.name} {s.percent:+.1f}% {c.score}分")

    if momentum:
        lines.append(f"▎动量")
        for c in momentum:
            s = c.stock
            lines.append(f" {s.rank} {s.name} {s.percent:+.1f}% {c.score}分")

    text = "\n".join(lines)

    try:
        resp = requests.post(FEISHU_WEBHOOK, json={
            "msg_type": "text",
            "content": {"text": text},
        }, timeout=10)
        result = resp.json()
        if result.get("code") != 0:
            print(f"\n  [!] 推送失败: {result.get('msg')}")
    except Exception as e:
        print(f"\n  [!] 推送异常: {e}")
