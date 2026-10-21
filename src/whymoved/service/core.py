"""Application rules. Every method that touches a user's data takes the authenticated user."""

from __future__ import annotations

import io
import json
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from whymoved.service import jobs, security
from whymoved.service.store import Store, dumps

log = logging.getLogger("whymoved")

ALLOWED_EXTENSIONS = (".csv", ".tsv", ".txt", ".xlsx")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


NOTE_LIMIT = 1500
MAX_NOTES = 50
ANALYSIS_KINDS = ("changes", "statements", "pvm", "market", "trends")


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass
class ServiceSettings:
    db_path: str = "data/whymoved.db"
    data_dir: str = "data/uploads"
    max_upload_bytes: int = 20 * 1024 * 1024
    max_rows: int = 2_000_000
    max_datasets_per_workspace: int = 25
    max_workspace_bytes: int = 200 * 1024 * 1024
    token_days: int = 7
    login_max_failures: int = 5
    login_window_seconds: int = 900


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).isoformat(
        timespec="seconds")


def _clean_text(text: str, limit: int) -> str:
    return _CONTROL.sub("", text).strip()[:limit]


class Service:
    def __init__(self, settings: ServiceSettings | None = None):
        self.s = settings or ServiceSettings()
        self.store = Store(self.s.db_path)
        self.data_dir = Path(self.s.data_dir)
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ accounts
    def _issue_token(self, uid: str) -> str:
        token = security.new_token()
        self.store.add_token(security.token_hash(token), uid,
                             time.time() + self.s.token_days * 86400)
        return token

    def register(self, email: str, password: str, workspace_name: str | None = None) -> dict:
        email = security.normalise_email(email)
        if not security.valid_email(email):
            raise ServiceError(422, "invalid_email", "that does not look like an email address")
        problem = security.password_problem(password)
        if problem:
            raise ServiceError(422, "weak_password", problem)
        uid = security.new_id()
        if not self.store.create_user(uid, email, security.hash_password(password)):
            raise ServiceError(409, "email_taken", "an account with this email already exists")
        wid = security.new_id()
        name = _clean_text(workspace_name or "", 80) or f"{email.split('@')[0]}'s workspace"
        self.store.create_workspace(wid, name, uid)
        return {"token": self._issue_token(uid), "user": {"id": uid, "email": email},
                "workspaces": self.store.workspaces_of(uid)}

    def login(self, email: str, password: str) -> dict:
        email = security.normalise_email(email)
        now = time.time()
        with self._lock:
            window = self.s.login_window_seconds
            recent = [t for t in self._failures.get(email, []) if now - t < window]
            self._failures[email] = recent
            if len(recent) >= self.s.login_max_failures:
                raise ServiceError(429, "too_many_attempts",
                                   "too many failed logins; try again in a few minutes")
        row = self.store.user_by_email(email)
        ok = security.verify_password(password, row["pw_hash"] if row else security.DUMMY_HASH)
        if not (row and ok):
            with self._lock:
                self._failures.setdefault(email, []).append(now)
            raise ServiceError(401, "bad_credentials", "wrong email or password")
        with self._lock:
            self._failures.pop(email, None)
        return {"token": self._issue_token(row["id"]),
                "user": {"id": row["id"], "email": row["email"]},
                "workspaces": self.store.workspaces_of(row["id"])}

    def authenticate(self, token: str | None) -> dict:
        row = self.store.user_for_token(security.token_hash(token)) if token else None
        if row is None:
            raise ServiceError(401, "not_authenticated", "sign in again")
        return {"id": row["id"], "email": row["email"]}

    def logout(self, token: str) -> None:
        self.store.delete_token(security.token_hash(token))

    def me(self, user: dict) -> dict:
        return {"user": user, "workspaces": self.store.workspaces_of(user["id"])}

    # ------------------------------------------------------------------ workspaces
    def _role(self, user: dict, wid: str) -> str:
        role = self.store.role(wid, user["id"])
        if role is None:  # same answer as "does not exist", so ids cannot be probed
            raise ServiceError(404, "not_found", "workspace not found")
        return role

    def create_workspace(self, user: dict, name: str) -> dict:
        name = _clean_text(name, 80)
        if not name:
            raise ServiceError(422, "invalid_name", "give the workspace a name")
        wid = security.new_id()
        self.store.create_workspace(wid, name, user["id"])
        return {"id": wid, "name": name, "role": "owner"}

    def members(self, user: dict, wid: str) -> list[dict]:
        self._role(user, wid)
        return self.store.members_of(wid)

    def add_member(self, user: dict, wid: str, email: str) -> list[dict]:
        if self._role(user, wid) != "owner":
            raise ServiceError(403, "forbidden", "only the owner can add people")
        target = self.store.user_by_email(security.normalise_email(email))
        if target is None:
            raise ServiceError(404, "no_such_user", "no account with that email; ask them to "
                                                    "register first")
        if not self.store.add_member(wid, target["id"], "member"):
            raise ServiceError(409, "already_member", "already in this workspace")
        return self.store.members_of(wid)

    # ------------------------------------------------------------------ datasets
    @staticmethod
    def _dataset_out(row) -> dict:
        return {"id": row["id"], "workspace_id": row["workspace_id"], "name": row["name"],
                "filename": row["filename"], "size_bytes": row["size_bytes"],
                "created_at": _iso(row["created_at"]), "meta": json.loads(row["meta_json"])}

    def _dataset_row(self, user: dict, did: str):
        row = self.store.dataset(did)
        if row is None or self.store.role(row["workspace_id"], user["id"]) is None:
            raise ServiceError(404, "not_found", "dataset not found")
        return row

    def upload_dataset(self, user: dict, wid: str, filename: str, data: bytes,
                       name: str | None = None, extra_meta: dict | None = None) -> dict:
        self._role(user, wid)
        filename = _clean_text(Path(filename.replace("\\", "/")).name, 200)
        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise ServiceError(415, "unsupported_type",
                               f"upload one of: {', '.join(ALLOWED_EXTENSIONS)}")
        if not data:
            raise ServiceError(422, "empty_file", "the file is empty")
        if len(data) > self.s.max_upload_bytes:
            raise ServiceError(413, "too_large",
                               f"the file is larger than {self.s.max_upload_bytes // 2**20} MB")
        count, used = self.store.workspace_usage(wid)
        if count >= self.s.max_datasets_per_workspace:
            raise ServiceError(409, "workspace_full", "this workspace has too many datasets; "
                                                      "delete one first")
        if used + len(data) > self.s.max_workspace_bytes:
            raise ServiceError(409, "workspace_full", "this workspace is out of storage; "
                                                      "delete a dataset first")
        try:
            meta = jobs.inspect_upload(data, filename, self.s.max_rows)
        except ValueError as exc:  # DataError subclasses ValueError; message is user-safe
            raise ServiceError(422, "bad_file", str(exc)) from exc
        except Exception as exc:  # unreadable, corrupt or hostile file
            log.warning("could not read upload: %r", exc)
            raise ServiceError(422, "bad_file", "the file could not be read as a table") from exc
        meta.update(extra_meta or {})
        did, stored = security.new_id(), f"{security.new_id()}{ext}"
        folder = self.data_dir / wid
        folder.mkdir(parents=True, exist_ok=True)
        (folder / stored).write_bytes(data)
        row = {"id": did, "workspace_id": wid,
               "name": _clean_text(name or "", 100) or Path(filename).stem[:100] or "dataset",
               "filename": filename, "stored_name": stored, "size_bytes": len(data),
               "meta_json": dumps(meta), "created_by": user["id"], "created_at": time.time()}
        self.store.add_dataset(row)
        return self._dataset_out(self.store.dataset(did))

    def list_datasets(self, user: dict, wid: str) -> list[dict]:
        self._role(user, wid)
        return [self._dataset_out(r) for r in self.store.datasets_in(wid)]

    def get_dataset(self, user: dict, did: str) -> dict:
        return self._dataset_out(self._dataset_row(user, did))

    def delete_dataset(self, user: dict, did: str) -> None:
        row = self._dataset_row(user, did)
        if not self._may_manage(user, row):
            raise ServiceError(403, "forbidden", "only the uploader or the owner can delete this")
        self.store.delete_dataset(did)
        (self.data_dir / row["workspace_id"] / row["stored_name"]).unlink(missing_ok=True)

    def create_demo(self, user: dict, wid: str, seed: int | None = None) -> dict:
        """Add a synthetic sample dataset (with a planted answer) to the workspace."""
        from whymoved.generator import make_scenario
        seed = secrets.randbelow(9000) + 100 if seed is None else seed
        sc = make_scenario("single", seed)
        buffer = io.StringIO()
        sc.data.to_csv(buffer, index=False)
        planted = "; ".join(s.label() for s in sc.shocks)
        return self.upload_dataset(
            user, wid, "demo_sales_synthetic.csv", buffer.getvalue().encode("utf-8"),
            name="Demo sales (synthetic)", extra_meta={"synthetic": True, "planted": planted})

    # ------------------------------------------------------------------ analyses
    @staticmethod
    def _params(raw: dict) -> dict:
        out: dict = {}
        if raw.get("metric") is not None:
            out["metric"] = _clean_text(str(raw["metric"]), 200)
        for key in ("period_a", "period_b"):
            if raw.get(key) is not None:
                out[key] = raw[key]
        kind = raw.get("kind") or "changes"
        if kind not in ANALYSIS_KINDS:
            raise ServiceError(422, "bad_params",
                               f"kind must be one of: {', '.join(ANALYSIS_KINDS)}")
        out["kind"] = kind
        for key in ("revenue", "units", "cost", "market", "model", "from_period", "to_period"):
            if raw.get(key) not in (None, ""):
                out[key] = _clean_text(str(raw[key]), 200)
        if isinstance(raw.get("financial"), bool):
            out["financial"] = raw["financial"]
        mapping = raw.get("mapping")
        if mapping is not None:
            if not isinstance(mapping, dict) or len(mapping) > 40:
                raise ServiceError(422, "bad_params", "mapping must be a short list of pairs")
            out["mapping"] = {_clean_text(str(k), 60): _clean_text(str(v), 200)
                              for k, v in mapping.items() if v not in (None, "")}
        out["mode"] = raw.get("mode") or "previous"
        out["currency"] = _clean_text(str(raw.get("currency") or ""), 8)
        out["indian"] = bool(raw.get("indian"))
        dims = raw.get("dimensions")
        if dims:
            if not isinstance(dims, list) or len(dims) > 10:
                raise ServiceError(422, "bad_params", "dimensions must be a short list of columns")
            out["dimensions"] = [str(d) for d in dims]
        return out

    def _analysis_out(self, row, full: bool = True, viewer_id: str | None = None) -> dict:
        out = {"id": row["id"], "dataset_id": row["dataset_id"], "status": row["status"],
               "created_at": _iso(row["created_at"]), "params": json.loads(row["params_json"]),
               "shared": self.store.active_share_count(row["id"]) > 0}
        if full:
            out["result"] = json.loads(row["result_json"]) if row["result_json"] else None
            out["notes"] = [self._note_out(n, n["author_id"] == viewer_id)
                            for n in self.store.notes_of(row["id"])] if viewer_id else []
        else:
            result = json.loads(row["result_json"]) if row["result_json"] else {}
            keys = ("kind", "metric", "verdict", "total_a", "total_b")
            out["headline"] = {k: result.get(k) for k in keys}
        return out

    def create_analysis(self, user: dict, did: str, raw_params: dict) -> dict:
        ds = self._dataset_row(user, did)
        params = self._params(raw_params)
        is_statement = json.loads(ds["meta_json"]).get("kind") == "statements"
        if is_statement != (params["kind"] == "statements"):
            raise ServiceError(422, "bad_params", (
                "this file is a financial statement: use the statement analysis" if is_statement
                else "the statement analysis needs a financial-statement file"))
        try:
            result = jobs.run_analysis(self.data_dir / ds["workspace_id"] / ds["stored_name"],
                                       ds["filename"], params)
        except ValueError as exc:
            raise ServiceError(422, "cannot_analyse", str(exc)) from exc
        except Exception as exc:
            log.exception("analysis failed: %r", exc)
            raise ServiceError(500, "analysis_failed", "the analysis failed unexpectedly") from exc
        aid = security.new_id()
        self.store.add_analysis({
            "id": aid, "dataset_id": did, "workspace_id": ds["workspace_id"],
            "params_json": dumps(params), "result_json": dumps(result), "status": "done",
            "error": None, "created_by": user["id"], "created_at": time.time()})
        return self._analysis_out(self.store.analysis(aid), viewer_id=user["id"])

    def compare(self, user: dict, did_a: str, did_b: str) -> dict:
        """Line two financial-statement datasets up side by side (not stored)."""
        from whymoved.service.analyses import run_statements
        from whymoved.watchlist import compare_statements
        if did_a == did_b:
            raise ServiceError(422, "bad_params", "choose two different datasets")
        results, names = [], []
        for did in (did_a, did_b):
            ds = self._dataset_row(user, did)
            if json.loads(ds["meta_json"]).get("kind") != "statements":
                raise ServiceError(422, "bad_params", "comparison needs two financial-statement "
                                                      "files")
            try:
                results.append(run_statements(
                    self.data_dir / ds["workspace_id"] / ds["stored_name"], ds["filename"], {}))
            except ValueError as exc:
                raise ServiceError(422, "cannot_analyse", str(exc)) from exc
            names.append(results[-1].get("company") or ds["name"])
        if names[0] == names[1]:
            names = [self._dataset_row(user, d)["name"] for d in (did_a, did_b)]
        from whymoved.service.jobs import clean_json
        return clean_json(compare_statements(results[0], results[1], (names[0], names[1])))

    def _analysis_row(self, user: dict, aid: str):
        row = self.store.analysis(aid)
        if row is None or self.store.role(row["workspace_id"], user["id"]) is None:
            raise ServiceError(404, "not_found", "analysis not found")
        return row

    def get_analysis(self, user: dict, aid: str) -> dict:
        return self._analysis_out(self._analysis_row(user, aid), viewer_id=user["id"])

    def list_analyses(self, user: dict, did: str) -> list[dict]:
        self._dataset_row(user, did)
        return [self._analysis_out(r, full=False) for r in self.store.analyses_of(did)]

    def _may_manage(self, user: dict, row) -> bool:
        return (row["created_by"] == user["id"]
                or self.store.role(row["workspace_id"], user["id"]) == "owner")

    def delete_analysis(self, user: dict, aid: str) -> None:
        row = self._analysis_row(user, aid)
        if not self._may_manage(user, row):
            raise ServiceError(403, "forbidden", "only its creator or the owner can delete this")
        self.store.delete_analysis(aid)

    # ------------------------------------------------------------------ notes
    # The tool says where a metric moved. A note is where a person writes down why, in their own
    # words. Notes are labelled as written by a person and are private unless made public.
    @staticmethod
    def _note_out(row, mine: bool | None = None) -> dict:
        out = {"id": row["id"], "author": row["author"], "body": row["body"],
               "public": bool(row["public"]), "created_at": _iso(row["created_at"])}
        if mine is not None:
            out["mine"] = mine
        return out

    def add_note(self, user: dict, aid: str, body: str, public: bool = False) -> dict:
        row = self._analysis_row(user, aid)
        text = "\n".join(_CONTROL.sub("", line).strip() for line in str(body).splitlines())
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if not text:
            raise ServiceError(422, "bad_params", "write something first")
        if len(text) > NOTE_LIMIT:
            raise ServiceError(422, "bad_params", f"keep a note under {NOTE_LIMIT} characters")
        if len(self.store.notes_of(aid)) >= MAX_NOTES:
            raise ServiceError(409, "too_many", "this analysis has as many notes as it can hold")
        nid = security.new_id()
        self.store.add_note({"id": nid, "analysis_id": row["id"], "author_id": user["id"],
                             "author": user["email"].split("@")[0][:40], "body": text,
                             "public": int(bool(public)), "created_at": time.time()})
        return self._note_out(self.store.note(nid), True)

    def delete_note(self, user: dict, aid: str, nid: str) -> None:
        row = self._analysis_row(user, aid)
        note = self.store.note(nid)
        if note is None or note["analysis_id"] != row["id"]:
            raise ServiceError(404, "not_found", "note not found")
        if note["author_id"] != user["id"] and not self._may_manage(user, row):
            raise ServiceError(403, "forbidden", "only the author or the owner can delete this")
        self.store.delete_note(nid)

    # ------------------------------------------------------------------ scan
    def scan(self, user: dict, did: str) -> dict:
        """Run the plain "what moved" analysis on every number column over the file's default
        periods and rank them by how much they changed. Not stored."""
        ds = self._dataset_row(user, did)
        meta = json.loads(ds["meta_json"])
        if meta.get("kind") == "statements":
            raise ServiceError(422, "bad_params", "scanning is for dated tables, not statements")
        path = self.data_dir / ds["workspace_id"] / ds["stored_name"]
        try:
            return jobs.scan_metrics(path, ds["filename"], meta)
        except ValueError as exc:
            raise ServiceError(422, "cannot_analyse", str(exc)) from exc
        except Exception as exc:
            log.exception("scan failed: %r", exc)
            raise ServiceError(500, "analysis_failed", "the scan failed unexpectedly") from exc

    # ------------------------------------------------------------------ sharing
    def create_share(self, user: dict, aid: str, days: int | None = None) -> dict:
        row = self._analysis_row(user, aid)
        if not self._may_manage(user, row):
            raise ServiceError(403, "forbidden", "only its creator or the owner can share this")
        if days is not None and not 1 <= days <= 365:
            raise ServiceError(422, "bad_params", "days must be between 1 and 365")
        token = security.new_token()
        expires = time.time() + days * 86400 if days else None
        self.store.add_share(security.token_hash(token), aid, user["id"], expires)
        return {"token": token, "path": f"/shared/{token}", "expires_at": _iso(expires)}

    def revoke_shares(self, user: dict, aid: str) -> int:
        row = self._analysis_row(user, aid)
        if not self._may_manage(user, row):
            raise ServiceError(403, "forbidden", "only its creator or the owner can do this")
        return self.store.revoke_shares(aid)

    def shared_result(self, token: str) -> dict:
        """What a share link shows: the finished analysis and nothing else (no data, no names)."""
        share = self.store.share(security.token_hash(token)) if token else None
        gone = ServiceError(404, "not_found", "this link does not exist or has expired")
        if share is None or share["revoked"]:
            raise gone
        if share["expires_at"] and share["expires_at"] < time.time():
            raise gone
        row = self.store.analysis(share["analysis_id"])
        if row is None or not row["result_json"]:
            raise gone
        notes = [{"body": n["body"], "created_at": _iso(n["created_at"])}
                 for n in self.store.notes_of(row["id"]) if n["public"]]
        return {"created_at": _iso(row["created_at"]), "result": json.loads(row["result_json"]),
                "notes": notes}
