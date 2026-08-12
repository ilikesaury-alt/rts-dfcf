import argparse
import json
import os
import re
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from scanner.config import DB_PATH, now_beijing  # noqa: E402  (BASE_DIR 路径注入后导入)
from scanner.database import get_cached_kline, get_consecutive_appearance_days  # noqa: E402
from scanner.indicators import (  # noqa: E402
    compute_adx,
    compute_bollinger_bands,
    compute_kdj,
    compute_macd,
    compute_rsi,
)

SEP = "━" * 55
SUB_SEP = "─" * 55


def find_stock(conn: sqlite3.Connection, query: str) -> list[dict]:
    query = query.strip().upper().replace("SZ", "").replace("SH", "").replace("BJ", "")
    results = []
    if re.match(r"^\d{6}$", query):
        # LIMIT 须作用于每个子查询，否则 UNION 后整体截断会漏匹配
        rows = conn.execute(
            "SELECT * FROM (SELECT DISTINCT symbol, name FROM appearances WHERE symbol LIKE ? LIMIT 1) "
            "UNION "
            "SELECT * FROM (SELECT DISTINCT symbol, name FROM recommendations WHERE symbol LIKE ? LIMIT 1)",
            (f"%{query}", f"%{query}"),
        ).fetchall()
        for r in rows:
            results.append({"symbol": r[0], "name": r[1]})
    else:
        rows = conn.execute(
            "SELECT * FROM (SELECT DISTINCT symbol, name FROM appearances WHERE name LIKE ? LIMIT 10) "
            "UNION "
            "SELECT * FROM (SELECT DISTINCT symbol, name FROM recommendations WHERE name LIKE ? LIMIT 10)",
            (f"%{query}%", f"%{query}%"),
        ).fetchall()
        seen = set()
        for r in rows:
            if r[0] not in seen:
                seen.add(r[0])
                results.append({"symbol": r[0], "name": r[1]})
    return results


def get_sector(conn: sqlite3.Connection, symbol: str) -> str:
    row = conn.execute("SELECT sector FROM sector_cache WHERE symbol = ?", (symbol,)).fetchone()
    return row[0] if row else ""


def get_recommendations(conn: sqlite3.Connection, symbol: str) -> list[dict]:
    rows = conn.execute(
        "SELECT date, time, category, score, percent, trend, score_breakdown, source "
        "FROM recommendations WHERE symbol = ? ORDER BY date DESC, time DESC",
        (symbol,),
    ).fetchall()
    return [
        {
            "date": r[0], "time": r[1], "category": r[2], "score": r[3],
            "percent": r[4], "trend": r[5], "breakdown": r[6], "source": r[7],
        }
        for r in rows
    ]


def get_appearances(conn: sqlite3.Connection, symbol: str) -> list[dict]:
    rows = conn.execute(
        "SELECT date, rank, percent, value FROM appearances WHERE symbol = ? ORDER BY date ASC",
        (symbol,),
    ).fetchall()
    return [{"date": r[0], "rank": r[1], "percent": r[2], "value": r[3]} for r in rows]


def format_value(v: float) -> str:
    if v >= 100_000_000:
        return f"{v / 100_000_000:.1f}亿"
    if v >= 10_000:
        return f"{v / 10_000:.1f}万"
    return f"{v:.0f}"


def sparkline(values: list[float], width: int = 20) -> str:
    if not values:
        return ""
    mn, mx = min(values), max(values)
    rng = mx - mn if mx != mn else 1
    bars = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
    n = len(values)
    step = max(1, n // width)
    sampled = values[::step][:width]
    line = "".join(bars[min(7, int((v - mn) / rng * 7))] for v in sampled)
    return f"{line}  {mn:.1f}~{mx:.1f}"


def trend_arrow(change: float) -> str:
    if change > 2:
        return "↑↑"
    if change > 0.5:
        return "↑"
    if change > -0.5:
        return "→"
    if change > -2:
        return "↓"
    return "↓↓"


def color(text: str, color_code: str) -> str:
    return f"{color_code}{text}\033[0m"


def red(text: str) -> str:
    return color(text, "\033[91m")


def green(text: str) -> str:
    return color(text, "\033[92m")


def yellow(text: str) -> str:
    return color(text, "\033[93m")


def blue(text: str) -> str:
    return color(text, "\033[94m")


def bold(text: str) -> str:
    return color(text, "\033[1m")


def print_section(title: str):
    print(f"\n{SEP}")
    print(f"  {bold(title)}")
    print(SUB_SEP)


def safe_round(v, digits=2):
    if v is None:
        return "N/A"
    return round(v, digits)


def main():
    parser = argparse.ArgumentParser(description="个股深度分析报告")
    parser.add_argument("query", help="股票代码(300319)或名称(麦捷科技)")
    parser.add_argument("--quick", action="store_true", help="快速模式: 仅本地数据,不调API")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    try:
        stocks = find_stock(conn, args.query)
        if not stocks:
            print(f"\n{red('[!] 未找到匹配的股票:')} {args.query}")
            print("   提示: 试试输入完整股票代码(300319)或中文名称")
            sys.exit(1)

        if len(stocks) > 1:
            print(f"\n{yellow('找到多个匹配:')}")
            for i, s in enumerate(stocks, 1):
                print(f"  {i}. {s['name']} ({s['symbol']})")
            print("   请使用更精确的名称重试")
            sys.exit(1)

        stock = stocks[0]
        symbol = stock["symbol"]
        name = stock["name"]

        appearances = get_appearances(conn, symbol)
        kline_data = get_cached_kline(conn, symbol)
        recs = get_recommendations(conn, symbol)
        sector = get_sector(conn, symbol)
        consecutive_days = get_consecutive_appearance_days(conn, symbol)

        live_data = {}
        if not args.quick and appearances:
            from scanner.api import fetch_market_caps_batch, make_session
            session = make_session()
            try:
                caps = fetch_market_caps_batch(session, [symbol])
                if symbol in caps:
                    live_data = caps[symbol]
            except Exception as e:
                print(f"  [警告] 获取实时数据失败: {e}")
            finally:
                session.close()
    except SystemExit:
        raise
    except Exception:
        # 任何意外异常都需关闭 conn 后再抛出，避免连接泄漏
        conn.close()
        raise

    # 报告生成全程纳入 try/finally，确保异常路径也关闭 conn
    try:
        # 以下是正常路径；若执行到末尾 finally 会关闭 conn。

        now = now_beijing()
        report_date = now.strftime("%Y-%m-%d %H:%M")

        closes = [k["close"] for k in (kline_data or [])]
        highs = [k["high"] for k in (kline_data or [])]
        lows = [k["low"] for k in (kline_data or [])]

        # 技术指标默认值：当 K 线数据不足（<5 天）时第 3 节不会赋值，
        # 第 7 节综合评价引用时需为 None，否则触发 NameError。
        rsi_val = None
        kdj_val = None
        macd_val = None
        boll_val = None
        adx_val = None
        volumes = [k["volume"] for k in (kline_data or [])]
        pcts = [k["percent"] for k in (kline_data or [])]

        print(f"\n\033[1;36m{'=' * 55}\033[0m")
        print(f"\033[1;36m  {name} ({symbol}) 深度分析报告\033[0m")
        print(f"\033[1;36m  {report_date}\033[0m")
        if args.quick:
            print("\033[1;36m  [快速模式 - 仅本地数据]\033[0m")
        print(f"\033[1;36m{'=' * 55}\033[0m")

        # ── Section 1: 基本信息 ──
        print_section("1. 基本信息")
        current_price = live_data.get("current", closes[-1] if closes else "N/A")
        live_pct = live_data.get("percent")
        mc = live_data.get("market_cap", 0)
        cmc = live_data.get("circ_market_cap", 0)
        tr = live_data.get("turnover_rate")
        first_app = appearances[0] if appearances else None

        print(f"  代码: {symbol}")
        print(f"  名称: {name}")
        if isinstance(current_price, (int, float)):
            # live_pct == 0.0 是有效值（未涨），不能当 falsy 处理；None 才表示数据缺失
            if live_pct is not None:
                if live_pct > 0:
                    pct_str = f"  ({green(f'+{live_pct:.2f}%')})"
                elif live_pct < 0:
                    pct_str = f"  ({red(f'{live_pct:.2f}%')})"
                else:
                    pct_str = f"  ({live_pct:.2f}%)"
            else:
                pct_str = ""
            print(f"  最新价: {current_price:.2f}  {pct_str}")
        if mc:
            print(f"  总市值: {format_value(mc)}")
        if cmc:
            print(f"  流通市值: {format_value(cmc)}")
        if tr:
            print(f"  换手率: {tr:.2f}%")
        if sector:
            print(f"  所属板块: {sector}")
        # 基本面风险（财务风险，stock_report 无 --quick 依赖 pywencai 也不强制：
        # 读 DB 当日缓存，无数据则尝试拉取）。资不抵债=退市风险级，醒目警示。
        try:
            from scanner.fundamentals import collect_fund_risk, get_fund_risk_from_db
            reason = get_fund_risk_from_db(conn, symbol)
            if reason is None and not args.quick:
                reason = collect_fund_risk(conn, [symbol]).get(symbol)
            if reason:
                print(f"  {red(f'⚠ 财务风险: {reason}（每股净资产<0，退市风险级，扫描器已排除）')}")
        except Exception:
            pass
        if first_app:
            print(f"  首次上榜: {first_app['date']} (排名{first_app['rank']}, 涨幅{(first_app['percent'] or 0):.2f}%)")
        print(f"  累计上榜: {len(appearances)}天")
        if consecutive_days:
            print(f"  连续上榜: {consecutive_days}天" if consecutive_days > 0 else "  连续上榜: 0天(今日未上榜)")

        # ── Section 2: 上榜轨迹 ──
        print_section("2. 上榜轨迹")
        if appearances:
            ranks = [a["rank"] for a in appearances]
            width = min(30, len(appearances))
            if len(ranks) > 1:
                r_max, r_min = max(ranks), min(ranks)
                r_rng = r_max - r_min if r_max != r_min else 1
                bars = ["█", "▇", "▆", "▅", "▄", "▃", "▂", "▁"]
                step = max(1, len(ranks) // width)
                sampled = ranks[::step][:width]
                rank_line = "".join(
                    bars[min(7, int((r - r_min) / r_rng * 7))] for r in sampled
                )
                print(f"  排名轨迹(高=█ 低=▁): {r_min}~{r_max}")
                print(f"  {rank_line}")

            recent = appearances[-15:]
            print(f"  {'日期':<8} {'排名':>4} {'涨幅':>8} {'走势':>4}")
            last_pct = None
            for a in recent:
                pct = a["percent"] or 0
                arrow = trend_arrow(pct - last_pct) if last_pct is not None else ""
                pct_display = green(f"+{pct:.2f}%") if pct > 0 else red(f"{pct:.2f}%")
                print(f"  {a['date'][5:]:<8} {a['rank']:>4}  {pct_display:>10}  {arrow}")
                last_pct = pct
        else:
            print(f"  {yellow('(无上榜记录)')}")

        # ── Section 3: K线与技术面 ──
        print_section("3. K线与技术面")
        if kline_data and len(closes) >= 5:
            recent_k = kline_data[-20:]
            print("  近20日K线(收盘价):")
            print(f"  {sparkline(closes[-40:], 30)}")
            print(f"  {'日期':<8} {'收盘':>8} {'涨幅':>8} {'量':>12}")
            for k in reversed(recent_k):
                p = k["percent"] or 0
                pct_display = green(f"+{p:.2f}%") if p > 0 else red(f"{p:.2f}%")
                print(f"  {k['date'][5:]:<8} {k['close']:>8.2f}  {pct_display:>10}  {format_value(k['volume']):>12}")

            # Moving averages
            if len(closes) >= 20:
                ma5 = sum(closes[-5:]) / 5
                ma10 = sum(closes[-10:]) / 10
                ma20 = sum(closes[-20:]) / 20
                last_close = closes[-1]
                print("\n  均线位置:")
                ma5_pos = ('(↑ 股价在其' + ('上方' if last_close > ma5 else '下方') + ')'
                           if ma5 and abs(last_close - ma5) / ma5 < 0.05 else '')
                print(f"  MA5  = {ma5:.2f}  {ma5_pos}")
                print(f"  MA10 = {ma10:.2f}")
                # MA20 趋势需对比"前一日 MA20"，要求至少 21 根 K 线才能取到完整 20 元素窗口
                ma20_trend = ""
                if len(closes) >= 21:
                    prev_ma20 = sum(closes[-21:-1]) / 20
                    ma20_trend = "  (↑ 趋势向上)" if ma20 > prev_ma20 else "  (↓ 趋势向下)"
                print(f"  MA20 = {ma20:.2f}{ma20_trend}")

            # Technical indicators
            rsi_val = compute_rsi(closes)
            kdj_val = compute_kdj(highs, lows, closes)
            macd_val = compute_macd(closes)
            boll_val = compute_bollinger_bands(closes)
            adx_val = compute_adx(highs, lows, closes)

            print("\n  技术指标:")
            if rsi_val is not None:
                rsi_str = green(f"RSI(14) = {rsi_val:.1f}") if rsi_val > 50 else red(f"RSI(14) = {rsi_val:.1f}")
                if rsi_val > 70:
                    rsi_str += yellow(" ⚠超买区")
                elif rsi_val < 30:
                    rsi_str += green(" ✓超卖区")
                print(f"  {rsi_str}")

            if kdj_val:
                k, d, j = kdj_val["K"], kdj_val["D"], kdj_val["J"]
                kdj_str = (green(f"KDJ K={k:.1f} D={d:.1f} J={j:.1f}") if j > k
                           else red(f"KDJ K={k:.1f} D={d:.1f} J={j:.1f}"))
                if j > 100:
                    kdj_str += yellow(" ⚠J值超买")
                elif j < 0:
                    kdj_str += green(" ✓J值超卖")
                if j > k:
                    kdj_str += " ↑金叉" if k > d else ""
                print(f"  {kdj_str}")

            if macd_val:
                hist = macd_val["histogram"]
                hist_prev = macd_val.get("histogram_prev", 0)
                macd_str = f"MACD={macd_val['macd']:.4f}  SIGNAL={macd_val['signal']:.4f}  HIST={hist:.4f}"
                if hist > 0 and hist > hist_prev:
                    print(f"  {green(macd_str)} ↑红柱扩大")
                elif hist > 0:
                    print(f"  {yellow(macd_str)} 红柱缩小")
                elif hist < 0 and hist < hist_prev:
                    print(f"  {red(macd_str)} ↓绿柱扩大")
                else:
                    print(f"  {red(macd_str)} 绿柱缩小")

            if boll_val:
                b_pct = boll_val["b_pct"]
                bw = boll_val["bandwidth"]
                boll_str = f"BOLL中轨={boll_val['middle']:.2f} 带宽={bw:.3f}  %B={b_pct:.2f}"
                if b_pct > 0.8:
                    print(f"  {yellow(boll_str)} ⚠近上轨")
                elif b_pct < 0.2:
                    print(f"  {green(boll_str)} ✓近下轨")
                else:
                    print(f"  {boll_str}")

            if adx_val:
                adx_str = f"ADX={adx_val['adx']:.1f}  +DI={adx_val['plus_di']:.1f}  -DI={adx_val['minus_di']:.1f}"
                if adx_val["adx"] > 25:
                    adx_str += " 趋势强"
                elif adx_val["adx"] < 20:
                    adx_str += " 趋势弱"
                if adx_val["plus_di"] > adx_val["minus_di"]:
                    print(f"  {green(adx_str)} ↑多头")
                else:
                    print(f"  {red(adx_str)} ↓空头")

            # Accumulated return analysis
            if len(pcts) >= 5:
                accum_5 = sum(pcts[-5:])
                accum_10 = sum(pcts[-10:]) if len(pcts) >= 10 else 0
                accum_20 = sum(pcts[-20:]) if len(pcts) >= 20 else 0
                print(f"\n  累计涨跌幅: 5日={accum_5:+.2f}%  10日={accum_10:+.2f}%  20日={accum_20:+.2f}%")

            # Volume analysis
            if len(volumes) >= 20:
                avg_vol = sum(volumes[-20:]) / 20
                recent_vol = sum(volumes[-5:]) / 5
                vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1
                vol_str = f"近5日均量/近20日均量: {vol_ratio:.2f}x"
                if vol_ratio > 1.5:
                    print(f"  {yellow(vol_str)} ⚠放量")
                elif vol_ratio < 0.7:
                    print(f"  {green(vol_str)} ✓缩量(回踩特征)")
                else:
                    print(f"  {vol_str}")
        else:
            print(f"  {yellow('(K线数据不足)')}")

        # ── Section 4: 推荐历史 ──
        print_section("4. 扫描器推荐历史")
        if recs:
            categories = [r["category"] for r in recs[:20]]
            scores = [r["score"] for r in recs[:20]]
            if scores:
                from collections import Counter
                cat_counts = Counter(categories)
                cat_summary = " | ".join(f"{k}: {v}次" for k, v in sorted(cat_counts.items(), key=lambda x: -x[1]))
                print(f"  策略分布: {cat_summary}")
            print(f"\n  {'日期':<8}  {'策略':<16}  {'评分':>3}  {'涨幅':>7}  {'趋势':<10}  来源")
            seen_in_day = set()
            for r in recs[:20]:
                if r["date"] not in seen_in_day:
                    seen_in_day.add(r["date"])
                if r["percent"] and r["percent"] > 0:
                    pct_display = green(f"+{r['percent']:.2f}%")
                elif r["percent"]:
                    pct_display = red(f"{r['percent']:.2f}%")
                else:
                    pct_display = "N/A"
                print(f"  {r['date'][5:]:<8}  {r['category']:<16}  {r['score']:>3}  "
                      f"{str(pct_display):>10}  {r['trend']:<10}  {r['source']}")

            last_rec = recs[0]
            if last_rec.get("breakdown"):
                try:
                    bd = json.loads(last_rec["breakdown"])
                    fatigue_items = {k: v for k, v in bd.items() if "fatigue" in k.lower()}
                    validation_items = {k: v for k, v in bd.items() if "validation" in k.lower() or "v_" in k.lower()}
                    list_items = {k: v for k, v in bd.items() if "list_" in k.lower()}
                    if fatigue_items:
                        print(f"\n  最新评分-疲劳相关: {json.dumps(fatigue_items, ensure_ascii=False)}")
                    if validation_items:
                        print(f"  最新评分-验证分: {json.dumps(validation_items, ensure_ascii=False)}")
                    print(f"  最新评分-榜单相关: {json.dumps(list_items, ensure_ascii=False)}")
                except (json.JSONDecodeError, TypeError):
                    pass
        else:
            print(f"  {yellow('(无推荐记录)')}")

        # ── Section 5: 量价结构 ──
        print_section("5. 量价结构")
        if len(closes) >= 2:
            high_ever = max(closes)
            low_ever = min(closes)
            current = closes[-1]
            pct_from_high = (current - high_ever) / max(high_ever, 0.01) * 100
            if high_ever > low_ever:
                print(f"  区间最高: {high_ever:.2f}")
                print(f"  区间最低: {low_ever:.2f}")
                retrace_str = (green(f"距最高: {pct_from_high:+.2f}%") if pct_from_high > -5
                               else red(f"距最高: {pct_from_high:+.2f}%"))
                print(f"  当前价:  {current:.2f}  ({retrace_str})")
                print(f"  区间涨幅: {(high_ever - low_ever) / low_ever * 100:.1f}%")

        if appearances:
            pct_values = [a["percent"] or 0 for a in appearances]
            if pct_values:
                avg_pct = sum(pct_values) / len(pct_values)
                max_pct = max(pct_values)
                min_pct = min(pct_values)
                print(f"\n  上榜日均涨幅: {avg_pct:.2f}%")
                print(f"  上榜单日最大涨幅: {green(f'+{max_pct:.2f}%')}")
                print(f"  上榜单日最大跌幅: {red(f'{min_pct:.2f}%')}")

            ranks = [a["rank"] for a in appearances]
            if ranks:
                avg_rank = sum(ranks) / len(ranks)
                print(f"  平均排名: {avg_rank:.0f}")

        # 行情增强（涨停池/资金流）：只读 market_extra_cache，不发起网络请求
        try:
            from scanner.database import get_market_extra_cache
            extra_lines = []
            zt = get_market_extra_cache(conn, [symbol], "zt_pool").get(symbol)
            ff = get_market_extra_cache(conn, [symbol], "fund_flow").get(symbol)
            if zt:
                extra_lines.append(
                    f"涨停池: 连板{zt.get('lianban', 0)} 统计{zt.get('zt_stat', '-')} "
                    f"炸板{zt.get('zhaban', 0)} 行业{zt.get('industry', '-')}")
            if ff:
                main_net = ff.get("main_net", 0) or 0
                main_pct = ff.get("main_pct", 0) or 0
                super_net = ff.get("super_net", 0) or 0
                cfn = green if main_net >= 0 else red
                extra_lines.append(
                    f"主力净流入: {cfn(f'{main_net/1e8:+.2f}亿 ({main_pct:+.2f}%)')} | "
                    f"超大单: {cfn(f'{super_net/1e8:+.2f}亿')}")
            if extra_lines:
                print("\n  行情增强:")
                for line in extra_lines:
                    print(f"    {line}")
        except Exception:
            pass

        # ── Section 6: 疲劳与风险 ──
        print_section("6. 疲劳与风险")
        fatigue_signals = []
        if consecutive_days >= 3:
            fatigue_signals.append(f"连续上榜{consecutive_days}天 ⚠️")
        if recs:
            for r in recs[:5]:
                if r["trend"] and ("涨多" in str(r["trend"]) or "⚠" in str(r["trend"])):
                    fatigue_signals.append(f"系统标记: {r['date']} {r['trend']}")
                    break
            if recs:
                last_trend = recs[0].get("trend", "")
                if "回踩" in str(last_trend):
                    fatigue_signals.append(f"当前趋势: {last_trend} (回调确认中)")
                elif "企稳" in str(last_trend):
                    fatigue_signals.append(f"当前趋势: {last_trend} (可能止跌)")

        if fatigue_signals:
            for sig in fatigue_signals:
                print(f"  {yellow(sig)}")
        else:
            print(f"  {green('✓ 无明显疲劳信号')}")

        if len(pcts) >= 5:
            accum_5 = sum(pcts[-5:])
            if accum_5 < -10:
                print(f"  {red(f'近5日累计跌幅 {accum_5:.1f}% ⚠️ 短期超跌')}")
            elif accum_5 > 20:
                print(f"  {yellow(f'近5日累计涨幅 {accum_5:.1f}% ⚠️ 短期过热')}")

        if not args.quick:
            print(f"\n  {blue('[提示]')} 输入 /stock-report {args.query} 可获取含网络资讯的完整报告")

        # ── Section 7: 综合评价 ──
        print_section("7. 综合评价")

        if not appearances:
            print(f"  {yellow('该股票未进入过扫描器榜单，暂无数据')}")
        else:
            strengths = []
            weaknesses = []

            if consecutive_days >= 5:
                strengths.append("连续上榜5天+，市场关注度高")
            elif consecutive_days >= 3:
                strengths.append("连续上榜，有持续性")

            if boll_val and boll_val["b_pct"] < 0.3:
                strengths.append("BOLL接近下轨，存在技术反弹空间")
            elif boll_val and boll_val["b_pct"] > 0.8:
                weaknesses.append("BOLL接近上轨，短期追高风险大")

            if rsi_val and rsi_val > 70:
                weaknesses.append("RSI超买，短期可能回调")
            elif rsi_val and rsi_val < 30:
                strengths.append("RSI超卖，技术反弹概率大")

            if len(pcts) >= 5:
                accum_5 = sum(pcts[-5:])
                if accum_5 < -5:
                    weaknesses.append(f"近5日跌幅{accum_5:.1f}%，处于调整期")

            if recs:
                last_cat = recs[0].get("category", "")
                if "pullback" in last_cat:
                    strengths.append("系统当前以'回踩'策略推荐，博弈回调后的反弹")
                elif "new_face" in last_cat:
                    strengths.append("系统当前以'新面孔'策略推荐，关注底部启动")
                elif "momentum" in last_cat:
                    weaknesses.append("系统以'动量'策略推荐，需警惕追高风险")

            last_score = recs[0].get("score", 0) if recs else 0
            if last_score >= 80:
                strengths.append(f"系统评分{last_score}，高分信号")
            elif last_score <= 30:
                weaknesses.append(f"系统评分{last_score}，信号较弱")

            if strengths:
                print(f"  {green('✓ 积极因素:')}")
                for s in strengths:
                    print(f"    • {s}")
            if weaknesses:
                print(f"  {red('✗ 风险因素:')}")
                for w in weaknesses:
                    print(f"    • {w}")

            if not strengths and not weaknesses:
                print(f"  {yellow('信号中性，缺乏明确方向')}")

        print(f"\n{'=' * 55}")
        print("  数据来源: 本地扫描器数据库")
        print(f"{'=' * 55}\n")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
