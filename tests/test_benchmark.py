"""Tests for the benchmark helpers (small runs only; the full run is scripts/run_benchmark.py)."""

import math

from whymoved.benchmark import (
    KindResult,
    Run,
    placebo_check,
    render_report,
    run_kind,
    scaled_frame,
    sensitivity,
    wilson,
)
from whymoved.decompose import STANDARD_METRICS
from whymoved.generator import DIMENSIONS, SCENARIO_KINDS, make_scenario


def test_wilson_interval_hand_values():
    p, lo, hi = wilson(50, 100)
    assert p == 0.5 and abs(lo - 0.4038) < 1e-3 and abs(hi - 0.5962) < 1e-3
    p, lo, hi = wilson(0, 40)
    assert p == 0 and lo == 0 and 0.08 < hi < 0.10
    assert all(math.isnan(x) for x in wilson(0, 0))
    assert wilson(10, 10)[2] == 1.0


def test_every_kind_runs_and_outcomes_have_the_expected_keys():
    for kind in SCENARIO_KINDS:
        res = run_kind(kind, n=2, n_boot=100)
        assert res.n == 2 and all(r.kind == kind and r.outcome for r in res.runs)


def test_unknown_kind_is_rejected():
    try:
        run_kind("nope", n=1)
    except ValueError:
        return
    raise AssertionError


def test_benchmark_seeds_are_not_the_ones_used_in_tests():
    from whymoved.benchmark import FIRST_SEED
    assert FIRST_SEED >= 20000


def test_scaled_frame_row_count_and_columns():
    base = make_scenario("single", 1).data
    big = scaled_frame(base, 3 * len(base))
    assert len(big) == 3 * len(base) and list(big.columns) == list(base.columns)
    first, second = big.iloc[: len(base)], big.iloc[len(base): 2 * len(base)]
    assert not first["revenue"].reset_index(drop=True).equals(
        second["revenue"].reset_index(drop=True))


def test_placebo_check_runs_and_validates_window():
    sc = make_scenario("none", 7)
    import pandas as pd
    window = (sc.period_a[0] - pd.Timedelta(days=28), sc.period_b[1])
    out = placebo_check(sc.data, STANDARD_METRICS["revenue"], DIMENSIONS, window,
                        trials=4, n_boot=100)
    assert out["trials"] == 4 and 0 <= out["false_driver_runs"] <= 4
    odd = (window[0], window[1] - pd.Timedelta(days=7))
    try:
        placebo_check(sc.data, STANDARD_METRICS["revenue"], DIMENSIONS, odd, trials=1)
    except ValueError:
        return
    raise AssertionError("an odd number of weeks must be rejected")


def test_a_real_change_is_reported_so_a_reported_driver_means_something():
    """Counterpart of the placebo: with a real planted change the engine does report a driver,
    so "no false alarms on placebo" is not just "never reports anything"."""
    from whymoved.trust import explain_with_trust
    sc = make_scenario("single", 3)
    ex = explain_with_trust(sc.data, STANDARD_METRICS["revenue"], sc.period_a, sc.period_b,
                            DIMENSIONS, n_boot=200)
    assert ex.findings


def test_sensitivity_returns_one_row_per_setting_and_counts_add_up():
    rows = sensitivity(multipliers=(0.5, 0.95), noises=(0.05,), n=4, n_boot=100)
    assert len(rows) == 2
    for r in rows:
        assert r["found"] + r["said_none"] + r["wrong_first"] == r["n"]
    big = next(r for r in rows if abs(r["drop"] - 0.5) < 1e-9)
    assert big["found"] >= 3


def test_report_lists_failures_and_states_its_limits():
    ok = Run("single", 1, {"top1_any": True, "top1_exact": True, "all_found": True,
                           "wrong_first": False, "missed": False}, "region=A", "region=A",
              "clear_driver")
    bad = Run("single", 2, {"top1_any": False, "top1_exact": False, "all_found": False,
                            "wrong_first": True, "missed": False}, "region=A", "region=B",
              "clear_driver")
    res = KindResult("single", [ok, bad])
    text = render_report({"single": res}, timings=[{"rows": 1000, "seconds": 0.5}],
                         placebo={"trials": 10, "false_driver_runs": 1, "rate": 0.1,
                                  "ci": (0.0, 0.4)})
    assert "Failure gallery" in text and "| 2 | region=A | region=B |" in text
    assert "Synthetic data only" in text and "false alarm" in text and "1,000" in text
    clean = render_report({"single": KindResult("single", [ok])})
    assert "No failures" in clean
