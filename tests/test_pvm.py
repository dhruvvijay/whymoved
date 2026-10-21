"""Tests for the price / volume / mix bridge. Plain asserts; run with `python -m pytest -q`."""

import numpy as np
import pandas as pd

from whymoved.generator import make_scenario
from whymoved.pvm import price_volume_mix


def _table(rng, segments, days_a=14, days_b=14, drop_in_b=(), new_in_b=()):
    """Random per-day rows for two periods; some segments can vanish or appear."""
    rows = []
    start = pd.Timestamp("2026-01-05")
    for period, (offset, n) in enumerate([(0, days_a), (days_a, days_b)]):
        for d in range(n):
            for seg in segments:
                if period == 0 and seg in new_in_b:
                    continue
                if period == 1 and seg in drop_in_b:
                    continue
                units = float(rng.integers(5, 200))
                price = float(rng.uniform(10, 500)) * (1 + 0.3 * period * rng.random())
                unit_cost = price * float(rng.uniform(0.3, 0.9))
                rows.append({"date": start + pd.Timedelta(days=offset + d), "seg": seg,
                             "other": "x" if d % 2 else "y", "orders": units,
                             "revenue": units * price, "cost": units * unit_cost})
    df = pd.DataFrame(rows)
    a = (start, start + pd.Timedelta(days=days_a - 1))
    b = (start + pd.Timedelta(days=days_a), start + pd.Timedelta(days=days_a + days_b - 1))
    return df, a, b


def test_effects_sum_exactly_on_random_tables():
    for seed in range(40):
        rng = np.random.default_rng(seed)
        df, a, b = _table(rng, list("ABCDE"))
        for cost in (None, "cost"):
            br = price_volume_mix(df, a, b, ["seg"], cost=cost, n_boot=0)
            assert abs(br.residual) < 1e-6 * max(1.0, abs(br.value_a)), (seed, cost)
            assert abs(br.table["total"].sum() - br.change) < 1e-6 * max(1.0, abs(br.value_a))


def test_vanishing_and_new_segments_still_sum_exactly():
    rng = np.random.default_rng(3)
    df, a, b = _table(rng, list("ABCDE"), drop_in_b=("B",), new_in_b=("E",))
    for cost in (None, "cost"):
        br = price_volume_mix(df, a, b, ["seg"], cost=cost, n_boot=0)
        assert abs(br.residual) < 1e-6 * br.value_a
        gone = br.table.loc["B"]
        assert gone["price"] == 0 and (cost is None or gone["cost"] == 0)


def test_two_dimensions_sum_exactly():
    rng = np.random.default_rng(8)
    df, a, b = _table(rng, list("ABC"))
    br = price_volume_mix(df, a, b, ["seg", "other"], cost="cost", n_boot=0)
    assert len(br.table) == 6 and abs(br.residual) < 1e-6 * br.value_a


def test_hand_computed_example():
    # Two products. A: X 100 units @10, Y 100 units @30. B: X 150 @10, Y 100 @30 -> pure volume
    # (+50 units) and mix (X, the cheaper one, grew) with price unchanged.
    rows = []
    for day, (qx, qy) in [("2026-01-05", (100, 100)), ("2026-01-12", (150, 100))]:
        rows += [{"date": day, "seg": "X", "orders": qx, "revenue": qx * 10.0},
                 {"date": day, "seg": "Y", "orders": qy, "revenue": qy * 30.0}]
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    br = price_volume_mix(df, ("2026-01-05", "2026-01-05"), ("2026-01-12", "2026-01-12"),
                          ["seg"], n_boot=0)
    # Q_A=200, Q_B=250, avg price A = 20; volume = 50*20 = 1000
    assert abs(br.totals["volume"] - 1000) < 1e-9
    # mix_X = (150 - 0.5*250)*10 = 250 ; mix_Y = (100-125)*30 = -750 -> -500
    assert abs(br.totals["mix"] - (-500)) < 1e-9
    assert abs(br.totals["price"]) < 1e-9
    assert abs(br.change - 500) < 1e-9  # revenue 4000 -> 4500


def test_pure_price_change_has_no_volume_or_mix():
    rows = []
    for day, p in [("2026-01-05", 10.0), ("2026-01-12", 12.0)]:
        for seg in "XY":
            rows.append({"date": day, "seg": seg, "orders": 100.0, "revenue": 100.0 * p})
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    br = price_volume_mix(df, ("2026-01-05",) * 2, ("2026-01-12",) * 2, ["seg"], n_boot=0)
    assert abs(br.totals["volume"]) < 1e-9 and abs(br.totals["mix"]) < 1e-9
    assert abs(br.totals["price"] - 400) < 1e-9


def test_cost_effect_sign_and_size():
    rows = []
    for day, c in [("2026-01-05", 6.0), ("2026-01-12", 7.0)]:
        rows.append({"date": day, "seg": "X", "orders": 100.0, "revenue": 1000.0,
                     "cost": 100.0 * c})
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    br = price_volume_mix(df, ("2026-01-05",) * 2, ("2026-01-12",) * 2, ["seg"],
                          cost="cost", n_boot=0)
    assert abs(br.totals["cost"] - (-100)) < 1e-9 and abs(br.change - (-100)) < 1e-9


def test_errors_are_clear():
    rng = np.random.default_rng(1)
    df, a, b = _table(rng, list("AB"))
    for kwargs in ({"dimensions": []}, {"dimensions": ["nope"]}, {"units": "nope"}):
        args = dict(dimensions=["seg"], n_boot=0) | kwargs
        try:
            price_volume_mix(df, a, b, **args)
        except ValueError:
            continue
        raise AssertionError(kwargs)


def test_pvm_scenario_places_effects_where_planted():
    hits_price = 0
    seeds = range(401, 411)
    for seed in seeds:
        sc = make_scenario("pvm", seed)
        br = price_volume_mix(sc.data, sc.period_a, sc.period_b, ["category"], n_boot=0)
        hits_price += br.table["price"].idxmax() == "Fashion"
        assert abs(br.residual) < 1e-4 * br.value_a
        lo, hi = price_volume_mix(sc.data, sc.period_a, sc.period_b, ["category"],
                                  n_boot=200).ci["price"]
        assert lo > 0, seed  # price rise is not noise
        # Grocery is the segment whose unit growth beats the total, so its mix is positive
        assert br.table.loc["Grocery", "mix"] > 0
    assert hits_price == len(seeds)


def test_cost_rise_scenario_shows_cost_effect_in_electronics():
    for seed in range(411, 421):
        sc = make_scenario("cost_rise", seed)
        br = price_volume_mix(sc.data, sc.period_a, sc.period_b, ["category"], cost="cost",
                              n_boot=200)
        assert br.table["cost"].idxmin() == "Electronics", seed
        lo, hi = br.ci["cost"]
        assert hi < 0, seed
        assert br.table.loc["Electronics", "cost"] / br.totals["cost"] > 0.8, seed


def test_no_change_scenario_intervals_cover_zero_mostly():
    covered = total = 0
    for seed in range(421, 441):
        sc = make_scenario("none", seed)
        br = price_volume_mix(sc.data, sc.period_a, sc.period_b, ["category"], cost="cost",
                              n_boot=200)
        for name in ("mix", "price", "cost"):
            lo, hi = br.ci[name]
            covered += lo <= 0 <= hi
            total += 1
    assert covered / total > 0.75, covered / total
