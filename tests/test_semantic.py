import pytest
from fastapi.testclient import TestClient

from work_memory.api import create_app
from work_memory.models import Observation
from work_memory.semantic import NoRedirect, Ollama, SemanticUnavailable, index, normalized, search
from work_memory.store import Store


class Embedder:
    def __init__(self):
        self.calls = []
        self.version = "weights-v1"

    def digest(self):
        return self.version

    def embed(self, texts, expected_digest):
        if self.version != expected_digest:
            raise SemanticUnavailable("Model changed")
        self.calls.append(texts)
        return [
            [1.0, 0.0] if any(w in t.lower() for w in ("migration", "trasferire")) else [0.0, 1.0]
            for t in texts
        ]


def observe(store, text, project="work", source_id="1"):
    return store.ingest(
        Observation(
            source="fixture",
            source_id=source_id,
            project=project,
            source_url="fixture://test",
            title="Note",
            content=text,
        )
    )["id"]


def test_index_resumes_without_reembedding_and_publishes_complete_documents(tmp_path):
    store = Store(tmp_path)
    oid = observe(store, "migration " * 600)
    client = Embedder()
    result = index(store, client=client, max_batches=1, batch_size=1)
    assert result["state"] == "partial" and len(client.calls) == 1
    assert search(store, "trasferire", None, 20, 0, client=client)["items"] == []
    before = len(client.calls)
    result = index(Store(tmp_path), client=client, batch_size=1)
    assert result["state"] == "complete"
    assert len(client.calls) - before == 3
    before = len(client.calls)
    index(store, client=client)
    assert len(client.calls) == before
    result = search(store, "trasferire", None, 20, 0, client=client)
    assert result["items"][0]["id"] == oid
    item = result["items"][0]
    text = store.observation(oid)["content"]
    assert text[item["start_offset"] : item["end_offset"]] == item["excerpt"]


def test_semantic_project_filters_and_withdrawal_remove_all_vector_copies(tmp_path):
    store = Store(tmp_path)
    client = Embedder()
    a = observe(store, "migration planning")
    observe(store, "garden trees", source_id="2")
    observe(store, "migration private project", project="other", source_id="3")
    index(store, client=client)
    result = search(store, "trasferire", "work", 1, 0, client=client)
    assert result["total"] == 2 and result["items"][0]["id"] == a and result["truncated"]
    store.withdraw("fixture", "1", "work")
    assert a not in {
        i["id"] for i in search(store, "trasferire", "work", 20, 0, client=client)["items"]
    }
    with store.connect() as c:
        assert (
            c.execute(
                "select count(*) from semantic_chunks where observation_id=?", (a,)
            ).fetchone()[0]
            == 0
        )


def test_quarantined_source_cannot_reappear_via_semantic_index(tmp_path):
    store = Store(tmp_path)
    client = Embedder()
    oid = observe(store, "migration")
    index(store, client=client)
    store.quarantine_observation(oid, "Bad acquisition")
    assert search(store, "trasferire", None, 20, 0, client=client)["items"] == []


def test_changed_or_missing_model_never_produces_mixed_space_results(tmp_path):
    store = Store(tmp_path)
    client = Embedder()
    observe(store, "migration")
    index(store, client=client)
    client.version = "new weights"
    with pytest.raises(SemanticUnavailable, match="changed"):
        search(store, "trasferire", None, 20, 0, client=client)


@pytest.mark.parametrize(
    "vector", [[], [0.0, 0.0], [float("nan")], [float("inf")], [True], "not a vector"]
)
def test_invalid_vectors_are_rejected(vector):
    with pytest.raises(SemanticUnavailable):
        normalized(vector)


def test_local_http_contract_refuses_cloud_models_redirects_and_truncation(monkeypatch):
    client = Ollama()
    calls = []

    def request(path, data=None):
        calls.append((path, data))
        if path == "/api/tags":
            return {"models": [{"name": "bge-m3:latest", "digest": "v1"}]}
        return {"embeddings": [[3.0, 4.0]]}

    monkeypatch.setattr(client, "request", request)
    assert client.embed(["local text"], "v1") == [[0.6, 0.8]]
    payload = next(d for p, d in calls if p == "/api/embed")
    assert payload["truncate"] is False
    monkeypatch.setattr(
        client,
        "request",
        lambda *_: {
            "models": [{"name": "bge-m3:latest", "digest": "v1", "remote_host": "https://remote"}]
        },
    )
    with pytest.raises(SemanticUnavailable):
        client.digest()
    with pytest.raises(SemanticUnavailable):
        NoRedirect().redirect_request()
    with pytest.raises(ValueError):
        Ollama().request("https://remote/api/embed")


def test_hybrid_fallback_and_http_service_unavailable_are_explicit(tmp_path):
    store = Store(tmp_path)
    observe(store, "migration")
    result = store.search("migration", mode="hybrid")
    assert result["effective_mode"] == "lexical" and "semantic_error" in result
    api = TestClient(create_app(store))
    headers = {"Authorization": "Bearer " + store.token()}
    response = api.get(
        "/v1/search", params={"query": "migration", "mode": "semantic"}, headers=headers
    )
    assert response.status_code == 503 and response.json()["fallback"] == "lexical"


def test_hybrid_combines_candidates_and_preserves_exact_semantic_passage(tmp_path, monkeypatch):
    store = Store(tmp_path)
    client = Embedder()
    a = observe(store, "migration schedule")
    observe(store, "trees and gardens", source_id="2")
    index(store, client=client)
    monkeypatch.setattr("work_memory.semantic.Ollama", lambda *_: client)
    result = store.search("migration", mode="hybrid")
    assert result["effective_mode"] == "hybrid" and result["items"][0]["id"] == a
    item = result["items"][0]
    assert (
        item["excerpt"]
        == store.observation(a)["content"][item["start_offset"] : item["end_offset"]]
    )
    assert result["score_kind"] == "reciprocal_rank_fusion"


def test_invalid_new_batch_keeps_successful_chunks_and_does_not_publish(tmp_path):
    store = Store(tmp_path)
    client = Embedder()
    oid = observe(store, "migration " * 600)
    index(store, client=client, max_batches=1, batch_size=1)
    client.embed = lambda *_: [[float("nan")]]
    with pytest.raises(SemanticUnavailable):
        index(store, client=client, batch_size=1)
    with store.connect() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM semantic_chunks WHERE observation_id=?", (oid,)
            ).fetchone()[0]
            == 1
        )
        assert (
            c.execute("SELECT state FROM semantic_jobs WHERE observation_id=?", (oid,)).fetchone()[
                0
            ]
            == "failed"
        )
