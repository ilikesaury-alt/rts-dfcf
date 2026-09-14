"""scanner.rule_validate 离线单元测试（audit §B1）。

**为什么需要这组测试**：B1 工具的价值全部落在三件事上，而这三件事都可以离线断言：

  1. **统计口径正确** —— `bootstrap_mean_ci` / `verdict_from_ci` 是判定的心脏，
     它算错就会把噪声判成信号（或反之），且不会有任何别的测试发现；
  2. **可见性硬校验有效** —— 这是「验证了一个根本没生效的改动」的唯一防线，
     一旦失效，工具会给出**看似有效的结论**（最危险的失效模式）；
  3. **override 会传播且能恢复** —— 项目用快照式导入（`from scanner.config import X`
     把值绑进导入方命名空间），只改目标模块对消费方无效；而传播后必须能精确回滚，
     否则测试之间会互相污染。

全程离线：不读 scanner.db、不联网、不跑重扫。`--evaluator rescore` 的重扫路径
由 `tests/test_rule_validate_smoke.py`（需真库）覆盖。
"""

from __future__ import annotations

import math
import sys
import types

import pytest

from scanner import rule_validate as rv

# ── 1. 纯统计函数 ──


class TestWilsonInterval:
    def test_empty_sample_returns_zero_interval(self):
        assert rv.wilson_interval(0, 0) == (0.0, 0.0)

    def test_interval_brackets_point_estimate(self):
        lo, hi = rv.wilson_interval(5, 10)
        assert lo < 0.5 < hi

    def test_zero_hits_lower_bound_is_zero(self):
        lo, hi = rv.wilson_interval(0, 10)
        assert lo == 0.0
        assert 0.0 < hi < 1.0

    def test_all_hits_upper_bound_is_one(self):
        lo, hi = rv.wilson_interval(10, 10)
        assert 0.0 < lo < 1.0
        assert hi == 1.0

    def test_bounds_never_leave_unit_interval(self):
        for k, n in [(0, 1), (1, 1), (1, 3), (7, 9), (2, 500)]:
            lo, hi = rv.wilson_interval(k, n)
            assert 0.0 <= lo <= hi <= 1.0

    def test_more_data_narrows_interval(self):
        """同样的比例，样本越大区间越窄 —— 这是它作为置信区间的底线性质。"""
        narrow_lo, narrow_hi = rv.wilson_interval(50, 100)
        wide_lo, wide_hi = rv.wilson_interval(5, 10)
        assert (narrow_hi - narrow_lo) < (wide_hi - wide_lo)


class TestDayEqualVsPooled:
    def test_day_equal_mean_averages_days_not_rows(self):
        assert rv.day_equal_mean([0.0, 1.0]) == pytest.approx(0.5)

    def test_day_equal_mean_empty_is_zero(self):
        assert rv.day_equal_mean([]) == 0.0

    def test_pooled_rate_is_row_weighted(self):
        assert rv.pooled_rate(3, 6) == pytest.approx(0.5)

    def test_pooled_rate_zero_n_is_zero(self):
        assert rv.pooled_rate(0, 0) == 0.0

    def test_two_metrics_diverge_on_imbalanced_days(self):
        """核心论点：小样本日与大样本日在两种口径下权重完全不同。

        日 1：1 票命中（日等权贡献 100%）；日 2：1/9 命中（日等权贡献 11%）。
        日等权 = 55.6%，行汇总 = 2/10 = 20% —— 差 35.6pp。
        """
        per_day = [1.0, 1 / 9]
        assert rv.day_equal_mean(per_day) == pytest.approx(0.5556, abs=1e-4)
        assert rv.pooled_rate(2, 10) == pytest.approx(0.20)
        assert rv.day_equal_mean(per_day) - rv.pooled_rate(2, 10) > 0.35


class TestBootstrapMeanCI:
    def test_empty_returns_uninformative_interval(self):
        ci = rv.bootstrap_mean_ci([])
        assert ci["n_days"] == 0
        assert ci["mde"] == math.inf

    def test_uniformly_positive_deltas_give_positive_ci(self):
        """所有日 Δ 相同且为正 → bootstrap 分布退化为单点，CI 不跨 0。"""
        ci = rv.bootstrap_mean_ci([0.1] * 8)
        assert ci["mean"] == pytest.approx(0.1)
        assert ci["lo"] > 0.0
        assert ci["mde"] == pytest.approx(0.0, abs=1e-12)
        assert rv.verdict_from_ci(ci)[0] == rv.EXIT_SUPPORTED

    def test_uniformly_zero_deltas_are_inconclusive(self):
        ci = rv.bootstrap_mean_ci([0.0] * 8)
        assert ci["lo"] == 0.0 and ci["hi"] == 0.0
        assert rv.verdict_from_ci(ci)[0] == rv.EXIT_INSUFFICIENT

    def test_uniformly_negative_deltas_verdict_worse(self):
        ci = rv.bootstrap_mean_ci([-0.1] * 8)
        assert ci["hi"] < 0.0
        assert rv.verdict_from_ci(ci)[0] == rv.EXIT_WORSE

    def test_noisy_deltas_span_zero(self):
        ci = rv.bootstrap_mean_ci([0.3, -0.3] * 20, n_boot=400)
        assert ci["lo"] < 0.0 < ci["hi"]
        assert rv.verdict_from_ci(ci)[0] == rv.EXIT_INSUFFICIENT

    def test_same_seed_is_reproducible(self):
        d = [0.1, -0.05, 0.2, 0.0, 0.15, -0.1]
        a = rv.bootstrap_mean_ci(d, n_boot=300, seed=7)
        b = rv.bootstrap_mean_ci(d, n_boot=300, seed=7)
        assert a == b

    def test_mde_shrinks_as_sample_grows(self):
        """MDE = 1.96×se：日数越多，可检出的效应越小（工具存在的意义）。"""
        small = rv.bootstrap_mean_ci([0.3, -0.3] * 5, n_boot=300, seed=1)
        large = rv.bootstrap_mean_ci([0.3, -0.3] * 100, n_boot=300, seed=1)
        assert large["mde"] < small["mde"]

    def test_mde_estimable_flag_tracks_dispersion(self):
        """2026-09-14 修复：配对差值零离散度时 MDE 退化为 0，必须标记为「不可估」。

        背景：MDE 由「观测到的配对差值」的 bootstrap 标准误得来。改动无效果时
        （或本就是空操作的基线自检）差值恒为常数 → se = 0 → mde = 0.0。
        原实现把 0.0pp 原样打印，会被读成「灵敏度无穷大」，与本工具核心判据
        「MDE 远大于 Δ 时 Δ 无法与噪声区分」直接冲突（MDE=0 时该判据永不触发）。
        故补 mde_estimable 标记，由 render 在不可估时打印 n/a。
        """
        no_op = rv.bootstrap_mean_ci([0.0] * 10)
        assert no_op["mde"] == pytest.approx(0.0, abs=1e-12)
        assert no_op["mde_estimable"] is False

        # 常数差值同样估不出噪声：CI 会退化成单点（判定仍可用），但 MDE 不可估
        constant = rv.bootstrap_mean_ci([0.1] * 10)
        assert constant["mde_estimable"] is False

        noisy = rv.bootstrap_mean_ci([0.3, -0.3] * 10, n_boot=300, seed=1)
        assert noisy["mde_estimable"] is True

        assert rv.bootstrap_mean_ci([])["mde_estimable"] is False

    def test_p_le_zero_is_fraction_of_nonpositive_bootstraps(self):
        ci = rv.bootstrap_mean_ci([0.5] * 10, n_boot=200)
        assert ci["p_le_zero"] == 0.0  # 全部重采样均值都 > 0


class TestVerdictFromCI:
    def test_no_paired_days_is_insufficient(self):
        code, text = rv.verdict_from_ci({"n_days": 0})
        assert code == rv.EXIT_INSUFFICIENT
        assert "配对" in text

    def test_positive_lower_bound_supports(self):
        assert rv.verdict_from_ci({"n_days": 5, "lo": 0.01, "hi": 0.2})[0] == rv.EXIT_SUPPORTED

    def test_negative_upper_bound_is_worse(self):
        assert rv.verdict_from_ci({"n_days": 5, "lo": -0.2, "hi": -0.01})[0] == rv.EXIT_WORSE

    def test_default_is_rejection(self):
        """默认拒绝是 B1 的核心行为：没有证据就是没有证据。"""
        assert rv.verdict_from_ci({"n_days": 40, "lo": -0.01, "hi": 0.02})[0] == rv.EXIT_INSUFFICIENT

    def test_zero_lower_bound_is_not_support(self):
        """lo 恰为 0 不算支持（必须是严格 >0）。"""
        assert rv.verdict_from_ci({"n_days": 40, "lo": 0.0, "hi": 0.02})[0] == rv.EXIT_INSUFFICIENT


# ── 2. 逐日指标 ──


class TestDayTopNHit:
    def test_picks_highest_scores_only(self):
        by_day = {"2026-01-05": [(0.1, 0.0), (0.9, 8.0), (0.5, 0.0)]}
        per_day, hits, taken = rv.day_topn_hit(by_day, top_n=1, threshold=7.0)
        assert hits == 1 and taken == 1
        assert per_day["2026-01-05"] == pytest.approx(1.0)

    def test_threshold_is_inclusive(self):
        by_day = {"d": [(1.0, 7.0)]}
        per_day, hits, _ = rv.day_topn_hit(by_day, top_n=1, threshold=7.0)
        assert hits == 1

    def test_empty_day_is_skipped(self):
        by_day = {"d1": [], "d2": [(1.0, 9.0)]}
        per_day, _hits, taken = rv.day_topn_hit(by_day, top_n=2, threshold=7.0)
        assert set(per_day) == {"d2"} and taken == 1

    def test_short_day_takes_all_available(self):
        by_day = {"d": [(1.0, 9.0)]}
        _per_day, _hits, taken = rv.day_topn_hit(by_day, top_n=5, threshold=7.0)
        assert taken == 1

    def test_per_day_rate_is_over_taken_not_over_pool(self):
        """日 1 只有 1 票（命中）→ 100%，不是 1/3 —— 分母是「取用数」。"""
        by_day = {"d1": [(3.0, 9.0)], "d2": [(1.0, 0.0), (2.0, 0.0)]}
        per_day, _hits, _taken = rv.day_topn_hit(by_day, top_n=2, threshold=7.0)
        assert per_day["d1"] == pytest.approx(1.0)
        assert per_day["d2"] == pytest.approx(0.0)


class TestDayRankIC:
    def test_skips_days_below_min_points(self):
        """3~4 点的日子直接跳过——spearman 对 <5 点返回 None，守卫必须一致。"""
        assert rv.MIN_IC_POINTS == 5
        by_day = {"small": [(float(i), float(i)) for i in range(4)]}
        assert rv.day_rank_ic(by_day) == {}

    def test_perfect_monotone_gives_ic_one(self):
        by_day = {"d": [(float(i), float(i) * 2) for i in range(6)]}
        ic = rv.day_rank_ic(by_day)
        assert ic["d"] == pytest.approx(1.0)

    def test_perfect_inverse_gives_ic_minus_one(self):
        by_day = {"d": [(float(i), -float(i)) for i in range(6)]}
        ic = rv.day_rank_ic(by_day)
        assert ic["d"] == pytest.approx(-1.0)

    def test_mixed_days_returns_only_evaluable_ones(self):
        by_day = {
            "big": [(float(i), float(i)) for i in range(6)],
            "small": [(1.0, 1.0), (2.0, 2.0)],
        }
        assert set(rv.day_rank_ic(by_day)) == {"big"}


# ── 3. override 解析 ──


class TestParseOverride:
    def test_integer_value(self):
        ov = rv.parse_override("scanner.config.MIN_SCORE=60")
        assert (ov.module, ov.attr, ov.value) == ("scanner.config", "MIN_SCORE", 60)
        assert isinstance(ov.value, int)

    def test_float_value(self):
        ov = rv.parse_override("scanner.nextday_prob.OR_MARKED=1.56")
        assert ov.value == pytest.approx(1.56)

    def test_bool_value(self):
        assert rv.parse_override("scanner.config.FLAG=true").value is True
        assert rv.parse_override("scanner.config.FLAG=false").value is False

    def test_string_value_via_quotes(self):
        assert rv.parse_override('scanner.config.MODE="fast"').value == "fast"

    def test_bare_word_falls_back_to_string(self):
        assert rv.parse_override("scanner.config.MODE=fast").value == "fast"

    def test_negative_number(self):
        assert rv.parse_override("scanner.config.X=-2.5").value == pytest.approx(-2.5)

    def test_missing_equals_raises(self):
        with pytest.raises(ValueError, match="MODULE.ATTR=VALUE"):
            rv.parse_override("scanner.config.MIN_SCORE")

    def test_missing_dot_raises(self):
        with pytest.raises(ValueError, match="模块.属性"):
            rv.parse_override("MIN_SCORE=60")

    def test_raw_is_preserved(self):
        spec = "scanner.config.MIN_SCORE=60"
        assert rv.parse_override(spec).raw == spec


# ── 4. 可见性硬校验（本模块最重要的一道防线）──


class TestCheckVisibility:
    def test_empty_override_list_is_always_clean(self):
        assert rv.check_visibility([], "stored-score") == []

    def test_stored_score_sees_nothing(self):
        """冻结分评估器对任何改动都看不见——防止「验证了没生效的改动」。"""
        problems = rv.check_visibility([rv.parse_override("scanner.config.MIN_SCORE=60")], "stored-score")
        assert len(problems) == 1
        assert "scanner.config" in problems[0]

    def test_nextday_prob_accepts_its_own_module(self):
        ov = rv.parse_override("scanner.nextday_prob.OR_MARKED=1.56")
        assert rv.check_visibility([ov], "nextday-prob") == []

    def test_nextday_prob_rejects_config_change(self):
        """典型误用：改权重却用概率排序评估器 —— 改动不体现在 _p 里。"""
        ov = rv.parse_override("scanner.config.MIN_SCORE=60")
        problems = rv.check_visibility([ov], "nextday-prob")
        assert len(problems) == 1
        assert "不在评估器 nextday-prob 的可见集合内" in problems[0]

    def test_rescore_accepts_scoring_chain_modules(self):
        ovs = [
            rv.parse_override("scanner.config.MIN_SCORE=60"),
            rv.parse_override("scanner.weights.NEW_FACE_WEIGHTS={}"),
        ]
        assert rv.check_visibility(ovs, "rescore") == []

    def test_rescore_rejects_nextday_prob_change(self):
        ov = rv.parse_override("scanner.nextday_prob.OR_MARKED=1.56")
        assert len(rv.check_visibility([ov], "rescore")) == 1

    def test_reports_every_offending_override(self):
        ovs = [
            rv.parse_override("scanner.config.MIN_SCORE=60"),
            rv.parse_override("scanner.foo.BAR=1"),
        ]
        assert len(rv.check_visibility(ovs, "nextday-prob")) == 2


class TestEvaluatorRegistry:
    def test_default_evaluator_is_registered(self):
        assert rv.DEFAULT_EVALUATOR in rv.EVALUATORS

    def test_scorers_cover_every_evaluator(self):
        """注册了评估器却没有打分器 = 运行期 KeyError，必须在这里挡住。"""
        assert set(rv.SCORERS) == set(rv.EVALUATORS)

    def test_every_evaluator_declares_sees_and_note(self):
        for name, e in rv.EVALUATORS.items():
            assert isinstance(e["sees"], frozenset), f"{name} 的 sees 必须是 frozenset"
            assert e["desc"].strip(), f"{name} 缺 desc"
            assert e["note"].strip(), f"{name} 缺 note"

    def test_visibility_set_only_contains_scanner_modules(self):
        """传播只发生在 scanner.* 内 —— 可见集合若含外部模块，校验就会失效。"""
        for name, e in rv.EVALUATORS.items():
            for mod in e["sees"]:
                assert mod.startswith("scanner."), f"{name} 的可见集合含非 scanner 模块：{mod}"


# ── 5. 宽松相等与消费方传播 ──


class TestSame:
    def test_identity_short_circuits(self):
        obj = object()
        assert rv._same(obj, obj)

    def test_equal_values(self):
        assert rv._same(1.0, 1.0)
        assert not rv._same(1.0, 2.0)

    def test_incomparable_objects_do_not_raise(self):
        class Weird:
            def __eq__(self, other):
                raise RuntimeError("boom")

        assert rv._same(Weird(), Weird()) is False


class TestPatchConsumers:
    """项目用快照式导入，只改目标模块对消费方无效 —— 传播是 override 生效的前提。"""

    @staticmethod
    def _fake_module(monkeypatch, name: str, **attrs):
        mod = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    def test_rewrites_project_local_copy_holding_the_old_value(self, monkeypatch):
        fake = self._fake_module(monkeypatch, "scanner._fake_consumer", SENTINEL=1.0)
        patched = rv._patch_consumers("scanner.rule_validate", "SENTINEL", 1.0, 9.0)
        assert "scanner._fake_consumer" in patched
        assert fake.SENTINEL == 9.0

    def test_skips_copy_that_already_diverged(self, monkeypatch):
        """只改写「仍是旧值」的副本，避免把已经独立设置的模块一起改掉。"""
        fake = self._fake_module(monkeypatch, "scanner._fake_consumer", SENTINEL=5.0)
        patched = rv._patch_consumers("scanner.rule_validate", "SENTINEL", 1.0, 9.0)
        assert "scanner._fake_consumer" not in patched
        assert fake.SENTINEL == 5.0

    def test_skips_module_without_the_attribute(self, monkeypatch):
        self._fake_module(monkeypatch, "scanner._fake_consumer", OTHER=1.0)
        patched = rv._patch_consumers("scanner.rule_validate", "SENTINEL", 1.0, 9.0)
        assert "scanner._fake_consumer" not in patched

    def test_skips_target_module_and_non_scanner_modules(self, monkeypatch):
        target = self._fake_module(monkeypatch, "scanner.rule_validate", SENTINEL=1.0)
        outside = self._fake_module(monkeypatch, "notscanner.mod", SENTINEL=1.0)
        rv._patch_consumers("scanner.rule_validate", "SENTINEL", 1.0, 9.0)
        assert target.SENTINEL == 1.0  # 目标模块由 apply_overrides 自己改
        assert outside.SENTINEL == 1.0  # 范围只限 scanner.*

    def test_patch_only_accepts_scanner_prefixed_modules(self):
        assert rv._PROPAGATE_PREFIX == "scanner."


class TestApplyAndRestore:
    def test_roundtrip_restores_original_value(self):
        """传播后必须能精确回滚，否则测试之间会互相污染。"""
        import scanner.nextday_prob as np_mod

        original = np_mod.OR_MARKED
        journal = rv.apply_overrides([rv.parse_override("scanner.nextday_prob.OR_MARKED=1.234")])
        try:
            assert pytest.approx(1.234) == np_mod.OR_MARKED
        finally:
            rv.restore_overrides(journal)
        assert original == np_mod.OR_MARKED

    def test_roundtrip_restores_propagated_copies(self):
        # rule_validate 自己 from ... import 了 NEXTDAY_HIT_THRESHOLD，
        # 用 nextday_prob 的常数无法验证传播；改用一个确实被多处导入的常量。
        import scanner.config as cfg
        import scanner.nextday_prob as np_mod
        import scanner.rule_validate as rv_mod

        original = cfg.WF_EMBARGO_DAYS
        journal = rv.apply_overrides([rv.parse_override("scanner.config.WF_EMBARGO_DAYS=7")])
        try:
            assert cfg.WF_EMBARGO_DAYS == 7
            assert rv_mod.WF_EMBARGO_DAYS == 7  # 消费方副本也被改写
        finally:
            rv.restore_overrides(journal)
        assert original == cfg.WF_EMBARGO_DAYS
        assert original == rv_mod.WF_EMBARGO_DAYS
        assert np_mod.OR_MARKED > 0  # 未受影响的模块保持原样

    def test_journal_records_spec_and_patched_modules(self):
        import scanner.config as cfg

        before = cfg.WF_EMBARGO_DAYS
        journal = rv.apply_overrides([rv.parse_override("scanner.config.WF_EMBARGO_DAYS=7")])
        try:
            rec = journal[0]
            assert rec["spec"] == "scanner.config.WF_EMBARGO_DAYS=7"
            assert rec["module"] == "scanner.config"
            assert rec["attr"] == "WF_EMBARGO_DAYS"
            assert rec["old"] == before
            assert rec["new"] == 7
            assert isinstance(rec["patched"], list)
        finally:
            rv.restore_overrides(journal)

    def test_unknown_attribute_raises(self):
        with pytest.raises(AttributeError, match="不存在"):
            rv.apply_overrides([rv.parse_override("scanner.config.NO_SUCH_CONSTANT=1")])

    def test_multiple_overrides_restore_in_reverse(self):
        import scanner.config as cfg

        o1, o2 = cfg.WF_EMBARGO_DAYS, cfg.NEXTDAY_HIT_THRESHOLD
        journal = rv.apply_overrides(
            [
                rv.parse_override("scanner.config.WF_EMBARGO_DAYS=7"),
                rv.parse_override("scanner.config.NEXTDAY_HIT_THRESHOLD=9"),
            ]
        )
        try:
            assert cfg.WF_EMBARGO_DAYS == 7 and cfg.NEXTDAY_HIT_THRESHOLD == 9
        finally:
            rv.restore_overrides(journal)
        assert o1 == cfg.WF_EMBARGO_DAYS and o2 == cfg.NEXTDAY_HIT_THRESHOLD


# ── 6. 默认参数契约 ──


def test_default_top_n_tracks_final_pick_width():
    """主指标取前 N 必须与真实买入预算一致，否则验证的不是上线口径。

    本模块首版把 2 写死，而 `FINAL_PICK_MAX` 已于 2026-09-08 放宽为 3 ——
    该测试确保这种失配不能再发生（改为引用而非复制）。
    """
    from scanner.config import FINAL_PICK_MAX

    assert rv.DEFAULT_TOP_N == FINAL_PICK_MAX


def test_parser_defaults_match_module_constants():
    """CLI 默认值必须与模块常量一致，否则命令行跑出来的口径与库调用不同。"""
    from scanner.config import DB_PATH, NEXTDAY_HIT_THRESHOLD

    args = rv.build_parser().parse_args([])
    assert args.top_n == rv.DEFAULT_TOP_N
    assert args.threshold == NEXTDAY_HIT_THRESHOLD
    assert args.train == rv.DEFAULT_TRAIN_DAYS
    assert args.test == rv.DEFAULT_TEST_DAYS
    assert args.boot == rv.DEFAULT_BOOT
    assert args.alpha == rv.DEFAULT_ALPHA
    assert args.seed == rv.DEFAULT_SEED
    assert args.evaluator == rv.DEFAULT_EVALUATOR
    assert args.db == DB_PATH
    assert args.sets == []


def test_parser_rejects_unknown_evaluator():
    with pytest.raises(SystemExit):
        rv.build_parser().parse_args(["--evaluator", "nope"])


def _minimal_result(mde_estimable: bool, flip_days: int, mde: float = 0.0) -> dict:
    """构造 render 所需的最小 result（只含渲染路径实际读取的键）。"""
    ci = {
        "mean": 0.0,
        "lo": 0.0,
        "hi": 0.0,
        "p_le_zero": 1.0,
        "se": 0.0,
        "mde": mde,
        "mde_estimable": mde_estimable,
        "n_days": 40,
    }
    metric = {
        "days": 40,
        "flip_days": flip_days,
        "base_day_equal_hit": 0.133,
        "new_day_equal_hit": 0.133,
        "delta_day_equal": 0.0,
        "base_pooled_hit": 0.136,
        "new_pooled_hit": 0.136,
        "delta_pooled": 0.0,
        "day_equal_vs_pooled_gap_base": -0.003,
        "base_day_equal_ic": 0.013,
        "new_day_equal_ic": 0.013,
        "delta_day_equal_ic": 0.0,
        "paired_days": 40,
        "ic_paired_days": 40,
        "ci": ci,
    }
    return {
        "evaluator": "nextday-prob",
        "evaluator_desc": "d",
        "evaluator_note": "n",
        "overrides": ["x=1"],
        "override_journal": [],
        "identical_output_warning": False,
        "sample_n": 2267,
        "n_windows": 4,
        "train_days": 30,
        "embargo_days": 1,
        "test_days": 10,
        "top_n": 3,
        "threshold": 7.0,
        "alpha": 0.05,
        "n_boot": 2000,
        "metrics": {"train": metric, "test": metric},
        "verdict": {
            "code": rv.EXIT_INSUFFICIENT,
            "text": "证据不足（CI 跨 0，默认拒绝）",
            "bonferroni_k": 1,
            "alpha_adjusted": 0.05,
            "note": "note",
        },
    }


class TestRenderMdeGuard:
    """2026-09-14 修复：MDE 不可估时必须打印 n/a，不得打印会被误读为 0 的数字。"""

    def test_not_estimable_prints_na_not_zero(self):
        out = rv.render(_minimal_result(mde_estimable=False, flip_days=0))
        assert "n/a" in out
        # 只对「MDE 列」断言。两种错误写法都实测过：
        #   ① `"0.0pp" not in out`      → 误伤：Δ 列本身就打 `+0.0pp`、CI 打 `[+0.0, +0.0]pp`
        #   ② `out.split("MDE")[-1]`    → 假守护：切到的是**文末说明段**（最后一个 "MDE"
        #      出现在"⚠ 本轮 MDE 显示 n/a"），不含表格行；把表格改回 0.0pp 仍为真
        # 正解：表格里 MDE 是最后一个字段、其后紧跟判定标记 → 用该组合特征串精确锁定。
        assert "n/a  ← 判定依据" in out
        assert "0.0pp  ← 判定依据" not in out

    def test_not_estimable_explains_why(self):
        out = rv.render(_minimal_result(mde_estimable=False, flip_days=0))
        assert "不可估" in out
        assert "翻转了 0/40" in out
        assert "翻转 0 天" in out

    def test_estimable_prints_numeric_mde(self):
        out = rv.render(_minimal_result(mde_estimable=True, flip_days=2, mde=0.023))
        assert "2.3pp" in out
        assert "n/a" not in out

    def test_flip_days_reported_for_estimable_case(self):
        out = rv.render(_minimal_result(mde_estimable=True, flip_days=2, mde=0.023))
        assert "翻转了 2/40" in out


def test_exit_codes_are_distinct_and_documented():
    codes = {rv.EXIT_SUPPORTED, rv.EXIT_INSUFFICIENT, rv.EXIT_WORSE, rv.EXIT_USAGE}
    assert len(codes) == 4
    assert rv.EXIT_SUPPORTED == 0
    assert "证据不足" in rv.__doc__
