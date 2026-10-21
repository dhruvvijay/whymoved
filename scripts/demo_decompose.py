"""Print the decomposition for a synthetic 'single' and 'mix_only' scenario.

Usage:
    python scripts/demo_decompose.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

from whymoved.decompose import STANDARD_METRICS, decompose, find_drivers  # noqa: E402
from whymoved.generator import DIMENSIONS, make_scenario  # noqa: E402

pd.options.display.float_format = "{:,.4f}".format
pd.options.display.width = 140


def show_single(seed: int = 7) -> None:
    sc = make_scenario("single", seed)
    spec = STANDARD_METRICS["revenue"]
    print(f"=== SINGLE (synthetic, seed {seed}): revenue, period A vs B ===")
    print("Planted truth:", sc.shocks[0].label())
    by_region = decompose(sc.data, spec, sc.period_a, sc.period_b, ["region"])
    print(f"\nTotal revenue: {by_region.total_a:,.0f} -> {by_region.total_b:,.0f} "
          f"(change {by_region.change:,.0f})")
    cols = ["region", "value_a", "value_b", "contribution", "share_of_change"]
    with pd.option_context("display.float_format", "{:,.2f}".format):
        print("\nStep 1, split by region:")
        print(by_region.table[cols].to_string(index=False))
    print(f"\nSum of contributions minus total change (should be ~0): {by_region.residual:.2e}")

    print("\nStep 2, ranked findings with drill-down:")
    for rank, f in enumerate(find_drivers(sc.data, spec, sc.period_a, sc.period_b, DIMENSIONS), 1):
        label = " & ".join(f"{k}={v}" for k, v in f.filters.items())
        print(f"  {rank}. {label:<38} contribution {f.contribution:>14,.0f}  "
              f"share of change {f.share_of_change:>7.1%}")


def show_mix_only(seed: int = 7) -> None:
    sc = make_scenario("mix_only", seed)
    spec = STANDARD_METRICS["conversion"]
    print(f"\n\n=== MIX_ONLY (synthetic, seed {seed}): conversion = orders / visits ===")
    print("Planted truth:", sc.shocks[0].label(), "(traffic shifts; no channel converts worse)")
    res = decompose(sc.data, spec, sc.period_a, sc.period_b, ["channel"])
    print(f"\nOverall conversion: {res.total_a:.4%} -> {res.total_b:.4%} "
          f"(change {res.change:+.4%})")
    cols = ["channel", "weight_a", "weight_b", "rate_a", "rate_b", "rate_effect", "mix_effect"]
    print("\nSplit by channel (effects are in conversion-rate units):")
    print(res.table[cols].to_string(index=False))
    print(f"\nTotal rate effect: {res.table['rate_effect'].sum():+.6f}")
    print(f"Total mix effect:  {res.table['mix_effect'].sum():+.6f}")
    print(f"Residual (should be ~0): {res.residual:.2e}")
    top = find_drivers(sc.data, spec, sc.period_a, sc.period_b, DIMENSIONS)[0]
    print("\nTop finding:", " & ".join(f"{k}={v}" for k, v in top.filters.items()),
          f"| mix {top.mix_effect:+.6f}, rate {top.rate_effect:+.6f}")


if __name__ == "__main__":
    show_single()
    show_mix_only()
