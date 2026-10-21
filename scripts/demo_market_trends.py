"""Market share bridge and monthly trend analysis on synthetic data.

    python scripts/demo_market_trends.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from whymoved.generator import make_scenario  # noqa: E402
from whymoved.market import market_share_bridge  # noqa: E402
from whymoved.monthly import make_monthly  # noqa: E402
from whymoved.narrative import describe_market, describe_trend  # noqa: E402
from whymoved.trends import growth_table, latest_summary, seasonal_split  # noqa: E402


def main() -> None:
    sc = make_scenario("share_loss", 7)
    print(f"== Market share (synthetic). Planted: {sc.shocks[0]}\n")
    bridge = market_share_bridge(sc.data, sc.period_a, sc.period_b, ["region"])
    print(bridge.table[["share_a", "share_b", "market", "exposure", "share"]].round(3), "\n")
    print(describe_market(bridge, currency="Rs ", indian_grouping=True))

    print("\n\n== Trend analysis (synthetic monthly data, 36 months)\n")
    series = make_monthly(7, shock_month=30, shock_multiplier=0.85).set_index("month")["revenue"]
    print(growth_table(series).tail(6).round(3), "\n")
    print(describe_trend(seasonal_split(series), latest_summary(series), label="revenue"))


if __name__ == "__main__":
    main()
