"""Price / volume / mix and profit bridge on the synthetic scenarios.

    python scripts/demo_pvm.py            # pvm scenario: Grocery volume up, Fashion price up
    python scripts/demo_pvm.py cost_rise  # Electronics unit cost up
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from whymoved.generator import SCENARIO_KINDS, make_scenario  # noqa: E402
from whymoved.narrative import describe_bridge  # noqa: E402
from whymoved.pvm import price_volume_mix  # noqa: E402


def main() -> None:
    kind = sys.argv[1] if len(sys.argv) > 1 else "pvm"
    if kind not in SCENARIO_KINDS:
        raise SystemExit(f"choose one of {', '.join(SCENARIO_KINDS)}")
    sc = make_scenario(kind, 7)
    print(f"Scenario {kind} (synthetic data). Planted: {sc.shocks}\n")
    bridge = price_volume_mix(sc.data, sc.period_a, sc.period_b, ["category"], cost="cost")
    print(bridge.table.round(0).to_string(), "\n")
    print(describe_bridge(bridge, currency="Rs ", indian_grouping=True))


if __name__ == "__main__":
    main()
