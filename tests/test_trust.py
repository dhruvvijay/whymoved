"""Tests for the trust layer. Plain asserts; run with `python -m pytest -q`."""

import numpy as np
import pandas as pd

from whymoved.decompose import STANDARD_METRICS, additive
from whymoved.evaluate import recovery_rank
from whymoved.generator import DIMENSIONS, make_scenario
from whymoved.trust import explain_with_trust

SEEDS = range(201, 221)  # not used while choosing thresholds


def _run(kind: str, metric: str, scenario_seed: int, **kwargs):
    sc = make_scenario(kind, scenario_seed)
    spec = STANDARD_METRICS[metric]
    return sc, explain_with_trust(sc.data, spec, sc.period_a, sc.period_b, DIMENSIONS, **kwargs)


def _raises(exc, fn, *args, **kwargs) -> bool:
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    return False


def _disjoint(f1: dict, f2: dict) -> bool:
    return any(k in f2 and f2[k] != v for k, v in f1.items())


def _two_period_frame(rows_a: list[dict], rows_b: list[dict]):
    """Two 30-day periods. Rows are listed segment by segment, one row per day each."""
    a_day, b_day = pd.Timestamp("2026-01-05"), pd.Timestamp("2026-02-05")
    df = pd.DataFrame(
        [{**r, "date": a_day + pd.Timedelta(days=i % 30)} for i, r in enumerate(rows_a)]
        + [{**r, "date": b_day + pd.Timedelta(days=i % 30)} for i, r in enumerate(rows_b)]
    )
    return df, (a_day, a_day + pd.Timedelta(days=29)), (b_day, b_day + pd.Timedelta(days=29))


def test_explained_equals_the_segments_own_change_for_sums():
    sc, ex = _run("single", "revenue", 201)
    top = ex.findings[0]
    seg = sc.data
    for k, v in top.filters.items():
        seg = seg[seg[k] == v]
    in_a = (seg["date"] >= sc.period_a[0]) & (seg["date"] <= sc.period_a[1])
    in_b = (seg["date"] >= sc.period_b[0]) & (seg["date"] <= sc.period_b[1])
    expected = seg[in_b]["revenue"].sum() - seg[in_a]["revenue"].sum()
    assert abs(top.explained - expected) < 1e-6 * abs(expected)


def test_single_cause_found_exactly_with_interval_excluding_zero():
    hits = 0
    for seed in SEEDS:
        sc, ex = _run("single", "revenue", seed)
        top = ex.findings[0] if ex.findings else None
        ok = top is not None and top.filters == sc.truth[0]
        ok = ok and top.ci_high < 0 and ex.verdict in ("clear_driver", "offsetting_moves")
        hits += ok
    assert hits / len(SEEDS) >= 0.9, hits


def test_noisy_cause_is_found():
    hits = 0
    for seed in SEEDS:
        sc, ex = _run("noisy", "revenue", seed)
        hits += recovery_rank(ex.findings, sc.truth[0], exact_only=True) is not None
    assert hits / len(SEEDS) >= 0.9, hits


def test_both_planted_causes_found_exactly_or_as_a_broader_segment():
    hits = 0
    for seed in SEEDS:
        sc, ex = _run("double", "revenue", seed)
        ranks = [recovery_rank(ex.findings, t, exact_only=False) for t in sc.truth]
        hits += all(r is not None for r in ranks)
    assert hits / len(SEEDS) >= 0.9, hits


def test_findings_never_overlap():
    for kind in ("single", "double"):
        for seed in range(201, 211):
            _, ex = _run(kind, "revenue", seed)
            for i, a in enumerate(ex.findings):
                for b in ex.findings[i + 1 :]:
                    assert _disjoint(a.filters, b.filters), (kind, seed, a.label(), b.label())


def test_nothing_planted_rarely_names_a_driver():
    false_drivers = 0
    for seed in SEEDS:
        for metric in ("revenue", "conversion"):
            _, ex = _run("none", metric, seed)
            false_drivers += bool(ex.findings)
    assert false_drivers <= 2, false_drivers  # out of 40 runs


def test_mix_only_is_a_clear_driver_on_social():
    for seed in range(201, 211):
        _, ex = _run("mix_only", "conversion", seed)
        assert ex.verdict == "clear_driver", (seed, ex.verdict)
        assert ex.findings[0].filters == {"channel": "Social"}
        assert abs(ex.findings[0].mix_effect) > 3 * abs(ex.findings[0].rate_effect)


def test_bootstrap_is_reproducible_for_a_fixed_seed():
    _, a = _run("single", "revenue", 201, seed=5)
    _, b = _run("single", "revenue", 201, seed=5)
    _, c = _run("single", "revenue", 201, seed=6)
    assert a.findings[0].z == b.findings[0].z
    assert a.findings[0].ci_low == b.findings[0].ci_low
    assert a.findings[0].z != c.findings[0].z


def test_low_sample_segment_is_not_judged_and_is_reported():
    rng = np.random.default_rng(0)
    rows_a = [{"seg": s, "revenue": 100 + rng.normal()} for s in ("X", "Y") for _ in range(5)]
    rows_b = [{"seg": s, "revenue": (30 if s == "X" else 100) + rng.normal()}
              for s in ("X", "Y") for _ in range(5)]
    df, pa, pb = _two_period_frame(rows_a, rows_b)
    ex = explain_with_trust(df, additive("revenue"), pa, pb, ["seg"])
    assert ex.findings == []
    assert ex.verdict == "no_clear_driver"
    assert any("only 5 day(s)" in note for note in ex.notes), ex.notes


def test_identical_periods_give_negligible_change():
    rows = [{"seg": s, "revenue": 100.0} for s in ("X", "Y") for _ in range(30)]
    df, pa, pb = _two_period_frame(rows, rows)
    ex = explain_with_trust(df, additive("revenue"), pa, pb, ["seg"])
    assert ex.verdict == "negligible_change"
    assert ex.findings == []


def test_equal_and_opposite_moves_are_flagged_as_offsetting():
    rng = np.random.default_rng(1)
    rows_a = [{"seg": s, "revenue": 100 + rng.normal()} for s in ("X", "Y") for _ in range(30)]
    rows_b = [{"seg": s, "revenue": (50 if s == "X" else 150) + rng.normal()}
              for s in ("X", "Y") for _ in range(30)]
    df, pa, pb = _two_period_frame(rows_a, rows_b)
    ex = explain_with_trust(df, additive("revenue"), pa, pb, ["seg"])
    assert ex.verdict == "offsetting_moves", (ex.verdict, ex.notes)
    # The situation is symmetric (X falls as much as Y rises), so either may come first.
    assert ex.findings[0].filters in ({"seg": "X"}, {"seg": "Y"})


def test_bad_inputs_raise_value_error():
    sc = make_scenario("single", 201)
    spec = STANDARD_METRICS["revenue"]
    assert _raises(
        ValueError, explain_with_trust, sc.data, spec, sc.period_a, sc.period_b, ["nope"]
    )
    empty = (pd.Timestamp("2030-01-01"), pd.Timestamp("2030-01-07"))
    assert _raises(ValueError, explain_with_trust, sc.data, spec, empty, sc.period_b, DIMENSIONS)


def test_daily_aggregated_data_is_still_judged():
    """One row per day per segment (at most 28 rows per period) must not trip the sample guard."""
    hits = 0
    for seed in range(201, 206):
        sc = make_scenario("single", seed)
        agg = sc.data.groupby(["date", "region", "device"], as_index=False)["revenue"].sum()
        ex = explain_with_trust(agg, additive("revenue"), sc.period_a, sc.period_b,
                                ["region", "device"])
        hits += bool(ex.findings) and ex.findings[0].filters == sc.truth[0]
    assert hits >= 4, hits


def test_finding_that_opposes_the_net_change_is_flagged():
    rng = np.random.default_rng(2)

    def block(level, sd):
        return [{"seg": None, "revenue": level + rng.normal(0, sd)} for _ in range(30)]

    rows_a, rows_b = [], []
    for seg, a_level, b_level, sd in (("X", 100, 50, 1), ("Y", 100, 130, 60), ("Z", 100, 130, 60)):
        for r in block(a_level, sd):
            rows_a.append({**r, "seg": seg})
        for r in block(b_level, sd):
            rows_b.append({**r, "seg": seg})
    df, pa, pb = _two_period_frame(rows_a, rows_b)
    ex = explain_with_trust(df, additive("revenue"), pa, pb, ["seg"])
    assert ex.change > 0 and ex.findings[0].filters == {"seg": "X"}
    assert ex.verdict == "offsetting_moves", (ex.verdict, ex.notes)
    assert any("opposite" in note for note in ex.notes), ex.notes
