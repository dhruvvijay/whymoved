"""Test-only HTTP server: the same routes as src/whymoved/api.py, written with the standard
library so the browser app can be tested where FastAPI cannot be installed. It calls the real
`Service`, so auth, isolation, analysis and sharing behave exactly as in production; only the
HTTP framing differs. Not for deployment.

    PYTHONPATH=src python web_tests/stub_server.py 8765
"""
import json
import re
import sys
import tempfile
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from whymoved.service import Service, ServiceError, ServiceSettings

WEB = Path(__file__).resolve().parents[1] / "web"
TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}
ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile(f"^{pattern}$"), fn))
        return fn
    return deco


def make_server(port=0):
    tmp = tempfile.mkdtemp()
    svc = Service(ServiceSettings(db_path=f"{tmp}/t.db", data_dir=f"{tmp}/u"))

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, status, body=None, ctype="application/json"):
            raw = b"" if body is None else (body if isinstance(body, bytes) else
                                            json.dumps(body).encode())
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def user(self):
            auth = self.headers.get("Authorization", "")
            if not auth.lower().startswith("bearer "):
                raise ServiceError(401, "not_authenticated", "sign in again")
            return svc.authenticate(auth[7:].strip())

        def handle_any(self, method):
            path = self.path.split("?")[0]
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            if method == "GET" and path.startswith("/app"):
                rel = path[len("/app"):].lstrip("/") or "index.html"
                f = (WEB / rel).resolve()
                if WEB not in f.parents and f != WEB or not f.is_file():
                    return self.send(404, {"error": {"code": "not_found", "message": "no"}})
                return self.send(200, f.read_bytes(), TYPES.get(f.suffix, "application/octet-stream"))
            try:
                for m, pat, fn in ROUTES:
                    mt = pat.match(path)
                    if m == method and mt:
                        out = fn(self, svc, raw, *[unquote(g) for g in mt.groups()])
                        status, body = out if isinstance(out, tuple) else (200, out)
                        return self.send(status, body)
                self.send(404, {"error": {"code": "not_found", "message": "no such route"}})
            except ServiceError as e:
                self.send(e.status, {"error": {"code": e.code, "message": e.message}})

        do_GET = lambda self: self.handle_any("GET")  # noqa: E731
        do_POST = lambda self: self.handle_any("POST")  # noqa: E731
        do_DELETE = lambda self: self.handle_any("DELETE")  # noqa: E731

    return ThreadingHTTPServer(("127.0.0.1", port), H), svc


def body(raw):
    return json.loads(raw or b"{}")


@route("GET", "/health")
def _health(h, s, raw): return {"status": "ok"}
@route("POST", "/auth/register")
def _reg(h, s, raw): b = body(raw); return 201, s.register(b["email"], b["password"], b.get("workspace_name"))
@route("POST", "/auth/login")
def _login(h, s, raw): b = body(raw); return s.login(b["email"], b["password"])
@route("POST", "/auth/logout")
def _logout(h, s, raw): h.user(); s.logout(h.headers["Authorization"][7:].strip()); return 204, None
@route("GET", "/me")
def _me(h, s, raw): return s.me(h.user())
@route("GET", "/workspaces/([^/]+)/datasets")
def _lds(h, s, raw, w): return s.list_datasets(h.user(), w)
@route("POST", "/workspaces/([^/]+)/demo")
def _demo(h, s, raw, w): return 201, s.create_demo(h.user(), w)


@route("POST", "/workspaces/([^/]+)/datasets")
def _upload(h, s, raw, w):
    msg = BytesParser(policy=default).parsebytes(
        b"Content-Type: " + h.headers["Content-Type"].encode() + b"\r\n\r\n" + raw)
    file, name = None, None
    for part in msg.iter_parts():
        field = part.get_param("name", header="content-disposition")
        if field == "file":
            file = (part.get_filename(), part.get_payload(decode=True))
        elif field == "name":
            name = part.get_content()
    if file is None:
        raise ServiceError(422, "bad_params", "no file")
    return 201, s.upload_dataset(h.user(), w, file[0] or "upload", file[1], name)


@route("GET", "/compare")
def _cmp(h, s, raw):
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(h.path).query)
    return s.compare(h.user(), q.get("a", [""])[0], q.get("b", [""])[0])


@route("GET", "/datasets/([^/]+)/scan")
def _scan(h, s, raw, d): return s.scan(h.user(), d)
@route("POST", "/analyses/([^/]+)/notes")
def _an(h, s, raw, a): b = body(raw); return 201, s.add_note(h.user(), a, b.get("body", ""), bool(b.get("public")))
@route("DELETE", "/analyses/([^/]+)/notes/([^/]+)")
def _dn(h, s, raw, a, n): s.delete_note(h.user(), a, n); return 204, None


@route("GET", "/datasets/([^/]+)")
def _gd(h, s, raw, d): return s.get_dataset(h.user(), d)
@route("DELETE", "/datasets/([^/]+)")
def _dd(h, s, raw, d): s.delete_dataset(h.user(), d); return 204, None
@route("POST", "/datasets/([^/]+)/analyses")
def _ca(h, s, raw, d): return 201, s.create_analysis(h.user(), d, body(raw))
@route("GET", "/datasets/([^/]+)/analyses")
def _la(h, s, raw, d): return s.list_analyses(h.user(), d)
@route("GET", "/analyses/([^/]+)")
def _ga(h, s, raw, a): return s.get_analysis(h.user(), a)
@route("DELETE", "/analyses/([^/]+)")
def _da(h, s, raw, a): s.delete_analysis(h.user(), a); return 204, None
@route("POST", "/analyses/([^/]+)/share")
def _cs(h, s, raw, a): return 201, s.create_share(h.user(), a, body(raw).get("days"))
@route("DELETE", "/analyses/([^/]+)/share")
def _rs(h, s, raw, a): return {"revoked": s.revoke_shares(h.user(), a)}
@route("GET", "/shared/([^/]+)")
def _sh(h, s, raw, t): return s.shared_result(t)


if __name__ == "__main__":
    srv, _ = make_server(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
    print("serving on", srv.server_address)
    srv.serve_forever()
