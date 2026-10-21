"""Write a synthetic scenario to data/synthetic/ as CSV plus a ground-truth JSON file.

Usage:
    python scripts/generate_sample.py --kind single --seed 7
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from whymoved.generator import SCENARIO_KINDS, make_scenario  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=SCENARIO_KINDS, default="single")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "synthetic")
    args = parser.parse_args()

    scenario = make_scenario(args.kind, args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    stem = f"whymoved_synthetic_{args.kind}_seed{args.seed}"
    csv_path = args.out / f"{stem}.csv"
    truth_path = args.out / f"{stem}.truth.json"

    scenario.data.to_csv(csv_path, index=False)
    truth_path.write_text(json.dumps(scenario.truth_dict(), indent=2), encoding="utf-8")

    print(f"Wrote {csv_path} ({len(scenario.data):,} rows) [SYNTHETIC]")
    print(f"Wrote {truth_path}")
    for shock in scenario.shocks:
        print(f"  planted: {shock.label()}")
    if not scenario.shocks:
        print("  planted: nothing (correct answer is 'no clear driver')")


if __name__ == "__main__":
    main()
