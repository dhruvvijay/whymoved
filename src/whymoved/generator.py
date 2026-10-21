"""Synthetic e-commerce data generator with planted, known causes.

ALL DATA PRODUCED HERE IS SYNTHETIC. It exists so the WhyMoved engine can be
benchmarked against ground truth: we decide the cause, then check whether the
engine finds it.

Time layout (84 days, starting on a Monday):
    days  0-27  warm-up (unused by analyses)
    days 28-55  period A (baseline, 4 full weeks)
    days 56-83  period B (comparison, 4 full weeks; shocks apply here only)
Both periods are whole weeks, so weekly seasonality cancels out.

Columns: date, region, channel, device, category, visits, orders, revenue,
cost, budget_revenue, market_revenue.
    * Each order is one unit, so orders = volume and revenue / orders = price.
    * budget_revenue is the noise-free, shock-free expectation (what a planner
      would have budgeted, including normal growth).
    * market_revenue is the whole market in that row's region and category, derived
      from the same shock-free expectation, so shocks never move the market.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import date
from typing import Literal

import numpy as np
import pandas as pd

Effect = Literal["visits", "conversion", "aov", "cost"]
Metric = Literal["revenue", "conversion", "margin", "market_share"]
Baseline = Literal["previous_period", "budget"]

START_DATE = date(2026, 1, 5)  # a Monday
N_DAYS = 84
A_START, B_START = 28, 56
PERIOD_LEN = 28

DAILY_VISITS = 40_000
BASE_CONVERSION = 0.03

# Planted region x device segments must carry at least this share of traffic, so a
# planted cause moves the total by roughly 3% or more instead of vanishing in noise.
MIN_SEGMENT_SHARE = 0.06

# Share of traffic per value; each dimension sums to 1.
DIMENSION_WEIGHTS: dict[str, dict[str, float]] = {
    "region": {
        "Delhi": 0.22,
        "Mumbai": 0.22,
        "Bengaluru": 0.20,
        "Tamil Nadu": 0.14,
        "West Bengal": 0.12,
        "Gujarat": 0.10,
    },
    "channel": {"Organic": 0.35, "Paid Search": 0.30, "Social": 0.20, "Email": 0.15},
    "device": {"Mobile": 0.60, "Desktop": 0.32, "Tablet": 0.08},
    "category": {"Electronics": 0.25, "Fashion": 0.30, "Home": 0.20, "Grocery": 0.25},
}

# Multipliers on BASE_CONVERSION.
CHANNEL_CONV = {"Organic": 1.5, "Paid Search": 1.0, "Social": 0.6, "Email": 2.0}
DEVICE_CONV = {"Mobile": 1.0, "Desktop": 1.3, "Tablet": 0.8}
CATEGORY_CONV = {"Electronics": 0.7, "Fashion": 1.0, "Home": 0.9, "Grocery": 1.4}

# Average order value (price per unit) in rupees.
CATEGORY_AOV = {"Electronics": 6000.0, "Fashion": 1500.0, "Home": 2500.0, "Grocery": 700.0}

# Cost of goods as a share of price.
COST_RATIO = {"Electronics": 0.78, "Fashion": 0.55, "Home": 0.62, "Grocery": 0.85}

# Our share of each regional market, and how fast the whole market grows per day.
MY_MARKET_SHARE = {
    "Delhi": 0.12,
    "Mumbai": 0.15,
    "Bengaluru": 0.10,
    "Tamil Nadu": 0.08,
    "West Bengal": 0.09,
    "Gujarat": 0.11,
}
MARKET_TREND_PER_DAY = 0.0008

# Monday..Sunday traffic multipliers.
WEEKDAY_FACTOR = np.array([1.00, 0.95, 0.95, 1.00, 1.10, 1.25, 1.20])

DIMENSIONS = list(DIMENSION_WEIGHTS)
SCENARIO_KINDS = (
    "single",
    "double",
    "mix_only",
    "noisy",
    "none",
    "budget_miss",
    "pvm",
    "cost_rise",
    "share_loss",
)


@dataclass(frozen=True)
class Shock:
    """A planted change applied to every row matching `filters` during period B."""

    filters: dict[str, str]
    effect: Effect
    multiplier: float

    def label(self) -> str:
        where = " & ".join(f"{k}={v}" for k, v in self.filters.items())
        return f"{where}: {self.effect} x{self.multiplier}"


@dataclass
class Scenario:
    """A dataset plus the ground truth needed to score an analysis engine."""

    kind: str
    seed: int
    data: pd.DataFrame
    metric: Metric
    shocks: list[Shock]
    period_a: tuple[pd.Timestamp, pd.Timestamp]
    period_b: tuple[pd.Timestamp, pd.Timestamp]
    baseline: Baseline = "previous_period"

    @property
    def expect_driver(self) -> bool:
        """False for the 'none' scenario: a correct engine says 'no clear driver'."""
        return len(self.shocks) > 0

    @property
    def truth(self) -> list[dict[str, str]]:
        return [dict(s.filters) for s in self.shocks]

    def truth_dict(self) -> dict:
        return {
            "synthetic": True,
            "kind": self.kind,
            "seed": self.seed,
            "metric": self.metric,
            "baseline": self.baseline,
            "period_a": [str(self.period_a[0].date()), str(self.period_a[1].date())],
            "period_b": [str(self.period_b[0].date()), str(self.period_b[1].date())],
            "expect_driver": self.expect_driver,
            "shocks": [
                {"filters": s.filters, "effect": s.effect, "multiplier": s.multiplier}
                for s in self.shocks
            ],
        }


def segment_share(filters: dict[str, str]) -> float:
    """Share of total traffic carried by the rows matching `filters`."""
    share = 1.0
    for dim, value in filters.items():
        share *= DIMENSION_WEIGHTS[dim][value]
    return share


def _mean_one_lognormal(rng: np.random.Generator, sigma: float, size: int) -> np.ndarray:
    """Lognormal noise with mean exactly 1, so noise adds no systematic bias."""
    return rng.lognormal(mean=-0.5 * sigma**2, sigma=sigma, size=size)


def generate_dataset(
    seed: int,
    shocks: tuple[Shock, ...] | list[Shock] = (),
    extra_noise: float = 0.05,
    trend_per_day: float = 0.0005,
) -> pd.DataFrame:
    """Build one row per day x region x channel x device x category.

    The same seed and arguments always give the same table.
    """
    rng = np.random.default_rng(seed)
    dates = pd.date_range(START_DATE, periods=N_DAYS, freq="D")

    combos = list(itertools.product(*(DIMENSION_WEIGHTS[k] for k in DIMENSIONS)))
    combo_df = pd.DataFrame(combos, columns=DIMENSIONS)
    n_combo = len(combo_df)

    df = pd.DataFrame({"date": dates.repeat(n_combo)})
    for dim in DIMENSIONS:
        df[dim] = np.tile(combo_df[dim].to_numpy(), N_DAYS)
    size = len(df)
    day_idx = np.repeat(np.arange(N_DAYS), n_combo)

    share = np.ones(size)
    for dim in DIMENSIONS:
        share *= df[dim].map(DIMENSION_WEIGHTS[dim]).to_numpy(dtype=float)

    conv = (
        BASE_CONVERSION
        * df["channel"].map(CHANNEL_CONV).to_numpy(dtype=float)
        * df["device"].map(DEVICE_CONV).to_numpy(dtype=float)
        * df["category"].map(CATEGORY_CONV).to_numpy(dtype=float)
    )
    aov = df["category"].map(CATEGORY_AOV).to_numpy(dtype=float)
    cost_ratio = df["category"].map(COST_RATIO).to_numpy(dtype=float)
    my_share = df["region"].map(MY_MARKET_SHARE).to_numpy(dtype=float)
    season = WEEKDAY_FACTOR[df["date"].dt.weekday.to_numpy()]
    trend = 1.0 + trend_per_day * day_idx

    visits_mult = np.ones(size)
    conv_mult = np.ones(size)
    aov_mult = np.ones(size)
    cost_mult = np.ones(size)
    in_b = day_idx >= B_START
    targets = {
        "visits": visits_mult,
        "conversion": conv_mult,
        "aov": aov_mult,
        "cost": cost_mult,
    }
    for shock in shocks:
        mask = in_b.copy()
        for dim, value in shock.filters.items():
            mask &= (df[dim] == value).to_numpy()
        targets[shock.effect][mask] *= shock.multiplier

    expected_visits = (
        DAILY_VISITS
        * share
        * season
        * trend
        * visits_mult
        * _mean_one_lognormal(rng, extra_noise, size)
    )
    visits = rng.poisson(expected_visits)
    orders = rng.binomial(visits, np.clip(conv * conv_mult, 0.0, 1.0))
    revenue = orders * aov * aov_mult * _mean_one_lognormal(rng, 0.05, size)

    # Drawn after the original columns so visits/orders/revenue are unchanged per seed.
    cost = orders * aov * cost_ratio * cost_mult * _mean_one_lognormal(rng, 0.03, size)
    base_expected = DAILY_VISITS * share * season * conv * aov  # no trend, no shocks
    budget = base_expected * trend
    market = (
        base_expected
        / my_share
        * (1.0 + MARKET_TREND_PER_DAY * day_idx)
        * _mean_one_lognormal(rng, 0.03, size)
    )

    df["visits"] = visits.astype("int64")
    df["orders"] = orders.astype("int64")
    df["revenue"] = np.round(revenue, 2)
    df["cost"] = np.round(cost, 2)
    df["budget_revenue"] = np.round(budget, 2)
    df["market_revenue"] = np.round(market, 2)
    return df


def _eligible_segments(avoid_region: str | None = None) -> list[tuple[str, str]]:
    """Region x (Mobile|Desktop) pairs that carry at least MIN_SEGMENT_SHARE of traffic."""
    out = []
    for region in DIMENSION_WEIGHTS["region"]:
        if region == avoid_region:
            continue
        for device in ("Mobile", "Desktop"):
            if segment_share({"region": region, "device": device}) >= MIN_SEGMENT_SHARE:
                out.append((region, device))
    return out


def _random_conversion_drop(
    rng: np.random.Generator, multiplier: float, avoid_region: str | None = None
) -> Shock:
    candidates = _eligible_segments(avoid_region)
    region, device = candidates[int(rng.integers(len(candidates)))]
    return Shock({"region": region, "device": device}, "conversion", multiplier)


def make_scenario(kind: str, seed: int) -> Scenario:
    """Build a benchmark scenario.

    single       one segment's conversion halves
    double       two segments in different regions drop
    mix_only     traffic shifts toward a low-converting channel; no segment gets worse
    noisy        one smaller drop hidden in extra noise
    none         noise and a mild uptrend only; the right answer is 'no clear driver'
    budget_miss  like single, but the comparison is actual vs budget
    pvm          Grocery volume surges (mix toward cheap goods) while Fashion price rises
    cost_rise    Electronics unit cost rises; revenue is flat but margin falls
    share_loss   one large region's traffic falls while the market does not
    """
    if kind not in SCENARIO_KINDS:
        raise ValueError(f"unknown scenario kind {kind!r}; choose from {SCENARIO_KINDS}")

    rng = np.random.default_rng([seed, 1])  # separate stream from the data noise
    metric: Metric = "revenue"
    baseline: Baseline = "previous_period"
    extra_noise = 0.05
    shocks: list[Shock] = []

    if kind == "single":
        shocks = [_random_conversion_drop(rng, 0.5)]
    elif kind == "double":
        first = _random_conversion_drop(rng, 0.5)
        second = _random_conversion_drop(rng, 0.6, avoid_region=first.filters["region"])
        shocks = [first, second]
    elif kind == "mix_only":
        metric = "conversion"
        shocks = [Shock({"channel": "Social"}, "visits", 2.0)]
    elif kind == "noisy":
        extra_noise = 0.20
        shocks = [_random_conversion_drop(rng, 0.65)]
    elif kind == "budget_miss":
        baseline = "budget"
        shocks = [_random_conversion_drop(rng, 0.5)]
    elif kind == "pvm":
        shocks = [
            Shock({"category": "Grocery"}, "visits", 1.5),
            Shock({"category": "Fashion"}, "aov", 1.10),
        ]
    elif kind == "cost_rise":
        metric = "margin"
        shocks = [Shock({"category": "Electronics"}, "cost", 1.12)]
    elif kind == "share_loss":
        metric = "market_share"
        regions = [r for r, w in DIMENSION_WEIGHTS["region"].items() if w >= 0.14]
        region = regions[int(rng.integers(len(regions)))]
        shocks = [Shock({"region": region}, "visits", 0.7)]

    data = generate_dataset(seed, shocks, extra_noise=extra_noise)
    dates = pd.date_range(START_DATE, periods=N_DAYS, freq="D")
    return Scenario(
        kind=kind,
        seed=seed,
        data=data,
        metric=metric,
        shocks=shocks,
        period_a=(dates[A_START], dates[A_START + PERIOD_LEN - 1]),
        period_b=(dates[B_START], dates[B_START + PERIOD_LEN - 1]),
        baseline=baseline,
    )
