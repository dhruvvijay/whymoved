"""Tests for monthly generator and trend analysis."""

import numpy as np
import pandas as pd

from whymoved.monthly import SEASONAL_INDEX, make_monthly
from whymoved.narrative import CAVEAT, describe_trend
from whymoved.trends import (
    TrendError,
    growth_table,
    latest_summary,
    monthly_series,
    seasonal_split,
)

from .test_narrative import CAUSAL


def _series(values, start="2023-01-01"):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq="MS"),
                     dtype=float)


def _raises(fn, *a, **k):
    try:
        fn(*a, **k)
    except TrendError:
        return True
    return False


def test_growth_table_hand_computed():
    s = _series([100, 110, 121] + [100] * 10 + [120, 99])
    g = growth_table(s)
    assert abs(g["mom"].iloc[1] - 0.10) < 1e-12
    assert abs(g["yoy"].iloc[12] - 0.0) < 1e-12           # month 13 (100) vs month 1 (100)
    assert abs(g["yoy"].iloc[13] - (120 / 110 - 1)) < 1e-12
    assert abs(g["ma3"].iloc[2] - (100 + 110 + 121) / 3) < 1e-12
    assert np.isnan(g["ma3"].iloc[1])


def test_monthly_series_sums_and_ratio_and_gaps():
    days = pd.date_range("2026-01-01", "2026-04-30", freq="D")
    df = pd.DataFrame({"date": days, "rev": 10.0, "units": 2.0})
    df = df[df["date"].dt.month != 3]  # no March at all
    s = monthly_series(df, "rev")
    assert s["2026-01-01"] == 310 and s["2026-02-01"] == 280 and np.isnan(s["2026-03-01"])
    r = monthly_series(df, "rev", denominator="units")
    assert abs(r["2026-02-01"] - 5.0) < 1e-12  # ratio of sums, not mean of ratios


def test_split_identity_is_exact_both_models():
    s = make_monthly(3).set_index("month")["revenue"]
    for model in ("multiplicative", "additive"):
        t = seasonal_split(s, model).table.dropna()
        if model == "multiplicative":
            rebuilt = t["trend"] * t["seasonal"] * t["leftover"]
        else:
            rebuilt = t["trend"] + t["seasonal"] + t["leftover"]
        assert np.allclose(rebuilt, t["value"], rtol=1e-12)


def test_seasonal_index_normalised():
    s = make_monthly(4).set_index("month")["revenue"]
    assert abs(seasonal_split(s).seasonal_index.mean() - 1) < 1e-12
    assert abs(seasonal_split(s, "additive").seasonal_index.mean()) < 1e-6 * s.mean()


def test_recovers_planted_seasonal_pattern():
    worst = 0.0
    for seed in range(601, 621):
        s = make_monthly(seed).set_index("month")["revenue"]
        sp = seasonal_split(s)
        worst = max(worst, float(np.abs(sp.seasonal_index.values - SEASONAL_INDEX).max()))
        assert np.corrcoef(sp.seasonal_index.values, SEASONAL_INDEX)[0, 1] > 0.95, seed
        assert sp.peak_month in (10, 11), seed
        assert sp.low_month in (6, 7), seed
    assert worst < 0.12, worst


def test_no_seasonality_gives_weak_strength_and_strong_gives_high():
    flat = [seasonal_split(make_monthly(s, seasonal_strength=0.0).set_index("month")["revenue"]
                           ).seasonal_strength for s in range(631, 641)]
    strong = [seasonal_split(make_monthly(s).set_index("month")["revenue"]).seasonal_strength
              for s in range(631, 641)]
    assert max(flat) < 0.6 and min(strong) > 0.75, (max(flat), min(strong))


def test_trend_tracks_planted_growth():
    s = make_monthly(7, monthly_growth=0.01, noise=0.01).set_index("month")["revenue"]
    tr = seasonal_split(s).table["trend"].dropna()
    growth = (tr.iloc[-1] / tr.iloc[0]) ** (1 / (len(tr) - 1)) - 1
    assert abs(growth - 0.01) < 0.002, growth


def test_refuses_short_gappy_or_nonpositive_series():
    s = make_monthly(1).set_index("month")["revenue"]
    assert _raises(seasonal_split, s.iloc[:20])
    gappy = s.copy()
    gappy.iloc[5] = np.nan
    assert _raises(seasonal_split, gappy)
    neg = s.copy()
    neg.iloc[3] = -1
    assert _raises(seasonal_split, neg)
    assert _raises(seasonal_split, s, "nonsense")
    assert _raises(latest_summary, s.iloc[:10])


def test_latest_summary_values():
    s = _series(list(range(100, 136)))  # 36 months
    ls = latest_summary(s)
    assert abs(ls["yoy"] - (135 / 123 - 1)) < 1e-12
    assert abs(ls["mom"] - (135 / 134 - 1)) < 1e-12
    assert abs(ls["ttm"] - s.iloc[-12:].sum()) < 1e-9


def test_level_drop_shows_in_yoy():
    d = make_monthly(9, shock_month=30, shock_multiplier=0.8, noise=0.01)
    s = d.set_index("month")["revenue"]
    assert growth_table(s)["yoy"].iloc[-1] < -0.05


def test_trend_text_is_plain_and_not_causal():
    s = make_monthly(11).set_index("month")["revenue"]
    text = describe_trend(seasonal_split(s), latest_summary(s), label="revenue")
    assert "November" in text and "July" in text and CAVEAT in text
    assert not CAUSAL.search(text.replace(CAVEAT, "")), text
    text.encode("ascii")
