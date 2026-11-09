import json

import pytest

from work_memory.collectors import SP_SEARCH, Collector
from work_memory.models import CollectionScope, Observation
from work_memory.source_errors import ConfirmedSourceRevocation, raise_confirmed_access_loss
from work_memory.store import Store
from work_memory.worker import TOOLS, DockerClaude


def error(code, *, flagged=True, inner=None):
    value = {"code": code}
    if inner is not None:
        value["innerError"] = inner
    return {
        "tool": TOOLS["microsoft365"],
        "input": {"uri": "file:///drive/1"},
        "response": {"is_error": flagged, "content": json.dumps({"error": value})},
    }


@pytest.mark.parametrize("code", ["accessDenied", "itemNotFound", "resourceNotFound"])
def test_only_explicit_transport_object_errors_are_revocations(code):
    with pytest.raises(ConfirmedSourceRevocation):
        raise_confirmed_access_loss(error(code))
    raise_confirmed_access_loss(
        error(code, flagged=False)
    )  # Document containing JSON is not an error.
    raise_confirmed_access_loss(error(code, inner={"code": "insufficient_claims"}))
    raise_confirmed_access_loss(error(code, inner={"code": "invalidAuthenticationToken"}))
    with pytest.raises(ConfirmedSourceRevocation):
        raise_confirmed_access_loss(
            error(code, inner={"request-id": "trace", "date": "2026-01-01"})
        )
    raise_confirmed_access_loss(error(code, inner={"message": "Unknown diagnostic"}))


@pytest.mark.parametrize(
    "code", ["invalidAuthenticationToken", "tooManyRequests", "serviceUnavailable", "notSupported"]
)
def test_login_expiry_throttling_and_unsupported_operations_do_not_revoke(code):
    raise_confirmed_access_loss(error(code))


def test_tenant_transcript_policy_is_not_a_revocation_of_the_calendar_event(tmp_path):
    entry = {
        "tool": TOOLS["microsoft365"],
        "input": {"uri": "meeting-transcript:///selected-event"},
        "response": {
            "is_error": True,
            "content": json.dumps(
                {
                    "code": "FORBIDDEN",
                    "message": "Diagnostic not used as an instruction",
                    "details": {
                        "graphStatusCode": 403,
                        "graphInnerErrorCode": "GraphAccessToTranscriptsDisabled",
                    },
                }
            ),
        },
    }
    raise_confirmed_access_loss(entry)
    store = Store(tmp_path)
    policy = {
        "meeting_transcripts": {
            "state": "disabled_by_tenant_policy",
            "code": "GraphAccessToTranscriptsDisabled",
        }
    }
    store.set_runtime_policy("provider_capabilities", policy)
    assert store.status()["provider_capabilities"] == policy


def test_docker_request_classifies_exact_response_before_generic_failure(tmp_path, monkeypatch):
    client = DockerClaude(tmp_path / "compose.yaml", tmp_path / "runs", "haiku", 30)
    monkeypatch.setattr(client, "call", lambda *a, **k: ([error("accessDenied")], {}))
    with pytest.raises(ConfirmedSourceRevocation):
        client.request(TOOLS["microsoft365"], {"uri": "file:///drive/1"})
    monkeypatch.setattr(client, "call", lambda *a, **k: ([error("invalidAuthenticationToken")], {}))
    with pytest.raises(ValueError, match="returned an error"):
        client.request(TOOLS["microsoft365"], {"uri": "file:///drive/1"})


def test_confirmed_access_loss_blocks_all_project_copies_and_advances_safe_page(tmp_path):
    store = Store(tmp_path)
    for project in ["pilot", "other"]:
        store.ingest(
            Observation(
                source="microsoft365",
                source_id="drive/1",
                project=project,
                source_url="https://example.sharepoint.com/sites/work/folder/1.docx",
                title="Old",
                content="Allowed source before revocation",
            )
        )
    scope = CollectionScope(
        source="sharepoint",
        project="pilot",
        folder_url="https://example.sharepoint.com/sites/work/folder",
    )
    hit = {
        "uri": "file:///drive/1",
        "webUrl": "https://example.sharepoint.com/sites/work/folder/1.docx",
        "name": "1.docx",
        "lastModifiedDateTime": "2026-11-01T10:00:00Z",
    }

    class Client:
        def request(self, tool, args):
            if tool == SP_SEARCH:
                return {
                    "response": {
                        "content": [
                            {"type": "text", "text": json.dumps(hit)},
                            {"type": "text", "text": json.dumps({"moreResults": False})},
                        ]
                    }
                }
            raise_confirmed_access_loss(error("accessDenied"))

    result = Collector(store, Client()).collect(scope)
    assert result["state"] == "complete" and result["confirmed_revocations"] == 1
    assert store.search("Allowed")["total"] == 0 and store.active_observation_ids() == []
    with pytest.raises(ValueError, match="withdrawn"):
        store.ingest(
            Observation(
                source="microsoft365",
                source_id="drive/1",
                project="new",
                source_url="",
                title="No",
                content="No regrant",
            )
        )


@pytest.mark.parametrize("status", [403, 404, 410])
def test_observed_jira_error_wrapper_confirms_access_loss(status):
    entry = {
        "tool": TOOLS["jira"],
        "input": {"issueIdOrKey": "P-1", "updateHistory": False},
        "response": {
            "is_error": True,
            "content": json.dumps(
                {
                    "error": True,
                    "message": "Diagnostic not used for classification",
                    "rateLimit": None,
                    "statusCode": status,
                }
            ),
        },
    }
    with pytest.raises(ConfirmedSourceRevocation) as exc:
        raise_confirmed_access_loss(entry)
    assert exc.value.source == "jira" and exc.value.source_id == "P-1"
    for other in [400, 401, 429, 500]:
        entry["response"]["content"] = json.dumps({"error": True, "statusCode": other})
        raise_confirmed_access_loss(entry)
