"""Trust layer: which findings are real, and is there a clear driver at all?

M2 ranks segments; this module decides how far to believe them.

* Peeling (overlap handling). The top-ranked segment is accepted, then ITS ROWS ARE
  REMOVED and the search runs again on what is left. Later findings are therefore
  disjoint from earlier ones, and a big segment that merely contains an accepted
  segment's rows (an "echo") has nothing left to show.

* Explained amount. For a segment, explained = (change in the overall metric) minus
  (change in the overall metric with that segment's rows left out). For sums this is
  exactly the segment's own change; for ratios it includes both its rate and mix
  parts. Across peeled findings the explained amounts add up without double counting.

* Significance. Days are resampled with replacement within each period (bootstrap).
  The test statistic is how far the segment moved relative to the REST of the data:
  for sums, b_seg - a_seg * (rest growth); for ratios, the explained amount. A finding
  counts only if |statistic| is at least `z_threshold` bootstrap standard errors from
  zero. The default of 4.5 is deliberately strict: we look at many segments (the best of
  many candidates always looks larger than a single test would), and with
  a plain 95% interval one of them would often pass by luck.

* Low-sample guard. The bootstrap resamples DAYS, so what limits it is how many days a
  segment has data for, not how many rows. A segment needs data on at least
  max(`min_days`, half of the days present in that period) days in BOTH periods;
  otherwise it is never accepted and is reported in `notes`. (An earlier version counted
  rows, which wrongly blocked every segment once data was aggregated to one row per day.)
  Weekly or monthly data has too few time points for this test, so it is refused, not guessed.

* Verdict. 'clear_driver', 'partial_driver', 'offsetting_moves' (tiny net change hiding real
  movements), 'no_clear_driver' or 'negligible_change'.

Findings say WHERE a metric moved. They do not establish why.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from whymoved.decompose import MetricSpec, find_drivers


@dataclass
class TrustedFinding:
    filters: dict[str, str]
    rank: int
    explained: float  # leave-out impact on the overall metric, in metric units
    share_of_change: float  # explained / total change (original data)
    ci_low: float  # 95% bootstrap interval of `explained`
    ci_high: float
    z: float  # test statistic / bootstrap standard error
    rows_a: int
    rows_b: int
    rate_effect: float | None = None
    mix_effect: float | None = None

    @property
    def depth(self) -> int:
        return len(self.filters)

    def label(self) -> str:
        return " & ".join(f"{k}={v}" for k, v in self.filters.items())


@dataclass
class TrustedExplanation:
    metric: MetricSpec
    total_a: float
    total_b: float
    verdict: str
    findings: list[TrustedFinding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    reference: str = "previous period"  # what period A stands for: or "budget"

    @property
    def change(self) -> float:
        return self.total_b - self.total_a

    @property
    def explained_total(self) -> float:
        return float(sum(f.explained for f in self.findings))

    @property
    def explained_share(self) -> float:
        return self.explained_total / self.change if self.change != 0 else float("nan")


def _in_period(df: pd.DataFrame, bounds, date_col: str) -> pd.Series:
    start, end = pd.Timestamp(bounds[0]), pd.Timestamp(bounds[1])
    return (df[date_col] >= start) & (df[date_col] <= end)


def _metric_value(sums: np.ndarray, spec: MetricSpec) -> np.ndarray:
    """Metric from column sums with shape (n, ncols); NaN where a ratio's denominator is 0."""
    if not spec.is_ratio:
        return sums[:, 0]
    out = np.full(len(sums), np.nan)
    np.divide(sums[:, 0], sums[:, 1], out=out, where=sums[:, 1] > 0)
    return out


def _statistics(spec: MetricSpec, all_a, all_b, seg_a, seg_b) -> tuple[np.ndarray, np.ndarray]:
    """Return (explained, test statistic) for column sums of shape (n, ncols)."""
    rest_a, rest_b = all_a - seg_a, all_b - seg_b
    delta_all = _metric_value(all_b, spec) - _metric_value(all_a, spec)
    delta_rest = _metric_value(rest_b, spec) - _metric_value(rest_a, spec)
    explained = delta_all - delta_rest
    if spec.is_ratio:
        return explained, explained
    a_rest = _metric_value(rest_a, spec)
    growth = np.full(len(a_rest), np.nan)
    np.divide(_metric_value(rest_b, spec), a_rest, out=growth, where=a_rest > 0)
    excess = _metric_value(seg_b, spec) - _metric_value(seg_a, spec) * growth
    return explained, excess


def _daily(part_all: pd.DataFrame, part_seg: pd.DataFrame, cols: list[str], date_col: str):
    """Per-day column sums for all rows and for the segment, on a shared day index."""
    days = np.sort(part_all[date_col].unique())
    all_d = part_all.groupby(date_col)[cols].sum().reindex(days).fillna(0.0)
    seg_d = part_seg.groupby(date_col)[cols].sum().reindex(days).fillna(0.0)
    return all_d.to_numpy(dtype=float), seg_d.to_numpy(dtype=float)


def _mask(df: pd.DataFrame, filters: dict[str, str]) -> pd.Series:
    mask = pd.Series(True, index=df.index)
    for dim, value in filters.items():
        mask &= df[dim] == value
    return mask


def _total(df: pd.DataFrame, spec: MetricSpec, bounds, date_col: str, label: str) -> float:
    part = df[_in_period(df, bounds, date_col)]
    if part.empty:
        raise ValueError(f"no rows in {label}")
    sums = part[spec.columns].sum().to_numpy(dtype=float).reshape(1, -1)
    value = float(_metric_value(sums, spec)[0])
    if np.isnan(value):
        raise ValueError(f"{spec.name} is undefined in {label}: the denominator is zero")
    return value


def _evaluate_segment(spec, remaining, mask, in_a, in_b, date_col, n_boot, rng):
    """Point estimate and bootstrap distribution for one segment; None if undefined."""
    cols = spec.columns
    all_a, seg_a = _daily(remaining[in_a], remaining[in_a & mask], cols, date_col)
    all_b, seg_b = _daily(remaining[in_b], remaining[in_b & mask], cols, date_col)

    point_expl, point_test = _statistics(
        spec, all_a.sum(0, keepdims=True), all_b.sum(0, keepdims=True),
        seg_a.sum(0, keepdims=True), seg_b.sum(0, keepdims=True),
    )
    if not (np.isfinite(point_expl[0]) and np.isfinite(point_test[0])):
        return None

    n_a, n_b = len(all_a), len(all_b)
    ca = rng.multinomial(n_a, np.full(n_a, 1.0 / n_a), size=n_boot)
    cb = rng.multinomial(n_b, np.full(n_b, 1.0 / n_b), size=n_boot)
    boot_expl, boot_test = _statistics(spec, ca @ all_a, cb @ all_b, ca @ seg_a, cb @ seg_b)
    boot_expl, boot_test = boot_expl[np.isfinite(boot_expl)], boot_test[np.isfinite(boot_test)]
    if len(boot_expl) < n_boot // 2 or len(boot_test) < n_boot // 2:
        return None

    sd = float(np.std(boot_test, ddof=1))
    if sd > 0:
        z = point_test[0] / sd
    else:
        z = np.inf * np.sign(point_test[0]) if point_test[0] else 0.0
    lo, hi = np.percentile(boot_expl, [2.5, 97.5])
    return float(point_expl[0]), float(lo), float(hi), float(z)


def explain_with_trust(
    df: pd.DataFrame,
    spec: MetricSpec,
    period_a,
    period_b,
    dimensions: list[str],
    date_col: str = "date",
    n_boot: int = 400,
    z_threshold: float = 4.5,
    min_days: int = 10,
    max_findings: int = 4,
    min_explained_share: float = 0.5,
    negligible_pct: float = 0.005,
    n_candidates: int = 5,
    max_depth: int = 2,
    seed: int = 0,
    reference: str = "previous period",
) -> TrustedExplanation:
    """Find the segments that moved `spec` between two periods, and say how far to trust them."""
    for col in [date_col, *dimensions, *spec.columns]:
        if col not in df.columns:
            raise ValueError(f"column {col!r} not found")
    total_a = _total(df, spec, period_a, date_col, "period A")
    total_b = _total(df, spec, period_b, date_col, "period B")
    change = total_b - total_a
    result = TrustedExplanation(
        spec, total_a, total_b, verdict="no_clear_driver", reference=reference
    )

    net_negligible = abs(change) <= negligible_pct * abs(total_a)

    rng = np.random.default_rng(seed)
    remaining = df
    noted: set[str] = set()
    for _ in range(max_findings):
        in_a = _in_period(remaining, period_a, date_col)
        in_b = _in_period(remaining, period_b, date_col)
        if not in_a.any() or not in_b.any():
            break
        try:
            candidates = find_drivers(
                remaining, spec, period_a, period_b, dimensions,
                max_depth=max_depth, top_k=n_candidates, date_col=date_col,
            )
        except ValueError:
            break

        accepted = None
        for cand in candidates:
            mask = _mask(remaining, cand.filters)
            rows_a, rows_b = int((mask & in_a).sum()), int((mask & in_b).sum())
            label = " & ".join(f"{k}={v}" for k, v in cand.filters.items())
            days_a = int(remaining.loc[mask & in_a, date_col].nunique())
            days_b = int(remaining.loc[mask & in_b, date_col].nunique())
            need_a = max(min_days, int(np.ceil(0.5 * remaining.loc[in_a, date_col].nunique())))
            need_b = max(min_days, int(np.ceil(0.5 * remaining.loc[in_b, date_col].nunique())))
            if days_a < need_a or days_b < need_b:
                if label not in noted:
                    noted.add(label)
                    result.notes.append(
                        f"{label}: data on only {days_a} day(s) in period A and {days_b} in "
                        f"period B (needs {need_a} and {need_b}), so it was not judged."
                    )
                continue
            evaluated = _evaluate_segment(spec, remaining, mask, in_a, in_b, date_col, n_boot, rng)
            if evaluated is None:
                continue
            explained, lo, hi, z = evaluated
            if abs(z) >= z_threshold:
                accepted = (cand, mask, rows_a, rows_b, explained, lo, hi, z)
                break
        if accepted is None:
            break

        cand, mask, rows_a, rows_b, explained, lo, hi, z = accepted
        result.findings.append(
            TrustedFinding(
                filters=dict(cand.filters),
                rank=len(result.findings) + 1,
                explained=explained,
                share_of_change=explained / change if change != 0 else float("nan"),
                ci_low=lo,
                ci_high=hi,
                z=z,
                rows_a=rows_a,
                rows_b=rows_b,
                rate_effect=cand.rate_effect,
                mix_effect=cand.mix_effect,
            )
        )
        remaining = remaining[~mask]

    if not result.findings:
        if net_negligible:
            result.verdict = "negligible_change"
            result.notes.append(
                f"{spec.name} changed by {change / total_a:+.2%} (under {negligible_pct:.1%}) "
                "and no segment moved clearly differently from the rest."
            )
        else:
            result.verdict = "no_clear_driver"
            result.notes.append(
                "No segment moved clearly differently from the rest; "
                "the change looks broad-based or noisy."
            )
        return result

    if net_negligible:
        result.verdict = "offsetting_moves"
        result.notes.append(
            f"The net change is only {change / total_a:+.2%}, but segments moved against "
            "each other; the net figure hides real movement."
        )
    elif result.explained_total * change < 0:
        result.verdict = "offsetting_moves"
        result.notes.append(
            f"The findings moved {spec.name} the opposite way to the net change "
            f"({change / total_a:+.2%}); the rest of the data moved by more. "
            "The net figure hides this segment's move."
        )
    elif result.explained_share >= min_explained_share:
        result.verdict = "clear_driver"
    else:
        result.verdict = "partial_driver"
        result.notes.append(
            f"The findings explain {result.explained_share:.0%} of the change; "
            "the rest is spread out."
        )
    if not net_negligible and abs(result.explained_total) > 1.25 * abs(change):
        result.notes.append(
            "Findings account for more than 100% of the net change; the rest of the "
            "data moved the other way."
        )
    return result
