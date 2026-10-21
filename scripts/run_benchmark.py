"""Run the benchmark and write docs/BENCHMARK.md.

    python scripts/run_benchmark.py              # 100 seeds per scenario (a few minutes)
    python scripts/run_benchmark.py --n 30       # quick check
    python scripts/run_benchmark.py --no-timing  # skip the 1M-row timing
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from whymoved.benchmark import (  # noqa: E402
    placebo_check,
    render_report,
    run_kind,
    sensitivity,
    time_engine,
)
from whymoved.decompose import STANDARD_METRICS  # noqa: E402
from whymoved.generator import DIMENSIONS, SCENARIO_KINDS, make_scenario  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100, help="seeds per scenario")
    ap.add_argument("--no-timing", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "docs" / "BENCHMARK.md"))
    args = ap.parse_args()

    results = {}
    for kind in SCENARIO_KINDS:
        print(f"running {kind} x {args.n} ...", flush=True)
        results[kind] = run_kind(kind, n=args.n)
    sc = make_scenario("none", 20_000)
    print("placebo ...", flush=True)
    placebo = placebo_check(sc.data, STANDARD_METRICS["revenue"], DIMENSIONS,
                            (sc.period_a[0] - __import__("pandas").Timedelta(days=28),
                             sc.period_b[1]), trials=40)
    print("sensitivity ...", flush=True)
    sweep = sensitivity(n=max(20, args.n // 2))
    timings = None
    if not args.no_timing:
        print("timing ...", flush=True)
        timings = time_engine()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report(results, timings, placebo, sweep=sweep), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
