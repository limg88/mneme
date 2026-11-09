from pathlib import Path

import pytest

from work_memory.access import AccessControlUnavailable
from work_memory.backups import backup, restore
from work_memory.budget import Budget
from work_memory.models import FactInput, Observation
from work_memory.store import Store


def proof(_):
    return {"filevault": "test fixture, not a real encryption check"}


def observe(store, project="test"):
    return store.ingest(
        Observation(
            source="fixture",
            source_id="1",
            project=project,
            source_url="fixture://1",
            title="Work",
            content="Deadline Monday",
        )
    )["id"]


def test_old_snapshot_cannot_resurrect_withdrawn_content_or_reset_spending(tmp_path):
    store = Store(tmp_path / "memory")
    oid = observe(store)
    fid = store.add_facts(
        oid,
        [FactInput(subject="task", predicate="deadline", value="Monday", quote="Deadline Monday")],
        "test",
    )[0]
    budget = Budget(tmp_path / "budget.db")
    store.set_runtime_policy("budget_file", str(budget.path))
    r, _ = budget.reserve("haiku")
    budget.settle(r, 0.01)
    snapshot = Path(backup(store, verifier=proof)["snapshot"])
    r, _ = budget.reserve("haiku")
    budget.settle(r, 0.02)
    store.withdraw("fixture", "1", "test")
    destination = tmp_path / "recovered"
    restore(store, snapshot, destination, verifier=proof)
    recovered = Store(destination)
    assert recovered.search("Monday")["total"] == 0
    assert recovered.facts()["total"] == 0
    with pytest.raises(KeyError):
        recovered.observation(oid)
    with pytest.raises(KeyError):
        recovered.history(fid)
    with pytest.raises(ValueError, match="withdrawn"):
        observe(recovered)
    assert recovered.status()["spending"]["total_accounted"] == 0.03
    assert recovered.token() != store.token()


def test_quarantine_survives_restoring_snapshot_before_the_quarantine(tmp_path):
    store = Store(tmp_path / "memory")
    oid = observe(store)
    snapshot = Path(backup(store, verifier=proof)["snapshot"])
    store.quarantine_observation(oid, "Corrupt response")
    restore(store, snapshot, tmp_path / "recovered", verifier=proof)
    recovered = Store(tmp_path / "recovered")
    assert recovered.search("Deadline")["total"] == 0
    with pytest.raises(KeyError):
        recovered.observation(oid)


def test_global_revocation_covers_all_projects_and_future_imports(tmp_path):
    store = Store(tmp_path / "memory")
    observe(store, "A")
    observe(store, "B")
    assert store.revoke_source("fixture", "1", "Confirmed provider revocation") == 2
    assert store.status()["sources"] == []
    with pytest.raises(ValueError):
        observe(store, "new project")


def test_ledger_blocks_reads_even_if_memory_update_was_interrupted(tmp_path):
    store = Store(tmp_path / "memory")
    oid = observe(store)
    store.access_ledger.block_source(
        store.origin, "fixture", "1", "test", "Simulated crash after deny"
    )
    with store.connect() as db:
        assert db.execute("SELECT active FROM observations WHERE id=?", (oid,)).fetchone()[0] == 1
    assert store.search("Monday")["total"] == 0
    with pytest.raises(KeyError):
        store.observation(oid)


def test_missing_authority_is_not_silently_recreated(tmp_path):
    store = Store(tmp_path / "memory")
    observe(store)
    path = store.access_ledger.path
    path.unlink()
    with pytest.raises(AccessControlUnavailable):
        store.search("Deadline")
    with pytest.raises(AccessControlUnavailable):
        Store(store.directory)
    assert not path.exists()


def test_snapshot_tampering_and_overwriting_are_refused(tmp_path):
    store = Store(tmp_path / "memory")
    observe(store)
    snapshot = Path(backup(store, verifier=proof)["snapshot"])
    with pytest.raises(ValueError, match="new directory"):
        restore(store, snapshot, store.directory, verifier=proof)
    with (snapshot / "memory.sqlite3").open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        restore(store, snapshot, tmp_path / "recovered", verifier=proof)


def test_restored_snapshot_is_not_presented_as_revalidated_current_truth(tmp_path):
    store = Store(tmp_path / "memory")
    oid = observe(store)
    store.add_facts(
        oid,
        [
            FactInput(
                subject="task",
                predicate="deadline",
                value="Monday",
                quote="Deadline Monday",
                certainty="explicit",
                valid_from="2026-01-01",
            )
        ],
        "test",
    )
    assert store.facts()["items"][0]["definitive"]
    snapshot = Path(backup(store, verifier=proof)["snapshot"])
    restore(store, snapshot, tmp_path / "recovered", verifier=proof)
    fact = Store(tmp_path / "recovered").facts()["items"][0]
    assert not fact["definitive"] and fact["restore_requires_resync"]


def test_restore_catchup_preserves_token_spending_and_revocations(tmp_path):
    from work_memory.backups import catch_up_from_live

    live = Store(tmp_path / "memory")
    oid = observe(live)
    budget = Budget(tmp_path / "budget.db")
    live.set_runtime_policy("budget_file", str(budget.path))
    snap = Path(backup(live, verifier=proof)["snapshot"])
    restore(live, snap, tmp_path / "recovered", verifier=proof)
    recovered = Store(tmp_path / "recovered")
    token = recovered.token()
    second = live.ingest(
        Observation(
            source="fixture",
            source_id="new",
            project="test",
            source_url="fixture://new",
            title="New",
            content="New decision",
        )
    )["id"]
    live.revoke_source("fixture", "1", "Provider revocation after restore")
    rid, _ = budget.reserve("haiku")
    budget.settle(rid, 0.01)
    result = catch_up_from_live(recovered, live, verifier=proof)
    assert result["state"] == "caught_up" and recovered.token() == token
    assert recovered.observation(second)["content"] == "New decision"
    with pytest.raises(KeyError):
        recovered.observation(oid)
    assert recovered.runtime_policy("restore_state")["requires_resync"] is False
    assert recovered.status()["spending"]["total_accounted"] == 0.01
    assert "inherited" in recovered.runtime_policy("restore_state")["limitations"][0]


def test_restore_catchup_refuses_new_target_work_and_other_origins(tmp_path):
    from work_memory.backups import catch_up_from_live

    live = Store(tmp_path / "memory")
    observe(live)
    snap = Path(backup(live, verifier=proof)["snapshot"])
    restore(live, snap, tmp_path / "recovered", verifier=proof)
    recovered = Store(tmp_path / "recovered")
    with pytest.raises(ValueError, match="same memory origin"):
        catch_up_from_live(recovered, Store(tmp_path / "unrelated"), verifier=proof)
    oid = recovered.ingest(
        Observation(
            source="fixture",
            source_id="new",
            project="test",
            source_url="fixture://new",
            title="New",
            content="New local work",
        )
    )["id"]
    with pytest.raises(ValueError, match="new work"):
        catch_up_from_live(recovered, live, verifier=proof)
    assert recovered.observation(oid)["content"] == "New local work"
    assert recovered.runtime_policy("restore_state")["requires_resync"] is True


def test_file_uri_aliases_cannot_bypass_revocation_or_restore(tmp_path):
    live = Store(tmp_path / "memory")
    ids = []
    for index, source_id in enumerate(["drive/1", "file:///drive/1"]):
        ids.append(
            live.ingest(
                Observation(
                    source="microsoft365",
                    source_id=source_id,
                    project=str(index),
                    source_url="https://example.sharepoint.com/file",
                    title="Selected file",
                    content="Restricted source",
                )
            )["id"]
        )
    snapshot = Path(backup(live, verifier=proof)["snapshot"])
    assert live.revoke_source("microsoft365", "drive/1", "Confirmed provider denial") == 2
    restore(live, snapshot, tmp_path / "recovered", verifier=proof)
    recovered = Store(tmp_path / "recovered")
    for oid in ids:
        with pytest.raises(KeyError):
            recovered.observation(oid)
    assert recovered.search("Restricted")["total"] == 0
    assert recovered.is_withdrawn("microsoft365", "file:///drive/1", "new-project")
