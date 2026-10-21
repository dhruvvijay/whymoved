"""Tests for notes on analyses, the metric scan, the watchlist and the company comparison."""

import json
import re

from tests.test_analyses import screener_workbook
from tests.test_narrative import CAUSAL
from tests.test_service import code_of, make_service, signup, tiny_csv
from whymoved.watchlist import compare_statements, takeaways, watchlist


def _analysis(svc, user, wid):
    ds = svc.upload_dataset(user, wid, "t.csv", tiny_csv())
    return ds, svc.create_analysis(user, ds["id"], {"metric": "revenue"})


# --------------------------------------------------------------------------- notes
def test_notes_are_private_until_made_public_and_hide_the_author():
    svc = make_service()
    user, _t, wid = signup(svc)
    _ds, a = _analysis(svc, user, wid)
    svc.add_note(user, a["id"], "Private thought.", public=False)
    svc.add_note(user, a["id"], "North region ran a promotion in that fortnight.", public=True)
    notes = svc.get_analysis(user, a["id"])["notes"]
    assert [n["public"] for n in notes] == [False, True] and all(n["mine"] for n in notes)
    share = svc.create_share(user, a["id"])
    shown = svc.shared_result(share["token"])["notes"]
    assert [n["body"] for n in shown] == ["North region ran a promotion in that fortnight."]
    assert set(shown[0]) == {"body", "created_at"}  # no author, no id
    assert "a@example.com" not in json.dumps(svc.shared_result(share["token"]))


def test_note_rules_and_isolation():
    svc = make_service()
    user, _t, wid = signup(svc)
    other, _t2, _w2 = signup(svc, "b@example.com")
    _ds, a = _analysis(svc, user, wid)
    assert code_of(svc.add_note, user, a["id"], "   ")[0] == 422
    assert code_of(svc.add_note, user, a["id"], "x" * 1600)[0] == 422
    assert code_of(svc.add_note, other, a["id"], "hello") == (404, "not_found")
    note = svc.add_note(user, a["id"], "line one\n\n\n\nline two\x00")
    assert note["body"] == "line one\n\nline two"
    assert code_of(svc.delete_note, other, a["id"], note["id"]) == (404, "not_found")
    svc.delete_note(user, a["id"], note["id"])
    assert svc.get_analysis(user, a["id"])["notes"] == []
    assert code_of(svc.delete_note, user, a["id"], note["id"]) == (404, "not_found")


def test_deleting_an_analysis_removes_its_notes():
    svc = make_service()
    user, _t, wid = signup(svc)
    _ds, a = _analysis(svc, user, wid)
    svc.add_note(user, a["id"], "keep?")
    svc.delete_analysis(user, a["id"])
    assert svc.store.notes_of(a["id"]) == []


# --------------------------------------------------------------------------- scan
def test_scan_ranks_columns_and_refuses_statements():
    svc = make_service()
    user, _t, wid = signup(svc)
    ds = svc.upload_dataset(user, wid, "t.csv", tiny_csv(drop_region="North"))
    out = svc.scan(user, ds["id"])
    assert out["kind"] == "scan" and {r["metric"] for r in out["rows"]} == {"revenue", "orders"}
    sizes = [abs(r["change_pct"]) for r in out["rows"] if r["change_pct"] is not None]
    assert sizes == sorted(sizes, reverse=True)
    assert not CAUSAL.search(out["caveat"])
    data, _truth = screener_workbook()
    st = svc.upload_dataset(user, wid, "co.xlsx", data)
    assert code_of(svc.scan, user, st["id"])[0] == 422
    other, _t2, _w2 = signup(svc, "b@example.com")
    assert code_of(svc.scan, other, ds["id"]) == (404, "not_found")


# --------------------------------------------------------------------------- watchlist
def _result(**series):
    periods = ["P1", "P2", "P3", "P4"]
    return {"periods": periods, "period_a": "P3", "period_b": "P4", "financial": False,
            "ratios": [{"key": k, "label": k, "unit": "days", "values": v, "formula": ""}
                       for k, v in series.items()],
            "lines": [], "growth": [], "dupont": None}


def test_watchlist_flags_a_rising_ratio_with_its_numbers_and_stays_quiet_otherwise():
    rising = watchlist(_result(receivable_days=[10.0, 11.0, 14.0, 20.0]))
    flag = next(f for f in rising["flags"] if f["id"] == "receivable_days")
    assert "11.0" in flag["detail"] and "20.0" in flag["detail"]
    assert [e["value"] for e in flag["evidence"]] == [11.0, 14.0, 20.0]
    assert not CAUSAL.search(flag["title"] + " " + flag["detail"])
    steady = watchlist(_result(receivable_days=[10.0, 10.5, 10.2, 10.4]))
    assert not [f for f in steady["flags"] if f["id"] == "receivable_days"]
    assert "Receivable days rising" in steady["clear"]
    assert "Inventory days rising" in steady["skipped"]  # no inventory line: skipped, not guessed


def test_real_shaped_workbook_gives_flags_with_evidence_and_plain_takeaways():
    svc = make_service()
    user, _t, wid = signup(svc)
    data, _truth = screener_workbook()
    ds = svc.upload_dataset(user, wid, "co.xlsx", data)
    r = svc.create_analysis(user, ds["id"], {"kind": "statements"})["result"]
    assert r["watchlist"]["checked"] + len(r["watchlist"]["skipped"]) == 12
    assert 1 <= len(r["takeaways"]) <= 4
    for text in r["takeaways"] + [f["detail"] for f in r["watchlist"]["flags"]]:
        assert not CAUSAL.search(text), text
    json.dumps(r, allow_nan=False)
    assert takeaways(r) == r["takeaways"]


# --------------------------------------------------------------------------- comparison
def test_compare_two_companies_and_refuse_wrong_inputs():
    svc = make_service()
    user, _t, wid = signup(svc)
    a = svc.upload_dataset(user, wid, "a.xlsx", screener_workbook("ALPHA LTD")[0])
    b = svc.upload_dataset(user, wid, "b.xlsx", screener_workbook("BETA LTD", 0.08)[0])
    out = svc.compare(user, a["id"], b["id"])
    assert out["names"] == ["ALPHA LTD", "BETA LTD"] and out["rows"]
    assert all(set(r["values"][0]) == {"from", "to"} for r in out["rows"])
    assert not CAUSAL.search(" ".join(out["summary_lines"]) + out["caveat"])
    json.dumps(out, allow_nan=False)
    assert code_of(svc.compare, user, a["id"], a["id"])[0] == 422
    csv = svc.upload_dataset(user, wid, "t.csv", tiny_csv())
    assert code_of(svc.compare, user, a["id"], csv["id"])[0] == 422
    other, _t2, _w2 = signup(svc, "b@example.com")
    assert code_of(svc.compare, other, a["id"], b["id"]) == (404, "not_found")


def test_compare_notes_a_bank_against_a_non_bank():
    base = _result(net_margin=[0.1, 0.1, 0.1, 0.1])
    bank = dict(base, financial=True)
    out = compare_statements(base, bank, ("Shop", "Bank"))
    assert any(re.search(r"bank or finance", n) for n in out["notes"])
