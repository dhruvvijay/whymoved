"""Tests for the application layer: accounts, isolation, uploads, analyses, share links."""

import json
import random
import sqlite3
import tempfile
import time
from pathlib import Path

from whymoved.service import Service, ServiceError, ServiceSettings, security

GOOD_PASSWORD = "correct horse battery"


def make_service(**overrides) -> Service:
    folder = tempfile.mkdtemp()
    return Service(ServiceSettings(db_path=f"{folder}/t.db", data_dir=f"{folder}/up", **overrides))


def signup(svc: Service, email: str = "a@example.com") -> tuple[dict, str, str]:
    r = svc.register(email, GOOD_PASSWORD)
    return svc.authenticate(r["token"]), r["token"], r["workspaces"][0]["id"]


def code_of(fn, *args, **kwargs) -> tuple[int, str] | None:
    try:
        fn(*args, **kwargs)
    except ServiceError as exc:
        return exc.status, exc.code
    return None


def tiny_csv(days: int = 56, drop_region: str | None = None) -> bytes:
    """Small synthetic daily file: two regions, two devices, revenue and orders."""
    rng = random.Random(1)
    rows = ["date,region,device,revenue,orders"]
    start = time.mktime(time.strptime("2026-01-05", "%Y-%m-%d"))
    for d in range(days):
        day = time.strftime("%Y-%m-%d", time.localtime(start + d * 86400))
        for region in ("North", "South"):
            for device in ("Mobile", "Desktop"):
                value = 1000 + 7 * (d % 5) + rng.uniform(0, 60)
                if drop_region == region and device == "Mobile" and d >= days // 2:
                    value *= 0.4
                rows.append(f"{day},{region},{device},{value:.2f},{int(value // 20)}")
    return "\n".join(rows).encode()


# --------------------------------------------------------------------------- accounts


def test_register_login_and_token_flow():
    svc = make_service()
    r = svc.register("  A@Example.COM ", GOOD_PASSWORD)
    assert r["user"]["email"] == "a@example.com" and len(r["workspaces"]) == 1
    assert svc.authenticate(r["token"])["email"] == "a@example.com"
    login = svc.login("a@example.com", GOOD_PASSWORD)
    assert login["token"] != r["token"]
    svc.logout(login["token"])
    assert code_of(svc.authenticate, login["token"]) == (401, "not_authenticated")
    assert svc.authenticate(r["token"])  # other sessions stay valid
    assert code_of(svc.authenticate, None) == (401, "not_authenticated")
    assert code_of(svc.authenticate, "made-up") == (401, "not_authenticated")


def test_registration_rules():
    svc = make_service()
    assert code_of(svc.register, "not-an-email", GOOD_PASSWORD) == (422, "invalid_email")
    evil = "a'; DROP TABLE users;--@x.com"
    assert code_of(svc.register, evil, GOOD_PASSWORD) == (422, "invalid_email")
    assert code_of(svc.register, "a@example.com", "short") == (422, "weak_password")
    assert code_of(svc.register, "a@example.com", "password") == (422, "weak_password")
    assert code_of(svc.register, "a@example.com", "x" * 200) == (422, "weak_password")
    svc.register("a@example.com", GOOD_PASSWORD)
    assert code_of(svc.register, "A@EXAMPLE.com", GOOD_PASSWORD) == (409, "email_taken")


def test_passwords_are_salted_hashes_and_tokens_are_not_stored_in_clear():
    svc = make_service()
    r1 = svc.register("a@example.com", GOOD_PASSWORD)
    svc.register("b@example.com", GOOD_PASSWORD)
    con = sqlite3.connect(svc.s.db_path)
    hashes = [row[0] for row in con.execute("SELECT pw_hash FROM users")]
    assert len(set(hashes)) == 2 and all(h.startswith("scrypt$") for h in hashes)
    assert all(GOOD_PASSWORD not in h for h in hashes)
    stored = [row[0] for row in con.execute("SELECT token_hash FROM tokens")]
    assert r1["token"] not in stored and security.token_hash(r1["token"]) in stored
    con.close()
    assert security.verify_password(GOOD_PASSWORD, hashes[0])
    assert not security.verify_password("wrong", hashes[0])
    assert not security.verify_password("x", "garbage") and not security.verify_password("x", "")


def test_wrong_password_and_unknown_email_look_the_same():
    svc = make_service()
    svc.register("a@example.com", GOOD_PASSWORD)
    one = code_of(svc.login, "a@example.com", "wrong password")
    two = code_of(svc.login, "nobody@example.com", "wrong password")
    assert one == two == (401, "bad_credentials")


def test_login_is_throttled_after_repeated_failures_and_recovers_on_success():
    svc = make_service(login_max_failures=3)
    svc.register("a@example.com", GOOD_PASSWORD)
    for _ in range(3):
        assert code_of(svc.login, "a@example.com", "nope nope nope") == (401, "bad_credentials")
    assert code_of(svc.login, "a@example.com", GOOD_PASSWORD) == (429, "too_many_attempts")
    svc2 = make_service(login_max_failures=3)
    svc2.register("a@example.com", GOOD_PASSWORD)
    for _ in range(2):
        code_of(svc2.login, "a@example.com", "nope nope nope")
    assert svc2.login("a@example.com", GOOD_PASSWORD)["token"]  # success clears the count
    for _ in range(2):
        code_of(svc2.login, "a@example.com", "nope nope nope")
    assert svc2.login("a@example.com", GOOD_PASSWORD)["token"]
    # unknown emails are throttled too, so lock-outs do not reveal which emails exist
    for _ in range(3):
        code_of(svc.login, "ghost@example.com", "x" * 10)
    assert code_of(svc.login, "ghost@example.com", "x" * 10) == (429, "too_many_attempts")


def test_expired_tokens_are_rejected():
    svc = make_service(token_days=-1)
    r = svc.register("a@example.com", GOOD_PASSWORD)
    assert code_of(svc.authenticate, r["token"]) == (401, "not_authenticated")


# --------------------------------------------------------------------------- uploads


def test_upload_rules():
    svc = make_service(max_upload_bytes=5000, max_datasets_per_workspace=2)
    user, _, wid = signup(svc)
    good = tiny_csv(days=14)[:4000]
    assert code_of(svc.upload_dataset, user, wid, "x.exe", good) == (415, "unsupported_type")
    assert code_of(svc.upload_dataset, user, wid, "x.csv", b"") == (422, "empty_file")
    assert code_of(svc.upload_dataset, user, wid, "x.csv", b"a" * 5001) == (413, "too_large")
    err = code_of(svc.upload_dataset, user, wid, "x.csv", b"\x00\x01\x02 not a table \xff\xfe")
    assert err is not None and err[0] == 422
    err = code_of(svc.upload_dataset, user, wid, "x.xlsx", b"PK this is not really a zip")
    assert err is not None and err[0] == 422
    ok = svc.upload_dataset(user, wid, "a.csv", tiny_csv(days=14))  # ~14 days is accepted
    assert ok["meta"]["date_col"] == "date" and ok["meta"]["rows"] == 56
    svc.upload_dataset(user, wid, "b.csv", tiny_csv(days=14))
    third = code_of(svc.upload_dataset, user, wid, "c.csv", tiny_csv(days=14))
    assert third == (409, "workspace_full")


def test_filenames_cannot_escape_the_upload_folder():
    svc = make_service()
    user, _, wid = signup(svc)
    ds = svc.upload_dataset(user, wid, "../../../etc/evil.csv", tiny_csv(days=14))
    assert ds["filename"] == "evil.csv"
    files = list(Path(svc.s.data_dir).rglob("*"))
    stored = [f for f in files if f.is_file()]
    assert len(stored) == 1 and stored[0].parent == Path(svc.s.data_dir) / wid
    assert "evil" not in stored[0].name
    ds = svc.upload_dataset(user, wid, "..\\..\\win\\evil2.csv", tiny_csv(days=14))
    assert ds["filename"] == "evil2.csv"


def test_row_limit_is_enforced():
    svc = make_service(max_rows=100)
    user, _, wid = signup(svc)
    err = code_of(svc.upload_dataset, user, wid, "x.csv", tiny_csv(days=56))
    assert err == (422, "bad_file")


def test_dataset_name_is_cleaned():
    svc = make_service()
    user, _, wid = signup(svc)
    messy = "  Q1\x00 'sales'; --\n" + "x" * 300
    ds = svc.upload_dataset(user, wid, "a.csv", tiny_csv(days=14), name=messy)
    assert "\x00" not in ds["name"] and "\n" not in ds["name"] and len(ds["name"]) <= 100
    assert svc.get_dataset(user, ds["id"])["name"] == ds["name"]


# --------------------------------------------------------------------------- isolation


def test_other_users_cannot_see_or_touch_anything():
    svc = make_service()
    alice, _, a_ws = signup(svc, "alice@example.com")
    bob, _, b_ws = signup(svc, "bob@example.com")
    ds = svc.upload_dataset(alice, a_ws, "a.csv", tiny_csv())
    an = svc.create_analysis(alice, ds["id"], {"metric": "revenue"})
    share = svc.create_share(alice, an["id"], 7)
    not_found = (404, "not_found")
    assert code_of(svc.get_dataset, bob, ds["id"]) == not_found
    assert code_of(svc.list_datasets, bob, a_ws) == not_found
    assert code_of(svc.upload_dataset, bob, a_ws, "b.csv", tiny_csv()) == not_found
    assert code_of(svc.delete_dataset, bob, ds["id"]) == not_found
    assert code_of(svc.create_analysis, bob, ds["id"], {}) == not_found
    assert code_of(svc.list_analyses, bob, ds["id"]) == not_found
    assert code_of(svc.get_analysis, bob, an["id"]) == not_found
    assert code_of(svc.delete_analysis, bob, an["id"]) == not_found
    assert code_of(svc.create_share, bob, an["id"]) == not_found
    assert code_of(svc.revoke_shares, bob, an["id"]) == not_found
    assert code_of(svc.members, bob, a_ws) == not_found
    assert code_of(svc.add_member, bob, a_ws, "bob@example.com") == not_found
    # unknown ids give the identical answer, so ids cannot be probed
    assert code_of(svc.get_dataset, bob, "0" * 32) == not_found
    assert svc.list_datasets(bob, b_ws) == []
    assert svc.shared_result(share["token"])["result"]["metric"]  # the link still works
    assert svc.get_dataset(alice, ds["id"])["id"] == ds["id"]


def test_workspace_members_and_their_limits():
    svc = make_service()
    alice, _, ws = signup(svc, "alice@example.com")
    bob, _, _ = signup(svc, "bob@example.com")
    ds = svc.upload_dataset(alice, ws, "a.csv", tiny_csv())
    assert code_of(svc.add_member, alice, ws, "ghost@example.com") == (404, "no_such_user")
    members = svc.add_member(alice, ws, "BOB@example.com")
    assert {m["email"]: m["role"] for m in members} == {"alice@example.com": "owner",
                                                        "bob@example.com": "member"}
    assert code_of(svc.add_member, alice, ws, "bob@example.com") == (409, "already_member")
    assert code_of(svc.add_member, bob, ws, "alice@example.com") == (403, "forbidden")
    assert svc.get_dataset(bob, ds["id"])["id"] == ds["id"]
    assert svc.create_analysis(bob, ds["id"], {"metric": "revenue"})["status"] == "done"
    assert code_of(svc.delete_dataset, bob, ds["id"]) == (403, "forbidden")  # not his upload
    mine = svc.upload_dataset(bob, ws, "b.csv", tiny_csv())
    svc.delete_dataset(bob, mine["id"])
    svc.delete_dataset(alice, ds["id"])  # the owner may delete anyone's


# --------------------------------------------------------------------------- analyses


def test_demo_analysis_finds_the_planted_segment_and_result_is_strict_json():
    svc = make_service()
    user, _, wid = signup(svc)
    ds = svc.create_demo(user, wid, seed=7)
    assert ds["meta"]["synthetic"] is True and "West Bengal" in ds["meta"]["planted"]
    an = svc.create_analysis(user, ds["id"],
                             {"metric": "revenue", "currency": "Rs ", "indian": True})
    result = an["result"]
    assert result["verdict"] == "clear_driver"
    assert result["findings"][0]["filters"] == {"region": "West Bengal", "device": "Mobile"}
    wf = result["waterfall"]
    assert wf[0]["kind"] == "total" and wf[-1]["kind"] == "total"
    assert abs(wf[0]["value"] + sum(w["value"] for w in wf[1:-1]) - wf[-1]["value"]) < 1e-3
    assert "Rs " in result["summary"] and result["caveat"] in result["summary"]
    # the stored and returned JSON has no NaN/Infinity
    json.loads(json.dumps(result, allow_nan=False))
    listed = svc.list_analyses(user, ds["id"])
    assert listed[0]["headline"]["verdict"] == "clear_driver" and "result" not in listed[0]


def test_analysis_modes_and_ratio_metrics():
    svc = make_service()
    user, _, wid = signup(svc)
    ds = svc.create_demo(user, wid, seed=11)
    ratio = svc.create_analysis(user, ds["id"], {"metric": "orders / visits"})["result"]
    assert ratio["is_ratio"] is True and ratio["trend"] is not None
    budget = svc.create_analysis(user, ds["id"], {"metric": "revenue", "mode": "budget"})["result"]
    assert budget["mode"] == "budget" and budget["trend"] is None
    assert budget["waterfall"][0]["label"] == "Budget"


def test_bad_analysis_requests_get_clear_errors_and_run_no_code():
    svc = make_service()
    user, _, wid = signup(svc)
    ds = svc.upload_dataset(user, wid, "a.csv", tiny_csv())
    for params in (
        {"metric": "__import__('os').system('echo hacked')"},
        {"metric": "revenue / 0"},
        {"metric": "nonexistent_column"},
        {"metric": "revenue", "mode": "weird"},
        {"metric": "revenue", "dimensions": ["not_a_column"]},
        {"metric": "revenue", "period_a": ["2026-01-05"], "period_b": ["2026-02-01", "2026-03-01"]},
        {"metric": "revenue", "period_a": ["2026-02-10", "2026-01-01"],
         "period_b": ["2026-02-02", "2026-03-01"]},
        {"metric": "revenue", "mode": "budget"},  # this file has no budget columns
    ):
        err = code_of(svc.create_analysis, user, ds["id"], params)
        assert err is not None and err[0] == 422, params
    assert code_of(svc.create_analysis, user, ds["id"],
                   {"metric": "revenue", "dimensions": "region"}) == (422, "bad_params")
    assert svc.list_analyses(user, ds["id"]) == []  # failures are not stored


def test_explicit_periods_and_selected_dimensions():
    svc = make_service()
    user, _, wid = signup(svc)
    ds = svc.upload_dataset(user, wid, "a.csv", tiny_csv(days=56, drop_region="North"))
    result = svc.create_analysis(user, ds["id"], {
        "metric": "revenue", "dimensions": ["region", "device"],
        "period_a": ["2026-01-05", "2026-02-01"],
        "period_b": ["2026-02-02", "2026-03-01"]})["result"]
    assert result["period_a"] == ["2026-01-05", "2026-02-01"]
    assert result["findings"] and result["findings"][0]["filters"] == {"region": "North",
                                                                      "device": "Mobile"}


# --------------------------------------------------------------------------- share links


def test_share_link_shows_only_the_result_and_can_be_revoked_or_expire():
    svc = make_service()
    user, _, wid = signup(svc)
    ds = svc.upload_dataset(user, wid, "secret_customers.csv", tiny_csv(drop_region="South"),
                            name="Secret customer file")
    an = svc.create_analysis(user, ds["id"], {"metric": "revenue"})
    assert an["shared"] is False
    share = svc.create_share(user, an["id"], days=3)
    assert share["path"] == f"/shared/{share['token']}" and share["expires_at"]
    assert svc.get_analysis(user, an["id"])["shared"] is True
    public = svc.shared_result(share["token"])
    text = json.dumps(public)
    for secret in ("secret_customers", "Secret customer", ds["id"], wid, user["email"], user["id"]):
        assert secret not in text, secret
    con = sqlite3.connect(svc.s.db_path)
    stored = [r[0] for r in con.execute("SELECT token_hash FROM shares")]
    assert share["token"] not in stored and security.token_hash(share["token"]) in stored
    # expiry
    con.execute("UPDATE shares SET expires_at=?", (time.time() - 5,))
    con.commit()
    assert code_of(svc.shared_result, share["token"]) == (404, "not_found")
    # a fresh link, then revoke
    again = svc.create_share(user, an["id"])
    assert again["expires_at"] is None and svc.shared_result(again["token"])
    assert svc.revoke_shares(user, an["id"]) >= 1
    assert code_of(svc.shared_result, again["token"]) == (404, "not_found")
    assert svc.get_analysis(user, an["id"])["shared"] is False
    assert code_of(svc.shared_result, "not-a-token") == (404, "not_found")
    assert code_of(svc.shared_result, "") == (404, "not_found")
    assert code_of(svc.create_share, user, an["id"], 0) == (422, "bad_params")
    assert code_of(svc.create_share, user, an["id"], 4000) == (422, "bad_params")
    con.close()


def test_members_cannot_share_someone_elses_analysis_unless_owner():
    svc = make_service()
    alice, _, ws = signup(svc, "alice@example.com")
    bob, _, _ = signup(svc, "bob@example.com")
    svc.add_member(alice, ws, "bob@example.com")
    ds = svc.upload_dataset(alice, ws, "a.csv", tiny_csv())
    bobs = svc.create_analysis(bob, ds["id"], {"metric": "revenue"})
    alices = svc.create_analysis(alice, ds["id"], {"metric": "revenue"})
    assert code_of(svc.create_share, bob, alices["id"]) == (403, "forbidden")
    assert svc.create_share(bob, bobs["id"])["token"]
    assert svc.create_share(alice, bobs["id"])["token"]  # the owner may share anything


def test_deleting_a_dataset_removes_its_file_analyses_and_share_links():
    svc = make_service()
    user, _, wid = signup(svc)
    ds = svc.upload_dataset(user, wid, "a.csv", tiny_csv())
    an = svc.create_analysis(user, ds["id"], {"metric": "revenue"})
    token = svc.create_share(user, an["id"])["token"]
    assert any(Path(svc.s.data_dir).rglob("*.csv"))
    svc.delete_dataset(user, ds["id"])
    assert not any(Path(svc.s.data_dir).rglob("*.csv"))
    assert code_of(svc.get_analysis, user, an["id"]) == (404, "not_found")
    assert code_of(svc.shared_result, token) == (404, "not_found")
