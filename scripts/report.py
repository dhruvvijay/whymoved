"""Explain a metric change in a CSV or Excel file and print a written summary.

Usage:
    python scripts/report.py                          # a messy SYNTHETIC sample workbook
    python scripts/report.py --metric Revenue
    python scripts/report.py --metric Revenue --budget      # actual versus the Budget_ column
    python scripts/report.py my_file.xlsx --metric "Revenue / Orders"
    python scripts/report.py my_file.csv --a 2026-01-05:2026-02-01 --b 2026-02-02:2026-03-01
    python scripts/report.py my_file.csv --metric margin --budget --currency "Rs " --indian

--metric takes a column name or a formula (see whymoved/metrics.py). Names with spaces need
`backticks`. With --budget only period B is used: it is compared with the budget columns.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import pandas as pd  # noqa: E402
from demo_ingest import build_messy_sample, parse_period  # noqa: E402

from whymoved.baseline import explain_budget, guess_budget_columns, trend_check  # noqa: E402
from whymoved.ingest import DataError, default_periods, prepare  # noqa: E402
from whymoved.metrics import FormulaError, build_budget_metric, build_metric  # noqa: E402
from whymoved.narrative import describe  # noqa: E402
from whymoved.trust import explain_with_trust  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("file", nargs="?")
    ap.add_argument("--metric", help="column name or formula; default: the first numeric column")
    ap.add_argument("--budget", action="store_true", help="compare period B with budget columns")
    ap.add_argument("--a", help="period A as YYYY-MM-DD:YYYY-MM-DD")
    ap.add_argument("--b", help="period B as YYYY-MM-DD:YYYY-MM-DD")
    ap.add_argument("--currency", default="", help='prefix for amounts, for example "Rs "')
    ap.add_argument("--indian", action="store_true", help="group digits as 12,34,567")
    args = ap.parse_args()

    path = args.file
    if path is None:
        path, truth = build_messy_sample()
        print(f"Built a messy SYNTHETIC workbook: {path}\n{truth}\n")

    try:
        first = prepare(path)
        if args.a and args.b:
            period_a, period_b = parse_period(args.a), parse_period(args.b)
        else:
            found = default_periods(first)
            if found is None:
                print("Could not choose periods (need daily data over at least 14 days). "
                      "Pass --a and --b.")
                return 1
            period_a, period_b = found
        p = prepare(path, period_a=period_a, period_b=period_b)
        problems = [*p.report.errors, *p.report.warnings]
        print(f"Date column: {p.date_col} | categories: {', '.join(p.dimension_cols)} | "
              f"numbers: {', '.join(p.metric_cols)}")
        for issue in problems:
            print(f"  [{issue.severity.upper()}] {issue.code}: {issue.message}")
        if not p.report.ok:
            print("\nStopping: fix the errors above first.")
            return 1

        formula = args.metric or p.metric_cols[0]
        if args.budget:
            mapping = guess_budget_columns(p.data.columns, p.metric_cols)
            data, spec, helper = build_budget_metric(p.data, formula, mapping)
            ex = explain_budget(data, spec, helper, period_b, p.dimension_cols,
                                date_col=p.date_col)
            trend = None
        else:
            data, spec = build_metric(p.data, formula)
            ex = explain_with_trust(data, spec, period_a, period_b, p.dimension_cols,
                                    date_col=p.date_col)
            trend = trend_check(data, spec, period_a, period_b, date_col=p.date_col)
    except (DataError, FormulaError, ValueError) as exc:
        print(f"Cannot continue: {exc}")
        return 1

    def label(period: tuple[pd.Timestamp, pd.Timestamp]) -> str:
        return f"{period[0].date()} to {period[1].date()}"

    print("\n" + "=" * 72 + "\n")
    print(describe(ex, trend=trend, validation=p.report, currency=args.currency,
                   indian_grouping=args.indian,
                   period_labels=(label(period_a), label(period_b))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
