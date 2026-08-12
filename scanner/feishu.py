import requests

from scanner.config import FEISHU_KEYWORD, FEISHU_MIN_INTERVAL, FEISHU_WEBHOOK, now_beijing
from scanner.display import fund_flow_signal, split_risk_flags
from scanner.models import Candidate

_last_push_time: float = 0.0
_last_push_symbols: set[str] = set()


def _build_card(
    new_faces: list[Candidate],
    momentum: list[Candidate],
    stale_candidates: list[Candidate],
    gem_total: int,
    filtered_large_cap: int = 0,
    current_rank_map: dict[str, int] | None = None,
    short_term_list: list[Candidate] | None = None,
    rebound_list: list[Candidate] | None = None,
    comeback_list: list[Candidate] | None = None,
) -> dict:
    now = now_beijing().strftime("%H:%M")
    if short_term_list is None:
        short_term_list = []
    if rebound_list is None:
        rebound_list = []
    if comeback_list is None:
        comeback_list = []
    all_c = new_faces + momentum + rebound_list + comeback_list + short_term_list

    sec_cnt: dict[str, int] = {}
    for c in all_c:
        if c.sector:
            sec_cnt[c.sector] = sec_cnt.get(c.sector, 0) + 1
    hot_secs = sorted(sec_cnt.items(), key=lambda x: -x[1])[:3]
    sec_line = " | ".join(f"{s}({n})" for s, n in hot_secs)

    # 大盘环境标签：与 display.py 对齐，从首个 candidate 的 dimensions 读取
    env_bonus = 0
    if all_c and all_c[0].kline:
        env_bonus = all_c[0].kline.dimensions.get("market_env_bonus", 0) or 0
    if env_bonus > 0:
        env_tag = " | 🟢大盘强势"
    elif env_bonus < 0:
        env_tag = " | 🔴大盘弱势·谨慎"
    else:
        env_tag = " | ⚪大盘中性"

    header_text = (f"**{now}** | 🟢新{len(new_faces)} 📈动{len(momentum)} "
                   f"🔄反{len(rebound_list)} 🌀马{len(comeback_list)} "
                   f"🔴超{len(short_term_list)}{env_tag}")
    if stale_candidates:
        header_text += f" ⏳掉{len(stale_candidates)}"
    if sec_line:
        header_text += f" | 🔥 {sec_line}"

    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": header_text}},
    ]

    # 顺序与 display.py 对齐：新面孔 → 动量 → 超短
    sections = [
        ("🆕 新面孔", new_faces),
        ("📈 动量延续", momentum),
        ("🔄 超跌反弹 — 暴跌后企稳/反转", rebound_list),
        ("🌀 回马枪 — 掉榜超跌/回调买点", comeback_list),
        ("🔴 超短次日", short_term_list),
    ]

    # 双挂去重：同一 symbol 在多个桶出现时，仅在先展示的桶显示一次
    displayed_syms: set[str] = set()

    def _fmt_row(c: Candidate, rank_str: str = "") -> str:
        s = c.stock
        rs = rank_str or f"{s.rank:>3}"
        pct_str = f"+{s.percent:.1f}%" if s.percent >= 0 else f"{s.percent:.1f}%"
        acc_val = c.kline.accumulated_pct if c.kline else None
        acc_str = f"{acc_val:+.1f}%" if acc_val is not None else "N/A"
        # 风险标签分级显示（与 display.py 共用 split_risk_flags，阈值集中在 config）：
        # 硬信号（超买/主力出货/趋势破位）展开，软信号折叠成 +N
        hard, soft_count = split_risk_flags(c.risk_flags)
        risk_parts = []
        if hard:
            tag = f"⚠{'/'.join(hard)}"
            if soft_count:
                tag += f"+{soft_count}"
            risk_parts.append(tag)
        elif soft_count:
            risk_parts.append(f"⚠+{soft_count}")
        risk_str = (" " + " ".join(risk_parts)) if risk_parts else ""
        # 行情增强标记：主力资金流强弱图标（5 档，与 display.py 同规则）+ 连板（仅在有数据时显示）
        dims = c.kline.dimensions if c.kline else {}
        extra_parts = []
        ff_pct = dims.get("fund_flow_main_pct")
        if ff_pct is not None:
            mark = {
                "strong_in": "🟢🟢",
                "in": "🟢",
                "neutral": "⚪",
                "out": "🔴",
                "strong_out": "🔴🔴",
            }.get(fund_flow_signal(float(ff_pct)))
            if mark:
                extra_parts.append(mark)
        if dims.get("zt_lianban"):
            extra_parts.append(f"📈{dims['zt_lianban']}板")
        extra_str = (" " + " ".join(extra_parts)) if extra_parts else ""
        return f"`{rs} {s.name:<8} {s.symbol} {pct_str:>7} {acc_str:>7}  {c.score:>2}分{risk_str}{extra_str}`"

    first = True
    for title, items in sections:
        # 双挂去重：跳过已展示的 symbol
        deduped = []
        for c in items[:5]:
            if c.stock.symbol in displayed_syms:
                continue
            displayed_syms.add(c.stock.symbol)
            deduped.append(c)
        if not deduped:
            continue
        if first:
            first = False
            elements.append({"tag": "hr"})
        else:
            elements.append({"tag": "hr"})
        content = f"**{title}**\n"
        for c in deduped:
            content += _fmt_row(c) + "\n"
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": content.rstrip("\n")}})

    if stale_candidates:
        elements.append({"tag": "hr"})
        content = "**⏳ 掉榜回顾**\n"
        if current_rank_map is None:
            current_rank_map = {}
        for c in stale_candidates[:5]:
            cur_rank = current_rank_map.get(c.stock.symbol)
            rank_str = f"{cur_rank:>3}" if isinstance(cur_rank, int) else f"{'—':>3}"
            content += _fmt_row(c, rank_str=rank_str) + "\n"
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": content.rstrip("\n")}})

    if not first or stale_candidates:
        elements.append({"tag": "hr"})
    elements.append({
        "tag": "note",
        "elements": [
            {"tag": "plain_text",
             "content": f"创业板共{gem_total}只"
                        + (f" | 过滤{filtered_large_cap}只" if filtered_large_cap else "")}
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
                     short_term_list: list[Candidate],
                     rebound_list: list[Candidate] | None = None,
                     comeback_list: list[Candidate] | None = None) -> set[str]:
    rebound_list = rebound_list or []
    comeback_list = comeback_list or []
    return {c.stock.symbol for c in new_faces + momentum + rebound_list + comeback_list + short_term_list}


def push_feishu(
    new_faces: list[Candidate],
    momentum: list[Candidate],
    stale_candidates: list[Candidate],
    gem_total: int,
    filtered_large_cap: int = 0,
    current_rank_map: dict[str, int] | None = None,
    short_term_list: list[Candidate] | None = None,
    rebound_list: list[Candidate] | None = None,
    comeback_list: list[Candidate] | None = None,
) -> bool:
    global _last_push_time, _last_push_symbols

    if not FEISHU_WEBHOOK:
        return False

    if short_term_list is None:
        short_term_list = []
    if rebound_list is None:
        rebound_list = []
    if comeback_list is None:
        comeback_list = []

    import time
    now = time.time()
    current_symbols = _extract_symbols(new_faces, momentum, short_term_list, rebound_list, comeback_list)
    has_change = current_symbols != _last_push_symbols

    if not has_change and (now - _last_push_time) < FEISHU_MIN_INTERVAL:
        return False

    try:
        card = _build_card(new_faces, momentum, stale_candidates,
                           gem_total, filtered_large_cap, current_rank_map,
                           short_term_list, rebound_list, comeback_list)
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
