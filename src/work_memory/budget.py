"""Persistent estimated spend guard, shared across worker processes."""

import math
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo


class SpendingLimitExceeded(RuntimeError):
    pass


class BudgetUnavailable(SpendingLimitExceeded):
    pass


def micros(value: float) -> int:
    if not math.isfinite(value) or value < 0:
        raise ValueError("Spend must be a finite nonnegative amount")
    return int((Decimal(str(value)) * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


class Budget:
    def __init__(
        self,
        path: Path,
        *,
        total_usd: float = 5,
        daily_usd: float = 0.5,
        reservation_usd: float = 0.05,
    ):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker = self.path.with_suffix(self.path.suffix + ".guard")
        if marker.exists() and not self.path.exists():
            raise BudgetUnavailable("Spending authority missing; refusing to reset the budget")
        self._allow_create = not self.path.exists()
        self.reservation = micros(reservation_usd)
        if not self.reservation or not micros(total_usd) or not micros(daily_usd):
            raise ValueError("Budget limits must be positive")
        with self.connect() as db:
            if marker.exists():
                try:
                    if not db.execute("SELECT 1 FROM policy WHERE id=1").fetchone():
                        raise BudgetUnavailable(
                            "Spending policy missing; refusing to reset the budget"
                        )
                    db.execute("SELECT count(*) FROM charges").fetchone()
                except sqlite3.Error as exc:
                    raise BudgetUnavailable(
                        "Spending authority damaged; no cloud call allowed"
                    ) from exc
            db.executescript("""
                CREATE TABLE IF NOT EXISTS policy (
                    id INTEGER PRIMARY KEY CHECK(id=1), total_limit INTEGER NOT NULL,
                    daily_limit INTEGER NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS charges (
                    id TEXT PRIMARY KEY, day TEXT NOT NULL, reserved INTEGER NOT NULL,
                    charged INTEGER, state TEXT NOT NULL, created_at TEXT NOT NULL,
                    model TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS log_directories (path TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS policy_changes (
                    id INTEGER PRIMARY KEY, changed_at TEXT NOT NULL,
                    old_daily_limit INTEGER NOT NULL, new_daily_limit INTEGER NOT NULL,
                    reason TEXT NOT NULL
                );
            """)
            db.execute(
                "INSERT OR IGNORE INTO policy VALUES (1,?,?,?)",
                (micros(total_usd), micros(daily_usd), datetime.now(UTC).isoformat()),
            )
            # Reopening a ledger never changes or replenishes its persisted authorization.
        os.chmod(self.path, 0o600)
        self._allow_create = False
        try:
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w") as stream:
                stream.write(
                    "Persistent spending authority; never recreate from a memory snapshot.\n"
                )

    @contextmanager
    def connect(self):
        mode = "rwc" if self._allow_create else "rw"
        try:
            db = sqlite3.connect(self.path.as_uri() + "?mode=" + mode, timeout=15, uri=True)
        except sqlite3.Error as exc:
            raise BudgetUnavailable(
                "Spending authority unavailable; no cloud call allowed"
            ) from exc
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def day(now: datetime | None = None) -> str:
        return (now or datetime.now(UTC)).astimezone(ZoneInfo("Europe/Rome")).date().isoformat()

    @staticmethod
    def used(db, day: str) -> tuple[int, int]:
        row = db.execute(
            "SELECT coalesce(sum(coalesce(charged,reserved)),0) AS total, "
            "coalesce(sum(CASE WHEN day=? THEN coalesce(charged,reserved) ELSE 0 END),0) AS daily "
            "FROM charges",
            (day,),
        ).fetchone()
        return row["total"], row["daily"]

    def reserve(self, model: str, *, now: datetime | None = None) -> tuple[str, float]:
        day = self.day(now)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            policy = db.execute("SELECT * FROM policy WHERE id=1").fetchone()
            total, daily = self.used(db, day)
            # Reserve a whole bounded call before starting it, including concurrent callers.
            if (
                total + self.reservation > policy["total_limit"]
                or daily + self.reservation > policy["daily_limit"]
            ):
                raise SpendingLimitExceeded("Estimated spend limit reached; no cloud call started")
            rid = str(uuid.uuid4())
            db.execute(
                "INSERT INTO charges VALUES (?,?,?,NULL,'reserved',?,?)",
                (rid, day, self.reservation, (now or datetime.now(UTC)).isoformat(), model),
            )
        return rid, self.reservation / 1_000_000

    def change_daily_limit(self, amount: float, *, reason: str):
        """Explicit authorization change; preserve charges, total cap and original date."""
        limit = micros(amount)
        if not limit or not reason.strip():
            raise ValueError("Positive daily limit and authorization reason required")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT daily_limit FROM policy WHERE id=1").fetchone()[0]
            if old != limit:
                db.execute("UPDATE policy SET daily_limit=? WHERE id=1", (limit,))
                db.execute(
                    "INSERT INTO policy_changes(changed_at,old_daily_limit,new_daily_limit,reason) "
                    "VALUES (?,?,?,?)",
                    (datetime.now(UTC).isoformat(), old, limit, reason.strip()),
                )

    def settle(self, rid: str, cost_usd: float | None):
        with self.connect() as db:
            row = db.execute("SELECT * FROM charges WHERE id=?", (rid,)).fetchone()
            if not row:
                raise ValueError("Unknown budget reservation")
            if row["state"] != "reserved":
                raise ValueError("Budget reservation already settled")
            cost = row["reserved"] if cost_usd is None else micros(cost_usd)
            db.execute(
                "UPDATE charges SET charged=?,state=? WHERE id=?",
                (cost, "unknown_cost" if cost_usd is None else "settled", rid),
            )

    def status(self, *, now: datetime | None = None) -> dict:
        day = self.day(now)
        with self.connect() as db:
            policy = db.execute("SELECT * FROM policy WHERE id=1").fetchone()
            total, daily = self.used(db, day)
            unknown = db.execute("SELECT count(*) FROM charges WHERE state!='settled'").fetchone()[
                0
            ]
        return {
            "currency": "USD",
            "basis": "CLI list-price estimates, not confirmed billing",
            "timezone": "Europe/Rome",
            "day": day,
            "authorized_at": policy["created_at"],
            "total_limit": policy["total_limit"] / 1_000_000,
            "daily_limit": policy["daily_limit"] / 1_000_000,
            "total_accounted": total / 1_000_000,
            "daily_accounted": daily / 1_000_000,
            "total_remaining": max(0, policy["total_limit"] - total) / 1_000_000,
            "daily_remaining": max(0, policy["daily_limit"] - daily) / 1_000_000,
            "reserved_or_unknown_calls": unknown,
            "can_start_call": total + self.reservation <= policy["total_limit"]
            and daily + self.reservation <= policy["daily_limit"],
            "limitations": [
                "A model turn can exceed the CLI per-call stop threshold",
                "Unknown or interrupted calls retain their full reservation",
            ],
        }

    def register_logs(self, directory: Path):
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO log_directories VALUES (?)", (str(directory.resolve()),)
            )

    def cleanup_logs(self) -> dict:
        from .retention import prune_logs

        with self.connect() as db:
            directories = [Path(row[0]) for row in db.execute("SELECT path FROM log_directories")]
        removed = sum(prune_logs(directory)["raw_files_removed"] for directory in directories)
        return {"retention_days": 7, "raw_files_removed": removed}
