"""dal 层：写入（P1-6 拆分，2026-08-21）。

所有 INSERT/UPDATE/DELETE + commit/rollback。只读查询在 queries.py。
失败语义保持原样：批量失败逐行回退 / fail-open 返回空，不向上抛。
"""

import json
import logging
import math
import sqlite3

from scanner.config import (
    REVERSAL_OVERSHOOT_DROP,
    REVERSAL_TURNED_RED_DROP,
    WATCH_POOL_MAX,
    now_beijing,
)
from scanner.db._common import _n_trading_days_ago
from scanner.models import KlineBar, RecommendationRow
from scanner.trading_session import is_trading_time
from scanner.utils import is_gem, to_float

logger = logging.getLogger(__name__)


def record_appearances(conn: sqlite3.Connection, symbols: list[dict]):
    today = now_beijing().date().isoformat()
    rows = []
    for i, item in enumerate(symbols, 1):
        # rank 优先用真实榜单排名；缺失时回退到过滤后列表的下标（仅兜底，不应发生）
        rank = item.get("rank", i)
        if rank is None:
            rank = i
        # symbol/name 用 .get() 容错：API 偶发返回缺字段时不应整批写入失败
        rows.append(
            (
                item.get("symbol", ""),
                item.get("name", ""),
                today,
                rank,
                item.get("percent", 0),
                item.get("value", 0),
            )
        )
    try:
        conn.executemany(
            "INSERT INTO appearances (symbol, name, date, rank, percent, value) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(symbol, date) DO UPDATE SET "
            "percent = excluded.percent, rank = excluded.rank, "
            "value = excluded.value, name = excluded.name",
            rows,
        )
        conn.commit()
    except Exception as e:
        print(f"  [!] 批量写入appearances失败: {e}, 逐行回退写入")
        try:
            conn.rollback()  # 事务失败后必须回滚，否则后续 execute 会报
            # "cannot start a transaction within a transaction"
        except sqlite3.Error:
            pass  # 回滚/清理失败无补救手段，外层已记录原始错误；仅捕获 sqlite3.Error，避免吞掉代码 bug
        for row in rows:
            try:
                conn.execute(
                    "INSERT INTO appearances (symbol, name, date, rank, percent, value) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(symbol, date) DO UPDATE SET "
                    "percent = excluded.percent, rank = excluded.rank, "
                    "value = excluded.value, name = excluded.name",
                    row,
                )
            except Exception as e2:
                print(f"  [!] 逐行写入appearances失败 {row[0]}: {e2}")
        conn.commit()


def save_kline_to_db(conn: sqlite3.Connection, symbol: str, kline: list[KlineBar]):
    rows = []
    today_str = now_beijing().date().isoformat()
    trading = is_trading_time()
    for k in kline:
        # 定稿标记：盘中写入的今日 bar 是未收盘快照（可能非最终收盘价），置 0；
        # 收盘后写入（定稿/backfill/repair）或历史 bar 置 1。
        finalized = 0 if (k["date"] == today_str and trading) else 1
        rows.append(
            (
                symbol,
                k.get("timestamp"),
                k["date"],
                k["open"],
                k["close"],
                k["high"],
                k["low"],
                k["volume"],
                k["percent"],
                finalized,
            )
        )
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO daily_kline "
            "(symbol, timestamp, date, open, close, high, low, volume, percent, finalized) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    except Exception as e:
        # 回滚残留在打开事务里的部分行，再逐行重写（与 record_appearances 同模式）
        try:
            conn.rollback()
        except sqlite3.Error:
            pass  # 回滚/清理失败无补救手段，外层已记录原始错误；仅捕获 sqlite3.Error，避免吞掉代码 bug
        print(f"  [!] 批量写入kline失败: {e}, 逐行回退写入")
        for row in rows:
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO daily_kline "
                    "(symbol, timestamp, date, open, close, high, low, volume, percent, finalized) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    row,
                )
            except Exception as e2:
                print(f"  [!] 逐行写入kline失败 {row[0]}: {e2}")
        conn.commit()


def save_market_caps(conn: sqlite3.Connection, caps: dict[str, dict], source: str = "xueqiu") -> int:
    """把成功的市值批量结果落库（陈旧缓存兜底源）。

    仅写本次查询返回的有效条目（market_cap 或 circ_market_cap > 0），0 值（停牌/降级）
    不入缓存以免污染兜底。返回写入条数。
    """
    if not caps:
        return 0
    rows = []
    for sym, d in caps.items():
        mc = d.get("market_cap") or 0
        cmc = d.get("circ_market_cap") or 0
        if mc <= 0 and cmc <= 0:
            continue
        rows.append(
            (
                sym,
                mc,
                cmc,
                d.get("turnover_rate") or 0.0,
                d.get("current") or 0.0,
                d.get("percent") or 0.0,
                source,
                now_beijing().date().isoformat(),
            )
        )
    if not rows:
        return 0
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO market_cap_cache "
            "(symbol, market_cap, circ_market_cap, turnover_rate, current, percent, source, updated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        return len(rows)
    except Exception as e:
        logger.warning(f"save_market_caps failed: {e}")
        return 0


def save_scan_quality(conn: sqlite3.Connection, stats: dict) -> None:
    """落库单轮扫描的数据质量快照（数据血缘日志，2026-08-14）。

    跨函数静默降级是本项目最难发现的 bug 类别：上游函数在故障路径返回"看似正常"
    的降级数据（补拉失败→旧缓存、缺今日 bar→昨日量），下游无感知消费导致误判
    （网宿科技案例：量比硬门误杀放量启动票）。单函数审查无法发现，因为每个函数
    单独都对。此日志把降级规模变成可查询的常态计数器：某日 fetch_failed/
    today_bar_missing 异常升高 + 推荐数骤降 → 关联即定位。

    stats 字段：gem_count / fetch_failed / today_bar_missing / minute_fallback /
    stale_recs。同日多轮扫描按最新一轮覆盖（取当日最后快照），查询历史看日级趋势。
    """
    today = now_beijing().date().isoformat()
    now = now_beijing().strftime("%H:%M:%S")
    try:
        conn.execute(
            """INSERT INTO scan_quality_log
               (date, time, gem_count, fetch_failed, today_bar_missing,
                minute_fallback, stale_recs, updated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
                 time = excluded.time,
                 gem_count = excluded.gem_count,
                 fetch_failed = excluded.fetch_failed,
                 today_bar_missing = excluded.today_bar_missing,
                 minute_fallback = excluded.minute_fallback,
                 stale_recs = excluded.stale_recs,
                 updated = excluded.updated""",
            (
                today,
                now,
                int(stats.get("gem_count", 0) or 0),
                int(stats.get("fetch_failed", 0) or 0),
                int(stats.get("today_bar_missing", 0) or 0),
                int(stats.get("minute_fallback", 0) or 0),
                int(stats.get("stale_recs", 0) or 0),
                now,
            ),
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"save_scan_quality failed: {e}")


def save_market_index_log(
    conn: sqlite3.Connection, index_pct: float | None, bar_date: str | None = None, source: str = "xueqiu"
) -> None:
    """落库单轮扫描使用的大盘指数（大盘指数血缘日志，2026-08-19）。

    大盘标签曾因 kline 接口 begin/count 语义错位把当日 -6.26% 崩盘读成昨日 -0.93%
    （展示"大盘中性"）而无痕：涨幅是瞬时值、不进 daily_kline、不参与评分，任何落库
    数值对账都碰不到它。此表记录「每轮扫描当时读到的大盘涨幅 + 其 bar 日期」，
    bar 日期是「读到哪一天的数据」的权威证据，供 data_health.check_market_index_health
    对账审计（读到旧 bar / 与独立源涨幅偏差超容差 → 告警）。

    同日多轮扫描按最新一轮覆盖（与 scan_quality_log 同语义）。
    """
    today = now_beijing().date().isoformat()
    now = now_beijing().strftime("%H:%M:%S")
    try:
        conn.execute(
            """INSERT INTO market_index_log (date, time, index_pct, bar_date, source, updated)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
                 time = excluded.time,
                 index_pct = excluded.index_pct,
                 bar_date = excluded.bar_date,
                 source = excluded.source,
                 updated = excluded.updated""",
            (today, now, index_pct if index_pct is not None else None, bar_date, source, now),
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"save_market_index_log failed: {e}")


def record_leaderboard_log(
    conn: sqlite3.Connection, source: str, items: list[dict], prev_symbols: set[str]
) -> set[str]:
    """落库单轮榜单快照（榜单可观测性，2026-08-19）。

    把雪球飙升榜/热搜榜每轮成分 + 排名分布写进 leaderboard_log 时间序列，maintainer 与
    scan_quality_log 互补：scan_quality_log 看「下游扫描数据质量」降级，本表看「上游源
    本身」的口径漂移。stats 全用可直接查询的列，symbol_snapshot 存前 40 成员 JSON 供
    重建成分分析。

    上游口径变更信号（对照历史即可识别）：
      - median_pct / up:down 结构突变（如从涨幅榜变热度榜，中位会失真/趋 0）
      - overlap_prev 骤降（排序键或分页变更导致成员剧烈抖动）
      - total / gem_listed 突变（样本过滤条件变更）

    统计口径防御：percent/rank_change 脏值（None/NaN/str）统一 to_float 过滤，
    不参与中位数/均值（与 candidates.filter_gem_stocks 的数值强转同族防御）。

    返回本轮的 symbol 集合，调用方应保存为下一轮的 prev_symbols（用于重叠率）。
    """
    today = now_beijing().date().isoformat()
    now = now_beijing().strftime("%H:%M:%S")
    try:
        syms = [str(i.get("symbol") or "") for i in items]
        valid_syms = [s for s in syms if s]
        cur_syms = set(valid_syms)
        total = len(items)
        gem_listed = sum(1 for cs in valid_syms if is_gem(cs))

        # 涨幅分布（防御：percent 可能为 None/字符串/NaN）
        pcts: list[float] = []
        for i in items:
            v = to_float(i.get("percent"), None)
            if v is not None and math.isfinite(v):
                pcts.append(v)
        up = sum(1 for p in pcts if p > 0)
        down = sum(1 for p in pcts if p < 0)
        flat = len(pcts) - up - down
        median_pct = _median(pcts) if pcts else None
        mean_pct = sum(pcts) / len(pcts) if pcts else None
        top10 = sorted(pcts, reverse=True)[:10]
        top10_mean = sum(top10) / len(top10) if top10 else None
        max_pct = max(pcts) if pcts else None

        # 排名变化中位数（防御：rank_change 可能为 "-"/None/NaN）
        rcs: list[float] = []
        for i in items:
            v = to_float(i.get("rank_change"), None)
            if v is not None and math.isfinite(v):
                rcs.append(v)
        median_rc = _median(rcs) if rcs else None

        # 与上一轮成员重叠比例
        overlap = 0.0
        if prev_symbols and cur_syms:
            overlap = len(cur_syms & prev_symbols) / len(cur_syms)

        # 前 40 成员紧凑快照（供成分重建，不全存以控体积：100条×6KB×240轮/日≈1.4MB/日）
        # 2026-08-20 修复：此前 percent 用 `pcts[idx]`，但 pcts 是过滤非法值后的列表——
        # 任一早期 item percent 为 None/字符串/NaN 时，其后的 symbol percent 全部错位。
        # 现逐条独立清洗（与中位数口径一致，脏值存 None 不误导消费方）。

        def _valid_pct(v):
            f = to_float(v, None)
            return f if f is not None and math.isfinite(f) else None

        snapshot = [
            {
                "symbol": str(i.get("symbol") or ""),
                "name": str(i.get("name") or ""),
                "percent": _valid_pct(i.get("percent")),
                "rank": int(to_float(i.get("rank"), idx + 1) or idx + 1),
                "rank_change": to_float(i.get("rank_change"), None),
            }
            for idx, i in enumerate(items[:40])
        ]
        conn.execute(
            """INSERT OR REPLACE INTO leaderboard_log
               (date, time, source, total, gem_listed, up_count, down_count, flat_count,
                median_pct, mean_pct, top10_mean_pct, max_pct, overlap_prev,
                median_rank_change, symbol_snapshot, updated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                today,
                now,
                source,
                total,
                gem_listed,
                up,
                down,
                flat,
                median_pct,
                mean_pct,
                top10_mean,
                max_pct,
                overlap,
                median_rc,
                json.dumps(snapshot, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
        return cur_syms
    except Exception as e:
        logger.warning(f"record_leaderboard_log failed: {e}")
        # fail-open：落库失败不影响扫描主流程，返回空集（不污染后续重叠率）
        return prev_symbols


def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def save_recommendations(conn: sqlite3.Connection, new_faces: list, rest: list, source: str | None = None):
    """保存当日推荐记录：new_faces（新面孔）+ rest（其余所有类别合并列表）。

    rest 在调用方由 momentum/rebound/short_term/comeback 各桶合并传入，
    与 new_faces 在本函数内同等对待（统一去重 + 取当日最高分）。

    去重实现（审查卫生项：原逐候选 SELECT 一次，N 只票 N 次查询）：单次预载当日
    全部 (symbol, category) → (id, score)，循环内维护内存 map——同批重复
    (symbol, category) 与跨轮重复同语义（仅更高分覆盖）。写入仍逐行 execute，
    保留单票失败不拖垮整批的错误隔离。

    事务卫生（2026-08-24 审查，与 record_appearances / save_kline_to_db 同族）：
    - 预载失败 fail-loud 上抛（表无 (date,symbol,category) 唯一约束，静默降级空 map
      会把本轮全部候选插成重复行，永久污染归因/回测样本；上抛只损失一轮展示，
      主循环 P-robust 兜底捕获后下轮重写）。
    - 单行失败先 rollback 清理残留事务再继续（否则后续 execute 连锁报错；
      已写入行未提交会随回滚丢弃，但下一扫描周期整体重写、高分覆盖语义兜底）。
    - commit 纳入保护并失败回滚，异常不再穿透到主循环跳过该轮 display/飞书推送。
    """
    today = now_beijing().date().isoformat()
    now = now_beijing().strftime("%H:%M:%S")
    existing_map: dict[tuple[str, str], list]
    try:
        existing_map = {
            (r[0], r[1]): [r[2], r[3]]
            for r in conn.execute(
                "SELECT symbol, category, id, score FROM recommendations WHERE date = ?",
                (today,),
            ).fetchall()
        }
    except Exception as e:
        logger.warning(f"save_recommendations 预载已有记录失败: {e}")
        raise
    for c in new_faces + rest:
        conn.execute("SAVEPOINT sp_rec")
        try:
            key = (c.stock.symbol, c.category)
            existing = existing_map.get(key)
            breakdown = json.dumps(c.kline.dimensions, ensure_ascii=False) if c.kline and c.kline.dimensions else None
            rec_source = source or getattr(c.stock, "source_tag", "unified")
            concept = getattr(c, "driving_concept", "") or ""
            accumulated = c.kline.accumulated_pct if c.kline else None
            stale_kline = 1 if getattr(c, "stale_kline", False) else 0
            excluded_reason = getattr(c, "excluded_reason", "") or ""
            if existing:
                # 同日同股同策略已存在：仅当新分更高时更新（保留当日最高分用于回测归因）
                if c.score > existing[1]:
                    conn.execute(
                        "UPDATE recommendations SET time = ?, score = ?, percent = ?, trend = ?, "
                        "score_breakdown = ?, source = ?, concept = ?, accumulated_pct = ?, "
                        "stale_kline = ?, excluded_reason = ? "
                        "WHERE id = ?",
                        (
                            now,
                            c.score,
                            c.stock.percent,
                            c.kline.trend if c.kline else None,
                            breakdown,
                            rec_source,
                            concept,
                            accumulated,
                            stale_kline,
                            excluded_reason,
                            existing[0],
                        ),
                    )
                    existing[1] = c.score  # 同批后续重复项按最新最高分比较
                conn.execute("RELEASE sp_rec")
                continue
            cur = conn.execute(
                "INSERT INTO recommendations (date, time, symbol, name, category, score, percent, "
                "trend, score_breakdown, source, concept, accumulated_pct, stale_kline, excluded_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    today,
                    now,
                    c.stock.symbol,
                    c.stock.name,
                    c.category,
                    c.score,
                    c.stock.percent,
                    c.kline.trend if c.kline else None,
                    breakdown,
                    rec_source,
                    concept,
                    accumulated,
                    stale_kline,
                    excluded_reason,
                ),
            )
            # 记录真实 rowid：同批后续重复项走高分 UPDATE 时需要定位到本行
            existing_map[key] = [cur.lastrowid, c.score]
            conn.execute("RELEASE sp_rec")
        except Exception as e:
            # 2026-08-30：注释原写「用 savepoint 隔离失败行」，实际执行的是
            # conn.rollback() —— 整批已写入行一并丢弃（注释自己也承认这点）。
            # 改用真 savepoint：单行失败只回滚该行，已成功的行保留，与注释语义一致。
            try:
                conn.execute("ROLLBACK TO sp_rec")
                conn.execute("RELEASE sp_rec")
            except sqlite3.Error:
                pass  # 回滚/清理失败无补救手段，外层已记录原始错误；仅捕获 sqlite3.Error，避免吞掉代码 bug
            print(f"  [!] 保存推荐记录失败 {c.stock.symbol}: {e}")
    try:
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass  # 回滚/清理失败无补救手段，外层已记录原始错误；仅捕获 sqlite3.Error，避免吞掉代码 bug
        logger.warning(f"save_recommendations 提交失败（本轮推荐未落库，下轮重写）: {e}")


def save_rejections(conn: sqlite3.Connection, rejected: list, today: str | None = None) -> int:
    """记录被硬过滤移出推荐的候选（审计表 scan_rejections，不进 recommendations）。

    为什么要独立表：orchestrator 先剔除 all_candidates 再落库，「当日首次成为候选
    即被过滤」的票在 recommendations 里连一行都没有，_update_excluded_marks 的
    UPDATE 也无行可更新 —— 于是「被杀掉的票次日涨得怎样」永远查不到，硬过滤有没有
    用不可验证（只看得到活下来的票 = 幸存者偏差）。

    不写进 recommendations 的原因：_load_signals（组合回测）读该表不过滤 excluded，
    混进去会让回测宇宙无端变大、与线上可买集进一步脱节。

    同一 (date, symbol, category) 重复命中只累加 hits 并刷新 reason/score。
    fail-open：落库失败仅告警，不影响扫描主流程。
    """
    if not rejected:
        return 0
    rec_date = today or now_beijing().date().isoformat()
    now_t = now_beijing().strftime("%H:%M:%S")
    rows = [
        (
            rec_date,
            c.stock.symbol,
            c.category,
            getattr(c, "excluded_reason", "") or "",
            getattr(c, "score", 0) or 0,
            to_float(getattr(c.stock, "percent", 0.0), default=0.0),
            now_t,
            now_t,
        )
        for c in rejected
    ]
    try:
        conn.executemany(
            "INSERT INTO scan_rejections "
            "(date, symbol, category, reason, score, percent, first_time, updated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(date, symbol, category) DO UPDATE SET "
            "reason = excluded.reason, "
            "score = MAX(scan_rejections.score, excluded.score), "
            "hits = scan_rejections.hits + 1, "
            "updated = excluded.updated",
            rows,
        )
        conn.commit()
        return len(rows)
    except Exception as e:
        logger.warning(f"save_rejections 落库失败（硬过滤审计缺失，不影响扫描）: {e}")
        try:
            conn.rollback()
        except sqlite3.Error:
            pass  # 回滚/清理失败无补救手段，外层已记录原始错误；仅捕获 sqlite3.Error，避免吞掉代码 bug
        return 0


def upsert_watch_symbol(
    conn: sqlite3.Connection, symbol: str, name: str, last_list_date: str | None = None, over_limit: bool = False
) -> None:
    """写入/刷新掉榜跟踪池（单条，委托批量实现）。

    - 在榜票每次扫描刷新 last_list_date（保活）；
    - 超限启动票（当日涨幅超过 short_term 上限）置 over_limit=1，持续盯防；
    - added_date 保持首次入池日期不变。
    """
    upsert_watch_symbols(
        conn,
        [
            {"symbol": symbol, "name": name, "last_list_date": last_list_date, "over_limit": over_limit},
        ],
    )


def upsert_watch_symbols(conn: sqlite3.Connection, entries: list[dict]) -> None:
    """批量写入/刷新掉榜跟踪池（单次事务，避免逐条 commit 拖慢扫描循环）。

    entries: [{symbol, name, last_list_date?, over_limit?}]
    """
    if not entries:
        return
    today = now_beijing().date().isoformat()
    rows = [
        (e.get("symbol", ""), e.get("name", ""), e.get("last_list_date") or today, 1 if e.get("over_limit") else 0)
        for e in entries
        if e.get("symbol")
    ]
    try:
        conn.executemany(
            """INSERT INTO watch_pool (symbol, name, added_date, last_list_date, last_eval_date, over_limit)
               VALUES (?, ?, ?, ?, NULL, ?)
               ON CONFLICT(symbol) DO UPDATE SET
                 name = excluded.name,
                 last_list_date = MAX(watch_pool.last_list_date, excluded.last_list_date),
                 over_limit = MAX(watch_pool.over_limit, excluded.over_limit)""",
            [(sym, name, today, lst, ov) for sym, name, lst, ov in rows],
        )
        # WATCH_POOL_MAX 容量上限：超限时淘汰 last_list_date 最旧的条目（含 over_limit 票，
        # 老旧的超限启动票不再盯防，防止池无限膨胀）。与 prune_watch_pool 的交易日剪枝互补。
        conn.execute(
            "DELETE FROM watch_pool WHERE symbol NOT IN ("
            "  SELECT symbol FROM watch_pool ORDER BY last_list_date DESC LIMIT ?"
            ")",
            (WATCH_POOL_MAX,),
        )
        conn.commit()
    except Exception as e:
        print(f"  [!] 批量写入watch_pool失败: {e}")
        try:
            conn.rollback()
        except sqlite3.Error:
            pass  # 回滚/清理失败无补救手段，外层已记录原始错误；仅捕获 sqlite3.Error，避免吞掉代码 bug


def mark_watch_evaluated(conn: sqlite3.Connection, symbols: list[str], today: str | None = None) -> None:
    """标记掉榜票今日已评估（避免同一交易日重复评估/重复补拉）。

    `today` 由调用方传入（2026-08-24 审查：原自行取真实时钟，而 evaluate_comeback
    的幂等判断用的是扫描锚定日——跨午夜长跑会出现「按锚定日判未评估、按真实日期
    标记」的错位；缺省保持旧行为）。
    """
    if not symbols:
        return
    today = today or now_beijing().date().isoformat()
    failed: list[str] = []
    for sym in symbols:
        try:
            conn.execute("UPDATE watch_pool SET last_eval_date = ? WHERE symbol = ?", (today, sym))
        except sqlite3.Error as e:
            # 2026-08-29：原为静默 pass。若整批失败（表缺失/库锁），标记不落地会让
            # evaluate_comeback 每个扫描周期重复评估、重复补拉同一批票却无人察觉。
            # 汇总告警，让"回马枪重复劳动"这类降级可见。
            failed.append(sym)
            logger.warning("mark_watch_evaluated 标记失败 %s: %s", sym, e)
    if failed:
        print(f"  [!] 回马枪已评估标记失败 {len(failed)}/{len(symbols)} 只（下轮可能重复评估）")
    try:
        conn.commit()
    except Exception as e:
        print(f"  [!] mark_watch_evaluated 提交失败: {e}")
        try:
            conn.rollback()
        except sqlite3.Error:
            pass  # 回滚/清理失败无补救手段，外层已记录原始错误；仅捕获 sqlite3.Error，避免吞掉代码 bug


def prune_watch_pool(conn: sqlite3.Connection, keep_trading_days: int = 15) -> int:
    """删除掉榜超过 keep_trading_days 个交易日的条目（last_list_date 过旧）。

    over_limit 票的 last_list_date 在其超限上榜日写入，同样按此剪枝，
    不额外豁免——避免超限队列无限期驻留。
    """
    cutoff = _n_trading_days_ago(keep_trading_days)
    try:
        cur = conn.execute("DELETE FROM watch_pool WHERE last_list_date < ?", (cutoff,))
        conn.commit()
        return cur.rowcount
    except Exception as e:
        logger.warning(f"prune_watch_pool failed: {e}")
        return 0


def mark_reversed_recommendations(
    conn: sqlite3.Connection,
    today_recs: list[RecommendationRow],
    active_syms: set[str],
    live_quotes: dict[str, dict],
    turned_red_drop: float = REVERSAL_TURNED_RED_DROP,
    overshoot_drop: float = REVERSAL_OVERSHOOT_DROP,
) -> list[str]:
    """推荐后快速反转移出（2026-08-13）：今日已推荐、当前不在候选池、且回落幅度超标的榜上
    主类别票，标 excluded=1 移出综合排序展示（保留落库记录），返回本次标记的 symbol 列表。

    **回落幅度口径（2026-08-13 改为「从当日最高价」计算）**：`drop = ref_pct - live_pct`，
    其中 ref_pct 优先取 live_quotes[sym]["high_pct"]（当日最高涨幅，由行情 API 的 high/昨收
    算出），缺失时回退推荐时刻涨幅（today_recs 的 percent）—— 以最高点为锚能更客观反映
    "动量从峰值衰减"，不受推荐时刻择时影响。
    命中任一条件即视为推荐失败：
      ① 已转负（live_pct<0）且 drop ≥ turned_red_drop（滤掉高位仅小幅回落就微幅翻绿的噪音）；
      ② drop ≥ overshoot_drop，无论红绿——大幅回吐即使未转负也"不敢买"（如从 +12% 高点回落到
         +2%，动量已破）。
    阈值按「从最高涨幅→收盘」回落分布校准（p75=4.49/p90=7.92/p95=10.54，见 config 注释），
    非单票凑参。回马枪（category=="comeback"）是掉榜跟踪池，不参与自动移出。硬过滤只评估当前
    轮次候选，够不着掉出候选池的旧推荐；本函数对 active_syms（本轮通过验证的候选）不下手，
    避免与 orchestrator 的 passed_syms 置 0 打架；重新成为候选的票由 orchestrator 置回
    excluded=0。行情缺失（live_quotes 无该 symbol / percent 为 None）时按无法度量 fail-open。
    """
    reversed_syms: list[str] = []
    for r in today_recs:
        sym = r["symbol"]
        if sym in active_syms:
            continue
        if r.get("category") in ("comeback", "core_dip"):
            continue
        rec_pct = r.get("percent")
        q = live_quotes.get(sym)
        if not q:
            continue
        # fail-open 防线（2026-08-14）：行情生产端（fetch_market_caps_batch 等）对缺失
        # 字段强转为 0.0，percent=None 检查实际不可达。current 存在且 <=0（无 A 股以
        # 0 元成交）即行情降级/停牌条目——0.00% 会被误当"已转负"、drop=ref-0 虚高，
        # 导致误移出，必须按无法度量跳过（与 docstring 的行情缺失 fail-open 语义对齐）。
        cur = q.get("current")
        if cur is not None and cur <= 0:
            continue
        live_pct = q.get("percent")
        if rec_pct is None or live_pct is None:
            continue
        ref_pct = q.get("high_pct")
        if ref_pct is None:
            ref_pct = rec_pct
        drop = ref_pct - live_pct
        if (live_pct < 0 and drop >= turned_red_drop) or drop >= overshoot_drop:
            reversed_syms.append(sym)
    if not reversed_syms:
        return []
    today = now_beijing().date().isoformat()
    try:
        # 2026-08-17 审查修复：UPDATE 加 COALESCE(category,'')!='comeback' 守卫——
        # 判定循环已跳过 comeback 行，但 UPDATE 原按 (date, symbol) 全量置 excluded，
        # 若同 symbol 当日既有榜上主类别行（触发反转移出）又有早先落库的 comeback 行，
        # 后者也会被连带移出（docstring 声明"回马枪不参与自动移出"）。
        # 2026-08-19：core_dip 与 comeback 同族，同样不被反转移出连带。
        conn.executemany(
            "UPDATE recommendations SET excluded=1 WHERE date=? AND symbol=? "
            "AND COALESCE(category, '') NOT IN ('comeback', 'core_dip')",
            [(today, sym) for sym in reversed_syms],
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"mark_reversed_recommendations failed: {e}")
        return []
    return reversed_syms


def save_concepts_cache(conn: sqlite3.Connection, concepts_map: dict[str, list[str]]):
    """批量写入 concept_cache（INSERT OR REPLACE），只在有数据时覆盖。"""
    if not concepts_map:
        return
    now = now_beijing().isoformat()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO concept_cache (symbol, concepts, updated) VALUES (?, ?, ?)",
            [(sym, json.dumps(concepts, ensure_ascii=False), now) for sym, concepts in concepts_map.items()],
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"save_concepts_cache failed: {e}")
        try:
            conn.rollback()
        except sqlite3.Error:
            pass  # 回滚/清理失败无补救手段，外层已记录原始错误；仅捕获 sqlite3.Error，避免吞掉代码 bug


def save_market_extra_cache(conn: sqlite3.Connection, data_map: dict[str, dict], data_type: str):
    """批量写入 market_extra_cache（INSERT OR REPLACE，按 symbol+data_type 覆盖）。"""
    if not data_map:
        return
    today = now_beijing().date().isoformat()
    now = now_beijing().isoformat()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO market_extra_cache (symbol, date, data_type, payload_json, updated) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (sym, today, data_type, json.dumps(payload, ensure_ascii=False), now)
                for sym, payload in data_map.items()
            ],
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"save_market_extra_cache failed: {e}")
        try:
            conn.rollback()
        except sqlite3.Error:
            pass  # 回滚/清理失败无补救手段，外层已记录原始错误；仅捕获 sqlite3.Error，避免吞掉代码 bug
