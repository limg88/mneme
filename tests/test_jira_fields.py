import json

from work_memory.jira_fields import EXTRACTOR, derive_fields
from work_memory.models import Observation
from work_memory.store import Store


def observe(store, status="Open", updated="2026-01-01T00:00:00Z", now=None, presence=True):
    data = {
        "summary": "Expected result: delivery",
        "description": "Expected result does not prove delivery.",
        "status": {"name": status},
        "assignee": {"displayName": "Mario Rossi", "accountId": "account-a"},
        "duedate": None,
        "updated": updated,
    }
    return store.ingest(
        Observation(
            source="jira",
            source_id="P-1",
            project="pilot",
            source_url="https://example.atlassian.net/browse/P-1",
            title="Task",
            content=json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2),
            source_updated_at=updated,
            metadata={
                "tool": "mcp__claude_ai_Atlassian_Rovo__getJiraIssue",
                **({"provider_fields_present": list(data)} if presence else {}),
            },
        ),
        now=now,
    )["id"]


def test_literal_fields_exact_quotes_no_business_completion_or_invented_dates(tmp_path):
    store = Store(tmp_path)
    oid = observe(store)
    assert derive_fields(store, oid) == 5
    facts = store.facts(limit=100)["items"]
    o = store.observation(oid)
    assert all(f["valid_from"] is None and not f["definitive"] for f in facts)
    assert all(o["content"][f["start_offset"] : f["end_offset"]] == f["quote"] for f in facts)
    assert {f["predicate"] for f in facts} == {
        "jira_reported_summary",
        "jira_reported_status",
        "jira_reported_assignee_id",
        "jira_reported_due_date",
        "jira_provider_account_id",
    }
    assert next(f for f in facts if f["kind"] == "person")["subject_identity"]["resolved"]
    assert store.status()["observations_without_completed_extraction"] == 0
    assert derive_fields(store, oid) == 0


def test_snapshot_update_preserves_known_history_and_person_identity(tmp_path):
    store = Store(tmp_path)
    a = observe(store, now="2026-01-01")
    derive_fields(store, a)
    old = next(
        f for f in store.facts(limit=100)["items"] if f["predicate"] == "jira_reported_status"
    )
    b = observe(store, "Closed", "2026-02-01T00:00:00Z", now="2026-02-02")
    derive_fields(store, b)
    current = [
        f for f in store.facts(limit=100)["items"] if f["predicate"] == "jira_reported_status"
    ]
    assert len(current) == 1 and current[0]["value"] == "Closed"
    history = store.history(old["id"])["items"]
    assert history[0]["known_to"] is not None
    assert len([f for f in store.facts(limit=100)["items"] if f["kind"] == "person"]) == 2


def test_stale_provider_snapshot_does_not_win_and_missing_is_not_null(tmp_path):
    store = Store(tmp_path)
    oid = observe(store, updated="2026-02-01T00:00:00Z", now="2026-02-02")
    derive_fields(store, oid)
    late = observe(store, "Old", "2026-01-01T00:00:00Z", now="2026-03-01", presence=False)
    derive_fields(store, late)
    current = [
        f for f in store.facts(limit=100)["items"] if f["predicate"] == "jira_reported_status"
    ]
    assert len(current) == 2 and all(f["conflicting"] for f in current)
    assert not any(
        f["observation_id"] == late and f["predicate"] == "jira_reported_due_date"
        for f in store.facts(limit=100)["items"]
    )


def test_older_snapshot_derived_late_cannot_replace_newer_at_same_observed_time(tmp_path):
    store = Store(tmp_path)
    a = observe(store, now="2026-03-01")
    b = observe(store, "Closed", "2026-02-01T00:00:00Z", now="2026-03-01")
    derive_fields(store, b)
    derive_fields(store, a)
    current = [
        f for f in store.facts(limit=100)["items"] if f["predicate"] == "jira_reported_status"
    ]
    assert len(current) == 1 and current[0]["value"] == "Closed"
    assert store.extraction_state(a, EXTRACTOR) == "complete"
