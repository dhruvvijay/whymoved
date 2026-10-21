"""Synthetic MONTHLY data with a known trend and a known seasonal pattern (for testing trends.py).

Everything here is invented. The seasonal shape loosely resembles Indian retail (festive peak
in Oct-Nov, quieter mid-year) but is not measured from any real business.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

# Multiplicative seasonal index by calendar month (Jan..Dec); normalised to mean 1 below.
_RAW_SEASON = np.array([0.92, 0.90, 0.97, 0.98, 0.95, 0.88, 0.86, 0.94, 1.00, 1.18, 1.24, 1.12])
SEASONAL_INDEX = _RAW_SEASON / _RAW_SEASON.mean()


@dataclass
class MonthlyTruth:
    monthly_growth: float            # underlying growth per month, e.g. 0.01 = 1%
    seasonal_index: np.ndarray       # by calendar month, mean 1
    shock_month: int | None = None   # position (0-based) where a level change starts
    shock_multiplier: float = 1.0


def make_monthly(
    seed: int,
    months: int = 36,
    start: str = "2023-01-01",
    monthly_growth: float = 0.008,
    seasonal_strength: float = 1.0,
    noise: float = 0.03,
    base: float = 5_000_000.0,
    shock_month: int | None = None,
    shock_multiplier: float = 1.0,
) -> pd.DataFrame:
    """Return columns month, revenue, orders. Truth is in `.attrs["truth"]`.

    revenue = base * (1 + g)^t * season^strength * (shock) * lognormal noise.
    """
    rng = np.random.default_rng([seed, 7])
    dates = pd.date_range(start, periods=months, freq="MS")
    t = np.arange(months)
    season = SEASONAL_INDEX[dates.month.to_numpy() - 1] ** seasonal_strength
    level = base * (1 + monthly_growth) ** t
    shock = np.ones(months)
    if shock_month is not None:
        shock[shock_month:] = shock_multiplier
    eps = np.exp(rng.normal(-noise**2 / 2, noise, months))
    revenue = np.round(level * season * shock * eps, 2)
    price = 900 * (1 + 0.002) ** t
    orders = np.round(revenue / price)
    out = pd.DataFrame({"month": dates, "revenue": revenue, "orders": orders})
    out.attrs["truth"] = MonthlyTruth(monthly_growth, SEASONAL_INDEX**seasonal_strength,
                                      shock_month, shock_multiplier)
    return out
