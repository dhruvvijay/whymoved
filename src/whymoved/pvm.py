"""Price / volume / mix bridge for revenue, and the same bridge extended to profit.

Question answered: of the change in revenue (or profit) between two periods, how much is
    volume  - more or fewer units overall, at last period's average price (or unit margin)
    mix     - units moving between segments that sell at different prices (or unit margins)
    price   - segments selling their units at a different price
    cost    - segments paying a different cost per unit  (profit bridge only)
and which segments each effect sits in.

Definitions, per segment i (Q = units, P = revenue / units, C = cost / units, m = P - C;
share_A,i = Q_A,i / Q_A; "A" = earlier period, "B" = later period):

    volume_i = (Q_B - Q_A) * share_A,i * m_A,i
    mix_i    = (Q_B,i - share_A,i * Q_B) * m_A,i
    price_i  = Q_B,i * (P_B,i - P_A,i)
    cost_i   = -Q_B,i * (C_B,i - C_A,i)

For the revenue bridge m is replaced by P and there is no cost effect. The four effects add up
EXACTLY to the change (tests check this on random tables). Edge cases:
    * a segment that disappears in B: its whole old value is in volume + mix, price/cost = 0;
    * a segment that is new in B has no old price, so the overall average price (and cost) of A is
      used as its reference; the rest of its value shows up as a price/cost effect.

What this is not: it is arithmetic on the data, not a cause. "Price" means the average selling
price per unit moved inside a segment; a discount, a change of pack size and a different product
inside the same segment all look identical. Segments must be fine enough that products inside
one segment are alike; otherwise some mix hides inside "price". A day-resampled interval is given
for the four totals so noise is not read as a signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

EFFECTS = ("volume", "mix", "price", "cost")


@dataclass
class Bridge:
    """Result of price_volume_mix()."""

    table: pd.DataFrame            # one row per segment
    totals: dict[str, float]       # volume, mix, price, cost, change
    ci: dict[str, tuple[float, float]]   # 95% interval per total effect (day-resampled)
    value_a: float
    value_b: float
    target: str                    # "revenue" or "profit"
    dimensions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def change(self) -> float:
        return self.value_b - self.value_a

    @property
    def residual(self) -> float:
        """Change minus the sum of the effects; zero up to rounding."""
        return self.change - sum(self.totals[e] for e in EFFECTS)


def _effects(qa, ra, ca, qb, rb, cb, use_cost: bool) -> dict[str, np.ndarray]:
    """Vectorised bridge on per-segment totals (arrays of equal length)."""
    qa, ra, ca, qb, rb, cb = (np.asarray(x, dtype=float) for x in (qa, ra, ca, qb, rb, cb))
    tqa, tqb = qa.sum(), qb.sum()
    if tqa <= 0:
        raise ValueError("no units sold in period A")
    avg_p, avg_c = ra.sum() / tqa, ca.sum() / tqa
    in_a, in_b = qa > 0, qb > 0
    pa = np.where(in_a, ra / np.where(in_a, qa, 1), avg_p)
    ca_u = np.where(in_a, ca / np.where(in_a, qa, 1), avg_c)
    pb = np.where(in_b, rb / np.where(in_b, qb, 1), pa)
    cb_u = np.where(in_b, cb / np.where(in_b, qb, 1), ca_u)
    ref = (pa - ca_u) if use_cost else pa
    share_a = qa / tqa
    volume = (tqb - tqa) * share_a * ref
    mix = (qb - share_a * tqb) * ref
    price = qb * (pb - pa)
    cost = -qb * (cb_u - ca_u) if use_cost else np.zeros_like(qb)
    return {"volume": volume, "mix": mix, "price": price, "cost": cost}


def _slice(df: pd.DataFrame, date_col: str, period) -> pd.DataFrame:
    lo, hi = (pd.Timestamp(x).normalize() for x in period)
    out = df[(df[date_col] >= lo) & (df[date_col] < hi + pd.Timedelta(days=1))]
    if out.empty:
        raise ValueError(f"no rows between {lo.date()} and {hi.date()}")
    return out


def price_volume_mix(
    df: pd.DataFrame,
    period_a,
    period_b,
    dimensions: list[str],
    *,
    revenue: str = "revenue",
    units: str = "orders",
    cost: str | None = None,
    date_col: str = "date",
    n_boot: int = 300,
    seed: int = 0,
) -> Bridge:
    """Bridge revenue (or profit, when `cost` is given) from period A to period B.

    `units` must be a count of things sold, so that revenue / units is a price.
    `dimensions` define the segments (e.g. ["category"] or ["category", "region"]).
    """
    if not dimensions:
        raise ValueError("give at least one dimension to split by")
    for col in [revenue, units, date_col, *dimensions, *([cost] if cost else [])]:
        if col not in df.columns:
            raise ValueError(f"column {col!r} not found")
    use_cost = cost is not None
    cols = [revenue, units] + ([cost] if use_cost else [])
    a, b = _slice(df, date_col, period_a), _slice(df, date_col, period_b)
    if a[units].sum() <= 0 or b[units].sum() <= 0:
        raise ValueError("units must add up to more than zero in both periods")
    notes: list[str] = []
    if (df[units] < 0).any() or (df[revenue] < 0).any():
        notes.append("Negative units or revenue (returns or credits) are included as given; "
                     "prices of such segments may look odd.")

    def seg_sums(part: pd.DataFrame) -> pd.DataFrame:
        return part.groupby(dimensions, observed=True)[cols].sum()

    sa, sb = seg_sums(a), seg_sums(b)
    index = sa.index.union(sb.index)
    sa, sb = sa.reindex(index, fill_value=0.0), sb.reindex(index, fill_value=0.0)
    zeros = np.zeros(len(index))
    ca_arr = sa[cost].to_numpy() if use_cost else zeros
    cb_arr = sb[cost].to_numpy() if use_cost else zeros
    eff = _effects(sa[units], sa[revenue], ca_arr, sb[units], sb[revenue], cb_arr, use_cost)

    tbl = pd.DataFrame(
        {"units_a": sa[units], "units_b": sb[units],
         "revenue_a": sa[revenue], "revenue_b": sb[revenue]}, index=index)
    with np.errstate(divide="ignore", invalid="ignore"):
        tbl["price_a"] = np.where(tbl.units_a > 0, tbl.revenue_a / tbl.units_a, np.nan)
        tbl["price_b"] = np.where(tbl.units_b > 0, tbl.revenue_b / tbl.units_b, np.nan)
    if use_cost:
        tbl["cost_a"], tbl["cost_b"] = sa[cost], sb[cost]
    for name in EFFECTS if use_cost else EFFECTS[:3]:
        tbl[name] = eff[name]
    tbl["total"] = tbl[[e for e in EFFECTS if e in tbl]].sum(axis=1)
    value_a = float(sa[revenue].sum() - (sa[cost].sum() if use_cost else 0.0))
    value_b = float(sb[revenue].sum() - (sb[cost].sum() if use_cost else 0.0))
    totals = {e: float(eff[e].sum()) for e in EFFECTS}
    totals["change"] = value_b - value_a

    ci = _bootstrap(a, b, dimensions, index, cols, units, revenue, cost, date_col, n_boot, seed)
    tbl = tbl.reindex(tbl["total"].abs().sort_values(ascending=False).index)
    if not use_cost:
        notes.append("Revenue bridge only: add a cost column to see cost-per-unit effects "
                     "and a profit bridge.")
    return Bridge(tbl, totals, ci, value_a, value_b, "profit" if use_cost else "revenue",
                  list(dimensions), notes)


def day_cube(part: pd.DataFrame, dims, index, cols, date_col: str) -> np.ndarray:
    """Array [day, segment, column] of daily sums, so days can be resampled quickly."""
    days = np.sort(part[date_col].dt.normalize().unique())
    g = part.assign(_d=part[date_col].dt.normalize()).groupby(["_d", *dims], observed=True)
    s = g[cols].sum()
    arr = np.zeros((len(days), len(index), len(cols)))
    di = {d: i for i, d in enumerate(days)}
    pos = {k: i for i, k in enumerate(index)}
    for key, row in zip(s.index, s.to_numpy(), strict=True):
        seg = key[1:] if len(dims) > 1 else key[1]
        arr[di[key[0]], pos[seg]] = row
    return arr


def _bootstrap(a, b, dims, index, cols, units, revenue, cost, date_col, n_boot, seed):
    """95% interval for each total effect, resampling DAYS within each period."""
    if n_boot <= 0:
        return {}
    use_cost = cost is not None

    ca = day_cube(a, dims, index, cols, date_col)
    cb = day_cube(b, dims, index, cols, date_col)
    rng = np.random.default_rng(seed)
    qi, ri = cols.index(units), cols.index(revenue)
    ki = cols.index(cost) if use_cost else None
    out = {e: [] for e in EFFECTS}
    for _ in range(n_boot):
        ta = ca[rng.integers(0, len(ca), len(ca))].sum(axis=0)
        tb = cb[rng.integers(0, len(cb), len(cb))].sum(axis=0)
        if ta[:, qi].sum() <= 0:
            continue
        zeros = np.zeros(len(index))
        e = _effects(ta[:, qi], ta[:, ri], ta[:, ki] if use_cost else zeros,
                     tb[:, qi], tb[:, ri], tb[:, ki] if use_cost else zeros, use_cost)
        for k in EFFECTS:
            out[k].append(float(e[k].sum()))
    return {k: (float(np.percentile(v, 2.5)), float(np.percentile(v, 97.5)))
            for k, v in out.items() if v}
