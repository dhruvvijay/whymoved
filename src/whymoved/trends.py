"""Trend analysis on a monthly series: growth rates, moving averages, trend / seasonal split.

Contents
    monthly_series     turn rows (any date column) into one value per calendar month
    growth_table       value, month-on-month %, year-on-year %, 3- and 12-month averages
    seasonal_split     classical split: value = trend x seasonal x leftover (or + for additive)

How to read it, and its limits
    * Year-on-year compares the same calendar month, so seasonality cancels. Month-on-month does
      not: October is "up" on September every year. Read MoM with the seasonal index beside it.
    * The seasonal index is the average pattern over the years supplied. With three years each
      month is averaged from three points, so it is rough, and it assumes the pattern is stable.
      Festivals that move on the calendar (Diwali shifts by weeks) blur it.
    * The trend is a centred 12-month moving average, so the last 6 months have no trend value.
      Use year-on-year for the most recent months.
    * A one-off event is not separated from a trend; it appears in the leftover or bends the
      trend, depending on its size and length.
    * It needs at least 24 months for a seasonal split and refuses to guess with less.
    This describes the shape of the series; it does not forecast and does not say why.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


class TrendError(ValueError):
    """The series cannot support the requested analysis."""


def monthly_series(
    df: pd.DataFrame, value: str, date_col: str = "date", denominator: str | None = None
) -> pd.Series:
    """One value per calendar month (month-start index). With `denominator`, the monthly value
    is sum(value) / sum(denominator), a ratio of totals. Months with no rows stay as NaN gaps."""
    if value not in df.columns or date_col not in df.columns:
        raise TrendError("value or date column not found")
    month = df[date_col].dt.to_period("M").dt.to_timestamp()
    sums = df.groupby(month)[[value] + ([denominator] if denominator else [])].sum()
    series = sums[value] / sums[denominator] if denominator else sums[value]
    full = pd.date_range(series.index.min(), series.index.max(), freq="MS")
    return series.reindex(full).rename(value)


def growth_table(series: pd.Series) -> pd.DataFrame:
    """value, mom, yoy (fractions), ma3 and ma12 (trailing averages)."""
    out = pd.DataFrame({"value": series})
    out["mom"] = series.pct_change(1, fill_method=None)
    out["yoy"] = series.pct_change(12, fill_method=None)
    out["ma3"] = series.rolling(3).mean()
    out["ma12"] = series.rolling(12).mean()
    return out


@dataclass
class SeasonalSplit:
    table: pd.DataFrame        # value, trend, seasonal, leftover
    seasonal_index: pd.Series  # indexed 1..12
    model: str
    seasonal_strength: float   # 0..1: adjusted share of detrended variation the pattern explains
    years: float
    notes: list[str]

    @property
    def peak_month(self) -> int:
        return int(self.seasonal_index.idxmax())

    @property
    def low_month(self) -> int:
        return int(self.seasonal_index.idxmin())


def seasonal_split(series: pd.Series, model: str = "multiplicative") -> SeasonalSplit:
    """Classical decomposition with a 12-month cycle.

    multiplicative: value = trend * seasonal * leftover (seasonal averages to 1)
    additive:       value = trend + seasonal + leftover (seasonal averages to 0)
    The identity holds exactly wherever the trend is defined.
    """
    if model not in ("multiplicative", "additive"):
        raise TrendError("model must be 'multiplicative' or 'additive'")
    s = series.astype(float)
    if s.isna().any():
        raise TrendError("the series has missing months; fill or trim them first")
    if len(s) < 24:
        raise TrendError(f"needs at least 24 months for a seasonal split; got {len(s)}")
    if model == "multiplicative" and (s <= 0).any():
        raise TrendError("multiplicative split needs all values above zero; try 'additive'")

    # centred 2x12 moving average
    trend = s.rolling(12).mean().rolling(2).mean().shift(-6)
    detr = s / trend if model == "multiplicative" else s - trend
    month = pd.Series(s.index.month, index=s.index)
    idx = detr.groupby(month).mean()
    idx = idx / idx.mean() if model == "multiplicative" else idx - idx.mean()
    seasonal = month.map(idx).astype(float)
    leftover = s / (trend * seasonal) if model == "multiplicative" else s - trend - seasonal

    table = pd.DataFrame({"value": s, "trend": trend, "seasonal": seasonal, "leftover": leftover})
    d = detr.dropna()
    lf = leftover.dropna()
    if model == "multiplicative":
        d, lf = np.log(d), np.log(lf)
    n, k = len(d), 11  # the seasonal pattern uses 11 free numbers
    ss_total = float(((d - d.mean()) ** 2).sum())
    ss_left = float((lf**2).sum())
    # adjusted R-squared: a pattern fitted to few points always looks good, so it is penalised
    adj = 1 - (ss_left / (n - k - 1)) / (ss_total / (n - 1)) if n > k + 1 and ss_total > 0 else 0.0
    strength = float(min(1.0, max(0.0, adj)))
    notes = []
    years = len(s) / 12
    if years < 3:
        notes.append("Under three years of history: each month's seasonal figure rests on only "
                     "two points, so treat the pattern as rough.")
    notes.append("The last 6 months have no trend value (centred average); use year-on-year "
                 "for recent months.")
    return SeasonalSplit(table, idx.rename("seasonal_index"), model, strength, years, notes)


def latest_summary(series: pd.Series) -> dict[str, float]:
    """Latest month with a value: MoM, YoY, and trailing-12-month total vs the one before."""
    s = series.dropna()
    if len(s) < 13:
        raise TrendError("needs at least 13 months for a year-on-year comparison")
    last = s.index[-1]
    prev_year = last - pd.DateOffset(years=1)
    out = {"month": last, "value": float(s.iloc[-1]),
           "yoy": float(s.iloc[-1] / s[prev_year] - 1) if prev_year in s.index else float("nan"),
           "mom": float(s.iloc[-1] / s.iloc[-2] - 1)
           if s.index[-2] == last - pd.DateOffset(months=1) else float("nan")}
    if len(s) >= 24:
        out["ttm"] = float(s.iloc[-12:].sum())
        out["ttm_prev"] = float(s.iloc[-24:-12].sum())
        out["ttm_growth"] = out["ttm"] / out["ttm_prev"] - 1
    return out
