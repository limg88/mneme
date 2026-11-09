"""Raw run-log retention. Memory, QA annotations and cost ledgers are never pruned."""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

RUN_FILE = re.compile(
    r"^(?:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|cli-probe-\d{8}T\d+Z)"
    r"\.(jsonl|stderr\.txt|meta\.json|materialized\.json)$"
)


def prune_logs(directory: Path, *, now: datetime | None = None) -> dict:
    directory = Path(directory)
    cutoff = (now or datetime.now(UTC)) - timedelta(days=7)
    removed = 0
    if directory.is_symlink():
        raise ValueError("Refusing to prune a symlinked log directory")
    if directory.exists():
        for path in directory.iterdir():
            try:
                if (
                    RUN_FILE.fullmatch(path.name)
                    and not path.is_symlink()
                    and path.is_file()
                    and path.stat().st_mtime < cutoff.timestamp()
                ):
                    path.unlink()
                    removed += 1
            except FileNotFoundError:
                continue  # Another worker may already have removed this expired log.
    return {
        "retention_days": 7,
        "raw_files_removed": removed,
        "memory_retention": "No automatic expiry during pilot",
    }
