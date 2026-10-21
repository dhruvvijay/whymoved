"""All application logic. No web framework in here.

Authorization rules (every method that touches a workspace checks them first):
    * you must be a member of the workspace; otherwise it looks like it does not exist (404),
      so ids cannot be probed;
    * only the owner can add members, delete the workspace, or delete other people's items;
    * members can upload, analyse, and share.
Share links are read-only, expire, can be revoked, and expose only the saved result: never the
uploaded rows, the file name, or any account details.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from whymoved.app import security
from whymoved.app.analysis import AnalysisParams, analyze, profile_file
from whymoved.app.config import Settings
from whymoved.app.db import Database
from whymoved.app.errors import (
    Conflict,
    Forbidden,
    Invalid,
    NotFound,
    RateLimited,
    TooLarge,
    Unauthorized,
)
from whymoved.generator import SCENARIO_KINDS, make_scenario

ALLOWED_EXT = {".csv", ".tsv", ".txt", ".xlsx"}
SAMPLE_KINDS = ("single", "double", "mix_only", "budget_miss", "pvm", "cost_rise",
                "share_loss", "none")
_SAMPLE_SETUP = {  # what to analyse for each sample: (mode, metric, extra params)
    "single": ("change", "revenue", {}), "double": ("change", "revenue", {}),
    "mix_only": ("change", "conversion", {}), "none": ("change", "revenue", {}),
    "budget_miss": ("budget", "revenue", {}),
    "pvm": ("pvm", None, {"units_col": "orders", "cost_col": "cost",
                           "bridge_dimensions": ["category"]}),
    "cost_rise": ("pvm", None, {"units_col": "orders", "cost_col": "cost",
                                 "bridge_dimensions": ["category"]}),
    "share_loss": ("market", None, {"market_col": "market_revenue",
                                     "bridge_dimensions": ["region"]}),
}


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class WhyMovedService:
    def __init__(self, settings: Settings | None = None, db: Database | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.settings = settings or Settings()
        self.settings.upload_dir.mkdir(parents=True, exist_ok=True)
        self.db = db or Database(self.settings.db_path)
        self.clock = clock or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------ accounts

    def register(self, email: str, password: str) -> dict:
        email = security.normalize_email(email)
        security.check_password_rules(password, email)
        if self.db.one("SELECT 1 FROM users WHERE email = ?", (email,)):
            raise Conflict("an account with this email already exists")
        uid = security.new_id()
        self.db.execute("INSERT INTO users (id, email, password_hash, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        (uid, email, security.hash_password(password), _iso(self.clock())))
        return {"id": uid, "email": email}

    def login(self, email: str, password: str) -> dict:
        """Returns {"token", "expires_at", "user"}. The error is the same whether the email is
        unknown or the password is wrong, and repeated failures lock the email for a while."""
        try:
            email = security.normalize_email(email)
        except Invalid:
            raise Unauthorized("wrong email or password") from None
        now = self.clock()
        since = _iso(now - timedelta(minutes=self.settings.lockout_minutes))
        failures = self.db.one("SELECT COUNT(*) AS n FROM login_failures WHERE email = ? "
                               "AND at >= ?", (email, since))["n"]
        if failures >= self.settings.max_failed_logins:
            raise RateLimited(f"too many failed attempts; try again in "
                              f"{self.settings.lockout_minutes} minutes")
        row = self.db.one("SELECT id, email, password_hash FROM users WHERE email = ?", (email,))
        ok = security.verify_password(password or "", row["password_hash"] if row
                                      else security.DUMMY_HASH)
        if not row or not ok:
            self.db.execute("INSERT INTO login_failures (email, at) VALUES (?, ?)",
                            (email, _iso(now)))
            raise Unauthorized("wrong email or password")
        self.db.execute("DELETE FROM login_failures WHERE email = ?", (email,))
        token = security.new_token()
        expires = now + timedelta(days=self.settings.session_days)
        self.db.execute("INSERT INTO sessions (token_hash, user_id, created_at, expires_at) "
                        "VALUES (?, ?, ?, ?)",
                        (security.hash_token(token), row["id"], _iso(now), _iso(expires)))
        return {"token": token, "expires_at": _iso(expires),
                "user": {"id": row["id"], "email": row["email"]}}

    def authenticate(self, token: str | None) -> dict:
        if not token:
            raise Unauthorized("sign in first")
        row = self.db.one(
            "SELECT u.id, u.email, s.expires_at FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ?", (security.hash_token(token),))
        if not row or row["expires_at"] <= _iso(self.clock()):
            raise Unauthorized("your session is not valid; sign in again")
        return {"id": row["id"], "email": row["email"]}

    def logout(self, token: str | None) -> None:
        if token:
            self.db.execute("DELETE FROM sessions WHERE token_hash = ?",
                            (security.hash_token(token),))

    # ------------------------------------------------------------------ workspaces

    def create_workspace(self, user: dict, name: str) -> dict:
        name = (name or "").strip()
        if not 1 <= len(name) <= 80:
            raise Invalid("workspace name must be 1 to 80 characters")
        owned = self.db.one("SELECT COUNT(*) AS n FROM workspaces WHERE owner_id = ?",
                            (user["id"],))["n"]
        if owned >= self.settings.max_workspaces_per_user:
            raise Invalid(f"limit of {self.settings.max_workspaces_per_user} workspaces reached")
        wid = security.new_id()
        with self.db.transaction():
            self.db.execute("INSERT INTO workspaces (id, name, owner_id, created_at) "
                            "VALUES (?, ?, ?, ?)", (wid, name, user["id"], _iso(self.clock())))
            self.db.execute("INSERT INTO members (workspace_id, user_id, role) "
                            "VALUES (?, ?, 'owner')", (wid, user["id"]))
        return {"id": wid, "name": name, "role": "owner"}

    def list_workspaces(self, user: dict) -> list[dict]:
        rows = self.db.all(
            "SELECT w.id, w.name, m.role, w.created_at FROM workspaces w "
            "JOIN members m ON m.workspace_id = w.id WHERE m.user_id = ? ORDER BY w.created_at",
            (user["id"],))
        return [dict(r) for r in rows]

    def _role(self, user: dict, workspace_id: str) -> str:
        row = self.db.one("SELECT role FROM members WHERE workspace_id = ? AND user_id = ?",
                          (workspace_id, user["id"]))
        if not row:
            raise NotFound("workspace not found")
        return row["role"]

    def _require_owner(self, user: dict, workspace_id: str) -> None:
        if self._role(user, workspace_id) != "owner":
            raise Forbidden("only the workspace owner can do this")

    def add_member(self, user: dict, workspace_id: str, email: str) -> dict:
        self._require_owner(user, workspace_id)
        email = security.normalize_email(email)
        target = self.db.one("SELECT id FROM users WHERE email = ?", (email,))
        if not target:
            raise NotFound("no account with that email; ask them to register first")
        if self.db.one("SELECT 1 FROM members WHERE workspace_id = ? AND user_id = ?",
                       (workspace_id, target["id"])):
            raise Conflict("already a member")
        self.db.execute("INSERT INTO members (workspace_id, user_id, role) "
                        "VALUES (?, ?, 'member')", (workspace_id, target["id"]))
        return {"email": email, "role": "member"}

    def list_members(self, user: dict, workspace_id: str) -> list[dict]:
        self._role(user, workspace_id)
        rows = self.db.all("SELECT u.email, m.role FROM members m JOIN users u "
                           "ON u.id = m.user_id WHERE m.workspace_id = ? ORDER BY u.email",
                           (workspace_id,))
        return [dict(r) for r in rows]

    def delete_workspace(self, user: dict, workspace_id: str) -> None:
        self._require_owner(user, workspace_id)
        names = [r["stored_name"] for r in self.db.all(
            "SELECT stored_name FROM datasets WHERE workspace_id = ?", (workspace_id,))]
        self.db.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))
        for name in names:
            self._remove_file(workspace_id, name)

    # ------------------------------------------------------------------ datasets

    def _path(self, workspace_id: str, stored_name: str) -> Path:
        return self.settings.upload_dir / workspace_id / stored_name

    def _remove_file(self, workspace_id: str, stored_name: str) -> None:
        try:
            self._path(workspace_id, stored_name).unlink(missing_ok=True)
        except OSError:
            pass

    def _check_zip(self, payload: bytes) -> None:
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as z:
                total = sum(i.file_size for i in z.infolist())
        except zipfile.BadZipFile:
            raise Invalid("this is not a valid .xlsx file") from None
        if total > self.settings.max_uncompressed_bytes:
            raise TooLarge("the workbook expands to too much data")

    def _store_dataset(self, user: dict, workspace_id: str, filename: str, payload: bytes,
                       synthetic: bool = False, extra_profile: dict | None = None) -> dict:
        count = self.db.one("SELECT COUNT(*) AS n FROM datasets WHERE workspace_id = ?",
                            (workspace_id,))["n"]
        if count >= self.settings.max_datasets_per_workspace:
            raise Invalid("this workspace has reached its dataset limit; delete one first")
        safe = re.sub(r"[^\w.\- ]", "_", Path(str(filename).replace("\\", "/")).name)[:120]
        ext = Path(safe).suffix.lower()
        if not safe or ext not in ALLOWED_EXT:
            raise Invalid("upload a .csv, .tsv or .xlsx file")
        if len(payload) == 0:
            raise Invalid("the file is empty")
        if len(payload) > self.settings.max_upload_bytes:
            raise TooLarge(f"the file is larger than {self.settings.max_upload_bytes // 2**20} MB")
        if ext == ".xlsx":
            self._check_zip(payload)
        did = security.new_id()
        stored = f"{did}{ext}"
        folder = self._path(workspace_id, stored).parent
        folder.mkdir(parents=True, exist_ok=True)
        path = self._path(workspace_id, stored)
        path.write_bytes(payload)
        try:
            profile = profile_file(path, safe, self.settings.max_rows)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        profile.update(extra_profile or {})
        self.db.execute(
            "INSERT INTO datasets (id, workspace_id, filename, stored_name, size_bytes, n_rows, "
            "profile_json, synthetic, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (did, workspace_id, safe, stored, len(payload), profile["rows_read"],
             json.dumps(profile), int(synthetic), user["id"], _iso(self.clock())))
        return self._dataset_view(self.db.one("SELECT * FROM datasets WHERE id = ?", (did,)))

    def upload_dataset(self, user: dict, workspace_id: str, filename: str, payload: bytes) -> dict:
        self._role(user, workspace_id)
        return self._store_dataset(user, workspace_id, filename, payload)

    def create_sample(self, user: dict, workspace_id: str, kind: str = "single",
                      seed: int = 7) -> dict:
        self._role(user, workspace_id)
        if kind not in SAMPLE_KINDS or kind not in SCENARIO_KINDS:
            raise Invalid(f"kind must be one of {', '.join(SAMPLE_KINDS)}")
        if not isinstance(seed, int) or not 0 <= seed < 10**9:
            raise Invalid("seed must be a whole number below 1,000,000,000")
        sc = make_scenario(kind, seed)
        payload = sc.data.to_csv(index=False).encode()
        planted = "; ".join(s.label() for s in sc.shocks) or "nothing (no real change planted)"
        mode, metric, extra = _SAMPLE_SETUP[kind]
        extra_profile = {"suggested": {"mode": mode, "metric": metric, **extra},
                         "synthetic_note": f"Synthetic data. Planted: {planted}."}
        return self._store_dataset(user, workspace_id, f"sample_{kind}_{seed}.csv", payload,
                                   synthetic=True, extra_profile=extra_profile)

    @staticmethod
    def _dataset_view(row) -> dict:
        profile = json.loads(row["profile_json"])
        return {"id": row["id"], "workspace_id": row["workspace_id"], "filename": row["filename"],
                "size_bytes": row["size_bytes"], "rows": row["n_rows"],
                "synthetic": bool(row["synthetic"]), "created_at": row["created_at"],
                "profile": profile}

    def _dataset_row(self, user: dict, workspace_id: str, dataset_id: str):
        self._role(user, workspace_id)
        row = self.db.one("SELECT * FROM datasets WHERE id = ? AND workspace_id = ?",
                          (dataset_id, workspace_id))
        if not row:
            raise NotFound("dataset not found")
        return row

    def list_datasets(self, user: dict, workspace_id: str) -> list[dict]:
        self._role(user, workspace_id)
        rows = self.db.all("SELECT * FROM datasets WHERE workspace_id = ? ORDER BY created_at",
                           (workspace_id,))
        return [self._dataset_view(r) for r in rows]

    def get_dataset(self, user: dict, workspace_id: str, dataset_id: str) -> dict:
        return self._dataset_view(self._dataset_row(user, workspace_id, dataset_id))

    def delete_dataset(self, user: dict, workspace_id: str, dataset_id: str) -> None:
        row = self._dataset_row(user, workspace_id, dataset_id)
        if row["created_by"] != user["id"]:
            self._require_owner(user, workspace_id)
        self.db.execute("DELETE FROM datasets WHERE id = ?", (dataset_id,))
        self._remove_file(workspace_id, row["stored_name"])

    # ------------------------------------------------------------------ analyses

    def run_analysis(self, user: dict, workspace_id: str, dataset_id: str,
                     params: dict | None = None, title: str | None = None) -> dict:
        row = self._dataset_row(user, workspace_id, dataset_id)
        params = dict(params or {})
        profile = json.loads(row["profile_json"])
        suggested = profile.get("suggested") or {}
        for key, value in suggested.items():  # a sample dataset supplies sensible defaults
            params.setdefault(key, value)
        allowed = set(AnalysisParams.__dataclass_fields__) - {"extra"}
        unknown = set(params) - allowed
        if unknown:
            raise Invalid(f"unknown option(s): {', '.join(sorted(unknown))}")
        for key in ("period_a", "period_b"):
            if params.get(key) is not None:
                params[key] = tuple(params[key])
        result = analyze(self._path(workspace_id, row["stored_name"]), row["filename"],
                         AnalysisParams(**params), self.settings.max_rows)
        result["synthetic"] = bool(row["synthetic"])
        if row["synthetic"]:
            result["synthetic_note"] = profile.get("synthetic_note", "Synthetic data.")
        # The default title must not contain the file name: share links show the title, and a
        # file name can be sensitive. A title the user typed is theirs to choose.
        default = f"{result['mode'].capitalize()} analysis ({result['data']['period_b'][1]})"
        title = (title or default).strip()[:120]
        aid = security.new_id()
        self.db.execute(
            "INSERT INTO analyses (id, workspace_id, dataset_id, title, params_json, "
            "result_json, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (aid, workspace_id, dataset_id, title, json.dumps(params, default=str),
             json.dumps(result), user["id"], _iso(self.clock())))
        return self.get_analysis(user, workspace_id, aid)

    def _analysis_row(self, user: dict, workspace_id: str, analysis_id: str):
        self._role(user, workspace_id)
        row = self.db.one("SELECT * FROM analyses WHERE id = ? AND workspace_id = ?",
                          (analysis_id, workspace_id))
        if not row:
            raise NotFound("analysis not found")
        return row

    @staticmethod
    def _analysis_view(row, with_result: bool = True) -> dict:
        out = {"id": row["id"], "workspace_id": row["workspace_id"],
               "dataset_id": row["dataset_id"], "title": row["title"],
               "created_at": row["created_at"], "params": json.loads(row["params_json"])}
        if with_result:
            out["result"] = json.loads(row["result_json"])
        return out

    def get_analysis(self, user: dict, workspace_id: str, analysis_id: str) -> dict:
        return self._analysis_view(self._analysis_row(user, workspace_id, analysis_id))

    def list_analyses(self, user: dict, workspace_id: str) -> list[dict]:
        self._role(user, workspace_id)
        rows = self.db.all("SELECT * FROM analyses WHERE workspace_id = ? ORDER BY created_at "
                           "DESC", (workspace_id,))
        return [self._analysis_view(r, with_result=False) for r in rows]

    def delete_analysis(self, user: dict, workspace_id: str, analysis_id: str) -> None:
        row = self._analysis_row(user, workspace_id, analysis_id)
        if row["created_by"] != user["id"]:
            self._require_owner(user, workspace_id)
        self.db.execute("DELETE FROM analyses WHERE id = ?", (analysis_id,))

    # ------------------------------------------------------------------ share links

    def create_share(self, user: dict, workspace_id: str, analysis_id: str,
                     days: int | None = None) -> dict:
        self._analysis_row(user, workspace_id, analysis_id)
        days = self.settings.share_default_days if days is None else days
        if not isinstance(days, int) or not 1 <= days <= self.settings.share_max_days:
            raise Invalid(f"days must be between 1 and {self.settings.share_max_days}")
        token = security.new_token()
        now = self.clock()
        sid = security.new_id()
        expires = _iso(now + timedelta(days=days))
        self.db.execute(
            "INSERT INTO shares (id, analysis_id, token_hash, created_by, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?)",
            (sid, analysis_id, security.hash_token(token), user["id"], _iso(now), expires))
        return {"id": sid, "token": token, "expires_at": expires}

    def list_shares(self, user: dict, workspace_id: str, analysis_id: str) -> list[dict]:
        self._analysis_row(user, workspace_id, analysis_id)
        rows = self.db.all("SELECT id, created_at, expires_at, revoked_at, view_count FROM shares "
                           "WHERE analysis_id = ? ORDER BY created_at", (analysis_id,))
        return [dict(r) for r in rows]

    def revoke_share(self, user: dict, workspace_id: str, analysis_id: str,
                     share_id: str) -> None:
        self._analysis_row(user, workspace_id, analysis_id)
        row = self.db.one("SELECT created_by FROM shares WHERE id = ? AND analysis_id = ?",
                          (share_id, analysis_id))
        if not row:
            raise NotFound("share link not found")
        if row["created_by"] != user["id"]:
            self._require_owner(user, workspace_id)
        self.db.execute("UPDATE shares SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                        (_iso(self.clock()), share_id))

    def get_shared(self, token: str) -> dict:
        """Public, read-only view. One generic error for unknown, expired and revoked links."""
        row = self.db.one(
            "SELECT s.id AS share_id, s.expires_at, s.revoked_at, a.title, a.result_json, "
            "a.created_at FROM shares s JOIN analyses a ON a.id = s.analysis_id "
            "WHERE s.token_hash = ?", (security.hash_token(token or ""),))
        if not row or row["revoked_at"] or row["expires_at"] <= _iso(self.clock()):
            raise NotFound("this link is not valid or has expired")
        self.db.execute("UPDATE shares SET view_count = view_count + 1 WHERE id = ?",
                        (row["share_id"],))
        return {"title": row["title"], "created_at": row["created_at"],
                "expires_at": row["expires_at"], "analysis": json.loads(row["result_json"])}
