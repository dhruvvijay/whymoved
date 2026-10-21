"""Run an analysis on a stored file and return a JSON-friendly result.

Modes
    change   period B versus period A (the main engine, with the trust layer)
    budget   period B versus the budget columns
    pvm      price / volume / mix (and cost) bridge; needs a units column
    market   market-share bridge; needs a market column

The result holds numbers, the verdict, a waterfall for charting, the validation issues and a
plain-English summary. It never contains raw rows.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from whymoved.app.errors import Invalid
from whymoved.baseline import explain_budget, guess_budget_columns, trend_check
from whymoved.ingest import DataError, default_periods, prepare
from whymoved.market import market_share_bridge
from whymoved.metrics import FormulaError, build_budget_metric, build_metric
from whymoved.narrative import (
    CAVEAT,
    describe,
    describe_bridge,
    describe_market,
)
from whymoved.pvm import price_volume_mix
from whymoved.trust import explain_with_trust

MODES = ("change", "budget", "pvm", "market")
RESULT_VERSION = 1


@dataclass
class AnalysisParams:
    mode: str = "change"
    metric: str | None = None
    period_a: tuple[str, str] | None = None
    period_b: tuple[str, str] | None = None
    dimensions: list[str] | None = None
    currency: str = ""
    indian: bool = False
    revenue_col: str | None = None
    units_col: str | None = None
    cost_col: str | None = None
    market_col: str | None = None
    bridge_dimensions: list[str] | None = None
    extra: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "extra"}


def clean(obj):
    """Convert numpy/pandas values to plain JSON types (NaN and inf become None)."""
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        f = float(obj)
        return f if math.isfinite(f) else None
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, pd.Timestamp):
        return obj.date().isoformat()
    if isinstance(obj, (str, int, bool)) or obj is None:
        return obj
    return str(obj)


def _period(value, name: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    try:
        lo, hi = value
        lo, hi = pd.Timestamp(lo).normalize(), pd.Timestamp(hi).normalize()
    except (TypeError, ValueError) as exc:
        raise Invalid(f"{name} must be two dates, start and end") from exc
    if hi < lo:
        raise Invalid(f"{name} ends before it starts")
    return lo, hi


def _label(p) -> str:
    return f"{p[0].date()} to {p[1].date()}"


def _issues(report) -> list[dict]:
    return [{"severity": i.severity, "code": i.code, "message": i.message, "column": i.column,
             "count": i.count} for i in report.issues]


def _finding(f) -> dict:
    return {"label": f.label(), "filters": f.filters, "rank": f.rank, "explained": f.explained,
            "share_of_change": f.share_of_change, "ci_low": f.ci_low, "ci_high": f.ci_high,
            "z": f.z, "rate_effect": f.rate_effect, "mix_effect": f.mix_effect,
            "rows_a": f.rows_a, "rows_b": f.rows_b}


def _waterfall(ex, a_label: str, b_label: str) -> list[dict]:
    steps = [{"label": a_label, "value": ex.total_a, "kind": "total"}]
    steps += [{"label": f.label(), "value": f.explained, "kind": "finding"} for f in ex.findings]
    other = ex.change - ex.explained_total
    steps.append({"label": "Everything else", "value": other, "kind": "other"})
    steps.append({"label": b_label, "value": ex.total_b, "kind": "total"})
    return steps


def profile_file(path: Path, filename: str, max_rows: int) -> dict:
    """Read and check a file; return what the UI needs to offer analysis choices."""
    try:
        p = prepare(path, filename=filename)
    except DataError as exc:
        raise Invalid(str(exc)) from exc
    if p.report.rows_read > max_rows:
        raise Invalid(f"the file has {p.report.rows_read:,} rows; the limit is {max_rows:,}")
    found = default_periods(p)
    return clean({
        "date_col": p.date_col, "metric_cols": p.metric_cols, "dimension_cols": p.dimension_cols,
        "rows_read": p.report.rows_read, "rows_used": p.report.rows_used,
        "date_min": p.info.get("date_min"), "date_max": p.info.get("date_max"),
        "frequency": p.info.get("frequency"),
        "suggested_periods": None if found is None else {"a": found[0], "b": found[1]},
        "validation": _issues(p.report), "ok": p.report.ok,
    })


def analyze(path: Path, filename: str, params: AnalysisParams, max_rows: int) -> dict:
    if params.mode not in MODES:
        raise Invalid(f"mode must be one of {', '.join(MODES)}")
    try:
        first = prepare(path, filename=filename)
        if first.report.rows_read > max_rows:
            raise Invalid(f"the file has {first.report.rows_read:,} rows; "
                          f"the limit is {max_rows:,}")
        if params.period_a and params.period_b:
            period_a, period_b = _period(params.period_a, "period A"), _period(params.period_b,
                                                                              "period B")
        else:
            found = default_periods(first)
            if found is None:
                raise Invalid("could not choose periods (needs daily data over at least 14 days);"
                              " give period A and period B")
            period_a, period_b = found
        p = prepare(path, filename=filename, period_a=period_a, period_b=period_b)
    except DataError as exc:
        raise Invalid(str(exc)) from exc
    if not p.report.ok:
        raise Invalid("the file has errors: " + "; ".join(i.message for i in p.report.errors))

    dims = list(params.dimensions or p.dimension_cols)
    unknown = [d for d in dims if d not in p.dimension_cols]
    if unknown:
        raise Invalid(f"not a category column: {', '.join(unknown)}")
    if not dims:
        raise Invalid("no category columns to split by")
    dims = dims[:6]

    out = {"version": RESULT_VERSION, "mode": params.mode,
           "data": {"date_col": p.date_col, "dimensions": dims, "rows_used": p.report.rows_used,
                    "rows_read": p.report.rows_read,
                    "period_a": [period_a[0], period_a[1]], "period_b": [period_b[0], period_b[1]]},
           "validation": _issues(p.report), "caveat": CAVEAT}
    labels = (_label(period_a), _label(period_b))
    try:
        if params.mode in ("change", "budget"):
            formula = params.metric or p.metric_cols[0]
            if params.mode == "budget":
                mapping = guess_budget_columns(p.data.columns, p.metric_cols)
                if not mapping:
                    raise Invalid("no budget columns found (for example Budget_Revenue)")
                data, spec, helper = build_budget_metric(p.data, formula, mapping)
                ex = explain_budget(data, spec, helper, period_b, dims, date_col=p.date_col)
                trend = None
            else:
                data, spec = build_metric(p.data, formula)
                ex = explain_with_trust(data, spec, period_a, period_b, dims, date_col=p.date_col)
                trend = trend_check(data, spec, period_a, period_b, date_col=p.date_col)
            summary = describe(ex, trend=trend, validation=p.report, currency=params.currency,
                               indian_grouping=params.indian, period_labels=labels)
            first_label, second_label = ("Budget" if params.mode == "budget" else "Period A",
                                         "Actual" if params.mode == "budget" else "Period B")
            out["result"] = {
                "metric": spec.name, "is_ratio": spec.is_ratio, "total_a": ex.total_a,
                "total_b": ex.total_b, "change": ex.change, "verdict": ex.verdict,
                "findings": [_finding(f) for f in ex.findings], "notes": ex.notes,
                "waterfall": _waterfall(ex, first_label, second_label),
                "trend": None if trend is None or not trend.available else {
                    "verdict": trend.verdict, "expected_change_pct": trend.expected_change_pct,
                    "actual_change_pct": trend.actual_change_pct, "z": trend.z},
            }
        elif params.mode == "pvm":
            rev = params.revenue_col or _pick(p.metric_cols, ("revenue", "sales", "amount"))
            units = params.units_col
            if not rev or not units:
                raise Invalid("price/volume/mix needs a revenue column and a units column "
                              "(a count of things sold)")
            bdims = params.bridge_dimensions or dims[:1]
            br = price_volume_mix(p.data, period_a, period_b, bdims, revenue=rev, units=units,
                                  cost=params.cost_col, date_col=p.date_col)
            summary = describe_bridge(br, currency=params.currency,
                                      indian_grouping=params.indian, period_labels=labels)
            effects = [e for e in ("volume", "mix", "price", "cost")
                       if e != "cost" or params.cost_col]
            steps = [{"label": "Period A", "value": br.value_a, "kind": "total"}]
            steps += [{"label": e.capitalize(), "value": br.totals[e], "kind": "effect"}
                      for e in effects]
            steps.append({"label": "Period B", "value": br.value_b, "kind": "total"})
            rows = br.table.reset_index()
            rows["segment"] = rows[bdims].astype(str).agg(" / ".join, axis=1)
            out["result"] = {
                "target": br.target, "value_a": br.value_a, "value_b": br.value_b,
                "totals": br.totals, "ci": {k: list(v) for k, v in br.ci.items()},
                "waterfall": steps, "notes": br.notes, "dimensions": bdims,
                "segments": rows[["segment", *effects, "total"]].to_dict("records"),
            }
        else:  # market
            rev = params.revenue_col or _pick(p.metric_cols, ("revenue", "sales", "amount"))
            mkt = params.market_col
            if not rev or not mkt:
                raise Invalid("market share needs your revenue column and a market column")
            bdims = params.bridge_dimensions or dims[:1]
            mb = market_share_bridge(p.data, period_a, period_b, bdims, revenue=rev, market=mkt,
                                     date_col=p.date_col)
            summary = describe_market(mb, currency=params.currency,
                                      indian_grouping=params.indian, period_labels=labels)
            steps = [{"label": "Period A", "value": mb.revenue_a, "kind": "total"}]
            steps += [{"label": n.capitalize(), "value": mb.totals[n], "kind": "effect"}
                      for n in ("market", "exposure", "share")]
            steps.append({"label": "Period B", "value": mb.revenue_b, "kind": "total"})
            rows = mb.table.reset_index()
            rows["segment"] = rows[bdims].astype(str).agg(" / ".join, axis=1)
            out["result"] = {
                "share_a": mb.share_a, "share_b": mb.share_b, "our_growth": mb.our_growth,
                "market_growth": mb.market_growth, "totals": mb.totals,
                "ci": {k: list(v) for k, v in mb.ci.items()}, "waterfall": steps,
                "notes": mb.notes, "dimensions": bdims,
                "segments": rows[["segment", "share_a", "share_b", "market", "exposure",
                                  "share"]].to_dict("records"),
            }
    except (FormulaError, ValueError) as exc:
        raise Invalid(str(exc)) from exc
    out["summary"] = summary
    return clean(out)


def _pick(columns: list[str], words: tuple[str, ...]) -> str | None:
    for w in words:
        for c in columns:
            if w in c.lower():
                return c
    return None
