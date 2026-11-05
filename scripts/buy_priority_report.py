#!/usr/bin/env python
"""scripts/buy_priority_report.py —— 盘中「买入优先级反馈报告」（2026-11-05）。

**独立脚本：只读库、不写库、不改任何生产逻辑、不进主循环/终端/飞书。**

## 用法（盘中随跑随出）

```bash
python scripts/buy_priority_report.py                      # 当日候选 → 终端报告（默认 ~8s）
python scripts/buy_priority_report.py --no-ann             # 不拉公告（纯本地 ~0.5s）
python scripts/buy_priority_report.py --out 今日报告.md     # 同时落盘
python scripts/buy_priority_report.py --symbols 300638,300568   # 指定清单
python scripts/buy_priority_report.py --dtb 0              # 关掉龙虎榜（省 2.6s）
python scripts/buy_priority_report.py --minute 4           # 加分时均价（雪球，+18s）
python scripts/buy_priority_report.py --ai 3               # 加 AI 深度复核（≈60s/只，慢档）
```

## 耗时预算（实测，2026-11-05）

| 档位 | 耗时 | 说明 |
|---|---|---|
| `--no-ann` | **0.5s** | 纯本地库，零网络 |
| 默认 | **8.3s** | 本地计算 + 公告 12 只 + 龙虎榜 4 只 |
| `--minute 4` | 26s | 雪球 minute.json 慢，只对第一/二梯队前 N 只 |
| `--ai 3` | +3min | LLM research（模型自己查真实数据），**只对重点票** |

⇒ 盘中「跑一下就出报告」。要 AI 就 `--ai 2~3`（别全量），要分时就 `--minute 2`。

## 证据来源（三层，零网络优先）

**① 零网络（默认全开，全部来自本地库）**

| 证据 | 来源 | 作用 |
|---|---|---|
| 类别先验 | `config_scoring.CATEGORY_HIT_RATE` | 唯一目标函数 = 次日 ≥7% hit 率（**只占 25 分**，不是主轴） |
| 当日资金 | `market_extra_cache` 当日快照 | 主力净占比/净额/超大单/量比/换手/成交额/市值 |
| 中期资金 | 同上，按日累计 | 5 日窗（10 日窗会误伤刚走出来的票）+ 20 日长周期双负才算失血 |
| 位置 | `daily_kline`（**排除当日半成品 bar**） | MA5 偏离、历史 5 日累计、量能萎缩 |
| 个股历史兑现率 | `recommendations.next_day_pct`（symbol-day 去重） | n≥20 才采信；n≥10 且 0 命中 ⇒ 降一档 |
| **个股「大涨之后」模式** | 同上（percent≥5% 的那些日子） | 该票自己大涨之后的次日表现（最强反证） |
| **板块排位** | `concept_cache` × 当日涨幅 | 它在主板块里排第几（跟随 vs 领涨） |
| **市场环境** | `daily_kline` 当日 | 普涨 / 结构行情（决定整体该不该出手） |
| 榜位 | `appearances` | 飙升榜名次 |

**② 联网（可选 / 半自动）**：公告事件（东财，默认对前 12 只）、龙虎榜（东财，默认前 4 只）、
分时均价（雪球，`--minute N`）、AI 深度复核（`--ai N`）。

**③ 未纳入**：东财日频资金流（fflow klt=101）—— 比本地快照口径准，但每只 1 请求。

## 评分与分档（**报告层规则，全部写死在本文件，可改可查**）

正向分项（合计 105）：先验 25 · 当日资金 20 · 中期资金 20 · 位置 20 · 个股历史 10 · 弹性 10
惩罚项（最多 −60）：已验证负面公告 −10/类 · C 档先验 −12 · 个股历史 0 命中 −8 ·
当日主力流出 −10 · 长短双失血 −8 · 换手过热 −8 · 成交额不足 −6 · 收盘低于均价 −6 ·
大涨后历史为负 −6 · 价格出弹性带 −4 · 偏离 MA5 过大 −5 · 量能萎缩 −4 · 市值超限 −3

**两条封顶规则**（用户 2026-11-05 决策：这类公告的确应该降级）：
- 先验 C 档（< 全体基线 7.8%）⇒ **最高第三梯队**；
- 命中**已验证负面**公告（减持/对外担保/股权激励）⇒ **最高第二梯队**。

**硬否决 → 回避**：主力 ≤ −8% · 当日涨幅 ≥ 8%（追高）· 历史 5 日累计 ≥ 50%（过热）· 超买硬信号。

## 公告事件的证据分层（**别混用**）

| 事件 | 证据等级 | 数字（v1 主线 1025 行 / 28 日，`scripts/ann_event_backtest.py` 实测） |
|---|---|---|
| 减持 | **已验证负面 ⇒ 降档** | 命中 0/26 vs 对照 7.29%；日配对 Δ −5.45pp，逐日 0/8 |
| 对外担保 | **已验证负面 ⇒ 降档** | 5.77% vs 7.17%；Δ −7.07pp，逐日 0/12，p=0.0005，分半同向 |
| 股权激励类 | **已验证负面 ⇒ 降档** | 3.39% vs 7.33%；Δ −7.24pp，逐日 0/14，p=0.0001 |
| 股份质押 | **证据冲突 ⇒ 只提示** | 日配对 −3.57（负）但按行汇总 +2.84（正）⇒ 不降档 |
| 计提减值 / 终止投资 / 解禁流通 | 机制可信未验证 ⇒ 只提示 | 有效日仅 5/1/0 ⇒ 样本不足 |

⚠ 两条硬约束：① 结论**总体特异**，不可跨段搬用；② **两个门合并即失效**（Δ −1.11pp CI 跨 0）⇒ 分门用。

## 已知缺口（报告尾部也会打出来）

- 板块排位**未做样本外检验** ⇒ 只作提示，**不进分数**；
- 资金累计用本地每日快照（与东财日频收盘值有差异）⇒ 长周期失血要求 5 日与 20 日**双负**才扣分；
- 公告只对排序靠前 N 只拉取 ⇒ 第一/二梯队之外可能未标（**未标 ≠ 没有**）；
- 候选池 = 当日 recommendations 去退池类别（**终端 v1 池选的超集**；终端另按资金流出/追高/减仓标签过滤）。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics as st
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scanner.config import DISPLAY_MAX_TODAY_PCT, HOT_MAX_MARKET_CAP  # noqa: E402
from scanner.config_scoring import (  # noqa: E402
    CATEGORY_HIT_RATE,
    CATEGORY_HIT_RATE_DEFAULT,
    OVERHEAT_ACCUM_MAX,  # noqa: E402
)
from scanner.config_sources import FUND_OUTFLOW_NET_PCT  # noqa: E402

# ── 报告层参数（自定，可改）──────────────────────────────────────────────
TIER_A_MIN = 0.10  # 先验 A 档下限（同 push_gate 的口径）
TIER_B_MIN = CATEGORY_HIT_RATE_DEFAULT  # 先验 B 档下限 = 全体基线 7.8%
PRICE_IDEAL = (3.0, 40.0)  # 次日 +7% 的弹性友好价区（沿用 hot_watch 理想区间）
HIST_TRUST_N = 20  # 个股历史兑现率 n≥20 才采信
HIST_HINT_N = 10  # n≥10 且 0 命中 ⇒ 降一档（方向性反证）
FLOW_WINDOW = 5  # 中期资金窗（**5 个交易日**：10 日窗会把「刚从失血走出来」的票误判成失血）
FLOW_LONG_WINDOW = 20  # 长周期失血检查（只作惩罚项，不进正向分）
MIN_AMOUNT = 1e8  # 当日成交额下限（低于此值 ⇒ 次日 +7% 需要的增量资金不够）
CHASE_PCT = 6.0  # 当日涨幅 ≥ 此值 ⇒ 已冲高，买点不在今日

#: 综合分档线（先跑真实数据看过分布后定的，改这里即可）
SCORE_T1 = 58.0  # ≥58 → 第一梯队
SCORE_T2 = 47.0  # ≥47 → 第二梯队
SCORE_T3 = 34.0  # ≥34 → 第三梯队；<34 → 仅观察

#: 公告事件 → 关键词（**报告口径**；研究用的更宽表在 scripts/ann_event_backtest.py）
ANN_EVENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "减持": ("减持",),
    "对外担保": ("对外担保", "担保额度", "提供担保"),
    "股权激励": ("作废", "回购注销限制性股票", "限制性股票", "股权激励"),
    "股份质押": ("质押", "解质"),
    "计提减值": ("计提资产减值", "减值准备", "商誉减值"),
    "终止投资": ("终止对外投资", "注销子公司", "终止重组", "终止重大资产"),
    "解禁流通": ("解除限售", "上市流通", "限售股份"),
}
#: 已验证负面（v1 主线四口径一致负）⇒ 命中**降一档**
ANN_NEGATIVE_VERIFIED = ("减持", "对外担保", "股权激励")
#: 机制可信但未验证 ⇒ 命中**只提示、不降档**
ANN_UNVERIFIED = ("股份质押", "计提减值", "终止投资", "解禁流通")
ANN_LOOKBACK = 10  # 信号日之前 N 个自然日（含当日）

CAT_SHORT = {
    "rebound": "RBD",
    "known_new_face": "kNF",
    "momentum": "MOM",
    "new_face": "NEW",
    "short_term": "ST",
}
EXCLUDED_CATS = ("comeback", "core_dip", "pool_pick", "pullback")


# ── 取数 ────────────────────────────────────────────────────────────────
def load_candidates(conn: sqlite3.Connection, day: str) -> list[dict]:
    rows = conn.execute(
        "SELECT symbol, name, category, score, percent, concept, accumulated_pct, score_breakdown "
        "FROM recommendations WHERE date = ? AND excluded = 0 ORDER BY id",
        (day,),
    ).fetchall()
    out = []
    for sym, name, cat, score, pct, concept, accum, bb in rows:
        if cat in EXCLUDED_CATS:
            continue
        try:
            bd = json.loads(bb) if bb else {}
        except json.JSONDecodeError:
            bd = {}
        out.append(
            {
                "symbol": sym,
                "name": name or sym,
                "category": cat,
                "score": score,
                "db_pct": pct,
                "concept": concept,
                "accum_db": accum,
                "overbought": any(bool(v) for k, v in bd.items() if k.endswith("overbought") and v is not None),
            }
        )
    return out


def load_live(conn: sqlite3.Connection, day: str) -> tuple[dict[str, dict], str]:
    """当日资金流快照（market_extra_cache）→ {symbol: {...}} + 快照时点。"""
    rows = conn.execute(
        "SELECT symbol, payload_json, updated FROM market_extra_cache WHERE date = ? AND data_type = 'fund_flow'",
        (day,),
    ).fetchall()
    out: dict[str, dict] = {}
    ts = ""
    for sym, payload, upd in rows:
        try:
            j = json.loads(payload or "{}")
        except json.JSONDecodeError:
            continue
        out[sym] = {
            "price": j.get("price"),
            "pct": j.get("percent"),
            "main_pct": j.get("main_pct"),
            "main_net": j.get("main_net"),
            "super_net": j.get("super_net"),
            "vol_ratio": j.get("vol_ratio"),
            "turnover": j.get("turnover"),
            "amount": j.get("amount"),
            "float_cap": j.get("float_cap"),
            "prev_close": j.get("prev_close"),
        }
        if upd and upd > ts:
            ts = upd
    return out, ts


def load_klines(conn: sqlite3.Connection, syms: list[str], day: str) -> dict[str, list[dict]]:
    """日线（**排除当日那根**：盘中 bar 是半成品，量能/位置指标会失真）。"""
    if not syms:
        return {}
    ph = ",".join("?" * len(syms))
    rows = conn.execute(
        f"SELECT symbol, date, close, percent, volume FROM daily_kline WHERE symbol IN ({ph}) "
        "AND date < ? ORDER BY symbol, date",
        (*syms, day),
    ).fetchall()
    out: dict[str, list[dict]] = {}
    for sym, d, close, pct, vol in rows:
        out.setdefault(sym, []).append({"date": d, "close": close, "pct": pct, "vol": vol})
    return out


def load_history(conn: sqlite3.Connection, day: str) -> dict[str, dict[str, float]]:
    """逐票历史兑现率：symbol-day 去重后的 next_day_pct 序列。"""
    rows = conn.execute(
        "SELECT symbol, date, next_day_pct FROM recommendations "
        "WHERE next_day_pct IS NOT NULL AND date < ? ORDER BY date",
        (day,),
    ).fetchall()
    out: dict[str, dict[str, float]] = {}
    for sym, d, ndp in rows:
        out.setdefault(sym, {})[d] = ndp
    return out


def load_appearances(conn: sqlite3.Connection, day: str) -> dict[str, int]:
    return {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT symbol, MIN(rank) FROM appearances WHERE date = ? GROUP BY symbol", (day,)
        ).fetchall()
    }


def load_flow_history(conn: sqlite3.Connection, syms: list[str], day: str) -> dict[str, dict]:
    """每票最近 N 个交易日的当日主力净额序列（market_extra_cache 的每日快照）。"""
    if not syms:
        return {}
    ph = ",".join("?" * len(syms))
    rows = conn.execute(
        f"SELECT symbol, date, payload_json FROM market_extra_cache "
        f"WHERE symbol IN ({ph}) AND data_type = 'fund_flow' AND date < ? ORDER BY symbol, date",
        (*syms, day),
    ).fetchall()
    out: dict[str, dict] = {}
    for sym, d, payload in rows:
        try:
            j = json.loads(payload or "{}")
        except json.JSONDecodeError:
            continue
        v = j.get("main_net")
        if v is None:
            continue
        out.setdefault(sym, {})[d] = float(v)
    return out


def load_sector_rank(conn: sqlite3.Connection, syms: list[str], day: str) -> dict[str, dict]:
    """每只票在**主板块**里的当日涨幅排位（**零网络**：本地 concept_cache JOIN daily_kline）。

    主板块 = 该票所属概念里、当日有行情数据的成分股最多的那个（避免用冷门概念）。
    返回 {symbol: {"concept", "rank", "total", "pct", "top_pct"}}；
    本地无概念/无当日行情 ⇒ 不在返回里（报告标「板块数据缺失」，**不当作掉队**）。
    """
    rows = conn.execute(
        "SELECT c.symbol, c.concepts, k.percent FROM concept_cache c "
        "JOIN daily_kline k ON k.symbol = c.symbol WHERE k.date = ? AND k.percent IS NOT NULL",
        (day,),
    ).fetchall()
    boards: dict[str, list[tuple[str, float]]] = {}
    member: dict[str, list[str]] = {}
    for sym, concepts, pct in rows:
        try:
            names = json.loads(concepts or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(names, list):
            continue
        for b in names:
            b = str(b)
            boards.setdefault(b, []).append((sym, float(pct)))
            member.setdefault(sym, []).append(b)
    if not boards:
        return {}
    rank_of: dict[str, tuple[str, int, int, float]] = {}
    for b, lst in boards.items():
        lst.sort(key=lambda t: t[1], reverse=True)
        total = len(lst)
        for i, (sym, _pct) in enumerate(lst, 1):
            cur = rank_of.get(sym)
            if cur is None or total > cur[2]:
                rank_of[sym] = (b, i, total, lst[0][1])
    out: dict[str, dict] = {}
    for sym in syms:
        hit = rank_of.get(sym)
        if not hit:
            continue
        b, i, total, top_pct = hit
        out[sym] = {"concept": b, "rank": i, "total": total, "pct": None, "top_pct": top_pct}
    return out


def load_market_env(conn: sqlite3.Connection, day: str) -> dict:
    """当日市场环境（**零网络**：本地 daily_kline）—— 普涨还是结构行情。

    为什么必须先说它：普涨日里"次日 ≥7%"的门槛被整体抬高（当天涨太多的票明天难接）；
    结构行情里只有板块龙头的次日才有溢价 ⇒ 这是**全局背景**，决定"今天该不该整体出手"。
    """
    row = conn.execute(
        "SELECT SUM(CASE WHEN percent > 0 THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN percent < 0 THEN 1 ELSE 0 END), count(*), "
        "SUM(CASE WHEN percent >= 19.5 THEN 1 ELSE 0 END) FROM daily_kline WHERE date = ?",
        (day,),
    ).fetchone()
    up, down, total, zt = row or (0, 0, 0, 0)
    env: dict = {"up": up or 0, "down": down or 0, "total": total or 0, "zt": zt or 0}
    env["up_ratio"] = (up or 0) / total * 100 if total else None
    r = env["up_ratio"]
    env["kind"] = (
        "普涨（次日门槛整体抬高）" if (r or 0) >= 70 else ("结构行情（只有龙头有溢价）" if (r or 0) <= 45 else "分化")
    )
    return env


def load_spike_followup(conn: sqlite3.Connection, syms: list[str], day: str, spike: float = 5.0) -> dict[str, dict]:
    """**「大涨次日」模式**（零网络）：该票历史上「当日涨幅 ≥ spike%」的那些日子，次日表现如何。

    为什么单独算：AI 复核里最硬的反证是「当日涨 5~7% → 次日再涨 7%」这个组合在
    这批票的历史里几乎不存在 ⇒ 对每只票单独统计自己的「大涨之后」，比桶级先验更贴。
    """
    if not syms:
        return {}
    ph = ",".join("?" * len(syms))
    rows = conn.execute(
        f"SELECT symbol, next_day_pct FROM recommendations WHERE symbol IN ({ph}) "
        "AND next_day_pct IS NOT NULL AND date < ? AND percent >= ?",
        (*syms, day, spike),
    ).fetchall()
    agg: dict[str, list[float]] = {}
    for sym, ndp in rows:
        agg.setdefault(sym, []).append(ndp)
    return {
        sym: {"n": len(vals), "avg": st.mean(vals), "hit": sum(1 for v in vals if v >= 7), "worst": min(vals)}
        for sym, vals in agg.items()
    }


def fetch_dtb(syms: list[str], workers: int = 6) -> dict[str, list[dict]]:
    """龙虎榜历史（东财，**网络**，1 请求/只）：上榜原因 + 榜单净额 + D1/D2/D5/D10 后续涨幅。

    为什么值得单独取：它是「当日大涨 → 次日分化」最硬的外部证据
    （实测广和通 2025-12-02 当日 +6.24% 上榜后 D1 −7.09%、D10 −20.35%）。
    """
    if not syms:
        return {}
    from scanner.ai_digest.em_data import fetch_dragon_tiger

    def one(sym: str) -> tuple[str, list[dict]]:
        try:
            return sym, fetch_dragon_tiger(sym, 5) or []
        except Exception:  # noqa: BLE001 - 取数旁路：单票失败只少一条证据
            return sym, []

    out: dict[str, list[dict]] = {}
    try:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for sym, rows in ex.map(one, syms):
                if rows:
                    out[sym] = rows
    except Exception as e:  # noqa: BLE001
        print(f"[!] 龙虎榜取数跳过: {type(e).__name__}: {e}")
    return out


def _parse_minute_text(text: str) -> dict:
    """从分时工具的文本里抠出「最新价 / 均价 / 最高 / 最低 / 尾盘量占比」。"""
    import re as _re

    out: dict = {}
    for key, pat in (
        ("last", r"最新\s*([\d.]+)"),
        ("avg", r"均价\s*([\d.]+)"),
        ("high", r"日内最高\s*([\d.]+)"),
        ("low", r"最低\s*([\d.]+)"),
        ("tail_pct", r"尾盘[^量]*量占比\s*([\d.]+)%"),
    ):
        m = _re.search(pat, text)
        if m:
            out[key] = float(m.group(1))
    return out


def fetch_minute_avg(syms: list[str], workers: int = 6) -> dict[str, dict]:
    """分时均价（雪球 minute.json，**网络**，1 请求/只）：收盘 vs 当日均价、尾盘半小时量占比。

    为什么重要：收盘价**在均价上方**= 尾盘有承接；反之 = 冲高回落（AI 复核里
    上能电气就是「唯一收在均价下方」被降级的）。
    """
    if not syms:
        return {}
    from scanner.ai_digest.research import ResearchContext, _tool_get_intraday_minute
    from scanner.db.schema import get_conn

    def one(sym: str) -> tuple[str, dict]:
        try:
            conn = get_conn()
            try:
                text = _tool_get_intraday_minute({"symbol": sym}, ResearchContext(conn=conn))
            finally:
                conn.close()
            return sym, _parse_minute_text(text)
        except Exception:  # noqa: BLE001
            return sym, {}

    out: dict[str, dict] = {}
    try:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for sym, d in ex.map(one, syms):
                if d:
                    out[sym] = d
    except Exception as e:  # noqa: BLE001
        print(f"[!] 分时取数跳过: {type(e).__name__}: {e}")
    return out


def run_ai_review(symbols: list[str], names: dict[str, str]) -> tuple[str, list[str]]:
    """AI 深度复核（**可选档**，默认关）：跑项目现成的 research 工具箱（模型自己查真实数据）。

    耗时 ≈ 60s/只（实测 5 只 294s）⇒ 只对第一/第二梯队的少数票用，别全量。
    返回 (正文, 幻觉巡检未逐字出现的数字)。
    """
    try:
        from dotenv import load_dotenv

        load_dotenv()
        from scanner.ai_digest.agent import run_research
        from scanner.ai_digest.llm import magpie_config
        from scanner.ai_digest.research import ResearchContext
        from scanner.db.schema import get_conn
    except Exception as e:  # noqa: BLE001
        return f"[AI 复核不可用] {type(e).__name__}: {e}", []

    cfg = magpie_config()
    if cfg is None:
        return "[AI 复核跳过] 未配置 MAGPIE_BASE_URL / MAGPIE_API_KEY / MAGPIE_MODEL", []

    conn = get_conn()
    try:
        ctx = ResearchContext(conn=conn)
        res = run_research(
            cfg,
            [{"symbol": s, "name": names.get(s, s)} for s in symbols],
            ctx,
            max_rounds=10,
            max_calls=40,
            origin="用户指定的买入优先级报告重点票",
        )
    except Exception as e:  # noqa: BLE001
        return f"[AI 复核失败] {type(e).__name__}: {e}", []
    finally:
        conn.close()
    try:
        from scanner.ai_digest.material import unverified_numbers

        unverified = unverified_numbers(res.text, res.tool_output_blob)
    except Exception:  # noqa: BLE001
        unverified = []
    return res.text, list(unverified)


def classify_events(ann: list[dict], *, signal_date: str, lookback: int = ANN_LOOKBACK) -> list[str]:
    """公告列表 → 命中的事件类型（**无前视**：只用信号日之前 lookback 天内的公告）。

    窗口 = [signal_date - lookback, signal_date]（含当日）。信号日**之后**的公告一律忽略。
    """
    base = date.fromisoformat(signal_date)
    lo = (base - timedelta(days=lookback)).isoformat()
    hits: list[str] = []
    for item in ann or []:
        d = str(item.get("date") or "")
        if d and not (lo <= d <= signal_date):
            continue
        title = str(item.get("title") or "")
        for kind, kws in ANN_EVENT_KEYWORDS.items():
            if kind not in hits and any(k in title for k in kws):
                hits.append(kind)
    return hits


def mark_text(events: list[str]) -> str:
    """事件类型 → 短标签文本（`⚑减持/质押`）。**跨端/跨模块单源**（报告与审计共用）。"""
    if not events:
        return ""
    short = {
        "减持": "减持",
        "对外担保": "担保",
        "股权激励": "激励",
        "股份质押": "质押",
        "计提减值": "减值",
        "终止投资": "终止",
        "解禁流通": "解禁",
    }
    return "⚑" + "/".join(short.get(e, e) for e in events)


def fetch_ann_marks(syms: list[str], day: str, workers: int = 6) -> dict[str, list[str]]:
    """公告事件标记（**网络**，fail-open；取不到就不标）。"""
    if not syms:
        return {}
    from scanner.ai_digest.em_data import fetch_announcements

    def one(sym: str) -> tuple[str, list[str]]:
        try:
            ann = fetch_announcements(sym, 30) or []
        except Exception:  # noqa: BLE001 - 取数旁路：单票失败只少一个标记
            return sym, []
        return sym, classify_events(ann, signal_date=day)

    out: dict[str, list[str]] = {}
    try:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for sym, hits in ex.map(one, syms):
                if hits:
                    out[sym] = hits
    except Exception as e:  # noqa: BLE001
        print(f"[!] 公告取数跳过: {type(e).__name__}: {e}")
    return out


# ── 分档 ────────────────────────────────────────────────────────────────
def prior_tier(cat: str | None) -> tuple[str, float]:
    hit = CATEGORY_HIT_RATE.get(cat or "", CATEGORY_HIT_RATE_DEFAULT)
    if hit >= TIER_A_MIN:
        return "A", hit
    if hit >= TIER_B_MIN:
        return "B", hit
    return "C", hit


def enrich(
    rows: list[dict],
    *,
    live,
    klines,
    hist,
    appears,
    flow_hist,
    ann_marks,
    sector=None,
    spike=None,
    dtb=None,
    minute=None,
) -> list[dict]:
    for r in rows:
        sym = r["symbol"]
        lv = live.get(sym, {})
        r["price"] = lv.get("price")
        r["pct"] = lv.get("pct") if lv.get("pct") is not None else r.get("db_pct")
        r["main_pct"] = lv.get("main_pct")
        r["main_net"] = lv.get("main_net")
        r["super_net"] = lv.get("super_net")
        r["vol_ratio"] = lv.get("vol_ratio")
        r["turnover"] = lv.get("turnover")
        r["amount"] = lv.get("amount")
        r["float_cap"] = lv.get("float_cap")
        r["prev_close"] = lv.get("prev_close")
        r["rank"] = appears.get(sym)

        bars = klines.get(sym, [])
        closes = [b["close"] for b in bars if b["close"]]
        vols = [b["vol"] for b in bars if b["vol"]]
        ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else None
        r["ma5"] = ma5
        r["above_ma5"] = (r["price"] / ma5 - 1) * 100 if (r["price"] and ma5) else None
        r["accum_hist5"] = sum(b["pct"] or 0 for b in bars[-5:]) if bars else None
        v5 = sum(vols[-5:]) / 5 if len(vols) >= 5 else None
        v10 = sum(vols[-10:-5]) / 5 if len(vols) >= 10 else None
        r["vol_vs_10d"] = (v5 / v10) if (v5 and v10) else None

        h = hist.get(sym, {})
        vals = list(h.values())
        r["hist_n"] = len(vals)
        r["hist_hit"] = sum(1 for v in vals if v >= 7)
        r["hist_rate"] = (r["hist_hit"] / len(vals)) if vals else None
        r["hist_avg"] = st.mean(vals) if vals else None

        fh = flow_hist.get(sym, {})
        seq = list(fh.values())[-FLOW_WINDOW:]
        r["flow_sum"] = sum(seq) if seq else None
        r["flow_pos_days"] = sum(1 for v in seq if v > 0)
        r["flow_days"] = len(seq)
        long_seq = list(fh.values())[-FLOW_LONG_WINDOW:]
        r["flow_sum_long"] = sum(long_seq) if long_seq else None

        r["ann"] = ann_marks.get(sym, [])
        r["sector"] = (sector or {}).get(sym)
        r["spike"] = (spike or {}).get(sym)
        r["dtb"] = (dtb or {}).get(sym)
        r["minute"] = (minute or {}).get(sym)
        tier, hit = prior_tier(r["category"])
        r["prior_tier"], r["prior_pct"] = tier, hit * 100
    return rows


def score_row(r: dict) -> tuple[float, list[str]]:
    """综合证据分 + 分项明细（**每项可核对**，不黑盒）。

    正向分项（合计 105）：先验 25 · 当日资金 20 · 中期资金 20 · 位置 20 · 个股历史 10 · 弹性 10
    惩罚项（最多 −40）：负面公告 / C 档先验 / 个股历史 0 命中 / 双流出 / 换手过热 / 偏离过大 / 量能萎缩…

    ⚠ 先验只是**其中 25 分**，不是主轴：桶级先验在个股身上可能失真（实测瑞丰高材在
    RBD 桶里 16 次兑现 0 次、均值 −3.3%），只拿它当一项证据。
    """
    parts: list[str] = []

    # ① 类别先验（25）
    p1 = min(25.0, (r["prior_pct"] / 100) / 0.179 * 25)
    parts.append(f"先验{p1:.0f}")

    # ② 当日资金（20）：主力净占比分档 + 超大单为正 +3
    mp = r.get("main_pct")
    p2 = 0.0
    if mp is not None:
        if mp >= 10:
            p2 = 20
        elif mp >= 5:
            p2 = 15
        elif mp >= 0:
            p2 = 8
    if (r.get("super_net") or 0) > 0:
        p2 += 3
    p2 = min(20.0, p2)
    parts.append(f"当日{p2:.0f}")

    # ③ 中期资金（20）：10 日累计为正 15 + 净流入天数 ≥6 再 +5
    fs = r.get("flow_sum")
    p3 = 15.0 if (fs is not None and fs > 0) else (5.0 if fs is not None else 8.0)
    if r.get("flow_pos_days", 0) >= 6:
        p3 += 5
    p3 = min(20.0, p3)
    parts.append(f"中期{p3:.0f}")

    # ④ 位置（20）：**不罚当日涨幅** —— 冲高只影响「买点在哪」，不影响「值不值得买」
    #    （涨停/大涨次日继续涨是 A 股常态）；只罚高位偏离与量能萎缩。
    p4 = 20.0
    if (r.get("above_ma5") or 0) > 12:
        p4 -= 5
    if r.get("vol_vs_10d") and r["vol_vs_10d"] < 0.8:
        p4 -= 4
    p4 = max(0.0, p4)
    parts.append(f"位置{p4:.0f}")

    # ⑤ 个股历史兑现（10）：n≥20 全额；n<20 半额（样本小打折）
    n = r.get("hist_n") or 0
    rate = r.get("hist_rate")
    p5 = (rate or 0) * 10 * (1.0 if n >= HIST_TRUST_N else 0.5)
    parts.append(f"历史{p5:.0f}")

    # ⑥ 弹性（10）：市值 ≤300 亿 5 + 价 3~40 元 5
    p6 = 0.0
    if r.get("float_cap") and r["float_cap"] <= HOT_MAX_MARKET_CAP:
        p6 += 5
    if r.get("price") and PRICE_IDEAL[0] <= r["price"] <= PRICE_IDEAL[1]:
        p6 += 5
    parts.append(f"弹性{p6:.0f}")

    # ⑦ 惩罚项（降档真正生效在这里）
    pen = 0.0
    neg = [a for a in r.get("ann", []) if a in ANN_NEGATIVE_VERIFIED]
    unv = [a for a in r.get("ann", []) if a in ANN_UNVERIFIED]
    if neg:
        d = min(20.0, 10 * len(neg))
        pen -= d
        parts.append(f"负面公告-{d:.0f}")
    if unv:
        d = min(4.0, 2 * len(unv))
        pen -= d
    if r["prior_tier"] == "C":
        pen -= 12
        parts.append("先验C档-12")
    if r.get("hist_n") and r["hist_n"] >= HIST_HINT_N and r["hist_hit"] == 0:
        pen -= 8
        parts.append("历史0命中-8")
    if mp is not None and mp < 0 and (r.get("flow_sum") or 0) < 0:
        pen -= 10
        parts.append("双流出-10")
    elif mp is not None and mp < 0:
        pen -= 10
        parts.append("当日主力流出-10")
    if r.get("flow_sum_long") is not None and r["flow_sum_long"] < 0:
        # ⚠ 只在「5 日与 20 日同时为负」时扣分（两个窗口互相印证）；单窗口为负只作提示
        # —— 本地快照口径与东财日频收盘口径有差异，单窗口容易误伤「刚走出来」的票。
        if (r.get("flow_sum") or 0) < 0:
            pen -= 8
            parts.append("长短双失血-8")
        else:
            parts.append("长周期失血(提示)")
    if r.get("amount") and r["amount"] < MIN_AMOUNT:
        pen -= 6
        parts.append("成交额不足-6")
    if r.get("turnover") and r["turnover"] > 25:
        pen -= 8
        parts.append("换手过热-8")
    if (r.get("above_ma5") or 0) > 12:
        pen -= 5
    if r.get("vol_vs_10d") and r["vol_vs_10d"] < 0.8:
        pen -= 4
    if r.get("hist_n", 0) >= HIST_TRUST_N and (r.get("hist_rate") or 0) < 0.05:
        pen -= 6
    if r.get("float_cap") and r["float_cap"] > HOT_MAX_MARKET_CAP:
        pen -= 3
    if r.get("price") and not (PRICE_IDEAL[0] <= r["price"] <= PRICE_IDEAL[1]):
        pen -= 4
        parts.append("价格出弹性带-4")
    # 「大涨次日」模式：该票历史上大涨之后的次日平均是负的 ⇒ 个股层面的硬反证
    sp = r.get("spike")
    if sp and sp["n"] >= 2 and sp["avg"] < 0:
        pen -= 6
        parts.append("大涨后历史为负-6")
    # 分时：收盘在均价**下方** ⇒ 尾盘走弱（冲高回落）
    mn = r.get("minute") or {}
    if mn.get("last") and mn.get("avg") and mn["last"] < mn["avg"]:
        pen -= 6
        parts.append("收盘低于均价-6")
    # 龙虎榜：最近一次「当日大涨上榜」之后 D1 明显跌 ⇒ 外部硬反证
    dtb_rows = r.get("dtb") or []
    if dtb_rows:
        latest = dtb_rows[0]
        try:
            d1 = float(latest.get("d1") or 0)
            chg = float(latest.get("change") or 0)
        except (TypeError, ValueError):
            d1 = chg = 0.0
        if chg >= 5 and d1 <= -5:
            pen -= 8
            parts.append("龙虎榜先例-8")

    r["score"] = round(p1 + p2 + p3 + p4 + p5 + p6 + pen, 1)
    r["score_parts"] = " ".join(parts)
    return r["score"], parts


def classify(rows: list[dict]) -> list[dict]:
    """分档 + 降档说明 + 排序（**报告层规则**，全部写死可查）。"""
    for r in rows:
        veto: list[str] = []
        downgrade: list[str] = []
        hint: list[str] = []

        mp = r.get("main_pct")
        pct = r.get("pct")
        accum_h = r.get("accum_hist5")

        # ── 硬否决（→ 回避）──
        if mp is not None and mp <= FUND_OUTFLOW_NET_PCT:
            veto.append(f"主力净占比 {mp:.1f}% ≤ {FUND_OUTFLOW_NET_PCT}%")
        if pct is not None and pct >= DISPLAY_MAX_TODAY_PCT:
            veto.append(f"当日 +{pct:.1f}% ≥ 追高线 {DISPLAY_MAX_TODAY_PCT}%")
        if accum_h is not None and accum_h >= OVERHEAT_ACCUM_MAX:
            veto.append(f"5 日累计 +{accum_h:.0f}% ≥ 过热 {OVERHEAT_ACCUM_MAX}%")
        if r.get("overbought"):
            veto.append("超买硬信号")

        score_row(r)

        # ── 降档说明（与分数里的惩罚项一一对应）──
        neg = [a for a in r["ann"] if a in ANN_NEGATIVE_VERIFIED]
        if neg:
            downgrade.append("已验证负面公告：" + "/".join(neg))
        if r["prior_tier"] == "C":
            downgrade.append(f"类别先验 {r['prior_pct']:.1f}% < 基线 {CATEGORY_HIT_RATE_DEFAULT * 100:.1f}%")
        if r.get("hist_n") and r["hist_n"] >= HIST_HINT_N and r["hist_hit"] == 0:
            downgrade.append(f"个股历史兑现 0/{r['hist_n']}（均值 {r['hist_avg']:.1f}%）")
        if mp is not None and mp < 0 and (r.get("flow_sum") or 0) < 0:
            downgrade.append("当日与 10 日主力双流出")

        # ── 只提示（不扣分）──
        if r.get("hist_n", 0) >= HIST_TRUST_N and (r.get("hist_rate") or 0) < 0.05:
            hint.append(f"个股历史兑现 {r['hist_hit']}/{r['hist_n']}（{r['hist_rate'] * 100:.0f}%）")
        if r.get("vol_vs_10d") and r["vol_vs_10d"] < 0.8:
            hint.append("量能萎缩（5 日均量 < 前 5 日 8 折）")
        if r.get("flow_sum_long") is not None and r["flow_sum_long"] < 0 and (r.get("flow_sum") or 0) >= 0:
            hint.append("长周期失血（20 日累计为负，但近 5 日已转正）")
        if (r.get("above_ma5") or 0) > 12:
            hint.append(f"偏离 MA5 +{r['above_ma5']:.1f}%")
        if r.get("float_cap") and r["float_cap"] > HOT_MAX_MARKET_CAP:
            hint.append(f"市值 {r['float_cap'] / 1e8:.0f} 亿 > 弹性上限 {HOT_MAX_MARKET_CAP / 1e8:.0f} 亿")
        if r.get("price") and not (PRICE_IDEAL[0] <= r["price"] <= PRICE_IDEAL[1]):
            hint.append(f"价格 {r['price']:.1f} 元在弹性区间外")
        if r.get("turnover") and r["turnover"] > 25:
            hint.append(f"换手 {r['turnover']:.0f}% 过热")

        # ── 档位 = 综合分档 + 两条**封顶**规则 ──
        if veto:
            adj = "X"
        elif r["score"] >= SCORE_T1:
            adj = "A"
        elif r["score"] >= SCORE_T2:
            adj = "B"
        elif r["score"] >= SCORE_T3:
            adj = "C"
        else:
            adj = "D"
        # 封顶①：类别先验 C 档（低于全体基线 7.8%）⇒ 最高第三梯队
        #   （先验是**结构性**事实，不是排序细节；但先验 A/B 不封顶 —— 让资金/位置/个股历史
        #    把 B 档票顶进第一梯队，广和通就是这样上去的）
        if r["prior_tier"] == "C" and adj in ("A", "B"):
            adj = "C"
        # 封顶②：命中**已验证负面**公告（减持/对外担保/股权激励）⇒ 最高第二梯队
        #   （用户 2026-11-05 决策：这类公告的确应该降级；未验证事件不封顶，只提示）
        if neg and adj == "A":
            adj = "B"

        r["veto"], r["downgrade"], r["hint"] = veto, downgrade, hint
        r["adj_tier"] = adj
    return rows


def sort_key(r: dict) -> tuple:
    """排序键：档位 → 综合分 → 当日主力 → 10 日累计 → 榜位 → 市值。"""
    tier_order = {"A": 0, "B": 1, "C": 2, "D": 3, "X": 4}
    return (
        tier_order.get(r["adj_tier"], 9),
        -r.get("score", 0),
        -(r.get("main_pct") or 0),
        -(r.get("flow_sum") or 0),
        r.get("rank") if r.get("rank") else 9999,
        r.get("float_cap") or 1e12,
    )


# ── 渲染 ────────────────────────────────────────────────────────────────
def fmt_money(v) -> str:
    if v is None:
        return "—"
    yi = v / 1e8
    return f"{yi:+.2f}亿" if abs(yi) >= 0.01 else f"{v / 1e4:+.0f}万"


def fmt_pct(v, digits: int = 2) -> str:
    return "—" if v is None else f"{v:+.{digits}f}%"


def action_lines(r: dict) -> tuple[str, str]:
    """成立条件 / 失效信号（规则生成，不用自由发挥）。"""
    price = r.get("price")
    ma5 = r.get("ma5")
    mp = r.get("main_pct")
    pct = r.get("pct") or 0.0
    prev = r.get("prev_close")
    # 「今日涨幅的一半」= 昨收 × (1 + 涨幅/2) —— 用**昨收**算，不是现价
    base = prev or price
    half = base * (1 + pct / 200) if base else None
    if pct >= CHASE_PCT:
        ok = f"回踩不破今日涨幅一半（≈{half:.2f}）且主力继续净流入" if half else "回踩不破今日开盘区且主力续正"
    else:
        ok = f"放量突破今日高点且主力续正（今日主力 {fmt_pct(mp)}）"
    bad = []
    if mp is not None:
        bad.append("主力转净流出")
    if ma5:
        bad.append(f"跌破 MA5 {ma5:.2f}")
    if half and (not price or half < price):
        bad.append(f"跌破 {half:.2f}")
    return ok, " / ".join(bad)


def render(rows: list[dict], *, day: str, snap_ts: str, elapsed: float, n_cands: int, env: dict | None = None) -> str:
    out: list[str] = []
    out.append("=" * 78)
    out.append(f"买入优先级反馈 · {day} · 候选 {n_cands} 只 · 耗时 {elapsed:.1f}s")
    if snap_ts:
        out.append(f"数据时点：行情/资金快照 {snap_ts[:19]}（主循环每轮刷新；快照停更则此值即数据年龄）")
    if env:
        out.append(
            f"市场环境：{env['kind']}｜涨 {env['up']} / 跌 {env['down']}（{env['total']} 只样本）｜涨停 {env['zt']} 家"
        )
    out.append(
        "目标函数：次日 ≥7% hit 率｜综合分 = 先验25 + 当日资金20 + 中期资金20 + 位置20 + 个股历史10 + 弹性10 − 惩罚项"
    )
    out.append("=" * 78)

    groups = {"A": [], "B": [], "C": [], "D": [], "X": []}
    for r in rows:
        groups[r["adj_tier"]].append(r)

    titles = {
        "A": "【第一梯队】证据最齐（买点通常在次日，不在今日）",
        "B": "【第二梯队】有一项硬伤，各看各的",
        "C": "【第三梯队】仅观察，明日不列入首选",
        "D": "【仅观察】分数偏低（列出不展开）",
        "X": "【回避】命中硬门",
    }
    for tier in ("A", "B", "C", "X"):
        g = groups[tier]
        if not g:
            continue
        out.append("")
        out.append(f"{titles[tier]}  {len(g)} 只")
        if tier == "X":
            for r in g:
                out.append(f"  · {r['name']} {r['symbol']}  {' / '.join(r['veto'])}")
            continue
        for i, r in enumerate(g, 1):
            head = (
                f" {i:>2}. {r['name']} {r['symbol']}  {r['price'] if r['price'] else '—'}  "
                f"{fmt_pct(r['pct'])}  {CAT_SHORT.get(r['category'], r['category'])}·{r['prior_pct']:.1f}%"
                f"  评分 {r.get('score', 0):.0f}"
            )
            if r.get("rank"):
                head += f"  榜位{r['rank']}"
            out.append(head)
            if tier in ("A", "B"):
                fs = r.get("flow_sum")
                out.append(
                    f"      资金 主力{fmt_pct(r.get('main_pct'))}"
                    f"（{fmt_money(r.get('main_net'))}｜超大单{fmt_money(r.get('super_net'))}）"
                    f"｜5日{fmt_money(fs)}（{r.get('flow_pos_days', 0)}/{r.get('flow_days', 0)} 天净流入）"
                )
                out.append(
                    f"      位置 MA5{fmt_pct(r.get('above_ma5'), 1)}｜历史5日{fmt_pct(r.get('accum_hist5'), 1)}"
                    f"｜量比{r.get('vol_ratio') if r.get('vol_ratio') is not None else '—'}"
                    f"｜换手{fmt_pct(r.get('turnover'), 1).lstrip('+') if r.get('turnover') is not None else '—'}"
                )
                out.append(
                    f"      历史 个股兑现 {r.get('hist_hit', 0)}/{r.get('hist_n', 0)}"
                    f"（均值{fmt_pct(r.get('hist_avg'), 1)}）｜弹性 "
                    f"{r.get('float_cap', 0) / 1e8:.0f}亿/{r.get('price') or 0:.1f}元"
                    f"｜评分 {r.get('score_parts', '')}"
                )
                neg = [a for a in r["ann"] if a in ANN_NEGATIVE_VERIFIED]
                unv = [a for a in r["ann"] if a in ANN_UNVERIFIED]
                if neg:
                    out.append(f"      📉 已验证负面公告：{'/'.join(neg)}（命中即降档）")
                if unv:
                    out.append(f"      ⚠ 未验证公告：{'/'.join(unv)}（只提示）")
                sec = r.get("sector")
                if sec:
                    pctile = sec["rank"] / sec["total"] * 100 if sec["total"] else 100
                    out.append(
                        f"      板块 {sec['concept']}：当日排第 {sec['rank']}/{sec['total']}"
                        f"（前 {pctile:.0f}%｜领涨 {sec['top_pct']:+.2f}%）"
                        + ("  ← 板块前列，跟随龙头" if pctile <= 33 else "")
                        + ("  ← 板块靠后，**别当主线**" if pctile > 67 else "")
                    )
                else:
                    out.append("      板块 本地无当日数据（查不到 ≠ 掉队）")
                sp = r.get("spike")
                if sp:
                    out.append(
                        f"      大涨之后 历史{sp['n']}次：次日均值{sp['avg']:+.2f}%"
                        f"（≥7% {sp['hit']} 次｜最差 {sp['worst']:+.2f}%）"
                    )
                mn = r.get("minute") or {}
                if mn.get("last") and mn.get("avg"):
                    rel = "收在均价**上方**" if mn["last"] >= mn["avg"] else "收在均价**下方**（尾盘走弱）"
                    out.append(
                        f"      分时 最新{mn['last']:.2f} / 均价{mn['avg']:.2f} → {rel}"
                        + (f"｜尾盘量占比 {mn['tail_pct']:.1f}%" if mn.get("tail_pct") is not None else "")
                    )
                dtb_rows = r.get("dtb") or []
                if dtb_rows:
                    lt = dtb_rows[0]
                    try:
                        out.append(
                            f"      龙虎榜 最近一次 {lt.get('date')}：当日{float(lt.get('change') or 0):+.2f}%"
                            f" → D1 {float(lt.get('d1') or 0):+.2f}% / D5 {float(lt.get('d5') or 0):+.2f}%"
                            f"（上榜{len(dtb_rows)}次）"
                        )
                    except (TypeError, ValueError):
                        pass
                ok, bad = action_lines(r)
                out.append(f"      ✅ 成立：{ok}")
                out.append(f"      ❌ 失效：{bad}")
            for d in r["downgrade"]:
                out.append(f"      ▼ 降档：{d}")
            for h in r["hint"]:
                out.append(f"      ⚠ {h}")
            for v in r["veto"]:
                out.append(f"      ⛔ {v}")
    if groups["D"]:
        out.append("")
        out.append(f"{titles['D']}  {len(groups['D'])} 只")
        out.append("  " + "、".join(f"{r['name']}({r.get('score', 0):.0f}分)" for r in groups["D"]))

    # 明日动作
    top = [r for r in rows if r["adj_tier"] == "A"] + [r for r in rows if r["adj_tier"] == "B"][:2]
    out.append("")
    out.append("── 明日动作 " + "─" * 62)
    if not top:
        out.append(" 1) 今日无第一/第二梯队 ⇒ 明日不新开仓（等信号，不等价格）")
    else:
        for i, r in enumerate(top[:4], 1):
            pct = r.get("pct") or 0
            tail = "（今日已冲高，不追，等回踩）" if pct >= CHASE_PCT else "（今日价格尚可）"
            ok, bad = action_lines(r)
            out.append(f" {i}) {r['name']} {r['symbol']}：{tail}")
            sec = r.get("sector")
            if sec:
                pctile = sec["rank"] / sec["total"] * 100 if sec["total"] else 100
                tag = "板块前列（可跟随）" if pctile <= 33 else ("板块居中" if pctile <= 67 else "板块靠后（别当主线）")
                out.append(f"    板块 {sec['concept']} 当日第 {sec['rank']}/{sec['total']}（{tag}）")
            out.append(f"    成立 {ok}｜失效 {bad}")
        n_rest = len([r for r in rows if r["adj_tier"] in ("C", "X")])
        out.append(f" 其余 {n_rest} 只：先验/资金/位置至少一项不过关 ⇒ 明日不列入首选")
    out.append("")
    out.append("── 口径与已知缺口 " + "─" * 58)
    out.append(f" 候选池 = 当日 recommendations 去退池类别（{n_cands} 只）＝终端 v1 池选的**超集**")
    out.append("          （终端另按资金流出 ≤-8% / 当日涨幅 ≥8% / 减仓纪律标签过滤，本报告把它们放进「回避」）")
    out.append(
        " 资金累计 = 本地 market_extra_cache 的每日快照（与东财日频收盘口径有差异；长周期失血需 5/20 日双负才扣分）"
    )
    out.append(" 板块排位 = 本地 concept_cache × 当日涨幅（**零网络**）；⚠ 未做样本外检验 ⇒ 只作提示，不进分数")
    out.append(" 公告只对排序靠前的 N 只拉取（默认 12）⇒ 第一/二梯队以外可能未标；未标 ≠ 没有")
    out.append(
        " 龙虎榜默认对前 4 只取（东财，+2.6s）；分时均价 `--minute N`（雪球，+18s）；AI 深度复核 `--ai N`（≈60s/只）"
    )
    out.append(" ⚠ 未纳入：东财日频资金流（fflow）—— 比本地快照准，但每只 1 请求")
    out.append("=" * 78)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="盘中买入优先级反馈报告（只读库，不改任何生产逻辑）")
    ap.add_argument("--date", default="", help="信号日（默认今天）")
    ap.add_argument("--symbols", default="", help="指定清单（逗号分隔），默认当日推荐池")
    ap.add_argument("--out", default="", help="报告落盘路径")
    ap.add_argument("--no-ann", action="store_true", help="不拉公告（最快）")
    ap.add_argument("--ann-top", type=int, default=12, help="只对排序靠前的 N 只拉公告")
    ap.add_argument("--ai", type=int, default=0, help="对第一/第二梯队前 N 只跑 AI 深度复核（≈60s/只，慢档）")
    ap.add_argument("--dtb", type=int, default=4, help="对第一/第二梯队前 N 只取龙虎榜（东财 1 请求/只，+2.6s）")
    ap.add_argument("--minute", type=int, default=0, help="对第一/第二梯队前 N 只取分时均价（雪球 1 请求/只）")
    ap.add_argument("--top", type=int, default=0, help="只输出前 N 只（0=全部）")
    ap.add_argument("--db", default="", help="DB 路径（默认仓库根 scanner.db）")
    args = ap.parse_args(argv)

    if sys.platform == "win32":
        rec = getattr(sys.stdout, "reconfigure", None)
        if callable(rec):
            rec(encoding="utf-8")

    t0 = time.time()
    day = args.date or date.today().isoformat()
    db_path = args.db or str(ROOT / "scanner.db")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    if args.symbols:
        syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        ph = ",".join("?" * len(syms))
        rows = [
            {
                "symbol": s,
                "name": s,
                "category": "",
                "score": 0,
                "db_pct": None,
                "concept": None,
                "accum_db": None,
                "overbought": False,
            }
            for s in syms
        ]
        cats = {
            r[0]: (r[1], r[2], r[3])
            for r in conn.execute(
                f"SELECT symbol, name, category, score FROM recommendations "
                f"WHERE symbol IN ({ph}) AND date = ? ORDER BY id",
                (*syms, day),
            ).fetchall()
        }
        for r in rows:
            if r["symbol"] in cats:
                r["name"], r["category"], r["score"] = cats[r["symbol"]]
    else:
        rows = load_candidates(conn, day)
    if not rows:
        print(f"[!] {day} 没有可分析的候选（当日 recommendations 为空？）")
        return 1

    syms = [r["symbol"] for r in rows]
    live, snap_ts = load_live(conn, day)
    klines = load_klines(conn, syms, day)
    hist = load_history(conn, day)
    appears = load_appearances(conn, day)
    flow_hist = load_flow_history(conn, syms, day)
    sector = load_sector_rank(conn, syms, day)  # 零网络：板块排位
    env = load_market_env(conn, day)  # 零网络：市场环境
    spike = load_spike_followup(conn, syms, day)  # 零网络：个股「大涨之后」模式
    conn.close()

    # 先算一次排序（为了只对靠前的 N 只拉公告）
    pre = enrich(
        [dict(r) for r in rows],
        live=live,
        klines=klines,
        hist=hist,
        appears=appears,
        flow_hist=flow_hist,
        ann_marks={},
        sector=sector,
        spike=spike,
    )
    pre = classify(pre)
    pre.sort(key=sort_key)
    ann_syms = [r["symbol"] for r in pre[: args.ann_top]] if not args.no_ann else []
    ann_marks = fetch_ann_marks(ann_syms, day) if ann_syms else {}

    # 可选联网档：龙虎榜 / 分时均价 —— 只对第一/第二梯队前 N 只
    focus = [r["symbol"] for r in pre if r["adj_tier"] in ("A", "B")]
    dtb_map: dict[str, list[dict]] = {}
    minute_map: dict[str, dict] = {}
    if args.dtb > 0:
        dtb_map = fetch_dtb(focus[: args.dtb])
    if args.minute > 0:
        minute_map = fetch_minute_avg(focus[: args.minute])

    rows2 = enrich(
        [dict(r) for r in rows],
        live=live,
        klines=klines,
        hist=hist,
        appears=appears,
        flow_hist=flow_hist,
        ann_marks=ann_marks,
        sector=sector,
        spike=spike,
        dtb=dtb_map,
        minute=minute_map,
    )
    rows2 = classify(rows2)
    rows2.sort(key=sort_key)
    if args.top:
        rows2 = rows2[: args.top]

    text = render(rows2, day=day, snap_ts=snap_ts, elapsed=time.time() - t0, n_cands=len(rows), env=env)

    # ── 可选：AI 深度复核（慢档，≈60s/只）──
    if args.ai > 0:
        picks = [r for r in rows2 if r["adj_tier"] in ("A", "B")][: args.ai]
        if picks:
            print(f"[i] AI 深度复核 {len(picks)} 只（≈60s/只，慢档）…", file=sys.stderr)
            names = {r["symbol"]: r["name"] for r in picks}
            ai_text, unverified = run_ai_review([r["symbol"] for r in picks], names)
            text += "\n" + "─" * 78 + "\n"
            text += "【AI 深度复核】（模型自己查真实数据后的主观判断，含幻觉巡检）\n"
            text += ai_text + "\n"
            if unverified:
                text += f"[!] 幻觉巡检：以下数字未在工具输出中逐字出现 → {'、'.join(unverified)}\n"
            else:
                text += "[幻觉巡检] 通过（正文数字均可在工具输出中逐字找到）\n"

    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8-sig")
        print(f"\n[i] 报告已存 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
