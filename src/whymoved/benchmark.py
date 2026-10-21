"""Benchmark: how often does WhyMoved find the planted cause, and how often is it wrong?

Every scenario kind from the generator is run on many seeds that were not used to choose any
threshold, and each result is scored against the planted truth. Nothing here proves the engine
works on real data: the data is synthetic, the causes are simple, and the engine's z-threshold
was chosen on earlier synthetic seeds. The point is to measure, and publish, where it fails.

Scoring per kind
    single, noisy, budget_miss, mix_only, double
        top1_any    the first finding is the planted segment or a coarser/narrower description
        top1_exact  the first finding is exactly the planted segment
        all_found   every planted cause appears in the first three findings (any match level)
        wrong_first the first finding matches no planted cause
        missed      the verdict says there is no driver although one was planted
    none
        correct     verdict is "no clear driver" (or negligible change) with no findings
        false_driver  any finding was reported
    pvm        Fashion has the largest price effect and its interval excludes zero
    cost_rise  Electronics has the largest cost effect and its interval is below zero
    share_loss the planted region has the largest share effect and its interval is below zero
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from whymoved.baseline import explain_budget
from whymoved.decompose import STANDARD_METRICS
from whymoved.evaluate import match_level
from whymoved.generator import (
    DIMENSIONS,
    SCENARIO_KINDS,
    Scenario,
    _random_conversion_drop,
    generate_dataset,
    make_scenario,
)
from whymoved.market import market_share_bridge
from whymoved.pvm import price_volume_mix
from whymoved.trust import explain_with_trust

FIRST_SEED = 20000  # benchmark seeds; earlier development and tests used other seeds
CAUSE_KINDS = ("single", "noisy", "budget_miss", "mix_only", "double")


@dataclass
class Run:
    kind: str
    seed: int
    outcome: dict[str, bool]
    planted: str
    found: str
    verdict: str = ""
    note: str = ""


@dataclass
class KindResult:
    kind: str
    runs: list[Run] = field(default_factory=list)

    def rate(self, key: str) -> tuple[float, float, float]:
        """(rate, low, high): Wilson 95% interval for the share of runs with outcome[key]."""
        vals = [r.outcome[key] for r in self.runs if key in r.outcome]
        return wilson(sum(vals), len(vals))

    @property
    def n(self) -> int:
        return len(self.runs)


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    if n == 0:
        return (float("nan"),) * 3
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return p, max(0.0, centre - half), min(1.0, centre + half)


def _labels(filters_list: list[dict[str, str]]) -> str:
    return " + ".join(" & ".join(f"{k}={v}" for k, v in f.items()) for f in filters_list) or "-"


def _score_causes(sc: Scenario, n_boot: int) -> Run:
    spec = STANDARD_METRICS[sc.metric]
    if sc.baseline == "budget":
        ex = explain_budget(sc.data, spec, {"revenue": "budget_revenue"}, sc.period_b,
                            DIMENSIONS, n_boot=n_boot)
    else:
        ex = explain_with_trust(sc.data, spec, sc.period_a, sc.period_b, DIMENSIONS,
                                n_boot=n_boot)
    truths = [dict(s.filters) for s in sc.shocks]
    found = [dict(f.filters) for f in ex.findings]
    levels = [match_level(found[0], t) for t in truths] if found else []
    top1_any = any(lv is not None for lv in levels)
    top3 = found[:3]
    all_found = all(any(match_level(f, t) for f in top3) for t in truths)
    outcome = {
        "top1_any": top1_any,
        "top1_exact": any(lv == "exact" for lv in levels),
        "all_found": all_found,
        "wrong_first": bool(found) and not top1_any,
        "missed": ex.verdict in ("no_clear_driver", "negligible_change"),
    }
    note = ""
    if top1_any and not outcome["top1_exact"]:
        note = "coarser or narrower than planted"
    return Run(sc.kind, sc.seed, outcome, _labels(truths), _labels(found), ex.verdict, note)


def _score_none(sc: Scenario, n_boot: int) -> Run:
    ex = explain_with_trust(sc.data, STANDARD_METRICS[sc.metric], sc.period_a, sc.period_b,
                            DIMENSIONS, n_boot=n_boot)
    found = [dict(f.filters) for f in ex.findings]
    ok = ex.verdict in ("no_clear_driver", "negligible_change") and not found
    return Run("none", sc.seed, {"correct": ok, "false_driver": bool(found)}, "-",
               _labels(found), ex.verdict)


def _score_pvm(sc: Scenario, n_boot: int) -> Run:
    br = price_volume_mix(sc.data, sc.period_a, sc.period_b, ["category"], n_boot=n_boot)
    top = br.table["price"].idxmax()
    lo = br.ci.get("price", (float("nan"),))[0]
    ok = top == "Fashion" and lo > 0
    return Run("pvm", sc.seed, {"price_in_fashion": bool(ok)}, "price: Fashion",
               f"price: {top}", note="" if ok else f"price interval low {lo:,.0f}")


def _score_cost(sc: Scenario, n_boot: int) -> Run:
    br = price_volume_mix(sc.data, sc.period_a, sc.period_b, ["category"], cost="cost",
                          n_boot=n_boot)
    top = br.table["cost"].idxmin()
    hi = br.ci.get("cost", (0.0, float("nan")))[1]
    ok = top == "Electronics" and hi < 0
    return Run("cost_rise", sc.seed, {"cost_in_electronics": bool(ok)}, "cost: Electronics",
               f"cost: {top}", note="" if ok else f"cost interval high {hi:,.0f}")


def _score_share(sc: Scenario, n_boot: int) -> Run:
    br = market_share_bridge(sc.data, sc.period_a, sc.period_b, ["region"], n_boot=n_boot)
    top = br.table.index[0]
    planted = sc.shocks[0].filters["region"]
    hi = br.ci.get("share", (0.0, float("nan")))[1]
    ok = top == planted and hi < 0
    return Run("share_loss", sc.seed, {"region_found": bool(ok)}, f"share: {planted}",
               f"share: {top}", note="" if ok else f"share interval high {hi:,.0f}")


def run_kind(kind: str, n: int = 100, n_boot: int = 400, first_seed: int = FIRST_SEED
             ) -> KindResult:
    if kind not in SCENARIO_KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    score = {"none": _score_none, "pvm": _score_pvm, "cost_rise": _score_cost,
             "share_loss": _score_share}.get(kind, _score_causes)
    result = KindResult(kind)
    for seed in range(first_seed, first_seed + n):
        result.runs.append(score(make_scenario(kind, seed), n_boot))
    return result


# --------------------------------------------------------------------------- sensitivity


def sensitivity(multipliers=(0.95, 0.9, 0.85, 0.8, 0.7, 0.5), noises=(0.05, 0.2, 0.4),
                n: int = 60, n_boot: int = 400, first_seed: int = FIRST_SEED) -> list[dict]:
    """How small a drop is still found? One segment's conversion is multiplied by `multiplier`
    (0.9 = a 10% drop) at each noise level; the planted segment is one of the eligible
    region x device pairs (each carries at least 6% of traffic)."""
    rows = []
    for noise in noises:
        for mult in multipliers:
            hit = said_none = false_other = 0
            for seed in range(first_seed, first_seed + n):
                base = make_scenario("single", seed)
                shock = _random_conversion_drop(np.random.default_rng([seed, 1]), mult)
                data = generate_dataset(seed, [shock], extra_noise=noise)
                ex = explain_with_trust(data, STANDARD_METRICS["revenue"], base.period_a,
                                        base.period_b, DIMENSIONS, n_boot=n_boot)
                found = [dict(f.filters) for f in ex.findings]
                if not found:
                    said_none += 1
                elif match_level(found[0], dict(shock.filters)):
                    hit += 1
                else:
                    false_other += 1
            rows.append({"noise": noise, "drop": 1 - mult, "n": n, "found": hit,
                         "said_none": said_none, "wrong_first": false_other})
    return rows


# --------------------------------------------------------------------------- timing


def scaled_frame(base: pd.DataFrame, rows: int, seed: int = 0) -> pd.DataFrame:
    """Repeat `base` until it has about `rows` rows, with random +-5% jitter on the amounts so
    copies differ. The number of segments and days stays the same; only rows per group grow
    (like transaction-level data summarised by the engine)."""
    rng = np.random.default_rng(seed)
    copies = max(1, round(rows / len(base)))
    num = base.select_dtypes("number").columns
    parts = []
    for _ in range(copies):
        part = base.copy()
        part[num] = part[num].to_numpy() * rng.uniform(0.95, 1.05, size=(len(part), len(num)))
        parts.append(part)
    return pd.concat(parts, ignore_index=True)


def time_engine(row_counts=(100_000, 500_000, 1_000_000), seed: int = FIRST_SEED,
                n_boot: int = 400) -> list[dict]:
    sc = make_scenario("single", seed)
    spec = STANDARD_METRICS[sc.metric]
    out = []
    for rows in row_counts:
        df = scaled_frame(sc.data, rows, seed)
        t = time.perf_counter()
        ex = explain_with_trust(df, spec, sc.period_a, sc.period_b, DIMENSIONS, n_boot=n_boot)
        out.append({"rows": len(df), "seconds": time.perf_counter() - t,
                    "verdict": ex.verdict})
    return out


# --------------------------------------------------------------------------- placebo


def placebo_check(df: pd.DataFrame, spec, dimensions: list[str], window: tuple,
                  date_col: str = "date", trials: int = 20, n_boot: int = 200, seed: int = 0
                  ) -> dict:
    """False-alarm test for ANY dataset, including real files.

    Takes the days inside `window` (an even number of whole weeks), randomly assigns whole weeks
    to a fake period A or fake period B, and asks the engine to explain the "change". Because
    the split is random, nothing real moved, so every reported driver is a false alarm.
    Returns {"trials", "false_driver_runs", "rate", "ci"}.
    """
    lo, hi = (pd.Timestamp(x).normalize() for x in window)
    part = df[(df[date_col] >= lo) & (df[date_col] <= hi)].copy()
    weeks = ((part[date_col] - lo).dt.days // 7).to_numpy()
    n_weeks = int(weeks.max()) + 1
    if n_weeks < 4 or n_weeks % 2:
        raise ValueError("the window must hold an even number of whole weeks, at least 4")
    rng = np.random.default_rng(seed)
    false_runs = 0
    for _ in range(trials):
        order = rng.permutation(n_weeks)
        in_a = set(order[: n_weeks // 2].tolist())
        a_weeks = sorted(in_a)
        b_weeks = sorted(set(range(n_weeks)) - in_a)
        # lay the chosen weeks end to end so each fake period is contiguous
        new_week = np.zeros(n_weeks, dtype=int)
        for pos, w in enumerate(a_weeks + b_weeks):
            new_week[w] = pos
        shifted = part.copy()
        offset_days = (new_week[weeks] - weeks) * 7
        shifted[date_col] = part[date_col] + pd.to_timedelta(offset_days, unit="D")
        half = n_weeks // 2
        period_a = (lo, lo + pd.Timedelta(days=7 * half - 1))
        period_b = (lo + pd.Timedelta(days=7 * half), lo + pd.Timedelta(days=7 * n_weeks - 1))
        ex = explain_with_trust(shifted, spec, period_a, period_b, dimensions, date_col=date_col,
                                n_boot=n_boot, seed=int(rng.integers(1 << 30)))
        false_runs += bool(ex.findings)
    p, lo_ci, hi_ci = wilson(false_runs, trials)
    return {"trials": trials, "false_driver_runs": false_runs, "rate": p,
            "ci": (lo_ci, hi_ci)}


# --------------------------------------------------------------------------- report


def render_report(results: dict[str, KindResult], timings: list[dict] | None = None,
                  placebo: dict | None = None, max_failures: int = 25,
                  sweep: list[dict] | None = None) -> str:
    """Markdown: summary tables, timings, and a gallery of every miss."""
    def pct(t: tuple[float, float, float]) -> str:
        p, lo, hi = t
        return f"{p * 100:.0f}% ({lo * 100:.0f}-{hi * 100:.0f}%)"

    lines = ["# WhyMoved benchmark", "",
             "Synthetic data only. Seeds were not used to choose any threshold. Rates show the "
             "95% Wilson interval in brackets. A good score here means the engine works on "
             "simple planted causes; it says little about real business data.", ""]
    cause = [results[k] for k in CAUSE_KINDS if k in results]
    if cause:
        lines += ["## Finding planted causes", "",
                  "| scenario | runs | first finding right | exactly right | all causes in top 3 "
                  "| wrong first finding | said no driver |",
                  "|---|---|---|---|---|---|---|"]
        for r in cause:
            lines.append(f"| {r.kind} | {r.n} | {pct(r.rate('top1_any'))} | "
                         f"{pct(r.rate('top1_exact'))} | {pct(r.rate('all_found'))} | "
                         f"{pct(r.rate('wrong_first'))} | {pct(r.rate('missed'))} |")
        lines += ["", "\"First finding right\" counts a coarser or narrower description of the "
                  "planted segment (for example `device=Mobile` for Mobile in two regions). "
                  "\"Exactly right\" does not.", ""]
    if "none" in results:
        r = results["none"]
        lines += ["## No real change planted", "",
                  f"- Said \"no clear driver\": {pct(r.rate('correct'))} of {r.n} runs",
                  f"- Reported a driver anyway (false alarm): {pct(r.rate('false_driver'))}", ""]
    bridges = [(k, key, text) for k, key, text in [
        ("pvm", "price_in_fashion", "price effect located in Fashion, interval above zero"),
        ("cost_rise", "cost_in_electronics",
         "cost effect located in Electronics, interval below zero"),
        ("share_loss", "region_found",
         "lost share located in the planted region, interval below zero"),
    ] if k in results]
    if bridges:
        lines += ["## Bridges", "",
                  "| scenario | runs | success | what counts |", "|---|---|---|---|"]
        for k, key, text in bridges:
            lines.append(f"| {k} | {results[k].n} | {pct(results[k].rate(key))} | {text} |")
        lines.append("")
    if placebo:
        lines += ["## Placebo test (random split of weeks)", "",
                  f"{placebo['false_driver_runs']} of {placebo['trials']} random splits produced "
                  f"a reported driver ({placebo['rate'] * 100:.0f}%, 95% interval "
                  f"{placebo['ci'][0] * 100:.0f}-{placebo['ci'][1] * 100:.0f}%). Every one is a "
                  "false alarm.", ""]
    if sweep:
        lines += ["## How small a drop is still found?", "",
                  "One region x device segment's conversion falls by the stated amount. "
                  "\"Found\" means the first finding is that segment (or a coarser/narrower "
                  "description). Noise is the extra day-level noise added by the generator "
                  "(0.05 is the default).", "",
                  "| extra noise | drop in segment conversion | runs | found | said no driver "
                  "| wrong first finding |", "|---|---|---|---|---|---|"]
        for r in sweep:
            lines.append(f"| {r['noise']:.2f} | {r['drop'] * 100:.0f}% | {r['n']} | "
                         f"{pct(wilson(r['found'], r['n']))} | "
                         f"{pct(wilson(r['said_none'], r['n']))} | "
                         f"{pct(wilson(r['wrong_first'], r['n']))} |")
        lines.append("")
    if timings:
        lines += ["## Speed (engine only, 4 dimensions, 400 resamples)", "",
                  "| rows | seconds |", "|---|---|"]
        lines += [f"| {t['rows']:,} | {t['seconds']:.1f} |" for t in timings]
        lines += ["", "Large tables are the synthetic table repeated with +-5% jitter, so the "
                  "segment count stays fixed; many more segments would cost more.", ""]

    failures: dict[str, list[Run]] = defaultdict(list)
    coarse: dict[str, list[Run]] = defaultdict(list)
    for kind, res in results.items():
        for run in res.runs:
            if not all(run.outcome.get(k, True) for k in _SUCCESS_KEYS.get(kind, ())):
                failures[kind].append(run)
            elif run.note:
                coarse[kind].append(run)
    lines += ["## Failure gallery", ""]
    if not failures:
        lines.append("No failures in these runs.")
    for kind, runs in failures.items():
        lines += [f"### {kind}: {len(runs)} failure(s)", "",
                  "| seed | planted | engine reported | verdict | note |", "|---|---|---|---|---|"]
        for r in runs[:max_failures]:
            lines.append(f"| {r.seed} | {r.planted} | {r.found} | {r.verdict} | {r.note} |")
        if len(runs) > max_failures:
            lines.append(f"| ... | {len(runs) - max_failures} more not shown | | | |")
        lines.append("")
    if coarse:
        lines += ["## Right place, different description", ""]
        for kind, runs in coarse.items():
            lines.append(f"- {kind}: {len(runs)} runs reported a broader or narrower segment than "
                         "planted, e.g. " + "; ".join(
                             f"seed {r.seed}: planted {r.planted}, reported {r.found}"
                             for r in runs[:3]))
        lines.append("")
    return "\n".join(lines)


_SUCCESS_KEYS = {
    "single": ("top1_any", "all_found"), "noisy": ("top1_any", "all_found"),
    "budget_miss": ("top1_any", "all_found"), "mix_only": ("top1_any", "all_found"),
    "double": ("top1_any", "all_found"), "none": ("correct",),
    "pvm": ("price_in_fashion",), "cost_rise": ("cost_in_electronics",),
    "share_loss": ("region_found",),
}
