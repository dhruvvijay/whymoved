"""Checks a RUNNING WhyMoved server end to end, using only the standard library.

    python scripts/smoke_test.py http://127.0.0.1:8000
    python scripts/smoke_test.py https://your-app.example.com

It creates a throw-away account (smoke-<random>@example.com), loads the sample data, runs an
analysis, makes and opens a share link, revokes it, then deletes what it made. Exit code 0 means
every step worked; 1 means a step failed and the message says which. Use it right after a deploy.
"""
import json
import secrets
import sys
import urllib.error
import urllib.request


def call(base, method, path, body=None, token=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            return err.code, json.loads(raw)
        except ValueError:
            return err.code, None
    except urllib.error.URLError as err:
        sys.exit(f"FAIL - cannot reach {base}: {err.reason}")


def main(base):
    base = base.rstrip("/")
    steps = []

    def step(name, ok, detail=""):
        steps.append(name)
        note = f"  ({detail})" if detail and not ok else ""
        print(("ok   - " if ok else "FAIL - ") + name + note)
        if not ok:
            sys.exit(1)

    status, body = call(base, "GET", "/health")
    step("health endpoint answers", status == 200 and body == {"status": "ok"}, f"{status} {body}")

    email = f"smoke-{secrets.token_hex(4)}@example.com"
    password = "Smoke-" + secrets.token_urlsafe(9)
    status, reg = call(base, "POST", "/auth/register", {"email": email, "password": password})
    step("register", status == 201 and "token" in (reg or {}), f"{status} {reg}")
    token = reg["token"]

    status, me = call(base, "GET", "/me", token=token)
    step("token works", status == 200, f"{status} {me}")
    wid = me["workspaces"][0]["id"]

    status, _ = call(base, "GET", "/me")
    step("no token is refused", status == 401, str(status))

    status, ds = call(base, "POST", f"/workspaces/{wid}/demo", token=token)
    step("sample data loads", status == 201 and ds.get("id"), f"{status} {ds}")

    status, an = call(base, "POST", f"/datasets/{ds['id']}/analyses",
                      {"metric": "conversion"}, token)
    result = (an or {}).get("result") or {}
    step("analysis runs", status == 201 and result.get("waterfall"), f"{status} {an}")
    found = result.get("verdict") in ("clear_driver", "partial_driver")
    step("planted driver is found", found and bool(result.get("findings")),
         str(result.get("verdict")))
    step("caveat is present", "not why" in (result.get("caveat") or "").lower())

    status, note = call(base, "POST", f"/analyses/{an['id']}/notes",
                        {"body": "Smoke-test note.", "public": True}, token)
    step("a note can be added", status == 201 and note.get("public") is True, f"{status} {note}")
    status, got = call(base, "GET", f"/analyses/{an['id']}", token=token)
    step("the note comes back with the analysis", status == 200 and len(got.get("notes", [])) == 1,
         f"{status}")
    status, scan = call(base, "GET", f"/datasets/{ds['id']}/scan", token=token)
    step("metric scan ranks the columns", status == 200 and len(scan.get("rows", [])) >= 2,
         f"{status} {scan}")

    status, sh = call(base, "POST", f"/analyses/{an['id']}/share", {"days": 1}, token)
    step("share link is created", status == 201 and sh.get("token"), f"{status} {sh}")
    status, pub = call(base, "GET", f"/shared/{sh['token']}")
    shown = (pub or {}).get("result") or {}
    step("share link opens without signing in", status == 200 and "findings" in shown, str(status))
    step("the public note is on the share page, without an author",
         [set(n) for n in pub.get("notes", [])] == [{"body", "created_at"}], str(pub.get("notes")))
    status, _ = call(base, "DELETE", f"/analyses/{an['id']}/notes/{note['id']}", token=token)
    step("a note can be deleted", status == 204, str(status))
    call(base, "DELETE", f"/analyses/{an['id']}/share", token=token)
    status, _ = call(base, "GET", f"/shared/{sh['token']}")
    step("revoked share link stops working", status == 404, str(status))

    status, _ = call(base, "DELETE", f"/datasets/{ds['id']}", token=token)
    step("dataset is deleted", status == 204, str(status))
    call(base, "POST", "/auth/logout", token=token)
    status, _ = call(base, "GET", "/me", token=token)
    step("logged-out token is refused", status == 401, str(status))
    print(f"\nall {len(steps)} checks passed against {base}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
