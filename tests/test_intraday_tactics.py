"""盘中操作纪律测试。

覆盖 12 条操盘纪律的时段边界 × 个股状态组合。注入 now 参数避免依赖真实时钟。
数据形状与生产一致（审查修复 2026-08-31）：
  - kline bars 日期由注入的 now 派生（此前硬编码 2026-08-31，日期过后静默走 fallback）
  - 分时场景用 kline.dimensions["minute_*"]（intraday_fetch 第 4 相产出的键）
  - 日内高点用 high_pct 参数（orchestrator 从 quote 的 high/昨收传入）
fail-open：无 kline / 无分时摘要 / 无 high_pct 时不误标、不报错。
"""
import datetime as _dt

from scanner.intraday_tactics import session_advice, stock_actions
from scanner.models import Candidate, KlineBar, KlineSummary, StockInfo

TZ = _dt.timezone(_dt.timedelta(hours=8))


def _now(hour: int, minute: int) -> _dt.datetime:
    """构造指定时间的北京时间。2026-08-31 是周一（非节假日，holidays fallback 未含）。"""
    return _dt.datetime(2026, 8, 31, hour, minute, tzinfo=TZ)


def _cand(
    pct: float = 2.0,
    current: float = 102.0,
    volume_ratio: float = 1.0,
    lianban: int = 0,
    zhaban: int = 0,
    risk_flags: list[str] | None = None,
    minute_dims: dict | None = None,
) -> Candidate:
    stock = StockInfo(
        symbol="SZ300001",
        name="测试",
        code="300001",
        percent=pct,
        current=current,
        value=1e8,
        rank=1,
        rank_change=0,
    )
    kline = KlineSummary(
        trend="up",
        accumulated_pct=5.0,
        volume_ratio=volume_ratio,
        bottom_confirmed=False,
        score=50,
        dimensions={},
    )
    kline.dimensions["zt_lianban"] = lianban
    kline.dimensions["zt_zhaban"] = zhaban
    if minute_dims:
        kline.dimensions.update(minute_dims)
    return Candidate(
        stock=stock,
        category="new_face",
        score=50,
        reason="test",
        kline=kline,
        risk_flags=risk_flags or [],
    )


def _bars(now: _dt.datetime, open_pct: float, prev_close: float = 100.0) -> list[KlineBar]:
    """构造 kline bars：昨日 bar + 今日 bar（日期从注入的 now 派生）。

    涨停前的交易日取 now.date() 的前一自然日（周一场景 → 周五，均非节假日）。
    """
    prev_day = now.date() - _dt.timedelta(days=1 if now.date().weekday() > 0 else 3)
    today_open = prev_close * (1 + open_pct / 100)
    return [
        KlineBar(date=prev_day.isoformat(), open=99, high=101, low=98, close=prev_close,
                 volume=1e6, percent=-0.5),
        KlineBar(date=now.date().isoformat(), open=today_open, high=today_open * 1.03,
                 low=today_open * 0.98, close=today_open * 1.02, volume=1.2e6, percent=open_pct + 2),
    ]


# ── session_advice 时段测试 ──

class TestSessionAdvice:
    def test_morning_top_window(self):
        """规则 9：09:30-09:33 冲高见顶段。"""
        advice = session_advice(_now(9, 31))
        assert advice is not None
        assert "冲高见顶" in advice

    def test_sell_window(self):
        """规则 8：09:45-10:00 卖出黄金窗口。"""
        advice = session_advice(_now(9, 50))
        assert advice is not None
        assert "卖出" in advice

    def test_afternoon_top_window(self):
        """规则 9：13:20-13:30 午后见顶段。"""
        advice = session_advice(_now(13, 25))
        assert advice is not None
        assert "午后" in advice

    def test_lowbuy_window(self):
        """规则 4：14:30-14:45 低吸窗口。"""
        advice = session_advice(_now(14, 35))
        assert advice is not None
        assert "低吸" in advice

    def test_no_advice_outside_windows(self):
        """非提醒时段返回 None。"""
        assert session_advice(_now(11, 0)) is None
        assert session_advice(_now(14, 0)) is None

    def test_pre_open_returns_none(self):
        """盘前（09:00）不输出任何提醒（is_trading_time 守卫，审查修复）。"""
        assert session_advice(_now(9, 0)) is None

    def test_after_close_returns_none(self):
        """收盘后（15:30）不输出任何提醒。"""
        assert session_advice(_now(15, 30)) is None

    def test_weekend_returns_none(self):
        """周末返回 None（2026-09-05 是周六）。"""
        t = _dt.datetime(2026, 9, 5, 9, 30, tzinfo=TZ)
        assert session_advice(t) is None


# ── 规则 2：高开≥5% 封不住板 → ⬇减半 ──

class TestRule2HighOpen:
    def test_high_open_no_limit_up(self):
        now = _now(9, 35)
        c = _cand(pct=6.0)
        actions = stock_actions(c, now, kline_bars=_bars(now, open_pct=5.5))
        assert "⬇减半" in actions

    def test_high_open_with_zhaban(self):
        now = _now(9, 35)
        c = _cand(pct=10.0, lianban=1, zhaban=1)
        actions = stock_actions(c, now, kline_bars=_bars(now, open_pct=6.0))
        assert "⬇减半" in actions

    def test_high_open_stable_limit_up_no_reduce(self):
        now = _now(9, 35)
        c = _cand(pct=10.0, lianban=1, zhaban=0)
        actions = stock_actions(c, now, kline_bars=_bars(now, open_pct=6.0))
        assert "⬇减半" not in actions

    def test_stale_kline_no_today_bar_no_false_reduce(self):
        """今日 bar 缺失（stale_kline）→ 高开未知 → 不误标 ⬇减半（审查修复）。"""
        now = _now(9, 35)
        c = _cand(pct=6.0)  # 平开但现涨 6% 的场景：旧实现拿现价冒充高开会误杀
        bars = [_bars(now, open_pct=0.0)[0]]  # 只有昨日 bar
        actions = stock_actions(c, now, kline_bars=bars)
        assert "⬇减半" not in actions

    def test_no_bars_no_false_reduce(self):
        """无 bars → 高开未知 → 不误标 ⬇减半。"""
        c = _cand(pct=6.0)
        actions = stock_actions(c, _now(9, 35), kline_bars=None)
        assert "⬇减半" not in actions


# ── 规则 3：平开+稳步走高+量能同步 → ⬆加仓 ──

class TestRule3SteadyRise:
    def test_flat_open_steady_rise_volume(self):
        """分时摘要：爬升占比 ≥0.6 且量能趋势 ≥1 → 加仓。"""
        now = _now(9, 55)
        c = _cand(pct=2.0, minute_dims={"minute_steady_rise": 0.75, "minute_vol_trend": 1.2})
        actions = stock_actions(c, now, kline_bars=_bars(now, open_pct=0.0))
        assert "⬆加仓" in actions

    def test_flat_open_no_volume_no_add(self):
        """量能趋势 <1 → 不加仓。"""
        now = _now(9, 55)
        c = _cand(pct=2.0, minute_dims={"minute_steady_rise": 0.75, "minute_vol_trend": 0.6})
        actions = stock_actions(c, now, kline_bars=_bars(now, open_pct=0.0))
        assert "⬆加仓" not in actions

    def test_low_rise_ratio_no_add(self):
        """爬升占比 <0.6 → 不加仓。"""
        now = _now(9, 55)
        c = _cand(pct=2.0, minute_dims={"minute_steady_rise": 0.3, "minute_vol_trend": 1.2})
        actions = stock_actions(c, now, kline_bars=_bars(now, open_pct=0.0))
        assert "⬆加仓" not in actions

    def test_no_minute_data_no_add(self):
        """无分时摘要（AKShare 源降级）→ 不加仓（fail-open）。"""
        now = _now(9, 55)
        c = _cand(pct=2.0)
        actions = stock_actions(c, now, kline_bars=_bars(now, open_pct=0.0))
        assert "⬆加仓" not in actions

    def test_gap_up_not_flat_no_add(self):
        """非平开（高开 3%）→ 不加仓。"""
        now = _now(9, 55)
        c = _cand(pct=4.0, minute_dims={"minute_steady_rise": 0.75, "minute_vol_trend": 1.2})
        actions = stock_actions(c, now, kline_bars=_bars(now, open_pct=3.0))
        assert "⬆加仓" not in actions


# ── 规则 5：14:30 后尾盘跳水 → 🔻勿接 ──

class TestRule5TailDive:
    def test_tail_dive_with_quote_high(self):
        """quote high_pct（真实日内高点）回落 ≥2% → 勿接。"""
        c = _cand(pct=5.0, minute_dims={"minute_day_high": 8.0})
        actions = stock_actions(c, _now(14, 35), high_pct=8.0)
        assert "🔻勿接" in actions

    def test_tail_dive_fallback_minute_high(self):
        """quote high_pct 缺失 → 回退分时摘要 day_high。"""
        c = _cand(pct=5.0, minute_dims={"minute_day_high": 8.0})
        actions = stock_actions(c, _now(14, 35), high_pct=None)
        assert "🔻勿接" in actions

    def test_no_high_data_no_dive_tag(self):
        """quote 与分时摘要均无 → 不标勿接（fail-open，不猜）。"""
        c = _cand(pct=5.0)
        actions = stock_actions(c, _now(14, 35), high_pct=None)
        assert "🔻勿接" not in actions

    def test_no_dive_small_pullback(self):
        """回落 <2% → 不标。"""
        c = _cand(pct=7.0, minute_dims={"minute_day_high": 8.0})
        actions = stock_actions(c, _now(14, 35), high_pct=8.0)
        assert "🔻勿接" not in actions


# ── 规则 6：14:00-14:30 涨停 → 💰落袋（规则 10 并入）──

class TestRule6LimitUp:
    def test_limit_up_at_1400_bag(self):
        """规则 6 可达性（审查修复：此前被 ⏸观望 永久抢占）。"""
        c = _cand(pct=10.0, lianban=1, zhaban=0)
        actions = stock_actions(c, _now(14, 10))
        assert "💰落袋" in actions

    def test_zhaban_no_clear(self):
        """曾炸板（封板不稳）→ 不落袋。"""
        c = _cand(pct=10.0, lianban=1, zhaban=1)
        actions = stock_actions(c, _now(14, 10))
        assert "💰落袋" not in actions

    def test_limit_up_outside_window_no_bag(self):
        """10:00 前封板（强势画像）→ 不落袋。"""
        c = _cand(pct=10.0, lianban=1, zhaban=0)
        actions = stock_actions(c, _now(9, 45))
        assert "💰落袋" not in actions


# ── 规则 7：午盘冲高回落+缩量 → ⬇减仓 ──

class TestRule7MiddayFade:
    def test_fade_below_am_high_shrink(self):
        """现价低于早盘高点 + 分时量能趋势 <1 → 减仓。"""
        c = _cand(pct=4.0, minute_dims={"minute_am_high": 6.0, "minute_vol_trend": 0.6})
        actions = stock_actions(c, _now(13, 35))
        assert "⬇减仓" in actions

    def test_fade_shrink_fallback_volume_ratio(self):
        """无分时量能 → 回退 K 线量比 <0.7 判缩量。"""
        c = _cand(pct=4.0, volume_ratio=0.5, minute_dims={"minute_am_high": 6.0})
        actions = stock_actions(c, _now(13, 35))
        assert "⬇减仓" in actions

    def test_above_am_high_no_reduce(self):
        """现价超过早盘高点 → 不减仓。"""
        c = _cand(pct=7.0, minute_dims={"minute_am_high": 6.0, "minute_vol_trend": 0.6})
        actions = stock_actions(c, _now(13, 35))
        assert "⬇减仓" not in actions

    def test_no_am_high_data_no_reduce(self):
        """无分时摘要 → 不减仓（fail-open，不拿日K历史冒充早盘高点）。"""
        c = _cand(pct=4.0, volume_ratio=0.5)
        actions = stock_actions(c, _now(13, 35))
        assert "⬇减仓" not in actions


# ── 规则 1/12：早盘冲高减仓 / 早盘大跌加仓 ──

class TestMorningRules:
    def test_rule1_morning_spike_reduce(self):
        """早盘现涨 ≥3% 且未封板 → 减仓。"""
        c = _cand(pct=4.0)
        actions = stock_actions(c, _now(9, 35))
        assert "⬇减仓" in actions

    def test_rule1_limit_up_no_reduce(self):
        """早盘已封板 → 不减仓。"""
        c = _cand(pct=10.0, lianban=1, zhaban=0)
        actions = stock_actions(c, _now(9, 35))
        assert "⬇减仓" not in actions

    def test_rule1_after_window_no_reduce(self):
        """10:00 后早盘冲高减仓不再触发。"""
        c = _cand(pct=4.0)
        actions = stock_actions(c, _now(10, 5))
        assert "⬇减仓" not in actions

    def test_rule12_morning_crash_add(self):
        """早盘大跌 + 无硬风险 → 加仓。"""
        c = _cand(pct=-4.0)
        actions = stock_actions(c, _now(9, 40))
        assert "⬆加仓" in actions

    def test_rule12_morning_crash_with_risk(self):
        """早盘大跌 + 主力出货 → 不加仓。"""
        c = _cand(pct=-4.0, risk_flags=["主力出货"])
        actions = stock_actions(c, _now(9, 40))
        assert "⬆加仓" not in actions

    def test_rule1_and_rule12_mutually_exclusive(self):
        """同一时刻涨跌幅只会命中一边（互斥守卫）。"""
        up = _cand(pct=4.0)
        down = _cand(pct=-4.0)
        assert "⬆加仓" not in stock_actions(up, _now(9, 40))
        assert "⬇减仓" not in stock_actions(down, _now(9, 40))


# ── fail-open / 开关 ──

class TestFailOpenAndSwitch:
    def test_empty_actions_no_noise(self):
        """无纪律命中（午间平盘）→ 空列表。"""
        c = _cand(pct=1.0)
        assert stock_actions(c, _now(11, 0)) == []

    def test_pre_open_returns_empty(self):
        """盘前扫描 → 空（is_trading_time 守卫）。"""
        c = _cand(pct=4.0)
        assert stock_actions(c, _now(9, 0)) == []

    def test_disable_tactics(self, monkeypatch):
        """ENABLE_INTRADAY_TACTICS=0 → 全部返回空。"""
        monkeypatch.setattr("scanner.intraday_tactics.ENABLE_INTRADAY_TACTICS", False)
        assert session_advice(_now(9, 31)) is None
        c = _cand(pct=6.0)
        assert stock_actions(c, _now(9, 35)) == []


# ── 标签字面量单源（2026-09-11 收敛回归）──

class TestTagSingleSource:
    """防回归：标签字面量必须只在 config.py 定义一份。

    收敛前有 7 份拷贝——产生端 4 处（本模块规则 1/2/3/5/6/7/12 的 append）+
    消费端 3 处（display 主表 / display v2 池选区 / final_pick 终选）。
    final_pick.py 的注释还声称「与 display 同源」，实为拷贝，改一处忘另两处
    会导致展示与终选硬过滤口径静默不一致。
    """

    def test_sell_tags_refers_to_config_single_source(self):
        """final_pick._SELL_TAGS 必须来自 config 单源，而非字面量拷贝。

        注意：这里断言「值相等 + 源码是引用」而非 `is` 身份相等。原因：全量测试下
        `scanner.config` 可能被重载（产生等值的另一个 frozenset 实例），
        `is` 断言会随测试顺序虚假失败。防回归靠的是下面的源码检查。
        """
        import pathlib
        import re

        from scanner import final_pick
        from scanner.config import TACTICS_SELL_TAGS

        assert set(final_pick._SELL_TAGS) == set(TACTICS_SELL_TAGS)

        src = pathlib.Path(final_pick.__file__).read_text(encoding="utf-8")
        # 定义行必须是 `_SELL_TAGS = TACTICS_SELL_TAGS`，不能是字面量集合
        assert re.search(r"^_SELL_TAGS\s*=\s*TACTICS_SELL_TAGS\s*$", src, re.M), (
            "final_pick._SELL_TAGS 不再是 config 单源引用——"
            "说明又被复制成了字面量，改标签会漏改此处"
        )

    def test_sell_tags_exact_membership(self):
        """卖出集合精确等于 4 项，且不含方向相反的 ⬆加仓。"""
        from scanner.config import TACTICS_SELL_TAGS, TACTICS_TAG_ADD

        assert set(TACTICS_SELL_TAGS) == {"⬇减仓", "⬇减半", "🔻勿接", "💰落袋"}
        assert TACTICS_TAG_ADD not in TACTICS_SELL_TAGS, "加仓是买点信号，不应被当成卖出标签剔除"

    def test_produced_tags_all_defined_in_config(self):
        """产生端产出的每个标签都必须是 config 常量（防止新增标签时又硬编码）。"""
        from scanner.config import (
            TACTICS_TAG_ADD,
            TACTICS_TAG_NO_CHASE,
            TACTICS_TAG_REDUCE,
            TACTICS_TAG_REDUCE_HALF,
            TACTICS_TAG_TAKE_PROFIT,
        )

        known = {
            TACTICS_TAG_REDUCE_HALF,
            TACTICS_TAG_TAKE_PROFIT,
            TACTICS_TAG_NO_CHASE,
            TACTICS_TAG_REDUCE,
            TACTICS_TAG_ADD,
        }
        # 高开减半（规则 2）：最高优先级，命中即 return
        _n = _now(9, 35)
        assert set(stock_actions(_cand(pct=6.0), _n, kline_bars=_bars(_n, open_pct=5.5))) <= known
        # 早盘冲高减仓（规则 1）
        assert set(stock_actions(_cand(pct=4.0), _now(9, 40))) <= known
        # 尾盘跳水勿接（规则 5）
        assert set(stock_actions(_cand(pct=2.0), _now(14, 35), high_pct=5.0)) <= known
        # 涨停落袋（规则 6）
        assert set(stock_actions(_cand(pct=10.0), _now(14, 10))) <= known

    def test_no_hardcoded_tag_literals_in_source(self):
        """源码中除 config.py 外不得出现标签字面量（docstring 说明除外）。"""
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent / "scanner"
        tags = ["⬇减仓", "⬇减半", "🔻勿接", "💰落袋", "⬆加仓"]
        offenders: list[str] = []
        for py in sorted(root.glob("*.py")):
            if py.name == "config.py":
                continue  # 定义处，豁免
            for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                # 跳过 docstring / 注释——这些是给人看的说明，不是代码里的真源
                if stripped.startswith(("#", '"', "'")) or set(stripped) <= set("\"' "):
                    continue
                if any(t in line for t in tags):
                    offenders.append(f"{py.name}:{i}: {stripped}")
        # 允许 docstring 内的标签描述（落在 """...""" 块中，上面的启发式可能漏判），
        # 故只把「含标签的赋值语句/成员判断」视为违规。
        offenders = [o for o in offenders if re.search(r'[=]\s*[{"]|in\s+[{"]', o)]
        assert not offenders, "发现硬编码标签字面量（应改为 config 单源常量）：\n" + "\n".join(offenders)
