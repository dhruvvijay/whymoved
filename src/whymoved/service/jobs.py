"""Turn an uploaded file into dataset metadata, and a dataset plus settings into a result.

Everything returned is plain JSON data (no NaN, no numpy, no Timestamps).
"""

from __future__ import annotations

import io
import math
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from whymoved.baseline import explain_budget, guess_budget_columns, trend_check
from whymoved.ingest import DataError, default_periods, prepare
from whymoved.metrics import build_budget_metric, build_metric
from whymoved.narrative import CAVEAT, describe
from whymoved.statement_import import (
    ROLE_INFO,
    StatementImportError,
    read_statements,
    suggest_roles,
)
from whymoved.trust import explain_with_trust

MAX_CATEGORIES = 30


def clean_json(obj):
    """Recursively convert numpy/pandas values to JSON-safe ones; NaN and inf become None."""
    if isinstance(obj, dict):
        return {str(k): clean_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean_json(v) for v in obj]
    if isinstance(obj, (pd.Timestamp, datetime, date)):
        if isinstance(obj, (pd.Timestamp, datetime)):
            return obj.date().isoformat()
        return obj.isoformat()
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        value = float(obj)
        return value if math.isfinite(value) else None
    if isinstance(obj, np.ndarray):
        return clean_json(obj.tolist())
    return obj


def _issues(report) -> list[dict]:
    return [{"severity": i.severity, "code": i.code, "message": i.message, "column": i.column,
             "count": i.count, "examples": i.examples} for i in report.issues]


def _iso(period) -> list[str]:
    return [pd.Timestamp(period[0]).date().isoformat(), pd.Timestamp(period[1]).date().isoformat()]


_GUESSES = {
    "revenue": ("revenue", "sales", "net_sales", "amount", "turnover", "value", "sale_value"),
    "units": ("units", "quantity", "qty", "orders", "volume", "units_sold", "items"),
    "market": ("market_revenue", "market_size", "market_sales", "market", "industry_revenue",
               "industry_sales", "category_sales"),
    "cost": ("cost", "cogs", "cost_of_goods_sold", "total_cost", "expenses"),
}


def suggest_columns(metric_cols: list[str]) -> dict[str, str | None]:
    """Best guess of the revenue, units, market and cost columns, by name. A guess only: the
    person confirms or changes each one."""
    lower = {c.lower().replace(" ", "_"): c for c in metric_cols}
    out: dict[str, str | None] = {}
    taken: set[str] = set()
    for role, names in _GUESSES.items():
        out[role] = None
        for name in names:
            col = lower.get(name)
            if col is not None and col not in taken:
                out[role] = col
                taken.add(col)
                break
    return out


def _inspect_table(data: bytes, filename: str, max_rows: int) -> dict:
    p = prepare(io.BytesIO(data), filename=filename, require_dimensions=False)
    if p.report.rows_read > max_rows:
        raise DataError(f"the file has {p.report.rows_read:,} rows; the limit is {max_rows:,}")
    if not p.report.ok:
        text = "; ".join(i.message for i in p.report.errors)
        raise DataError(f"the file cannot be used: {text}")
    periods = default_periods(p)
    return clean_json({
        "kind": "table",
        "rows": p.report.rows_used, "rows_read": p.report.rows_read,
        "date_col": p.date_col, "metric_cols": p.metric_cols, "dimension_cols": p.dimension_cols,
        "date_min": p.info.get("date_min"), "date_max": p.info.get("date_max"),
        "frequency": p.info.get("frequency"),
        "default_periods": {"a": _iso(periods[0]), "b": _iso(periods[1])} if periods else None,
        "budget_columns": guess_budget_columns(p.data.columns, p.metric_cols),
        "suggested": suggest_columns(p.metric_cols),
        "issues": _issues(p.report),
    })


MAX_LINE_CHOICES = 400


def _inspect_statements(table) -> dict:
    roles = suggest_roles(table)
    return clean_json({
        "kind": "statements", "company": table.company, "periods": table.periods,
        "sheets": table.sheets, "rows": len(table.rows),
        "lines": [{"key": r.key, "label": r.label, "sheet": r.sheet, "derived": r.derived}
                  for r in table.rows[:MAX_LINE_CHOICES]],
        "roles": [{"role": k, "label": v[0], "group": v[1]} for k, v in ROLE_INFO.items()],
        "suggested_roles": roles, "notes": table.notes, "issues": [],
        "default_periods": {"a": table.periods[-2], "b": table.periods[-1]}
        if len(table.periods) >= 2 else None,
    })


def inspect_upload(data: bytes, filename: str, max_rows: int) -> dict:
    """Read and check a file. A table of dated rows (one row per day or month per segment) is
    kind "table"; a financial statement (line items down the side, years across) is kind
    "statements". Raises DataError if it is neither."""
    try:
        return _inspect_table(data, filename, max_rows)
    except DataError as table_error:
        try:
            table = read_statements(data, filename)
        except StatementImportError:
            raise table_error from None
        return _inspect_statements(table)


def _parse_period(value, label: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    try:
        start, end = (pd.Timestamp(x).normalize() for x in value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{label} must be two dates, e.g. [\"2026-01-05\", \"2026-02-01\"]") from exc
    if end < start:
        raise ValueError(f"{label} ends before it starts")
    return start, end


def run_analysis(path: Path, filename: str, params: dict) -> dict:
    """Explain the change described by `params` on the stored file. Raises ValueError (including
    DataError and FormulaError, which subclass it) with a message safe to show to the user."""
    kind = params.get("kind") or "changes"
    if kind != "changes":
        from whymoved.service.analyses import RUNNERS  # imported late: it imports this module
        if kind not in RUNNERS:
            raise ValueError(f"kind must be one of: changes, {', '.join(RUNNERS)}")
        return RUNNERS[kind](path, filename, params)

    def read(**kw):
        with open(path, "rb") as handle:
            return prepare(handle, filename=filename, require_dimensions=False, **kw)

    mode = params.get("mode", "previous")
    if mode not in ("previous", "budget"):
        raise ValueError("mode must be 'previous' or 'budget'")
    first = read()
    dims = params.get("dimensions") or first.dimension_cols
    unknown = [d for d in dims if d not in first.dimension_cols]
    if unknown:
        raise ValueError(f"unknown category column(s): {', '.join(unknown)}")
    if not dims:
        raise ValueError("this file has no category column to split by, so it can only be used "
                         "for the trend analysis")
    if params.get("period_a") and params.get("period_b"):
        period_a = _parse_period(params["period_a"], "period_a")
        period_b = _parse_period(params["period_b"], "period_b")
    else:
        found = default_periods(first)
        if found is None:
            raise ValueError("could not choose periods automatically (needs daily data over at "
                             "least 14 days); give period_a and period_b")
        period_a, period_b = found
    p = read(period_a=period_a, period_b=period_b, dimension_cols=list(dims))
    if not p.report.ok:
        raise DataError("; ".join(i.message for i in p.report.errors))
    formula = (params.get("metric") or p.metric_cols[0]).strip()

    trend = None
    if mode == "budget":
        mapping = guess_budget_columns(p.data.columns, p.metric_cols)
        data, spec, helper = build_budget_metric(p.data, formula, mapping)
        ex = explain_budget(data, spec, helper, period_b, p.dimension_cols, date_col=p.date_col)
    else:
        data, spec = build_metric(p.data, formula)
        ex = explain_with_trust(data, spec, period_a, period_b, p.dimension_cols,
                                date_col=p.date_col)
        trend = trend_check(data, spec, period_a, period_b, date_col=p.date_col)

    def label(period):
        return f"{period[0].date()} to {period[1].date()}"

    currency = str(params.get("currency") or "").strip()[:8]
    if currency and currency[-1].isalpha():
        currency += " "  # "Rs" -> "Rs 1,234", while symbols such as the rupee sign stay attached
    summary = describe(ex, trend=trend, validation=p.report, currency=currency,
                       indian_grouping=bool(params.get("indian")),
                       period_labels=(label(period_a), label(period_b)))
    change = ex.total_b - ex.total_a
    findings = [{
        "rank": f.rank, "label": f.label(), "filters": f.filters, "explained": f.explained,
        "share_of_change": f.share_of_change, "ci_low": f.ci_low, "ci_high": f.ci_high,
        "z": f.z, "rate_effect": f.rate_effect, "mix_effect": f.mix_effect,
    } for f in ex.findings]
    rest = change - sum(f.explained for f in ex.findings)
    waterfall = [{"label": "Period A" if mode == "previous" else "Budget",
                  "value": ex.total_a, "kind": "total"}]
    waterfall += [{"label": f["label"], "value": f["explained"], "kind": "finding"}
                  for f in findings]
    waterfall += [{"label": "Everything else", "value": rest, "kind": "rest"},
                  {"label": "Period B" if mode == "previous" else "Actual",
                   "value": ex.total_b, "kind": "total"}]
    return clean_json({
        "kind": "changes",
        "metric": spec.name, "formula": formula, "is_ratio": spec.is_ratio, "mode": mode,
        "period_a": _iso(period_a), "period_b": _iso(period_b),
        "total_a": ex.total_a, "total_b": ex.total_b, "change": change, "verdict": ex.verdict,
        "findings": findings, "notes": ex.notes, "waterfall": waterfall,
        "trend": asdict(trend) if trend is not None else None,
        "summary": summary, "caveat": CAVEAT, "validation": _issues(p.report),
        "currency": currency, "indian": bool(params.get("indian")),
        "dimensions": list(p.dimension_cols),
    })


MAX_SCAN_METRICS = 10


def scan_metrics(path: Path, filename: str, meta: dict) -> dict:
    """Run the "what moved" analysis on each number column over the default periods and rank the
    columns by size of change. A digest for deciding where to look first; every row can be opened
    as a full analysis. A column that cannot be analysed is listed with its reason, not hidden."""
    cols = list(meta.get("metric_cols") or [])[:MAX_SCAN_METRICS]
    if not meta.get("dimension_cols"):
        raise ValueError("this file has no category column to split by, so there is nothing "
                         "to scan")
    rows, skipped = [], []
    for col in cols:
        try:
            r = run_analysis(path, filename, {"metric": col, "mode": "previous"})
        except ValueError as exc:
            skipped.append({"metric": col, "reason": str(exc)})
            continue
        top = r["findings"][0] if r["findings"] else None
        base = r["total_a"]
        rows.append({
            "metric": r["metric"], "formula": r["formula"], "total_a": r["total_a"],
            "total_b": r["total_b"], "change": r["change"],
            "change_pct": (r["change"] / abs(base)) if base else None,
            "verdict": r["verdict"],
            "top": {"label": top["label"], "share_of_change": top["share_of_change"]}
            if top else None,
            "period_a": r["period_a"], "period_b": r["period_b"],
        })
    rows.sort(key=lambda x: -abs(x["change_pct"]) if x["change_pct"] is not None else 0)
    return clean_json({
        "kind": "scan", "rows": rows, "skipped": skipped,
        "scanned": len(cols), "total_columns": len(meta.get("metric_cols") or []),
        "caveat": "A scan ranks columns by how much they changed between the two default "
                  "periods. It shows where to look first. It does not say why anything moved.",
    })
