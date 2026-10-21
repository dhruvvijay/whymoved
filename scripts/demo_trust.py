"""Show the trust layer on synthetic scenarios: findings, intervals, verdicts.

Usage:
    python scripts/demo_trust.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from whymoved.decompose import STANDARD_METRICS  # noqa: E402
from whymoved.generator import DIMENSIONS, make_scenario  # noqa: E402
from whymoved.trust import explain_with_trust  # noqa: E402


def show(kind: str, metric: str, seed: int) -> None:
    sc = make_scenario(kind, seed)
    spec = STANDARD_METRICS[metric]
    ex = explain_with_trust(sc.data, spec, sc.period_a, sc.period_b, DIMENSIONS)
    planted = "; ".join(s.label() for s in sc.shocks) or "nothing (correct answer: no clear driver)"
    print(f"\n=== {kind.upper()} (synthetic, seed {seed}) | metric: {metric} ===")
    print(f"Planted: {planted}")
    fmt = "{:.4%}" if spec.is_ratio else "{:,.0f}"
    print(f"{metric}: {fmt.format(ex.total_a)} -> {fmt.format(ex.total_b)}")
    print(f"VERDICT: {ex.verdict}")
    for f in ex.findings:
        unit = "{:+.5f}" if spec.is_ratio else "{:+,.0f}"
        print(
            f"  #{f.rank} {f.label():<36} explained {unit.format(f.explained):>14} "
            f"({f.share_of_change:>6.0%} of net change)  95% CI "
            f"[{unit.format(f.ci_low)}, {unit.format(f.ci_high)}]  z={f.z:+.1f}  "
            f"rows {f.rows_a}/{f.rows_b}"
        )
    for note in ex.notes:
        print(f"  note: {note}")


if __name__ == "__main__":
    show("single", "revenue", 7)
    show("none", "revenue", 1)
    show("mix_only", "conversion", 7)
    show("double", "revenue", 103)  # two causes sharing a device -> a broader finding
