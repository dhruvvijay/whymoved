"""Tests for statement ratios, checks, DuPont split and the synthetic company generator."""

import math
import re

import numpy as np
import pandas as pd

from whymoved.narrative import CAVEAT, describe_ratios
from whymoved.statements import (
    RATIO_INFO,
    StatementError,
    check_statements,
    compute_ratios,
    dupont_change,
    make_statements,
)

from .test_narrative import CAUSAL


def _hand():
    income = pd.DataFrame(
        {"revenue": [1000.0, 1200.0], "cogs": [600.0, 720.0], "opex": [200.0, 240.0],
         "depreciation": [50.0, 60.0], "interest": [20.0, 30.0], "tax": [30.0, 45.0]},
        index=["P1", "P2"])
    balance = pd.DataFrame(
        {"cash": [100.0, 120.0], "receivables": [150.0, 200.0], "inventory": [100.0, 120.0],
         "other_current_assets": [50.0, 60.0], "fixed_assets": [600.0, 700.0],
         "payables": [100.0, 120.0], "short_term_debt": [50.0, 60.0],
         "other_current_liabilities": [50.0, 60.0], "long_term_debt": [300.0, 300.0],
         "equity": [500.0, 660.0]}, index=["P1", "P2"])
    return income, balance


def _raises(fn, *a, **k):
    try:
        fn(*a, **k)
    except StatementError:
        return True
    return False


def test_hand_computed_ratios():
    r = compute_ratios(*_hand())
    p2 = r.loc["P2"]
    expected = {
        "gross_margin": 0.4, "ebitda_margin": 0.2, "operating_margin": 0.15,
        "net_margin": 105 / 1200, "current_ratio": 500 / 240, "quick_ratio": 320 / 240,
        "cash_ratio": 0.5, "debt_to_equity": 360 / 660, "debt_to_assets": 0.3,
        "interest_coverage": 6.0, "roe": 105 / 580, "roa": 105 / 1100,
        "asset_turnover": 1200 / 1100, "equity_multiplier": 1100 / 580,
        "inventory_days": 120 / 720 * 365, "receivable_days": 200 / 1200 * 365,
        "payable_days": 120 / 720 * 365, "cash_conversion_days": 120 / 720 * 365,
    }
    assert set(expected) == set(RATIO_INFO)
    for name, value in expected.items():
        assert math.isclose(p2[name], value, rel_tol=1e-12), name
    p1 = r.loc["P1"]
    assert math.isclose(p1["gross_margin"], 0.4) and math.isclose(p1["net_margin"], 0.1)
    for name in ("roe", "roa", "asset_turnover", "equity_multiplier"):
        assert math.isnan(p1[name]), name  # no opening balance for the first period


def test_zero_denominators_give_nan_not_infinity():
    income, balance = _hand()
    income.loc["P2", "interest"] = 0.0
    income.loc["P2", "cogs"] = 0.0
    r = compute_ratios(income, balance)
    assert math.isnan(r.loc["P2", "interest_coverage"])
    assert math.isnan(r.loc["P2", "inventory_days"])
    assert not np.isinf(r.to_numpy(dtype=float)).any()


def test_roe_equals_dupont_product():
    r = compute_ratios(*_tuple(make_statements(2)))
    t = r.dropna()
    assert np.allclose(t["roe"], t["net_margin"] * t["asset_turnover"] * t["equity_multiplier"])


def _tuple(s):
    return s.income, s.balance


def test_dupont_hand_example_and_exact_sum():
    ratios = pd.DataFrame(
        {"net_margin": [0.10, 0.12], "asset_turnover": [1.0, 1.1],
         "equity_multiplier": [2.0, 2.5], "roe": [0.20, 0.12 * 1.1 * 2.5]}, index=["A", "B"])
    d = dupont_change(ratios, "A", "B")
    assert math.isclose(d.margin, 0.04) and math.isclose(d.turnover, 0.024)
    assert math.isclose(d.leverage, 0.066) and abs(d.residual) < 1e-12


def test_dupont_sums_exactly_on_generated_companies():
    for seed in range(30):
        r = compute_ratios(*_tuple(make_statements(seed, margin_squeeze_year=2023,
                                                   buyback_year=2024)))
        for a, b in [("FY2021", "FY2022"), ("FY2022", "FY2025"), ("FY2023", "FY2024")]:
            assert abs(dupont_change(r, a, b).residual) < 1e-12, (seed, a, b)


def test_dupont_refuses_first_period_and_unknown_period():
    r = compute_ratios(*_tuple(make_statements(1)))
    assert _raises(dupont_change, r, "FY2020", "FY2022")
    assert _raises(dupont_change, r, "FY2021", "FY1999")


def test_check_statements_finds_problems():
    income, balance = _hand()
    assert check_statements(income, balance) == []
    bad = balance.copy()
    bad.loc["P2", "equity"] = 700.0
    assert any("P2" in x and "differ" in x for x in check_statements(income, bad))
    assert any("missing" in x for x in check_statements(income.drop(columns="tax"), balance))
    neg = balance.copy()
    neg.loc["P1", "inventory"] = -5.0
    assert any("inventory" in x for x in check_statements(income, neg))
    assert any("different periods" in x for x in check_statements(income, balance.iloc[:1]))
    zero = income.copy()
    zero.loc["P1", "revenue"] = 0.0
    assert any("revenue" in x for x in check_statements(zero, balance))


def test_generated_statements_balance_and_stay_sensible():
    for seed in range(60):
        for kw in ({}, {"margin_squeeze_year": 2023}, {"buyback_year": 2024}):
            s = make_statements(seed, **kw)
            assert check_statements(s.income, s.balance, tolerance=1e-6) == [], (seed, kw)
            assert (s.balance[["cash", "receivables", "inventory", "payables"]] >= 0).all().all()


def test_generator_is_deterministic():
    a, b = make_statements(5), make_statements(5)
    assert a.income.equals(b.income) and a.balance.equals(b.balance)
    assert not a.income.equals(make_statements(6).income)


def test_planted_margin_squeeze_is_a_margin_effect():
    for seed in range(20):
        r = compute_ratios(*_tuple(make_statements(seed, margin_squeeze_year=2024)))
        d = dupont_change(r, "FY2023", "FY2024")
        assert d.change < 0 and d.margin / d.change > 0.7, seed
        assert r.loc["FY2024", "gross_margin"] < r.loc["FY2023", "gross_margin"] - 0.04


def test_planted_buyback_is_a_leverage_effect():
    for seed in range(20):
        r = compute_ratios(*_tuple(make_statements(seed, buyback_year=2024)))
        d = dupont_change(r, "FY2023", "FY2024")
        assert d.leverage > 0 and d.leverage > 1.5 * max(abs(d.margin), abs(d.turnover)), seed
        assert r.loc["FY2024", "debt_to_equity"] > 2 * r.loc["FY2023", "debt_to_equity"]


def test_no_event_means_no_sudden_margin_or_leverage_jump():
    for seed in range(20):
        r = compute_ratios(*_tuple(make_statements(seed, noise=0.003)))
        assert r["gross_margin"].diff().abs().max() < 0.01, seed
        assert r["debt_to_equity"].diff().abs().max() < 0.1, seed


def test_ratio_summary_is_plain_and_not_causal():
    r = compute_ratios(*_tuple(make_statements(3, buyback_year=2024)))
    text = describe_ratios(r, "FY2023", "FY2024", dupont_change(r, "FY2023", "FY2024"))
    assert "Leverage" in text and "debt to equity" in text and "no benchmark" in text
    assert not re.search(r"-0\.0+(?!\d)", text) and CAVEAT in text
    assert not CAUSAL.search(text.replace(CAVEAT, "")), text
    text.encode("ascii")
