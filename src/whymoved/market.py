"""Market share: did we grow with the market, ahead of it, or behind it?

Needs our value and the market's value (same unit, same segments), e.g. `revenue` and
`market_revenue`. Share = our total / market total (a ratio of totals, never an average of
segment shares).

Our change is split into three parts that add up EXACTLY, for the whole business and for each
segment i (R = our value, M = market value, g = growth M_B / M_A - 1, s = R / M):

    market   = R_A,i * g_total              what growing at the whole market's rate adds
    exposure = R_A,i * (g_i - g_total)      we were in segments where the market grew faster/slower
    share    = M_B,i * (s_B,i - s_A,i)      we took or lost share inside the segment

Sum over segments: market + exposure + share = R_B - R_A. "Share" is the part that says we
did better or worse than the market in that segment.

Limits: market figures are only as good as their source (surveys, panels, estimates), and a
segment that is new in B (no market or revenue in A) has no growth to compare, so its whole
value counts as share. The intervals resample days; if the market data is monthly or
estimated, they understate real uncertainty. This says where we gained or lost ground against
the market, not why.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from whymoved.pvm import _slice, day_cube

PARTS = ("market", "exposure", "share")


@dataclass
class MarketBridge:
    table: pd.DataFrame
    totals: dict[str, float]
    ci: dict[str, tuple[float, float]]
    revenue_a: float
    revenue_b: float
    market_a: float
    market_b: float
    dimensions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def share_a(self) -> float:
        return self.revenue_a / self.market_a

    @property
    def share_b(self) -> float:
        return self.revenue_b / self.market_b

    @property
    def our_growth(self) -> float:
        return self.revenue_b / self.revenue_a - 1

    @property
    def market_growth(self) -> float:
        return self.market_b / self.market_a - 1

    @property
    def residual(self) -> float:
        return (self.revenue_b - self.revenue_a) - sum(self.totals[p] for p in PARTS)


def _parts(ra, ma, rb, mb) -> dict[str, np.ndarray]:
    ra, ma, rb, mb = (np.asarray(x, dtype=float) for x in (ra, ma, rb, mb))
    if ma.sum() <= 0 or mb.sum() <= 0:
        raise ValueError("market value must be above zero in both periods")
    g_total = mb.sum() / ma.sum() - 1
    has_a = ma > 0
    g_seg = np.where(has_a, mb / np.where(has_a, ma, 1) - 1, g_total)
    market = ra * g_total
    exposure = ra * (g_seg - g_total)
    # share effect is the remainder of each segment's change, which equals M_B * (s_B - s_A)
    # wherever the segment exists in both periods and keeps the sum exact everywhere else
    share = (rb - ra) - market - exposure
    return {"market": market, "exposure": exposure, "share": share}


def market_share_bridge(
    df: pd.DataFrame,
    period_a,
    period_b,
    dimensions: list[str],
    *,
    revenue: str = "revenue",
    market: str = "market_revenue",
    date_col: str = "date",
    n_boot: int = 300,
    seed: int = 0,
) -> MarketBridge:
    """Split the change in our value into market growth, market exposure and share change."""
    if not dimensions:
        raise ValueError("give at least one dimension to split by")
    for col in [revenue, market, date_col, *dimensions]:
        if col not in df.columns:
            raise ValueError(f"column {col!r} not found")
    cols = [revenue, market]
    a, b = _slice(df, date_col, period_a), _slice(df, date_col, period_b)

    def seg(part: pd.DataFrame) -> pd.DataFrame:
        return part.groupby(dimensions, observed=True)[cols].sum()

    sa, sb = seg(a), seg(b)
    index = sa.index.union(sb.index)
    sa, sb = sa.reindex(index, fill_value=0.0), sb.reindex(index, fill_value=0.0)
    parts = _parts(sa[revenue], sa[market], sb[revenue], sb[market])

    tbl = pd.DataFrame({
        "revenue_a": sa[revenue], "revenue_b": sb[revenue],
        "market_a": sa[market], "market_b": sb[market]}, index=index)
    with np.errstate(divide="ignore", invalid="ignore"):
        tbl["share_a"] = np.where(tbl.market_a > 0, tbl.revenue_a / tbl.market_a, np.nan)
        tbl["share_b"] = np.where(tbl.market_b > 0, tbl.revenue_b / tbl.market_b, np.nan)
        tbl["our_growth"] = np.where(tbl.revenue_a > 0, tbl.revenue_b / tbl.revenue_a - 1, np.nan)
        tbl["market_growth"] = np.where(tbl.market_a > 0, tbl.market_b / tbl.market_a - 1, np.nan)
    for name in PARTS:
        tbl[name] = parts[name]
    tbl["total"] = tbl[list(PARTS)].sum(axis=1)
    totals = {p: float(parts[p].sum()) for p in PARTS}

    notes: list[str] = []
    no_market = ((sa[market] <= 0) & (sa[revenue] > 0)) | ((sb[market] <= 0) & (sb[revenue] > 0))
    if no_market.any():
        notes.append("Some segments have revenue but no market figure; their growth is not "
                     "compared with a market.")
    ci: dict[str, tuple[float, float]] = {}
    if n_boot > 0:
        ca = day_cube(a, dimensions, index, cols, date_col)
        cb = day_cube(b, dimensions, index, cols, date_col)
        rng = np.random.default_rng(seed)
        draws = {p: [] for p in PARTS}
        for _ in range(n_boot):
            ta = ca[rng.integers(0, len(ca), len(ca))].sum(axis=0)
            tb = cb[rng.integers(0, len(cb), len(cb))].sum(axis=0)
            if ta[:, 1].sum() <= 0 or tb[:, 1].sum() <= 0:
                continue
            e = _parts(ta[:, 0], ta[:, 1], tb[:, 0], tb[:, 1])
            for p in PARTS:
                draws[p].append(float(e[p].sum()))
        ci = {p: (float(np.percentile(v, 2.5)), float(np.percentile(v, 97.5)))
              for p, v in draws.items() if v}
    tbl = tbl.reindex(tbl["share"].abs().sort_values(ascending=False).index)
    return MarketBridge(tbl, totals, ci, float(sa[revenue].sum()), float(sb[revenue].sum()),
                        float(sa[market].sum()), float(sb[market].sum()), list(dimensions), notes)
