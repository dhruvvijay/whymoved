"""Tests for market-share bridge. Plain asserts; run with `python -m pytest -q`."""

import numpy as np
import pandas as pd

from whymoved.generator import make_scenario
from whymoved.market import market_share_bridge


def _table(rng, segs, new_in_b=(), drop_in_b=(), days=14):
    rows = []
    start = pd.Timestamp("2026-01-05")
    for period in (0, 1):
        for d in range(days):
            for seg in segs:
                if (period == 0 and seg in new_in_b) or (period == 1 and seg in drop_in_b):
                    continue
                m = float(rng.uniform(1000, 5000)) * (1 + 0.2 * period * rng.random())
                share = float(rng.uniform(0.05, 0.2))
                rows.append({"date": start + pd.Timedelta(days=period * days + d), "seg": seg,
                             "revenue": m * share, "market_revenue": m})
    a = (start, start + pd.Timedelta(days=days - 1))
    b = (start + pd.Timedelta(days=days), start + pd.Timedelta(days=2 * days - 1))
    return pd.DataFrame(rows), a, b


def test_parts_sum_exactly_on_random_tables():
    for seed in range(30):
        df, a, b = _table(np.random.default_rng(seed), list("ABCDE"))
        br = market_share_bridge(df, a, b, ["seg"], n_boot=0)
        assert abs(br.residual) < 1e-6 * br.revenue_a, seed
        assert abs(br.table["total"].sum() - (br.revenue_b - br.revenue_a)) < 1e-6 * br.revenue_a


def test_new_and_vanishing_segments_sum_exactly():
    df, a, b = _table(np.random.default_rng(4), list("ABCDE"), new_in_b=("E",), drop_in_b=("B",))
    br = market_share_bridge(df, a, b, ["seg"], n_boot=0)
    assert abs(br.residual) < 1e-6 * br.revenue_a


def test_share_effect_equals_market_b_times_share_change():
    df, a, b = _table(np.random.default_rng(2), list("ABC"))
    t = market_share_bridge(df, a, b, ["seg"], n_boot=0).table
    expected = t["market_b"] * (t["share_b"] - t["share_a"])
    assert np.allclose(t["share"], expected, rtol=1e-9, atol=1e-6)


def test_hand_computed_example():
    # Market: X 100 -> 120, Y 100 -> 100. Us: X 10 -> 12 (share held), Y 20 -> 10 (share halved)
    rows = []
    weeks = [("2026-01-05", [(10, 100), (20, 100)]), ("2026-01-12", [(12, 120), (10, 100)])]
    for day, vals in weeks:
        for seg, (r, m) in zip("XY", vals, strict=True):
            rows.append({"date": day, "seg": seg, "revenue": float(r), "market_revenue": float(m)})
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    br = market_share_bridge(df, ("2026-01-05",) * 2, ("2026-01-12",) * 2, ["seg"], n_boot=0)
    # total market 200 -> 220: g = 10%. R_A = 30 -> R_B = 22.
    assert abs(br.totals["market"] - 3.0) < 1e-9
    # exposure: X: 10*(0.2-0.1)=1 ; Y: 20*(0-0.1)=-2  -> -1
    assert abs(br.totals["exposure"] - (-1.0)) < 1e-9
    # share: X 120*(0.1-0.1)=0 ; Y 100*(0.1-0.2)=-10
    assert abs(br.totals["share"] - (-10.0)) < 1e-9
    assert abs(br.table.loc["Y", "share"] - (-10.0)) < 1e-9


def test_growing_exactly_with_the_market_has_no_share_effect():
    rows = []
    for day, m in [("2026-01-05", 100.0), ("2026-01-12", 130.0)]:
        for seg in "XY":
            rows.append({"date": day, "seg": seg, "revenue": m * 0.1, "market_revenue": m})
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    br = market_share_bridge(df, ("2026-01-05",) * 2, ("2026-01-12",) * 2, ["seg"], n_boot=0)
    assert abs(br.totals["share"]) < 1e-9 and abs(br.totals["exposure"]) < 1e-9
    assert abs(br.share_a - br.share_b) < 1e-12


def test_errors_are_clear():
    df, a, b = _table(np.random.default_rng(1), list("AB"))
    for kwargs in ({"dimensions": []}, {"dimensions": ["nope"]}, {"market": "nope"}):
        args = dict(dimensions=["seg"], n_boot=0) | kwargs
        try:
            market_share_bridge(df, a, b, **args)
        except ValueError:
            continue
        raise AssertionError(kwargs)


def test_share_loss_scenario_finds_the_region_and_interval_excludes_zero():
    for seed in range(501, 516):
        sc = make_scenario("share_loss", seed)
        br = market_share_bridge(sc.data, sc.period_a, sc.period_b, ["region"], n_boot=200)
        assert br.table.index[0] == sc.shocks[0].filters["region"], seed
        lo, hi = br.ci["share"]
        assert hi < 0, seed
        assert br.table["share"].iloc[0] / br.totals["share"] > 0.5, seed


def _true_null(seed):
    """Scenario data where our share of each region is constant by construction: the market is
    the shock-free expectation (budget) divided by a fixed share, with its own noise."""
    sc = make_scenario("none", seed)
    df = sc.data.copy()
    shares = {r: 0.05 + 0.01 * i for i, r in enumerate(sorted(df["region"].unique()))}
    rng = np.random.default_rng([seed, 99])
    noise = np.exp(rng.normal(-0.03**2 / 2, 0.03, len(df)))
    df["market_revenue"] = df["budget_revenue"] / df["region"].map(shares) * noise
    return sc, df


def test_constant_share_data_interval_covers_zero_at_close_to_nominal_rate():
    covered = 0
    seeds = range(521, 561)
    for seed in seeds:
        sc, df = _true_null(seed)
        lo, hi = market_share_bridge(df, sc.period_a, sc.period_b, ["region"],
                                     n_boot=200).ci["share"]
        covered += lo <= 0 <= hi
    assert covered / len(seeds) >= 0.88, covered
