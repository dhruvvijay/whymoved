"""False-alarm check on ANY real file (a public dataset, or your own).

    python scripts/public_check.py superstore.xlsx --metric Sales
    python scripts/public_check.py orders.csv --metric "Revenue / Orders" --trials 40

What it does: takes the last few whole weeks of the file, randomly assigns whole weeks to a fake
period A or B, and asks WhyMoved to explain the "change". The split is random, so nothing real
moved; every driver it reports is a false alarm. It also prints the real comparison of the last
two equal periods for you to look at. Real data has trends, holidays and heavy-tailed amounts
that the synthetic generator does not, so this tells you whether the engine's threshold is
right for YOUR data. A rate far above about 5% means the threshold needs raising.

I could not run this on a public dataset myself: the sandbox this project was built in cannot
download files. Run it on any dataset with a date column, a number and a few category columns
(Kaggle "Superstore", an Olist export, your own sales file) and keep the output.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

from whymoved.benchmark import placebo_check  # noqa: E402
from whymoved.ingest import DataError, prepare  # noqa: E402
from whymoved.metrics import FormulaError, build_metric  # noqa: E402
from whymoved.narrative import describe  # noqa: E402
from whymoved.trust import explain_with_trust  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--metric", help="column name or formula; default: first numeric column")
    ap.add_argument("--weeks", type=int, default=24, help="most weeks to use (even number)")
    ap.add_argument("--trials", type=int, default=30)
    args = ap.parse_args()
    try:
        p = prepare(args.file)
        for issue in [*p.report.errors, *p.report.warnings]:
            print(f"  [{issue.severity.upper()}] {issue.code}: {issue.message}")
        if not p.report.ok:
            print("Stopping: fix the errors above first.")
            return 1
        data, spec = build_metric(p.data, args.metric or p.metric_cols[0])
        last = data[p.date_col].max().normalize()
        total_weeks = int((last - data[p.date_col].min().normalize()).days + 1) // 7
        weeks = min(args.weeks, total_weeks) // 2 * 2
        if weeks < 4:
            print("Need at least 4 whole weeks of daily data.")
            return 1
        start = last - pd.Timedelta(days=7 * weeks - 1)
        print(f"Using {weeks} weeks: {start.date()} to {last.date()}; "
              f"categories: {', '.join(p.dimension_cols)}\n")

        half = 7 * (weeks // 2)
        period_a = (start, start + pd.Timedelta(days=half - 1))
        period_b = (start + pd.Timedelta(days=half), last)
        ex = explain_with_trust(data, spec, period_a, period_b, p.dimension_cols,
                                date_col=p.date_col)
        print("REAL comparison of the two halves:")
        print(describe(ex, validation=p.report), "\n")

        result = placebo_check(data, spec, p.dimension_cols, (start, last),
                               date_col=p.date_col, trials=args.trials)
        print("PLACEBO (random week assignment; any driver is a false alarm):")
        print(f"  {result['false_driver_runs']} of {result['trials']} random splits reported a "
              f"driver ({result['rate'] * 100:.0f}%, 95% interval "
              f"{result['ci'][0] * 100:.0f}-{result['ci'][1] * 100:.0f}%)")
        if result["ci"][0] > 0.05:
            print("  The false-alarm rate is above 5%: raise z_threshold for this data.")
    except (DataError, FormulaError, ValueError) as exc:
        print(f"Cannot continue: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
