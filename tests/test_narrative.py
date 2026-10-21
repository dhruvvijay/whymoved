"""Tests for the narrative generator. Plain asserts; run with `python -m pytest -q`."""

import re

from whymoved.baseline import TrendCheck, explain_budget, trend_check
from whymoved.decompose import STANDARD_METRICS, additive
from whymoved.generator import DIMENSIONS, make_scenario
from whymoved.ingest import ValidationReport
from whymoved.narrative import CAVEAT, describe, format_number
from whymoved.trust import TrustedExplanation, TrustedFinding, explain_with_trust

CAUSAL = re.compile(
    r"\b(caused?|causing|because|due to|led to|leads to|reason|result of|resulted|"
    r"driven by|thanks to|owing to)\b",
    re.I,
)


def _finding(filters, explained, share, lo=None, hi=None, mix=None, rate=None):
    return TrustedFinding(
        filters=filters, rank=1, explained=explained, share_of_change=share,
        ci_low=explained * 1.2 if lo is None else lo,
        ci_high=explained * 0.8 if hi is None else hi,
        z=-6.0, rows_a=100, rows_b=100, mix_effect=mix, rate_effect=rate,
    )


def _explanation(verdict, findings=(), a=1000.0, b=900.0, metric=None, notes=()):
    return TrustedExplanation(metric or additive("revenue"), a, b, verdict,
                              findings=list(findings), notes=list(notes))


def test_number_formatting():
    assert format_number(1234567.8) == "1,234,568"
    assert format_number(1234567.8, indian=True) == "12,34,568"
    assert format_number(123, indian=True) == "123" and format_number(1234, indian=True) == "1,234"
    assert format_number(-36026.3, currency="Rs ", indian=True) == "-Rs 36,026"
    assert format_number(5, sign=True) == "+5" and format_number(-5, sign=True) == "-5"
    assert format_number(0.4, sign=True) == "+0" or format_number(0.4, sign=True) == "0"
    assert format_number(0.0, sign=True) == "0"
    assert format_number(2.5, decimals=2) == "2.50"
    assert format_number(float("nan")) == "n/a"
    assert format_number(12345678, indian=True) == "1,23,45,678"


def test_every_number_in_the_text_comes_from_the_analysis():
    sc = make_scenario("single", 7)
    ex = explain_with_trust(sc.data, STANDARD_METRICS["revenue"], sc.period_a, sc.period_b,
                            DIMENSIONS)
    text = describe(ex)
    top = ex.findings[0]
    for number in (ex.total_a, ex.total_b):
        assert format_number(number) in text
    assert format_number(top.explained, sign=True) in text
    assert format_number(top.ci_low, sign=True) in text
    assert format_number(top.ci_high, sign=True) in text
    assert f"{top.share_of_change:.0%}" in text
    assert "region = West Bengal and device = Mobile" in text
    assert text.endswith(CAVEAT)


def test_text_never_claims_a_cause_and_is_ascii_across_scenarios():
    checked = 0
    for kind, metric in (("single", "revenue"), ("double", "revenue"), ("noisy", "revenue"),
                         ("none", "revenue"), ("mix_only", "conversion"),
                         ("cost_rise", "revenue"), ("share_loss", "revenue")):
        for seed in (201, 202, 203):
            sc = make_scenario(kind, seed)
            spec = STANDARD_METRICS[metric]
            ex = explain_with_trust(sc.data, spec, sc.period_a, sc.period_b, DIMENSIONS)
            tr = trend_check(sc.data, spec, sc.period_a, sc.period_b)
            report = ValidationReport()
            report.add("warning", "date_gaps", "x")
            text = describe(ex, trend=tr, validation=report)
            assert not CAUSAL.search(text), (kind, seed, CAUSAL.search(text).group(0))
            assert text.isascii(), (kind, seed)
            checked += 1
    budget = make_scenario("budget_miss", 201)
    bex = explain_budget(budget.data, STANDARD_METRICS["revenue"], {"revenue": "budget_revenue"},
                         budget.period_b, DIMENSIONS)
    assert not CAUSAL.search(describe(bex))
    assert checked == 21


def test_causal_check_itself_catches_causal_wording():
    assert CAUSAL.search("Sales fell because of the app") and CAUSAL.search("due to rain")
    assert not CAUSAL.search("Mobile accounts for 98% of the net change")


def test_each_verdict_gets_its_own_wording():
    seg = _finding({"region": "North"}, -80.0, 0.8)
    texts = {
        "clear": describe(_explanation("clear_driver", [seg])),
        "partial": describe(_explanation("partial_driver", [_finding({"a": "x"}, -40, 0.4)])),
        "offset": describe(_explanation("offsetting_moves", [_finding({"a": "x"}, -50, -0.5)],
                                        a=1000, b=1100)),
        "none": describe(_explanation("no_clear_driver")),
        "tiny": describe(_explanation("negligible_change", a=1000, b=1000.2)),
    }
    assert "Most of the change sits in one place" in texts["clear"]
    assert "Part of the change sits in specific segments (40% of it)" in texts["partial"]
    assert "opposing moves" in texts["offset"] and "against the net change" in texts["offset"]
    assert "No single segment stands out" in texts["none"]
    assert "very small" in texts["tiny"] and "essentially flat" in texts["tiny"]
    assert len(set(texts.values())) == 5


def test_a_finding_larger_than_the_whole_change_is_not_shown_as_a_share():
    ex = _explanation("clear_driver", [_finding({"a": "x"}, -150.0, 1.5)], a=1000, b=900)
    text = describe(ex)
    assert "150%" not in text and "more than the whole net change" in text


def test_ratio_metrics_show_percent_points_and_split_mix_from_rate():
    spec = STANDARD_METRICS["conversion"]
    mix_heavy = _finding({"channel": "Social"}, -0.0036, 0.94, mix=-0.0028, rate=-0.0002)
    rate_heavy = _finding({"channel": "Email"}, -0.002, 0.5, mix=-0.0001, rate=-0.0009)
    both = _finding({"channel": "Ads"}, -0.002, 0.5, mix=-0.0006, rate=-0.0005)
    opposed = _finding({"channel": "Org"}, -0.002, 0.5, mix=-0.0006, rate=0.0003)
    ex = lambda f: _explanation("clear_driver", [f], a=0.0408, b=0.0370, metric=spec)  # noqa: E731
    t1, t2, t3, t4 = (describe(ex(f)) for f in (mix_heavy, rate_heavy, both, opposed))
    assert "4.08% to 3.70%" in t1 and "-0.38 pp" in t1 and "-0.36 pp" in t1
    assert "is mix (traffic moving between segments)" in t1 and "rate changed little" in t1
    assert "segment's own rate changing" in t2
    assert "Both mix" in t3
    assert "opposite directions" in t4


def test_non_percent_ratio_is_shown_as_a_plain_number():
    spec = STANDARD_METRICS["price"]
    text = describe(_explanation("no_clear_driver", a=1500.0, b=1620.0, metric=spec))
    assert "1,500.00 to 1,620.00" in text and "%" in text and "pp" not in text


def test_budget_text_talks_about_a_gap_and_skips_the_trend_check():
    sc = make_scenario("budget_miss", 7)
    ex = explain_budget(sc.data, STANDARD_METRICS["revenue"], {"revenue": "budget_revenue"},
                        sc.period_b, DIMENSIONS)
    trend = TrendCheck(available=True, verdict="within_expected", expected_change_pct=0.01,
                       actual_change_pct=0.0, z=0.1)
    text = describe(ex, trend=trend)
    assert "against a budget of" in text and "net gap" in text
    assert "net change" not in text and "Trend check" not in text


def test_trend_sentences_cover_all_outcomes():
    ex = _explanation("no_clear_driver")
    base = dict(available=True, expected_change_pct=0.02, actual_change_pct=-0.03, z=-4.0)
    below = describe(ex, trend=TrendCheck(verdict="below_expected", **base))
    above = describe(ex, trend=TrendCheck(verdict="above_expected", **base))
    within = describe(ex, trend=TrendCheck(verdict="within_expected", **base))
    off = describe(ex, trend=TrendCheck(available=False, reason="needs 4 complete weeks"))
    assert "lower than that" in below and "higher than that" in above
    assert "within normal week-to-week variation" in within and "would not stand out" in within
    assert "Trend check not available: needs 4 complete weeks." in off
    assert "z = -4.0" in below and "+2.0%" in below and "-3.0%" in below


def test_notes_and_data_warnings_appear_only_when_present():
    quiet = describe(_explanation("no_clear_driver", notes=["No segment moved clearly."]))
    assert "Notes:" not in quiet and "Data notes" not in quiet  # duplicate of the verdict line
    noted = describe(_explanation("no_clear_driver", notes=["East: data on only 3 day(s)"]))
    assert "Notes: East: data on only 3 day(s)." in noted
    report = ValidationReport()
    for code in ("date_gaps", "total_rows_removed", "duplicate_rows", "negative_values"):
        report.add("warning", code, "x")
    report.add("info", "date_range", "x")
    text = describe(_explanation("no_clear_driver"), validation=report)
    assert "raised 4 warning(s)" in text and "date_gaps" in text and ", ..." in text
    assert "Data notes" not in describe(_explanation("no_clear_driver"),
                                         validation=ValidationReport())


def test_period_labels_and_currency_are_used():
    ex = _explanation("negligible_change", a=1000.0, b=1001.0)
    text = describe(ex, currency="Rs ", indian_grouping=True,
                    period_labels=("1-28 Feb", "1-28 Mar"))
    assert "between 1-28 Feb and 1-28 Mar" in text and "Rs 1,000" in text


def test_text_is_deterministic():
    sc = make_scenario("double", 103)
    spec = STANDARD_METRICS["revenue"]
    one = describe(explain_with_trust(sc.data, spec, sc.period_a, sc.period_b, DIMENSIONS))
    two = describe(explain_with_trust(sc.data, spec, sc.period_a, sc.period_b, DIMENSIONS))
    assert one == two


def test_bridge_summary_names_segments_and_avoids_causal_words():
    from whymoved.narrative import describe_bridge
    from whymoved.pvm import price_volume_mix

    sc = make_scenario("cost_rise", 7)
    br = price_volume_mix(sc.data, sc.period_a, sc.period_b, ["category"], cost="cost",
                          n_boot=200)
    text = describe_bridge(br, currency="Rs ", indian_grouping=True)
    assert "Electronics" in text and "profit" in text.lower()
    assert "cost" in text and CAVEAT in text
    assert not CAUSAL.search(text.replace(CAVEAT, "")), text


def test_market_summary_names_region_and_avoids_causal_words():
    from whymoved.market import market_share_bridge
    from whymoved.narrative import describe_market

    sc = make_scenario("share_loss", 7)
    br = market_share_bridge(sc.data, sc.period_a, sc.period_b, ["region"], n_boot=200)
    text = describe_market(br, currency="Rs ", indian_grouping=True)
    assert sc.shocks[0].filters["region"] in text and "behind the market" in text
    assert CAVEAT in text and not CAUSAL.search(text.replace(CAVEAT, "")), text
    text.encode("ascii")
