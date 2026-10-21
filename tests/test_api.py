"""HTTP tests. They need `pip install -r requirements-api.txt` (fastapi + httpx); without those
packages this file defines no tests, so the rest of the suite still runs.

They were written without being able to run FastAPI in the build sandbox, so run them first:
    python -m pytest tests/test_api.py -q
"""

import tempfile

try:
    from fastapi.testclient import TestClient

    from whymoved.api import create_app
    HAVE_FASTAPI = True
except ImportError:  # pragma: no cover
    HAVE_FASTAPI = False

from whymoved.service import Service, ServiceSettings

from .test_service import GOOD_PASSWORD, tiny_csv

if HAVE_FASTAPI:

    def make_client(**overrides) -> TestClient:
        folder = tempfile.mkdtemp()
        settings = ServiceSettings(db_path=f"{folder}/t.db", data_dir=f"{folder}/up", **overrides)
        svc = Service(settings)
        return TestClient(create_app(svc))

    def register(client: TestClient, email: str = "a@example.com") -> tuple[dict, str]:
        r = client.post("/auth/register", json={"email": email, "password": GOOD_PASSWORD})
        assert r.status_code == 201, r.text
        body = r.json()
        return {"Authorization": f"Bearer {body['token']}"}, body["workspaces"][0]["id"]

    def upload(client, headers, wid, data=None, name="a.csv"):
        content = data or tiny_csv(drop_region="North")
        return client.post(f"/workspaces/{wid}/datasets", headers=headers,
                           files={"file": (name, content, "text/csv")})

    def test_health_and_security_headers():
        r = make_client().get("/health")
        assert r.status_code == 200 and r.json() == {"status": "ok"}
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["cache-control"] == "no-store"

    def test_auth_required_and_error_shape():
        c = make_client()
        for headers in ({}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer nope"}):
            r = c.get("/me", headers=headers)
            assert r.status_code == 401 and r.json()["error"]["code"] == "not_authenticated"
        headers, _ = register(c)
        assert c.get("/me", headers=headers).json()["user"]["email"] == "a@example.com"
        dup = c.post("/auth/register", json={"email": "A@example.com", "password": GOOD_PASSWORD})
        assert dup.status_code == 409 and dup.json()["error"]["code"] == "email_taken"
        bad = c.post("/auth/login", json={"email": "a@example.com", "password": "wrong password"})
        assert bad.status_code == 401
        assert c.post("/auth/register", json={"email": "x"}).status_code == 422  # missing field
        assert c.post("/auth/logout", headers=headers).status_code == 204
        assert c.get("/me", headers=headers).status_code == 401

    def test_full_flow_upload_analyse_share_revoke():
        c = make_client()
        headers, wid = register(c)
        r = upload(c, headers, wid)
        assert r.status_code == 201, r.text
        ds = r.json()
        assert c.get(f"/workspaces/{wid}/datasets", headers=headers).json()[0]["id"] == ds["id"]
        r = c.post(f"/datasets/{ds['id']}/analyses", headers=headers, json={"metric": "revenue"})
        assert r.status_code == 201, r.text
        an = r.json()
        assert an["result"]["findings"][0]["filters"] == {"region": "North", "device": "Mobile"}
        assert c.get(f"/analyses/{an['id']}", headers=headers).json()["status"] == "done"
        assert len(c.get(f"/datasets/{ds['id']}/analyses", headers=headers).json()) == 1
        share = c.post(f"/analyses/{an['id']}/share", headers=headers, json={"days": 3}).json()
        public = c.get(share["path"])  # no Authorization header
        assert public.status_code == 200 and public.json()["result"]["metric"]
        assert "a.csv" not in public.text
        assert c.delete(f"/analyses/{an['id']}/share", headers=headers).json() == {"revoked": 1}
        assert c.get(share["path"]).status_code == 404
        assert c.delete(f"/analyses/{an['id']}", headers=headers).status_code == 204
        assert c.delete(f"/datasets/{ds['id']}", headers=headers).status_code == 204

    def test_users_are_isolated_over_http():
        c = make_client()
        alice, a_ws = register(c, "alice@example.com")
        bob, _ = register(c, "bob@example.com")
        ds = upload(c, alice, a_ws).json()
        assert c.get(f"/datasets/{ds['id']}", headers=bob).status_code == 404
        assert c.get(f"/workspaces/{a_ws}/datasets", headers=bob).status_code == 404
        assert upload(c, bob, a_ws).status_code == 404
        assert c.post(f"/datasets/{ds['id']}/analyses", headers=bob, json={}).status_code == 404

    def test_upload_limits_and_bad_requests():
        c = make_client(max_upload_bytes=3000)
        headers, wid = register(c)
        assert upload(c, headers, wid, b"x" * 4000).status_code in (413, 422)
        assert upload(c, headers, wid, tiny_csv(days=14), name="x.exe").status_code == 415
        assert c.post(f"/workspaces/{wid}/datasets", headers=headers).status_code == 422  # no file
        big = make_client(max_upload_bytes=1000)
        h2, w2 = register(big)
        r = big.post(f"/workspaces/{w2}/datasets", headers=h2,
                     files={"file": ("a.csv", b"a,b\n" + b"1,2\n" * 600_000, "text/csv")})
        assert r.status_code == 413

    def test_bad_analysis_input_is_422_with_a_message():
        c = make_client()
        headers, wid = register(c)
        ds = upload(c, headers, wid).json()
        r = c.post(f"/datasets/{ds['id']}/analyses", headers=headers,
                   json={"metric": "__import__('os').getcwd()"})
        assert r.status_code == 422 and r.json()["error"]["message"]
        r = c.post(f"/datasets/{ds['id']}/analyses", headers=headers, json={"mode": 5})
        assert r.status_code == 422

    def test_cors_allows_only_configured_origins():
        c = make_client()
        ok = c.options("/me", headers={"Origin": "http://localhost:5173",
                                       "Access-Control-Request-Method": "GET",
                                       "Access-Control-Request-Headers": "authorization"})
        assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
        bad = c.options("/me", headers={"Origin": "https://evil.example",
                                        "Access-Control-Request-Method": "GET"})
        assert "access-control-allow-origin" not in bad.headers
