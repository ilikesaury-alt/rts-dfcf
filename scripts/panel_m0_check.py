"""M0 门「判据 1」的自动化执行器（design §13.1 M0）。

**逐条比对终端渲染与云端读回**：行数 / 首行 symbol / 完整顺序，三项全等才算通过。

用法（本地 wrangler dev，无需公网域名）::

    cd rts-panel-cloud/worker && npx wrangler dev --local --port 8787
    python scripts/panel_m0_check.py --url http://127.0.0.1:8787 --rounds 3

为什么要有这个脚本：M0 判据 1 原本被记为「必须人工」，但它要验的是
**序列化→ingest→regions 读回**这条链路的一致性，与公网/Access/WAF 无关
（那些是 M2 的判据）。本地 wrangler dev + 本地 D1 就能把这条链路完整跑通，
把 M0 的**技术**部分从「等云端」里解放出来，只把公网面留给 M2。

⚠ 本脚本**复用生产代码路径**（`build_scan_view` → `render_terminal` → `report()`），
不自造 view —— 自造 view 验的是「我自己和自己一致」，没有意义。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scanner.config import DB_PATH  # noqa: E402
from scanner.panel_reporter import report  # noqa: E402
from scanner.view.assemble import build_scan_view  # noqa: E402
from scanner.view.render import render_terminal  # noqa: E402

SYM_RE = re.compile(r"\bS[ZH]\d{6}\b")
# 区块标题：终端四区块的固定前缀。用来把一张大表切成四个区域分别比对。
REGION_RE = re.compile(r"^\s*◆\s*(v1 池选|v1 回捞|沪深飙升|榜外异动|飞书过门)")


def terminal_regions(text: str) -> dict[str, list[str]]:
    """从终端输出里切出各区块的有序 symbol 列表（按渲染顺序）。"""
    out: dict[str, list[str]] = {}
    cur: str | None = None
    for line in text.splitlines():
        if "◆" in line:
            m = REGION_RE.match(line)
            if m:
                cur = m.group(1)
                out.setdefault(cur, [])
                continue
            cur = None
        if cur is None:
            continue
        for s in SYM_RE.findall(line):
            out[cur].append(s)
    return out


def cloud_regions(payload: dict) -> dict[str, list[str]]:
    """从 `/api/regions` 响应里取各区块的有序 symbol 列表。

    云端**不重排**（FR-V2），故这里必须与终端同序；顺序不一致即为真 bug。
    """
    r = payload["data"]["regions"]
    keymap = {"v1 池选": "main", "v1 回捞": "hist", "沪深飙升": "hot", "榜外异动": "offboard"}
    out: dict[str, list[str]] = {}
    for label, key in keymap.items():
        out[label] = [row.get("symbol", "") for row in (r.get(key) or [])]
    return out


def get_json(url: str, timeout: float = 10.0) -> dict:
    # S310 会同时标记 Request(...) 与 urlopen(...) 两行开 URL 的调用，
    # 故两个都要 noqa。URL 由调用方（--url）指定，非本脚本内生的外部输入。
    req = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode())


def one_round(conn: sqlite3.Connection, ingest_url: str, regions_url: str, secret: str, seq: int) -> dict:
    """跑一轮：终端渲染 → 上报 → 云端读回 → 逐条比对。返回该轮的比对结果。"""
    view = build_scan_view(conn=conn)
    if view is None:
        return {"ok": False, "why": "build_scan_view 返回 None（当日无推荐）"}

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        render_terminal(view)
    t_regions = terminal_regions(buf.getvalue())

    now = datetime.now()
    date_s, time_s = now.date().isoformat(), now.strftime("%H:%M:%S")
    report(view, url=ingest_url, secret=secret, conn=conn, seq=seq, date=date_s, time=time_s, durationMs=0)

    # report() 是异步的（daemon 线程）→ 轮询等本轮落库
    payload = None
    matched = False
    for _ in range(100):  # 最多 10s
        time.sleep(0.1)
        try:
            payload = get_json(regions_url)
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            continue
        if payload.get("data", {}).get("date") == date_s and payload.get("data", {}).get("time") == time_s:
            matched = True
            break
    # ⚠ 必须用 matched 判定成功，**不能**只看 payload != None：
    #   若库里另有一个 time 更晚的行（取轮按 time DESC 排序，它会遮住本轮），
    #   轮询拿到的就会是那一条 —— 带着它去比对会报出「行数 0」这种
    #   看似是产品 bug、实为测试数据污染的假失败（本人已踩过一次）。
    if not matched or payload is None:
        latest = payload.get("data", {}) if payload else {}
        return {
            "ok": False,
            "why": f"10s 内未读回本轮 ({date_s} {time_s})；当前云端最新为 {latest.get('date')} {latest.get('time')}",
        }

    c_regions = cloud_regions(payload)
    diffs: list[str] = []
    for label, t_syms in t_regions.items():
        c_syms = c_regions.get(label, [])
        if label == "飞书过门":
            # 过门区终端只打印**名字**不打印代码，且是门内子集，单独口径。
            continue
        if len(t_syms) != len(c_syms):
            diffs.append(f"{label}: 行数 终端{len(t_syms)} != 云端{len(c_syms)}")
            continue
        if t_syms and c_syms[0] != t_syms[0]:
            diffs.append(f"{label}: 首行 终端{t_syms[0]} != 云端{c_syms[0]}")
        if t_syms != c_syms:
            # strict=True：上一行已断言两序列等长，这里把该不变量交给运行时兜住，
            # 免得日后有人删掉行数检查后，静默变成「只比对公共前缀」。
            first = next((i for i, (a, b) in enumerate(zip(t_syms, c_syms, strict=True)) if a != b), -1)
            diffs.append(f"{label}: 第{first + 1}行起顺序不同 终端{t_syms[first:][:3]} vs 云端{c_syms[first:][:3]}")
    return {
        "ok": not diffs,
        "diffs": diffs,
        "date": payload["data"]["date"],
        "time": payload["data"]["time"],
        "counts": {k: len(v) for k, v in t_regions.items() if k != "飞书过门"},
        "cloud_counts": {k: len(v) for k, v in c_regions.items()},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="M0 判据 1：终端 vs 云端逐条比对")
    ap.add_argument("--url", default="http://127.0.0.1:8787", help="Worker 根地址（不含 /api）")
    ap.add_argument("--secret", default="local-m0-secret", help="INGEST_SECRET（须与 .dev.vars 一致）")
    ap.add_argument("--rounds", type=int, default=3, help="连续轮次（M0 要求 3）")
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()

    base = args.url.rstrip("/")
    ingest = f"{base}/api/ingest"
    regions_url = f"{base}/api/regions"

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    results = []
    for i in range(1, args.rounds + 1):
        r = one_round(conn, ingest, regions_url, args.secret, seq=i)
        results.append(r)
        if r["ok"]:
            print(f"[轮 {i}] ✅ 一致  counts={r['counts']}  cloud={r.get('cloud_counts')}")
        else:
            print(f"[轮 {i}] ❌ {r.get('why') or r.get('diffs')}")
        time.sleep(1.5)  # 模拟轮间隔，让 (date,time) 天然不同

    ok = sum(1 for r in results if r["ok"])
    print(f"\n=== 判据 1：{ok}/{args.rounds} 轮逐条一致 ===")
    print("PASS" if ok == args.rounds else "FAIL")
    return 0 if ok == args.rounds else 1


if __name__ == "__main__":
    sys.exit(main())
