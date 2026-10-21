"""Tests for reading statement workbooks: layouts, role suggestions, figures and ratios."""

import io

import numpy as np
import pandas as pd

from whymoved.statement_import import (
    StatementImportError,
    build_lines,
    check_lines,
    growth_from_lines,
    parse_period,
    ratios_from_lines,
    read_statements,
    suggest_roles,
    validate_mapping,
)
from whymoved.statements import compute_ratios, make_statements


def _xlsx(sheets: dict[str, list[list]]) -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _fails(fn, *a, **k) -> str | None:
    try:
        fn(*a, **k)
    except StatementImportError as exc:
        return str(exc)
    return None


YEARS = ["Mar 2021", "Mar 2022", "Mar 2023", "Mar 2024"]
PL = [
    ("Sales", [1000, 1100, 1250, 1400]),
    ("Expenses", [800, 880, 990, 1100]),
    ("Operating Profit", [200, 220, 260, 300]),
    ("OPM %", [20, 20, 21, 21]),
    ("Other Income", [10, 12, 14, 15]),
    ("Depreciation", [40, 44, 50, 56]),
    ("Interest", [30, 28, 25, 22]),
    ("Profit before tax", [140, 160, 199, 237]),
    ("Tax", [35, 40, 49, 59]),
    ("Net Profit", [105, 120, 150, 178]),
]
BS = [
    ("Equity Capital", [100, 100, 100, 100]),
    ("Reserves", [400, 500, 620, 760]),
    ("Borrowings", [300, 280, 250, 220]),
    ("Other Liabilities", [200, 220, 230, 250]),
    ("Total", [1000, 1100, 1200, 1330]),
    ("Net Block", [500, 520, 560, 600]),
    ("Investments", [100, 100, 100, 100]),
    ("Other Assets", [400, 480, 540, 630]),
    ("Total", [1000, 1100, 1200, 1330]),
]


def _two_sheet(pl=PL, bs=BS, years=YEARS) -> bytes:
    return _xlsx({
        "Profit & Loss": [["Particulars", *years], *[[k, *v] for k, v in pl]],
        "Balance Sheet": [["Particulars", *years], *[[k, *v] for k, v in bs]],
    })


def test_period_parsing():
    assert parse_period("Mar 2024") == ((2024, 3), "Mar 2024")
    assert parse_period("FY24")[0] == (2024, 0)
    assert parse_period("2023-24")[0] == (2024, 0)
    assert parse_period(2022)[0] == (2022, 0)
    assert parse_period(pd.Timestamp("2025-03-31"))[0] == (2025, 3)
    for text in ("TTM", "Notes", "Sales", 12.5, None, "", True):
        assert parse_period(text) is None


def test_two_sheet_annual_report_layout():
    table = read_statements(_two_sheet(), "report.xlsx")
    assert table.periods == YEARS
    roles = suggest_roles(table)
    assert roles["revenue"] == "Sales"
    assert roles["net_income"] == "Net Profit"
    assert roles["borrowings"] == "Borrowings"
    assert "OPM %" not in roles.values()          # a ratio line is never offered as a figure


def test_latest_first_columns_are_put_in_time_order():
    pl = [["Particulars", *reversed(YEARS)], *[[k, *reversed(v)] for k, v in PL]]
    table = read_statements(_xlsx({"P&L": pl}), "x.xlsx")
    assert table.periods == YEARS
    assert table.frame.loc["Sales"].tolist() == [1000, 1100, 1250, 1400]


def test_ttm_and_notes_columns_are_ignored():
    pl = [["Particulars", "Notes", *YEARS, "TTM"], *[[k, "n", *v, 9999] for k, v in PL]]
    table = read_statements(_xlsx({"P&L": pl}), "x.xlsx")
    assert table.periods == YEARS
    assert 9999 not in table.frame.to_numpy()


def test_csv_with_text_numbers_and_parentheses():
    text = "Particulars,FY22,FY23\nSales,\"1,000\",\"1,200\"\nExpenses,(800),(900)\n" \
           "Net profit,100,150\nTax,20,30\n"
    table = read_statements(text.encode(), "x.csv")
    assert table.frame.loc["Sales"].tolist() == [1000, 1200]
    assert table.frame.loc["Expenses"].tolist() == [-800, -900]


def test_not_a_statement_is_refused_with_a_clear_message():
    rows = [["date", "category", "amount"], ["2025-01-01", "a", 5], ["2025-01-02", "b", 6]]
    message = _fails(read_statements, _xlsx({"Sheet1": rows}), "x.xlsx")
    assert message and "statement" in message.lower()


def test_validate_mapping():
    table = read_statements(_two_sheet(), "report.xlsx")
    assert validate_mapping(table, {"revenue": "Sales", "tax": ""}) == {"revenue": "Sales"}
    assert _fails(validate_mapping, table, {"revenue": "No such row"})
    assert _fails(validate_mapping, table, {"nonsense": "Sales"})
    assert _fails(validate_mapping, table, {"revenue": "Sales", "net_income": "Sales"})


def test_lines_and_ratios_from_a_partial_statement():
    table = read_statements(_two_sheet(), "report.xlsx")
    mapping = validate_mapping(table, suggest_roles(table))
    lines, _ = build_lines(table, mapping)
    assert lines.index.tolist() == YEARS
    ratios, unavailable = ratios_from_lines(lines)
    assert "net_margin" in ratios and abs(ratios["net_margin"].iloc[-1] - 178 / 1400) < 1e-9
    # no current assets or payables were given: those ratios are listed, not invented
    assert "current_ratio" in unavailable and "current_ratio" not in ratios
    assert "payable_days" in unavailable


def test_every_ratio_matches_the_tested_calculator_on_a_full_statement_set():
    st = make_statements(seed=3, years=6)
    inc, bal = st.income, st.balance
    periods = list(inc.index)
    ebitda = inc["revenue"] - inc["cogs"] - inc["opex"]
    pbt = ebitda - inc["depreciation"] - inc["interest"]
    current_assets = (bal["cash"] + bal["receivables"] + bal["inventory"]
                      + bal["other_current_assets"])
    current_liab = bal["payables"] + bal["short_term_debt"] + bal["other_current_liabilities"]
    rows = {
        "Sales": inc["revenue"], "Cost of goods sold": inc["cogs"],
        "Operating Profit": ebitda, "Depreciation": inc["depreciation"],
        "Interest": inc["interest"], "Profit before tax": pbt, "Tax": inc["tax"],
        "Net profit": pbt - inc["tax"],
        "Cash and cash equivalents": bal["cash"], "Trade receivables": bal["receivables"],
        "Inventories": bal["inventory"], "Net Block": bal["fixed_assets"],
        "Total current assets": current_assets,
        "Total assets": current_assets + bal["fixed_assets"],
        "Trade payables": bal["payables"], "Total current liabilities": current_liab,
        "Short term borrowings": bal["short_term_debt"],
        "Long term borrowings": bal["long_term_debt"], "Total equity": bal["equity"],
    }
    sheet = [["Particulars", *periods], *[[k, *v.tolist()] for k, v in rows.items()]]
    table = read_statements(_xlsx({"Statements": sheet}), "full.xlsx")
    mapping = validate_mapping(table, suggest_roles(table))
    lines, _ = build_lines(table, mapping)
    ratios, unavailable = ratios_from_lines(lines)
    expected = compute_ratios(inc, bal)
    shared = [c for c in expected.columns if c in ratios.columns]
    assert len(shared) >= 14, (shared, unavailable)
    for col in shared:
        got, want = ratios[col].to_numpy(float), expected[col].to_numpy(float)
        assert np.allclose(got, want, equal_nan=True, rtol=1e-9, atol=1e-12), col


def test_growth_is_undefined_after_a_zero_or_negative_base():
    lines = pd.DataFrame({"revenue": [0.0, 100.0, 150.0], "net_income": [-5.0, 10.0, 20.0]},
                         index=["a", "b", "c"])
    growth = growth_from_lines(lines)
    assert np.isnan(growth.loc["b", "revenue"]) and np.isnan(growth.loc["b", "net_income"])
    assert abs(growth.loc["c", "revenue"] - 0.5) < 1e-12


def test_checks_flag_missing_revenue():
    table = read_statements(_two_sheet(), "report.xlsx")
    mapping = {"net_income": "Net Profit"}
    lines, _ = build_lines(table, mapping)
    issues = check_lines(lines, table, mapping)
    assert any("revenue" in i.lower() for i in issues)
