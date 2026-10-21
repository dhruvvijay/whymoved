"""Decomposition engine: which segments drove the change in a metric between two periods.

Two kinds of metric are supported.

Additive (revenue, orders, cost):
    contribution_i = B_i - A_i, so contributions sum exactly to the total change.
    excess_i = B_i - A_i * (B_total / A_total) is the part of that change that is NOT
    just the segment growing at the overall rate, so steady growth is not mistaken
    for a cause. Excess sums to zero across a partition.

Ratio (conversion = orders / visits, price = revenue / orders, margin = profit / revenue):
    with w = a segment's share of the denominator, r = its own rate, R = the overall rate,
        rate_i = w_B,i * (r_B,i - r_A,i)       the segment itself got better or worse
        mix_i  = (w_B,i - w_A,i) * (r_A,i - R_A)  traffic moved toward a stronger or weaker segment
    and sum_i (rate_i + mix_i) = R_B - R_A exactly. A segment absent from one period is
    handled so the identity still holds (see _ratio_table).

Findings say WHERE a metric moved. They do not establish why.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class MetricSpec:
    """An additive metric (denominator is None) or a ratio of two summed columns."""

    name: str
    numerator: str
    denominator: str | None = None

    @property
    def is_ratio(self) -> bool:
        return self.denominator is not None

    @property
    def columns(self) -> list[str]:
        return [self.numerator] + ([self.denominator] if self.denominator else [])


def additive(name: str, column: str | None = None) -> MetricSpec:
    return MetricSpec(name, column or name)


def ratio(name: str, numerator: str, denominator: str) -> MetricSpec:
    return MetricSpec(name, numerator, denominator)


def with_gross_profit(df: pd.DataFrame) -> pd.DataFrame:
    """Add gross_profit = revenue - cost so margin can be a ratio of two columns."""
    out = df.copy()
    out["gross_profit"] = out["revenue"] - out["cost"]
    return out


STANDARD_METRICS: dict[str, MetricSpec] = {
    "revenue": additive("revenue"),
    "orders": additive("orders"),
    "visits": additive("visits"),
    "cost": additive("cost"),
    "conversion": ratio("conversion", "orders", "visits"),
    "price": ratio("price", "revenue", "orders"),
    "margin": ratio("margin", "gross_profit", "revenue"),
}


@dataclass
class Decomposition:
    metric: MetricSpec
    dims: list[str]
    total_a: float
    total_b: float
    table: pd.DataFrame  # one row per segment, sorted by |score| descending

    @property
    def change(self) -> float:
        return self.total_b - self.total_a

    @property
    def residual(self) -> float:
        """Total change minus the sum of segment contributions. Should be ~0."""
        return self.change - float(self.table["contribution"].sum())


@dataclass(frozen=True)
class Finding:
    filters: dict[str, str]
    contribution: float
    share_of_change: float
    score: float
    depth: int
    rate_effect: float | None = None
    mix_effect: float | None = None


def _safe_div(num: np.ndarray, den: np.ndarray, fallback: np.ndarray) -> np.ndarray:
    out = fallback.astype(float).copy()
    np.divide(num, den, out=out, where=den > 0)
    return out


def _aggregate(
    df: pd.DataFrame, bounds, dims: list[str], cols: list[str], date_col: str, label: str
) -> pd.DataFrame:
    start, end = pd.Timestamp(bounds[0]), pd.Timestamp(bounds[1])
    part = df[(df[date_col] >= start) & (df[date_col] <= end)]
    if part.empty:
        raise ValueError(f"no rows in {label} ({start.date()} to {end.date()})")
    return part.groupby(dims, observed=True)[cols].sum()


def _additive_table(t: pd.DataFrame, spec: MetricSpec) -> tuple[float, float, pd.DataFrame]:
    a = t[f"{spec.numerator}_a"].to_numpy(dtype=float)
    b = t[f"{spec.numerator}_b"].to_numpy(dtype=float)
    total_a, total_b = float(a.sum()), float(b.sum())
    change = total_b - total_a
    contribution = b - a
    growth = total_b / total_a if total_a != 0 else 1.0
    out = pd.DataFrame(
        {
            "value_a": a,
            "value_b": b,
            "contribution": contribution,
            "excess": b - a * growth,
            "share_of_change": contribution / change if change != 0 else np.nan,
        }
    )
    out["score"] = out["excess"]
    return total_a, total_b, out


def _ratio_table(t: pd.DataFrame, spec: MetricSpec) -> tuple[float, float, pd.DataFrame]:
    num_a = t[f"{spec.numerator}_a"].to_numpy(dtype=float)
    num_b = t[f"{spec.numerator}_b"].to_numpy(dtype=float)
    den_a = t[f"{spec.denominator}_a"].to_numpy(dtype=float)
    den_b = t[f"{spec.denominator}_b"].to_numpy(dtype=float)
    if den_a.sum() <= 0 or den_b.sum() <= 0:
        raise ValueError(f"{spec.name} is undefined: the denominator is zero in a period")

    total_a = float(num_a.sum() / den_a.sum())
    total_b = float(num_b.sum() / den_b.sum())
    w_a, w_b = den_a / den_a.sum(), den_b / den_b.sum()
    # Absent in A -> use the overall rate as its A rate (zero mix, all effect is rate).
    r_a = _safe_div(num_a, den_a, np.full(len(t), total_a))
    # Absent in B -> weight is zero, so its B rate cannot matter.
    r_b = _safe_div(num_b, den_b, r_a)
    rate_effect = w_b * (r_b - r_a)
    mix_effect = (w_b - w_a) * (r_a - total_a)
    contribution = rate_effect + mix_effect
    change = total_b - total_a
    out = pd.DataFrame(
        {
            "num_a": num_a,
            "den_a": den_a,
            "num_b": num_b,
            "den_b": den_b,
            "rate_a": r_a,
            "rate_b": r_b,
            "weight_a": w_a,
            "weight_b": w_b,
            "rate_effect": rate_effect,
            "mix_effect": mix_effect,
            "contribution": contribution,
            "share_of_change": contribution / change if change != 0 else np.nan,
        }
    )
    out["score"] = out["contribution"]
    return total_a, total_b, out


def decompose(
    df: pd.DataFrame,
    metric: MetricSpec,
    period_a,
    period_b,
    dims: list[str],
    date_col: str = "date",
) -> Decomposition:
    """Split the change in `metric` between two periods across segments of `dims`."""
    dims = list(dims)
    if not dims:
        raise ValueError("dims must contain at least one column")
    for col in [date_col, *dims, *metric.columns]:
        if col not in df.columns:
            raise ValueError(f"column {col!r} not found")

    ga = _aggregate(df, period_a, dims, metric.columns, date_col, "period A").add_suffix("_a")
    gb = _aggregate(df, period_b, dims, metric.columns, date_col, "period B").add_suffix("_b")
    joined = ga.join(gb, how="outer").fillna(0.0)

    build = _ratio_table if metric.is_ratio else _additive_table
    total_a, total_b, values = build(joined, metric)
    table = pd.concat([joined.index.to_frame(index=False), values], axis=1)
    order = np.argsort(-np.abs(table["score"].to_numpy()), kind="stable")
    table = table.iloc[order].reset_index(drop=True)
    return Decomposition(metric, dims, total_a, total_b, table)


def _finding(row: pd.Series, dims: list[str], metric: MetricSpec) -> Finding:
    return Finding(
        filters={d: row[d] for d in dims},
        contribution=float(row["contribution"]),
        share_of_change=float(row["share_of_change"]),
        score=float(row["score"]),
        depth=len(dims),
        rate_effect=float(row["rate_effect"]) if metric.is_ratio else None,
        mix_effect=float(row["mix_effect"]) if metric.is_ratio else None,
    )


def find_drivers(
    df: pd.DataFrame,
    metric: MetricSpec,
    period_a,
    period_b,
    dimensions: list[str],
    max_depth: int = 2,
    top_k: int = 5,
    n_candidates: int = 6,
    drill_threshold: float = 0.7,
    date_col: str = "date",
) -> list[Finding]:
    """Rank the segments that moved the metric most, drilling into the most specific one.

    1. Score every single-dimension segment and keep the best `n_candidates`.
    2. For each, look at finer segments inside it. If one finer segment carries at least
       `drill_threshold` of the candidate's contribution (same direction), the finding
       becomes that finer segment. Repeat up to `max_depth`.
    3. Drop duplicates reached by different routes; return the top `top_k` by |score|.
    """
    dims = [d for d in dimensions if df[d].nunique() > 1]
    cache: dict[tuple[str, ...], Decomposition] = {}

    def dec(ds: list[str]) -> Decomposition:
        ordered = sorted(ds, key=dims.index)
        key = tuple(ordered)
        if key not in cache:
            cache[key] = decompose(df, metric, period_a, period_b, ordered, date_col)
        return cache[key]

    candidates: list[tuple[float, dict, pd.Series, list[str]]] = []
    for d in dims:
        for _, row in dec([d]).table.iterrows():
            candidates.append((abs(row["score"]), {d: row[d]}, row, [d]))
    candidates.sort(key=lambda c: -c[0])

    findings: dict[frozenset, Finding] = {}
    for _, filters, row, cur_dims in candidates[:n_candidates]:
        cur_filters, cur_row, cur_dims = dict(filters), row, list(cur_dims)
        while len(cur_filters) < max_depth:
            best_row, best_dims = None, None
            for d in dims:
                if d in cur_filters:
                    continue
                t = dec(list(cur_filters) + [d])
                table = t.table
                mask = np.ones(len(table), dtype=bool)
                for k, v in cur_filters.items():
                    mask &= (table[k] == v).to_numpy()
                sub = table[mask]
                if sub.empty:
                    continue
                cand = sub.iloc[int(np.argmax(np.abs(sub["contribution"].to_numpy())))]
                if best_row is None or abs(cand["contribution"]) > abs(best_row["contribution"]):
                    best_row, best_dims = cand, t.dims
            if best_row is None:
                break
            parent_c = cur_row["contribution"]
            child_c = best_row["contribution"]
            if parent_c == 0 or np.sign(child_c) != np.sign(parent_c):
                break
            if abs(child_c) < drill_threshold * abs(parent_c):
                break
            cur_row, cur_dims = best_row, list(best_dims)
            cur_filters = {d: best_row[d] for d in cur_dims}
        finding = _finding(cur_row, cur_dims, metric)
        key = frozenset(finding.filters.items())
        if key not in findings or abs(finding.score) > abs(findings[key].score):
            findings[key] = finding

    ranked = sorted(findings.values(), key=lambda f: -abs(f.score))
    return ranked[:top_k]
