import json

import pytest

from work_memory.collectors import (
    Collector,
    jira_page,
    scope_id,
    sharepoint_page,
    within_folder,
)
from work_memory.models import CollectionScope, Settings, folder_url
from work_memory.store import Store
from work_memory.worker import CloudBudgetExceeded, CorporateLoginRequired, DockerClaude, sync


def test_expired_login_stops_other_remote_scopes_without_revoking_sources(tmp_path):
    store = Store(tmp_path)
    client = FakeClient([CorporateLoginRequired()])
    settings = Settings(
        scopes=[jira_scope(), jira_scope().model_copy(update={"project": "second"})]
    )
    result = sync(store, settings, client, extract=True)
    assert result["reauthentication_required"] and result["state"] == "partial"
    assert len(client.requests) == 1
    assert store.status()["sources"] == []
    assert store.status()["cloud_authentication"]["state"] == "renewal_required"


def entry(data, multiple=False):
    content = (
        [{"type": "text", "text": json.dumps(x)} for x in data] if multiple else json.dumps(data)
    )
    return {"response": {"content": content}}


def jira(items, next_cursor=None):
    return entry(
        {
            "issues": {
                "nodes": items,
                "pageInfo": {
                    "hasNextPage": next_cursor is not None,
                    "endCursor": next_cursor,
                },
            }
        }
    )


def issue(key, description="First state"):
    return {
        "key": key,
        "fields": {"summary": key, "description": description, "updated": "2026-11-01T10:00:00Z"},
    }


def jira_scope(**kwargs):
    return CollectionScope(
        source="jira",
        project="pilot",
        project_key="P",
        cloud_id="cloud",
        base_url="https://example.atlassian.net",
        page_size=1,
        max_pages_per_sync=1,
        **kwargs,
    )


def sp_scope():
    return CollectionScope(
        source="sharepoint",
        project="pilot",
        folder_url="https://example.sharepoint.com/sites/work/folder",
        page_size=2,
        max_pages_per_sync=1,
    )


def hit(number):
    return {
        "uri": f"file:///drive/{number}",
        "name": f"{number}.docx",
        "webUrl": f"https://example.sharepoint.com/sites/work/folder/{number}.docx",
        "lastModifiedDateTime": "2026-11-01T10:00:00Z",
    }


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, tool, arguments):
        self.requests.append((tool, arguments))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return {**result, "tool": tool, "input": arguments}


def test_folder_link_is_normalized_without_view_or_user_parameters():
    link = (
        "https://example.sharepoint.com/sites/work/docs/Forms/AllItems.aspx?"
        "id=%2Fsites%2Fwork%2Fdocs%2FSelected%20Folder&ovuser=private&viewid=irrelevant"
    )
    assert folder_url(link) == "https://example.sharepoint.com/sites/work/docs/Selected%20Folder"
    assert within_folder(
        "https://example.sharepoint.com/sites/work/docs/Selected Folder/a.docx", folder_url(link)
    )
    assert not within_folder(
        "https://example.sharepoint.com/sites/work/docs/Selected Folder-other/a", folder_url(link)
    )
    assert not within_folder(
        "https://example.sharepoint.com/sites/work/docs/Selected Folder/../other", folder_url(link)
    )
    with pytest.raises(ValueError):
        folder_url("https://example.sharepoint.com/sites/work/docs/Forms/AllItems.aspx")


def test_jira_cursor_resume_keeps_time_window_and_advances_only_after_last_page(tmp_path):
    store, scope = Store(tmp_path), jira_scope()
    client = FakeClient([jira([issue("P-1")], "next"), jira([issue("P-2")])])
    first = Collector(store, client).collect(scope)
    pending = store.collection_state(scope_id(scope))
    assert first["state"] == "scanning" and not pending.get("watermark")
    upper = pending["scan_upper"]
    second = Collector(store, client).collect(scope)
    assert second["state"] == "complete"
    assert client.requests[0][1]["jql"] == client.requests[1][1]["jql"]
    assert client.requests[1][1]["nextPageToken"] == "next"
    state = store.collection_state(scope_id(scope))
    assert state["watermark"] == upper and state["scan_upper"] is None
    assert len(store.status()["sources"]) == 2


def test_bad_pagination_or_cross_project_results_do_not_advance_checkpoint(tmp_path):
    store, scope = Store(tmp_path), jira_scope()
    client = FakeClient([jira([issue("OTHER-1")], "next")])
    with pytest.raises(ValueError, match="outside"):
        Collector(store, client).collect(scope)
    state = store.collection_state(scope_id(scope))
    assert state["cursor"] is None and not state.get("watermark")
    assert state["state"] == "incomplete"
    assert store.status()["sources"] == []
    with pytest.raises(ValueError, match="pagination"):
        jira_page(entry({"issues": {"nodes": []}}))


def test_repeated_jira_cursor_is_not_a_success(tmp_path):
    store, scope = Store(tmp_path), jira_scope()
    client = FakeClient([jira([issue("P-1")], "next"), jira([issue("P-2")], "next")])
    Collector(store, client).collect(scope)
    with pytest.raises(ValueError, match="repeated"):
        Collector(store, client).collect(scope)
    assert not store.collection_state(scope_id(scope)).get("watermark")


def test_sharepoint_partial_page_persists_successes_and_does_not_reread_unchanged(tmp_path):
    store, scope = Store(tmp_path), sp_scope()
    page = entry([hit(1), hit(2), {"moreResults": False, "totalResultCount": 2}], multiple=True)
    body = {"response": {"content": [{"type": "text", "text": "Full body"}]}}
    client = FakeClient([page, body, CloudBudgetExceeded()])
    with pytest.raises(CloudBudgetExceeded):
        Collector(store, client).collect(scope)
    assert len(store.status()["sources"]) == 1
    assert not store.collection_state(scope_id(scope)).get("watermark")
    retry = FakeClient([page, body])
    result = Collector(store, retry).collect(scope)
    assert result["state"] == "complete" and result["new"] == 1
    assert len(retry.requests) == 2
    assert retry.requests[1][1]["uri"] == "file:///drive/2"
    assert len(store.status()["sources"]) == 2


def test_sharepoint_snippet_is_never_imported_as_full_content(tmp_path):
    store, scope = Store(tmp_path), sp_scope()
    partial = hit(1) | {"content": "snippet", "isPartialContent": True}
    page = entry([partial, {"moreResults": False}], multiple=True)
    body = {"response": {"content": [{"type": "text", "text": "Complete body"}]}}
    Collector(store, FakeClient([page, body])).collect(scope)
    found = store.search("Complete")["items"]
    assert len(found) == 1 and store.search("snippet")["total"] == 0
    assert store.observation(found[0]["id"])["content"] == "Complete body"


def test_outside_folder_hit_is_never_read(tmp_path):
    item = hit(1) | {"webUrl": "https://example.sharepoint.com/sites/work/another/a.docx"}
    client = FakeClient([entry([item, {"moreResults": False}], multiple=True)])
    with pytest.raises(ValueError, match="outside"):
        Collector(Store(tmp_path), client).collect(sp_scope())
    assert len(client.requests) == 1


def test_jira_updated_state_creates_new_observation_without_deleting_history(tmp_path):
    from work_memory.models import utcnow

    store, scope = Store(tmp_path), jira_scope()
    changed = issue("P-1", "Second state")
    changed["fields"]["updated"] = utcnow()
    client = FakeClient([jira([issue("P-1")]), jira([changed])])
    assert Collector(store, client).collect(scope)["new"] == 1
    assert Collector(store, client).collect(scope)["new"] == 1
    assert store.status()["sources"][0]["versions"] == 2
    assert store.search("First")["total"] == 1
    assert store.search("Second")["total"] == 1


def test_empty_result_does_not_delete_previous_observations(tmp_path):
    store, scope = Store(tmp_path), jira_scope()
    client = FakeClient([jira([issue("P-1")]), jira([])])
    Collector(store, client).collect(scope)
    Collector(store, client).collect(scope)
    assert len(store.status()["sources"]) == 1


def test_cloud_budget_blocks_before_any_process_starts(tmp_path):
    client = DockerClaude(tmp_path / "compose.yaml", tmp_path, "haiku", 10)
    client.max_call_attempts = 0
    with pytest.raises(CloudBudgetExceeded):
        client.call("No model should be called")
    assert client.call_attempts == 0


def test_sync_exposes_bounded_scan_as_partial(tmp_path):
    store = Store(tmp_path)
    client = FakeClient([jira([issue("P-1")], "next")])
    result = sync(store, Settings(scopes=[jira_scope()]), client, extract=False)
    assert result["state"] == "partial" and not result["errors"]
    assert store.status()["recent_runs"][0]["state"] == "partial"


def test_sharepoint_requires_explicit_pagination_and_checks_offset():
    with pytest.raises(ValueError, match="pagination"):
        sharepoint_page(entry([hit(1)], multiple=True))
    with pytest.raises(ValueError, match="offset"):
        sharepoint_page(entry([{"moreResults": True, "nextOffset": None}], multiple=True))


def test_explicit_empty_result_and_percent_escaped_folder_names():
    assert sharepoint_page(entry({"totalResultCount": 0})) == ([], None)
    with pytest.raises(ValueError):
        sharepoint_page(entry({"totalResultCount": False}))
    value = (
        "https://example.sharepoint.com/sites/work/Forms/AllItems.aspx?"
        "id=%2Fsites%2Fwork%2FLiteral%252FName"
    )
    assert folder_url(value).endswith("Literal%252FName")


def test_delta_uses_completed_watermark_with_overlap_and_full_reconciliation(tmp_path, monkeypatch):
    clock = iter(
        ["2026-11-06T10:00:00+00:00", "2026-11-06T11:00:00+00:00", "2026-11-20T10:00:00+00:00"]
    )
    monkeypatch.setattr("work_memory.collectors.utcnow", lambda: next(clock))
    store, scope = Store(tmp_path), jira_scope()
    client = FakeClient([jira([]), jira([]), jira([])])
    for _ in range(3):
        Collector(store, client).collect(scope)
    state = store.collection_state(scope_id(scope))
    assert state["last_full_scan"] == "2026-11-20T10:00:00+00:00"
    from work_memory.collectors import epoch_ms

    assert f"updated >= {epoch_ms('2026-11-05T10:00:00+00:00')}" in client.requests[1][1]["jql"]
    assert f"updated >= {epoch_ms('2025-11-20T10:00:00+00:00')}" in client.requests[2][1]["jql"]


def test_budget_exhaustion_is_visible_deferral_not_a_success_or_permission_failure(tmp_path):
    store = Store(tmp_path)
    result = sync(
        store, Settings(scopes=[jira_scope()]), FakeClient([CloudBudgetExceeded()]), extract=False
    )
    assert result["collection_deferred"] and result["state"] == "partial"
    assert not result["errors"]
    assert store.status()["collection_scopes"][0]["state"]["state"] == "incomplete"


def test_bounded_cycles_rotate_scope_priority_and_keep_incomplete_checkpoint(tmp_path):
    store = Store(tmp_path)
    first = jira_scope()
    second = first.model_copy(update={"project": "other", "project_key": "Q"})
    settings = Settings(scopes=[first, second])
    sync(store, settings, FakeClient([CloudBudgetExceeded()]), extract=False)
    old_checkpoint = store.collection_state(scope_id(first))
    restarted = Store(tmp_path)
    client = FakeClient([jira([]), CloudBudgetExceeded()])
    sync(restarted, settings, client, extract=False)
    assert 'project = "Q"' in client.requests[0][1]["jql"]
    assert restarted.collection_state(scope_id(second))["state"] == "complete"
    retained = restarted.collection_state(scope_id(first))
    assert retained["scan_upper"] == old_checkpoint["scan_upper"]
    assert not retained.get("watermark")


def test_renamed_document_is_not_hidden_by_unchanged_modification_date(tmp_path):
    store, scope = Store(tmp_path), sp_scope()
    body = {"response": {"content": [{"type": "text", "text": "Document body"}]}}
    original = entry([hit(1), {"moreResults": False}], multiple=True)
    renamed = entry([hit(1) | {"name": "Renamed.docx"}, {"moreResults": False}], multiple=True)
    Collector(store, FakeClient([original, body])).collect(scope)
    client = FakeClient([renamed, body])
    assert Collector(store, client).collect(scope)["new"] == 1
    assert len(client.requests) == 2


def test_failed_extractions_back_off_and_do_not_starve_new_evidence(tmp_path):
    from work_memory.models import Observation

    store = Store(tmp_path)
    item = Observation(
        source="fixture",
        source_id="one",
        project="test",
        source_url="fixture://1",
        title="Test",
        content="Test content",
    )
    oid = store.ingest(item)["id"]
    assert store.pending_extractions("v1") == [oid]
    store.extraction_failed(oid, "v1")
    assert store.pending_extractions("v1") == []
    assert store.status()["observations_without_completed_extraction"] == 1


def test_extract_only_uses_saved_evidence_without_collecting_sources(tmp_path):
    from work_memory.models import Extraction, Observation

    store = Store(tmp_path)
    store.ingest(
        Observation(
            source="fixture",
            source_id="one",
            project="pilot",
            source_url="fixture://one",
            title="Test",
            content="Saved evidence",
        )
    )

    class Extractor:
        def request(self, *_):
            raise AssertionError("Source tools must not be called")

        def extract(self, observation, previous):
            assert observation["content"] == "Saved evidence"
            return Extraction()

    result = sync(store, Settings(scopes=[jira_scope()]), Extractor(), collect=False)
    assert result["state"] == "complete" and not result["scopes"]
    assert store.status()["observations_without_completed_extraction"] == 0


def test_jira_outside_selected_history_is_not_persisted(tmp_path):
    store = Store(tmp_path)
    scope = jira_scope(since="2026-11-05")
    client = FakeClient([jira([issue("P-1")])])
    with pytest.raises(ValueError, match="date window"):
        Collector(store, client).collect(scope)
    assert store.active_observation_ids() == []
    assert not store.collection_state(scope_id(scope)).get("watermark")


def test_sharepoint_explicit_total_and_offsets_prove_end_without_more_flag():
    page = entry(
        [{**hit(1), "offset": 60}, {**hit(2), "offset": 61}, {"totalResultCount": 62}],
        multiple=True,
    )
    page["input"] = {"offset": 60, "limit": 20}
    items, cursor = sharepoint_page(page)
    assert len(items) == 2 and cursor is None
    page["response"]["content"][-1]["text"] = json.dumps({"totalResultCount": 80})
    assert sharepoint_page(page)[1] == 62
    page["input"]["offset"] = 59
    with pytest.raises(ValueError, match="offsets"):
        sharepoint_page(page)


def test_process_crash_after_committed_page_resumes_from_sqlite(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    data = tmp_path / "crash-memory"
    program = """
import os,runpy,sys
import work_memory.collectors as collectors
helpers=runpy.run_path(sys.argv[1])
collectors.utcnow=lambda:'2026-11-07T12:00:00.000000+00:00'
scope=helpers['jira_scope']().model_copy(update={'max_pages_per_sync':2})
class CrashAfterFirstPage:
 def __init__(self):self.calls=0
 def request(self,tool,args):
  self.calls+=1
  if self.calls==2:os._exit(75)
  return helpers['jira']([helpers['issue']('P-1')],'persisted-cursor')
collectors.Collector(helpers['Store'](sys.argv[2]),CrashAfterFirstPage()).collect(scope)
"""
    exited = subprocess.run(
        [sys.executable, "-c", program, str(Path(__file__).resolve()), str(data)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert exited.returncode == 75, exited.stderr
    store = Store(data)
    scope = jira_scope()
    state = store.collection_state(scope_id(scope))
    assert state["cursor"] == "persisted-cursor" and not state.get("watermark")
    assert len(store.active_observation_ids()) == 1
    client = FakeClient([jira([issue("P-2")])])
    result = Collector(store, client).collect(scope)
    assert client.requests[0][1]["nextPageToken"] == "persisted-cursor"
    assert result["new"] == 1 and result["state"] == "complete"
    assert len(store.active_observation_ids()) == 2


def test_missing_provider_output_never_finishes_a_scan(tmp_path):
    from work_memory.collectors import JIRA_SEARCH, EmptyProviderOutput

    store = Store(tmp_path)
    client = FakeClient(
        [
            {
                "tool": JIRA_SEARCH,
                "response": {"content": f"({JIRA_SEARCH} completed with no output)"},
            }
        ]
    )
    with pytest.raises(EmptyProviderOutput):
        Collector(store, client).collect(jira_scope())
    state = store.collection_state(scope_id(jira_scope()))
    assert state["state"] == "incomplete" and state["error"] == "EmptyProviderOutput"
    assert not state.get("watermark")
