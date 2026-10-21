"""SQLite storage. All SQL uses bound parameters (never string formatting of user input).

One connection guarded by a lock keeps it simple and safe for a single-process server. For
Postgres, this class is the only file to replace (same four methods).
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE users (
    id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL);
CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE login_failures (email TEXT NOT NULL, at TEXT NOT NULL);
CREATE INDEX login_failures_email ON login_failures(email);
CREATE TABLE workspaces (
    id TEXT PRIMARY KEY, name TEXT NOT NULL,
    owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, created_at TEXT NOT NULL);
CREATE TABLE members (
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('owner', 'member')),
    PRIMARY KEY (workspace_id, user_id));
CREATE TABLE datasets (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    filename TEXT NOT NULL, stored_name TEXT NOT NULL, size_bytes INTEGER NOT NULL,
    n_rows INTEGER NOT NULL, profile_json TEXT NOT NULL, synthetic INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL);
CREATE TABLE analyses (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    dataset_id TEXT REFERENCES datasets(id) ON DELETE SET NULL,
    title TEXT NOT NULL, params_json TEXT NOT NULL, result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL);
CREATE TABLE shares (
    id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL, revoked_at TEXT, view_count INTEGER NOT NULL DEFAULT 0);
"""


class Database:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()
        self._depth = 0
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                self._conn.executescript(SCHEMA)
                self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self._conn.commit()
            elif version != SCHEMA_VERSION:
                raise RuntimeError(f"database schema version {version} is not supported")

    @contextmanager
    def transaction(self):
        """Group several statements; rolled back if anything inside raises."""
        with self._lock:
            self._depth += 1
            try:
                yield self
            except BaseException:
                self._depth -= 1
                if self._depth == 0:
                    self._conn.rollback()
                raise
            self._depth -= 1
            if self._depth == 0:
                self._conn.commit()

    def execute(self, sql: str, params: Iterable = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            if self._depth == 0:
                self._conn.commit()
            return cur.rowcount

    def one(self, sql: str, params: Iterable = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def all(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
