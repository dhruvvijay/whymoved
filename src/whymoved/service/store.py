"""SQLite storage (standard library only). One short connection per call, WAL mode.

Swapping to Postgres later means re-implementing this one class; nothing else touches SQL.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL,
    created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS members (
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL, PRIMARY KEY (workspace_id, user_id));
CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name TEXT NOT NULL, filename TEXT NOT NULL, stored_name TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    meta_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS analyses (
    id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL, params_json TEXT NOT NULL, result_json TEXT, status TEXT NOT NULL,
    error TEXT, created_by TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS shares (
    token_hash TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
    created_by TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL,
    revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY, analysis_id TEXT NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
    author_id TEXT NOT NULL, author TEXT NOT NULL, body TEXT NOT NULL,
    public INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_notes_analysis ON notes(analysis_id);
CREATE INDEX IF NOT EXISTS idx_datasets_ws ON datasets(workspace_id);
CREATE INDEX IF NOT EXISTS idx_analyses_ds ON analyses(dataset_id);
CREATE INDEX IF NOT EXISTS idx_shares_analysis ON shares(analysis_id);
"""


class Store:
    def __init__(self, db_path: str | Path):
        self.path = str(db_path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._memory = None
        if self.path == ":memory:":  # keep one shared connection alive for tests
            self._memory = sqlite3.connect(":memory:", check_same_thread=False)
            self._memory.row_factory = sqlite3.Row
            self._memory.execute("PRAGMA foreign_keys=ON")
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        if self._memory is not None:
            yield self._memory
            self._memory.commit()
            return
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ users and tokens
    def create_user(self, uid: str, email: str, pw_hash: str) -> bool:
        try:
            with self._conn() as c:
                c.execute("INSERT INTO users VALUES (?,?,?,?)", (uid, email, pw_hash, time.time()))
            return True
        except sqlite3.IntegrityError:
            return False

    def user_by_email(self, email: str):
        with self._conn() as c:
            return c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()

    def user_by_id(self, uid: str):
        with self._conn() as c:
            return c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

    def add_token(self, thash: str, uid: str, expires_at: float) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO tokens VALUES (?,?,?)", (thash, uid, expires_at))
            c.execute("DELETE FROM tokens WHERE expires_at < ?", (time.time(),))

    def user_for_token(self, thash: str):
        with self._conn() as c:
            return c.execute(
                "SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id "
                "WHERE t.token_hash=? AND t.expires_at>?", (thash, time.time())).fetchone()

    def delete_token(self, thash: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM tokens WHERE token_hash=?", (thash,))

    # ------------------------------------------------------------------ workspaces
    def create_workspace(self, wid: str, name: str, owner_id: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO workspaces VALUES (?,?,?)", (wid, name, time.time()))
            c.execute("INSERT INTO members VALUES (?,?,?)", (wid, owner_id, "owner"))

    def role(self, wid: str, uid: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT role FROM members WHERE workspace_id=? AND user_id=?",
                            (wid, uid)).fetchone()
        return row["role"] if row else None

    def workspaces_of(self, uid: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT w.id, w.name, m.role FROM workspaces w "
                "JOIN members m ON m.workspace_id=w.id "
                "WHERE m.user_id=? ORDER BY w.created_at", (uid,)).fetchall()
        return [dict(r) for r in rows]

    def members_of(self, wid: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT u.email, m.role FROM members m JOIN users u ON u.id=m.user_id "
                "WHERE m.workspace_id=? ORDER BY u.email", (wid,)).fetchall()
        return [dict(r) for r in rows]

    def add_member(self, wid: str, uid: str, role: str) -> bool:
        try:
            with self._conn() as c:
                c.execute("INSERT INTO members VALUES (?,?,?)", (wid, uid, role))
            return True
        except sqlite3.IntegrityError:
            return False

    # ------------------------------------------------------------------ datasets
    def add_dataset(self, row: dict) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO datasets VALUES (:id,:workspace_id,:name,:filename,:stored_name,"
                      ":size_bytes,:meta_json,:created_by,:created_at)", row)

    def dataset(self, did: str):
        with self._conn() as c:
            return c.execute("SELECT * FROM datasets WHERE id=?", (did,)).fetchone()

    def datasets_in(self, wid: str) -> list:
        with self._conn() as c:
            return c.execute("SELECT * FROM datasets WHERE workspace_id=? ORDER BY created_at DESC",
                             (wid,)).fetchall()

    def delete_dataset(self, did: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM datasets WHERE id=?", (did,))

    def workspace_usage(self, wid: str) -> tuple[int, int]:
        with self._conn() as c:
            row = c.execute("SELECT COUNT(*) n, COALESCE(SUM(size_bytes),0) b FROM datasets "
                            "WHERE workspace_id=?", (wid,)).fetchone()
        return int(row["n"]), int(row["b"])

    # ------------------------------------------------------------------ analyses
    def add_analysis(self, row: dict) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO analyses VALUES (:id,:dataset_id,:workspace_id,:params_json,"
                      ":result_json,:status,:error,:created_by,:created_at)", row)

    def analysis(self, aid: str):
        with self._conn() as c:
            return c.execute("SELECT * FROM analyses WHERE id=?", (aid,)).fetchone()

    def analyses_of(self, did: str) -> list:
        with self._conn() as c:
            return c.execute("SELECT * FROM analyses WHERE dataset_id=? ORDER BY created_at DESC",
                             (did,)).fetchall()

    def delete_analysis(self, aid: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM analyses WHERE id=?", (aid,))

    # ------------------------------------------------------------------ notes
    def add_note(self, row: dict) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO notes VALUES (:id,:analysis_id,:author_id,:author,:body,"
                      ":public,:created_at)", row)

    def notes_of(self, aid: str) -> list:
        with self._conn() as c:
            return c.execute("SELECT * FROM notes WHERE analysis_id=? ORDER BY created_at",
                             (aid,)).fetchall()

    def note(self, nid: str):
        with self._conn() as c:
            return c.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()

    def delete_note(self, nid: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM notes WHERE id=?", (nid,))

    # ------------------------------------------------------------------ shares
    def add_share(self, thash: str, aid: str, uid: str, expires_at: float | None) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO shares VALUES (?,?,?,?,?,0)",
                      (thash, aid, uid, time.time(), expires_at))

    def share(self, thash: str):
        with self._conn() as c:
            return c.execute("SELECT * FROM shares WHERE token_hash=?", (thash,)).fetchone()

    def revoke_shares(self, aid: str) -> int:
        with self._conn() as c:
            return c.execute("UPDATE shares SET revoked=1 WHERE analysis_id=? AND revoked=0",
                             (aid,)).rowcount

    def active_share_count(self, aid: str) -> int:
        with self._conn() as c:
            row = c.execute("SELECT COUNT(*) n FROM shares WHERE analysis_id=? AND revoked=0 AND "
                            "(expires_at IS NULL OR expires_at>?)", (aid, time.time())).fetchone()
        return int(row["n"])


def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), allow_nan=False)
