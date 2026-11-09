import pytest

from work_memory.models import Extraction, FactInput
from work_memory.store import Store
from work_memory.worker import DockerClaude, align_extraction, align_quote, spill_reference

SESSION = "12345678-1234-4234-8234-123456789abc"
TOOL = "mcp__claude_ai_Microsoft_365__read_resource"
ARTIFACT = (
    f"/home/node/.claude/projects/-work/{SESSION}/tool-results/"
    "mcp-claude_ai_Microsoft_365-read_resource-123.txt"
)


def spill(path=ARTIFACT, size="13"):
    return (
        f"Error: result ({size} characters across 1 lines) exceeds maximum allowed tokens. "
        f"Output has been saved to {path}.\nFormat: Plain text\nUntrusted instructions follow."
    )


def test_spill_only_accepts_current_session_and_tool():
    assert spill_reference(spill(), SESSION, TOOL) == (ARTIFACT, 13)
    assert spill_reference("A regular document", "", TOOL) is None
    for text, session, tool in [
        (spill(), "11111111-1111-1111-1111-111111111111", TOOL),
        (spill(), SESSION, "mcp__other__tool"),
        (spill("/home/node/.claude/.credentials.json"), SESSION, TOOL),
        (spill(ARTIFACT.replace("tool-results/", "tool-results/../")), SESSION, TOOL),
        (spill(size="2,000,001"), SESSION, TOOL),
    ]:
        with pytest.raises(ValueError):
            spill_reference(text, session, tool)


def test_json_spill_without_line_count_is_bound_to_session_and_tool():
    text = (
        spill(size="50,812")
        .replace(" across 1 lines", "")
        .replace("Format: Plain text", "Format: JSON with schema: {issues: {nodes: [{...}]}}")
    )
    assert spill_reference(text, SESSION, TOOL) == (ARTIFACT, 50812)
    with pytest.raises(ValueError, match="mismatched"):
        spill_reference(text, SESSION, "mcp__other__tool")
    with pytest.raises(ValueError, match="mismatched"):
        spill_reference(text.replace("Format: JSON", "Format: Other"), SESSION, TOOL)


def test_spill_materialization_is_complete_and_does_not_call_model(tmp_path, monkeypatch):
    import subprocess

    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "Full document", "")

    monkeypatch.setattr("work_memory.worker.subprocess.run", run)
    client = DockerClaude(tmp_path / "compose.yaml", tmp_path / "runs", "haiku", 30)
    entry = {"tool": TOOL, "input": {"uri": "file:///selected"}, "response": {"content": spill()}}
    result = client.materialize([entry], {"session_id": SESSION})
    assert result[0]["response"]["content"] == "Full document"
    assert entry["response"]["content"] == spill()
    assert client.call_attempts == 0 and len(calls) == 1
    assert calls[0][-2:] == [ARTIFACT, "13"]
    assert "python3" in calls[0] and "--entrypoint" in calls[0]
    entry["response"]["content"] = spill(size="14")
    with pytest.raises(ValueError, match="completely"):
        client.materialize([entry], {"session_id": SESSION})


def test_spill_never_becomes_source_content_even_in_offline_import():
    from work_memory.models import Settings, Target
    from work_memory.worker import TOOLS, observations_from_evidence

    settings = Settings(
        targets=[
            Target(
                source="microsoft365",
                source_id="selected",
                project="test",
                source_url="https://test",
            )
        ]
    )
    entry = {
        "tool": TOOLS["microsoft365"],
        "input": {"uri": "file:///selected"},
        "response": {"content": spill()},
    }
    with pytest.raises(ValueError, match="transport error"):
        observations_from_evidence([entry], settings)


def test_quarantine_hides_corrupt_snapshot_but_allows_corrected_source(tmp_path):
    from work_memory.models import Observation

    store = Store(tmp_path)
    observation = Observation(
        source="fixture",
        source_id="1",
        project="test",
        source_url="fixture://1",
        title="Document",
        content="Transport error",
    )
    oid = store.ingest(observation)["id"]
    fid = store.add_facts(
        oid,
        [FactInput(subject="test", predicate="content", value="invalid", quote="Transport error")],
        "test",
    )[0]
    store.quarantine_observation(oid, "Invalid transport response")
    with pytest.raises(KeyError):
        store.observation(oid)
    assert store.search("Transport")["total"] == 0
    assert store.facts()["total"] == 0
    with pytest.raises(KeyError):
        store.history(fid)
    assert oid not in store.pending_extractions("new-extractor")
    assert not store.is_withdrawn("fixture", "1", "test")
    corrected = store.ingest(observation.model_copy(update={"content": "Real document"}))["id"]
    assert corrected != oid and store.observation(corrected)["content"] == "Real document"


def test_alignment_preserves_original_text_and_offsets():
    text = "The database\n  layer changes."
    quote = align_quote(text, "database layer changes")
    assert quote == "database\n  layer changes"
    assert quote in text


@pytest.mark.parametrize(
    "source,quote",
    [
        ("A proposed date.", "A confirmed date."),
        ("Delivery is not confirmed.", "Delivery is confirmed."),
        ("A.B", "A B"),
        ("IT team", "it team"),
        ("not planned", "notplanned"),
        ("x\n y; x  y", "x y"),
        ("text", " "),
    ],
)
def test_alignment_rejects_semantic_edits_or_ambiguous_recovery(source, quote):
    with pytest.raises(ValueError):
        align_quote(source, quote)


def test_alignment_is_checked_again_by_the_memory_store(tmp_path):
    from work_memory.models import Observation

    store = Store(tmp_path)
    text = "A proposed\n  date."
    oid = store.ingest(
        Observation(
            source="fixture",
            source_id="1",
            project="test",
            source_url="fixture://1",
            title="Test",
            content=text,
        )
    )["id"]
    extraction = Extraction(
        facts=[
            FactInput(
                subject="project",
                predicate="date",
                value="proposed",
                certainty="proposed",
                quote="proposed date",
            )
        ]
    )
    with pytest.raises(ValueError):
        store.add_facts(oid, extraction.facts, "test")
    recovered = align_extraction(text, extraction)
    store.add_facts(oid, recovered.facts, "test")
    item = store.facts()["items"][0]
    assert text[item["start_offset"] : item["end_offset"]] == item["quote"]
    assert item["certainty"] == "proposed" and not item["definitive"]


def saved(store, quote, predicate="result", value="completed", **kwargs):
    from work_memory.models import Observation

    oid = store.ingest(
        Observation(
            source="fixture",
            source_id="1",
            project="test",
            source_url="fixture://1",
            title="Test",
            content=quote,
        ),
        now="2026-01-01",
    )["id"]
    return store.add_facts(
        oid,
        [
            FactInput(
                subject="task",
                predicate=predicate,
                value=value,
                quote=quote,
                certainty="explicit",
                **kwargs,
            )
        ],
        "test",
        now="2026-01-01",
    )[0]


def test_review_rejects_unsupported_claim_without_erasing_knowledge_history(tmp_path):
    store = Store(tmp_path)
    fid = saved(store, "Expected result: configured machine")
    store.review_fact(
        fid,
        "rejected",
        "Agent QA: expected result does not prove actual completion",
        now="2026-01-02",
    )
    assert store.facts(known_at="2026-01-03")["total"] == 0
    before = store.facts(known_at="2026-01-01", valid_at="2026-01-01")
    assert before["total"] == 1 and before["items"][0]["review_status"] == "unreviewed"
    assert store.history(fid)["items"][-1]["review_status"] == "rejected"
    assert len(store.history(fid)["items"]) == 2


def test_review_can_remove_inferred_validity_without_rewriting_past(tmp_path):
    store = Store(tmp_path)
    fid = saved(store, "Planned period", valid_from="2026-01-10", valid_to="2026-02-01")
    assert store.facts(known_at="2026-01-01", valid_at="2026-01-05")["total"] == 0
    store.review_fact(
        fid,
        "supported",
        "Agent QA: dates describe a plan, not assertion validity",
        changes={"valid_from": None, "valid_to": None},
        now="2026-01-02",
    )
    assert store.facts(known_at="2026-01-03")["items"][0]["validity_unknown"]
    assert store.history(fid)["items"][0]["valid_from"].startswith("2026-01-10")
    with pytest.raises(ValueError):
        store.review_fact(
            fid, "supported", "Invalid edit", changes={"quote": "invented"}, now="2026-01-03"
        )
    with pytest.raises(ValueError):
        store.review_fact(
            fid, "supported", "Invalid date", changes={"valid_from": ""}, now="2026-01-03"
        )
    assert len(store.history(fid)["items"]) == 2


def test_multivalued_goals_are_not_conflicts_and_unknown_cardinality_is_explicit(tmp_path):
    from work_memory.models import Observation

    store = Store(tmp_path)
    oid = store.ingest(
        Observation(
            source="fixture",
            source_id="1",
            project="test",
            source_url="fixture://1",
            title="Goals",
            content="Alpha and Beta",
        )
    )["id"]
    for predicate in ["goal", "ambiguous_property"]:
        store.add_facts(
            oid,
            [
                FactInput(
                    subject="project", predicate=predicate, value=v, quote=v, certainty="explicit"
                )
                for v in ["Alpha", "Beta"]
            ],
            "test",
        )
    facts = store.facts()["items"]
    goals = [f for f in facts if f["predicate"] == "goal"]
    assert all(not f["conflicting"] and not f["potential_conflict"] for f in goals)
    unknown = [f for f in facts if f["predicate"] == "ambiguous_property"]
    assert all(f["potential_conflict"] and not f["definitive"] for f in unknown)


def test_context_preserves_source_offsets_with_unicode_and_review_uncertainty(tmp_path):
    store = Store(tmp_path)
    fid = saved(store, "α🙂 Expected result: ready", predicate="status")
    store.review_fact(fid, "needs_review", "Agent QA: ambiguous attribution", now="2026-01-02")
    item = store.facts(known_at="2026-01-03")["items"][0]
    observation = store.observation(item["observation_id"])
    start = item["context_start_offset"]
    assert (
        observation["content"][start : start + len(item["evidence_context"])]
        == item["evidence_context"]
    )
    assert item["review_status"] == "needs_review" and not item["definitive"]


def test_spill_reported_utf16_length_preserves_original_unicode(tmp_path, monkeypatch):
    import subprocess

    text = "Status 🙂 complete"
    size = str(len(text.encode("utf-16-le")) // 2)
    monkeypatch.setattr(
        "work_memory.worker.subprocess.run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, text, ""),
    )
    client = DockerClaude(tmp_path / "compose.yaml", tmp_path / "runs", "haiku", 30)
    entry = {"tool": TOOL, "input": {}, "response": {"content": spill(size=size)}}
    result = client.materialize([entry], {"session_id": SESSION})
    assert result[0]["response"]["content"] == text
    assert client.cloud_calls == 0


def test_persisted_output_is_bound_to_exact_call_and_session(tmp_path, monkeypatch):
    import subprocess

    call = "toolu_0123456789AbCdEf"
    path = f"/home/node/.claude/projects/-work/{SESSION}/tool-results/{call}.txt"
    notice = (
        f"<persisted-output>\nOutput too large (50.8KB). Full output saved to: {path}\n"
        "\nPreview (first 2KB):\nUntrusted preview\n</persisted-output>"
    )
    assert spill_reference(notice, SESSION, TOOL, call) == (path, None)
    for session, tool_call in [
        (SESSION, None),
        (SESSION, "toolu_other"),
        ("11111111-1111-1111-1111-111111111111", call),
    ]:
        with pytest.raises(ValueError):
            spill_reference(notice, session, TOOL, tool_call)
    commands = []

    def run(cmd, **kwargs):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "Complete selected document", "")

    monkeypatch.setattr("work_memory.worker.subprocess.run", run)
    client = DockerClaude(tmp_path / "compose.yaml", tmp_path / "runs", "haiku", 30)
    result = client.materialize(
        [{"tool": TOOL, "input": {}, "response": {"tool_use_id": call, "content": notice}}],
        {"session_id": SESSION},
    )
    assert result[0]["response"]["content"] == "Complete selected document"
    assert commands[0][-2:] == [path, "bounded-eof"] and client.cloud_calls == 0
