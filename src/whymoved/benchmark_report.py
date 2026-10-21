"""Turn benchmark results into a Markdown report with a failure gallery."""

from __future__ import annotations

import pandas as pd

KIND_DESCRIPTION = {
    "single": "one segment's conversion halves",
    "double": "two segments in different regions drop",
    "mix_only": "traffic shifts to a lower-converting channel; no segment gets worse",
    "noisy": "one smaller drop inside extra noise",
    "none": "noise and a mild uptrend only (correct answer: no clear driver)",
    "budget_miss": "one segment misses budget",
    "pvm": "Grocery volume up, Fashion price up (price/volume/mix bridge)",
    "cost_rise": "Electronics unit cost up (profit bridge and margin explanation)",
    "share_loss": "one region loses share while the market does not (market-share bridge)",
}


def render_report(results: pd.DataFrame, summary: pd.DataFrame, scaling: pd.DataFrame | None,
                  start: int, count: int) -> str:
    lines = [
        "# Benchmark",
        "",
        "All data here is synthetic. Each run plants a known cause, asks the engine to find it, "
        "and scores the answer against the planted truth. These numbers say how often the engine "
        "recovers what the generator planted. They are not accuracy on real businesses.",
        "",
        f"Seeds {start}-{start + count - 1} per scenario ({count} runs each). These seeds were "
        "not used while building or tuning the engine, but the generator is the same one the "
        "engine was developed against, so treat the rates as an upper bound for real data. The "
        "significance threshold (z >= 4.5) was chosen on earlier synthetic runs.",
        "",
        "## Results",
        "",
        "| Scenario | What is planted | Runs | Success | 95% interval | Exact match | "
        "Runs with an extra finding |",
        "|---|---|---|---|---|---|---|",
    ]
    for _, r in summary.iterrows():
        exact = "n/a" if pd.isna(r["exact_rate"]) else f"{r['exact_rate']:.0%}"
        lines.append(
            f"| {r['kind']} | {KIND_DESCRIPTION.get(r['kind'], '')} | {r['runs']} | "
            f"{r['ok']} ({r['ok_rate']:.0%}) | {r['ci_low']:.0%} to {r['ci_high']:.0%} | "
            f"{exact} | {r['runs_with_extra_findings']} |")
    lines += [
        "",
        "Success is defined per scenario in `src/whymoved/benchmark.py`. 'Exact match' counts "
        "runs where the finding was the planted segment itself and not a coarser or finer "
        "description of it (for example `device=Mobile` for a Delhi + Mobile drop). "
        "'Extra finding' counts runs where at least one reported finding matched no planted "
        "cause at all.",
        "",
    ]
    if scaling is not None and len(scaling):
        lines += ["## Speed", "",
                  "One `explain_with_trust` call (400 bootstrap draws, 4 dimensions, depth 2) on "
                  "the synthetic scenario inflated to the stated row count, on a 2-core cloud "
                  "container. Your machine will differ.", "",
                  "| Rows | Seconds | Peak memory (MB) | Verdict |", "|---|---|---|---|"]
        for _, r in scaling.iterrows():
            lines.append(f"| {int(r['rows']):,} | {r['seconds']} | {int(r['peak_memory_mb'])} | "
                         f"{r['verdict']} |")
        lines.append("")
    fails = results[~results["ok"]]
    lines += ["## Failure gallery", "",
              f"Every run that did not meet its success rule: {len(fails)} of {len(results)}.", ""]
    if fails.empty:
        lines.append("No failures in this run.")
    else:
        lines += ["| Scenario | Seed | Planted | What the engine said | Verdict |",
                  "|---|---|---|---|---|"]
        for _, r in fails.sort_values(["kind", "seed"]).iterrows():
            said = str(r["engine_said"]).replace("|", "/")
            lines.append(f"| {r['kind']} | {r['seed']} | {r['planted']} | {said} | "
                         f"{r['verdict']} |")
    lines.append("")
    return "\n".join(lines)
