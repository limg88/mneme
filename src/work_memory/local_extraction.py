"""Bounded, local-only structured extraction from already collected observations."""

import hashlib
import json
import re
import urllib.request

from .models import Extraction
from .semantic import Ollama
from .worker import align_extraction, extraction_prompt

CONTEXT = 16384
OUTPUT = 2048


class LocalExtractionUnavailable(RuntimeError):
    pass


class LocalExtractor:
    def __init__(self, model="qwen3:8b"):
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", model):
            raise ValueError("Expected an installed local model name")
        self.model = model
        self.runtime = Ollama(model)
        self.digest = self.runtime.digest()
        if not re.fullmatch(r"[0-9a-f]{64}", self.digest):
            raise LocalExtractionUnavailable("Local weights require a stable SHA-256 digest")
        self.version = "local-evidence-v2-json-4k:" + model + ":" + self.digest
        self.last_extraction_provenance = None

    def generate(self, data):
        request = urllib.request.Request(
            "http://127.0.0.1:11434/api/generate",
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with self.runtime.opener.open(request, timeout=180) as response:
                raw = response.read(4_000_001)
            if len(raw) > 4_000_000:
                raise LocalExtractionUnavailable("Oversized local extraction response")
            return json.loads(raw)
        except (OSError, ValueError) as exc:
            raise LocalExtractionUnavailable(
                "Local extraction unavailable or invalid response"
            ) from exc

    def extract(self, observation):
        prompt, rules = extraction_prompt(observation, [])
        prompt = prompt.replace("at most 40", "at most 8")
        prompt = (
            "For this local pilot always return null valid_from, valid_to and supersedes. "
            "Keep dates as values, never as validity.\n" + prompt
        )
        schema = Extraction.model_json_schema()
        # Some installed runners reject bounded-string schema grammars. JSON mode
        # keeps transport generation compatible; Pydantic enforces the full schema.
        prompt = (
            "Return exactly one JSON object conforming to this schema:\n"
            + json.dumps(schema)
            + "\n"
            + prompt
        )
        # UTF-8 bytes are a conservative upper bound on byte-fallback input tokens.
        # Include schema, template margin and the entire reserved output; never truncate.
        if len(prompt.encode()) + 1024 + OUTPUT > CONTEXT:
            raise LocalExtractionUnavailable("Source section exceeds the safe local context budget")
        if self.runtime.digest() != self.digest:
            raise LocalExtractionUnavailable("Local model changed; extraction stopped")
        result = self.generate(
            {
                "model": self.model,
                "prompt": prompt,
                "format": "json",
                "stream": False,
                "think": False,
                "keep_alive": "1m",
                "options": {
                    "num_ctx": CONTEXT,
                    "num_predict": OUTPUT,
                    "num_thread": 2,
                    "temperature": 0,
                    "seed": 7,
                },
            }
        )
        if self.runtime.digest() != self.digest:
            raise LocalExtractionUnavailable("Local model changed; result discarded")
        if (
            not isinstance(result, dict)
            or result.get("done") is not True
            or result.get("done_reason") != "stop"
            or result.get("model") != self.model
        ):
            raise LocalExtractionUnavailable("Incomplete or unexpected local generation")
        try:
            extraction = Extraction.model_validate_json(result["response"])
        except (KeyError, ValueError, TypeError) as exc:
            raise LocalExtractionUnavailable(
                "Local model returned invalid structured facts"
            ) from exc
        if len(extraction.facts) > 8 or any(f.supersedes for f in extraction.facts):
            raise LocalExtractionUnavailable("Local facts exceed the requested scope")
        extraction = align_extraction(observation["content"], extraction)
        candidates = [
            {"fact_index": i, "valid_from": f.valid_from, "valid_to": f.valid_to}
            for i, f in enumerate(extraction.facts)
            if f.valid_from or f.valid_to
        ]
        extraction = Extraction(
            facts=[
                f.model_copy(update={"valid_from": None, "valid_to": None})
                for f in extraction.facts
            ]
        )
        self.last_extraction_provenance = {
            "engine": "local Ollama",
            "requested_model": self.model,
            "model_digest": self.digest,
            "rules": rules,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "prompt_eval_count": result.get("prompt_eval_count"),
            "eval_count": result.get("eval_count"),
            "cloud_calls": 0,
            "temporal_policy": "validity_unknown",
            "unvalidated_temporal_candidates": candidates,
        }
        return extraction


def extract_local(
    store, *, model="qwen3:8b", observation_id=None, project=None, max_sections=10, client=None
):
    if not 1 <= max_sections <= 1000:
        raise ValueError("max_sections must be between 1 and 1000")
    if not observation_id and not project:
        raise ValueError("Select one observation or an explicit project")
    client = client or LocalExtractor(model)
    version = client.version
    if observation_id:
        ids = [store.observation(observation_id)["id"]]
    else:
        with store.connect() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM available_observations WHERE source='local' AND project=? "
                    "ORDER BY length(content),observed_at,storage_rowid",
                    (project,),
                )
            ]
    store.set_runtime_policy("active_local_extractor", version)
    summary = {
        "local_model_calls": 0,
        "sections_completed": 0,
        "observations_completed": 0,
        "facts": 0,
        "errors": [],
        "cloud_calls": 0,
        "state": "complete",
    }
    for oid in ids:
        observation = store.observation(oid)
        if project is not None and observation["project"] != project:
            raise ValueError("Observation is outside the selected project")
        if observation["source"] != "local":
            raise ValueError("Local extraction pilot currently supports local documents only")
        if store.extraction_state(oid, version) == "complete":
            continue
        store.prepare_extractions(version, oid, section_chars=4000)
        try:
            for part in store.extraction_parts(oid, version):
                if part["state"] == "complete":
                    continue
                if summary["local_model_calls"] >= max_sections:
                    summary["state"] = "partial"
                    return summary
                section = {
                    **observation,
                    "content": observation["content"][part["start_offset"] : part["end_offset"]],
                    "section": {
                        k: part[k] for k in ("label", "start_offset", "end_offset", "planner")
                    },
                    "document_context": observation["content"][:1200]
                    if part["start_offset"]
                    else None,
                }
                summary["local_model_calls"] += 1
                extraction = client.extract(section)
                store.save_extraction_part(
                    oid,
                    version,
                    part["part_id"],
                    extraction.facts,
                    provenance=client.last_extraction_provenance,
                )
                summary["sections_completed"] += 1
            summary["facts"] += len(store.publish_extraction_parts(oid, version))
            summary["observations_completed"] += 1
        except (ValueError, RuntimeError) as exc:
            store.extraction_failed(oid, version)
            summary["errors"].append({"observation_id": oid, "error": type(exc).__name__})
            summary["state"] = "partial"
    return summary
