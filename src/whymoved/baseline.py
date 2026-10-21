"""Expectations: is the change bigger than normal, and how did actuals compare with a budget?

Two tools, both honest about what they are:

trend_check
    Fits a straight line through the weekly values BEFORE period B (whole 7-day blocks, counted
    back from the first day of B so every block starts on the same weekday as B) and extends
    it across B. The result says whether B is where the recent trend would have put it.
    Whole weeks cancel weekday effects. It does NOT model yearly seasonality (Diwali, summer):
    that needs more than a year of history. Weekly values are assumed independent.
    For ratio metrics the weekly value is sum(numerator) / sum(denominator) per week and a
    period's value is the plain average of its weekly ratios (not the pooled ratio).

budget_frame / explain_budget
    "Actual versus budget" is turned into the two-period problem the engine already solves:
    the budget (or plan, or forecast) values are laid out as a stand-in period A, shifted back
    in time, and the actuals as period B. Everything else (peeling, bootstrap, verdicts) is
    reused unchanged. Days are resampled independently in the two blocks even though they are
    paired, which makes the test conservative (it can miss a small real gap, not invent one).
    A ratio metric needs a budget for its numerator AND its denominator.

Neither tool says why a change happened.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
import pandas as pd

from whymoved.decompose import MetricSpec
from whymoved.trust import TrustedExplanation, explain_with_trust

BUDGET_PATTERNS = ("budget_{c}", "{c}_budget", "plan_{c}", "{c}_plan", "target_{c}",
                   "{c}_target", "expected_{c}", "{c}_expected", "forecast_{c}", "{c}_forecast")


# --------------------------------------------------------------------------- trend check


@dataclass
class TrendCheck:
    available: bool
    reason: str = ""
    metric: str = ""
    actual_a: float = float("nan")
    actual_b: float = float("nan")
    expected_b: float = float("nan")
    actual_change_pct: float = float("nan")
    expected_change_pct: float = float("nan")
    z: float = float("nan")
    weeks_of_history: int = 0
    verdict: str = "unavailable"  # within_expected | below_expected | above_expected


def _week_values(df: pd.DataFrame, spec: MetricSpec, start: pd.Timestamp, step: int,
                 count: int, date_col: str) -> list[float] | None:
    """Weekly metric values for `count` consecutive 7-day blocks beginning at `start` and
    moving by `step` days (+7 forward, -7 backward). None if any block lacks a day."""
    values = []
    for i in range(count):
        lo = start + pd.Timedelta(days=step * i)
        hi = lo + pd.Timedelta(days=6)
        block = df[(df[date_col] >= lo) & (df[date_col] <= hi)]
        if block[date_col].nunique() < 7:
            return None
        sums = block[spec.columns].sum()
        if spec.is_ratio:
            if sums[spec.denominator] <= 0:
                return None
            values.append(float(sums[spec.numerator] / sums[spec.denominator]))
        else:
            values.append(float(sums[spec.numerator]))
    return values


def trend_check(
    df: pd.DataFrame,
    spec: MetricSpec,
    period_a,
    period_b,
    date_col: str = "date",
    min_weeks: int = 4,
    max_weeks: int = 12,
    z_threshold: float = 3.0,
) -> TrendCheck:
    """Compare period B with the value the pre-B weekly trend predicts."""
    a0, a1 = (pd.Timestamp(x).normalize() for x in period_a)
    b0, b1 = (pd.Timestamp(x).normalize() for x in period_b)
    out = TrendCheck(available=False, metric=spec.name)
    len_a, len_b = (a1 - a0).days + 1, (b1 - b0).days + 1
    if len_a != len_b or len_b % 7:
        out.reason = "needs two periods of equal length made of whole weeks"
        return out
    weeks = len_b // 7

    history: list[float] = []
    for k in range(1, max_weeks + 1):  # block k: 7k..7(k-1)+1 days before B starts
        got = _week_values(df, spec, b0 - pd.Timedelta(days=7 * k), 7, 1, date_col)
        if got is None:
            break
        history.append(got[0])
    history.reverse()  # oldest first; x = -n .. -1
    n = len(history)
    if n < min_weeks:
        out.reason = f"needs {min_weeks} complete weeks before period B; found {n}"
        return out

    actual_b_weeks = _week_values(df, spec, b0, 7, weeks, date_col)
    actual_a_weeks = _week_values(df, spec, a0, 7, weeks, date_col)
    if actual_b_weeks is None or actual_a_weeks is None:
        out.reason = "a week inside period A or B has missing days"
        return out

    x = np.arange(-n, 0, dtype=float)
    slope, intercept = np.polyfit(x, np.asarray(history), 1)
    resid = np.asarray(history) - (intercept + slope * x)
    s = float(np.sqrt((resid**2).sum() / (n - 2)))
    future = np.arange(weeks, dtype=float)
    pred = intercept + slope * future
    sxx = float(((x - x.mean()) ** 2).sum())
    var_mean = s**2 * (1 / weeks + 1 / n + (future.mean() - x.mean()) ** 2 / sxx)

    if spec.is_ratio:
        expected_b, actual_b, actual_a = (float(pred.mean()), float(np.mean(actual_b_weeks)),
                                          float(np.mean(actual_a_weeks)))
        sd = float(np.sqrt(var_mean))
    else:
        expected_b, actual_b, actual_a = (float(pred.sum()), float(np.sum(actual_b_weeks)),
                                          float(np.sum(actual_a_weeks)))
        sd = float(weeks * np.sqrt(var_mean))

    scale = max(1.0, abs(expected_b))
    if sd > 1e-12 * scale:
        z = (actual_b - expected_b) / sd
    else:  # a perfectly straight history: any real gap is infinitely surprising
        gap = actual_b - expected_b
        z = 0.0 if abs(gap) <= 1e-9 * scale else float(np.sign(gap)) * np.inf
    out.available, out.weeks_of_history = True, n
    out.actual_a, out.actual_b, out.expected_b = actual_a, actual_b, expected_b
    out.actual_change_pct = actual_b / actual_a - 1 if actual_a else float("nan")
    out.expected_change_pct = expected_b / actual_a - 1 if actual_a else float("nan")
    out.z = float(z)
    out.verdict = ("within_expected" if abs(z) < z_threshold
                   else "below_expected" if z < 0 else "above_expected")
    return out


# --------------------------------------------------------------------------- budget mode


def guess_budget_columns(columns, metric_columns) -> dict[str, str]:
    """Match metric columns to budget-like columns by name (budget_revenue, revenue_plan...)."""
    lookup = {str(c).lower(): c for c in columns}
    found: dict[str, str] = {}
    for col in metric_columns:
        base = re.escape(str(col).lower())
        for pattern in BUDGET_PATTERNS:
            cand = pattern.format(c=str(col).lower())
            if cand in lookup and re.fullmatch(pattern.format(c=base), cand):
                found[col] = lookup[cand]
                break
    return found


def budget_frame(
    df: pd.DataFrame,
    spec: MetricSpec,
    mapping: dict[str, str],
    period,
    date_col: str = "date",
) -> tuple[pd.DataFrame, tuple[pd.Timestamp, pd.Timestamp], tuple[pd.Timestamp, pd.Timestamp]]:
    """Stack budget (as a stand-in period A) and actuals (period B) for one period.

    `mapping` says which budget column goes with each metric column the spec uses.
    Returns (stacked frame, period_a, period_b) ready for the engine.
    """
    missing = [c for c in spec.columns if c not in mapping]
    if missing:
        raise ValueError(
            f"no budget column given for {missing}; a ratio needs a budget for both its parts"
        )
    for col in [*spec.columns, *(mapping[c] for c in spec.columns)]:
        if col not in df.columns:
            raise ValueError(f"column {col!r} not found")
    p0, p1 = (pd.Timestamp(x).normalize() for x in period)
    part = df[(df[date_col] >= p0) & (df[date_col] <= p1)]
    if part.empty:
        raise ValueError(f"no rows between {p0.date()} and {p1.date()}")
    length = (p1 - p0).days + 1
    shift = pd.Timedelta(days=7 * int(np.ceil((length + 1) / 7)) + 7)

    expected = part.copy()
    for col in spec.columns:
        expected[col] = part[mapping[col]].to_numpy()
    expected[date_col] = expected[date_col] - shift
    stacked = pd.concat([expected, part], ignore_index=True)
    return stacked, (p0 - shift, p1 - shift), (p0, p1)


def explain_budget(
    df: pd.DataFrame,
    spec: MetricSpec,
    mapping: dict[str, str],
    period,
    dimensions: list[str],
    date_col: str = "date",
    **kwargs,
) -> TrustedExplanation:
    """Which segments account for the gap between actuals and budget in `period`?"""
    stacked, period_a, period_b = budget_frame(df, spec, mapping, period, date_col)
    return explain_with_trust(
        stacked, spec, period_a, period_b, dimensions, date_col=date_col,
        reference="budget", **kwargs,
    )
