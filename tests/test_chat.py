import json

import pytest
from fastapi.testclient import TestClient

from work_memory.api import create_app
from work_memory.budget import SpendingLimitExceeded
from work_memory.chat import Chat, ChatBusy, ChatRequest, ChatUnavailable
from work_memory.models import FactInput, Observation
from work_memory.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "memory")


def observe(
    store,
    text="Il 5 novembre abbiamo deciso di usare SQLite per semplicità.",
    project="pilot",
    source_id="decision",
):
    return store.ingest(
        Observation(
            source="fixture",
            source_id=source_id,
            project=project,
            source_url="https://example.test/decision",
            title="Decisione archivio",
            content=text,
        )
    )["id"]


class Worker:
    model = "haiku"

    def __init__(self, answer=None, during_call=None):
        self.prompts = []
        self.custom_answer = answer
        self.during_call = during_call

    def call(self, prompt, *, schema):
        self.prompts.append(prompt)
        data = json.loads(prompt.split("DATI:\n", 1)[1])
        if self.during_call:
            self.during_call()
        source = data["sources"][0]
        quote = source["passage"]
        answer = self.custom_answer or {
            "claims": [
                {
                    "text": "È stato scelto SQLite per semplicità.",
                    "kind": "decision",
                    "citations": [{"source": source["id"], "quote": quote}],
                }
            ],
            "timeline": [
                {
                    "date": "5 novembre",
                    "label": "Scelta dell’archivio",
                    "citations": [{"source": source["id"], "quote": quote}],
                }
            ],
            "gaps": ["L’autore della decisione non è documentato."],
        }
        return [], {"structured_output": answer}


def test_question_evidence_and_followup_share_store_without_writes(store):
    oid = observe(store)
    store.add_facts(
        oid,
        [
            FactInput(
                subject="Archivio",
                predicate="technology",
                value="SQLite",
                quote="deciso di usare SQLite",
                certainty="explicit",
            )
        ],
        "test",
    )
    worker = Worker()
    chat = Chat(store, worker)
    answer = chat.answer(ChatRequest(question="Cosa abbiamo deciso su SQLite e perché?"))
    assert answer["cloud_calls"] == 1
    assert answer["sources"][0]["observation_id"] == oid
    assert answer["claims"][0]["citations"][0]["quote"] in store.observation(oid)["content"]
    assert "parole chiave" in " ".join(answer["limitations"])
    assert store.facts()["total"] == 1
    data = json.loads(worker.prompts[0].split("DATI:\n", 1)[1])
    assert data["sources"][0]["facts"][0]["definitive"] is False
    followup = chat.answer(
        ChatRequest(
            question="Chi l’ha deciso?",
            previous_questions=["Cosa abbiamo deciso su SQLite?"],
            evidence_ids=[oid],
        )
    )
    assert followup["sources"][0]["observation_id"] == oid
    assert "Chi l’ha deciso?" in worker.prompts[-1]


@pytest.mark.parametrize("quote,source", [("Citazione inventata", "E1"), ("SQLite", "E8")])
def test_invalid_citations_never_reach_the_ui(store, quote, source):
    observe(store)
    worker = Worker(
        answer={
            "claims": [
                {
                    "text": "Falso",
                    "kind": "decision",
                    "citations": [{"source": source, "quote": quote}],
                }
            ],
            "timeline": [],
            "gaps": [],
        }
    )
    with pytest.raises(ChatUnavailable, match="citazioni"):
        Chat(store, worker).answer(ChatRequest(question="SQLite"))


def test_empty_memory_does_not_spend_cloud_quota(store):
    worker = Worker()
    answer = Chat(store, worker).answer(ChatRequest(question="SQLite"))
    assert answer["cloud_calls"] == 0 and answer["gaps"] and not worker.prompts


def test_project_filter_also_applies_to_previous_evidence(store):
    other = observe(store, project="other")
    worker = Worker()
    answer = Chat(store, worker).answer(
        ChatRequest(
            question="SQLite",
            project="pilot",
            evidence_ids=[other],
            previous_questions=["SQLite"],
        )
    )
    assert not answer["sources"] and not worker.prompts


def test_revoked_evidence_cannot_be_reused_for_followups(store):
    oid = observe(store)
    worker = Worker()
    store.withdraw("fixture", "decision", "pilot")
    with pytest.raises(KeyError):
        Chat(store, worker).answer(ChatRequest(question="SQLite", evidence_ids=[oid]))
    assert not worker.prompts


def test_revocation_while_generating_blocks_the_whole_answer(store):
    observe(store)
    worker = Worker(during_call=lambda: store.withdraw("fixture", "decision", "pilot"))
    with pytest.raises(KeyError):
        Chat(store, worker).answer(ChatRequest(question="SQLite"))


def test_chat_requires_explicit_enablement_and_serializes_calls(store):
    with pytest.raises(ChatUnavailable):
        Chat(store).answer(ChatRequest(question="SQLite"))
    chat = Chat(store, Worker())
    chat.lock.acquire()
    with pytest.raises(ChatBusy):
        chat.answer(ChatRequest(question="SQLite"))
    chat.lock.release()
    worker = Worker()
    worker.model = "sonnet"
    with pytest.raises(ValueError, match="Haiku"):
        Chat(store, worker)


def test_browser_session_one_use_login_and_csrf(store):
    observe(store)
    client = TestClient(create_app(store, chat=Chat(store, Worker()), launch_key="launch-once"))
    page = client.get("/")
    assert page.status_code == 200
    assert store.token() not in page.text
    assert page.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/assets/api-token").status_code == 404
    assert client.get("/v1/chat/info").status_code == 401
    assert client.get("/connect/launch-once", follow_redirects=False).status_code == 303
    assert client.get("/connect/launch-once").status_code == 401
    assert client.get("/v1/chat/info").json()["enabled"] is True
    assert client.cookies.get("mneme_session")
    assert client.post("/v1/chat", json={"question": "SQLite"}).status_code == 403
    assert (
        client.post(
            "/v1/chat", json={"question": "SQLite"}, headers={"origin": "https://evil.test"}
        ).status_code
        == 403
    )
    answer = client.post(
        "/v1/chat", json={"question": "SQLite"}, headers={"origin": "http://testserver"}
    )
    assert answer.status_code == 200 and answer.json()["claims"]
    second_browser = TestClient(client.app)
    assert second_browser.get("/v1/chat/info").status_code == 401


def test_bearer_api_budget_busy_and_worker_failures(store):
    observe(store)
    worker = Worker()
    chat = Chat(store, worker)
    client = TestClient(create_app(store, chat=chat))
    headers = {"Authorization": "Bearer " + store.token()}
    assert client.post("/v1/chat", json={"question": " "}, headers=headers).status_code == 422
    assert (
        client.post("/v1/chat", json={"question": "x" * 2001}, headers=headers).status_code == 422
    )
    assert (
        client.post(
            "/v1/chat", json={"question": "x", "previous_questions": ["x"] * 5}, headers=headers
        ).status_code
        == 422
    )
    chat.lock.acquire()
    assert client.post("/v1/chat", json={"question": "SQLite"}, headers=headers).status_code == 429
    chat.lock.release()

    def failed(prompt, **kwargs):
        raise SpendingLimitExceeded("No more quota")

    worker.call = failed
    assert client.post("/v1/chat", json={"question": "SQLite"}, headers=headers).status_code == 429

    def offline(prompt, **kwargs):
        raise OSError("PRIVATE transport details")

    worker.call = offline
    response = client.post("/v1/chat", json={"question": "SQLite"}, headers=headers)
    assert response.status_code == 503 and "PRIVATE" not in response.text


def test_fact_filter_uses_shared_store_rules(store):
    first = observe(store, source_id="one")
    second = observe(store, source_id="two")
    for oid in (first, second):
        store.add_facts(
            oid,
            [FactInput(subject="Archivio", predicate="technology", value="SQLite", quote="SQLite")],
            "test",
        )
    assert store.facts()["total"] == 2
    assert store.facts(observation_id=first)["total"] == 1
    store.withdraw("fixture", "one", "pilot")
    assert store.facts(observation_id=first)["total"] == 0


def test_observation_filter_keeps_conflicts_with_other_observations(store):
    first = observe(store, text="Monday", source_id="one")
    second = observe(store, text="Tuesday", source_id="two")
    for oid, value in [(first, "Monday"), (second, "Tuesday")]:
        store.add_facts(
            oid,
            [
                FactInput(
                    subject="pilot",
                    predicate="deadline",
                    value=value,
                    quote=value,
                    certainty="explicit",
                    valid_from="2026-01-01",
                )
            ],
            "test",
        )
    item = store.facts(observation_id=first)["items"][0]
    assert item["conflicting"] and not item["definitive"]
