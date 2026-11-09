import json

import pytest

from work_memory.local_extraction import LocalExtractionUnavailable, LocalExtractor, extract_local
from work_memory.models import Extraction, FactInput, Observation, Settings
from work_memory.store import Store
from work_memory.worker import sync

DIGEST = "a" * 64


class Runtime:
    def __init__(self, model):
        self.model = model

    def digest(self):
        return DIGEST


@pytest.fixture
def extractor(monkeypatch):
    monkeypatch.setattr("work_memory.local_extraction.Ollama", Runtime)
    return LocalExtractor()


def observation(text="Planned delivery in June"):
    return {
        "source": "local",
        "source_id": "/selected/file.md",
        "source_url": "file:///selected/file.md",
        "project": "pilot",
        "title": "Plan",
        "content": text,
        "metadata": {"kind": "local_document"},
    }


def reply(response=None, **kwargs):
    data = {
        "done": True,
        "done_reason": "stop",
        "model": "qwen3:8b",
        "response": json.dumps(response if response is not None else {"facts": []}),
        "prompt_eval_count": 100,
        "eval_count": 50,
    }
    return {**data, **kwargs}


def test_local_generation_schema_and_provenance_without_tools(extractor, monkeypatch):
    calls = []

    def generate(data):
        calls.append(data)
        return reply(
            {
                "facts": [
                    {
                        "subject": "pilot",
                        "predicate": "planned_delivery",
                        "value": "June",
                        "certainty": "proposed",
                        "quote": "Planned delivery in June",
                    }
                ]
            }
        )

    monkeypatch.setattr(extractor, "generate", generate)
    result = extractor.extract(observation())
    assert result.facts[0].certainty == "proposed" and result.facts[0].valid_from is None
    assert calls[0]["format"] == "json"
    assert json.dumps(Extraction.model_json_schema()) in calls[0]["prompt"]
    assert calls[0]["stream"] is False and calls[0]["think"] is False
    assert calls[0]["options"]["num_predict"] == 2048
    assert "tools" not in calls[0] and "previous_facts" in calls[0]["prompt"]
    assert extractor.last_extraction_provenance["model_digest"] == DIGEST
    assert extractor.last_extraction_provenance["cloud_calls"] == 0


@pytest.mark.parametrize(
    "result",
    [
        reply(done_reason="length"),
        reply(done=False),
        reply(model="another"),
        reply(response="invalid-json"),
    ],
)
def test_incomplete_or_unexpected_generation_is_never_published(extractor, monkeypatch, result):
    monkeypatch.setattr(extractor, "generate", lambda data: result)
    with pytest.raises(LocalExtractionUnavailable):
        extractor.extract(observation())


def test_context_bound_and_weights_change_stop_processing(extractor, monkeypatch):
    def unexpected(data):
        pytest.fail("Context must fail before generation")

    monkeypatch.setattr(extractor, "generate", unexpected)
    with pytest.raises(LocalExtractionUnavailable, match="context budget"):
        extractor.extract(observation("🙂" * 12000))
    monkeypatch.setattr(extractor.runtime, "digest", lambda: "b" * 64)
    with pytest.raises(LocalExtractionUnavailable, match="model changed"):
        extractor.extract(observation())


class FakeExtractor:
    version = "local-test:weights-a"
    last_extraction_provenance = {"engine": "local fake test", "cloud_calls": 0}

    def __init__(self):
        self.calls = 0

    def extract(self, item):
        self.calls += 1
        text = item["content"][:30]
        return Extraction(
            facts=[
                FactInput(
                    subject="pilot",
                    predicate="statement",
                    value=text,
                    quote=text,
                    certainty="uncertain",
                )
            ]
        )


def test_local_parts_resume_and_publish_atomically_with_correct_active_status(tmp_path):
    store = Store(tmp_path)
    oid = store.ingest(
        Observation(**observation("Alpha\n" + "a\n" * 6500 + "Beta\n" + "b\n" * 100))
    )["id"]
    store.set_runtime_policy("active_extractor", "evidence-v2:haiku")
    client = FakeExtractor()
    first = extract_local(store, project="pilot", max_sections=1, client=client)
    assert first["state"] == "partial" and first["facts"] == 0 and client.calls == 1
    assert store.facts()["total"] == 0
    second = extract_local(store, observation_id=oid, max_sections=10, client=client)
    assert second["state"] == "complete" and second["facts"] >= 2 and client.calls == 4
    assert extract_local(store, observation_id=oid, client=client)["local_model_calls"] == 0
    assert store.status()["observations_without_completed_extraction"] == 0
    assert all(f["current_extraction_state"] == "complete" for f in store.facts()["items"])

    class Cloud:
        model = "haiku"
        cloud_calls = 0

        def extract(self, *args):
            pytest.fail("Local documents must not be sent to the cloud extractor")

    result = sync(store, Settings(), Cloud(), collect=False)
    assert result["local_extraction_deferred"] == 1 and result["cloud_calls"] == 0


def test_bad_quote_is_not_saved_and_errors_do_not_include_source_text(tmp_path):
    store = Store(tmp_path)
    store.ingest(Observation(**observation()))

    class Bad(FakeExtractor):
        def extract(self, item):
            return Extraction(
                facts=[
                    FactInput(subject="pilot", predicate="statement", value="bad", quote="invented")
                ]
            )

    result = extract_local(store, project="pilot", client=Bad())
    assert result["state"] == "partial" and result["errors"][0]["error"] == "ValueError"
    assert store.facts()["total"] == 0 and "invented" not in json.dumps(result)


def test_local_temporal_candidates_stay_unknown_until_validated(extractor, monkeypatch):
    monkeypatch.setattr(
        extractor,
        "generate",
        lambda data: reply(
            {
                "facts": [
                    {
                        "subject": "pilot",
                        "predicate": "planned_delivery",
                        "value": "June",
                        "quote": "Planned delivery in June",
                        "certainty": "proposed",
                        "valid_from": "2026-06-01",
                    }
                ]
            }
        ),
    )
    result = extractor.extract(observation())
    assert result.facts[0].valid_from is None and result.facts[0].valid_to is None
    assert extractor.last_extraction_provenance["temporal_policy"] == "validity_unknown"
    assert extractor.last_extraction_provenance["unvalidated_temporal_candidates"][0][
        "valid_from"
    ].startswith("2026-06-01")


def test_sync_collects_and_extracts_locally_with_a_shared_section_limit(tmp_path, monkeypatch):
    from work_memory.models import LocalScope

    folder = tmp_path / "selected"
    folder.mkdir()
    (folder / "a.md").write_text("Alpha planned delivery")
    (folder / "b.md").write_text("Beta planned delivery")
    store = Store(tmp_path / "memory")
    client = FakeExtractor()
    monkeypatch.setattr("work_memory.local_extraction.LocalExtractor", lambda model: client)
    settings = Settings(
        local_scopes=[LocalScope(root=str(folder), project="pilot")],
        local_model="qwen3:8b",
        max_local_sections_per_sync=1,
    )
    first = sync(store, settings, None)
    assert first["new"] == 2 and first["facts"] == 1 and first["state"] == "partial"
    assert first["cloud_calls"] == 0 and first["local_extraction"]["local_model_calls"] == 1
    second = sync(store, settings, None)
    assert second["new"] == 0 and second["facts"] == 1 and second["state"] == "complete"
    assert store.facts()["total"] == 2 and client.calls == 2
    third = sync(store, settings, None)
    assert third["facts"] == 0 and third["local_extraction"]["local_model_calls"] == 0


def test_local_sync_no_extract_does_not_start_a_model(tmp_path, monkeypatch):
    from work_memory.models import LocalScope

    folder = tmp_path / "selected"
    folder.mkdir()
    (folder / "a.md").write_text("Selected note")

    def unexpected(*args):
        pytest.fail("No extraction requested")

    monkeypatch.setattr("work_memory.local_extraction.LocalExtractor", unexpected)
    result = sync(
        Store(tmp_path / "memory"),
        Settings(
            local_scopes=[LocalScope(root=str(folder), project="pilot")], local_model="qwen3:8b"
        ),
        None,
        extract=False,
    )
    assert result["new"] == 1 and result["cloud_calls"] == 0


def test_cli_local_sync_needs_no_cloud_consent(tmp_path, monkeypatch, capsys):
    import sys

    from work_memory import cli

    monkeypatch.chdir(tmp_path)
    config = tmp_path / "settings.json"
    config.write_text(Settings(local_model="qwen3:8b").model_dump_json())

    def fake_sync(store, settings, client, **kwargs):
        assert client is None and settings.local_model == "qwen3:8b"
        return {"errors": [], "cloud_calls": 0}

    monkeypatch.setattr(cli, "sync", fake_sync)
    monkeypatch.setattr(
        sys,
        "argv",
        ["work-memory", "--data", str(tmp_path / "memory"), "sync", "--config", str(config)],
    )
    cli.main()
    assert json.loads(capsys.readouterr().out)["cloud_calls"] == 0
