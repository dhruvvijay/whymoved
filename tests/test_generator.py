"""Tests for the synthetic generator. Plain asserts; run with `python -m pytest -q`."""

import pandas as pd

from whymoved.generator import (
    DIMENSION_WEIGHTS,
    MIN_SEGMENT_SHARE,
    PERIOD_LEN,
    SCENARIO_KINDS,
    generate_dataset,
    make_scenario,
    segment_share,
)

COLUMNS = [
    "date",
    "region",
    "channel",
    "device",
    "category",
    "visits",
    "orders",
    "revenue",
    "cost",
    "budget_revenue",
    "market_revenue",
]


def _period(df: pd.DataFrame, bounds) -> pd.DataFrame:
    return df[(df["date"] >= bounds[0]) & (df["date"] <= bounds[1])]


def _match(df: pd.DataFrame, filters: dict[str, str]) -> pd.Series:
    mask = pd.Series(True, index=df.index)
    for dim, value in filters.items():
        mask &= df[dim] == value
    return mask


def _conversion_ratio(df: pd.DataFrame, a, b, by: list[str]) -> pd.Series:
    """Conversion in period B divided by conversion in period A, per segment."""

    def conv(part: pd.DataFrame) -> pd.Series:
        g = part.groupby(by)[["orders", "visits"]].sum()
        return g["orders"] / g["visits"]

    return conv(_period(df, b)) / conv(_period(df, a))


def test_schema_and_row_count():
    df = generate_dataset(seed=1)
    n_combo = 1
    for weights in DIMENSION_WEIGHTS.values():
        n_combo *= len(weights)
    assert list(df.columns) == COLUMNS
    assert len(df) == n_combo * 84
    assert (df["orders"] <= df["visits"]).all()
    assert (df[["revenue", "cost", "budget_revenue", "market_revenue"]] >= 0).all().all()
    assert df.isna().sum().sum() == 0


def test_dimension_weights_sum_to_one():
    for name, weights in DIMENSION_WEIGHTS.items():
        assert abs(sum(weights.values()) - 1.0) < 1e-9, name


def test_same_seed_is_reproducible_and_different_seed_differs():
    a = generate_dataset(seed=5)
    b = generate_dataset(seed=5)
    c = generate_dataset(seed=6)
    pd.testing.assert_frame_equal(a, b)
    assert not a["visits"].equals(c["visits"])


def test_periods_are_equal_whole_weeks_starting_monday():
    sc = make_scenario("none", seed=3)
    for start, end in (sc.period_a, sc.period_b):
        assert start.weekday() == 0
        assert (end - start).days + 1 == PERIOD_LEN
        assert PERIOD_LEN % 7 == 0


def test_every_kind_builds_and_truth_matches_expectation():
    for kind in SCENARIO_KINDS:
        sc = make_scenario(kind, seed=11)
        assert len(sc.data) > 0
        assert sc.expect_driver == (kind != "none")
        assert sc.truth_dict()["synthetic"] is True


def test_unknown_kind_raises():
    try:
        make_scenario("bogus", seed=1)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_single_cause_only_hits_target_segment():
    sc = make_scenario("single", seed=7)
    target = sc.truth[0]
    ratios = _conversion_ratio(sc.data, sc.period_a, sc.period_b, ["region", "device"])
    key = (target["region"], target["device"])
    assert 0.4 < ratios[key] < 0.6, ratios[key]
    others = ratios.drop(index=key)
    assert others.between(0.75, 1.25).all(), others.sort_values().head(3)


def test_planted_segments_are_big_enough_to_matter():
    """The traffic-share fix: every planted drop is large relative to total revenue."""
    for seed in range(1, 21):
        for kind, floor in (("single", 0.020), ("double", 0.015)):
            sc = make_scenario(kind, seed)
            a, b = _period(sc.data, sc.period_a), _period(sc.data, sc.period_b)
            for shock in sc.shocks:
                assert segment_share(shock.filters) >= MIN_SEGMENT_SHARE
                drop = a[_match(a, shock.filters)]["revenue"].sum()
                drop -= b[_match(b, shock.filters)]["revenue"].sum()
                assert drop / a["revenue"].sum() > floor, (kind, seed, shock.label())


def test_double_has_two_causes_in_different_regions():
    sc = make_scenario("double", seed=7)
    assert len(sc.truth) == 2
    assert sc.truth[0]["region"] != sc.truth[1]["region"]


def test_mix_only_changes_overall_rate_but_not_social_rate():
    sc = make_scenario("mix_only", seed=7)
    by_channel = _conversion_ratio(sc.data, sc.period_a, sc.period_b, ["channel"])
    assert 0.9 < by_channel["Social"] < 1.1, by_channel["Social"]

    def overall(bounds):
        part = _period(sc.data, bounds)
        return part["orders"].sum() / part["visits"].sum()

    assert overall(sc.period_b) < 0.95 * overall(sc.period_a)


def test_none_scenario_total_revenue_is_stable():
    sc = make_scenario("none", seed=7)
    rev_a = _period(sc.data, sc.period_a)["revenue"].sum()
    rev_b = _period(sc.data, sc.period_b)["revenue"].sum()
    assert abs(rev_b / rev_a - 1) < 0.06


def test_budget_tracks_actuals_when_nothing_is_planted():
    sc = make_scenario("none", seed=7)
    b = _period(sc.data, sc.period_b)
    assert abs(b["revenue"].sum() / b["budget_revenue"].sum() - 1) < 0.03


def test_budget_miss_uses_budget_baseline_and_actuals_fall_short():
    sc = make_scenario("budget_miss", seed=7)
    assert sc.baseline == "budget"
    b = _period(sc.data, sc.period_b)
    shortfall = 1 - b["revenue"].sum() / b["budget_revenue"].sum()
    assert shortfall > 0.02, shortfall
    target = sc.shocks[0].filters
    seg = b[_match(b, target)]
    assert seg["revenue"].sum() < 0.65 * seg["budget_revenue"].sum()


def test_cost_rise_hits_margin_not_revenue():
    sc = make_scenario("cost_rise", seed=7)
    a, b = _period(sc.data, sc.period_a), _period(sc.data, sc.period_b)

    def margin(part):
        return 1 - part["cost"].sum() / part["revenue"].sum()

    assert margin(a) - margin(b) > 0.02, (margin(a), margin(b))
    assert abs(b["revenue"].sum() / a["revenue"].sum() - 1) < 0.06


def test_share_loss_only_hits_target_region_and_market_is_untouched():
    sc = make_scenario("share_loss", seed=7)
    a, b = _period(sc.data, sc.period_a), _period(sc.data, sc.period_b)

    def share(part):
        g = part.groupby("region")[["revenue", "market_revenue"]].sum()
        return g["revenue"] / g["market_revenue"]

    ratio = share(b) / share(a)
    region = sc.truth[0]["region"]
    assert ratio[region] < 0.85, ratio[region]
    assert ratio.drop(region).between(0.93, 1.07).all(), ratio


def test_pvm_scenario_moves_volume_price_and_mix():
    sc = make_scenario("pvm", seed=7)
    a, b = _period(sc.data, sc.period_a), _period(sc.data, sc.period_b)

    def by_cat(part):
        g = part.groupby("category")[["orders", "revenue"]].sum()
        g["price"] = g["revenue"] / g["orders"]
        return g

    ga, gb = by_cat(a), by_cat(b)
    assert gb.loc["Grocery", "orders"] > 1.3 * ga.loc["Grocery", "orders"]
    assert 1.05 < gb.loc["Fashion", "price"] / ga.loc["Fashion", "price"] < 1.15
    aov_a = ga["revenue"].sum() / ga["orders"].sum()
    aov_b = gb["revenue"].sum() / gb["orders"].sum()
    assert aov_b < aov_a  # mix toward cheap goods outweighs the Fashion price rise
