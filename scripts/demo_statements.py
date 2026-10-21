"""Statement ratios on an invented company, with a planted margin squeeze and leverage shift.

    python scripts/demo_statements.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from whymoved.narrative import describe_ratios  # noqa: E402
from whymoved.statements import (  # noqa: E402
    check_statements,
    compute_ratios,
    dupont_change,
    make_statements,
)


def main() -> None:
    s = make_statements(7, margin_squeeze_year=2023, buyback_year=2024)
    print("Synthetic company. Planted:", s.truth)
    print("Checks:", check_statements(s.income, s.balance) or "balance sheet balances\n")
    ratios = compute_ratios(s.income, s.balance)
    print(ratios[["gross_margin", "net_margin", "roe", "debt_to_equity", "current_ratio"]]
          .round(3), "\n")
    for a, b in [("FY2022", "FY2023"), ("FY2023", "FY2024")]:
        print(describe_ratios(ratios, a, b, dupont_change(ratios, a, b)), "\n" + "=" * 70)


if __name__ == "__main__":
    main()
