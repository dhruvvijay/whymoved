"""Settings. Everything has a safe default; override with environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("data/app")
    max_upload_bytes: int = 25 * 1024 * 1024
    max_uncompressed_bytes: int = 250 * 1024 * 1024   # guard for .xlsx (a zip file)
    max_rows: int = 1_000_000
    session_days: int = 14
    share_default_days: int = 30
    share_max_days: int = 365
    max_failed_logins: int = 5
    lockout_minutes: int = 15
    max_workspaces_per_user: int = 20
    max_datasets_per_workspace: int = 50
    cors_origins: tuple[str, ...] = ()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "whymoved.sqlite3"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        origins = tuple(o.strip() for o in env.get("WHYMOVED_CORS_ORIGINS", "").split(",")
                        if o.strip())
        return cls(
            data_dir=Path(env.get("WHYMOVED_DATA_DIR", "data/app")),
            max_upload_bytes=int(float(env.get("WHYMOVED_MAX_UPLOAD_MB", "25")) * 1024 * 1024),
            max_rows=int(env.get("WHYMOVED_MAX_ROWS", "1000000")),
            cors_origins=origins,
        )
