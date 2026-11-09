"""Bounded local document reading and explicitly selected folder collection."""

import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

from .models import Observation, utcnow

MAX_FILE_BYTES = 20_000_000
MAX_TEXT_CHARS = 2_000_000
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class LocalFormatError(ValueError):
    pass


def read_document(path: Path) -> tuple[str, dict]:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise LocalFormatError("Document exceeds the local file size limit")
    suffix = path.suffix.lower()
    limitations = []
    if suffix in {".txt", ".md"}:
        text = path.read_text(encoding="utf-8-sig")
    elif suffix == ".docx":
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > 25_000_000:
                raise LocalFormatError("Oversized DOCX body")
            with archive.open(info) as source:
                raw = source.read(25_000_001)
        if len(raw) > 25_000_000 or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise LocalFormatError("Unsupported XML declarations or DOCX size")
        root = ElementTree.fromstring(raw)
        if any(element.tag in {W + "ins", W + "del"} for element in root.iter()):
            raise LocalFormatError("DOCX tracked changes require explicit revision interpretation")
        body = root.find(W + "body")
        if body is None:
            raise LocalFormatError("Unsupported DOCX body namespace")

        def paragraph(element):
            return "".join(
                node.text or "" if node.tag == W + "t" else "\t" if node.tag == W + "tab" else "\n"
                for node in element.iter()
                if node.tag in {W + "t", W + "tab", W + "br", W + "cr"}
            )

        lines = []
        for element in body:
            if element.tag == W + "p":
                lines.append(paragraph(element))
            elif element.tag == W + "tbl":
                for row in element.findall(W + "tr"):
                    lines.append(
                        "\t".join(
                            "\n".join(paragraph(p) for p in cell.iter(W + "p"))
                            for cell in row.findall(W + "tc")
                        )
                    )
        text = "\n".join(lines)
        limitations = [
            "DOCX body text and flattened tables; headers, footers, comments "
            "and embedded files omitted"
        ]
    elif suffix == ".pdf":
        converter = shutil.which("pdftotext")
        if not converter:
            raise LocalFormatError("pdftotext is not installed; PDF not imported")
        with path.open("rb") as source:
            if source.read(5) != b"%PDF-":
                raise LocalFormatError("Invalid PDF signature")
        with tempfile.TemporaryDirectory(prefix="work-memory-pdf-") as temporary:
            output = Path(temporary) / "text.txt"
            result = subprocess.run(
                [converter, "-enc", "UTF-8", "-layout", str(path), str(output)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
            if result.returncode or not output.exists():
                raise LocalFormatError("PDF text unavailable; encrypted or unsupported document")
            if output.stat().st_size > 8_000_000:
                raise LocalFormatError("Oversized PDF text output")
            text = output.read_text(encoding="utf-8")
        limitations = ["PDF text extraction may lose reading order or table structure; no OCR"]
    else:
        raise LocalFormatError("Unsupported local document format")
    if not text.strip() or len(text) > MAX_TEXT_CHARS:
        raise LocalFormatError("Document has no extractable text or exceeds the text limit")
    return text, {
        "kind": "local_document",
        "format": suffix.removeprefix("."),
        "coverage": "normalized_text",
        "limitations": limitations,
    }


def local_observation(path: Path, project: str) -> Observation:
    before = path.stat()
    text, metadata = read_document(path)
    after = path.stat()
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        raise LocalFormatError("File changed while being read; retry next synchronization")
    metadata["file_stat"] = {
        "mtime_ns": after.st_mtime_ns,
        "size": after.st_size,
        "device": after.st_dev,
    }
    return Observation(
        source="local",
        source_id=str(path),
        project=project,
        source_url=path.as_uri(),
        title=path.name,
        content=text,
        source_updated_at=datetime.fromtimestamp(after.st_mtime, UTC).isoformat(),
        metadata=metadata,
    )


def collect_folder(store, scope):
    root = Path(scope.root).expanduser().resolve()
    key = (
        "local:"
        + hashlib.sha256(json.dumps(scope.model_dump(), sort_keys=True).encode()).hexdigest()
    )
    state = store.collection_state(key)
    result = {
        "scope_id": key,
        "source": "local",
        "observed": 0,
        "new": 0,
        "revoked": 0,
        "deferred": 0,
    }
    issues = []
    with store.connect() as db:
        known = {
            r[0]
            for r in db.execute(
                "SELECT DISTINCT source_id FROM available_observations "
                "WHERE source='local' AND project=?",
                (scope.project,),
            )
            if Path(r[0]).is_relative_to(root)
        }
    try:
        root_stat = root.stat()
        if not root.is_dir():
            raise LocalFormatError("Local scope root is not a directory")
        if state.get("root_device") is not None and state["root_device"] != root_stat.st_dev:
            raise LocalFormatError("Local scope volume changed; no deletion inference performed")
        state["root_device"] = root_stat.st_dev
        paths = []

        def failed_walk(error):
            raise error

        for directory, dirs, files in os.walk(root, followlinks=False, onerror=failed_walk):
            dirs[:] = sorted(
                d for d in dirs if not d.startswith(".") and not (Path(directory) / d).is_symlink()
            )
            for name in sorted(files):
                path = Path(directory) / name
                relative = str(path.relative_to(root))
                if name.startswith(".") or path.is_symlink():
                    continue
                if any(fnmatch.fnmatch(relative, g) for g in scope.include_globs) and not any(
                    fnmatch.fnmatch(relative, g) for g in scope.exclude_globs
                ):
                    paths.append(path)
                if len(paths) > 10_000:
                    raise LocalFormatError("Local candidate limit reached; narrow the folder")
        processed = 0
        for path in sorted(paths):
            if store.is_withdrawn("local", str(path), scope.project):
                continue
            try:
                stat = path.stat()
                if not os.access(path, os.R_OK):
                    raise PermissionError(str(path))
                previous = store.latest_observation("local", str(path), scope.project)
                signature = {
                    "mtime_ns": stat.st_mtime_ns,
                    "size": stat.st_size,
                    "device": stat.st_dev,
                }
                checked = state.get("verified", {}).get(str(path))
                deep_due = (
                    not checked
                    or (
                        datetime.fromisoformat(utcnow()) - datetime.fromisoformat(checked)
                    ).total_seconds()
                    >= scope.reconcile_seconds
                )
                if (
                    previous
                    and json.loads(previous["metadata"]).get("file_stat") == signature
                    and not deep_due
                ):
                    store.touch_observation(previous["id"])
                    continue
                if processed >= scope.max_files_per_sync:
                    result["deferred"] += 1
                    continue
                processed += 1
                item = local_observation(path, scope.project)
                ingested = store.ingest(item)
                result["observed"] += 1
                result["new"] += int(ingested["created"])
                state.setdefault("verified", {})[str(path)] = utcnow()
            except PermissionError:
                if str(path) in known and not os.access(path, os.R_OK):
                    result["revoked"] += store.revoke_source(
                        "local", str(path), "Confirmed local read permission denied"
                    )
                issues.append({"path": str(path), "error": "PermissionError"})
            except Exception as exc:
                issues.append({"path": str(path), "error": type(exc).__name__})
        # A missing root, failed enumeration or changed mount never implies mass deletion.
        for path in known:
            try:
                Path(path).stat()
            except FileNotFoundError:
                if root.is_dir() and os.access(root, os.R_OK | os.X_OK):
                    result["revoked"] += store.revoke_source(
                        "local", path, "Confirmed missing file in accessible local scope"
                    )
        result["state"] = "partial" if issues or result["deferred"] else "complete"
    except PermissionError as exc:
        denied = Path(exc.filename).resolve() if exc.filename else root
        if denied.is_relative_to(root) and not os.access(denied, os.R_OK | os.X_OK):
            for path in known:
                if Path(path).is_relative_to(denied):
                    result["revoked"] += store.revoke_source(
                        "local", path, "Confirmed local folder permission denied"
                    )
        issues.append({"path": str(root), "error": "PermissionError"})
        result["state"] = "incomplete"
    except Exception as exc:
        issues.append({"path": str(root), "error": type(exc).__name__})
        result["state"] = "incomplete"
    state.update(
        root=str(root), state=result["state"], last_attempt=utcnow(), issues=issues, summary=result
    )
    if result["state"] == "complete":
        state["last_complete"] = utcnow()
    store.save_collection_state(key, state)
    result["error_counts"] = dict(Counter(i["error"] for i in issues))
    return result
