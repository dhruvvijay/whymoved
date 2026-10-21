"""Tests for the formula parser. Plain asserts; run with `python -m pytest -q`."""

import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from whymoved.decompose import STANDARD_METRICS, decompose, with_gross_profit
from whymoved.generator import make_scenario
from whymoved.metrics import FormulaError, build_metric

A_DAY, B_DAY = pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-12")


def _rejects(df, formula) -> bool:
    try:
        build_metric(df, formula)
    except FormulaError:
        return True
    return False


def _frame() -> pd.DataFrame:
    rng = np.random.default_rng(0)
    rows = [
        {"date": day, "seg": seg, "revenue": float(rng.uniform(50, 500)),
         "orders": float(rng.uniform(5, 50)), "cost": float(rng.uniform(10, 300)),
         "Net Sales": float(rng.uniform(1, 9)), "label": "x"}
        for day in (A_DAY, B_DAY) for seg in ("N", "S", "E") for _ in range(4)
    ]
    return pd.DataFrame(rows)


def test_named_metrics_match_the_standard_ones_exactly():
    sc = make_scenario("cost_rise", 7)
    data, spec = build_metric(sc.data, "margin")
    ours = decompose(data, spec, sc.period_a, sc.period_b, ["category"])
    std = decompose(with_gross_profit(sc.data), STANDARD_METRICS["margin"], sc.period_a,
                    sc.period_b, ["category"])
    assert abs(ours.total_a - std.total_a) < 1e-12 and abs(ours.total_b - std.total_b) < 1e-12
    assert spec.name == "margin" and spec.is_ratio


def test_a_ratio_is_the_ratio_of_totals_not_the_total_of_ratios():
    df = _frame()
    data, spec = build_metric(df, "revenue / orders")
    part = df[df["date"] == A_DAY]
    expected = part["revenue"].sum() / part["orders"].sum()
    got = decompose(data, spec, (A_DAY, A_DAY), (B_DAY, B_DAY), ["seg"]).total_a
    assert abs(got - expected) < 1e-12
    assert abs(got - (part["revenue"] / part["orders"]).sum()) > 1e-3  # not the naive sum


def test_derived_metrics_keep_the_sum_identity_on_random_tables():
    df = _frame()
    for formula in ("revenue - cost", "revenue / orders", "(revenue - cost) / revenue",
                    "2 * revenue + cost", "-cost", "revenue / 100", "revenue - 0.5 * cost"):
        data, spec = build_metric(df, formula)
        res = decompose(data, spec, (A_DAY, A_DAY), (B_DAY, B_DAY), ["seg"])
        assert abs(res.residual) < 1e-9, (formula, res.residual)


def test_row_wise_formula_values_are_correct():
    df = _frame()
    data, spec = build_metric(df, "revenue - 0.5 * cost + 3")
    assert np.allclose(data[spec.numerator], df["revenue"] - 0.5 * df["cost"] + 3)
    data2, spec2 = build_metric(df, "revenue / 100")
    assert np.allclose(data2[spec2.numerator], df["revenue"] / 100) and not spec2.is_ratio


def test_names_with_spaces_need_backticks_and_work():
    df = _frame()
    data, spec = build_metric(df, "`Net Sales` / orders")
    assert spec.is_ratio and np.allclose(data[spec.numerator], df["Net Sales"])
    assert _rejects(df, "Net Sales / orders")  # unticked: not a valid expression


def test_a_bare_column_is_additive_and_the_frame_is_untouched():
    df = _frame()
    before = df.copy()
    data, spec = build_metric(df, "revenue")
    assert data is df and spec.numerator == "revenue" and not spec.is_ratio
    build_metric(df, "revenue / orders")
    pd.testing.assert_frame_equal(df, before)  # helper columns went into a copy


def test_hostile_and_unsupported_formulas_are_rejected_and_never_run():
    df = _frame()
    marker = Path(tempfile.mkdtemp()) / "pwned"
    hostile = [
        f"__import__('os').system('touch {marker}')",
        f"open('{marker}', 'w')",
        "revenue.__class__",
        "revenue ** 2",
        "revenue if cost else orders",
        "[revenue]",
        "lambda: revenue",
        "revenue; orders",
        "'text'",
        "True + revenue",
        "1j * revenue",
        "revenue // orders",
        "revenue % 3",
    ]
    for formula in hostile:
        assert _rejects(df, formula), formula
    assert not marker.exists()


def test_bad_references_and_degenerate_ratios_give_clear_errors():
    df = _frame()
    assert _rejects(df, "")
    assert _rejects(df, "   ")
    assert _rejects(df, "revenue +")
    assert _rejects(df, "nope + 1")
    assert _rejects(df, "label")  # not numeric
    assert _rejects(df, "revenue / 0")
    assert _rejects(df, "revenue" + " + 1" * 200)  # over the length limit
    assert _rejects(df, "(revenue / orders) + (cost / orders)")  # sum of ratios
    assert _rejects(df, "revenue / orders * 100")  # division not at the top
    assert _rejects(df, "revenue / (orders - orders)")  # denominator sums to zero
    assert _rejects(df, "5")  # uses no columns
