import hashlib
import json
import os
import re
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from .access import AccessControlUnavailable, AccessLedger, source_aliases
from .identity import subject_identity
from .models import FactInput, Observation, instant, utcnow
from .sections import PLANNER_VERSION, plan_sections

EXTRACTION_ROUTE = (
    "coalesce(CASE WHEN json_extract(o.metadata,'$.kind')='jira_issue_history' "
    "THEN 'jira-history-v1' "
    "WHEN o.source='jira' "
    "AND coalesce(json_extract(o.metadata,'$.projection'),'content')='content' "
    "AND json_extract(o.metadata,'$.tool') IN "
    "('mcp__claude_ai_Atlassian_Rovo__getJiraIssue',"
    "'mcp__claude_ai_Atlassian_Rovo__searchJiraIssuesUsingJql') THEN 'jira-fields-v1' "
    "WHEN o.source='local' THEN ? END,?)"
)


def analysis_route(alias):
    return (
        f"CASE WHEN {alias}.source='jira' AND f.extractor NOT IN "
        "('jira-fields-v1','jira-person-ref-v1','jira-history-v1') "
        "THEN coalesce(?,f.extractor) ELSE coalesce("
        + EXTRACTION_ROUTE.replace("o.", alias + ".")
        + ",f.extractor) END"
    )


def source_instant(value):
    try:
        return instant(value) if isinstance(value, str) else None
    except ValueError:
        return None


EXTRACTION_SCOPES = {
    "jira-fields-v1": "provider_fields_only",
    "jira-person-ref-v1": "provider_identity_reference",
    "jira-history-v1": "provider_field_history",
    "literal-tables-v1": "literal_table_values",
    "communication-headers-v1": "headers_only",
}


def extraction_scope(extractor, metadata):
    metadata = json.loads(metadata) if isinstance(metadata, str) else metadata
    if metadata.get("coverage") == "metadata_only":
        return "headers_only"
    if metadata.get("kind") == "jira_issue_history":
        return "provider_field_history"
    return EXTRACTION_SCOPES.get(extractor, "source_section_semantics")


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS observations (
 id TEXT PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL,
 project TEXT NOT NULL, source_url TEXT NOT NULL, title TEXT NOT NULL,
 content TEXT NOT NULL, content_hash TEXT NOT NULL, metadata TEXT NOT NULL,
 source_updated_at TEXT, observed_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS source_lookup ON observations(source,source_id,project,observed_at);
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
 observation_id UNINDEXED, title, content, tokenize='unicode61'
);
CREATE TABLE IF NOT EXISTS facts (
 row_id INTEGER PRIMARY KEY, id TEXT NOT NULL, observation_id TEXT NOT NULL,
 project TEXT NOT NULL, subject TEXT NOT NULL, predicate TEXT NOT NULL,
 value TEXT NOT NULL, kind TEXT NOT NULL, certainty TEXT NOT NULL,
 valid_from TEXT, valid_to TEXT, known_from TEXT NOT NULL, known_to TEXT,
 quote TEXT NOT NULL, start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL,
 supersedes TEXT, extractor TEXT NOT NULL,
 review_status TEXT NOT NULL DEFAULT 'unreviewed', review_note TEXT,
 FOREIGN KEY(observation_id) REFERENCES observations(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS current_fact ON facts(id) WHERE known_to IS NULL;
CREATE INDEX IF NOT EXISTS fact_lookup ON facts(project,subject,predicate,known_to);
CREATE TABLE IF NOT EXISTS extraction_jobs (
 observation_id TEXT NOT NULL, extractor TEXT NOT NULL, state TEXT NOT NULL,
 updated_at TEXT NOT NULL, error TEXT,
 PRIMARY KEY(observation_id,extractor),
 FOREIGN KEY(observation_id) REFERENCES observations(id)
);
CREATE TABLE IF NOT EXISTS extraction_publications (
 observation_id TEXT NOT NULL,extractor TEXT NOT NULL,scope TEXT NOT NULL,
 published_at TEXT NOT NULL,PRIMARY KEY(observation_id,extractor),
 FOREIGN KEY(observation_id) REFERENCES observations(id)
);
CREATE TABLE IF NOT EXISTS extraction_parts (
 observation_id TEXT NOT NULL, extractor TEXT NOT NULL, part_id TEXT NOT NULL,
 ordinal INTEGER NOT NULL, label TEXT NOT NULL, start_offset INTEGER NOT NULL,
 end_offset INTEGER NOT NULL, planner TEXT NOT NULL, state TEXT NOT NULL,
 result TEXT, updated_at TEXT NOT NULL,
 PRIMARY KEY(observation_id,extractor,part_id),
 FOREIGN KEY(observation_id) REFERENCES observations(id)
);
CREATE TABLE IF NOT EXISTS sync_runs (
 id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
 state TEXT NOT NULL, error TEXT, summary TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS withdrawals (
 source TEXT NOT NULL, source_id TEXT NOT NULL, project TEXT NOT NULL,
 PRIMARY KEY(source,source_id,project)
);
CREATE TABLE IF NOT EXISTS collection_state (
 scope_id TEXT PRIMARY KEY, state TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_policy (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS semantic_chunks (
 observation_id TEXT NOT NULL,index_key TEXT NOT NULL,ordinal INTEGER NOT NULL,
 start_offset INTEGER NOT NULL,end_offset INTEGER NOT NULL,
 dimensions INTEGER NOT NULL,vector BLOB NOT NULL,
 PRIMARY KEY(observation_id,index_key,ordinal),
 FOREIGN KEY(observation_id) REFERENCES observations(id)
);
CREATE TABLE IF NOT EXISTS semantic_jobs (
 observation_id TEXT NOT NULL,index_key TEXT NOT NULL,expected_chunks INTEGER NOT NULL,
 state TEXT NOT NULL,updated_at TEXT NOT NULL,error TEXT,
 PRIMARY KEY(observation_id,index_key),
 FOREIGN KEY(observation_id) REFERENCES observations(id)
);
"""


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Store:
    def __init__(self, directory: str | Path, *, control_dir: Path | None = None):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "memory.sqlite3"
        self.access_ledger = None
        self.origin = None
        with self.connect() as db:
            db.executescript(SCHEMA)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(facts)")}
            if "review_status" not in columns:
                db.execute(
                    "ALTER TABLE facts ADD COLUMN review_status TEXT NOT NULL DEFAULT 'unreviewed'"
                )
            if "review_note" not in columns:
                db.execute("ALTER TABLE facts ADD COLUMN review_note TEXT")
            if not db.execute(
                "SELECT 1 FROM runtime_policy WHERE key='publication_migration'"
            ).fetchone():
                legacy = db.execute(
                    "SELECT f.observation_id,f.extractor,min(f.known_from) AS published_at,"
                    "o.metadata "
                    "FROM facts f JOIN observations o ON o.id=f.observation_id "
                    "GROUP BY f.observation_id,f.extractor UNION ALL "
                    "SELECT e.observation_id,e.extractor,e.updated_at,o.metadata "
                    "FROM extraction_jobs e JOIN observations o ON o.id=e.observation_id "
                    "WHERE e.state='complete'"
                ).fetchall()
                for row in legacy:
                    db.execute(
                        "INSERT OR IGNORE INTO extraction_publications VALUES (?,?,?,?)",
                        (
                            row["observation_id"],
                            row["extractor"],
                            extraction_scope(row["extractor"], row["metadata"]),
                            row["published_at"],
                        ),
                    )
                db.execute("INSERT INTO runtime_policy VALUES ('publication_migration','1')")
        os.chmod(self.path, 0o600)
        authority = self.runtime_policy("access_control")
        if authority:
            self.origin = str(uuid.UUID(authority["origin"]))
            self.access_ledger = AccessLedger(Path(authority["path"]))
        else:
            origin = str(uuid.uuid4())
            ledger = AccessLedger(
                (control_dir or self.directory / "control") / "access.sqlite3", create=True
            )
            with self.connect() as db:
                for row in db.execute("SELECT * FROM withdrawals"):
                    ledger.block_source(
                        origin,
                        row["source"],
                        row["source_id"],
                        row["project"],
                        "Migrated withdrawal",
                    )
                for row in db.execute("SELECT id FROM observations WHERE active=0"):
                    ledger.block_observation(origin, row["id"], "Migrated inactive observation")
            self.set_runtime_policy("access_control", {"origin": origin, "path": str(ledger.path)})
            self.origin, self.access_ledger = origin, ledger

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15, uri=True)
        db.row_factory = sqlite3.Row
        db.create_function("memory_instant", 1, source_instant, deterministic=True)
        db.create_function("memory_extraction_scope", 2, extraction_scope, deterministic=True)
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if self.access_ledger:
                if not self.access_ledger.path.exists():
                    raise AccessControlUnavailable(
                        "Access ledger missing; cached content remains blocked"
                    )
                db.execute(
                    "ATTACH DATABASE ? AS access_control",
                    (self.access_ledger.path.as_uri() + "?mode=ro",),
                )
                # origin is a validated UUID generated locally, never source input.
                db.execute(f"""CREATE TEMP VIEW available_observations AS
SELECT o.rowid AS storage_rowid,o.* FROM observations o WHERE o.active=1
AND NOT EXISTS (SELECT 1 FROM access_control.source_blocks b
 WHERE b.origin='{self.origin}' AND b.source=o.source AND b.source_id IN (o.source_id,
 CASE WHEN o.source='microsoft365' AND substr(o.source_id,1,8)='file:///' THEN substr(o.source_id,9)
 WHEN o.source='microsoft365' THEN 'file:///'||o.source_id ELSE o.source_id END)
 AND (b.project='' OR b.project=o.project))
AND NOT EXISTS (SELECT 1 FROM access_control.observation_blocks b
 WHERE b.origin='{self.origin}' AND b.observation_id=o.id)
""")
            with db:
                yield db
        finally:
            db.close()

    def token(self) -> str:
        path = self.directory / "api-token"
        if not path.exists():
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "w") as stream:
                    stream.write(secrets.token_urlsafe(32))
        return path.read_text().strip()

    def ingest(self, item: Observation, now: str | None = None) -> dict[str, Any]:
        now = instant(now) if now else utcnow()
        # Metadata changes matter; adapters remove transient session/transport data.
        signature = digest(json.dumps(item.model_dump(), sort_keys=True, ensure_ascii=False))
        if self.access_ledger.source_blocked(
            self.origin, item.source, item.source_id, item.project
        ):
            raise ValueError("Source object was withdrawn; explicit recovery is required")
        with self.connect() as db:
            if db.execute(
                "SELECT 1 FROM withdrawals WHERE source=? AND source_id=? AND project=?",
                (item.source, item.source_id, item.project),
            ).fetchone():
                raise ValueError("Source object was withdrawn; explicit recovery is required")
            old = db.execute(
                "SELECT id,active,content_hash FROM observations WHERE source=? AND source_id=? "
                "AND project=? AND coalesce(json_extract(metadata,'$.projection'),'content')=? "
                "ORDER BY observed_at DESC,rowid DESC LIMIT 1",
                (
                    item.source,
                    item.source_id,
                    item.project,
                    item.metadata.get("projection", "content"),
                ),
            ).fetchone()
            if old and old["content_hash"] == signature:
                if not old["active"] or self.access_ledger.observation_blocked(
                    self.origin, old["id"]
                ):
                    raise ValueError(
                        "This observation was withdrawn; explicit recovery is required"
                    )
                db.execute("UPDATE observations SET last_seen_at=? WHERE id=?", (now, old["id"]))
                return {"id": old["id"], "created": False}
            oid = str(uuid.uuid4())
            db.execute(
                "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (
                    oid,
                    item.source,
                    item.source_id,
                    item.project,
                    item.source_url,
                    item.title,
                    item.content,
                    signature,
                    json.dumps(item.metadata, ensure_ascii=False),
                    item.source_updated_at,
                    now,
                    now,
                ),
            )
            db.execute("INSERT INTO documents_fts VALUES (?,?,?)", (oid, item.title, item.content))
            return {"id": oid, "created": True}

    def observation(self, oid: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM available_observations WHERE id=?", (oid,)).fetchone()
            if not row:
                raise KeyError("Observation unavailable")
            result = dict(row)
            result.pop("storage_rowid", None)
            result["metadata"] = json.loads(result["metadata"])
            restore = db.execute(
                "SELECT value FROM runtime_policy WHERE key='restore_state'"
            ).fetchone()
            if restore:
                result["restore_state"] = json.loads(restore[0])
            return result

    def active_observation_ids(self) -> list[str]:
        with self.connect() as db:
            return [
                r[0]
                for r in db.execute(
                    "SELECT id FROM available_observations ORDER BY observed_at,storage_rowid"
                )
            ]

    def add_facts(
        self,
        oid: str,
        facts: list[FactInput],
        extractor: str,
        now: str | None = None,
        *,
        evidence_offsets: list[int] | None = None,
        replace_native_projection: bool = False,
    ) -> list[str]:
        now = instant(now) if now else utcnow()
        observation = self.observation(oid)
        ids = []
        if evidence_offsets is not None and len(evidence_offsets) != len(facts):
            raise ValueError("Each fact requires its source offset")
        with self.connect() as db:
            current_observation = db.execute(
                "SELECT storage_rowid FROM available_observations WHERE id=?", (oid,)
            ).fetchone()
            if not current_observation:
                raise KeyError("Observation unavailable")
            if replace_native_projection:
                native_projections = {
                    "jira-history-v1": "jira-changelog-v1",
                    "jira-fields-v1": "content",
                }
                projection = observation["metadata"].get("projection", "content")
                if (
                    observation["source"] != "jira"
                    or native_projections.get(extractor) != projection
                ):
                    raise ValueError(
                        "Only the native Jira processor may replace its own projection"
                    )
                revisions = db.execute(
                    "SELECT f.row_id,f.known_from,o.observed_at,o.storage_rowid FROM facts f "
                    "JOIN available_observations o ON o.id=f.observation_id "
                    "WHERE o.source=? AND o.source_id=? AND o.project=? "
                    "AND coalesce(json_extract(o.metadata,'$.projection'),'content')=? "
                    "AND f.extractor=? AND f.known_to IS NULL AND o.id!=?",
                    (
                        observation["source"],
                        observation["source_id"],
                        observation["project"],
                        projection,
                        extractor,
                        oid,
                    ),
                ).fetchall()
                if any(
                    now < r["known_from"]
                    or (now == r["known_from"] and extractor != "jira-fields-v1")
                    or (r["observed_at"], r["storage_rowid"])
                    > (observation["observed_at"], current_observation["storage_rowid"])
                    for r in revisions
                ):
                    raise ValueError("Native knowledge revision must advance in order")
                db.executemany(
                    "UPDATE facts SET known_to=? WHERE row_id=?",
                    [(now, r["row_id"]) for r in revisions],
                )
            # Entire extraction is atomic: any invalid quote rejects the batch.
            for index, fact in enumerate(facts):
                offset = (
                    evidence_offsets[index]
                    if evidence_offsets is not None
                    else observation["content"].find(fact.quote)
                )
                if (
                    offset < 0
                    or observation["content"][offset : offset + len(fact.quote)] != fact.quote
                ):
                    raise ValueError("Evidence quote is not an exact substring of the observation")
                fid = digest(json.dumps([oid, fact.model_dump()], sort_keys=True))
                if db.execute("SELECT 1 FROM facts WHERE id=?", (fid,)).fetchone():
                    ids.append(fid)
                    continue
                if fact.supersedes:
                    previous = db.execute(
                        "SELECT * FROM facts WHERE id=? AND known_to IS NULL",
                        (fact.supersedes,),
                    ).fetchone()
                    if not previous:
                        raise ValueError("Replacement target is unavailable")
                    if (previous["project"], previous["subject"], previous["predicate"]) != (
                        observation["project"],
                        fact.subject,
                        fact.predicate,
                    ):
                        raise ValueError(
                            "Replacement must refer to the same subject/property/project"
                        )
                    if fact.certainty != "explicit" or previous["valid_from"] is None:
                        raise ValueError("Replacement needs explicit evidence and known validity")
                    if fact.valid_from < previous["valid_from"] or (
                        previous["valid_to"] and fact.valid_from >= previous["valid_to"]
                    ):
                        raise ValueError("Replacement date is outside the previous interval")
                    if now <= previous["known_from"]:
                        raise ValueError("Knowledge time must advance")
                    db.execute(
                        "UPDATE facts SET known_to=? WHERE row_id=?", (now, previous["row_id"])
                    )
                    fields = dict(previous)
                    del fields["row_id"]
                    fields.update(valid_to=fact.valid_from, known_from=now, known_to=None)
                    db.execute(
                        f"INSERT INTO facts ({','.join(fields)}) "
                        f"VALUES ({','.join('?' for _ in fields)})",
                        tuple(fields.values()),
                    )
                db.execute(
                    "INSERT INTO facts (id,observation_id,project,subject,predicate,value,kind,"
                    "certainty,valid_from,valid_to,known_from,quote,start_offset,end_offset,"
                    "supersedes,extractor) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        fid,
                        oid,
                        observation["project"],
                        fact.subject,
                        fact.predicate,
                        fact.value,
                        fact.kind,
                        fact.certainty,
                        fact.valid_from,
                        fact.valid_to,
                        now,
                        fact.quote,
                        offset,
                        offset + len(fact.quote),
                        fact.supersedes,
                        extractor,
                    ),
                )
                ids.append(fid)
            db.execute(
                "INSERT INTO extraction_jobs VALUES (?,?,'complete',?,NULL) "
                "ON CONFLICT(observation_id,extractor) DO UPDATE SET "
                "state='complete',updated_at=excluded.updated_at,error=NULL",
                (oid, extractor, now),
            )
            db.execute(
                "INSERT OR IGNORE INTO extraction_publications VALUES (?,?,?,?)",
                (
                    oid,
                    extractor,
                    extraction_scope(extractor, observation["metadata"]),
                    now,
                ),
            )
        return ids

    def extraction_state(self, oid: str, extractor: str) -> str | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT state FROM extraction_jobs WHERE observation_id=? AND extractor=?",
                (oid, extractor),
            ).fetchone()
            return row["state"] if row else None

    def prepare_extractions(
        self, extractor: str, observation_id: str | None = None, *, section_chars: int = 12000
    ):
        """Freeze a deterministic plan; never discard successful, paid section results."""
        if observation_id is not None:
            self.observation(observation_id)
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,content FROM available_observations o WHERE active=1 "
                "AND (? IS NULL OR id=?) AND NOT EXISTS "
                "(SELECT 1 FROM extraction_jobs e WHERE e.observation_id=o.id "
                "AND e.extractor=? AND e.state='complete') ORDER BY observed_at,storage_rowid",
                (observation_id, observation_id, extractor),
            ).fetchall()
            for row in rows:
                planned = plan_sections(row["content"], max_chars=section_chars)
                existing = {
                    r[0]
                    for r in db.execute(
                        "SELECT part_id FROM extraction_parts "
                        "WHERE observation_id=? AND extractor=?",
                        (row["id"], extractor),
                    )
                }
                if existing and existing != {p.id for p in planned}:
                    raise ValueError("Extraction plan changed; use a new extractor version")
                for ordinal, part in enumerate(planned):
                    db.execute(
                        "INSERT OR IGNORE INTO extraction_parts VALUES "
                        "(?,?,?,?,?,?,?,?,'pending',NULL,?)",
                        (
                            row["id"],
                            extractor,
                            part.id,
                            ordinal,
                            part.label,
                            part.start,
                            part.end,
                            PLANNER_VERSION
                            if section_chars == 12000
                            else f"{PLANNER_VERSION}:{section_chars}",
                            utcnow(),
                        ),
                    )
                db.execute(
                    "INSERT OR IGNORE INTO extraction_jobs VALUES (?,?,'pending',?,NULL)",
                    (row["id"], extractor, utcnow()),
                )

    def extraction_parts(self, oid: str, extractor: str) -> list[dict]:
        self.observation(oid)
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM extraction_parts WHERE observation_id=? AND extractor=? "
                    "ORDER BY ordinal",
                    (oid, extractor),
                )
            ]

    def save_extraction_part(
        self,
        oid: str,
        extractor: str,
        part_id: str,
        facts: list[FactInput],
        *,
        provenance: dict | None = None,
    ):
        with self.connect() as db:
            source = db.execute(
                "SELECT content FROM available_observations WHERE id=?",
                (oid,),
            ).fetchone()
            part = db.execute(
                "SELECT * FROM extraction_parts "
                "WHERE observation_id=? AND extractor=? AND part_id=?",
                (oid, extractor, part_id),
            ).fetchone()
            if not source or not part:
                raise KeyError("Extraction part unavailable")
            if part["state"] == "complete":
                return
            text = source["content"][part["start_offset"] : part["end_offset"]]
            offsets = []
            for fact in facts:
                offset = text.find(fact.quote)
                if offset < 0:
                    raise ValueError(
                        "Evidence quote is not an exact substring of its source section"
                    )
                offsets.append(part["start_offset"] + offset)
            result = json.dumps(
                {
                    "facts": [f.model_dump() for f in facts],
                    "offsets": offsets,
                    "provenance": provenance,
                }
            )
            db.execute(
                "UPDATE extraction_parts SET state='complete',result=?,updated_at=? "
                "WHERE observation_id=? AND extractor=? AND part_id=?",
                (result, utcnow(), oid, extractor, part_id),
            )

    def publish_extraction_parts(self, oid: str, extractor: str) -> list[str]:
        parts = self.extraction_parts(oid, extractor)
        if not parts or any(p["state"] != "complete" for p in parts):
            raise ValueError("Cannot publish an incomplete extraction plan")
        facts, offsets = [], []
        for part in parts:
            result = json.loads(part["result"])
            facts.extend(FactInput.model_validate(f) for f in result["facts"])
            offsets.extend(result["offsets"])
        # Publish the entire new interpretation atomically. Existing assertions are
        # not withdrawn merely because a newer extractor omitted or renamed them.
        return self.add_facts(oid, facts, extractor, evidence_offsets=offsets)

    def review_fact(
        self,
        fact_id: str,
        status: str,
        note: str,
        *,
        changes: dict | None = None,
        now: str | None = None,
    ):
        """Local QA operation; never exposed as a consumer MCP/API write tool.

        A review is an interpretation of evidence, not confirmation by the user.
        Knowledge history is retained even when an unsupported assertion is rejected.
        """
        if status not in {"supported", "needs_review", "rejected"} or not note.strip():
            raise ValueError("A review needs a supported status and a reason")
        if len(note) > 2000 or set(changes or {}) - {"valid_from", "valid_to", "quote"}:
            raise ValueError("Unsupported review correction")
        now = instant(now) if now else utcnow()
        with self.connect() as db:
            previous = db.execute(
                "SELECT f.* FROM facts f JOIN available_observations o ON o.id=f.observation_id "
                "WHERE f.id=? AND f.known_to IS NULL AND o.active=1",
                (fact_id,),
            ).fetchone()
            if not previous:
                raise KeyError("Fact unavailable")
            if now <= previous["known_from"]:
                raise ValueError("Knowledge time must advance")
            fields = dict(previous)
            del fields["row_id"]
            fields.update(review_status=status, review_note=note, known_from=now, known_to=None)
            for key, value in (changes or {}).items():
                if value is not None and not isinstance(value, str):
                    raise ValueError("Reviewed dates and quotes must be strings or null dates")
                if key == "quote" and value is None:
                    raise ValueError("Reviewed quote cannot be null")
                fields[key] = (
                    (instant(value) if value is not None else None)
                    if key.startswith("valid_")
                    else value
                )
            if fields["valid_from"] and fields["valid_to"]:
                if fields["valid_to"] <= fields["valid_from"]:
                    raise ValueError("Invalid reviewed validity interval")
            content = db.execute(
                "SELECT content FROM available_observations WHERE id=?", (fields["observation_id"],)
            ).fetchone()[0]
            offset = content.find(fields["quote"])
            if not fields["quote"].strip() or offset < 0:
                raise ValueError("Reviewed evidence must be an exact source span")
            fields.update(start_offset=offset, end_offset=offset + len(fields["quote"]))
            db.execute("UPDATE facts SET known_to=? WHERE row_id=?", (now, previous["row_id"]))
            db.execute(
                f"INSERT INTO facts ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                tuple(fields.values()),
            )

    def extraction_failed(self, oid: str, extractor: str):
        with self.connect() as db:
            db.execute(
                "INSERT INTO extraction_jobs VALUES (?,?,'failed',?,'Extraction failed') "
                "ON CONFLICT(observation_id,extractor) DO UPDATE SET "
                "state='failed',updated_at=excluded.updated_at,error=excluded.error",
                (oid, extractor, utcnow()),
            )

    def facts(
        self,
        project: str | None = None,
        subject: str | None = None,
        valid_at: str | None = None,
        known_at: str | None = None,
        limit: int = 50,
        offset: int = 0,
        identity_key: str | None = None,
    ) -> dict[str, Any]:
        limit, offset = self.bounds(limit, offset)
        if identity_key is not None and not re.fullmatch(r"[0-9a-f]{64}", identity_key):
            raise ValueError("Expected a subject identity key")
        valid = instant(valid_at) if valid_at else utcnow()
        known = instant(known_at) if known_at else utcnow()
        clauses = [
            "o.active=1",
            "f.review_status!='rejected'",
            "f.known_from<=?",
            "(f.known_to IS NULL OR f.known_to>?)",
            "(f.valid_from IS NULL OR f.valid_from<=?)",
            "(f.valid_to IS NULL OR f.valid_to>?)",
        ]
        args = [known, known, valid, valid]
        for column, value in [("f.project", project), ("f.subject", subject)]:
            if value is not None:
                clauses.append(column + "=?")
                args.append(value)
        with self.connect() as db:
            # Conflict detection spans all eligible facts, before pagination.
            active = db.execute(
                "SELECT value FROM runtime_policy WHERE key='active_extractor'"
            ).fetchone()
            active = json.loads(active[0]) if active else None
            local_active = db.execute(
                "SELECT value FROM runtime_policy WHERE key='active_local_extractor'"
            ).fetchone()
            local_active = json.loads(local_active[0]) if local_active else None
            restore = db.execute(
                "SELECT value FROM runtime_policy WHERE key='restore_state'"
            ).fetchone()
            restore_pending = bool(restore and json.loads(restore[0]).get("requires_resync"))
            rows = db.execute(
                "SELECT f.*,o.source,o.source_id,o.source_url,o.title AS source_title,"
                "o.source_updated_at,o.observed_at AS source_observed_at,"
                "o.last_seen_at AS source_last_seen_at,"
                "json_extract(o.metadata,'$.kind') AS source_kind,"
                "json_extract(o.metadata,'$.coverage') AS source_coverage,"
                "json_extract(o.metadata,'$.source_tool') AS source_tool,"
                "json_extract(CASE WHEN json_valid(o.content) THEN o.content END,'$.assignee') "
                "AS identity_assignee,json_extract(o.metadata,'$.actor_ref') AS identity_actor,"
                "substr(o.content,max(1,f.start_offset-199),"
                "min(length(o.content),f.end_offset+200)-max(0,f.start_offset-200)) "
                "AS evidence_context,max(0,f.start_offset-200) AS context_start_offset,"
                "(SELECT p.label FROM extraction_parts p WHERE p.observation_id=f.observation_id "
                "AND p.extractor=f.extractor AND p.start_offset<=f.start_offset "
                "AND p.end_offset>=f.end_offset ORDER BY p.ordinal LIMIT 1) AS evidence_section,"
                "(SELECT json_extract(p.result,'$.provenance') FROM extraction_parts p "
                "WHERE p.observation_id=f.observation_id AND p.extractor=f.extractor "
                "AND p.start_offset<=f.start_offset AND p.end_offset>=f.end_offset "
                "ORDER BY p.ordinal LIMIT 1) AS extraction_provenance,"
                "(SELECT e.state FROM extraction_jobs e WHERE e.observation_id=f.observation_id "
                f"AND e.extractor={analysis_route('o')}) "
                "AS current_extraction_state,"
                "EXISTS(SELECT 1 FROM available_observations newer WHERE newer.source=o.source "
                "AND newer.source_id IN (o.source_id,CASE "
                "WHEN o.source='microsoft365' AND substr(o.source_id,1,8)='file:///' "
                "THEN substr(o.source_id,9) WHEN o.source='microsoft365' "
                "THEN 'file:///'||o.source_id "
                "ELSE o.source_id END) AND newer.project=o.project "
                "AND coalesce(json_extract(newer.metadata,'$.projection'),'content')="
                "coalesce(json_extract(o.metadata,'$.projection'),'content') "
                "AND (newer.observed_at>o.observed_at OR (newer.observed_at=o.observed_at "
                "AND newer.storage_rowid>o.storage_rowid)) AND newer.observed_at<=? "
                "AND NOT EXISTS(SELECT 1 FROM extraction_publications job "
                "WHERE job.observation_id=newer.id "
                "AND job.scope=memory_extraction_scope(f.extractor,o.metadata) "
                "AND (job.extractor=f.extractor OR (job.scope='source_section_semantics' "
                "AND (job.extractor LIKE 'evidence-%' OR job.extractor LIKE 'local-evidence-%') "
                "AND (f.extractor LIKE 'evidence-%' OR f.extractor LIKE 'local-evidence-%'))) "
                "AND job.published_at<=?)) AS pending_source_analysis,"
                "(f.extractor='jira-history-v1' AND f.valid_to IS NULL "
                "AND ?>memory_instant(o.source_updated_at) AND EXISTS("
                "SELECT 1 FROM available_observations revision WHERE revision.source=o.source "
                "AND revision.source_id=o.source_id AND revision.project=o.project "
                "AND revision.observed_at<=? AND memory_instant(revision.source_updated_at)>"
                "memory_instant(o.source_updated_at))) AS pending_history_analysis "
                "FROM facts f JOIN available_observations o ON o.id=f.observation_id WHERE "
                + " AND ".join(clauses)
                + " ORDER BY f.subject,f.predicate,f.id",
                [active, local_active, active, known, known, valid, known, *args],
            ).fetchall()
        rows = [dict(row) for row in rows]
        groups = {}
        prepared = []
        for row in rows:
            row["subject_identity"] = subject_identity(row)
            row.pop("identity_assignee")
            row.pop("identity_actor")
            row["pending_source_analysis"] = bool(row["pending_source_analysis"])
            row["pending_history_analysis"] = bool(row["pending_history_analysis"])
            if identity_key is not None and row["subject_identity"]["key"] != identity_key:
                continue
            prepared.append(row)
            if row["certainty"] in ("explicit", "uncertain"):
                subject_key = (
                    row["subject_identity"]["key"]
                    if row["kind"] in {"person", "project"} or row["subject_identity"]["resolved"]
                    else row["subject"]
                )
                groups.setdefault((row["project"], subject_key, row["predicate"]), set()).add(
                    row["value"]
                )
        rows = prepared
        items = []
        for row in rows[offset : offset + limit]:
            item = dict(row)
            item.pop("row_id")
            if item["extraction_provenance"] is not None:
                item["extraction_provenance"] = json.loads(item["extraction_provenance"])
            item["extraction_scope"] = extraction_scope(
                row["extractor"],
                {
                    "kind": row["source_kind"],
                    "coverage": row["source_coverage"],
                },
            )
            if row["extractor"] in EXTRACTION_SCOPES and item["extraction_provenance"] is None:
                item["extraction_provenance"] = {
                    "engine": "deterministic",
                    "rules": row["extractor"],
                    "scope": item["extraction_scope"],
                }
            item["source_tool_provenance"] = (
                "captured_transport" if item["source_tool"] else "legacy_adapter_label_only"
            )
            item["validity_unknown"] = row["valid_from"] is None
            subject_key = (
                row["subject_identity"]["key"]
                if row["kind"] in {"person", "project"} or row["subject_identity"]["resolved"]
                else row["subject"]
            )
            multi = len(groups.get((row["project"], subject_key, row["predicate"]), set())) > 1
            single_properties = {
                "status",
                "assigned_to",
                "deadline",
                "due_date",
                "start_date",
                "end_date",
                "sprint_count",
                "document_title",
                "jira_status_id",
                "jira_due_date",
                "jira_reported_status",
                "jira_reported_assignee_id",
                "jira_reported_assignee_label",
                "jira_reported_summary",
                "jira_reported_due_date",
            }
            multiple_properties = {"goal", "planned_activity", "expected_result"}
            cardinality = (
                "single"
                if row["predicate"] in single_properties
                else "multiple"
                if row["predicate"] in multiple_properties
                else "unknown"
            )
            item["cardinality"] = cardinality
            item["conflicting"] = multi and cardinality == "single"
            item["potential_conflict"] = multi and cardinality == "unknown"
            item["needs_semantic_validation"] = (
                row["extractor"].startswith(("evidence-", "local-evidence-"))
                and row["review_status"] == "unreviewed"
            )
            item["definitive"] = (
                row["certainty"] == "explicit"
                and not item["validity_unknown"]
                and not item["conflicting"]
                and not item["potential_conflict"]
                and row["review_status"] != "needs_review"
                and not restore_pending
                and not item["pending_source_analysis"]
                and not item["pending_history_analysis"]
                and (
                    row["kind"] not in {"person", "project"} or item["subject_identity"]["resolved"]
                )
                and not item["needs_semantic_validation"]
            )
            item["restore_requires_resync"] = restore_pending
            items.append(item)
        return {
            "items": items,
            "total": len(rows),
            "limit": limit,
            "offset": offset,
            "truncated": offset + len(items) < len(rows),
            "valid_at": valid,
            "known_at": known,
            "limitations": [
                "Unknown validity is included and explicitly marked",
                "Conflicts require known single-valued properties; unknown cardinality is explicit",
            ],
        }

    @staticmethod
    def bounds(limit: int, offset: int) -> tuple[int, int]:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("limit must be 1..100 and offset nonnegative")
        return limit, offset

    def search(
        self,
        query: str,
        project: str | None = None,
        limit: int = 20,
        offset: int = 0,
        mode: Literal["lexical", "semantic", "hybrid"] = "lexical",
    ) -> dict[str, Any]:
        limit, offset = self.bounds(limit, offset)
        if not query.strip() or len(query) > 8000:
            raise ValueError("Search query must contain 1..8000 characters")
        if mode == "lexical":
            result = self._lexical_search(query, project, limit, offset)
        else:
            from . import semantic

            if mode == "semantic":
                result = semantic.search(self, query, project, limit, offset)
            elif mode == "hybrid":
                result = semantic.hybrid(self, query, project, limit, offset)
            else:
                raise ValueError("Unknown search mode")
        restored = self.runtime_policy("restore_state")
        if restored:
            result["restore_state"] = restored
        return result

    def _lexical_search(
        self, query: str, project: str | None = None, limit: int = 20, offset: int = 0
    ) -> dict[str, Any]:
        limit, offset = self.bounds(limit, offset)
        tokens = re.findall(r"\w+", query, re.UNICODE)
        if not tokens or len(query) > 500:
            raise ValueError("Provide a nonempty query of at most 500 characters")
        match = " AND ".join('"' + t + '"' for t in tokens)
        args = [match]
        where = "documents_fts MATCH ? AND o.active=1"
        if project:
            where += " AND o.project=?"
            args.append(project)
        base = (
            " FROM documents_fts JOIN available_observations o ON o.id=observation_id WHERE "
            + where
        )
        with self.connect() as db:
            total = db.execute("SELECT count(*)" + base, args).fetchone()[0]
            rows = db.execute(
                "SELECT o.id,o.source,o.source_id,o.project,o.title,o.source_url,"
                "o.observed_at,o.last_seen_at,snippet(documents_fts,2,'[',']','…',35) AS excerpt"
                + base
                + " ORDER BY rank,o.id LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ).fetchall()
        return {
            "items": [dict(r) for r in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
            "truncated": offset + len(rows) < total,
            "limitations": [
                "Lexical search; includes historical observation versions",
                "Selected objects only; not exhaustive source coverage",
            ],
        }

    def quarantine_observation(self, oid: str, reason: str) -> None:
        """Hide a corrupt acquisition; a corrected source snapshot may still be imported."""
        if not reason.strip():
            raise ValueError("A quarantine reason is required")
        self.access_ledger.block_observation(self.origin, oid, reason)
        with self.connect() as db:
            row = db.execute("SELECT metadata FROM observations WHERE id=?", (oid,)).fetchone()
            if not row:
                raise KeyError("Observation unavailable")
            metadata = json.loads(row["metadata"])
            metadata["quarantine"] = {"reason": reason, "at": utcnow()}
            db.execute(
                "UPDATE observations SET active=0,metadata=? WHERE id=?",
                (json.dumps(metadata), oid),
            )
            db.execute("DELETE FROM documents_fts WHERE observation_id=?", (oid,))
            db.execute("DELETE FROM semantic_chunks WHERE observation_id=?", (oid,))
            db.execute("UPDATE semantic_jobs SET state='blocked' WHERE observation_id=?", (oid,))

    def withdraw(self, source: str, source_id: str, project: str) -> int:
        """Explicit deny-access operation; not inferred from search absence."""
        self.access_ledger.block_source(
            self.origin, source, source_id, project, "Withdrawn locally"
        )
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO withdrawals VALUES (?,?,?)", (source, source_id, project)
            )
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM observations WHERE source=? "
                    "AND source_id IN (?,?) AND project=?",
                    (source, *source_aliases(source, source_id), project),
                )
            ]
            for oid in ids:
                db.execute("DELETE FROM documents_fts WHERE observation_id=?", (oid,))
                db.execute("DELETE FROM semantic_chunks WHERE observation_id=?", (oid,))
                db.execute(
                    "UPDATE semantic_jobs SET state='blocked' WHERE observation_id=?", (oid,)
                )
                db.execute("UPDATE observations SET active=0 WHERE id=?", (oid,))
        return len(ids)

    def revoke_source(self, source: str, source_id: str, reason: str) -> int:
        """A confirmed provider/filesystem revocation blocks every project and future import."""
        if not reason.strip():
            raise ValueError("A confirmed revocation reason is required")
        self.access_ledger.block_source(self.origin, source, source_id, "", reason)
        with self.connect() as db:
            projects = [
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT project FROM observations WHERE source=? "
                    "AND source_id IN (?,?)",
                    (source, *source_aliases(source, source_id)),
                )
            ]
        return sum(self.withdraw(source, source_id, project) for project in projects)

    def history(self, fact_id: str) -> dict[str, Any]:
        selection = (
            "SELECT f.*,o.source,o.source_id,o.source_url,"
            "json_extract(CASE WHEN json_valid(o.content) THEN o.content END,'$.assignee') "
            "AS identity_assignee,json_extract(o.metadata,'$.actor_ref') AS identity_actor "
            "FROM facts f JOIN available_observations o ON o.id=f.observation_id "
        )
        with self.connect() as db:
            seed = db.execute(
                selection + "WHERE f.id=? AND o.active=1 ORDER BY f.known_from DESC LIMIT 1",
                (fact_id,),
            ).fetchone()
            if not seed:
                raise KeyError("Fact unavailable")
            seed = dict(seed)
            identity = subject_identity(seed)
            identity_bound = seed["kind"] in {"person", "project"} or identity["resolved"]
            rows = db.execute(
                selection
                + "WHERE o.active=1 AND f.project=? AND (f.id=? OR (f.predicate=? "
                + ("" if identity_bound else "AND f.subject=? ")
                + ")) ORDER BY f.known_from,f.row_id",
                (seed["project"], fact_id, seed["predicate"])
                + (() if identity_bound else (seed["subject"],)),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["subject_identity"] = subject_identity(item)
            if (
                identity_bound
                and item["id"] != fact_id
                and item["subject_identity"]["key"] != identity["key"]
            ):
                continue
            for field in ("row_id", "identity_assignee", "identity_actor"):
                item.pop(field)
            items.append(item)
        return {
            "items": items,
            "subject_identity": identity,
            "limitations": ["Withdrawn evidence and its facts are inaccessible, including history"],
        }

    def status(self, detail_limit: int = 10, detail_offset: int = 0) -> dict[str, Any]:
        detail_limit, detail_offset = self.bounds(detail_limit, detail_offset)
        with self.connect() as db:
            runs = [
                dict(r)
                for r in db.execute("SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT 3")
            ]
            for run in runs:
                run["summary"] = json.loads(run["summary"])
                run["summary"].pop("retention", None)
                run["summary"].pop("cost_note", None)
                for scope in run["summary"].get("scopes", []):
                    scope.pop("limitations", None)
            sources = [
                dict(r)
                for r in db.execute(
                    "SELECT source,source_id,project,max(last_seen_at) AS last_seen_at,"
                    "count(*) AS versions,"
                    "group_concat(DISTINCT json_extract(metadata,'$.coverage')) AS coverage "
                    "FROM available_observations WHERE active=1 GROUP BY source,source_id,project "
                    "ORDER BY source,source_id,project"
                )
            ]
            for source in sources:
                source["coverage"] = (source["coverage"] or "unspecified").split(",")
            jobs = [
                dict(r)
                for r in db.execute(
                    "SELECT state,count(*) AS count FROM extraction_jobs GROUP BY state"
                )
            ]
            scopes = [
                {
                    "scope_id": r["scope_id"],
                    "state": json.loads(r["state"]),
                    "updated_at": r["updated_at"],
                }
                for r in db.execute("SELECT * FROM collection_state ORDER BY scope_id")
            ]
            for scope in scopes:
                state = scope["state"]
                for key in (
                    "verified",
                    "seen_cursors",
                    "page_completed_uris",
                    "page_completed_history_keys",
                    "calendar_windows",
                ):
                    if key in state:
                        state[key + "_count"] = len(state.pop(key))
                if isinstance(state.get("cursor"), str):
                    state["cursor"] = "opaque continuation present"
                if isinstance(state.get("issues"), list):
                    state["issues_total"] = len(state["issues"])
                    state["issues_truncated"] = len(state["issues"]) > detail_limit
                    state["issues"] = state["issues"][:detail_limit]
            policy = {
                r["key"]: json.loads(r["value"]) for r in db.execute("SELECT * FROM runtime_policy")
            }
            active_extractor = policy.get("active_extractor")
            local_extractor = policy.get("active_local_extractor")
            pending = db.execute(
                "SELECT count(*) FROM available_observations o WHERE active=1 AND NOT EXISTS "
                "(SELECT 1 FROM extraction_jobs e WHERE e.observation_id=o.id "
                "AND e.state='complete' AND "
                f"({EXTRACTION_ROUTE} IS NULL OR e.extractor={EXTRACTION_ROUTE}))",
                (local_extractor, active_extractor, local_extractor, active_extractor),
            ).fetchone()[0]
            parts = [
                dict(r)
                for r in db.execute(
                    "SELECT p.observation_id,p.extractor,count(*) AS total,"
                    "sum(p.state='complete') AS completed,"
                    "sum(p.end_offset-p.start_offset) AS characters "
                    "FROM extraction_parts p "
                    "JOIN available_observations o ON o.id=p.observation_id "
                    "WHERE o.active=1 AND "
                    f"({EXTRACTION_ROUTE} IS NULL OR p.extractor={EXTRACTION_ROUTE}) "
                    "GROUP BY p.observation_id,p.extractor ORDER BY p.observation_id",
                    (local_extractor, active_extractor, local_extractor, active_extractor),
                )
            ]
            reviews = [
                dict(r)
                for r in db.execute(
                    "SELECT f.review_status,count(*) AS count FROM facts f "
                    "JOIN available_observations o ON o.id=f.observation_id "
                    "WHERE f.known_to IS NULL AND o.active=1 GROUP BY f.review_status"
                )
            ]
        budget_status = {"state": "unavailable" if policy.get("budget_file") else "not_configured"}
        if policy.get("budget_file") and Path(policy["budget_file"]).exists():
            from .budget import Budget

            budget_status = Budget(Path(policy["budget_file"])).status()
        from .semantic import status as semantic_status

        source_counts = {}
        for source in sources:
            counts = source_counts.setdefault(source["source"], {"objects": 0, "observations": 0})
            counts["objects"] += 1
            counts["observations"] += source["versions"]

        def page_info(total):
            return {
                "total": total,
                "limit": detail_limit,
                "offset": detail_offset,
                "truncated": detail_offset + detail_limit < total,
            }

        return {
            "sources": sources[detail_offset : detail_offset + detail_limit],
            "source_pagination": page_info(len(sources)),
            "source_summary": source_counts,
            "recent_runs": runs,
            "recent_runs_limit": 3,
            "extraction_jobs": jobs,
            "extraction_parts": parts[detail_offset : detail_offset + detail_limit],
            "extraction_parts_pagination": page_info(len(parts)),
            "active_extractor": active_extractor,
            "active_local_extractor": local_extractor,
            "structured_jira_extraction": {
                "extractor": "jira-fields-v1",
                "scope": "provider_fields_only",
                "description_semantics": "not automatically analyzed; available in source search",
            },
            "extraction_coverage": "Sections processed is not proof of exhaustive fact extraction",
            "collection_scopes": scopes,
            "observations_without_completed_extraction": pending,
            "fact_reviews": reviews,
            "spending": budget_status,
            "cloud_authentication": policy.get("cloud_authentication"),
            "provider_capabilities": policy.get("provider_capabilities", {}),
            "semantic_index": semantic_status(self),
            "retention": {
                "memory_and_history": "No automatic expiry during pilot",
                "raw_logs_days": 7,
            },
            "coverage": "Selected known objects; absence does not prove deletion",
            "encryption": "SQLite is not application-encrypted; use approved disk encryption",
            "storage_verification": policy.get("storage_encryption"),
            "restore_state": policy.get("restore_state"),
        }

    def set_runtime_policy(self, key: str, value):
        with self.connect() as db:
            db.execute(
                "INSERT INTO runtime_policy VALUES (?,?) ON CONFLICT(key) "
                "DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def runtime_policy(self, key: str):
        with self.connect() as db:
            row = db.execute("SELECT value FROM runtime_policy WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def collection_state(self, scope_id: str) -> dict:
        with self.connect() as db:
            row = db.execute(
                "SELECT state FROM collection_state WHERE scope_id=?", (scope_id,)
            ).fetchone()
            return json.loads(row[0]) if row else {}

    def save_collection_state(self, scope_id: str, state: dict):
        with self.connect() as db:
            db.execute(
                "INSERT INTO collection_state VALUES (?,?,?) "
                "ON CONFLICT(scope_id) DO UPDATE SET "
                "state=excluded.state,updated_at=excluded.updated_at",
                (scope_id, json.dumps(state), utcnow()),
            )

    def latest_observation(self, source: str, source_id: str, project: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM available_observations WHERE source=? AND source_id=? "
                "AND project=? AND active=1 ORDER BY observed_at DESC,storage_rowid DESC LIMIT 1",
                (source, source_id, project),
            ).fetchone()
            return dict(row) if row else None

    def is_withdrawn(self, source: str, source_id: str, project: str) -> bool:
        if self.access_ledger.source_blocked(self.origin, source, source_id, project):
            return True
        with self.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM withdrawals WHERE source=? AND source_id=? AND project=?",
                    (source, source_id, project),
                ).fetchone()
                is not None
            )

    def touch_observation(self, oid: str):
        with self.connect() as db:
            db.execute(
                "UPDATE observations SET last_seen_at=? WHERE id=? AND active=1", (utcnow(), oid)
            )

    def pending_extractions(
        self, extractor: str, limit: int = 100, *, observation_id: str | None = None
    ) -> list[str]:
        retry_before = (datetime.fromisoformat(utcnow()) - timedelta(hours=1)).isoformat()
        with self.connect() as db:
            return [
                row[0]
                for row in db.execute(
                    "SELECT o.id FROM available_observations o LEFT JOIN extraction_jobs e "
                    "ON o.id=e.observation_id AND e.extractor=? "
                    "WHERE o.active=1 AND (e.state IS NULL OR e.state='pending' OR "
                    "(e.state='failed' AND e.updated_at<=?)) "
                    "AND (? IS NULL OR o.id=?) "
                    "ORDER BY o.observed_at,o.storage_rowid LIMIT ?",
                    (extractor, retry_before, observation_id, observation_id, limit),
                )
            ]

    def start_run(self) -> str:
        rid = str(uuid.uuid4())
        with self.connect() as db:
            # A killed worker may leave a previous running record; scheduler lock is held by caller.
            db.execute(
                "UPDATE sync_runs SET state='interrupted',finished_at=? WHERE state='running'",
                (utcnow(),),
            )
            db.execute(
                "INSERT INTO sync_runs(id,started_at,state) VALUES (?,?,'running')", (rid, utcnow())
            )
        return rid

    def finish_run(self, rid: str, state: str, summary: dict):
        with self.connect() as db:
            db.execute(
                "UPDATE sync_runs SET finished_at=?,state=?,summary=? WHERE id=?",
                (utcnow(), state, json.dumps(summary), rid),
            )
