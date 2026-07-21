import requests

from scanner.config import FEISHU_KEYWORD, FEISHU_MIN_INTERVAL, FEISHU_WEBHOOK, now_beijing
from scanner.models import Candidate

_last_push_time: float = 0.0
_last_push_symbols: set[str] = set()


def _build_card(
    new_faces: list[Candidate],
    momentum: list[Candidate],
    pullback_list: list[Candidate],
    stale_candidates: list[Candidate],
    gem_total: int,
    filtered_large_cap: int = 0,
    current_rank_map: dict[str, int] | None = None,
    short_term_list: list[Candidate] | None = None,
) -> dict:
    now = now_beijing().strftime("%H:%M")
    if short_term_list is None:
        short_term_list = []
    all_c = new_faces + momentum + pullback_list + short_term_list

    sec_cnt: dict[str, int] = {}
    for c in all_c:
        if c.sector:
            sec_cnt[c.sector] = sec_cnt.get(c.sector, 0) + 1
    hot_secs = sorted(sec_cnt.items(), key=lambda x: -x[1])[:3]
    sec_line = " | ".join(f"{s}({n})" for s, n in hot_secs)

    header_text = f"**{now}** | 🟢新{len(new_faces)} 📈动{len(momentum)} 🔄回{len(pullback_list)} 🔴超{len(short_term_list)}"
    if stale_candidates:
        header_text += f" ⏳掉{len(stale_candidates)}"
    if sec_line:
        header_text += f" | 🔥 {sec_line}"

    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": header_text}},
    ]

    sections = [
        ("🆕 新面孔", new_faces),
        ("📈 动量延续", momentum),
        ("🔄 回调介入", pullback_list),
        ("🔴 超短次日", short_term_list),
    ]

    first = True
    for title, items in sections:
        if not items:
            continue
        if first:
            first = False
            elements.append({"tag": "hr"})
        else:
            elements.append({"tag": "hr"})
        content = f"**{title}**\n"
        for c in items[:5]:
            s = c.stock
            pct_str = f"+{s.percent:.1f}%" if s.percent >= 0 else f"{s.percent:.1f}%"
            acc_val = c.kline.accumulated_pct if c.kline else None
            acc_str = f"{acc_val:+.1f}%" if acc_val is not None else "N/A"
            content += f"`{s.rank:>3} {s.name:<8} {s.symbol} {pct_str:>7} {acc_str:>7}  {c.score:>2}分`\n"
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": content.rstrip("\n")}})

    if stale_candidates:
        elements.append({"tag": "hr"})
        content = "**⏳ 掉榜回顾**\n"
        if current_rank_map is None:
            current_rank_map = {}
        for c in stale_candidates[:5]:
            s = c.stock
            rank_str = f"{current_rank_map.get(s.symbol, '—'):>3}" if isinstance(current_rank_map.get(s.symbol), int) else f"{'—':>3}"
            pct_str = f"+{s.percent:.1f}%" if s.percent >= 0 else f"{s.percent:.1f}%"
            acc_val = c.kline.accumulated_pct if c.kline else None
            acc_str = f"{acc_val:+.1f}%" if acc_val is not None else "N/A"
            content += f"`{rank_str} {s.name:<8} {s.symbol} {pct_str:>7} {acc_str:>7}  {c.score:>2}分`\n"
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": content.rstrip("\n")}})

    if not first or stale_candidates:
        elements.append({"tag": "hr"})
    elements.append({
        "tag": "note",
        "elements": [
            {"tag": "plain_text", "content": f"创业板共{gem_total}只" + (f" | 过滤{filtered_large_cap}只" if filtered_large_cap else "")}
        ],
    })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"🧧 {FEISHU_KEYWORD} 扫描简报"},
            "template": "indigo",
        },
        "elements": elements,
    }


def _extract_symbols(new_faces: list[Candidate], momentum: list[Candidate],
                     pullback_list: list[Candidate], short_term_list: list[Candidate]) -> set[str]:
    return {c.stock.symbol for c in new_faces + momentum + pullback_list + short_term_list}


def push_feishu(
    new_faces: list[Candidate],
    momentum: list[Candidate],
    pullback_list: list[Candidate],
    stale_candidates: list[Candidate],
    gem_total: int,
    filtered_large_cap: int = 0,
    current_rank_map: dict[str, int] | None = None,
    short_term_list: list[Candidate] | None = None,
) -> bool:
    global _last_push_time, _last_push_symbols

    if not FEISHU_WEBHOOK:
        return False

    if short_term_list is None:
        short_term_list = []

    import time
    now = time.time()
    current_symbols = _extract_symbols(new_faces, momentum, pullback_list, short_term_list)
    has_change = current_symbols != _last_push_symbols

    if not has_change and (now - _last_push_time) < FEISHU_MIN_INTERVAL:
        return False

    try:
        card = _build_card(new_faces, momentum, pullback_list, stale_candidates,
                           gem_total, filtered_large_cap, current_rank_map,
                           short_term_list)
        resp = requests.post(FEISHU_WEBHOOK,
                             json={"msg_type": "interactive", "card": card},
                             timeout=10)
        result = resp.json()
        if result.get("code") != 0:
            print(f"\n  [!] 飞书推送失败: {result.get('msg')}")
            return False
        _last_push_time = now
        _last_push_symbols = current_symbols
        return True
    except Exception as e:
        print(f"\n  [!] 飞书推送异常: {e}")
        return False
