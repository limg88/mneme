"""Local-only embeddings and resumable SQLite indexing. No cloud fallback or model pulls."""

import hashlib
import json
import math
import struct
import urllib.error
import urllib.request

from .models import utcnow

CHUNKER = "chars-v1:1800:200"


class SemanticUnavailable(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise SemanticUnavailable("Local embedding redirects are refused")


class Ollama:
    def __init__(self, model="bge-m3:latest"):
        self.model = model
        # Do not send corporate text through environment HTTP proxies or redirects.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path, data=None):
        if path not in {"/api/tags", "/api/embed"}:
            raise ValueError("Unsupported local embedding operation")
        request = urllib.request.Request(
            "http://127.0.0.1:11434" + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Content-Type": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=60) as response:
                raw = response.read(16_000_001)
            if len(raw) > 16_000_000:
                raise SemanticUnavailable("Oversized local embedding response")
            return json.loads(raw)
        except (OSError, urllib.error.URLError, ValueError) as exc:
            raise SemanticUnavailable(
                "Local embedding service unavailable or invalid response"
            ) from exc

    def digest(self):
        tags = self.request("/api/tags")
        for model in tags.get("models", []):
            if model.get("name") == self.model or model.get("model") == self.model:
                if (
                    model.get("remote_host")
                    or model.get("remote_model")
                    or self.model.endswith("-cloud")
                ):
                    raise SemanticUnavailable("Only installed local embedding models are allowed")
                digest = model.get("digest")
                if isinstance(digest, str) and digest:
                    return digest
        raise SemanticUnavailable("Embedding model is not installed locally; no download attempted")

    def embed(self, texts, expected_digest):
        if self.digest() != expected_digest:
            raise SemanticUnavailable("Local model changed; rebuild the semantic index")
        result = self.request(
            "/api/embed",
            {
                "model": self.model,
                "input": texts,
                "truncate": False,
                "keep_alive": "1m",
                "options": {"num_thread": 2},
            },
        )
        if self.digest() != expected_digest:
            raise SemanticUnavailable("Local model changed while embedding; batch discarded")
        values = result.get("embeddings")
        if not isinstance(values, list) or len(values) != len(texts):
            raise SemanticUnavailable("Local model returned an incomplete embedding batch")
        vectors = [normalized(v) for v in values]
        if len({len(v) for v in vectors}) != 1:
            raise SemanticUnavailable("Embedding dimensions are inconsistent")
        return vectors


def normalized(vector):
    if (
        not isinstance(vector, list)
        or not vector
        or len(vector) > 8192
        or any(type(v) not in (float, int) or not math.isfinite(v) for v in vector)
    ):
        raise SemanticUnavailable("Invalid embedding vector")
    norm = math.sqrt(sum(v * v for v in vector))
    if not norm or not math.isfinite(norm):
        raise SemanticUnavailable("Invalid embedding magnitude")
    return [v / norm for v in vector]


def chunks(text):
    start = 0
    while start < len(text):
        end = min(start + 1800, len(text))
        yield start, end
        if end == len(text):
            break
        start = end - 200


def index(
    store, *, model="bge-m3:latest", observation_id=None, max_batches=20, batch_size=8, client=None
):
    if not 1 <= max_batches <= 1000 or not 1 <= batch_size <= 32:
        raise ValueError("Index batch limits out of range")
    client = client or Ollama(model)
    digest = client.digest()
    key = hashlib.sha256((model + digest + CHUNKER).encode()).hexdigest()
    config = {"model": model, "digest": digest, "index_key": key, "chunker": CHUNKER}
    store.set_runtime_policy("semantic_index", config)
    ids = [observation_id] if observation_id else store.active_observation_ids()
    calls, saved, complete = 0, 0, 0
    for oid in ids:
        observation = store.observation(oid)
        plan = list(chunks(observation["content"]))
        with store.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO semantic_jobs VALUES (?,?,?,'pending',?,NULL)",
                (oid, key, len(plan), utcnow()),
            )
            existing = {
                r[0]
                for r in db.execute(
                    "SELECT ordinal FROM semantic_chunks WHERE observation_id=? AND index_key=?",
                    (oid, key),
                )
            }
        pending = [(i, start, end) for i, (start, end) in enumerate(plan) if i not in existing]
        for offset in range(0, len(pending), batch_size):
            if calls >= max_batches:
                return {
                    "state": "partial",
                    "batches": calls,
                    "chunks_saved": saved,
                    "observations_completed": complete,
                    "cloud_calls": 0,
                }
            batch = pending[offset : offset + batch_size]
            try:
                vectors = client.embed(
                    [
                        observation["title"][:500] + "\n" + observation["content"][start:end]
                        for _, start, end in batch
                    ],
                    digest,
                )
                if len(vectors) != len(batch):
                    raise SemanticUnavailable("Incomplete local embedding batch")
                with store.connect() as db:
                    if not db.execute(
                        "SELECT 1 FROM available_observations WHERE id=?", (oid,)
                    ).fetchone():
                        raise KeyError("Observation unavailable")
                    known_dimension = db.execute(
                        "SELECT dimensions FROM semantic_chunks WHERE index_key=? LIMIT 1", (key,)
                    ).fetchone()
                    for (ordinal, start, end), vector in zip(batch, vectors, strict=True):
                        vector = normalized(vector)
                        if known_dimension and len(vector) != known_dimension[0]:
                            raise SemanticUnavailable(
                                "Embedding dimension changed; batch discarded"
                            )
                        known_dimension = (len(vector),)
                        db.execute(
                            "INSERT OR IGNORE INTO semantic_chunks VALUES (?,?,?,?,?,?,?)",
                            (
                                oid,
                                key,
                                ordinal,
                                start,
                                end,
                                len(vector),
                                struct.pack(f"<{len(vector)}f", *vector),
                            ),
                        )
                saved += len(batch)
                calls += 1
            except Exception:
                with store.connect() as db:
                    db.execute(
                        "UPDATE semantic_jobs SET state='failed',updated_at=?,error=? "
                        "WHERE observation_id=? AND index_key=?",
                        (utcnow(), "Local indexing failed; saved chunks retained", oid, key),
                    )
                raise
        with store.connect() as db:
            db.execute(
                "UPDATE semantic_jobs SET state='complete',updated_at=?,error=NULL "
                "WHERE observation_id=? AND index_key=?",
                (utcnow(), oid, key),
            )
        complete += 1
    return {
        "state": "complete",
        "batches": calls,
        "chunks_saved": saved,
        "observations_completed": complete,
        "cloud_calls": 0,
    }


def status(store):
    config = store.runtime_policy("semantic_index")
    if not config:
        return {"state": "not_configured", "engine": "local Ollama + SQLite"}
    with store.connect() as db:
        total = db.execute("SELECT count(*) FROM available_observations").fetchone()[0]
        complete = db.execute(
            "SELECT count(*) FROM semantic_jobs j "
            "JOIN available_observations o ON o.id=j.observation_id "
            "WHERE o.active=1 AND j.index_key=? AND j.state='complete'",
            (config["index_key"],),
        ).fetchone()[0]
        saved = db.execute(
            "SELECT count(*) FROM semantic_chunks c "
            "JOIN available_observations o ON o.id=c.observation_id "
            "WHERE o.active=1 AND c.index_key=?",
            (config["index_key"],),
        ).fetchone()[0]
    return {
        **config,
        "state": "complete" if complete == total else "partial",
        "observations_indexed": complete,
        "observations_total": total,
        "chunks_saved": saved,
    }


def search(store, query, project, limit, offset, *, client=None):
    if not query.strip() or len(query) > 8000:
        raise ValueError("Semantic query must contain 1..8000 characters")
    config = store.runtime_policy("semantic_index")
    if not config:
        raise SemanticUnavailable("Semantic index is not configured")
    client = client or Ollama(config["model"])
    vector = client.embed([query], config["digest"])[0]
    with store.connect() as db:
        rows = db.execute(
            "SELECT o.id,o.source,o.source_id,o.project,o.title,o.source_url,o.observed_at,"
            "o.last_seen_at,c.start_offset,c.end_offset,c.dimensions,c.vector,"
            "substr(o.content,c.start_offset+1,c.end_offset-c.start_offset) AS excerpt "
            "FROM semantic_chunks c JOIN available_observations o ON o.id=c.observation_id "
            "JOIN semantic_jobs j ON j.observation_id=c.observation_id AND j.index_key=c.index_key "
            "WHERE o.active=1 AND j.state='complete' AND c.index_key=? "
            "AND (? IS NULL OR o.project=?) ORDER BY o.id,c.ordinal LIMIT 50001",
            (config["index_key"], project, project),
        ).fetchall()
    if len(rows) > 50000:
        raise SemanticUnavailable("Semantic candidate limit reached; narrow the project filter")
    best = {}
    for row in rows:
        if row["dimensions"] != len(vector):
            raise SemanticUnavailable("Index dimension mismatch; rebuild the index")
        other = struct.unpack(f"<{len(vector)}f", row["vector"])
        score = max(-1.0, min(1.0, sum(a * b for a, b in zip(vector, other, strict=True))))
        item = {k: v for k, v in dict(row).items() if k not in {"dimensions", "vector"}}
        item["score"] = score
        if row["id"] not in best or score > best[row["id"]]["score"]:
            best[row["id"]] = item
    ranked = sorted(best.values(), key=lambda x: (-x["score"], x["id"]))
    return {
        "items": ranked[offset : offset + limit],
        "total": len(ranked),
        "limit": limit,
        "offset": offset,
        "truncated": offset + limit < len(ranked),
        "mode": "semantic",
        "score_kind": "cosine_similarity",
        "coverage": status(store),
        "limitations": [
            "Similarity is relevance, not factual certainty",
            "Includes historical observation versions; one best passage per observation",
        ],
    }


def hybrid(store, query, project, limit, offset):
    lexical = store._lexical_search(query, project, 100, 0)
    try:
        dense = search(store, query, project, 100, 0)
    except SemanticUnavailable as exc:
        fallback = store._lexical_search(query, project, limit, offset)
        return {
            **fallback,
            "mode": "hybrid",
            "effective_mode": "lexical",
            "semantic_error": str(exc),
        }
    scores, items = {}, {}
    for result in (lexical, dense):
        for rank, item in enumerate(result["items"], 1):
            scores[item["id"]] = scores.get(item["id"], 0) + 1 / (60 + rank)
            # Prefer the exact original semantic passage over a highlighted FTS snippet.
            items[item["id"]] = item
    ranked = sorted(items.values(), key=lambda x: (-scores[x["id"]], x["id"]))
    for item in ranked:
        item["score"] = scores[item["id"]]
    return {
        "items": ranked[offset : offset + limit],
        "total": len(ranked),
        "limit": limit,
        "offset": offset,
        "truncated": offset + limit < len(ranked),
        "mode": "hybrid",
        "effective_mode": "hybrid",
        "score_kind": "reciprocal_rank_fusion",
        "coverage": dense["coverage"],
        "candidate_pool_limited": lexical["total"] > 100 or dense["total"] > 100,
        "limitations": [
            "Reciprocal-rank fusion of up to 100 lexical and 100 semantic candidates",
            "Total counts ranked candidates, not exhaustive corpus matches",
            "Includes historical observation versions; similarity does not establish truth",
        ],
    }
