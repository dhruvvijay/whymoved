"""Plain-English summary of an explanation, built only from the computed numbers.

Rules the text follows:
    * Every number comes from the analysis objects; nothing is invented or estimated here.
    * It says WHERE a metric moved, never WHY. Words that claim a cause ("caused", "because",
      "due to", "led to", "driven by") are never used; tests enforce this.
    * Uncertainty is part of the sentence: a 95% interval accompanies each finding, and a
      weak or missing signal is stated plainly instead of being dressed up.
    * ASCII only (no unicode minus, no currency symbol unless the caller passes one), so the
      text prints safely in any terminal.
"""

from __future__ import annotations

import math
import re

from whymoved.baseline import TrendCheck
from whymoved.ingest import ValidationReport
from whymoved.market import MarketBridge
from whymoved.pvm import EFFECTS, Bridge
from whymoved.statements import PERCENT_RATIOS, DuPont
from whymoved.trends import SeasonalSplit
from whymoved.trust import TrustedExplanation, TrustedFinding

# Engine notes that repeat what the verdict paragraph already says.
_SAID_ABOVE = (
    "No segment moved clearly",
    "The findings moved",
    "The net change is only",
    "The findings explain",
    "Findings account for more than 100%",
)

CAVEAT = (
    "This shows where the metric moved, not why it moved. Working out why takes someone "
    "who knows the business."
)


def format_number(
    value: float, decimals: int = 0, currency: str = "", indian: bool = False, sign: bool = False
) -> str:
    """1234567.8 -> '1,234,568' (or '12,34,568' with indian=True); `sign` forces + or -."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    text = f"{abs(value):.{decimals}f}"
    whole, _, frac = text.partition(".")
    if indian and len(whole) > 3:
        head = re.sub(r"(\d)(?=(\d{2})+$)", r"\1,", whole[:-3])
        whole = f"{head},{whole[-3:]}"
    elif not indian:
        whole = f"{int(whole):,}"
    body = f"{currency}{whole}" + (f".{frac}" if frac else "")
    if value < 0 and float(text) != 0:
        return f"-{body}"
    return f"+{body}" if sign and float(text) != 0 else body


def _pct(value: float, decimals: int = 1, sign: bool = False) -> str:
    if value is None or math.isnan(value) or math.isinf(value):
        return "n/a"
    text = f"{value * 100:.{decimals}f}%"
    return f"+{text}" if sign and value > 0 else text


def _label(finding: TrustedFinding) -> str:
    return " and ".join(f"{k} = {v}" for k, v in finding.filters.items())


class _Fmt:
    """Formats values of one metric consistently (percent for rates, amounts otherwise)."""

    def __init__(self, ex: TrustedExplanation, currency: str, indian: bool):
        self.ratio = ex.metric.is_ratio
        self.percent = self.ratio and abs(ex.total_a) <= 1 and abs(ex.total_b) <= 1
        self.currency = "" if self.ratio else currency
        self.indian = indian

    def value(self, v: float) -> str:
        if self.percent:
            return _pct(v, 2)
        return format_number(v, 2 if self.ratio else 0, self.currency, self.indian)

    def effect(self, v: float) -> str:
        if self.percent:
            return f"{v * 100:+.2f} pp"
        return format_number(v, 2 if self.ratio else 0, self.currency, self.indian, sign=True)


def _headline(ex: TrustedExplanation, f: _Fmt, labels: tuple[str, str]) -> str:
    name = ex.metric.name[:1].upper() + ex.metric.name[1:]
    change = ex.total_b - ex.total_a
    rel = change / ex.total_a if ex.total_a else float("nan")
    if f.percent:
        delta = f"{f.effect(change)}, {_pct(rel, 1, sign=True)} relative"
    else:
        delta = _pct(rel, 1, sign=True)
    if ex.reference == "budget":
        verb = "came in at" if change else "matched"
        return (f"{name} {verb} {f.value(ex.total_b)} against a budget of {f.value(ex.total_a)} "
                f"({delta}).")
    if abs(rel) < 0.0005:
        return (f"{name} was essentially flat between {labels[0]} and {labels[1]} "
                f"({f.value(ex.total_a)} to {f.value(ex.total_b)}).")
    verb = "rose" if change > 0 else "fell"
    return (f"{name} {verb} from {f.value(ex.total_a)} to {f.value(ex.total_b)} "
            f"({delta}) between {labels[0]} and {labels[1]}.")


def _finding_sentence(finding: TrustedFinding, f: _Fmt, change: float) -> str:
    amount = f.effect(finding.explained)
    interval = f"95% interval {f.effect(finding.ci_low)} to {f.effect(finding.ci_high)}"
    share = finding.share_of_change
    where = _label(finding)
    if change != 0 and 0 < share <= 1.25:
        text = (f"{where} accounts for {_pct(share, 0)} of the net change "
                f"({amount}; {interval}).")
    elif change != 0 and share < 0:
        text = f"{where} moved by {amount}, against the net change ({interval})."
    else:
        text = (f"{where} moved by {amount}, more than the whole net change; the rest of "
                f"the data moved the other way ({interval}).")
    if f.ratio and finding.mix_effect is not None and finding.rate_effect is not None:
        mix, rate = finding.mix_effect, finding.rate_effect
        total = mix + rate
        if mix * rate < 0:
            text += (" Its mix effect (traffic moving between segments) and its own rate effect "
                     "pulled in opposite directions.")
        elif total != 0 and abs(mix) >= 2 * abs(rate):
            text += (f" About {_pct(mix / total, 0)} of that is mix (traffic moving between "
                     "segments); the segment's own rate changed little.")
        elif total != 0 and abs(rate) >= 2 * abs(mix):
            text += (f" About {_pct(rate / total, 0)} of that is the segment's own rate "
                     "changing; the mix of traffic shifted little.")
        elif total != 0:
            text += " Both mix (traffic moving between segments) and the segment's own rate moved."
    return text


def _trend_sentence(trend: TrendCheck) -> str:
    if not trend.available:
        return f"Trend check not available: {trend.reason}."
    exp = _pct(trend.expected_change_pct, 1, sign=True)
    act = _pct(trend.actual_change_pct, 1, sign=True)
    z = f"z = {trend.z:+.1f}"
    if trend.verdict == "within_expected":
        return (f"Trend check: the recent weekly trend pointed to {exp}; the actual change of "
                f"{act} is within normal week-to-week variation ({z}). On its own, the net "
                "figure would not stand out.")
    side = "lower" if trend.verdict == "below_expected" else "higher"
    return (f"Trend check: the recent weekly trend pointed to {exp}; the actual change of {act} "
            f"is {side} than that by more than normal week-to-week variation ({z}).")


def describe(
    ex: TrustedExplanation,
    *,
    trend: TrendCheck | None = None,
    validation: ValidationReport | None = None,
    currency: str = "",
    indian_grouping: bool = False,
    period_labels: tuple[str, str] = ("period A", "period B"),
) -> str:
    """Return a short multi-paragraph summary for `ex`."""
    f = _Fmt(ex, currency, indian_grouping)
    change = ex.total_b - ex.total_a
    paragraphs = [_headline(ex, f, period_labels)]

    lines: list[str] = []
    if ex.verdict == "negligible_change":
        lines.append("The change is very small, too small to be worth explaining.")
    elif ex.verdict == "no_clear_driver":
        lines.append("No single segment stands out. The change is spread across the data, "
                     "or is within normal day-to-day noise.")
    elif ex.verdict == "offsetting_moves":
        lines.append("The net figure hides opposing moves.")
        lines += [_finding_sentence(x, f, change) for x in ex.findings]
    elif ex.verdict == "partial_driver":
        lines.append(f"Part of the change sits in specific segments "
                     f"({_pct(ex.explained_share, 0)} of it); the rest is spread out.")
        lines += [_finding_sentence(x, f, change) for x in ex.findings]
    else:  # clear_driver
        first, *rest = ex.findings
        lines.append("Most of the change sits in one place. "
                     + _finding_sentence(first, f, change))
        if rest:
            lines.append("Also moving: " + " ".join(_finding_sentence(x, f, change) for x in rest))
    body = "\n".join(lines)
    if ex.reference == "budget":
        body = body.replace("net change", "net gap").replace("the change", "the gap")
    paragraphs.append(body)

    if trend is not None and ex.reference != "budget":
        paragraphs.append(_trend_sentence(trend))
    notes = [n for n in ex.notes if not n.startswith(_SAID_ABOVE)]
    if notes:
        paragraphs.append("Notes: " + " ".join(n if n.endswith(".") else n + "." for n in notes))
    if validation is not None and validation.warnings:
        codes = sorted({i.code for i in validation.warnings})
        paragraphs.append(
            f"Data notes: the file check raised {len(validation.warnings)} warning(s) "
            f"({', '.join(codes[:3])}{', ...' if len(codes) > 3 else ''}); "
            "see the validation report."
        )
    paragraphs.append(CAVEAT)
    return "\n\n".join(paragraphs)


_EFFECT_TEXT = {
    "volume": "more or fewer units overall",
    "mix": "units shifting between segments with different prices",
    "price": "a different average price per unit within segments",
    "cost": "a different cost per unit within segments",
}


def describe_bridge(
    bridge: Bridge, *, currency: str = "", indian_grouping: bool = False,
    period_labels: tuple[str, str] = ("period A", "period B"), top: int = 2,
) -> str:
    """Plain-English summary of a price / volume / mix (/ cost) bridge."""
    def money(v: float, sign: bool = True) -> str:
        return format_number(v, 0, currency, indian_grouping, sign=sign)

    name = bridge.target.capitalize()
    change = bridge.change
    verb = "rose" if change > 0 else "fell"
    out = [f"{name} {verb} from {money(bridge.value_a, False)} to {money(bridge.value_b, False)} "
           f"({money(change)}) between {period_labels[0]} and {period_labels[1]}. The change adds "
           "up as:"]
    lines = []
    used = [e for e in EFFECTS if e != "cost" or bridge.target == "profit"]
    for e in used:
        amount = bridge.totals[e]
        text = f"  {e:<7}{money(amount):>16}   ({_EFFECT_TEXT[e]}"
        if e in bridge.ci:
            lo, hi = bridge.ci[e]
            text += f"; 95% interval {money(lo)} to {money(hi)}"
            if lo <= 0 <= hi:
                text += "; could be noise"
        lines.append(text + ")")
    out.append("\n".join(lines))
    moves = []
    for e in used:
        lo, hi = bridge.ci.get(e, (0.0, 0.0))
        if not bridge.ci or lo <= 0 <= hi:
            continue
        col = bridge.table[e]
        if bridge.totals[e] == 0:
            continue
        seg = col.abs().sort_values(ascending=False).head(top)
        names = ", ".join(
            f"{' / '.join(map(str, k)) if isinstance(k, tuple) else k} ({money(col[k])})"
            for k in seg.index)
        moves.append(f"{e.capitalize()} effect sits mostly in {names}.")
    if moves:
        out.append(" ".join(moves))
    out.append("Segment mix figures are relative to the average: a segment can show a positive "
               "mix effect when its units grew faster than the total, whatever its own price did.")
    out += list(bridge.notes)
    out.append(CAVEAT)
    return "\n\n".join(out)


_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"]


def describe_market(
    bridge: MarketBridge, *, currency: str = "", indian_grouping: bool = False,
    period_labels: tuple[str, str] = ("period A", "period B"), top: int = 2,
) -> str:
    """Plain-English summary of a market-share bridge."""
    def money(v: float, sign: bool = True) -> str:
        return format_number(v, 0, currency, indian_grouping, sign=sign)

    ours, mkt = bridge.our_growth, bridge.market_growth
    gap_pp = (bridge.share_b - bridge.share_a) * 100
    if abs(ours - mkt) < 0.005:
        stance = "about in line with the market"
    else:
        stance = "ahead of the market" if ours > mkt else "behind the market"
    out = [
        f"Between {period_labels[0]} and {period_labels[1]} our value moved {_pct(ours, 1, True)} "
        f"while the market moved {_pct(mkt, 1, True)}: {stance}. Our share went from "
        f"{_pct(bridge.share_a, 2)} to {_pct(bridge.share_b, 2)} ({gap_pp:+.2f} pp)."
    ]
    labels = {"market": "growing at the market's overall rate",
              "exposure": "being in segments where the market grew faster or slower than overall",
              "share": "winning or losing share inside segments"}
    lines = []
    for p in ("market", "exposure", "share"):
        text = f"  {p:<9}{money(bridge.totals[p]):>16}   ({labels[p]}"
        if p in bridge.ci:
            lo, hi = bridge.ci[p]
            text += f"; 95% interval {money(lo)} to {money(hi)}"
            if lo <= 0 <= hi:
                text += "; could be noise"
        lines.append(text + ")")
    out.append(f"The change of {money(bridge.revenue_b - bridge.revenue_a)} adds up as:\n"
               + "\n".join(lines))
    lo, hi = bridge.ci.get("share", (0.0, 0.0))
    if bridge.ci and not (lo <= 0 <= hi) and bridge.totals["share"] != 0:
        col = bridge.table["share"]
        seg = col.abs().sort_values(ascending=False).head(top)
        names = ", ".join(
            f"{' / '.join(map(str, k)) if isinstance(k, tuple) else k} ({money(col[k])})"
            for k in seg.index)
        word = "lost" if bridge.totals["share"] < 0 else "gained"
        out.append(f"Most of the share we {word} sits in {names}.")
    elif bridge.ci:
        out.append("The share effect is within normal day-to-day variation, so no segment is "
                   "singled out.")
    out += list(bridge.notes)
    out.append("Market figures are only as reliable as their source.")
    out.append(CAVEAT)
    return "\n\n".join(out)


def describe_trend(split: SeasonalSplit, latest: dict, *, label: str = "value") -> str:
    """Plain-English summary of a monthly trend and its seasonal pattern."""
    month = latest["month"]
    name = f"{_MONTHS[month.month - 1]} {month.year}"
    parts = [f"Latest month ({name}): {label} {_pct(latest['yoy'], 1, True)} on the same month "
             f"last year, {_pct(latest['mom'], 1, True)} on the previous month."]
    if "ttm_growth" in latest:
        parts[0] += (f" The last 12 months together are {_pct(latest['ttm_growth'], 1, True)} "
                     "on the 12 months before.")
    idx = split.seasonal_index
    peak, low = split.peak_month, split.low_month
    kind = "multiplier" if split.model == "multiplicative" else "adjustment"
    strength = _pct(split.seasonal_strength, 0)
    parts.append(
        f"Seasonal pattern ({split.years:.0f} years of history): the strongest month is "
        f"{_MONTHS[peak - 1]} (seasonal {kind} {idx[peak]:.2f}) and the weakest is "
        f"{_MONTHS[low - 1]} ({idx[low]:.2f}). The pattern accounts for about {strength} of the "
        "variation around the trend. Month-on-month moves should be read against this pattern; "
        "year-on-year already cancels it.")
    parts += split.notes
    parts.append(CAVEAT)
    return "\n\n".join(parts)


_RATIO_GROUPS = {
    "Profitability": ["gross_margin", "ebitda_margin", "operating_margin", "net_margin", "roe",
                      "roa"],
    "Liquidity": ["current_ratio", "quick_ratio", "cash_ratio"],
    "Leverage": ["debt_to_equity", "debt_to_assets", "interest_coverage"],
    "Efficiency": ["asset_turnover", "inventory_days", "receivable_days", "payable_days",
                   "cash_conversion_days"],
}


def _ratio_text(name: str, value: float) -> str:
    if value is None or math.isnan(value):
        return "n/a"
    if name in PERCENT_RATIOS:
        return f"{value * 100:.1f}%"
    if name.endswith("_days"):
        return f"{value:.1f} days"
    return f"{value:.2f}x"


def _ratio_move(name: str, a: float, b: float) -> str:
    if math.isnan(a) or math.isnan(b):
        return f"{name.replace('_', ' ')}: {_ratio_text(name, a)} to {_ratio_text(name, b)}"
    diff = b - a
    if name in PERCENT_RATIOS:
        change = f"{diff * 100 + 0.0:+.1f} pp"
    elif name.endswith("_days"):
        change = f"{diff + 0.0:+.1f} days"
    else:
        change = f"{diff + 0.0:+.2f}"
    change = change.replace("-0.0 pp", "0.0 pp").replace("-0.0 days", "0.0 days")
    change = "0.00" if change == "-0.00" else change
    return (f"{name.replace('_', ' ')}: {_ratio_text(name, a)} to {_ratio_text(name, b)} "
            f"({change})")


def describe_ratios(ratios, period_a: str, period_b: str, dupont: DuPont | None = None,
                    *, intro: str | None = None, notes: list[str] | None = None) -> str:
    """Plain-English comparison of two periods of statement ratios. No good/bad judgements."""
    for p in (period_a, period_b):
        if p not in ratios.index:
            raise ValueError(f"period {p!r} not found")
    a, b = ratios.loc[period_a], ratios.loc[period_b]
    out = [intro] if intro else []
    out += [f"Ratios for {period_a} compared with {period_b}, worked out from the statements "
           "supplied."]
    for group, names in _RATIO_GROUPS.items():
        lines = [f"  {_ratio_move(n, float(a[n]), float(b[n]))}" for n in names if n in ratios]
        if lines:
            out.append(group + ":\n" + "\n".join(lines))
    if dupont is not None:
        parts = {"net margin": dupont.margin, "asset turnover": dupont.turnover,
                 "leverage (equity multiplier)": dupont.leverage}
        lines = [f"  {k:<30}{(v * 100 + 0.0):+.1f} pp".replace("-0.0 pp", "0.0 pp")
                 for k, v in parts.items()]
        out.append(
            f"Return on equity went from {_pct(dupont.roe_a, 1)} to {_pct(dupont.roe_b, 1)} "
            f"({dupont.change * 100:+.1f} pp). Split into three parts that add up to that change:\n"
            + "\n".join(lines)
            + "\nThe split depends on the order the three factors are changed (margin, then "
              "turnover, then leverage).")
    out += notes or []
    out.append("These are arithmetic on the figures supplied. What counts as a high or low level "
               "depends on the industry and on accounting choices, and no benchmark is applied.")
    out.append(CAVEAT)
    return "\n\n".join(out)


_LEVEL_WORDS = [("revenue", "Revenue"), ("ebitda", "Operating profit"),
                ("pbt", "Profit before tax"), ("net_income", "Net profit"),
                ("total_assets", "Total assets"), ("equity", "Equity"),
                ("borrowings", "Borrowings")]


def describe_levels(lines, period_a: str, period_b: str, *, currency: str = "",
                    indian_grouping: bool = False) -> str:
    """Opening sentences: the main statement lines in the two periods and their change."""
    out = []
    for key, name in _LEVEL_WORDS:
        if key not in lines.columns:
            continue
        va, vb = float(lines.at[period_a, key]), float(lines.at[period_b, key])
        if math.isnan(va) or math.isnan(vb):
            continue
        move = f" ({_pct(vb / va - 1, 1, True)})" if va > 0 else ""
        out.append(f"{name} went from {format_number(va, 0, currency, indian_grouping)} to "
                   f"{format_number(vb, 0, currency, indian_grouping)}{move}.")
    return " ".join(out)


def describe_growth_only(latest: dict, *, label: str = "value") -> str:
    """Trend summary when there is too little history for a seasonal split."""
    month = latest["month"]
    name = f"{_MONTHS[month.month - 1]} {month.year}"
    text = (f"Latest month ({name}): {label} {_pct(latest['yoy'], 1, True)} on the same month "
            f"last year, {_pct(latest['mom'], 1, True)} on the previous month.")
    if "ttm_growth" in latest:
        text += (f" The last 12 months together are {_pct(latest['ttm_growth'], 1, True)} on "
                 "the 12 months before.")
    return text + "\n\nThere is too little history for a seasonal split.\n\n" + CAVEAT
