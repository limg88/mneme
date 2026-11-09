import copy
import json

import pytest

from work_memory.jira_history import EXTRACTOR, derive_history, normalize_history
from work_memory.models import FactInput, Observation, Target
from work_memory.store import Store

TARGET = Target(
    source="jira",
    source_id="DEMO-1",
    project="pilot",
    source_url="https://example.atlassian.net/browse/DEMO-1",
    cloud_id="company",
)


def entry(*, total=2, status="3", second_from="2", second_time="2026-02-01T10:00:00Z"):
    return {
        "tool": "mcp__jira__getJiraIssue",
        "input": {
            "cloudId": "company",
            "issueIdOrKey": "DEMO-1",
            "updateHistory": False,
            "expand": "changelog",
        },
        "response": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "issues": {
                                "nodes": [
                                    {
                                        "key": "DEMO-1",
                                        "fields": {
                                            "updated": "2026-03-01T00:00:00Z",
                                            "created": "2025-01-01",
                                            "status": {"id": status},
                                            "duedate": None,
                                        },
                                        "changelog": {
                                            "startAt": 0,
                                            "total": total,
                                            "histories": [
                                                {
                                                    "id": "11",
                                                    "author": {"email": "private@example.test"},
                                                    "created": "2026-01-01T10:00:00Z",
                                                    "items": [
                                                        {
                                                            "fieldId": "status",
                                                            "from": "1",
                                                            "to": "2",
                                                            "fromString": "Open",
                                                            "toString": "Doing",
                                                        },
                                                        {
                                                            "fieldId": "customfield_123",
                                                            "field": "status",
                                                            "to": "secret",
                                                        },
                                                    ],
                                                },
                                                {
                                                    "id": "12",
                                                    "created": second_time,
                                                    "items": [
                                                        {
                                                            "fieldId": "status",
                                                            "from": second_from,
                                                            "to": "3",
                                                            "fromString": "Doing",
                                                            "toString": "Done",
                                                        }
                                                    ],
                                                },
                                            ],
                                        },
                                    }
                                ]
                            }
                        }
                    ),
                }
            ]
        },
    }


def import_history(store, data, observed="2026-03-02", known="2026-03-03"):
    oid = store.ingest(normalize_history(data, TARGET), now=observed)["id"]
    derive_history(store, oid, now=known)
    return oid


def native(store, **kwargs):
    return [
        f
        for f in store.facts(project="pilot", limit=100, **kwargs)["items"]
        if f["predicate"] == "jira_status_id"
    ]


def test_native_intervals_late_knowledge_exact_evidence(tmp_path):
    store = Store(tmp_path)
    oid = import_history(store, entry())
    assert native(store, valid_at="2026-01-15", known_at="2026-03-02") == []
    old = native(store, valid_at="2026-01-15", known_at="2026-03-04")
    assert len(old) == 1 and old[0]["value"] == "2" and old[0]["definitive"]
    assert old[0]["valid_to"].startswith("2026-02-01")
    current = native(store, valid_at="2026-02-01T10:00:00Z", known_at="2026-03-04")
    assert len(current) == 1 and current[0]["value"] == "3"
    assert native(store, valid_at="2025-12-01", known_at="2026-03-04") == []
    observation = store.observation(oid)
    assert (
        "private@example" not in observation["content"] and "secret" not in observation["content"]
    )
    for fact in old + current:
        assert observation["content"][fact["start_offset"] : fact["end_offset"]] == fact["quote"]
    assert derive_history(store, oid) == 0


@pytest.mark.parametrize(
    "options",
    [{"total": 3}, {"second_from": "9"}, {"status": "4"}, {"second_time": "2026-01-01T10:00:00Z"}],
)
def test_gaps_never_become_dated_certainty(tmp_path, options):
    store = Store(tmp_path)
    import_history(store, entry(**options))
    facts = native(store, valid_at="2026-04-01", known_at="2026-04-01")
    assert len(facts) == 1 and facts[0]["validity_unknown"]
    assert facts[0]["certainty"] == "uncertain" and not facts[0]["definitive"]


def test_projection_dedup_and_withdrawal(tmp_path):
    store = Store(tmp_path)
    source = Observation(
        source="jira",
        source_id="DEMO-1",
        source_url=TARGET.source_url,
        project="pilot",
        title="Issue",
        content="Current issue",
    )
    normal = store.ingest(source, now="2026-03-01")
    oid = import_history(store, entry())
    assert store.ingest(source, now="2026-03-04")["id"] == normal["id"]
    assert store.ingest(normalize_history(entry(), TARGET), now="2026-03-04")["id"] == oid
    store.revoke_source("jira", "DEMO-1", "Confirmed removal")
    for candidate in (normal["id"], oid):
        with pytest.raises(KeyError):
            store.observation(candidate)
    assert native(store) == []


def test_append_revision_preserves_known_history_and_updates_old_interval(tmp_path):
    store = Store(tmp_path)
    old_oid = import_history(store, entry())
    updated = entry()
    payload = json.loads(updated["response"]["content"][0]["text"])
    issue = payload["issues"]["nodes"][0]
    issue["fields"].update(updated="2026-04-01T00:00:00Z", status={"id": "4"})
    issue["changelog"]["total"] = 3
    issue["changelog"]["histories"].append(
        {
            "id": "13",
            "created": "2026-03-15T10:00:00Z",
            "items": [{"fieldId": "status", "from": "3", "to": "4"}],
        }
    )
    updated["response"]["content"][0]["text"] = json.dumps(payload)
    new_oid = import_history(store, updated, "2026-04-02", "2026-04-03")
    assert native(store, valid_at="2026-03-20", known_at="2026-03-25")[0]["value"] == "3"
    current = native(store, valid_at="2026-03-20", known_at="2026-04-04")
    assert len(current) == 1 and current[0]["value"] == "4" and current[0]["definitive"]
    earlier = native(store, valid_at="2026-03-10", known_at="2026-04-04")
    assert (
        len(earlier) == 1 and earlier[0]["value"] == "3" and earlier[0]["observation_id"] == new_oid
    )
    assert earlier[0]["valid_to"].startswith("2026-03-15")
    assert store.extraction_state(old_oid, EXTRACTOR) == "complete"


def test_incomplete_revision_retires_old_open_certainty_without_rewriting_history(tmp_path):
    store = Store(tmp_path)
    import_history(store, entry())
    import_history(store, entry(total=10), "2026-04-02", "2026-04-03")
    old = native(store, valid_at="2026-04-01", known_at="2026-03-25")
    assert old[0]["definitive"]
    new = native(store, valid_at="2026-04-01", known_at="2026-04-04")
    assert len(new) == 1 and not new[0]["definitive"] and new[0]["validity_unknown"]


def test_request_binding_and_atomic_revision_validation(tmp_path):
    invalid = copy.deepcopy(entry())
    invalid["input"]["cloudId"] = "other"
    with pytest.raises(ValueError):
        normalize_history(invalid, TARGET)
    store = Store(tmp_path)
    old_oid = import_history(store, entry())
    newer = store.ingest(normalize_history(entry(total=10), TARGET), now="2026-04-02")["id"]
    with pytest.raises(ValueError):
        store.add_facts(
            newer,
            [
                FactInput(
                    subject="DEMO-1", predicate="jira_status_id", value="bad", quote="invented"
                )
            ],
            EXTRACTOR,
            now="2026-04-03",
            replace_native_projection=True,
        )
    assert native(store, known_at="2026-04-04")[0]["observation_id"] == old_oid


def test_native_collection_resumes_without_repeating_completed_history(tmp_path):
    from work_memory.collectors import JIRA_SEARCH, Collector
    from work_memory.models import CollectionScope
    from work_memory.worker import TOOLS, CloudBudgetExceeded

    store = Store(tmp_path)
    scope = CollectionScope(
        source="jira",
        project="pilot",
        project_key="DEMO",
        cloud_id="company",
        base_url="https://example.atlassian.net",
        page_size=2,
        max_pages_per_sync=1,
        native_history=True,
    )
    data = json.loads(entry()["response"]["content"][0]["text"])
    original = data["issues"]["nodes"][0]
    second = copy.deepcopy(original)
    second["key"] = "DEMO-2"
    page = {"issues": {"nodes": [original, second], "pageInfo": {"hasNextPage": False}}}

    class Client:
        def __init__(self):
            self.reads = []
            self.fail = True

        def request(self, tool, arguments):
            if tool == JIRA_SEARCH:
                return {"response": {"content": json.dumps(page)}}
            assert tool == TOOLS["jira"] and arguments["updateHistory"] is False
            key = arguments["issueIdOrKey"]
            self.reads.append(key)
            if key == "DEMO-2" and self.fail:
                raise CloudBudgetExceeded("stop")
            issue = copy.deepcopy(original)
            issue["key"] = key
            return {
                "tool": tool,
                "input": arguments,
                "response": {"content": json.dumps({"issues": {"nodes": [issue]}})},
            }

    client = Client()
    with pytest.raises(CloudBudgetExceeded):
        Collector(store, client).collect(scope)
    assert len(store.active_observation_ids()) == 3  # Both bodies and first native history.
    client.fail = False
    assert Collector(store, client).collect(scope)["state"] == "complete"
    assert client.reads == ["DEMO-1", "DEMO-2", "DEMO-2"]
    assert len(store.active_observation_ids()) == 4
    assert derive_history(store, now="2027-01-01") == 6


def test_native_history_status_does_not_require_ai_extraction(tmp_path):
    store = Store(tmp_path)
    store.set_runtime_policy("active_extractor", "evidence-v2:haiku")
    import_history(store, entry())
    assert store.status()["observations_without_completed_extraction"] == 0
    items = native(store, known_at="2026-04-01")
    assert items and all(f["current_extraction_state"] == "complete" for f in items)


def test_new_provider_version_requires_history_refresh_for_unbounded_current_state(tmp_path):
    store = Store(tmp_path)
    import_history(store, entry())
    store.ingest(
        Observation(
            source="jira",
            source_id="DEMO-1",
            project="pilot",
            source_url=TARGET.source_url,
            title="Current issue",
            content="Changed provider snapshot",
            source_updated_at="2026-04-01T10:00:00+0100",
        ),
        now="2026-04-02",
    )
    before = native(store, valid_at="2026-03-10", known_at="2026-03-20")[0]
    assert before["definitive"] and not before["pending_history_analysis"]
    now = native(store, valid_at="2026-03-10", known_at="2026-04-03")[0]
    assert not now["definitive"] and now["pending_history_analysis"]
    past = native(store, valid_at="2026-02-15", known_at="2026-04-03")[0]
    assert past["definitive"] and not past["pending_history_analysis"]
