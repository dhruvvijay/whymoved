"""Tests for the decomposition engine. Plain asserts; run with `python -m pytest -q`."""

import numpy as np
import pandas as pd

from whymoved.decompose import (
    STANDARD_METRICS,
    additive,
    decompose,
    find_drivers,
    ratio,
)
from whymoved.evaluate import recovery_rank
from whymoved.generator import DIMENSIONS, make_scenario

A_DAY = pd.Timestamp("2026-01-05")
B_DAY = pd.Timestamp("2026-01-12")
PERIOD_A = (A_DAY, A_DAY)
PERIOD_B = (B_DAY, B_DAY)


def _raises(exc, fn, *args, **kwargs) -> bool:
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    return False


def test_additive_hand_example_regions():
    df = pd.DataFrame(
        {
            "date": [A_DAY] * 3 + [B_DAY] * 3,
            "region": ["Delhi", "Mumbai", "Tamil Nadu"] * 2,
            "revenue": [400_000, 300_000, 300_000, 390_000, 290_000, 120_000],
        }
    )
    result = decompose(df, additive("revenue"), PERIOD_A, PERIOD_B, ["region"])
    assert result.total_a == 1_000_000 and result.total_b == 800_000
    top = result.table.iloc[0]
    assert top["region"] == "Tamil Nadu"
    assert top["contribution"] == -180_000
    assert abs(top["share_of_change"] - 0.9) < 1e-12
    assert abs(result.residual) < 1e-9
    # Excess (change beyond growing at the overall rate) sums to zero over a partition.
    assert abs(result.table["excess"].sum()) < 1e-9


def test_ratio_hand_example_pure_mix():
    """Neither channel's own rate changed, so the whole drop is a mix effect."""
    df = pd.DataFrame(
        {
            "date": [A_DAY, A_DAY, B_DAY, B_DAY],
            "channel": ["Ads", "Organic", "Ads", "Organic"],
            "orders": [20, 60, 60, 60],
            "visits": [1000, 1000, 3000, 1000],
        }
    )
    result = decompose(df, ratio("conv", "orders", "visits"), PERIOD_A, PERIOD_B, ["channel"])
    assert abs(result.total_a - 0.04) < 1e-12 and abs(result.total_b - 0.03) < 1e-12
    t = result.table.set_index("channel")
    assert abs(t["rate_effect"]).max() < 1e-12
    assert abs(t.loc["Ads", "mix_effect"] + 0.005) < 1e-12
    assert abs(t.loc["Organic", "mix_effect"] + 0.005) < 1e-12
    assert abs(result.residual) < 1e-12


def test_ratio_segment_absent_from_period_a_keeps_identity():
    df = pd.DataFrame(
        {
            "date": [A_DAY, B_DAY, B_DAY],
            "seg": ["X", "X", "Y"],
            "num": [10.0, 12.0, 30.0],
            "den": [100.0, 100.0, 200.0],
        }
    )
    result = decompose(df, ratio("r", "num", "den"), PERIOD_A, PERIOD_B, ["seg"])
    assert abs(result.residual) < 1e-12
    y = result.table.set_index("seg").loc["Y"]
    assert y["mix_effect"] == 0.0 and np.isfinite(y["rate_effect"])


def _random_table(rng: np.random.Generator, with_gaps: bool) -> pd.DataFrame:
    rows = []
    for day in (A_DAY, B_DAY):
        for region in ("N", "S", "E"):
            for channel in ("web", "app", "store"):
                if with_gaps and rng.random() < 0.25:
                    continue
                rows.append(
                    {
                        "date": day,
                        "region": region,
                        "channel": channel,
                        "num": rng.uniform(1, 100),
                        "den": rng.uniform(50, 500),
                    }
                )
        rows.append(  # guarantee both periods are non-empty
            {"date": day, "region": "N", "channel": "web", "num": 5.0, "den": 100.0}
        )
    return pd.DataFrame(rows)


def test_contributions_always_sum_to_total_change_on_random_tables():
    rng = np.random.default_rng(0)
    for i in range(60):
        df = _random_table(rng, with_gaps=bool(i % 2))
        for dims in (["region"], ["channel"], ["region", "channel"]):
            for spec in (additive("num"), ratio("r", "num", "den")):
                result = decompose(df, spec, PERIOD_A, PERIOD_B, dims)
                assert abs(result.residual) < 1e-9, (i, dims, spec.name, result.residual)


def test_bad_inputs_raise_value_error():
    df = pd.DataFrame({"date": [A_DAY, B_DAY], "region": ["N", "N"], "revenue": [1.0, 2.0]})
    spec = additive("revenue")
    assert _raises(ValueError, decompose, df, spec, PERIOD_A, PERIOD_B, ["nope"])
    assert _raises(ValueError, decompose, df, spec, PERIOD_A, PERIOD_B, [])
    empty = (pd.Timestamp("2030-01-01"), pd.Timestamp("2030-01-07"))
    assert _raises(ValueError, decompose, df, spec, empty, PERIOD_B, ["region"])


def _recovered(kind: str, metric: str, seeds: range, k: int, exact_only: bool) -> float:
    spec = STANDARD_METRICS[metric]
    ok = 0
    for seed in seeds:
        sc = make_scenario(kind, seed)
        found = find_drivers(sc.data, spec, sc.period_a, sc.period_b, DIMENSIONS, top_k=5)
        ranks = [recovery_rank(found, t, exact_only=exact_only) for t in sc.truth]
        ok += all(r is not None and r <= k for r in ranks)
    return ok / len(seeds)


def test_single_cause_is_top_ranked_exactly():
    assert _recovered("single", "revenue", range(1, 21), k=1, exact_only=True) >= 0.95


def test_noisy_cause_is_in_top_three():
    assert _recovered("noisy", "revenue", range(1, 21), k=3, exact_only=True) >= 0.9


def test_both_causes_in_top_three_when_two_are_planted():
    assert _recovered("double", "revenue", range(1, 21), k=3, exact_only=False) >= 0.85


def test_mix_only_is_found_and_labelled_as_mix_not_rate():
    sc = make_scenario("mix_only", seed=7)
    spec = STANDARD_METRICS["conversion"]
    found = find_drivers(sc.data, spec, sc.period_a, sc.period_b, DIMENSIONS, top_k=5)
    assert found[0].filters == {"channel": "Social"}

    result = decompose(sc.data, spec, sc.period_a, sc.period_b, ["channel"])
    assert result.change < 0
    total_mix = result.table["mix_effect"].sum()
    total_rate = result.table["rate_effect"].sum()
    assert abs(total_rate) < 0.2 * abs(total_mix), (total_rate, total_mix)
    assert abs(result.residual) < 1e-12
