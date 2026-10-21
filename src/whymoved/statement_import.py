"""Read financial statements laid out the way annual reports and data sites lay them out.

The usual layout is the opposite of an analysis table: row labels run down the left ("Sales",
"Net profit", "Total assets") and one column per year or quarter runs across. This module reads
such a workbook (every tab), finds the period heading, suggests which row is which line item
("role"), lets the caller confirm or change that mapping, and then builds the statement lines and
the same ratios as `statements.compute_ratios`.

Nothing is guessed silently: a row is only suggested when its label matches a known pattern, a
ratio is only computed when all its inputs were mapped, and every ratio that could not be computed
is listed with what it needs.
"""

from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
import pandas as pd

from .statements import RATIO_INFO, _div

MAX_ROWS = 400          # rows read per sheet
MAX_COLS = 60           # columns read per sheet
MAX_SHEETS = 12         # sheets read per workbook
MIN_LINES = 3           # a statement block needs at least this many labelled rows
MIN_PERIODS = 2         # ... and at least this many period columns


class StatementImportError(ValueError):
    """The file cannot be read as financial statements; the message says why."""


# role -> (what it means, which statement it belongs to)
ROLE_INFO: dict[str, tuple[str, str]] = {
    "revenue": ("Revenue / sales", "Income statement"),
    "cogs": ("Cost of goods sold", "Income statement"),
    "total_expenses": ("Total expenses (before depreciation and interest)", "Income statement"),
    "ebitda": ("Operating profit / EBITDA", "Income statement"),
    "other_income": ("Other income", "Income statement"),
    "depreciation": ("Depreciation and amortisation", "Income statement"),
    "interest": ("Interest / finance cost", "Income statement"),
    "pbt": ("Profit before tax", "Income statement"),
    "tax": ("Tax", "Income statement"),
    "net_income": ("Net profit", "Income statement"),
    "cash": ("Cash and equivalents", "Balance sheet"),
    "receivables": ("Receivables / debtors", "Balance sheet"),
    "inventory": ("Inventory", "Balance sheet"),
    "fixed_assets": ("Fixed assets / net block", "Balance sheet"),
    "current_assets": ("Total current assets", "Balance sheet"),
    "total_assets": ("Total assets", "Balance sheet"),
    "payables": ("Payables / creditors", "Balance sheet"),
    "current_liabilities": ("Total current liabilities", "Balance sheet"),
    "short_term_debt": ("Short-term borrowings", "Balance sheet"),
    "long_term_debt": ("Long-term borrowings", "Balance sheet"),
    "borrowings": ("Total borrowings (short and long together)", "Balance sheet"),
    "equity": ("Total shareholders' equity / net worth", "Balance sheet"),
    "equity_capital": ("Equity share capital", "Balance sheet"),
    "reserves": ("Reserves and surplus", "Balance sheet"),
    "liabilities_and_equity": ("Total liabilities and equity (for the balance check)",
                               "Balance sheet"),
}
ROLES = list(ROLE_INFO)

# (role, score, pattern). Patterns match the whole normalised label (see _norm).
_PATTERNS: list[tuple[str, int, str]] = [
    ("revenue", 100, r"(net )?sales( turnover)?"),
    ("revenue", 100, r"revenue from operations( net)?"),
    ("revenue", 95, r"(total )?(net )?revenues?( from operations)?"),
    ("revenue", 95, r"net sales( and other operating income)?"),
    ("revenue", 90, r"(total )?operating (income|revenues?)"),
    ("revenue", 90, r"turnover|income from operations"),
    ("revenue", 60, r"total income"),
    ("cogs", 100, r"cost of (goods sold|sales|revenue)|cogs"),
    ("cogs", 70, r"cost of materials consumed"),
    ("cogs", 60, r"raw materials?( costs?)?"),
    ("total_expenses", 100, r"(total )?expenses|total expenditure"),
    ("total_expenses", 70, r"(total )?operating expenses"),
    ("ebitda", 100, r"operating profit|ebitda|ebidta|pbdit"),
    ("ebitda", 100, r"profit before depreciation interest and tax"),
    ("ebitda", 90, r"operating profit before .*"),
    ("other_income", 100, r"other income"),
    ("depreciation", 100, r"depreciation( and amorti[sz]ation( expenses?)?)?"),
    ("depreciation", 90, r"depreciation.*amorti[sz]ation.*"),
    ("depreciation", 50, r"amorti[sz]ation"),
    ("interest", 100, r"interest|finance costs?|interest expenses?"),
    ("interest", 90, r"interest and finance charges"),
    ("pbt", 100, r"profit before tax(ation)?|pbt|profit loss before tax"),
    ("pbt", 50, r"profit before tax and exceptional.*"),
    ("tax", 100, r"tax|tax expenses?|total tax expenses?|provision for taxation"),
    ("tax", 100, r"(provision for |income )?tax(es)?( expenses?)?"),
    ("net_income", 100, r"net profit|profit after tax(ation)?|pat|net income"),
    ("net_income", 100, r"profit (loss )?for the (year|period)"),
    ("net_income", 90, r"net profit loss.*"),
    ("cash", 100, r"cash and cash equivalents|cash and bank( balances?)?"),
    ("cash", 95, r"cash and cash equivalents and bank balances?"),
    ("cash", 80, r"cash"),
    ("receivables", 100, r"(trade )?receivables|sundry debtors|debtors|accounts receivable"),
    ("receivables", 90, r"trade receivables.*"),
    ("inventory", 100, r"inventor(y|ies)"),
    ("inventory", 90, r"stock in trade"),
    ("inventory", 80, r"stocks?"),
    ("fixed_assets", 100, r"(net )?fixed assets|net block"),
    ("fixed_assets", 95, r"property plant and equipment"),
    ("fixed_assets", 70, r"tangible assets"),
    ("current_assets", 100, r"(total )?current assets"),
    ("total_assets", 100, r"total assets"),
    ("payables", 100, r"(trade )?payables|sundry creditors|creditors|accounts payable"),
    ("payables", 90, r"trade payables.*"),
    ("current_liabilities", 100, r"(total )?current liabilities"),
    ("short_term_debt", 100, r"short term (borrowings|debt)"),
    ("short_term_debt", 90, r"current borrowings"),
    ("long_term_debt", 100, r"long term (borrowings|debt)|non current borrowings"),
    ("borrowings", 100, r"(total )?borrowings|total debt"),
    ("borrowings", 80, r"debt"),
    ("equity", 100, r"(total )?(shareholders|stockholders|owners) (funds|equity)"),
    ("equity", 100, r"net worth|total equity"),
    ("equity", 60, r"equity"),
    ("equity_capital", 100, r"equity (share )?capital|share capital"),
    ("reserves", 100, r"reserves( and surplus)?|other equity"),
    ("liabilities_and_equity", 100,
     r"total (equity and liabilities|liabilities and (equity|shareholders (funds|equity)))"),
    ("liabilities_and_equity", 60, r"total liabilities"),
]
_COMPILED = [(role, score, re.compile(rf"^(?:{pat})$")) for role, score, pat in _PATTERNS]

# Lines that are already ratios or per-share figures: never suggested for a role.
_DERIVED = re.compile(r"%|\bmargins?\b|\bgrowth\b|\bratios?\b|\bper share\b|\beps\b|\bdays\b|"
                      r"\byield\b|\bdividends?\b", re.IGNORECASE)


def _norm(label: str) -> str:
    s = str(label).lower().replace("&", " and ").replace("/", " ")
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[^a-z0-9 \-]", "", s).replace("-", " ")
    return re.sub(r"\s+", " ", s).strip()


def _clean_label(value) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text.rstrip("+:*").strip()


# --------------------------------------------------------------------------- cells


_NA = {"", "-", "--", "—", "–", "na", "n/a", "nan", "nil", "none", "null", "#n/a", "#div/0!"}
_SYMBOLS = re.compile(r"(?i)(₹|\$|€|£|rs\.?|inr|usd)")
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")


def _num(value) -> float | None:
    """A number from a cell, or None. Handles '1,234', '(500)', 'Rs. 12.5', '12%' (as 0.12)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return None
    if isinstance(value, (int, float, np.integer, np.floating)):
        out = float(value)
        return out if math.isfinite(out) else None
    text = str(value).strip()
    if text.lower() in _NA:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = _SYMBOLS.sub("", text.strip("()")).replace(",", "").replace(" ", "")
    percent = text.endswith("%")
    text = text.rstrip("%")
    if text.startswith("−"):  # unicode minus
        text = "-" + text[1:]
    if not _NUMBER.match(text):
        return None
    out = float(text)
    out = out / 100 if percent else out
    return -abs(out) if negative else out


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MON = "|".join(_MONTHS)


def _year(text: str) -> int:
    y = int(text)
    return y + 2000 if y < 100 else y


def parse_period(cell) -> tuple[tuple[int, int], str] | None:
    """((year, month or 0), display label) if the cell is a period heading, else None.

    Understands 2024, "FY24", "FY2024", "2023-24", "Mar 2024", "Mar-24", "March 2024",
    "31-03-2024", "2024-03-31" and real date cells. A fiscal year written 2023-24 means the year
    that ends in 2024. "TTM", "Notes" and anything else is not a period.
    """
    if cell is None or isinstance(cell, bool):
        return None
    if isinstance(cell, (datetime, date, pd.Timestamp)):
        if pd.isna(cell):
            return None
        stamp = pd.Timestamp(cell)
        return (stamp.year, stamp.month), f"{stamp.strftime('%b')} {stamp.year}"
    if isinstance(cell, (int, float, np.integer, np.floating)):
        if isinstance(cell, (float, np.floating)) and not float(cell).is_integer():
            return None
        y = int(cell)
        return ((y, 0), str(y)) if 1990 <= y <= 2100 else None
    text = re.sub(r"\s+", " ", str(cell)).strip()
    if not text:
        return None
    low = text.lower().replace("’", "'")
    m = re.fullmatch(r"(?:fy|f\.y\.?)\s*'?(\d{2}|\d{4})", low)
    if m:
        return (_year(m.group(1)), 0), text
    m = re.fullmatch(r"(\d{4})\s*[-/–]\s*(\d{2}|\d{4})", low)
    if m:
        end = _year(m.group(2)) if len(m.group(2)) == 4 else (int(m.group(1)) // 100) * 100 + int(
            m.group(2))
        return (end, 0), text
    m = re.fullmatch(rf"({_MON})[a-z]*[ \-'./]*(\d{{2}}|\d{{4}})", low)
    if m:
        return (_year(m.group(2)), _MONTHS[m.group(1)]), text
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", low)
    if m and 1 <= int(m.group(2)) <= 12 and 1 <= int(m.group(1)) <= 31:
        return (int(m.group(3)), int(m.group(2))), text
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?: 00:00:00)?", low)
    if m and 1 <= int(m.group(2)) <= 12:
        return (int(m.group(1)), int(m.group(2))), text
    m = re.fullmatch(r"(\d{4})", low)
    if m and 1990 <= int(m.group(1)) <= 2100:
        return (int(m.group(1)), 0), text
    return None


# --------------------------------------------------------------------------- reading


@dataclass
class RowInfo:
    key: str          # unique name used for mapping (the label, or "Sheet > label" on clashes)
    label: str        # the label as written (cleaned)
    sheet: str
    derived: bool     # already a ratio / per-share line: not offered for a role by default


@dataclass
class StatementTable:
    periods: list[str]              # display labels, oldest first
    frame: pd.DataFrame             # index = row key, columns = periods, floats (NaN = blank)
    rows: list[RowInfo]
    sheets: list[str]
    notes: list[str] = field(default_factory=list)
    company: str | None = None      # from a "COMPANY NAME" cell when the file has one


@dataclass
class _Sheet:
    name: str
    periods: list[tuple[tuple[int, int], str]]
    rows: list[tuple[str, list[float | None]]]
    notes: list[str]
    quarterly: bool = False


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _grids(data: bytes, filename: str) -> dict[str, pd.DataFrame]:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext in ("csv", "tsv", "txt"):
        text = _decode(data)
        try:
            delim = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
        except csv.Error:
            delim = "\t" if ext == "tsv" else ","
        records = []
        for i, rec in enumerate(csv.reader(io.StringIO(text), delimiter=delim)):
            if i >= MAX_ROWS:
                break
            records.append(rec[:MAX_COLS])
        width = max((len(r) for r in records), default=0)
        grid = pd.DataFrame([r + [None] * (width - len(r)) for r in records], dtype=object)
        return {"Sheet1": grid}
    if ext in ("xlsx", "xlsm"):
        sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, dtype=object,
                               nrows=MAX_ROWS)
        names = list(sheets)[:MAX_SHEETS]
        return {n: sheets[n].iloc[:, :MAX_COLS] for n in names}
    if ext == "xls":
        raise StatementImportError("old .xls files are not supported; save as .xlsx or .csv")
    raise StatementImportError(f"unsupported file type {ext!r}; use .csv or .xlsx")


def _header_candidates(grid: pd.DataFrame) -> list[tuple[int, list]]:
    """Rows that look like a period heading: at least two period cells and (almost) nothing
    else. A sheet can hold several statements stacked under each other, each with its own
    heading row (the usual layout of data-site exports), so every such row is returned."""
    found: list[tuple[int, list]] = []
    for r in range(grid.shape[0]):
        hits = []
        for c in range(grid.shape[1]):
            period = parse_period(grid.iat[r, c])
            if period is not None:
                hits.append((c, period))
        if len(hits) < MIN_PERIODS:
            continue
        if r >= 40 and all(re.fullmatch(r"\d{4}", p[1]) for _, p in hits):
            continue  # far down a sheet, a row of bare years is more likely data than a heading
        hit_cols = {h[0] for h in hits}
        others = sum(1 for c in range(grid.shape[1])
                     if pd.notna(grid.iat[r, c]) and str(grid.iat[r, c]).strip()
                     and c not in hit_cols)
        if others <= 4:
            found.append((r, hits))
    return found


def _looks_quarterly(hits: list) -> bool:
    """True if the heading columns are about three months apart (several months in a year)."""
    if any(p[0][1] == 0 for _, p in hits):
        return False  # "FY2024" style headings are annual
    stamps = sorted({p[0][0] * 12 + p[0][1] for _, p in hits})
    if len(stamps) < 3:
        return False
    gaps = sorted(b - a for a, b in zip(stamps, stamps[1:], strict=False))
    return gaps[len(gaps) // 2] <= 4


def _block(name: str, grid: pd.DataFrame, header_row: int, hits: list, stop: int
           ) -> _Sheet | None:
    first_col = hits[0][0]
    below = grid.iloc[header_row + 1:stop]
    label_col, best_text = None, 0
    for c in range(first_col):
        texts = sum(1 for v in below.iloc[:, c]
                    if isinstance(v, str) and v.strip() and _num(v) is None)
        if texts > best_text:
            label_col, best_text = c, texts
    if label_col is None or best_text < MIN_LINES:
        return None
    # one column per year: where quarters repeat a year, keep the most common month
    months = [p[1][0][1] for p in hits]
    notes: list[str] = []
    modal = max(set(months), key=months.count)
    seen: dict[int, int] = {}
    kept: list[tuple[int, tuple[tuple[int, int], str]]] = []
    dropped = 0
    for c, period in hits:
        year = period[0][0]
        clash = year in seen
        if clash and period[0][1] == modal and kept[seen[year]][1][0][1] != modal:
            kept[seen[year]] = (c, period)  # prefer the modal month
            continue
        if clash:
            dropped += 1
            continue
        seen[year] = len(kept)
        kept.append((c, period))
    if dropped:
        notes.append(f"Sheet {name!r}: {dropped} column(s) repeated a year (quarters or "
                     "restated columns) and were ignored; one column per year was kept.")
    rows: list[tuple[str, list[float | None]]] = []
    for r in range(header_row + 1, stop):
        raw = grid.iat[r, label_col]
        if not isinstance(raw, str) or not raw.strip():
            continue
        label = _clean_label(raw)
        values = [_num(grid.iat[r, c]) for c, _ in kept]
        if all(v is None for v in values):
            continue  # a section heading, blank line or note
        rows.append((label, values))
    if len(rows) < MIN_LINES:
        return None
    return _Sheet(name, [p for _, p in kept], rows, notes, _looks_quarterly(hits))


def _read_blocks(name: str, grid: pd.DataFrame) -> list[_Sheet]:
    """Every statement block on a sheet, each read against its OWN heading row, so a quarterly
    block can never be lined up under annual columns."""
    if grid.shape[0] < MIN_LINES + 1 or grid.shape[1] < 2:
        return []
    candidates = _header_candidates(grid)
    if not candidates:
        return []
    top = max(len(h) for _, h in candidates[:])
    # the first 40 rows decide what a heading looks like; weak look-alikes are not headings
    candidates = [(r, h) for r, h in candidates if len(h) >= max(MIN_PERIODS, top / 2)]
    # a single heading keeps the old rule: the row with the most period cells wins
    if len(candidates) > 1:
        rows_only = [r for r, _ in candidates]
        if rows_only[-1] - rows_only[0] < 3:  # adjacent look-alikes: keep the widest
            candidates = [max(candidates, key=lambda c: len(c[1]))]
    blocks = []
    for i, (row, hits) in enumerate(candidates):
        stop = candidates[i + 1][0] if i + 1 < len(candidates) else grid.shape[0]
        block = _block(name, grid, row, hits, stop)
        if block is not None:
            blocks.append(block)
    if any(not b.quarterly for b in blocks) and any(b.quarterly for b in blocks):
        left = [b for b in blocks if b.quarterly]
        blocks = [b for b in blocks if not b.quarterly]
        for b in left:
            blocks[0].notes.append(
                f"Sheet {name!r}: a block of quarterly columns ({b.periods[0][1]} to "
                f"{b.periods[-1][1]}) was left out; the figures use the annual blocks only.")
    return blocks


def _company(grids: dict[str, pd.DataFrame]) -> str | None:
    """The company name, from a 'COMPANY NAME' cell near the top of a sheet, if there is one."""
    for grid in grids.values():
        if grid.shape[1] < 2:
            continue
        for r in range(min(12, grid.shape[0])):
            label = grid.iat[r, 0]
            if isinstance(label, str) and _norm(label) in ("company name", "name of the company"):
                value = grid.iat[r, 1]
                if isinstance(value, str) and value.strip():
                    return _clean_label(value)[:80]
    return None


def _source_note(grids: dict[str, pd.DataFrame]) -> str | None:
    """A note about the unit when the file is a known data-site export."""
    for grid in grids.values():
        top = grid.iloc[:6, :8].to_numpy().ravel()
        if any(isinstance(v, str) and "screener.in" in v.lower() for v in top):
            return ("This looks like a Screener.in export. Those report money figures in rupees "
                    "crore; they are shown here exactly as given, in that unit.")
    return None


def _empty_sheets(grids: dict[str, pd.DataFrame], skipped: list[str]) -> list[str]:
    """Skipped sheets that hold labels but (almost) no numbers, e.g. formula cells that were never
    calculated, which read as empty. A stray zero or two is tolerated."""
    out = []
    for name in skipped:
        grid = grids[name]
        labels = sum(1 for v in grid.iloc[:, 0] if isinstance(v, str) and v.strip())
        numbers = sum(1 for v in grid.to_numpy().ravel() if _num(v) is not None)
        if labels >= MIN_LINES and numbers <= max(2, labels // 5):
            out.append(name)
    return out


def read_statements(data: bytes, filename: str) -> StatementTable:
    """Read every sheet that looks like a statement and combine them. Raises
    StatementImportError (safe to show) if none does."""
    try:
        grids = _grids(data, filename)
    except StatementImportError:
        raise
    except Exception as exc:
        raise StatementImportError("the file could not be read as a table") from exc
    sheets = [b for n, g in grids.items() for b in _read_blocks(n, g)]
    if not sheets:
        raise StatementImportError(
            "no financial-statement layout found: expected line items down the left and one "
            "column per year or period across, with at least 5 numeric lines and 2 periods")
    order: dict[int, tuple[tuple[int, int], str]] = {}
    for s in sheets:
        for key, label in s.periods:
            order.setdefault(key[0], (key, label))
    years = sorted(order)
    periods = [order[y][1] for y in years]
    notes = [n for s in sheets for n in s.notes]
    counts: dict[str, int] = {}
    for s in sheets:
        for label, _ in s.rows:
            counts[label] = counts.get(label, 0) + 1
    infos: list[RowInfo] = []
    data_rows: dict[str, list[float]] = {}
    used_by_sheet: dict[str, dict[str, int]] = {}
    for s in sheets:
        position = {key[0]: i for i, (key, _) in enumerate(s.periods)}
        used = used_by_sheet.setdefault(s.name, {})
        for label, values in s.rows:
            key = label if counts[label] == 1 else f"{s.name} > {label}"
            used[key] = used.get(key, 0) + 1
            if used[key] > 1:
                key = f"{key} ({used[key]})"
            aligned = [values[position[y]] if y in position else None for y in years]
            data_rows[key] = [np.nan if v is None else v for v in aligned]
            infos.append(RowInfo(key, label, s.name, bool(_DERIVED.search(label))))
    frame = pd.DataFrame.from_dict(data_rows, orient="index", columns=periods).astype(float)
    names = list(dict.fromkeys(s.name for s in sheets))
    if len(names) > 1:
        notes.append(f"Read {len(names)} sheets: {', '.join(names)}. "
                     "Lines that appear on more than one sheet are named with their sheet.")
    skipped = [n for n in grids if n not in set(names)]
    empty = _empty_sheets(grids, skipped)
    if empty:
        notes.append(f"Sheets with labels but no numbers were not used ({', '.join(empty)}); "
                     "for example, their cells may be formulas that were never calculated in "
                     "Excel. The figures come from the sheets that do hold numbers.")
    skipped = [n for n in skipped if n not in empty]
    if skipped:
        notes.append(f"Sheets not used (no statement layout found): {', '.join(skipped)}.")
    source = _source_note(grids)
    if source:
        notes.append(source)
    if len(periods) < 3:
        notes.append("Only two periods: return ratios (ROE and so on) need an opening balance, "
                     "so they are undefined for the first period.")
    return StatementTable(periods, frame, infos, names, notes, _company(grids))


# --------------------------------------------------------------------------- mapping


def suggest_roles(table: StatementTable) -> dict[str, str]:
    """role -> row key. Each row serves one role, each role takes its best-scoring row."""
    candidates: list[tuple[int, int, str, str]] = []
    for i, info in enumerate(table.rows):
        if info.derived:
            continue
        text = _norm(info.label)
        for role, score, pattern in _COMPILED:
            if pattern.match(text):
                candidates.append((-score, i, role, info.key))
    candidates.sort()
    out: dict[str, str] = {}
    used: set[str] = set()
    for _, _, role, key in candidates:
        if role in out or key in used:
            continue
        out[role] = key
        used.add(key)
    # Balance sheets laid out as two stacked halves end each half with a row called just
    # "Total": liabilities and equity first, then assets. (If the two totals are equal, which
    # is which does not matter; if they differ, the balance check says so.)
    totals = [i.key for i in table.rows if _norm(i.label) == "total" and i.key not in used]
    if len(totals) >= 2 and "total_assets" not in out and "liabilities_and_equity" not in out:
        out["liabilities_and_equity"], out["total_assets"] = totals[0], totals[1]
    return out


def validate_mapping(table: StatementTable, mapping: dict) -> dict[str, str]:
    """Check a person-supplied mapping {role: row key}; returns a cleaned copy."""
    if not isinstance(mapping, dict):
        raise StatementImportError("the mapping must be a list of role to line-item pairs")
    keys = set(table.frame.index)
    clean: dict[str, str] = {}
    for role, key in mapping.items():
        if role not in ROLE_INFO:
            raise StatementImportError(f"unknown role {role!r}")
        if key in (None, ""):
            continue
        if key not in keys:
            raise StatementImportError(f"line item {str(key)[:80]!r} is not in the file")
        clean[role] = key
    used = list(clean.values())
    if len(used) != len(set(used)):
        raise StatementImportError("one line item was given two different roles")
    return clean


# --------------------------------------------------------------------------- figures


def build_lines(table: StatementTable, mapping: dict[str, str]
                ) -> tuple[pd.DataFrame, list[str]]:
    """Period-by-figure table (index = periods, oldest first) and notes on how it was built."""
    series = {role: table.frame.loc[key] for role, key in mapping.items()}
    notes: list[str] = []
    lines = pd.DataFrame(index=table.frame.columns)

    def have(*roles: str) -> bool:
        return all(r in series for r in roles)

    for role in ("revenue", "cogs", "depreciation", "interest", "tax", "other_income", "cash",
                 "receivables", "inventory", "fixed_assets", "current_assets", "total_assets",
                 "payables", "current_liabilities"):
        if role in series:
            lines[role] = series[role]
    if have("revenue", "cogs"):
        lines["gross_profit"] = series["revenue"] - series["cogs"]
    if "ebitda" in series:
        lines["ebitda"] = series["ebitda"]
    elif have("revenue", "total_expenses"):
        lines["ebitda"] = series["revenue"] - series["total_expenses"]
        notes.append("EBITDA was taken as revenue minus total expenses (no operating-profit "
                     "line was chosen).")
    elif have("pbt", "depreciation", "interest"):
        other = series["other_income"] if "other_income" in series else 0.0
        lines["ebitda"] = series["pbt"] + series["depreciation"] + series["interest"] - other
        notes.append(
            "Operating profit (EBITDA) was worked back from profit before tax: profit before "
            "tax + depreciation + interest - other income. No operating-profit or total-"
            "expenses line exists for the yearly columns" + (
                "." if "other_income" in series else ", and no other-income line was chosen, "
                "so other income is inside it."))
    if "cogs" in series and re.search(r"raw material", series["cogs"].name or "", re.I):
        notes.append("Cost of goods sold is raw material cost only. It leaves out change in "
                     "inventory, power and fuel and other manufacturing costs, so gross margin "
                     "and the inventory and payable day counts are rough.")
    if "ebitda" in lines and "depreciation" in lines:
        lines["ebit"] = lines["ebitda"] - lines["depreciation"]
    if "pbt" in series:
        lines["pbt"] = series["pbt"]
    if "net_income" in series:
        lines["net_income"] = series["net_income"]
    elif have("pbt", "tax"):
        lines["net_income"] = series["pbt"] - series["tax"]
        notes.append("Net profit was taken as profit before tax minus tax (no net-profit line "
                     "was chosen).")
    if "borrowings" in series:
        lines["debt"] = series["borrowings"]
    elif "short_term_debt" in series or "long_term_debt" in series:
        parts = [series[r] for r in ("short_term_debt", "long_term_debt") if r in series]
        lines["debt"] = pd.concat(parts, axis=1).sum(axis=1, min_count=1)
        if len(parts) == 1:
            notes.append("Only one of short-term and long-term borrowings was chosen, so debt "
                         "counts that one alone.")
    if "equity" in series:
        lines["equity"] = series["equity"]
    elif have("equity_capital", "reserves"):
        lines["equity"] = series["equity_capital"] + series["reserves"]
        notes.append("Equity was taken as equity share capital plus reserves.")
    return lines, notes


# Each ratio: the figures it needs (after build_lines), and a plain reason if they are missing.
_NEEDS: dict[str, tuple[str, ...]] = {
    "gross_margin": ("revenue", "cogs"),
    "ebitda_margin": ("revenue", "ebitda"),
    "operating_margin": ("revenue", "ebit"),
    "net_margin": ("revenue", "net_income"),
    "current_ratio": ("current_assets", "current_liabilities"),
    "quick_ratio": ("cash", "receivables", "current_liabilities"),
    "cash_ratio": ("cash", "current_liabilities"),
    "debt_to_equity": ("debt", "equity"),
    "debt_to_assets": ("debt", "total_assets"),
    "interest_coverage": ("ebit", "interest"),
    "roe": ("net_income", "equity"),
    "roa": ("net_income", "total_assets"),
    "asset_turnover": ("revenue", "total_assets"),
    "equity_multiplier": ("total_assets", "equity"),
    "inventory_days": ("inventory", "cogs"),
    "receivable_days": ("receivables", "revenue"),
    "payable_days": ("payables", "cogs"),
    "cash_conversion_days": ("inventory", "cogs", "receivables", "revenue", "payables"),
}
_NEED_TEXT = {
    "revenue": "revenue", "cogs": "cost of goods sold", "ebitda": "operating profit (or total "
    "expenses)", "ebit": "operating profit and depreciation", "net_income": "net profit (or "
    "profit before tax and tax)", "current_assets": "total current assets",
    "current_liabilities": "total current liabilities", "cash": "cash", "receivables":
    "receivables", "debt": "borrowings", "equity": "equity (or share capital and reserves)",
    "total_assets": "total assets", "interest": "interest", "inventory": "inventory",
    "payables": "payables",
}


_FINANCIAL_NAME = re.compile(
    r"\b(banks?|finance|financial|financials|insurance|assurance|nbfc)\b", re.IGNORECASE)

# Ratios built for companies that make and sell goods. For a bank or lender, interest is the
# product's main cost, deposits are not ordinary borrowings, and there is no stock or gross
# margin, so these would show numbers that mean something else.
NOT_FOR_FINANCIALS = (
    "gross_margin", "ebitda_margin", "operating_margin", "current_ratio", "quick_ratio",
    "cash_ratio", "debt_to_equity", "debt_to_assets", "interest_coverage", "inventory_days",
    "receivable_days", "payable_days", "cash_conversion_days")


def looks_financial(company: str | None, lines: pd.DataFrame) -> str | None:
    """A short reason if the company looks like a bank or lender, else None. A guess: the
    person can override it."""
    if company and _FINANCIAL_NAME.search(company):
        return f"the company name ({company}) looks like a bank or finance company"
    if "interest" in lines and "revenue" in lines:
        both = lines[["revenue", "interest"]].dropna()
        if not both.empty:
            last = both.iloc[-1]
            if last["revenue"] > 0 and last["interest"] / last["revenue"] > 0.30:
                return ("interest is more than 30% of revenue, which is typical of a bank or "
                        "lender")
    return None


def ratios_from_lines(lines: pd.DataFrame, financial: bool = False
                      ) -> tuple[pd.DataFrame, dict[str, str]]:
    """(ratios, unavailable). Same formulas as statements.compute_ratios; a ratio appears only if
    all its inputs were supplied. `unavailable` maps each missing ratio to what it needs.
    With `financial=True` the ratios in NOT_FOR_FINANCIALS are left out and listed as such."""
    avg = lambda s: (s + s.shift(1)) / 2  # noqa: E731
    g = {c: lines[c] for c in lines.columns}
    out = pd.DataFrame(index=lines.index)
    unavailable: dict[str, str] = {}
    for name in RATIO_INFO:
        if financial and name in NOT_FOR_FINANCIALS:
            unavailable[name] = ("not shown for banks and finance companies: interest is their "
                                 "main cost of doing business and deposits are not ordinary "
                                 "borrowings")
            continue
        missing = [r for r in _NEEDS[name] if r not in g]
        if missing:
            unavailable[name] = "needs " + ", ".join(_NEED_TEXT[m] for m in missing)
            continue
        if name == "gross_margin":
            out[name] = _div(g["revenue"] - g["cogs"], g["revenue"])
        elif name == "ebitda_margin":
            out[name] = _div(g["ebitda"], g["revenue"])
        elif name == "operating_margin":
            out[name] = _div(g["ebit"], g["revenue"])
        elif name == "net_margin":
            out[name] = _div(g["net_income"], g["revenue"])
        elif name == "current_ratio":
            out[name] = _div(g["current_assets"], g["current_liabilities"])
        elif name == "quick_ratio":
            out[name] = _div(g["cash"] + g["receivables"], g["current_liabilities"])
        elif name == "cash_ratio":
            out[name] = _div(g["cash"], g["current_liabilities"])
        elif name == "debt_to_equity":
            out[name] = _div(g["debt"], g["equity"])
        elif name == "debt_to_assets":
            out[name] = _div(g["debt"], g["total_assets"])
        elif name == "interest_coverage":
            out[name] = _div(g["ebit"], g["interest"])
        elif name == "roe":
            out[name] = _div(g["net_income"], avg(g["equity"]))
        elif name == "roa":
            out[name] = _div(g["net_income"], avg(g["total_assets"]))
        elif name == "asset_turnover":
            out[name] = _div(g["revenue"], avg(g["total_assets"]))
        elif name == "equity_multiplier":
            out[name] = _div(avg(g["total_assets"]), avg(g["equity"]))
        elif name == "inventory_days":
            out[name] = _div(g["inventory"], g["cogs"]) * 365
        elif name == "receivable_days":
            out[name] = _div(g["receivables"], g["revenue"]) * 365
        elif name == "payable_days":
            out[name] = _div(g["payables"], g["cogs"]) * 365
        elif name == "cash_conversion_days":
            out[name] = (_div(g["inventory"], g["cogs"]) * 365
                         + _div(g["receivables"], g["revenue"]) * 365
                         - _div(g["payables"], g["cogs"]) * 365)
    return out, unavailable


GROWTH_LINES = ("revenue", "ebitda", "net_income", "total_assets", "equity")


def growth_from_lines(lines: pd.DataFrame) -> pd.DataFrame:
    """Period-on-period change as a fraction, for the main lines that were supplied. Undefined
    (NaN) when the earlier figure is zero or negative, where a percentage would mislead."""
    out = pd.DataFrame(index=lines.index)
    for col in GROWTH_LINES:
        if col in lines:
            prev = lines[col].shift(1)
            out[col] = (lines[col] / prev.where(prev > 0)) - 1
    return out


def check_lines(lines: pd.DataFrame, table: StatementTable, mapping: dict[str, str]
                ) -> list[str]:
    """Problems worth showing next to the result. Empty means none found."""
    issues: list[str] = []
    if "revenue" not in lines:
        issues.append("No revenue line was chosen, so margins and turnover cannot be worked out.")
    else:
        if (lines["revenue"] <= 0).any():
            issues.append("Revenue is zero or negative in some periods; margins there are "
                          "undefined.")
        if lines["revenue"].isna().any():
            when = ", ".join(map(str, lines.index[lines["revenue"].isna()]))
            issues.append(f"Revenue is blank in: {when}.")
    if "liabilities_and_equity" in mapping and "total_assets" in lines:
        total = table.frame.loc[mapping["liabilities_and_equity"]]
        gap = (lines["total_assets"] - total).abs()
        off = gap > 0.005 * lines["total_assets"].abs().clip(lower=1e-9)
        for period in lines.index[off.fillna(False)]:
            issues.append(f"{period}: total assets differ from total liabilities and equity by "
                          f"{gap[period]:,.2f}.")
    if "pbt" in lines and "tax" in lines and "net_income" in lines:
        diff = (lines["pbt"] - lines["tax"] - lines["net_income"]).abs()
        off = diff > 0.02 * lines["net_income"].abs().clip(lower=1e-9)
        if off.fillna(False).any():
            issues.append("Net profit differs from profit before tax minus tax by more than 2% "
                          "in some periods (items such as minority interest or exceptional "
                          "items are not in that subtraction).")
    if len(lines.columns) == 0:
        issues.append("No line items have been given a role yet.")
    return issues
