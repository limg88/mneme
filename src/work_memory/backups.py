"""Same-volume snapshots. Access and spending ledgers are deliberately never rolled back."""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from contextlib import closing
from pathlib import Path

from .models import utcnow
from .store import Store


def verify_storage(directory: Path) -> dict:
    if sys.platform != "darwin" or directory.stat().st_dev != Path.home().stat().st_dev:
        raise RuntimeError("Backup storage must be the verified FileVault-protected home volume")
    result = subprocess.run(
        ["/usr/bin/fdesetup", "status"], capture_output=True, text=True, timeout=10
    )
    if result.returncode or result.stdout.strip() != "FileVault is On.":
        raise RuntimeError("FileVault protection could not be verified; no snapshot created")
    return {"filevault": "on", "verified_at": utcnow(), "application_encryption": False}


def file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def backup(store, *, verifier=verify_storage):
    proof = verifier(store.directory)
    root = store.directory.parent / "backups"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.stat().st_dev != store.directory.stat().st_dev:
        raise RuntimeError("Snapshot destination is not on the verified source volume")
    destination = root / str(uuid.uuid4())
    destination.mkdir(mode=0o700)
    database = destination / "memory.sqlite3"
    target = sqlite3.connect(database)
    try:
        with store.connect() as source:
            source.backup(target)
        if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Snapshot integrity check failed")
    finally:
        target.close()
    os.chmod(database, 0o600)
    manifest = {
        "created_at": utcnow(),
        "origin": store.origin,
        "sha256": file_digest(database),
        "storage": proof,
        "access_ledger": str(store.access_ledger.path),
        "limitations": [
            "Same-Mac snapshot; not an off-device disaster recovery copy",
            "Access and spending authorities are not included or restored",
            "Restore requires the live access ledger; no automatic bypass",
        ],
    }
    path = destination / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2))
    os.chmod(path, 0o600)
    store.set_runtime_policy("storage_encryption", proof)
    return {"snapshot": str(destination), "integrity": "ok", "cloud_calls": 0}


def restore(store, snapshot: Path, destination: Path, *, verifier=verify_storage) -> dict:
    proof = verifier(store.directory)
    snapshot = Path(snapshot).resolve(strict=True)
    database = snapshot / "memory.sqlite3"
    manifest = json.loads((snapshot / "manifest.json").read_text())
    if database.is_symlink() or manifest.get("origin") != store.origin:
        raise ValueError("Snapshot does not belong to this memory authority")
    if file_digest(database) != manifest.get("sha256"):
        raise ValueError("Snapshot checksum mismatch")
    if not store.access_ledger.path.exists():
        raise RuntimeError("Live access authority unavailable; snapshot cannot be unlocked")
    current_budget = store.runtime_policy("budget_file")
    if current_budget and not Path(current_budget).exists():
        raise RuntimeError("Spending ledger missing; cannot restore or reset the budget")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        authority = json.loads(
            db.execute("SELECT value FROM runtime_policy WHERE key='access_control'").fetchone()[0]
        )
        if authority != {"origin": store.origin, "path": str(store.access_ledger.path)}:
            raise ValueError("Snapshot access authority mismatch")
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError("Restore requires a new directory; existing data is never overwritten")
    destination = destination.resolve()
    if destination.parent.stat().st_dev != store.directory.stat().st_dev:
        raise ValueError("Restore must remain on the same verified volume")
    destination.mkdir(mode=0o700)
    shutil.copy2(database, destination / "memory.sqlite3")
    os.chmod(destination / "memory.sqlite3", 0o600)
    recovered = Store(destination)
    if current_budget:
        recovered.set_runtime_policy("budget_file", current_budget)
    recovered.set_runtime_policy(
        "restore_state",
        {"snapshot_at": manifest["created_at"], "restored_at": utcnow(), "requires_resync": True},
    )
    recovered.set_runtime_policy("storage_encryption", proof)
    recovered.token()  # New credential; original tokens and account credentials are never copied.
    return {
        "restored_directory": str(destination),
        "access_blocks": "live authority enforced",
        "budget": "live ledger preserved",
        "requires_resync": True,
        "cloud_calls": 0,
    }


def catch_up_from_live(recovered: Store, live: Store, *, verifier=verify_storage):
    """Refresh an unused restored copy from current live memory, never provider freshness."""
    state = recovered.runtime_policy("restore_state")
    if not state or not state.get("requires_resync"):
        raise ValueError("Destination must be a restored memory awaiting synchronization")
    if recovered.path == live.path or recovered.origin != live.origin:
        raise ValueError("Catch-up requires a distinct copy of the same memory origin")
    if recovered.access_ledger.path != live.access_ledger.path:
        raise ValueError("Access authorities must match")
    if recovered.runtime_policy("budget_file") != live.runtime_policy("budget_file"):
        raise ValueError("Spending authorities must match")
    if (live.runtime_policy("restore_state") or {}).get("requires_resync"):
        raise ValueError("Source must be live memory, not another pending restore")
    verifier(recovered.directory)
    verifier(live.directory)
    with recovered.connect() as db:
        progress = db.execute(
            "SELECT 1 FROM observations WHERE observed_at>=? UNION ALL "
            "SELECT 1 FROM facts WHERE known_from>=? UNION ALL "
            "SELECT 1 FROM sync_runs WHERE started_at>=? LIMIT 1",
            (state["restored_at"],) * 3,
        ).fetchone()
        if progress:
            raise ValueError("Restored copy has new work; refusing to overwrite it")
    with live.connect() as source:
        if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Live memory integrity check failed")
        with closing(sqlite3.connect(recovered.path, timeout=15)) as destination:
            source.backup(destination)
    try:
        with recovered.connect() as db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Caught-up memory integrity check failed")
        recovered.set_runtime_policy(
            "restore_state",
            {
                **state,
                "requires_resync": False,
                "caught_up_at": utcnow(),
                "validation": "current_live_memory_copy",
                "live_directory": str(live.directory),
                "limitations": [
                    "Provider freshness and collection gaps are inherited from live memory"
                ],
            },
        )
    except Exception:
        recovered.set_runtime_policy("restore_state", state)
        raise
    return {
        "state": "caught_up",
        "requires_resync": False,
        "access_blocks": "live authority enforced",
        "budget": "live ledger preserved",
        "api_token": "unchanged",
        "freshness": "same as live memory; no independent provider revalidation",
        "cloud_calls": 0,
    }
