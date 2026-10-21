"""Tests for trend_check and budget mode. Plain asserts; run with `python -m pytest -q`."""

import numpy as np
import pandas as pd

from whymoved.baseline import (
    budget_frame,
    explain_budget,
    guess_budget_columns,
    trend_check,
)
from whymoved.decompose import STANDARD_METRICS, additive, ratio
from whymoved.generator import DIMENSIONS, make_scenario
from whymoved.metrics import FormulaError, build_budget_metric

SEEDS = range(301, 331)  # not used when choosing thresholds
REVENUE = STANDARD_METRICS["revenue"]


def _raises(exc, fn, *args, **kwargs) -> bool:
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    return False


def _daily(weekly_levels: list[float], noise: float = 0.0, seed: int = 0) -> pd.DataFrame:
    """One row per day; each week's total follows `weekly_levels` (7 equal days + noise)."""
    rng = np.random.default_rng(seed)
    start = pd.Timestamp("2026-01-05")
    rows = []
    for w, level in enumerate(weekly_levels):
        for d in range(7):
            rows.append({"date": start + pd.Timedelta(days=7 * w + d),
                         "revenue": level / 7 * (1 + rng.normal(0, noise)),
                         "orders": 10.0 + rng.normal(0, noise)})
    return pd.DataFrame(rows)


def _periods(weeks_total: int, length_weeks: int = 2):
    start = pd.Timestamp("2026-01-05")
    b1 = start + pd.Timedelta(days=7 * weeks_total - 1)
    b0 = b1 - pd.Timedelta(days=7 * length_weeks - 1)
    a1 = b0 - pd.Timedelta(days=1)
    a0 = a1 - pd.Timedelta(days=7 * length_weeks - 1)
    return (a0, a1), (b0, b1)


# ------------------------------------------------------------------ trend check


def test_a_perfectly_straight_trend_is_exactly_as_expected():
    df = _daily([100 + 10 * w for w in range(10)])
    a, b = _periods(10)
    t = trend_check(df, REVENUE, a, b)
    assert t.available and t.verdict == "within_expected"
    assert abs(t.expected_b - t.actual_b) < 1e-6 and t.z == 0.0
    assert t.weeks_of_history == 8
    assert abs(t.expected_change_pct - t.actual_change_pct) < 1e-9


def test_a_jump_above_a_steady_history_is_flagged():
    levels = [100.0] * 8 + [130.0, 130.0]
    df = _daily(levels, noise=0.01)
    t = trend_check(df, REVENUE, *_periods(10))
    assert t.verdict == "above_expected" and t.z > 3
    assert abs(t.actual_change_pct - 0.30) < 0.05


def test_trend_check_on_generator_data():
    def share(kind, metric, verdict_is_flagged):
        flagged = 0
        for seed in SEEDS:
            sc = make_scenario(kind, seed)
            t = trend_check(sc.data, STANDARD_METRICS[metric], sc.period_a, sc.period_b)
            assert t.available, t.reason
            flagged += t.verdict != "within_expected"
        return flagged / len(SEEDS) if verdict_is_flagged else 1 - flagged / len(SEEDS)

    assert share("none", "revenue", False) >= 0.9      # normal weeks are not flagged
    assert share("none", "conversion", False) >= 0.9
    assert share("mix_only", "conversion", True) >= 0.95
    assert share("double", "revenue", True) >= 0.85


def test_ratio_trend_uses_weekly_ratios():
    sc = make_scenario("mix_only", 7)
    t = trend_check(sc.data, STANDARD_METRICS["conversion"], sc.period_a, sc.period_b)
    assert t.verdict == "below_expected" and t.actual_change_pct < -0.05


def test_trend_check_says_why_it_is_unavailable():
    df = _daily([100.0] * 10)
    a, b = _periods(10)
    unequal = trend_check(df, REVENUE, a, (b[0], b[1] - pd.Timedelta(days=3)))
    assert not unequal.available and "whole weeks" in unequal.reason
    short = trend_check(df, REVENUE, *_periods(5))  # only 3 clean weeks before B
    assert not short.available and "complete weeks" in short.reason
    near = df[df["date"] != pd.Timestamp("2026-02-14")]  # hole 3 weeks before B: chain too short
    assert not trend_check(near, REVENUE, a, b).available
    old_hole = df[df["date"] != pd.Timestamp("2026-01-20")]  # hole in week 3 of 10: history stops
    kept = trend_check(old_hole, REVENUE, a, b)              # there, so 5 clean weeks still count
    assert kept.available and kept.weeks_of_history == 5
    inside = df[df["date"] != b[0] + pd.Timedelta(days=2)]  # a missing day inside B
    assert not trend_check(inside, REVENUE, a, b).available


# ------------------------------------------------------------------ budget columns and frame


def test_budget_columns_are_matched_by_name():
    cols = ["date", "revenue", "budget_revenue", "orders", "Orders_Plan", "visits",
            "target_visits", "cost", "budgeting_cost"]
    found = guess_budget_columns(cols, ["revenue", "orders", "visits", "cost"])
    assert found == {"revenue": "budget_revenue", "orders": "Orders_Plan",
                     "visits": "target_visits"}


def test_budget_frame_lays_budget_out_as_a_separate_earlier_period():
    sc = make_scenario("budget_miss", 7)
    stacked, a, b = budget_frame(sc.data, REVENUE, {"revenue": "budget_revenue"}, sc.period_b)
    in_b = sc.data[(sc.data["date"] >= b[0]) & (sc.data["date"] <= b[1])]
    assert len(stacked) == 2 * len(in_b)
    assert a[1] < b[0] and (a[1] - a[0]) == (b[1] - b[0])  # earlier, same length, no overlap
    sa = stacked[(stacked["date"] >= a[0]) & (stacked["date"] <= a[1])]
    sb = stacked[(stacked["date"] >= b[0]) & (stacked["date"] <= b[1])]
    assert abs(sa["revenue"].sum() - in_b["budget_revenue"].sum()) < 1e-6
    assert abs(sb["revenue"].sum() - in_b["revenue"].sum()) < 1e-6
    assert (sa["date"].dt.weekday.value_counts() == sb["date"].dt.weekday.value_counts()).all()


def test_budget_frame_errors_are_clear():
    sc = make_scenario("budget_miss", 7)
    need_both = ratio("conv", "orders", "visits")
    assert _raises(ValueError, budget_frame, sc.data, need_both, {"orders": "budget_revenue"},
                   sc.period_b)  # no budget for the denominator
    assert _raises(ValueError, budget_frame, sc.data, REVENUE, {"revenue": "nope"}, sc.period_b)
    far = (pd.Timestamp("2030-01-01"), pd.Timestamp("2030-01-28"))
    assert _raises(ValueError, budget_frame, sc.data, REVENUE, {"revenue": "budget_revenue"}, far)


# ------------------------------------------------------------------ budget mode end to end


def test_budget_miss_is_traced_to_the_planted_segment():
    hits = 0
    for seed in SEEDS:
        sc = make_scenario("budget_miss", seed)
        ex = explain_budget(sc.data, REVENUE, {"revenue": "budget_revenue"}, sc.period_b,
                            DIMENSIONS)
        hits += bool(ex.findings) and ex.findings[0].filters == sc.truth[0]
        assert ex.reference == "budget" and ex.total_b < ex.total_a
    assert hits / len(SEEDS) >= 0.95, hits


def test_on_budget_data_names_no_driver():
    false_drivers = 0
    for seed in SEEDS:
        sc = make_scenario("none", seed)
        ex = explain_budget(sc.data, REVENUE, {"revenue": "budget_revenue"}, sc.period_b,
                            DIMENSIONS)
        false_drivers += bool(ex.findings)
    assert false_drivers <= 1, false_drivers


def test_a_uniformly_optimistic_budget_is_not_blamed_on_a_segment():
    """Planners are usually biased. A miss that hits every segment alike has no culprit."""
    false_drivers = 0
    for seed in range(301, 316):
        sc = make_scenario("none", seed)
        data = sc.data.copy()
        data["budget_revenue"] *= 1.10
        ex = explain_budget(data, REVENUE, {"revenue": "budget_revenue"}, sc.period_b,
                            DIMENSIONS)
        assert ex.total_b < 0.95 * ex.total_a  # a real 9% shortfall overall
        false_drivers += bool(ex.findings)
    assert false_drivers <= 1, false_drivers


def test_a_budget_that_is_wrong_in_one_region_points_at_that_region():
    hits = 0
    for seed in range(301, 316):
        sc = make_scenario("none", seed)
        data = sc.data.copy()
        data.loc[data["region"] == "Mumbai", "budget_revenue"] *= 1.25
        ex = explain_budget(data, REVENUE, {"revenue": "budget_revenue"}, sc.period_b,
                            DIMENSIONS)
        hits += bool(ex.findings) and ex.findings[0].filters == {"region": "Mumbai"}
    assert hits >= 14, hits


def test_budget_for_a_derived_additive_metric():
    sc = make_scenario("budget_miss", 7)
    data = sc.data.copy()
    data["budget_orders"] = data["orders"]  # orders were on plan; only revenue missed
    spec = additive("orders")
    ex = explain_budget(data, spec, {"orders": "budget_orders"}, sc.period_b, DIMENSIONS)
    assert ex.verdict == "negligible_change" and ex.findings == []


# ------------------------------------------------------------------ budgets for derived metrics

MARGIN_MAP = {"revenue": "budget_revenue", "cost": "budget_cost"}


def _with_unplanned_cost_rise(sc) -> pd.DataFrame:
    """Budget cost = the cost the plan expected, i.e. before the planted Electronics rise."""
    data = sc.data.copy()
    in_b = data["date"] >= sc.period_b[0]
    unplanned = (data["category"] == "Electronics") & in_b
    data["budget_cost"] = data["cost"] / np.where(unplanned, 1.12, 1.0)
    return data


def test_margin_against_budget_points_at_the_unplanned_cost_rise():
    hits = 0
    for seed in range(301, 316):
        sc = make_scenario("cost_rise", seed)
        data, spec, helper = build_budget_metric(_with_unplanned_cost_rise(sc), "margin",
                                                 MARGIN_MAP)
        ex = explain_budget(data, spec, helper, sc.period_b, DIMENSIONS)
        assert ex.total_b < ex.total_a  # margin came in below plan
        hits += bool(ex.findings) and ex.findings[0].filters.get("category") == "Electronics"
    assert hits >= 14, hits


def test_margin_on_plan_names_no_driver():
    false_drivers = 0
    for seed in range(301, 316):
        sc = make_scenario("none", seed)
        data = sc.data.copy()
        data["budget_cost"] = data["cost"]
        data, spec, helper = build_budget_metric(data, "margin", MARGIN_MAP)
        false_drivers += bool(explain_budget(data, spec, helper, sc.period_b, DIMENSIONS).findings)
    assert false_drivers <= 1, false_drivers


def test_budget_metric_helper_columns_are_the_formula_applied_to_budget_columns():
    sc = make_scenario("cost_rise", 301)
    data = _with_unplanned_cost_rise(sc)
    out, spec, helper = build_budget_metric(data, "revenue - cost", MARGIN_MAP)
    assert np.allclose(out[helper[spec.numerator]], data["budget_revenue"] - data["budget_cost"])
    out, spec, helper = build_budget_metric(data, "margin", MARGIN_MAP)
    assert np.allclose(out[helper[spec.numerator]], data["budget_revenue"] - data["budget_cost"])
    assert np.allclose(out[helper[spec.denominator]], data["budget_revenue"])
    out, spec, helper = build_budget_metric(data, "revenue", MARGIN_MAP)
    assert out is data and helper == {"revenue": "budget_revenue"}


def test_budget_metric_errors_are_clear():
    sc = make_scenario("cost_rise", 301)
    data = _with_unplanned_cost_rise(sc)
    for mapping in ({"revenue": "budget_revenue"},                    # cost has no budget
                    {"revenue": "budget_revenue", "cost": "missing"},  # budget column absent
                    {"revenue": "budget_revenue", "cost": "region"}):  # budget column not numeric
        assert _raises(FormulaError, build_budget_metric, data, "margin", mapping), mapping
