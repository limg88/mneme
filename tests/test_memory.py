import asyncio
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from work_memory.api import create_app
from work_memory.mcp_server import create_server
from work_memory.models import Extraction, FactInput, Observation, Settings, Target
from work_memory.store import Store
from work_memory.worker import exclusive, observations_from_evidence, parse_events, schedule, sync


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "memory")


def observe(store, text, now="2026-01-01", source_id="meeting"):
    return store.ingest(
        Observation(
            source="fixture",
            source_id=source_id,
            project="pilot",
            source_url="fixture://meeting",
            title="Decision",
            content=text,
        ),
        now=now,
    )["id"]


def fact(value, quote, **kwargs):
    return FactInput(
        subject="pilot",
        predicate="deadline",
        value=value,
        quote=quote,
        certainty="explicit",
        **kwargs,
    )


def values(result):
    return {f["value"] for f in result["items"]}


def test_future_replacement_and_late_knowledge(store):
    oid = observe(store, "Deadline 10 Nov effective 1 Nov", "2026-11-01")
    old = store.add_facts(
        oid, [fact("10 Nov", "Deadline 10 Nov", valid_from="2026-11-01")], "test", now="2026-11-01"
    )[0]
    oid2 = observe(store, "From 5 Nov the deadline changes to 15 Nov", "2026-11-06")
    store.add_facts(
        oid2,
        [
            fact(
                "15 Nov",
                "From 5 Nov the deadline changes to 15 Nov",
                valid_from="2026-11-05",
                supersedes=old,
            )
        ],
        "test",
        now="2026-11-06",
    )
    assert values(store.facts(valid_at="2026-11-07", known_at="2026-11-04")) == {"10 Nov"}
    assert values(store.facts(valid_at="2026-11-04", known_at="2026-11-07")) == {"10 Nov"}
    assert values(store.facts(valid_at="2026-11-05", known_at="2026-11-07")) == {"15 Nov"}
    assert len(store.history(old)["items"]) == 3


def test_future_change_does_not_apply_early(store):
    a = observe(store, "Monday deadline", "2026-01-01")
    old = store.add_facts(
        a, [fact("Monday", "Monday", valid_from="2026-01-01")], "test", now="2026-01-01"
    )[0]
    b = observe(store, "From 1 Feb change to Tuesday", "2026-01-05")
    store.add_facts(
        b,
        [fact("Tuesday", "From 1 Feb change to Tuesday", valid_from="2026-02-01", supersedes=old)],
        "test",
        now="2026-01-05",
    )
    assert values(store.facts(valid_at="2026-01-31", known_at="2026-01-06")) == {"Monday"}
    assert values(store.facts(valid_at="2026-02-01", known_at="2026-01-06")) == {"Tuesday"}


def test_unknown_validity_and_paginated_conflicts_are_not_definitive(store):
    a = observe(store, "Monday or Tuesday")
    store.add_facts(a, [fact("Monday", "Monday"), fact("Tuesday", "Tuesday")], "test")
    results = store.facts(limit=1)
    assert results["total"] == 2 and results["truncated"]
    assert results["items"][0]["conflicting"]
    assert results["items"][0]["validity_unknown"]
    assert not results["items"][0]["definitive"]


def test_quote_validation_rolls_back_whole_extraction(store):
    oid = observe(store, "Monday")
    with pytest.raises(ValueError, match="substring"):
        store.add_facts(oid, [fact("Monday", "Monday"), fact("Tuesday", "Tuesday")], "test")
    assert store.facts()["total"] == 0


def test_observation_idempotence_and_return_to_previous_state(store):
    a = observe(store, "Monday", "2026-01-01")
    assert observe(store, "Monday", "2026-01-02") == a
    b = observe(store, "Tuesday", "2026-01-03")
    c = observe(store, "Monday", "2026-01-04")
    assert len({a, b, c}) == 3
    first = store.add_facts(c, [fact("Monday", "Monday")], "test")
    assert first == store.add_facts(c, [fact("Monday", "Monday")], "test")
    assert store.facts()["total"] == 1


def test_withdrawal_blocks_history_search_and_reingestion(store):
    oid = observe(store, "Monday")
    fid = store.add_facts(oid, [fact("Monday", "Monday")], "test")[0]
    assert store.search("Monday")["total"] == 1
    assert store.withdraw("fixture", "meeting", "pilot") == 1
    assert store.search("Monday")["total"] == 0
    assert store.facts()["total"] == 0
    with pytest.raises(KeyError):
        store.history(fid)
    with pytest.raises(KeyError):
        store.observation(oid)
    with pytest.raises(ValueError, match="withdrawn"):
        observe(store, "Tuesday")


def test_invalid_replacement_and_invalid_dates(store):
    oid = observe(store, "Monday")
    fid = store.add_facts(oid, [fact("Monday", "Monday")], "test")[0]
    with pytest.raises(ValueError, match="known validity"):
        store.add_facts(
            oid, [fact("Tuesday", "Monday", valid_from="2026-01-01", supersedes=fid)], "test"
        )
    with pytest.raises(ValueError):
        fact("Monday", "Monday", valid_from="2026-01-01T12:00:00")
    with pytest.raises(ValueError):
        store.facts(limit=0)


def test_api_and_mcp_use_the_same_results_and_require_http_auth(store):
    oid = observe(store, "A useful decision")
    store.add_facts(oid, [fact("decision", "useful decision")], "test")
    api = TestClient(create_app(store))
    assert api.get("/v1/search", params={"query": "decision"}).status_code == 401
    headers = {"Authorization": "Bearer " + store.token()}
    http_result = api.get("/v1/search", params={"query": "decision"}, headers=headers).json()
    assert http_result["total"] == 1
    assert api.get("/v1/search", params={"query": '"'}, headers=headers).status_code == 422
    assert api.get("/v1/observations/missing", headers=headers).status_code == 404

    async def check():
        async with Client(create_server(store)) as client:
            result = await client.call_tool("memory_search", {"query": "decision"})
            assert not result.is_error
            assert result.structured_content == http_result

    asyncio.run(check())


def test_real_stdio_transport(store):
    async def check():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "work_memory.cli", "--data", str(store.directory), "mcp"],
        )
        async with Client(params) as client:
            result = await client.call_tool("memory_status", {})
            assert not result.is_error
            assert result.structured_content["sources"] == []

    asyncio.run(check())


def test_scheduler_lock_and_local_sync_retry(store, tmp_path):
    note = tmp_path / "note.md"
    note.write_text("A selected note")
    cfg = Settings(local_paths=[str(note)])
    with exclusive(store.directory), pytest.raises(RuntimeError, match="lock"):
        sync(store, cfg, None, extract=False)
    assert sync(store, cfg, None, extract=False)["new"] == 1
    assert sync(store, cfg, None, extract=False)["new"] == 0
    note.write_text("An updated selected note")
    assert sync(store, cfg, None, extract=False)["new"] == 1
    assert store.status()["sources"][0]["versions"] == 2


def test_adapter_binds_responses_to_selected_objects_and_minimizes_personal_fields():
    cfg = Settings(
        targets=[
            Target(
                source="jira",
                source_id="P-1",
                cloud_id="cloud",
                source_url="https://example/P-1",
                project="pilot",
            )
        ]
    )
    payload = {
        "issues": {
            "nodes": [
                {
                    "key": "P-1",
                    "fields": {
                        "summary": "Title",
                        "description": "Text",
                        "assignee": {"displayName": "Test", "emailAddress": "private@example.com"},
                        "status": {"name": "Open"},
                    },
                }
            ]
        },
        "context": {"sessionId": "not-for-indexing"},
    }
    evidence = [
        {
            "tool": "mcp__claude_ai_Atlassian_Rovo__getJiraIssue",
            "input": {"cloudId": "cloud", "issueIdOrKey": "P-1"},
            "response": {"content": json.dumps(payload)},
        }
    ]
    result = observations_from_evidence(evidence, cfg)[0]
    assert "private@example.com" not in result.content
    assert "not-for-indexing" not in result.content
    evidence[0]["input"]["issueIdOrKey"] = "P-2"
    with pytest.raises(ValueError, match="Expected"):
        observations_from_evidence(evidence, cfg)


def test_no_successful_result_or_denied_permission_is_a_failure():
    with pytest.raises(ValueError):
        parse_events('{"type":"result","is_error":false,"permission_denials":[{}]}')
    with pytest.raises(ValueError):
        parse_events('{"type":"assistant"}')


def test_unchanged_content_does_not_repeat_extraction(store, tmp_path):
    note = tmp_path / "note.txt"
    note.write_text("Monday")
    cfg = Settings(local_paths=[str(note)])

    class LocalTestExtractor:
        calls = 0

        def extract(self, observation, previous):
            self.calls += 1
            return Extraction(facts=[fact("Monday", "Monday")])

    client = LocalTestExtractor()
    assert not sync(store, cfg, client)["errors"]
    assert not sync(store, cfg, client)["errors"]
    assert client.calls == 1


def test_foreground_timer_runs_immediately_and_observes_next_change(store, tmp_path, monkeypatch):
    note = tmp_path / "scheduled.md"
    note.write_text("First state")
    sleeps = []

    def next_tick(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            note.write_text("Second state")
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr("work_memory.worker.time.sleep", next_tick)
    with pytest.raises(KeyboardInterrupt):
        schedule(store, Settings(local_paths=[str(note)]), None, extract=False)
    state = store.status()
    assert len(state["recent_runs"]) == 2
    assert all(run["state"] == "complete" for run in state["recent_runs"])
    assert state["sources"][0]["versions"] == 2
    assert all(1 <= delay <= 900 for delay in sleeps)


@pytest.mark.parametrize(
    "tool,arguments,allowed",
    [
        ("mcp__claude_ai_Microsoft_365__read_resource", {"uri": "file:///drive/file"}, True),
        ("mcp__claude_ai_Microsoft_365__read_resource", {"uri": "file:///drive/other"}, False),
        (
            "mcp__claude_ai_Atlassian_Rovo__getJiraIssue",
            {
                "cloudId": "cloud",
                "issueIdOrKey": "P-1",
                "fields": ["summary"],
                "updateHistory": False,
            },
            True,
        ),
        (
            "mcp__claude_ai_Atlassian_Rovo__getJiraIssue",
            {
                "cloudId": "cloud",
                "issueIdOrKey": "P-1",
                "fields": ["summary"],
                "updateHistory": True,
            },
            False,
        ),
        ("Bash", {"command": "anything"}, False),
        (
            "mcp__claude_ai_Microsoft_365__sharepoint_search",
            {"query": "selected-folder", "limit": 1, "offset": 0},
            True,
        ),
        (
            "mcp__claude_ai_Microsoft_365__sharepoint_search",
            {"query": "entire-tenant", "limit": 1, "offset": 0},
            False,
        ),
    ],
)
def test_worker_scope_is_enforced_before_tool_execution(tmp_path, tool, arguments, allowed):
    scope = [
        {"source": "microsoft365", "source_id": "drive/file"},
        {"source": "jira", "source_id": "P-1", "cloud_id": "cloud"},
        {
            "source": "request",
            "tool": "mcp__claude_ai_Microsoft_365__sharepoint_search",
            "input": {"query": "selected-folder", "limit": 1, "offset": 0},
        },
    ]
    encoded = base64.b64encode(json.dumps(scope).encode()).decode()
    guard = Path(__file__).resolve().parents[1] / "docker/claude-probe/guard.py"
    audit = tmp_path / "audit.jsonl"
    result = subprocess.run(
        [sys.executable, str(guard), encoded],
        input=json.dumps({"tool_name": tool, "tool_input": arguments}),
        text=True,
        capture_output=True,
        env={**os.environ, "WORK_MEMORY_GUARD_AUDIT": str(audit)},
        timeout=5,
    )
    assert result.returncode == (0 if allowed else 2)
    assert json.loads(audit.read_text())["allowed"] == allowed


def test_bounded_scheduler_stops_after_two_cycles_without_final_sleep(store, monkeypatch, capsys):
    from work_memory.worker import SynchronizationLocked

    calls = []
    delays = []

    def tick(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise SynchronizationLocked("held")
        return {"state": "complete"}

    monkeypatch.setattr("work_memory.worker.sync", tick)
    monkeypatch.setattr("work_memory.worker.time.monotonic", lambda: 0)
    monkeypatch.setattr("work_memory.worker.time.sleep", delays.append)
    schedule(store, Settings(), None, extract=False, cycles=2)
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(calls) == 2 and delays == [900]
    assert [r["state"] for r in output] == ["skipped_locked", "complete"]
    assert [r["cycle"] for r in output] == [1, 2]
    assert all(r["started_at"] for r in output)
    with pytest.raises(ValueError, match="cycles"):
        schedule(store, Settings(), None, cycles=0)


def test_scheduler_does_not_label_runtime_failures_as_locks(store, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise RuntimeError("private diagnostic")

    monkeypatch.setattr("work_memory.worker.sync", fail)
    schedule(store, Settings(), None, extract=False, cycles=1)
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "failed" and result["error_type"] == "RuntimeError"
    assert "private diagnostic" not in json.dumps(result)


def test_status_paginates_details_without_exposing_internal_checkpoint_cache(store):
    for n in range(25):
        store.ingest(
            Observation(
                source="fixture",
                source_id=str(n),
                project="pilot",
                source_url="fixture://" + str(n),
                title="Fixture",
                content="Synthetic status pagination",
            )
        )
    store.save_collection_state(
        "fixture",
        {
            "state": "incomplete",
            "verified": {str(n): "private cache" for n in range(500)},
            "seen_cursors": ["opaque-token"],
            "calendar_windows": [["2026-01-01", "2026-02-01"]],
        },
    )
    assert len(store.status()["sources"]) == 10
    first = store.status(detail_limit=20)
    second = store.status(detail_limit=20, detail_offset=20)
    assert len(first["sources"]) == 20 and len(second["sources"]) == 5
    assert first["source_pagination"] == {"total": 25, "limit": 20, "offset": 0, "truncated": True}
    assert second["source_pagination"]["truncated"] is False
    assert first["source_summary"]["fixture"] == {"objects": 25, "observations": 25}
    checkpoint = first["collection_scopes"][0]["state"]
    assert checkpoint["verified_count"] == 500 and checkpoint["seen_cursors_count"] == 1
    assert "private cache" not in json.dumps(first) and "opaque-token" not in json.dumps(first)
    assert len(store.collection_state("fixture")["verified"]) == 500
    assert not (
        {s["source_id"] for s in first["sources"]} & {s["source_id"] for s in second["sources"]}
    )
