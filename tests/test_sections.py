import pytest

from work_memory.budget import SpendingLimitExceeded
from work_memory.models import Extraction, FactInput, Observation, Settings
from work_memory.sections import MAX_SECTION_CHARS, plan_sections
from work_memory.store import Store
from work_memory.worker import EXTRACTOR_VERSION, sync

VERSION = EXTRACTOR_VERSION + ":haiku"


def content():
    return (
        "## Sheet: Alpha\nShared evidence\n"
        + "a" * 7_000
        + ("\n## Sheet: Beta\nShared evidence\n" + "b" * 7_000)
    )


def observe(store, text=None, source_id="1"):
    return store.ingest(
        Observation(
            source="fixture",
            source_id=source_id,
            project="test",
            source_url="fixture://test",
            title="Workbook",
            content=text or content(),
        )
    )["id"]


def assertion(subject="Alpha", quote="Shared evidence", **kwargs):
    return FactInput(subject=subject, predicate="goal", value="documented", quote=quote, **kwargs)


@pytest.mark.parametrize(
    "text",
    [
        "Small text",
        content(),
        "é🚀\n" * 12_000,
        "Large line " * 10_000,
        ("## Heading\nText\n" * 10_000),
    ],
)
def test_plan_covers_every_original_character_with_bounded_stable_slices(text):
    parts = plan_sections(text)
    assert parts == plan_sections(text)
    assert parts[0].start == 0 and parts[-1].end == len(text)
    assert all(a.end == b.start for a, b in zip(parts, parts[1:], strict=False))
    assert all(0 < p.end - p.start <= MAX_SECTION_CHARS for p in parts)
    assert "".join(text[p.start : p.end] for p in parts) == text
    assert len(parts) < 1_000


def test_paid_sections_survive_budget_interruption_and_process_restart(tmp_path):
    store = Store(tmp_path)
    oid = observe(store)
    old = store.add_facts(oid, [assertion("Previous")], "evidence-v1:haiku")[0]

    class Client:
        def __init__(self, allowance):
            self.calls = []
            self.allowance = allowance

        def acquire(self, _):
            raise AssertionError("Extraction must not fetch the source")

        def extract(self, observation, _):
            if len(self.calls) >= self.allowance:
                raise SpendingLimitExceeded("Synthetic budget interruption")
            self.calls.append(observation["section"]["label"])
            return Extraction(facts=[assertion(observation["section"]["label"])])

    first = Client(1)
    result = sync(store, Settings(), first, collect=False, observation_id=oid)
    assert result["state"] == "partial" and result["sections_completed"] == 1
    assert result["facts"] == 0 and store.facts()["total"] == 1
    assert store.extraction_state(oid, VERSION) == "pending"
    assert store.status()["observations_without_completed_extraction"] == 1

    store = Store(tmp_path)
    second = Client(10)
    result = sync(store, Settings(), second, collect=False, observation_id=oid)
    assert result["state"] == "complete" and result["sections_completed"] == 1
    assert second.calls == ["## Sheet: Beta"]
    assert store.facts()["total"] == 3  # Omission by a new extractor is not a withdrawal.
    assert old in {f["id"] for f in store.facts()["items"]}
    beta = next(f for f in store.facts()["items"] if f["subject"] == "## Sheet: Beta")
    assert beta["start_offset"] > content().index("## Sheet: Beta")
    assert content()[beta["start_offset"] : beta["end_offset"]] == beta["quote"]
    assert beta["evidence_section"] == "## Sheet: Beta"
    assert beta["current_extraction_state"] == "complete"
    assert store.status()["extraction_parts"][0]["completed"] == 2
    assert store.status()["observations_without_completed_extraction"] == 0
    result = sync(store, Settings(), second, collect=False, observation_id=oid)
    assert result["facts"] == 0 and len(second.calls) == 1


def test_part_validation_and_incomplete_plan_never_publish_partial_facts(tmp_path):
    store = Store(tmp_path)
    oid = observe(store)
    store.prepare_extractions(VERSION)
    alpha, beta = store.extraction_parts(oid, VERSION)
    with pytest.raises(ValueError, match="source section"):
        store.save_extraction_part(oid, VERSION, alpha["part_id"], [assertion(quote="Sheet: Beta")])
    assert store.extraction_parts(oid, VERSION)[0]["state"] == "pending"
    provenance = {"rules": "fixture", "prompt_sha256": "a" * 64, "reported_models": []}
    store.save_extraction_part(oid, VERSION, alpha["part_id"], [assertion()], provenance=provenance)
    with pytest.raises(ValueError, match="incomplete"):
        store.publish_extraction_parts(oid, VERSION)
    assert store.facts()["total"] == 0
    store.prepare_extractions(VERSION)
    assert store.extraction_parts(oid, VERSION)[0]["state"] == "complete"
    store.save_extraction_part(oid, VERSION, beta["part_id"], [])
    store.publish_extraction_parts(oid, VERSION)
    assert store.facts()["total"] == 1
    assert store.facts()["items"][0]["extraction_provenance"] == provenance


def test_bad_replacement_rejects_publication_without_losing_paid_results(tmp_path):
    store = Store(tmp_path)
    oid = observe(store)
    store.prepare_extractions(VERSION)
    alpha, beta = store.extraction_parts(oid, VERSION)
    store.save_extraction_part(oid, VERSION, alpha["part_id"], [assertion()])
    store.save_extraction_part(
        oid,
        VERSION,
        beta["part_id"],
        [
            assertion(
                "Beta", supersedes="missing-fact", valid_from="2026-01-01", certainty="explicit"
            )
        ],
    )
    with pytest.raises(ValueError, match="unavailable"):
        store.publish_extraction_parts(oid, VERSION)
    assert store.facts()["total"] == 0
    assert all(p["state"] == "complete" for p in store.extraction_parts(oid, VERSION))
    assert store.extraction_state(oid, VERSION) == "pending"


def test_revocation_blocks_staged_results_and_state(tmp_path):
    store = Store(tmp_path)
    oid = observe(store)
    store.prepare_extractions(VERSION)
    part = store.extraction_parts(oid, VERSION)[0]
    store.save_extraction_part(oid, VERSION, part["part_id"], [assertion()])
    store.withdraw("fixture", "1", "test")
    with pytest.raises(KeyError):
        store.publish_extraction_parts(oid, VERSION)
    with pytest.raises(KeyError):
        store.save_extraction_part(oid, VERSION, part["part_id"], [])
    assert store.status()["extraction_parts"] == []
    assert store.pending_extractions(VERSION) == []


def test_reprocessing_preserves_rejection_and_does_not_duplicate_unchanged_fact(tmp_path):
    store = Store(tmp_path)
    oid = observe(store, "Shared evidence")
    fact = assertion()
    fid = store.add_facts(oid, [fact], "evidence-v1:haiku")[0]
    store.review_fact(fid, "rejected", "Unsupported interpretation")
    store.prepare_extractions(VERSION)
    part = store.extraction_parts(oid, VERSION)[0]
    store.save_extraction_part(oid, VERSION, part["part_id"], [fact])
    assert store.publish_extraction_parts(oid, VERSION) == [fid]
    assert store.facts()["total"] == 0
    assert len(store.history(fid)["items"]) == 2


def test_targeted_extraction_cannot_process_other_observations(tmp_path):
    store = Store(tmp_path)
    a = observe(store, "First")
    b = observe(store, "Second", "2")
    store.prepare_extractions(VERSION, a)
    assert store.pending_extractions(VERSION, observation_id=a) == [a]
    assert store.extraction_parts(b, VERSION) == []
    with pytest.raises(KeyError):
        store.prepare_extractions(VERSION, "unavailable")
