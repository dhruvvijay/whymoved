"""The analyses behind the app beyond a plain "what moved": financial statements, price / volume /
mix, market share and trends. Each runner takes the stored file and the user's settings and
returns plain JSON data with a written summary, a caveat and notes. No runner claims a cause.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from whymoved.ingest import DataError, default_periods, prepare
from whymoved.market import market_share_bridge
from whymoved.metrics import build_metric
from whymoved.narrative import (
    _RATIO_GROUPS,
    CAVEAT,
    describe_bridge,
    describe_growth_only,
    describe_levels,
    describe_market,
    describe_ratios,
    describe_trend,
)
from whymoved.pvm import price_volume_mix
from whymoved.service.jobs import (
    _iso,
    _issues,
    _parse_period,
    clean_json,
    suggest_columns,
)
from whymoved.statement_import import (
    ROLE_INFO,
    StatementImportError,
    build_lines,
    check_lines,
    growth_from_lines,
    looks_financial,
    ratios_from_lines,
    read_statements,
    suggest_roles,
    validate_mapping,
)
from whymoved.statements import PERCENT_RATIOS, RATIO_INFO, StatementError, dupont_change
from whymoved.trends import (
    TrendError,
    growth_table,
    latest_summary,
    monthly_series,
    seasonal_split,
)
from whymoved.watchlist import takeaways, watchlist

RATIO_LABELS = {
    "gross_margin": "Gross margin",
    "ebitda_margin": "Operating (EBITDA) margin",
    "operating_margin": "EBIT margin",
    "net_margin": "Net margin",
    "current_ratio": "Current ratio",
    "quick_ratio": "Quick ratio",
    "cash_ratio": "Cash ratio",
    "debt_to_equity": "Debt to equity",
    "debt_to_assets": "Debt to assets",
    "interest_coverage": "Interest coverage",
    "roe": "Return on equity",
    "roa": "Return on assets",
    "asset_turnover": "Asset turnover",
    "equity_multiplier": "Equity multiplier",
    "inventory_days": "Inventory days",
    "receivable_days": "Receivable days",
    "payable_days": "Payable days",
    "cash_conversion_days": "Cash conversion days",
}
LINE_LABELS = {role: info[0] for role, info in ROLE_INFO.items()}
LINE_LABELS.update({"gross_profit": "Gross profit", "ebit": "EBIT"})


def _read(path: Path, filename: str, need_dims: bool, **kw):
    with open(path, "rb") as handle:
        return prepare(handle, filename=filename, require_dimensions=need_dims, **kw)


def _pick(wanted, guess, available: list[str], what: str) -> str:
    column = wanted or guess
    if not column:
        raise ValueError(f"choose the {what} column")
    if column not in available:
        raise ValueError(f"{what} column {str(column)[:60]!r} is not a number column of this file")
    return column


def _load_two_periods(path: Path, filename: str, params: dict):
    """(prepared, dims, period_a, period_b) for the analyses that compare two periods."""
    first = _read(path, filename, True)
    dims = params.get("dimensions") or first.dimension_cols
    unknown = [d for d in dims if d not in first.dimension_cols]
    if unknown:
        raise ValueError(f"unknown category column(s): {', '.join(unknown)}")
    if not dims:
        raise ValueError("this file has no category column to split by")
    if params.get("period_a") and params.get("period_b"):
        period_a = _parse_period(params["period_a"], "period_a")
        period_b = _parse_period(params["period_b"], "period_b")
    else:
        found = default_periods(first)
        if found is None:
            raise ValueError("could not choose periods automatically (needs daily data over at "
                             "least 14 days); give both periods")
        period_a, period_b = found
    p = _read(path, filename, True, period_a=period_a, period_b=period_b,
              dimension_cols=list(dims))
    if not p.report.ok:
        raise DataError("; ".join(i.message for i in p.report.errors))
    return p, list(dims), period_a, period_b


def _seg_label(key) -> str:
    return " / ".join(map(str, key)) if isinstance(key, tuple) else str(key)


def _currency(params: dict) -> str:
    text = str(params.get("currency") or "").strip()[:8]
    return text + " " if text and text[-1].isalpha() else text


def _ci(ci: dict) -> dict:
    return {k: [lo, hi] for k, (lo, hi) in ci.items()}


def _bridge_waterfall(start_label, start, effects: dict, end_label, end) -> list[dict]:
    steps = [{"label": start_label, "value": start, "kind": "total"}]
    steps += [{"label": name, "value": value, "kind": "finding"} for name, value in effects.items()]
    steps.append({"label": end_label, "value": end, "kind": "total"})
    return steps


# --------------------------------------------------------------------------- price / volume / mix


def run_pvm(path: Path, filename: str, params: dict) -> dict:
    p, dims, period_a, period_b = _load_two_periods(path, filename, params)
    guess = suggest_columns(p.metric_cols)
    revenue = _pick(params.get("revenue"), guess["revenue"], p.metric_cols, "revenue")
    units = _pick(params.get("units"), guess["units"], p.metric_cols, "units")
    cost = params.get("cost") or None
    if cost is not None:
        cost = _pick(cost, None, p.metric_cols, "cost")
    if len({revenue, units, cost or ""} - {""}) != len([c for c in (revenue, units, cost) if c]):
        raise ValueError("revenue, units and cost must be three different columns")
    bridge = price_volume_mix(p.data, period_a, period_b, dims, revenue=revenue, units=units,
                              cost=cost, date_col=p.date_col)
    currency, indian = _currency(params), bool(params.get("indian"))

    def label(period):
        return f"{period[0].date()} to {period[1].date()}"

    summary = describe_bridge(bridge, currency=currency, indian_grouping=indian,
                              period_labels=(label(period_a), label(period_b)))
    effects = ["volume", "mix", "price"] + (["cost"] if cost else [])
    name = bridge.target.capitalize()
    waterfall = _bridge_waterfall(f"{name}, earlier period", bridge.value_a,
                                  {e.capitalize(): bridge.totals[e] for e in effects},
                                  f"{name}, later period", bridge.value_b)
    segments = []
    for key, row in bridge.table.head(15).iterrows():
        seg = {"label": _seg_label(key)}
        for col in ("units_a", "units_b", "price_a", "price_b", *effects, "total"):
            seg[col] = row[col]
        segments.append(seg)
    return clean_json({
        "kind": "pvm", "metric": f"{name} bridge", "target": bridge.target, "mode": "pvm",
        "is_ratio": False, "total_a": bridge.value_a, "total_b": bridge.value_b,
        "change": bridge.change, "verdict": None,
        "period_a": _iso(period_a), "period_b": _iso(period_b), "dimensions": dims,
        "columns": {"revenue": revenue, "units": units, "cost": cost},
        "effects": effects, "totals": bridge.totals, "ci": _ci(bridge.ci),
        "segments": segments, "segments_total": len(bridge.table), "waterfall": waterfall,
        "notes": bridge.notes, "summary": summary, "caveat": CAVEAT,
        "validation": _issues(p.report), "currency": currency, "indian": indian,
    })


# --------------------------------------------------------------------------- market share


def run_market(path: Path, filename: str, params: dict) -> dict:
    p, dims, period_a, period_b = _load_two_periods(path, filename, params)
    guess = suggest_columns(p.metric_cols)
    revenue = _pick(params.get("revenue"), guess["revenue"], p.metric_cols, "our value")
    market = _pick(params.get("market"), guess["market"], p.metric_cols, "market value")
    if revenue == market:
        raise ValueError("our value and the market value must be different columns")
    bridge = market_share_bridge(p.data, period_a, period_b, dims, revenue=revenue,
                                 market=market, date_col=p.date_col)
    currency, indian = _currency(params), bool(params.get("indian"))

    def label(period):
        return f"{period[0].date()} to {period[1].date()}"

    summary = describe_market(bridge, currency=currency, indian_grouping=indian,
                              period_labels=(label(period_a), label(period_b)))
    parts = ["market", "exposure", "share"]
    names = {"market": "Market growth", "exposure": "Segment mix", "share": "Share change"}
    waterfall = _bridge_waterfall("Our value, earlier period", bridge.revenue_a,
                                  {names[x]: bridge.totals[x] for x in parts},
                                  "Our value, later period", bridge.revenue_b)
    segments = []
    for key, row in bridge.table.head(15).iterrows():
        seg = {"label": _seg_label(key)}
        for col in ("revenue_a", "revenue_b", "share_a", "share_b", "our_growth",
                    "market_growth", *parts, "total"):
            seg[col] = row[col]
        segments.append(seg)
    return clean_json({
        "kind": "market", "metric": "Market share bridge", "mode": "market", "is_ratio": False,
        "total_a": bridge.revenue_a, "total_b": bridge.revenue_b,
        "change": bridge.revenue_b - bridge.revenue_a, "verdict": None,
        "period_a": _iso(period_a), "period_b": _iso(period_b), "dimensions": dims,
        "columns": {"revenue": revenue, "market": market},
        "share_a": bridge.share_a, "share_b": bridge.share_b, "our_growth": bridge.our_growth,
        "market_growth": bridge.market_growth, "totals": bridge.totals, "ci": _ci(bridge.ci),
        "segments": segments, "segments_total": len(bridge.table), "waterfall": waterfall,
        "notes": bridge.notes, "summary": summary, "caveat": CAVEAT,
        "validation": _issues(p.report), "currency": currency, "indian": indian,
    })


# --------------------------------------------------------------------------- trends


def _trim_partial_months(df: pd.DataFrame, date_col: str) -> tuple[pd.DataFrame, list[str]]:
    """Daily data: drop the first and last calendar month if they are less than 90% covered,
    because a partial month's total is not comparable with a full one."""
    days = df[date_col].dt.normalize().drop_duplicates()
    per = days.dt.to_period("M")
    counts = per.value_counts()
    drop = [m for m in {per.min(), per.max()} if counts[m] < 0.9 * m.days_in_month]
    if not drop:
        return df, []
    keep = ~df[date_col].dt.to_period("M").isin(drop)
    notes = [f"Left out {', '.join(sorted(str(m) for m in drop))}: the month is only partly "
             "covered by the data, so its total is not comparable with a full month."]
    return df[keep].reset_index(drop=True), notes


def run_trends(path: Path, filename: str, params: dict) -> dict:
    p = _read(path, filename, False)
    freq = p.info.get("frequency")
    if freq not in ("daily", "monthly"):
        raise ValueError(f"trend analysis needs daily or monthly data; this file looks "
                         f"{freq}")
    formula = (params.get("metric") or p.metric_cols[0]).strip()
    data, spec = build_metric(p.data, formula)
    notes: list[str] = []
    if freq == "daily":
        data, notes = _trim_partial_months(data, p.date_col)
    series = monthly_series(data, spec.numerator, p.date_col, denominator=spec.denominator)
    months = int(series.notna().sum())
    if months < 13:
        raise ValueError(f"trend analysis needs at least 13 months of data; this file has "
                         f"{months}")
    latest = latest_summary(series)
    table = growth_table(series)
    model = params.get("model") or "multiplicative"
    split, seasonal_note = None, None
    if len(series) >= 24 and not series.isna().any():
        try:
            split = seasonal_split(series, model)
        except TrendError as exc:
            seasonal_note = str(exc)
    else:
        seasonal_note = ("A seasonal split needs 24 months in a row with no gaps; this series "
                         f"has {len(series)} months" + (" with gaps." if series.isna().any()
                                                         else "."))
    label = spec.name
    summary = (describe_trend(split, latest, label=label) if split
               else describe_growth_only(latest, label=label))
    if seasonal_note and split is None:
        notes.append(seasonal_note)
    rows = []
    for month in series.index:
        row = {"month": month.strftime("%Y-%m"), "value": series[month],
               "mom": table.at[month, "mom"], "yoy": table.at[month, "yoy"],
               "ma3": table.at[month, "ma3"], "ma12": table.at[month, "ma12"]}
        if split is not None:
            row["trend"] = split.table.at[month, "trend"]
            row["seasonal"] = split.table.at[month, "seasonal"]
        rows.append(row)
    out = {
        "kind": "trends", "metric": label, "mode": "trends", "is_ratio": spec.is_ratio,
        "total_a": float(series.dropna().iloc[0]), "total_b": float(series.dropna().iloc[-1]),
        "verdict": None, "formula": formula, "months": months, "series": rows,
        "latest": {**latest, "month": latest["month"].strftime("%Y-%m")},
        "seasonal": None, "notes": notes + (split.notes if split else []), "summary": summary,
        "caveat": CAVEAT, "validation": _issues(p.report), "currency": _currency(params),
        "indian": bool(params.get("indian")), "dimensions": [],
    }
    if split is not None:
        out["seasonal"] = {
            "model": split.model, "index": [float(split.seasonal_index[m]) for m in range(1, 13)],
            "strength": split.seasonal_strength, "years": split.years,
            "peak_month": split.peak_month, "low_month": split.low_month}
    return clean_json(out)


# --------------------------------------------------------------------------- statements


def _unit(name: str) -> str:
    if name in PERCENT_RATIOS:
        return "percent"
    return "days" if name.endswith("_days") else "x"


def run_statements(path: Path, filename: str, params: dict) -> dict:
    try:
        table = read_statements(Path(path).read_bytes(), filename)
        mapping = validate_mapping(table, params.get("mapping") or suggest_roles(table))
    except StatementImportError as exc:
        raise ValueError(str(exc)) from exc
    if not mapping:
        raise ValueError("choose at least one line item (revenue and net profit are a good "
                         "start)")
    periods = list(table.frame.columns)
    a = params.get("from_period") or periods[-2]
    b = params.get("to_period") or periods[-1]
    for label_, value in (("from_period", a), ("to_period", b)):
        if value not in periods:
            raise ValueError(f"{label_} {str(value)[:40]!r} is not one of the periods: "
                             f"{', '.join(periods)}")
    if periods.index(a) >= periods.index(b):
        raise ValueError("the first period must come before the second")
    lines, basis = build_lines(table, mapping)
    financial_reason = looks_financial(table.company, lines)
    override = params.get("financial")
    financial = bool(override) if isinstance(override, bool) else financial_reason is not None
    ratios, unavailable = ratios_from_lines(lines, financial=financial)
    growth = growth_from_lines(lines)
    issues = check_lines(lines, table, mapping)
    currency, indian = _currency(params), bool(params.get("indian"))

    dupont = None
    needed = ("net_margin", "asset_turnover", "equity_multiplier", "roe")
    dupont_note = None
    if all(c in ratios for c in needed):
        try:
            dupont = dupont_change(ratios, a, b)
        except StatementError as exc:
            dupont_note = f"The ROE split was not worked out: {exc}."
    else:
        dupont_note = ("The ROE split needs revenue, net profit, total assets and equity; at "
                       "least one of them was not chosen.")
    notes = list(table.notes) + basis + ([dupont_note] if dupont_note else [])
    if financial:
        why = financial_reason or "you marked it as a bank or finance company"
        notes.append(f"Treated as a bank or finance company ({why}), so ratios built for goods "
                     "businesses (margins on operating profit, interest cover, debt to equity, "
                     "current ratios, stock and debtor days) are not shown.")
    missing = {k: v for k, v in unavailable.items() if not v.startswith("not shown")}
    if missing:
        shown = "; ".join(f"{RATIO_LABELS[k]} ({v})" for k, v in missing.items())
        notes.append(f"Not worked out (a line they need is missing): {shown}.")
    if financial:  # operating profit and gross profit mean something else for a bank
        lines = lines.drop(columns=[c for c in ("ebitda", "ebit", "gross_profit")
                                    if c in lines.columns])
    intro = describe_levels(lines, a, b, currency=currency, indian_grouping=indian)
    if ratios.empty:
        summary = "\n\n".join(x for x in (
            intro, "No ratio could be worked out from the lines chosen.", *notes, CAVEAT) if x)
    else:
        summary = describe_ratios(ratios, a, b, dupont, intro=intro, notes=notes)
    waterfall = None
    if dupont is not None:
        waterfall = [
            {"label": f"ROE {a}", "value": dupont.roe_a, "kind": "total"},
            {"label": "Net margin", "value": dupont.margin, "kind": "finding"},
            {"label": "Asset turnover", "value": dupont.turnover, "kind": "finding"},
            {"label": "Leverage", "value": dupont.leverage, "kind": "finding"},
            {"label": f"ROE {b}", "value": dupont.roe_b, "kind": "total"}]
    ordered = [c for c in LINE_LABELS if c in lines.columns]
    groups = [{"group": g, "keys": [k for k in names if k in ratios.columns]}
              for g, names in _RATIO_GROUPS.items()]
    result = clean_json({
        "kind": "statements", "metric": "Financial statements", "mode": "statements",
        "company": table.company, "financial": financial,
        "is_ratio": True, "verdict": None, "total_a": None, "total_b": None,
        "periods": periods, "period_a": a, "period_b": b, "sheets": table.sheets,
        "lines": [{"key": c, "label": LINE_LABELS[c], "values": lines[c].tolist()}
                  for c in ordered],
        "ratios": [{"key": c, "label": RATIO_LABELS[c], "formula": RATIO_INFO[c],
                    "unit": _unit(c), "values": ratios[c].tolist()} for c in ratios.columns],
        "ratio_groups": [g for g in groups if g["keys"]],
        "growth": [{"key": c, "label": LINE_LABELS[c], "values": growth[c].tolist()}
                   for c in growth.columns],
        "dupont": None if dupont is None else {
            "roe_a": dupont.roe_a, "roe_b": dupont.roe_b, "margin": dupont.margin,
            "turnover": dupont.turnover, "leverage": dupont.leverage, "change": dupont.change},
        "waterfall": waterfall,
        "unavailable": [{"ratio": k, "label": RATIO_LABELS[k], "reason": v}
                        for k, v in unavailable.items()
                        if not v.startswith("not shown")],
        "checks": issues, "notes": notes,
        "mapping": {role: {"key": key, "label": ROLE_INFO[role][0]}
                    for role, key in mapping.items()},
        "summary": summary, "caveat": CAVEAT, "validation": [], "dimensions": [],
        "currency": currency, "indian": indian,
    })
    result["watchlist"] = watchlist(result)
    result["takeaways"] = takeaways(result)
    return result


RUNNERS = {"pvm": run_pvm, "market": run_market, "trends": run_trends,
           "statements": run_statements}
