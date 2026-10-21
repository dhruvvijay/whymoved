"""Ingest: turn a messy CSV or Excel file into a clean table the engine can use.

Pipeline (`prepare`):
    read_table      CSV (any common delimiter, UTF-8 or Windows encodings) or .xlsx
                    (finds the header row below title lines; first non-empty sheet)
    guess_roles     date / metric / dimension / ignore for every column, with reasons
    clean           parse dates and numbers, drop "Total" rows, merge case/space
                    spelling variants, group rare categories into "Other"
    validate        every problem found goes into a ValidationReport; nothing is
                    changed silently

Honesty rules:
    * Fatal problems (no date column, no numbers, no categories) raise DataError.
    * Everything else is reported. Data is never dropped without an Issue saying so.
    * Exact duplicate rows are REPORTED, not removed, unless drop_duplicates=True:
      two identical orders can be genuine.
    * Role guesses are guesses. The caller should show them and let the user confirm.

Not handled: wide tables (months as columns), several tables on one sheet, merged
cells (use fill_down), headerless files, month-only labels such as "Jan-26", old .xls.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

TEXT_EXT = {".csv", ".tsv", ".txt"}
EXCEL_EXT = {".xlsx", ".xlsm"}
TOTAL_ROW = re.compile(r"^\s*(grand\s*total|sub\s*-?\s*total|total|overall|all)\s*:?\s*$", re.I)
ID_NAME = re.compile(
    r"(^|_|\s)(id|uuid|code|ref|reference|invoice|txn|sku|number|no)(_|\s|$)", re.I
)
DATE_NAME = re.compile(r"date|day|period|time|month|week", re.I)
RATE_NAME = re.compile(r"rate|ratio|pct|percent|avg|average|mean|aov|margin|per_", re.I)
CALENDAR_NAME = re.compile(r"^(year|month|week|quarter|qtr|fy|day)$", re.I)
CURRENCY = re.compile(r"rs\.?|inr|usd|eur|[₹$€£,\s]", re.I)
ISO_LIKE = re.compile(r"^\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2}")
DMY_LIKE = re.compile(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})")


class DataError(ValueError):
    """The file cannot be used at all (as opposed to having fixable problems)."""


# --------------------------------------------------------------------------- report


@dataclass
class Issue:
    severity: str  # "error" | "warning" | "info"
    code: str
    message: str
    column: str | None = None
    count: int | None = None
    examples: list[str] = field(default_factory=list)


@dataclass
class ValidationReport:
    issues: list[Issue] = field(default_factory=list)
    rows_read: int = 0
    rows_used: int = 0

    def add(self, severity, code, message, column=None, count=None, examples=None) -> None:
        self.issues.append(Issue(severity, code, message, column, count, list(examples or [])[:5]))

    def of(self, severity: str) -> list[Issue]:
        return [i for i in self.issues if i.severity == severity]

    @property
    def errors(self) -> list[Issue]:
        return self.of("error")

    @property
    def warnings(self) -> list[Issue]:
        return self.of("warning")

    @property
    def ok(self) -> bool:
        return not self.errors

    def has(self, code: str) -> bool:
        return any(i.code == code for i in self.issues)

    def get(self, code: str) -> list[Issue]:
        return [i for i in self.issues if i.code == code]

    def to_text(self) -> str:
        lines = [f"Rows read: {self.rows_read:,} | rows used: {self.rows_used:,}"]
        order = {"error": 0, "warning": 1, "info": 2}
        for i in sorted(self.issues, key=lambda x: order[x.severity]):
            ex = f" e.g. {', '.join(i.examples)}" if i.examples else ""
            lines.append(f"[{i.severity.upper():7s}] {i.code}: {i.message}{ex}")
        if self.ok:
            lines.append("Result: usable" + (" with warnings." if self.warnings else "."))
        else:
            lines.append("Result: has errors; fix them before trusting any analysis.")
        return "\n".join(lines)


# --------------------------------------------------------------------------- reading


def _blank_to_nan(df: pd.DataFrame) -> pd.DataFrame:
    return df.replace(r"^\s*$", np.nan, regex=True)


def _find_header_row(raw: pd.DataFrame, max_scan: int = 30) -> int:
    """First row that looks like column names: mostly filled, all text, mostly distinct."""
    ncols = int(raw.notna().any(axis=0).sum())
    if ncols == 0:
        return 0
    need = max(2, int(np.ceil(0.6 * ncols))) if ncols > 1 else 1
    for i in range(min(max_scan, len(raw))):
        vals = raw.iloc[i].dropna()
        if len(vals) < need or not all(isinstance(v, str) for v in vals):
            continue
        if vals.astype(str).str.strip().nunique() >= 0.9 * len(vals):
            return i
    return 0


def _unique_names(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for n in names:
        if n in seen:
            seen[n] += 1
            out.append(f"{n}_{seen[n]}")
        else:
            seen[n] = 1
            out.append(n)
    return out


def _decode(raw: bytes) -> tuple[str, str]:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1"), "latin-1"


def _read_text(raw: bytes) -> tuple[pd.DataFrame, dict]:
    text, enc = _decode(raw)
    sample = "\n".join(text.splitlines()[:20])
    try:
        delim = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        delim = ","
    try:
        df = pd.read_csv(io.StringIO(text), sep=delim, dtype=str, skipinitialspace=True)
    except pd.errors.ParserError as exc:
        raise DataError(f"the file has rows with a different number of columns: {exc}") from exc
    except pd.errors.EmptyDataError as exc:
        raise DataError("the file is empty") from exc
    return df, {"format": "csv", "encoding": enc, "delimiter": delim}


def _read_excel(source, sheet) -> tuple[pd.DataFrame, dict]:
    try:
        xls = pd.ExcelFile(source, engine="openpyxl")
    except ImportError as exc:
        raise DataError("reading .xlsx needs the openpyxl package (pip install openpyxl)") from exc
    except Exception as exc:  # corrupt file, password-protected, wrong format
        raise DataError(f"could not open the Excel file: {exc}") from exc
    names = list(xls.sheet_names)
    if sheet is not None:
        if isinstance(sheet, int):
            if not 0 <= sheet < len(names):
                raise DataError(f"sheet index {sheet} out of range; sheets: {names}")
            chosen = names[sheet]
        elif sheet in names:
            chosen = sheet
        else:
            raise DataError(f"sheet {sheet!r} not found; sheets: {names}")
        candidates = [chosen]
    else:
        candidates = names
    for name in candidates:
        raw = _blank_to_nan(xls.parse(name, header=None, dtype=object)).dropna(how="all")
        if len(raw) < 2:
            continue
        raw = raw.dropna(axis=1, how="all").reset_index(drop=True)
        h = _find_header_row(raw)
        header = raw.iloc[h].astype("string").str.strip().tolist()
        header = [
            h_ if isinstance(h_, str) and h_ else f"unnamed_{j}" for j, h_ in enumerate(header)
        ]
        df = raw.iloc[h + 1 :].copy()
        df.columns = _unique_names(header)
        df = df.reset_index(drop=True).infer_objects()
        return df, {"format": "xlsx", "sheet": name, "sheets": names, "header_row": h + 1,
                    "rows_above_header": h}
    raise DataError("no sheet with data found" if sheet is None else f"sheet {sheet!r} is empty")


def read_table(source, sheet=None, filename: str | None = None) -> tuple[pd.DataFrame, dict]:
    """Read a CSV/TSV/xlsx path, an uploaded file-like object (give `filename`), or a DataFrame."""
    if isinstance(source, pd.DataFrame):
        return source.copy(), {"format": "dataframe"}
    if hasattr(source, "read"):
        if not filename:
            raise DataError("give the original filename so the file type can be recognised")
        ext, payload = Path(filename).suffix.lower(), source.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        buffer = io.BytesIO(payload)
    else:
        path = Path(source)
        if not path.exists():
            raise DataError(f"file not found: {path}")
        ext = path.suffix.lower()
        buffer = path
    if ext in TEXT_EXT:
        raw = buffer.getvalue() if isinstance(buffer, io.BytesIO) else buffer.read_bytes()
        df, info = _read_text(raw)
    elif ext in EXCEL_EXT:
        df, info = _read_excel(buffer, sheet)
    elif ext == ".xls":
        raise DataError("old .xls files are not supported; save the file as .xlsx or .csv")
    else:
        raise DataError(f"unsupported file type {ext!r}; use .csv or .xlsx")
    df.columns = _unique_names([str(c).strip() for c in df.columns])
    df = _blank_to_nan(df)
    if df.dropna(how="all").empty:
        raise DataError("the file has no data rows")
    return df, info


# --------------------------------------------------------------------------- coercion


def _to_numeric(s: pd.Series) -> tuple[pd.Series, float]:
    """Numbers from numbers or text like '₹1,23,456', '(500)', '12%'.

    Returns (values, share of non-blank cells that parsed).
    """
    if pd.api.types.is_bool_dtype(s):
        return pd.Series(np.nan, index=s.index, dtype=float), 0.0
    if pd.api.types.is_numeric_dtype(s):
        out = s.astype(float).replace([np.inf, -np.inf], np.nan)
        return out, 1.0
    nn = s.dropna()
    out = pd.Series(np.nan, index=s.index, dtype=float)
    if nn.empty:
        return out, 0.0
    text = nn.astype("string").str.strip()
    neg = text.str.match(r"^\(.*\)$").fillna(False).astype(bool)
    t = text.str.replace(r"^\((.*)\)$", r"\1", regex=True)
    pct = t.str.endswith("%").fillna(False).astype(bool)
    t = t.str.replace("%", "", regex=False).str.replace(CURRENCY, "", regex=True)
    vals = pd.to_numeric(t, errors="coerce").astype(float)
    vals[neg] = -vals[neg]
    vals[pct] = vals[pct] / 100.0
    vals = vals.replace([np.inf, -np.inf], np.nan)
    out.loc[vals.index] = vals
    return out, float(vals.notna().mean())


def _parse_dates(s: pd.Series) -> tuple[pd.Series, dict]:
    """Parse a date column. Returns (timestamps, info) with info['ambiguous'] / ['dayfirst']."""
    info = {"ambiguous": False, "dayfirst": None, "conflict": False}
    empty = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    nn = s.dropna()
    if nn.empty:
        return empty, info
    kind = pd.api.types.infer_dtype(nn, skipna=True)
    if kind in ("datetime", "datetime64", "date"):
        return pd.to_datetime(s, errors="coerce"), info
    if kind not in ("string", "mixed"):
        return empty, info
    text = s.astype("string").str.strip()
    # Plain decimal numbers such as '1023.45' or '1,234.50' are amounts, never dates (the date
    # parser would otherwise read '1023.45' as a year and a month).
    if text.dropna().str.match(r"^[+-]?\d[\d,]*(\.\d+)?$").mean() >= 0.9:
        return empty, info
    has_marker = text.dropna().str.contains(r"[/\-. ]|[A-Za-z]", regex=True)
    if has_marker.mean() < 0.9:  # '20260105' or '5000': refuse to guess
        return empty, info
    iso = text.dropna().str.match(ISO_LIKE).mean()
    dayfirst = False
    if iso < 0.5:
        parts = text.str.extract(DMY_LIKE)
        matched = parts[0].notna().mean() if len(parts) else 0
        if matched >= 0.5:
            first = pd.to_numeric(parts[0], errors="coerce")
            second = pd.to_numeric(parts[1], errors="coerce")
            first_big, second_big = bool((first > 12).any()), bool((second > 12).any())
            if first_big and not second_big:
                dayfirst = True
            elif second_big and not first_big:
                dayfirst = False
            elif first_big and second_big:
                info["conflict"], dayfirst = True, True
            else:
                info["ambiguous"], dayfirst = True, True  # India: assume day first
            info["dayfirst"] = dayfirst
    parsed = pd.to_datetime(text, errors="coerce", dayfirst=dayfirst, format="mixed")
    if getattr(parsed.dt, "tz", None) is not None:
        parsed = parsed.dt.tz_localize(None)
    return parsed, info


# --------------------------------------------------------------------------- roles


@dataclass
class ColumnGuess:
    name: str
    role: str  # "date" | "metric" | "dimension" | "ignore"
    reason: str
    confidence: str = "high"  # "high" | "medium" | "low"


@dataclass
class Roles:
    date: str | None
    metrics: list[str]
    dimensions: list[str]
    guesses: list[ColumnGuess]

    def explain(self) -> str:
        return "\n".join(f"{g.name}: {g.role} ({g.confidence}) - {g.reason}" for g in self.guesses)


def guess_roles(
    df: pd.DataFrame, max_distinct: int = 200, id_unique_share: float = 0.9
) -> Roles:
    """Guess which column is the date, which are metrics, which are categories."""
    n = len(df)
    date_cands: list[tuple[float, str, float, dict]] = []
    info_by_col: dict[str, dict] = {}
    date_frac: dict[str, float] = {}
    kinds: dict[str, str] = {}

    for col in df.columns:
        s = df[col]
        nn = s.dropna()
        if nn.empty:
            kinds[col] = "empty"
            continue
        if nn.nunique() == 1 and n > 1:
            kinds[col] = "constant"
            continue
        parsed, dinfo = _parse_dates(s)
        frac = float(parsed.notna().sum() / len(nn))
        named_like_date = bool(DATE_NAME.search(col))
        # A column NAMED like a date with many bad values is still the date column: say so
        # (and let the validation report explain the bad values) instead of "no date found".
        if frac >= 0.9 or (frac >= 0.5 and named_like_date):
            score = frac + (0.5 if named_like_date else 0.0)
            date_cands.append((score, col, frac, dinfo))
            info_by_col[col] = dinfo
            date_frac[col] = frac
            kinds[col] = "date"
            continue
        _, nfrac = _to_numeric(s)
        kinds[col] = "numeric" if nfrac >= 0.9 else "text"

    date_cands.sort(key=lambda c: (-c[0], list(df.columns).index(c[1])))
    chosen_date = date_cands[0][1] if date_cands else None

    guesses: list[ColumnGuess] = []
    for col in df.columns:
        kind, s = kinds[col], df[col]
        nn = s.dropna()
        if kind == "empty":
            guesses.append(ColumnGuess(col, "ignore", "column is empty"))
        elif kind == "constant":
            guesses.append(ColumnGuess(col, "ignore", "single value in every row"))
        elif kind == "date":
            if col == chosen_date:
                d = info_by_col[col]
                weak = d["ambiguous"] or d["conflict"]
                why = "values parse as dates" + (
                    "; day/month order is ambiguous, assuming day first" if weak else ""
                )
                conf = "medium" if weak else "high"
                if date_frac[col] < 0.9:
                    why = f"only {date_frac[col]:.0%} of values parse as dates"
                    conf = "low"
                guesses.append(ColumnGuess(col, "date", why, conf))
            else:
                guesses.append(
                    ColumnGuess(col, "ignore", f"extra date column (using {chosen_date})")
                )
        elif kind == "numeric":
            vals, frac = _to_numeric(s)
            clean = vals.dropna()
            unique_share = clean.nunique() / max(len(clean), 1)
            integer_like = bool(len(clean)) and bool((clean == clean.round()).all())
            sequence = integer_like and len(clean) > 2 and bool(clean.is_monotonic_increasing) \
                and bool(clean.diff().dropna().eq(1).all())
            if CALENDAR_NAME.match(col) and clean.nunique() <= 60:
                guesses.append(ColumnGuess(col, "dimension", "calendar part; used as a category"))
            elif sequence:
                guesses.append(ColumnGuess(col, "ignore", "running row number"))
            elif ID_NAME.search(col) and unique_share >= id_unique_share and integer_like:
                guesses.append(ColumnGuess(col, "ignore", "looks like an identifier"))
            else:
                conf = "high" if frac >= 0.98 else "medium"
                guesses.append(ColumnGuess(col, "metric", "numeric values", conf))
        else:
            k = s.dropna().astype("string").str.strip().nunique()
            if k > max_distinct or (n >= 100 and k / n > 0.5):
                guesses.append(ColumnGuess(
                    col, "ignore", f"{k} distinct values; looks like an ID or free text"))
            else:
                guesses.append(ColumnGuess(col, "dimension", f"text with {k} distinct values"))

    return Roles(
        date=chosen_date,
        metrics=[g.name for g in guesses if g.role == "metric"],
        dimensions=[g.name for g in guesses if g.role == "dimension"],
        guesses=guesses,
    )


# --------------------------------------------------------------------------- prepare


@dataclass
class Prepared:
    data: pd.DataFrame
    date_col: str
    metric_cols: list[str]
    dimension_cols: list[str]
    roles: Roles
    report: ValidationReport
    info: dict


def _examples(values, limit: int = 5) -> list[str]:
    return [str(v) for v in list(values)[:limit]]


def _freq_label(days: np.ndarray) -> str:
    if len(days) < 2:
        return "single"
    med = float(np.median(days))
    if med == 1:
        return "daily"
    if med == 7:
        return "weekly"
    if 28 <= med <= 31:
        return "monthly"
    return "irregular"


def prepare(
    source,
    *,
    sheet=None,
    filename: str | None = None,
    date_col: str | None = None,
    metric_cols: list[str] | None = None,
    dimension_cols: list[str] | None = None,
    fill_down: tuple[str, ...] | list[str] = (),
    drop_duplicates: bool = False,
    max_categories: int = 30,
    min_category_share: float = 0.005,
    max_bad_date_share: float = 0.05,
    period_a=None,
    period_b=None,
    require_dimensions: bool = True,
) -> Prepared:
    """Read, check and clean a file. See the module docstring for the guarantees."""
    df, info = read_table(source, sheet=sheet, filename=filename)
    report = ValidationReport(rows_read=len(df))

    df = df.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
    if info.get("rows_above_header"):
        report.add("info", "header_found",
                   f"Skipped {info['rows_above_header']} line(s) above the header row "
                   f"(header is row {info['header_row']}).")
    if info.get("format") == "xlsx" and len(info.get("sheets", [])) > 1:
        report.add("info", "sheet_used",
                   f"Used sheet {info['sheet']!r}; other sheets were ignored: "
                   f"{[s for s in info['sheets'] if s != info['sheet']]}.")

    for col in fill_down:
        if col not in df.columns:
            raise DataError(f"fill_down column {col!r} not found")
        df[col] = df[col].ffill()

    # ---- roles -----------------------------------------------------------------
    guessed = guess_roles(df)
    chosen_date = date_col or guessed.date
    metrics = list(metric_cols) if metric_cols is not None else list(guessed.metrics)
    dims = list(dimension_cols) if dimension_cols is not None else list(guessed.dimensions)
    for name in [chosen_date, *metrics, *dims]:
        if name is not None and name not in df.columns:
            raise DataError(f"column {name!r} not found; columns are {list(df.columns)}")
    if chosen_date is None:
        raise DataError("no date column found. Columns: " + "; ".join(
            f"{g.name} ({g.reason})" for g in guessed.guesses))
    metrics = [m for m in metrics if m != chosen_date]
    dims = [d for d in dims if d != chosen_date and d not in metrics]
    if not metrics:
        raise DataError("no numeric metric column found to analyse")
    if not dims and require_dimensions:
        raise DataError("no category column found to split the metric by")
    roles = Roles(chosen_date, metrics, dims, guessed.guesses)
    unused = [c for c in df.columns if c not in {chosen_date, *metrics, *dims}]
    if unused:
        report.add("info", "columns_ignored", f"Ignored columns: {unused}.", count=len(unused))

    # ---- total / subtotal rows -------------------------------------------------
    text_cols = [c for c in df.columns if c not in metrics]
    is_total = pd.Series(False, index=df.index)
    for c in text_cols:
        hit = df[c].dropna().astype("string").str.match(TOTAL_ROW)
        is_total |= hit.reindex(df.index).fillna(False).astype(bool)
    if is_total.any():
        label = df.loc[is_total, text_cols[0]].astype("string")
        report.add("warning", "total_rows_removed",
                   f"Removed {int(is_total.sum())} row(s) labelled Total/Subtotal; keeping them "
                   "would double count.", count=int(is_total.sum()),
                   examples=_examples(label.dropna()))
        df = df.loc[~is_total].reset_index(drop=True)

    # ---- dates ------------------------------------------------------------------
    parsed, dinfo = _parse_dates(df[chosen_date])
    missing = int(df[chosen_date].isna().sum())
    unparsed_mask = parsed.isna() & df[chosen_date].notna()
    if dinfo["ambiguous"]:
        report.add("warning", "ambiguous_date_format",
                   "Every date has day and month both 12 or lower, so day/month order cannot be "
                   "told apart. Assumed DAY first (dd/mm/yyyy). Check the first few dates.",
                   column=chosen_date)
    if dinfo["conflict"]:
        report.add("warning", "mixed_date_formats",
                   "The date column mixes day-first and month-first values; "
                   "some dates will be wrong.",
                   column=chosen_date)
    if unparsed_mask.any():
        report.add("warning", "unparsable_dates",
                   f"{int(unparsed_mask.sum())} value(s) in {chosen_date!r} are not dates; "
                   "those rows were dropped.", column=chosen_date, count=int(unparsed_mask.sum()),
                   examples=_examples(df.loc[unparsed_mask, chosen_date].unique()))
    if missing:
        report.add("warning", "missing_dates", f"{missing} row(s) have no date; dropped.",
                   column=chosen_date, count=missing)
    bad = parsed.isna()
    if bad.any():
        if bad.mean() > max_bad_date_share:
            report.add("error", "too_many_bad_dates",
                       f"{bad.mean():.0%} of rows have a missing or invalid date "
                       f"(limit {max_bad_date_share:.0%}). The date column may be wrong.",
                       column=chosen_date)
        df, parsed = df.loc[~bad].reset_index(drop=True), parsed.loc[~bad].reset_index(drop=True)
    if len(df) == 0:
        raise DataError(
            f"no usable rows: none of the values in {chosen_date!r} could be read as dates"
        )
    if (parsed != parsed.dt.normalize()).any():
        report.add("info", "times_dropped",
                   "Dates included a time of day; times were dropped so each day counts as one.",
                   column=chosen_date)
    df[chosen_date] = parsed.dt.normalize()

    # ---- metrics -----------------------------------------------------------------
    for m in metrics:
        before = df[m]
        vals, _ = _to_numeric(before)
        bad_text = vals.isna() & before.notna()
        if bad_text.any():
            report.add("warning", "non_numeric_values",
                       f"{int(bad_text.sum())} value(s) in {m!r} are not numbers; treated as 0.",
                       column=m, count=int(bad_text.sum()),
                       examples=_examples(before[bad_text].unique()))
        blanks = int(before.isna().sum())
        if blanks:
            report.add("warning", "missing_values",
                       f"{blanks} blank value(s) in {m!r}; treated as 0. If a blank means "
                       "'unknown' rather than zero, rates built from this column will be off.",
                       column=m, count=blanks)
        df[m] = vals.fillna(0.0)
        if RATE_NAME.search(m) and df[m].min() >= 0 and df[m].max() <= 100:
            report.add("warning", "looks_like_rate",
                       f"{m!r} looks like a ready-made rate or average. Adding it up across rows "
                       "is meaningless; use the two columns it is made from (for example orders "
                       "and visits) with a formula such as 'orders / visits'.", column=m)
        neg = int((df[m] < 0).sum())
        if neg:
            report.add("warning", "negative_values",
                       f"{neg} negative value(s) in {m!r}. Kept: they may be refunds or credits.",
                       column=m, count=neg)

    # ---- dimensions ------------------------------------------------------------
    for d in dims:
        s = df[d].astype("string").str.strip().str.replace(r"\s+", " ", regex=True)
        blank = s.isna()
        if blank.any():
            share = blank.mean()
            hint = (f" If this is a merged-cell layout (a label shown once above its rows), "
                    f"pass fill_down=[{d!r}] to repeat each label down."
                    if share >= 0.2 and not blank.iloc[0] else "")
            report.add("warning", "blank_categories",
                       f"{int(blank.sum())} blank value(s) in {d!r}; labelled 'Unknown'.{hint}",
                       column=d, count=int(blank.sum()))
            s = s.fillna("Unknown")
        counts = s.value_counts()
        groups: dict[str, list[str]] = {}
        for value in counts.index:
            groups.setdefault(value.casefold(), []).append(value)
        mapping = {}
        merged_examples = []
        for variants in groups.values():
            if len(variants) > 1:
                winner = sorted(variants, key=lambda v: (-counts[v], v))[0]
                merged_examples.append("/".join(variants) + f" -> {winner}")
                mapping.update({v: winner for v in variants})
        if mapping:
            s = s.map(lambda v, m=mapping: m.get(v, v))
            report.add("info", "category_variants",
                       f"{d!r}: merged {len(mapping)} spelling variants that differ only by case "
                       "or spacing. Other differences (abbreviations, typos) cannot be detected.",
                       column=d, count=len(mapping), examples=merged_examples)
        df[d] = s.astype(str)

    # ---- duplicates --------------------------------------------------------------
    keep = [chosen_date, *dims, *metrics]
    dup = df.duplicated(subset=keep, keep="first")
    if dup.any():
        if drop_duplicates:
            report.add("warning", "duplicates_removed",
                       f"Removed {int(dup.sum())} exact duplicate row(s) as requested.",
                       count=int(dup.sum()))
            df = df.loc[~dup]
        else:
            report.add("warning", "duplicate_rows",
                       f"{int(dup.sum())} row(s) exactly repeat an earlier row. Kept, because "
                       "identical orders can be genuine; set drop_duplicates=True if they are "
                       "export errors.", count=int(dup.sum()))
    df = df[keep].reset_index(drop=True)

    # ---- categories: rare and too many ---------------------------------------------
    for d in dims:
        share = df[d].value_counts(normalize=True)
        rare = share[share < min_category_share].index.tolist()
        if len(rare) >= 2:
            df[d] = df[d].where(~df[d].isin(rare), "Other")
            report.add("info", "rare_categories_grouped",
                       f"{d!r}: grouped {len(rare)} categories under {min_category_share:.1%} of "
                       "rows each into 'Other'.", column=d, count=len(rare), examples=rare)
        counts = df[d].value_counts()
        if len(counts) > max_categories:
            top = counts.drop(index="Other", errors="ignore").index[: max_categories - 1]
            lumped = len(counts) - len(top) - (1 if "Other" in counts.index else 0)
            df[d] = df[d].where(df[d].isin(top), "Other")
            report.add("info", "categories_capped",
                       f"{d!r} had {len(counts)} categories; kept the {len(top)} most common and "
                       f"grouped {lumped} into 'Other'.", column=d, count=lumped)

    # ---- date coverage -------------------------------------------------------------
    days = np.sort(df[chosen_date].unique())
    first, last = pd.Timestamp(days[0]), pd.Timestamp(days[-1])
    gaps = np.diff(days).astype("timedelta64[D]").astype(int) if len(days) > 1 else np.array([])
    freq = _freq_label(gaps)
    info.update({"date_min": first, "date_max": last, "n_dates": len(days), "frequency": freq})
    report.add("info", "date_range",
               f"{len(days)} distinct dates from {first.date()} to {last.date()} ({freq}).")
    if freq == "daily":
        full = pd.date_range(first, last, freq="D")
        absent = full.difference(pd.DatetimeIndex(days))
        if len(absent):
            report.add("warning", "date_gaps",
                       f"{len(absent)} day(s) between the first and last date have no rows. "
                       "Period totals will be understated for those days.",
                       count=len(absent), examples=[str(x.date()) for x in absent[:5]])
    if len(days) < 14:
        report.add("warning", "few_dates",
                   f"Only {len(days)} distinct dates; too few to compare two periods reliably.")

    # ---- periods -------------------------------------------------------------------
    if period_a is not None and period_b is not None:
        _check_periods(df, chosen_date, dims, period_a, period_b, first, last, freq, report)

    report.rows_used = len(df)
    return Prepared(df, chosen_date, metrics, dims, roles, report, info)


def _check_periods(df, date_col, dims, period_a, period_b, first, last, freq, report) -> None:
    a0, a1 = (pd.Timestamp(x).normalize() for x in period_a)
    b0, b1 = (pd.Timestamp(x).normalize() for x in period_b)
    if a0 > a1 or b0 > b1:
        report.add("error", "period_reversed", "A period starts after it ends.")
        return
    if max(a0, b0) <= min(a1, b1):
        report.add("error", "periods_overlap", "Period A and period B overlap.")
        return
    in_a = (df[date_col] >= a0) & (df[date_col] <= a1)
    in_b = (df[date_col] >= b0) & (df[date_col] <= b1)
    for label, mask in (("A", in_a), ("B", in_b)):
        if not mask.any():
            report.add("error", "empty_period", f"Period {label} contains no rows.")
    if not (in_a.any() and in_b.any()):
        return
    for label, lo, hi in (("A", a0, a1), ("B", b0, b1)):
        if lo < first or hi > last:
            report.add("warning", "partial_period",
                       f"Period {label} extends beyond the data ({first.date()} to {last.date()}); "
                       "it is only partly covered, so totals are not like for like.")
    len_a, len_b = (a1 - a0).days + 1, (b1 - b0).days + 1
    if len_a != len_b:
        report.add("warning", "unequal_period_lengths",
                   f"Period A is {len_a} days and B is {len_b} days; additive metrics such as "
                   "revenue will differ simply because of length. Compare equal lengths.")
    elif freq == "daily" and len_a % 7:
        report.add("warning", "not_whole_weeks",
                   f"Periods are {len_a} days, not whole weeks, so weekday mix differs between "
                   "them and weekly seasonality can look like a change.")
    for label, mask in (("A", in_a), ("B", in_b)):
        if int(mask.sum()) < 30:
            report.add("warning", "few_rows_in_period",
                       f"Only {int(mask.sum())} row(s) in period {label}.")
    for d in dims:
        va, vb = set(df.loc[in_a, d]), set(df.loc[in_b, d])
        new, gone = sorted(vb - va), sorted(va - vb)
        if new:
            report.add("info", "new_categories",
                       f"{d!r}: {len(new)} value(s) appear only in period B (new, or renamed?).",
                       column=d, count=len(new), examples=new)
        if gone:
            report.add("info", "vanished_categories",
                       f"{d!r}: {len(gone)} value(s) appear only in period A "
                       "(dropped, or renamed?).",
                       column=d, count=len(gone), examples=gone)


def default_periods(prepared: Prepared):
    """Suggest (period_a, period_b): the last equal-length block versus the block before it."""
    first, last = prepared.info["date_min"], prepared.info["date_max"]
    span = (last - first).days + 1
    if prepared.info.get("frequency") != "daily":
        return None
    for length in (28, 14, 7):
        if span >= 2 * length:
            b1 = last
            b0 = b1 - pd.Timedelta(days=length - 1)
            a1 = b0 - pd.Timedelta(days=1)
            a0 = a1 - pd.Timedelta(days=length - 1)
            return (a0, a1), (b0, b1)
    return None
