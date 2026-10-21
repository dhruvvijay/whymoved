"""Financial statement ratios: a calculator, a validator, and a synthetic company generator.

What this is
    * `check_statements` tests that the statements are usable (balance sheet balances, no
      missing columns, no negative amounts where they make no sense).
    * `compute_ratios` turns an income statement and a balance sheet into standard ratios.
    * `dupont_change` splits a change in return on equity into margin, asset turnover and
      leverage effects that add up exactly.
    * `make_statements` builds an invented company (with optional planted events) for testing.

What this is not
    No forecasting, no scoring of "healthy" or "unhealthy", no peer benchmarks, no advice. A
    ratio is arithmetic on the numbers supplied; whether a value is good depends on the industry,
    the accounting policies and the business model. Only the user's own figures are used.

Conventions
    * Return ratios (ROE, ROA, asset turnover, equity multiplier) use AVERAGE of opening and
      closing balances, so they are undefined (NaN) for the first period.
    * Liquidity, leverage and "days" ratios use closing balances.
    * Days use a 365-day year. Division by zero gives NaN, never infinity.
    * `debt` is short-term plus long-term borrowings; lease liabilities count only if the caller
      puts them in those columns.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

INCOME_INPUT = ["revenue", "cogs", "opex", "depreciation", "interest", "tax"]
BALANCE_INPUT = ["cash", "receivables", "inventory", "other_current_assets", "fixed_assets",
                 "payables", "short_term_debt", "other_current_liabilities", "long_term_debt",
                 "equity"]

RATIO_INFO: dict[str, str] = {
    "gross_margin": "(revenue - cogs) / revenue",
    "ebitda_margin": "EBITDA / revenue",
    "operating_margin": "EBIT / revenue",
    "net_margin": "net income / revenue",
    "current_ratio": "current assets / current liabilities",
    "quick_ratio": "(cash + receivables) / current liabilities",
    "cash_ratio": "cash / current liabilities",
    "debt_to_equity": "(short-term + long-term debt) / equity",
    "debt_to_assets": "debt / total assets",
    "interest_coverage": "EBIT / interest expense",
    "roe": "net income / average equity",
    "roa": "net income / average total assets",
    "asset_turnover": "revenue / average total assets",
    "equity_multiplier": "average total assets / average equity",
    "inventory_days": "inventory / cogs x 365",
    "receivable_days": "receivables / revenue x 365",
    "payable_days": "payables / cogs x 365",
    "cash_conversion_days": "inventory days + receivable days - payable days",
}
PERCENT_RATIOS = {"gross_margin", "ebitda_margin", "operating_margin", "net_margin", "roe", "roa",
                  "debt_to_assets"}


class StatementError(ValueError):
    """The statements cannot be used."""


def _need(df: pd.DataFrame, cols: list[str], what: str) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise StatementError(f"{what} is missing columns: {', '.join(missing)}")


def derive(income: pd.DataFrame, balance: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Add subtotals (gross_profit, ebitda, ebit, pbt, net_income; current/total lines)."""
    _need(income, INCOME_INPUT, "income statement")
    _need(balance, BALANCE_INPUT, "balance sheet")
    inc, bal = income.copy(), balance.copy()
    inc["gross_profit"] = inc["revenue"] - inc["cogs"]
    inc["ebitda"] = inc["gross_profit"] - inc["opex"]
    inc["ebit"] = inc["ebitda"] - inc["depreciation"]
    inc["pbt"] = inc["ebit"] - inc["interest"]
    inc["net_income"] = inc["pbt"] - inc["tax"]
    bal["current_assets"] = bal[["cash", "receivables", "inventory", "other_current_assets"]].sum(
        axis=1)
    bal["total_assets"] = bal["current_assets"] + bal["fixed_assets"]
    bal["current_liabilities"] = bal[["payables", "short_term_debt",
                                      "other_current_liabilities"]].sum(axis=1)
    bal["total_liabilities"] = bal["current_liabilities"] + bal["long_term_debt"]
    bal["debt"] = bal["short_term_debt"] + bal["long_term_debt"]
    return inc, bal


def check_statements(income: pd.DataFrame, balance: pd.DataFrame, tolerance: float = 0.005
                     ) -> list[str]:
    """Return a list of problems (empty if none). `tolerance` is relative to total assets."""
    issues: list[str] = []
    try:
        inc, bal = derive(income, balance)
    except StatementError as exc:
        return [str(exc)]
    if not inc.index.equals(bal.index):
        issues.append("income statement and balance sheet cover different periods")
        return issues
    gap = (bal["total_assets"] - bal["total_liabilities"] - bal["equity"]).abs()
    off = gap > tolerance * bal["total_assets"].abs().clip(lower=1e-9)
    for period in bal.index[off]:
        issues.append(f"{period}: assets differ from liabilities plus equity by "
                      f"{gap[period]:,.2f}")
    for col in ("revenue", "cogs", "opex", "depreciation", "interest"):
        if (inc[col] < 0).any():
            where = ", ".join(map(str, inc.index[inc[col] < 0]))
            issues.append(f"{col} has negative values in {where}")
    for col in ("cash", "receivables", "inventory", "fixed_assets", "payables"):
        if (bal[col] < 0).any():
            where = ", ".join(map(str, bal.index[bal[col] < 0]))
            issues.append(f"{col} has negative values in {where}")
    if (inc["revenue"] <= 0).any():
        issues.append("revenue is zero or negative in some periods; margins will be undefined")
    if bal.isna().any().any() or inc.isna().any().any():
        issues.append("some cells are empty; ratios touching them will be undefined")
    return issues


def _div(a, b) -> pd.Series:
    b = b.where(b != 0)
    return a / b


def compute_ratios(income: pd.DataFrame, balance: pd.DataFrame) -> pd.DataFrame:
    """One row per period, one column per ratio in RATIO_INFO."""
    inc, bal = derive(income, balance)
    if not inc.index.equals(bal.index):
        raise StatementError("income statement and balance sheet cover different periods")
    avg = lambda s: (s + s.shift(1)) / 2  # noqa: E731
    avg_assets, avg_equity = avg(bal["total_assets"]), avg(bal["equity"])
    r = pd.DataFrame(index=inc.index)
    r["gross_margin"] = _div(inc["gross_profit"], inc["revenue"])
    r["ebitda_margin"] = _div(inc["ebitda"], inc["revenue"])
    r["operating_margin"] = _div(inc["ebit"], inc["revenue"])
    r["net_margin"] = _div(inc["net_income"], inc["revenue"])
    r["current_ratio"] = _div(bal["current_assets"], bal["current_liabilities"])
    r["quick_ratio"] = _div(bal["cash"] + bal["receivables"], bal["current_liabilities"])
    r["cash_ratio"] = _div(bal["cash"], bal["current_liabilities"])
    r["debt_to_equity"] = _div(bal["debt"], bal["equity"])
    r["debt_to_assets"] = _div(bal["debt"], bal["total_assets"])
    r["interest_coverage"] = _div(inc["ebit"], inc["interest"])
    r["roe"] = _div(inc["net_income"], avg_equity)
    r["roa"] = _div(inc["net_income"], avg_assets)
    r["asset_turnover"] = _div(inc["revenue"], avg_assets)
    r["equity_multiplier"] = _div(avg_assets, avg_equity)
    r["inventory_days"] = _div(bal["inventory"], inc["cogs"]) * 365
    r["receivable_days"] = _div(bal["receivables"], inc["revenue"]) * 365
    r["payable_days"] = _div(bal["payables"], inc["cogs"]) * 365
    r["cash_conversion_days"] = r["inventory_days"] + r["receivable_days"] - r["payable_days"]
    return r[list(RATIO_INFO)]


@dataclass
class DuPont:
    period_a: str
    period_b: str
    roe_a: float
    roe_b: float
    margin: float      # effect of net margin
    turnover: float    # effect of asset turnover
    leverage: float    # effect of equity multiplier

    @property
    def change(self) -> float:
        return self.roe_b - self.roe_a

    @property
    def residual(self) -> float:
        return self.change - (self.margin + self.turnover + self.leverage)


def dupont_change(ratios: pd.DataFrame, period_a, period_b) -> DuPont:
    """ROE = net margin x asset turnover x equity multiplier. Split the ROE change by swapping
    one factor at a time, in that order (margin first, leverage last). The three effects add up
    exactly; a different order would share the interaction terms differently."""
    for p in (period_a, period_b):
        if p not in ratios.index:
            raise StatementError(f"period {p!r} not found")
    a, b = ratios.loc[period_a], ratios.loc[period_b]
    cols = ["net_margin", "asset_turnover", "equity_multiplier", "roe"]
    if a[cols].isna().any() or b[cols].isna().any():
        raise StatementError("ROE factors are undefined for these periods (the first period has "
                             "no opening balance, or a denominator is zero)")
    m_a, t_a, l_a = a["net_margin"], a["asset_turnover"], a["equity_multiplier"]
    m_b, t_b, l_b = b["net_margin"], b["asset_turnover"], b["equity_multiplier"]
    return DuPont(str(period_a), str(period_b), float(a["roe"]), float(b["roe"]),
                  float((m_b - m_a) * t_a * l_a), float(m_b * (t_b - t_a) * l_a),
                  float(m_b * t_b * (l_b - l_a)))


# --------------------------------------------------------------------------- generator


@dataclass
class Statements:
    income: pd.DataFrame
    balance: pd.DataFrame
    truth: dict = field(default_factory=dict)


def make_statements(
    seed: int,
    years: int = 6,
    first_year: int = 2020,
    margin_squeeze_year: int | None = None,
    squeeze_size: float = 0.06,
    buyback_year: int | None = None,
    buyback_share: float = 0.35,
    noise: float = 0.01,
) -> Statements:
    """An invented company (amounts in a made-up currency unit, e.g. crore).

    margin_squeeze_year: from this year on, cost of goods is `squeeze_size` higher as a share
        of revenue (a planted fall in gross margin).
    buyback_year: that year, borrowing rises and the same amount is paid out to owners, so debt
        and the equity multiplier rise while operations do not change (a planted leverage shift).
    """
    rng = np.random.default_rng([seed, 11])
    n = years
    labels = [f"FY{first_year + i}" for i in range(n)]
    sq = None if margin_squeeze_year is None else margin_squeeze_year - first_year
    bb = None if buyback_year is None else buyback_year - first_year

    def jitter() -> float:
        return float(np.exp(rng.normal(0, noise)))

    revenue, cogs_ratio, opex_ratio = 1000.0, 0.62, 0.17
    fixed, tax_rate, rate, payout = 380.0, 0.25, 0.09, 0.60
    rec_days, inv_days, pay_days = 45.0, 60.0, 50.0
    inc_rows, bal_rows = [], []
    ltd = 0.22 * fixed
    std = 0.04 * fixed
    equity = None
    prev_debt = ltd + std
    for i in range(n):
        if i > 0:
            revenue *= 1.08 * jitter()
            fixed *= 1.06
        ratio = cogs_ratio + (squeeze_size if sq is not None and i >= sq else 0.0)
        cogs = revenue * ratio * jitter()
        opex = revenue * opex_ratio * jitter()
        dep = 0.08 * fixed
        interest = rate * prev_debt
        pbt = revenue - cogs - opex - dep - interest
        tax = tax_rate * max(pbt, 0.0)
        net = pbt - tax
        receivables = revenue * rec_days / 365
        inventory = cogs * inv_days / 365
        payables = cogs * pay_days / 365
        other_ca, other_cl = 0.02 * revenue, 0.03 * revenue
        extra_dividend = 0.0
        if bb is not None and i == bb:
            extra = buyback_share * (equity if equity is not None else 400.0)
            ltd += extra
            extra_dividend = extra
        noncash = receivables + inventory + other_ca + fixed
        if equity is None:  # opening year: equity is whatever balances the sheet
            cash = 0.05 * revenue
            liabilities = payables + std + other_cl + ltd
            equity = cash + noncash - liabilities
        else:
            equity = equity + net - payout * max(net, 0.0) - extra_dividend
            liabilities = payables + std + other_cl + ltd
            cash = liabilities + equity - noncash  # the balancing line
            floor = 0.02 * revenue
            if cash < floor:  # borrow what is needed to keep a minimum cash balance
                shortfall = floor - cash
                ltd += shortfall
                cash += shortfall
        inc_rows.append({"revenue": revenue, "cogs": cogs, "opex": opex, "depreciation": dep,
                         "interest": interest, "tax": tax})
        bal_rows.append({"cash": cash, "receivables": receivables, "inventory": inventory,
                         "other_current_assets": other_ca, "fixed_assets": fixed,
                         "payables": payables, "short_term_debt": std,
                         "other_current_liabilities": other_cl, "long_term_debt": ltd,
                         "equity": equity})
        prev_debt = ltd + std
    income = pd.DataFrame(inc_rows, index=labels).round(4)
    balance = pd.DataFrame(bal_rows, index=labels).round(4)
    truth = {"margin_squeeze_year": margin_squeeze_year, "squeeze_size": squeeze_size,
             "buyback_year": buyback_year, "buyback_share": buyback_share}
    return Statements(income, balance, truth)
