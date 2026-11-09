"""Monotonic access blocks kept outside memory snapshots, including after a restore."""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .models import utcnow


class AccessControlUnavailable(RuntimeError):
    pass


def source_aliases(source, source_id):
    if source == "microsoft365":
        other = source_id[8:] if source_id.startswith("file:///") else "file:///" + source_id
        return source_id, other
    return source_id, source_id


class AccessLedger:
    def __init__(self, path: Path, *, create=False):
        self.path = Path(path).resolve()
        if not self.path.exists() and not create:
            raise AccessControlUnavailable("Access ledger missing; cached content remains blocked")
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            db = sqlite3.connect(self.path)
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript("""
CREATE TABLE IF NOT EXISTS source_blocks (
 origin TEXT NOT NULL,source TEXT NOT NULL,source_id TEXT NOT NULL,project TEXT NOT NULL,
 reason TEXT NOT NULL,blocked_at TEXT NOT NULL,PRIMARY KEY(origin,source,source_id,project)
);
CREATE TABLE IF NOT EXISTS observation_blocks (
 origin TEXT NOT NULL,observation_id TEXT NOT NULL,reason TEXT NOT NULL,blocked_at TEXT NOT NULL,
 PRIMARY KEY(origin,observation_id)
);
""")
                db.commit()
            finally:
                db.close()
            os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        if not self.path.exists():
            raise AccessControlUnavailable("Access ledger missing; cached content remains blocked")
        try:
            db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True)
        except sqlite3.Error as exc:
            raise AccessControlUnavailable(
                "Access ledger unavailable; content remains blocked"
            ) from exc
        try:
            with db:
                yield db
        finally:
            db.close()

    def block_source(self, origin, source, source_id, project, reason):
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO source_blocks VALUES (?,?,?,?,?,?)",
                (origin, source, source_id, project, reason, utcnow()),
            )

    def block_observation(self, origin, oid, reason):
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO observation_blocks VALUES (?,?,?,?)",
                (origin, oid, reason, utcnow()),
            )

    def source_blocked(self, origin, source, source_id, project):
        with self.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM source_blocks WHERE origin=? AND source=? "
                    "AND source_id IN (?,?) "
                    "AND (project='' OR project=?)",
                    (origin, source, *source_aliases(source, source_id), project),
                ).fetchone()
                is not None
            )

    def observation_blocked(self, origin, oid):
        with self.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM observation_blocks WHERE origin=? AND observation_id=?",
                    (origin, oid),
                ).fetchone()
                is not None
            )
