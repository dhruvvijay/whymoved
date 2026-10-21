"""Score engine findings against planted ground truth from the synthetic generator."""

from __future__ import annotations

from whymoved.decompose import Finding


def match_level(found: dict[str, str], truth: dict[str, str]) -> str | None:
    """'exact' if identical, 'partial' if one is a strict sub-description of the other."""
    if found == truth:
        return "exact"
    f, t = set(found.items()), set(truth.items())
    if f < t or t < f:
        return "partial"
    return None


def recovery_rank(
    findings: list[Finding], truth: dict[str, str], exact_only: bool = False
) -> int | None:
    """1-based rank of the first finding matching `truth`, or None if it is missing."""
    for rank, finding in enumerate(findings, start=1):
        level = match_level(finding.filters, truth)
        if level == "exact" or (level == "partial" and not exact_only):
            return rank
    return None
