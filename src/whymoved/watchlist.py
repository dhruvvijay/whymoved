"""A watchlist and a few headline sentences for a financial-statement result.

Every rule is a plain comparison of numbers that are already in the result, and every flag shows
the numbers it used. A flag means "worth a closer look", never "something is wrong", and no rule
says why a number moved. Rules whose inputs are missing are skipped and listed as not checked.
"""

from __future__ import annotations

from collections.abc import Callable

LOOK, NOTE = "look", "note"


def _num(v) -> float | None:
    return v if isinstance(v, (int, float)) and v == v else None


class _View:
    """Series of a statements result, looked up by key, up to and including the later period."""

    def __init__(self, result: dict):
        self.periods: list[str] = list(result["periods"])
        self.end = self.periods.index(result["period_b"])
        self.start = self.periods.index(result["period_a"])
        self.ratios = {r["key"]: r for r in result.get("ratios", [])}
        self.lines = {x["key"]: x for x in result.get("lines", [])}
        self.growth = {g["key"]: g for g in result.get("growth", [])}
        self.dupont = result.get("dupont")
        self.financial = bool(result.get("financial"))

    def ratio(self, key: str) -> list | None:
        return self.ratios[key]["values"] if key in self.ratios else None

    def line(self, key: str) -> list | None:
        return self.lines[key]["values"] if key in self.lines else None

    def at(self, values: list | None, back: int = 0) -> float | None:
        i = self.end - back
        return _num(values[i]) if values is not None and 0 <= i < len(values) else None

    def evidence(self, label: str, values: list, unit: str, back: int) -> list[dict]:
        out = []
        for i in range(max(0, self.end - back), self.end + 1):
            if _num(values[i]) is not None:
                out.append({"label": label, "period": self.periods[i], "value": values[i],
                            "unit": unit})
        return out


def _pct(v: float) -> str:
    return f"{v * 100:.1f}%"


def _flag(rule_id: str, level: str, title: str, detail: str, evidence: list[dict]) -> dict:
    return {"id": rule_id, "level": level, "title": title, "detail": detail,
            "evidence": evidence}


# Each rule returns a flag, None (checked, nothing found) or the string "skip" (inputs missing).
def _receivable_days(v: _View):
    s = v.ratio("receivable_days")
    now, before = v.at(s, 0), v.at(s, 2)
    if now is None or before is None or before <= 0:
        return "skip"
    if now / before - 1 >= 0.25 and now - before >= 3:
        return _flag("receivable_days", LOOK, "Customers take longer to pay",
                     f"Receivable days went from {before:.1f} to {now:.1f} over two periods "
                     f"({now / before - 1:+.0%}).", v.evidence("Receivable days", s, "days", 2))
    return None


def _inventory_days(v: _View):
    s = v.ratio("inventory_days")
    now, before = v.at(s, 0), v.at(s, 2)
    if now is None or before is None or before <= 0:
        return "skip"
    if now / before - 1 >= 0.25 and now - before >= 5:
        return _flag("inventory_days", LOOK, "Stock sits longer before it is sold",
                     f"Inventory days went from {before:.1f} to {now:.1f} over two periods "
                     f"({now / before - 1:+.0%}).", v.evidence("Inventory days", s, "days", 2))
    return None


def _margin_squeeze(v: _View):
    s, rev = v.ratio("ebitda_margin"), v.line("revenue")
    now, before = v.at(s, 0), v.at(s, 2)
    r_now, r_before = v.at(rev, 0), v.at(rev, 2)
    if None in (now, before, r_now, r_before):
        return "skip"
    if before - now >= 0.02 and r_now > r_before:
        return _flag("margin_squeeze", LOOK, "Revenue grew while the operating margin shrank",
                     f"Operating (EBITDA) margin went from {_pct(before)} to {_pct(now)} over two "
                     f"periods while revenue rose {r_now / r_before - 1:+.0%}.",
                     v.evidence("Operating margin", s, "percent", 2))
    return None


def _interest_cover(v: _View):
    s = v.ratio("interest_coverage")
    now, before = v.at(s, 0), v.at(s, 2)
    if now is None:
        return "skip"
    if now < 1.5:
        return _flag("interest_cover", LOOK, "Operating profit barely covers interest",
                     f"Interest coverage is {now:.2f}x; below 1.5x the profit from operations is "
                     "close to the interest bill.", v.evidence("Interest coverage", s, "x", 2))
    if before and before > 0 and now / before - 1 <= -0.25 and now < 4:
        return _flag("interest_cover", NOTE, "Interest coverage has fallen",
                     f"Interest coverage went from {before:.2f}x to {now:.2f}x over two periods.",
                     v.evidence("Interest coverage", s, "x", 2))
    return None


def _debt_trend(v: _View):
    s = v.ratio("debt_to_equity")
    vals = [v.at(s, b) for b in (3, 2, 1, 0)]
    if None in vals:
        return "skip"
    if vals[0] < vals[1] < vals[2] < vals[3] and vals[3] - vals[0] >= 0.1:
        return _flag("debt_trend", NOTE, "Debt to equity rose three periods running",
                     f"It went from {vals[0]:.2f}x to {vals[3]:.2f}x.",
                     v.evidence("Debt to equity", s, "x", 3))
    return None


def _profit_vs_sales(v: _View):
    rev, np_ = v.line("revenue"), v.line("net_income")
    r0, r1, n0, n1 = v.at(rev, 1), v.at(rev, 0), v.at(np_, 1), v.at(np_, 0)
    if None in (r0, r1, n0, n1) or r0 <= 0 or n0 <= 0:
        return "skip"
    rg, ng = r1 / r0 - 1, n1 / n0 - 1
    if ng - rg >= 0.20 and ng > 0:
        return _flag("profit_vs_sales", NOTE, "Profit grew much faster than sales",
                     f"Net profit {ng:+.0%} against revenue {rg:+.0%} in the latest period.",
                     v.evidence("Net profit", np_, "money", 1))
    return None


def _other_income(v: _View):
    oi, pbt = v.line("other_income"), v.line("pbt")
    if v.financial:  # for a bank, other income is mostly fees and trading: not a side line
        return "skip"
    a, b = v.at(oi, 0), v.at(pbt, 0)
    if a is None or b is None or b <= 0:
        return "skip"
    if a / b >= 0.25:
        return _flag("other_income", NOTE, "A large part of pre-tax profit is other income",
                     f"Other income is {_pct(a / b)} of profit before tax.",
                     v.evidence("Other income", oi, "money", 1))
    return None


def _tax_swing(v: _View):
    tax, pbt = v.line("tax"), v.line("pbt")
    t1, p1, t0, p0 = v.at(tax, 0), v.at(pbt, 0), v.at(tax, 1), v.at(pbt, 1)
    if None in (t1, p1, t0, p0) or p1 <= 0 or p0 <= 0:
        return "skip"
    now, before = t1 / p1, t0 / p0
    if abs(now - before) >= 0.08:
        return _flag("tax_swing", NOTE, "The share of profit paid as tax changed a lot",
                     f"Tax was {_pct(before)} of profit before tax, then {_pct(now)}.",
                     [{"label": "Tax rate", "period": v.periods[v.end - 1], "value": before,
                       "unit": "percent"},
                      {"label": "Tax rate", "period": v.periods[v.end], "value": now,
                       "unit": "percent"}])
    return None


def _revenue_fall(v: _View):
    rev = v.line("revenue")
    r0, r1 = v.at(rev, 1), v.at(rev, 0)
    if r0 is None or r1 is None or r0 <= 0:
        return "skip"
    if r1 / r0 - 1 <= -0.05:
        return _flag("revenue_fall", LOOK, "Revenue fell",
                     f"Revenue changed {r1 / r0 - 1:+.1%} in the latest period.",
                     v.evidence("Revenue", rev, "money", 2))
    return None


def _loss(v: _View):
    np_ = v.line("net_income")
    n0, n1 = v.at(np_, 1), v.at(np_, 0)
    if n1 is None:
        return "skip"
    if n1 < 0:
        return _flag("loss", LOOK, "Net loss in the latest period", "Net profit is below zero.",
                     v.evidence("Net profit", np_, "money", 2))
    if n0 and n0 > 0 and n1 / n0 - 1 <= -0.30:
        return _flag("profit_fall", LOOK, "Net profit fell by almost a third or more",
                     f"Net profit changed {n1 / n0 - 1:+.0%} in the latest period.",
                     v.evidence("Net profit", np_, "money", 2))
    return None


def _roe_slide(v: _View):
    s = v.ratio("roe")
    vals = [v.at(s, b) for b in (2, 1, 0)]
    if None in vals:
        return "skip"
    if vals[0] > vals[1] > vals[2] and vals[0] - vals[2] >= 0.02:
        return _flag("roe_slide", NOTE, "Return on equity fell two periods running",
                     f"It went from {_pct(vals[0])} to {_pct(vals[2])}.",
                     v.evidence("ROE", s, "percent", 2))
    return None


def _leverage_only(v: _View):
    d = v.dupont
    if not d:
        return "skip"
    if d["change"] > 0.005 and d["leverage"] > 0 and d["margin"] + d["turnover"] <= 0:
        return _flag("leverage_only", LOOK, "Return on equity rose only through leverage",
                     "In the three-way split, net margin and asset turnover together did not add "
                     f"to ROE ({(d['margin'] + d['turnover']) * 100:+.2f} pp); the equity "
                     f"multiplier added {d['leverage'] * 100:+.2f} pp.", [])
    return None


RULES: list[tuple[str, str, Callable]] = [
    ("revenue_fall", "Revenue fell", _revenue_fall),
    ("loss", "Net loss or a large fall in profit", _loss),
    ("margin_squeeze", "Revenue up, operating margin down", _margin_squeeze),
    ("profit_vs_sales", "Profit growing far faster than sales", _profit_vs_sales),
    ("receivable_days", "Receivable days rising", _receivable_days),
    ("inventory_days", "Inventory days rising", _inventory_days),
    ("interest_cover", "Interest coverage low or falling", _interest_cover),
    ("debt_trend", "Debt to equity rising", _debt_trend),
    ("other_income", "Other income a large share of profit", _other_income),
    ("tax_swing", "Tax share of profit swung", _tax_swing),
    ("roe_slide", "Return on equity falling", _roe_slide),
    ("leverage_only", "ROE helped by leverage alone", _leverage_only),
]


def watchlist(result: dict) -> dict:
    """Run every rule against a statements result. Returns the flags, the names of the rules
    that ran and found nothing, and the names of the rules skipped for want of a line."""
    v = _View(result)
    flags, clear, skipped = [], [], []
    for _rid, name, rule in RULES:
        out = rule(v)
        if out == "skip":
            skipped.append(name)
        elif out is None:
            clear.append(name)
        else:
            flags.append(out)
    flags.sort(key=lambda f: f["level"] != LOOK)
    return {"flags": flags, "clear": clear, "skipped": skipped,
            "checked": len(flags) + len(clear)}


def _change_text(label: str, old: float, new: float, unit: str) -> str:
    if unit == "percent":
        return f"{label} went from {_pct(old)} to {_pct(new)} ({(new - old) * 100:+.1f} pp)."
    if unit == "days":
        return f"{label} went from {old:.1f} to {new:.1f} days."
    return f"{label} went from {old:.2f}x to {new:.2f}x."


def takeaways(result: dict, limit: int = 4) -> list[str]:
    """Plain sentences for the top of the page: the biggest moves between the two periods."""
    v = _View(result)
    out: list[str] = []
    for key, label in (("revenue", "Revenue"), ("net_income", "Net profit")):
        s = v.line(key)
        old, new = v.at(s, v.end - v.start), v.at(s, 0)
        if old and new is not None and old > 0:
            out.append(f"{label} {new / old - 1:+.1%} between {v.periods[v.start]} and "
                       f"{v.periods[v.end]}.")
    if v.dupont:
        d = v.dupont
        out.append(_change_text("Return on equity", d["roe_a"], d["roe_b"], "percent"))
    moves = []
    for key, r in ([] if v.financial else v.ratios.items()):
        if key in ("roe", "equity_multiplier"):
            continue
        old, new = v.at(r["values"], v.end - v.start), v.at(r["values"], 0)
        if old is None or new is None or old == 0:
            continue
        moves.append((abs(new / old - 1), r, old, new))
    moves.sort(key=lambda m: -m[0])
    for _size, r, old, new in moves[:1]:
        out.append("Largest relative move among the ratios: "
                   + _change_text(r["label"], old, new, r["unit"]))
    return out[:limit]


def compare_statements(first: dict, second: dict, names: tuple[str, str]) -> dict:
    """Line two statements results up on the ratios both have. Pure arithmetic and wording; it
    says which figure is higher and never why."""
    sides = []
    for r, name in zip((first, second), names, strict=True):
        v = _View(r)
        sides.append({"name": name, "company": r.get("company"), "financial": v.financial,
                      "period_a": r["period_a"], "period_b": r["period_b"],
                      "currency": r.get("currency", ""), "view": v})
    rows, only_one = [], []
    keys = [k for k in sides[0]["view"].ratios if k in sides[1]["view"].ratios]
    for k in keys:
        ra, rb = sides[0]["view"].ratios[k], sides[1]["view"].ratios[k]
        row = {"key": k, "label": ra["label"], "unit": ra["unit"], "formula": ra["formula"],
               "values": []}
        for s, r in zip(sides, (ra, rb), strict=True):
            vw = s["view"]
            row["values"].append({"from": vw.at(r["values"], vw.end - vw.start),
                                  "to": vw.at(r["values"], 0)})
        rows.append(row)
    for k, r in sides[0]["view"].ratios.items():
        if k not in sides[1]["view"].ratios:
            only_one.append(f"{r['label']}: only {names[0]} has it")
    for k, r in sides[1]["view"].ratios.items():
        if k not in sides[0]["view"].ratios:
            only_one.append(f"{r['label']}: only {names[1]} has it")
    growth = []
    for key, label in (("revenue", "Revenue"), ("net_income", "Net profit")):
        vals = []
        for s in sides:
            vw = s["view"]
            ser = vw.line(key)
            old, new = vw.at(ser, vw.end - vw.start), vw.at(ser, 0)
            vals.append(new / old - 1 if old and new is not None and old > 0 else None)
        growth.append({"key": key, "label": label, "values": vals})
    notes = []
    if sides[0]["financial"] != sides[1]["financial"]:
        notes.append("One of these is a bank or finance company and the other is not. Margins, "
                     "leverage and turnover mean different things for the two, so only the "
                     "ratios both have are lined up and they should not be read as like for like.")
    if (sides[0]["period_a"], sides[0]["period_b"]) != (sides[1]["period_a"], sides[1]["period_b"]):
        notes.append("The two files cover different periods: "
                     f"{names[0]} {sides[0]['period_a']} to {sides[0]['period_b']}, "
                     f"{names[1]} {sides[1]['period_a']} to {sides[1]['period_b']}.")
    lines = []
    for row in rows:
        a, b = row["values"][0]["to"], row["values"][1]["to"]
        if a is None or b is None:
            continue
        hi = names[0] if a > b else names[1]
        if a == b:
            continue
        fmt = _fmt_value(row["unit"])
        lines.append(f"{row['label']}: {hi} is higher ({names[0]} {fmt(a)}, {names[1]} {fmt(b)}).")
    caveat = ("Companies differ in industry, size, accounting choices and year end. A higher or "
              "lower figure is not better or worse by itself, and this does not say why the "
              "figures differ.")
    return {"kind": "compare", "names": list(names),
            "companies": [{k: s[k] for k in ("name", "company", "financial", "period_a",
                                              "period_b")} for s in sides],
            "rows": rows, "growth": growth, "summary_lines": lines, "notes": notes + only_one,
            "caveat": caveat}


def _fmt_value(unit: str) -> Callable[[float], str]:
    if unit == "percent":
        return _pct
    if unit == "days":
        return lambda x: f"{x:.1f} days"
    return lambda x: f"{x:.2f}x"
