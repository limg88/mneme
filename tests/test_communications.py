import importlib.util
import json
from pathlib import Path

import pytest

from work_memory.collectors import MAIL_SEARCH, Collector, mail_page, scope_id
from work_memory.communications import (
    calendar_header_facts,
    calendar_time,
    communication_eligibility,
)
from work_memory.models import CalendarScope, MailScope, Settings, TeamsScope
from work_memory.store import Store
from work_memory.worker import CloudBudgetExceeded, sync


def test_calendar_cap_splits_fixed_window_and_resumes_before_watermark(tmp_path, monkeypatch):
    monkeypatch.setattr("work_memory.collectors.utcnow", lambda: "2026-11-07T12:00:00+00:00")
    store = Store(tmp_path)
    scope = CalendarScope(
        project="DEMO", query="DEMO", include_terms=["DEMO"], max_pages_per_sync=1
    )
    notice = (
        "Note: only 1000 events in the date range were checked against the filters, "
        "so some matches may be missing. The events checked start at 2026-07-14T07:30:00Z "
        "or earlier. To check later events, search again keeping the other parameters, "
        "afterDateTime set to 2026-07-14T07:29:00Z and offset 0."
    )
    initial = Replay([{"response": {"content": [{"type": "text", "text": notice}]}}])
    assert Collector(store, initial).collect(scope)["state"] == "scanning"
    state = store.collection_state(scope_id(scope))
    assert not state.get("watermark") and len(state["calendar_windows"]) == 2
    windows = state["calendar_windows"]
    assert windows[0][0] == state["scan_lower"]
    assert windows[0][1] == windows[1][0] and windows[1][1] == state["scan_upper"]
    with pytest.raises(CloudBudgetExceeded):
        Collector(store, Replay([CloudBudgetExceeded()])).collect(scope)
    assert store.collection_state(scope_id(scope))["calendar_windows"] == windows
    assert not store.collection_state(scope_id(scope)).get("watermark")
    first = Replay([page([])])
    assert Collector(Store(tmp_path), first).collect(scope)["state"] == "scanning"
    assert first.requests[0][1]["beforeDateTime"] == windows[0][1]
    assert not store.collection_state(scope_id(scope)).get("watermark")
    second = Replay([page([])])
    assert Collector(store, second).collect(scope)["state"] == "complete"
    assert second.requests[0][1]["afterDateTime"] == windows[1][0]
    assert store.collection_state(scope_id(scope))["watermark"] == state["scan_upper"]


def test_mail_final_count_requires_consistent_offsets():
    response = page([{**hit(), "offset": 2}])
    response["response"]["content"][-1]["text"] = json.dumps({"totalResultCount": 3})
    response["input"] = {"offset": 2, "limit": 10}
    assert mail_page(response)[1] is None
    response["input"]["offset"] = 1
    with pytest.raises(ValueError, match="offsets"):
        mail_page(response)


@pytest.mark.parametrize(
    "hit,expected",
    [
        ({"subject": "Accettata: DEMO PI7"}, "eligible"),
        ({"subject": "Generico", "summary": "EXAMPLEB"}, "project_not_confirmed"),
        ({"subject": "", "summary": "DEMO: nuova scadenza"}, "eligible"),
        ({"subject": "DEMO ferie"}, "excluded_category"),
        ({"subject": "DEMO", "summary": "contenuto personale"}, "excluded_category"),
        ({"subject": "EXAMPLEB", "sensitivity": "private"}, "excluded_category"),
        ({"subject": "Fattura DEMO"}, "eligible"),
        ({"subject": "", "summary": "Fattura DEMO"}, "project_not_confirmed"),
        ({"subject": "UNDEMOED"}, "project_not_confirmed"),
    ],
)
def test_metadata_policy_precedes_full_body_read(hit, expected):
    assert communication_eligibility(hit, ["DEMO", "EXAMPLEB"]) == expected


def test_project_terms_are_not_inferred_from_untrusted_hit():
    with pytest.raises(ValueError):
        communication_eligibility({"subject": "DEMO"}, [])


def test_exact_selected_series_with_brackets_is_not_a_broader_project_filter():
    term = "[PROJECT] Daily Standup"
    assert communication_eligibility({"subject": term}, [term]) == "eligible"
    assert (
        communication_eligibility({"subject": "[PROJECT] Payroll"}, [term])
        == "project_not_confirmed"
    )
    assert communication_eligibility({"subject": "UNDEMOED"}, ["DEMO"]) == "project_not_confirmed"


@pytest.mark.parametrize(
    "suffix", ["outlook_email_search", "chat_message_search", "outlook_calendar_search"]
)
def test_communication_guard_allows_only_the_exact_preselected_query(suffix):
    path = Path(__file__).resolve().parents[1] / "docker/claude-probe/guard.py"
    spec = importlib.util.spec_from_file_location("guard", path)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    tool = "mcp__claude_ai_Microsoft_365__" + suffix
    args = {"query": "DEMO", "limit": 1, "offset": 0}
    scope = [{"source": "request", "tool": tool, "input": args}]
    assert guard.permitted({"tool_name": tool, "tool_input": args}, scope)
    assert not guard.permitted({"tool_name": tool, "tool_input": {**args, "query": "*"}}, scope)
    assert not guard.permitted({"tool_name": "mcp__send_email", "tool_input": args}, scope)


def hit(number=1, subject="DEMO PI7"):
    return {
        "id": f"id{number}",
        "uri": f"mail:///id{number}",
        "subject": subject,
        "receivedDateTime": "2026-11-07T11:00:00Z",
        "webLink": "https://outlook.example/mail",
    }


def page(hits, footer=None):
    return {
        "response": {
            "content": [
                {"type": "text", "text": json.dumps(x)}
                for x in [*hits, footer or {"moreResults": False}]
            ]
        }
    }


def body(number=1, content="<p>Planned date: 10 November.</p>", subject="DEMO PI7"):
    return {
        "tool": "mcp__claude_ai_Microsoft_365__read_resource",
        "response": {
            "content": json.dumps(
                {
                    "id": f"id{number}",
                    "subject": subject,
                    "sender": {"name": "Operator", "address": "omit@example.com"},
                    "receivedDateTime": "2026-11-07T11:00:00Z",
                    "body": {"contentType": "html", "content": content},
                }
            )
        },
    }


class Replay:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def request(self, tool, args):
        self.requests.append((tool, args))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def scope():
    return MailScope(project="DEMO", query="DEMO", include_terms=["DEMO", "EXAMPLEB"], page_size=2)


@pytest.fixture(autouse=True)
def fixed_collection_clock(monkeypatch):
    monkeypatch.setattr("work_memory.collectors.utcnow", lambda: "2026-11-07T12:00:00+00:00")


def test_mail_scope_disallows_broad_queries_and_empty_terms():
    for query in ("*", "DEMO OR HR", "other"):
        with pytest.raises(ValueError):
            MailScope(project="DEMO", query=query, include_terms=["DEMO"])
    with pytest.raises(ValueError):
        MailScope(project="DEMO", query="DEMO", include_terms=[])


def test_metadata_filter_runs_before_body_reads_and_omits_personal_fields(tmp_path):
    store = Store(tmp_path)
    client = Replay([page([hit(), hit(2, "DEMO HR ferie")]), body()])
    result = Collector(store, client).collect(scope())
    assert result["state"] == "complete" and result["new"] == 1 and result["filtered_metadata"] == 1
    assert len(client.requests) == 2 and client.requests[0][0] == MAIL_SEARCH
    found = store.search("Planned")["items"][0]
    o = store.observation(found["id"])
    assert "omit@example.com" not in o["content"] and "<p>" not in o["content"]
    assert o["metadata"]["coverage"] == "body_as_returned"


def test_mail_page_resumes_without_repaying_a_completed_body(tmp_path):
    store = Store(tmp_path)
    s = scope()
    p = page([hit(), hit(2)])
    first = Replay([p, body(), CloudBudgetExceeded()])
    with pytest.raises(CloudBudgetExceeded):
        Collector(store, first).collect(s)
    assert store.collection_state(scope_id(s))["page_completed_uris"] == ["mail:///id1"]
    assert store.search("Planned")["total"] == 1
    second = Replay([p, body(2)])
    result = Collector(Store(tmp_path), second).collect(s)
    assert result["new"] == 1 and len(second.requests) == 2
    assert second.requests[1][1]["uri"] == "mail:///id2"
    assert store.collection_state(scope_id(s))["page_completed_uris"] == []


def test_mail_full_body_filter_and_mismatched_identity_never_publish(tmp_path):
    store = Store(tmp_path)
    s = scope()
    result = Collector(store, Replay([page([hit()]), body(content="Contenuto personale")])).collect(
        s
    )
    assert result["filtered_bodies"] == 1 and store.status()["sources"] == []
    with pytest.raises(ValueError, match="identity"):
        Collector(store, Replay([page([hit()]), body(2)])).collect(s)
    assert store.status()["sources"] == []


def test_mail_cursor_and_absence_are_not_a_deletion_event(tmp_path):
    store = Store(tmp_path)
    s = scope()
    first = Replay([page([hit()], {"moreResults": True, "nextCursor": "opaque"}), body()])
    assert Collector(store, first).collect(s)["state"] == "scanning"
    second = Replay([page([])])
    assert Collector(store, second).collect(s)["state"] == "complete"
    assert second.requests[0][1] == {"cursor": "opaque"}
    assert len(store.status()["sources"]) == 1
    with pytest.raises(ValueError, match="pagination"):
        mail_page({"response": {"content": json.dumps({"uri": "mail:///1"})}})


def test_null_body_is_metadata_only_and_never_sent_to_model(tmp_path):
    store = Store(tmp_path)
    response = body()
    data = json.loads(response["response"]["content"])
    data["body"]["content"] = None
    response["response"]["content"] = json.dumps(data)
    Collector(store, Replay([page([hit()]), response])).collect(scope())

    class NoModel:
        def extract(self, *_):
            raise AssertionError("Metadata-only mail must not generate body facts")

    result = sync(store, Settings(), NoModel(), collect=False)
    assert result["metadata_only_observations"] == 1
    facts = store.facts()["items"]
    assert facts and all(f["source_coverage"] == "metadata_only" for f in facts)
    assert all(f["predicate"].startswith("mail_") for f in facts)
    assert store.status()["sources"][0]["coverage"] == ["metadata_only"]


def calendar_hit():
    return {
        "id": "event1",
        "uri": "calendar:///event1",
        "subject": "EXAMPLEB kickoff",
        "start": {"dateTime": "2026-11-07T11:00:00", "timeZone": "UTC"},
        "end": {"dateTime": "2026-11-07T11:30:00", "timeZone": "UTC"},
    }


def test_calendar_dates_are_values_and_cancellation_is_not_access_revocation(tmp_path):
    store = Store(tmp_path)
    hit = calendar_hit()
    s = CalendarScope(project="DEMO", query="EXAMPLEB", include_terms=["EXAMPLEB"])
    data = {
        **hit,
        "isCancelled": True,
        "organizer": {"name": "Operator", "address": "omit@example.com"},
        "body": {"contentType": "text", "content": "EXAMPLEB planning agenda"},
        "meetingTranscriptUrl": "meeting-transcript:///event1",
    }
    response = {
        "tool": "mcp__claude_ai_Microsoft_365__read_resource",
        "response": {"content": json.dumps(data)},
    }
    result = Collector(store, Replay([page([hit]), response])).collect(s)
    assert result["new"] == 1
    observation = store.observation(store.active_observation_ids()[0])
    assert observation["metadata"]["transcript_state"] == "not_collected"
    assert "omit@example.com" not in observation["content"]
    facts = calendar_header_facts(observation)
    assert any(f.predicate == "calendar_cancelled" and f.value == "true" for f in facts)
    assert all(f.valid_from is None and f.valid_to is None for f in facts)
    assert not store.is_withdrawn("microsoft365", hit["uri"], "DEMO")
    assert calendar_time(hit["start"]) == "2026-11-07T11:00:00.000000+00:00"


def teams_hit():
    return {
        "id": "m1",
        "uri": "teams:///c1/m1",
        "chatId": "c1",
        "chatUri": "teams:///c1",
        "subject": None,
        "summary": "DEMO planning",
        "createdDateTime": "2026-11-07T11:00:00Z",
    }


def teams_body(deleted=False):
    return {
        "tool": "mcp__claude_ai_Microsoft_365__read_resource",
        "response": {
            "content": json.dumps(
                {
                    "id": "m1",
                    "chatId": "c1",
                    "subject": None,
                    "from": {"id": "user1", "displayName": "Operator"},
                    "createdDateTime": "2026-11-07T11:00:00Z",
                    "lastModifiedDateTime": "2026-11-07T11:30:00Z",
                    "body": {"contentType": "html", "content": "<p>DEMO planning</p>"},
                    "deletedDateTime": "2026-11-07T11:30:00Z" if deleted else None,
                }
            )
        },
    }


def test_teams_only_reads_single_messages_and_explicit_deletion_blocks_copies(tmp_path):
    store = Store(tmp_path)
    s = TeamsScope(project="DEMO", query="DEMO", include_terms=["DEMO"])
    hit = teams_hit()
    result = Collector(store, Replay([page([hit]), teams_body()])).collect(s)
    assert result["new"] == 1 and store.search("planning")["total"] == 1
    result = Collector(store, Replay([page([hit]), teams_body(True)])).collect(s)
    assert result["confirmed_revocations"] == 1
    assert store.search("planning")["total"] == 0
    assert store.is_withdrawn("microsoft365", hit["uri"], "another project")


def test_teams_chat_uri_is_rejected_before_any_body_read(tmp_path):
    hit = teams_hit()
    hit["uri"] = hit["chatUri"]
    client = Replay([page([hit])])
    store = Store(tmp_path)
    with pytest.raises(ValueError, match="one message"):
        Collector(store, client).collect(
            TeamsScope(project="DEMO", query="DEMO", include_terms=["DEMO"])
        )
    assert len(client.requests) == 1 and store.active_observation_ids() == []


def test_unstructured_empty_first_page_can_later_find_new_events(tmp_path, monkeypatch):
    from work_memory.collectors import CALENDAR_SEARCH, EmptyProviderOutput

    store = Store(tmp_path)
    selected = CalendarScope(
        project="DEMO", query="DEMO", include_terms=["DEMO"], page_size=1, max_pages_per_sync=1
    )
    monkeypatch.setattr("work_memory.collectors.utcnow", lambda: "2026-11-07T00:00:00+00:00")
    missing = {
        "tool": CALENDAR_SEARCH,
        "response": {"content": f"({CALENDAR_SEARCH} completed with no output)"},
    }
    with pytest.raises(EmptyProviderOutput):
        Collector(store, Replay([missing])).collect(selected)
    state = store.collection_state(scope_id(selected))
    assert state["scan_upper"] is None and not state.get("watermark")
    assert state["retry_with_fresh_window"] and state["state"] == "incomplete"
    monkeypatch.setattr("work_memory.collectors.utcnow", lambda: "2026-11-08T00:00:00+00:00")
    hit = calendar_hit()
    # The event is newer than the failed, unconfirmed window's upper bound.
    hit["subject"] = "DEMO checkpoint retry"
    fixture = Replay(
        [
            page([hit]),
            {
                "tool": "mcp__claude_ai_Microsoft_365__read_resource",
                "response": {
                    "content": json.dumps(
                        {
                            "id": hit["id"],
                            "subject": hit["subject"],
                            "start": hit["start"],
                            "end": hit["end"],
                            "body": {"contentType": "text", "content": "DEMO scheduled session."},
                        }
                    )
                },
            },
        ]
    )
    result = Collector(store, fixture).collect(selected)
    assert result["state"] == "complete" and result["new"] == 1
    assert fixture.requests[0][1]["beforeDateTime"] == "2026-11-08T00:00:00+00:00"
    assert not store.collection_state(scope_id(selected)).get("retry_with_fresh_window")
