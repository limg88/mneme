import json

from work_memory.models import FactInput, Observation
from work_memory.store import Store


def person(store, key, account, name="Mario Rossi", value="active", host="example.atlassian.net"):
    text = json.dumps({"assignee": {"displayName": name, "accountId": account}, "statement": value})
    oid = store.ingest(
        Observation(
            source="jira",
            source_id=key,
            project="pilot",
            source_url="https://" + host + "/browse/" + key,
            title=key,
            content=text,
        ),
        now="2026-01-01",
    )["id"]
    store.add_facts(
        oid,
        [
            FactInput(
                subject=name,
                predicate="status",
                value=value,
                kind="person",
                certainty="explicit",
                valid_from="2026-01-01",
                quote=text,
            )
        ],
        "test",
        now="2026-01-02",
    )


def test_homonyms_are_separate_and_aliases_require_provider_ids(tmp_path):
    store = Store(tmp_path)
    person(store, "P-1", "account-a", value="active")
    person(store, "P-2", "account-b", value="inactive")
    facts = store.facts(known_at="2026-02-01")["items"]
    assert len({f["subject_identity"]["key"] for f in facts}) == 2
    assert all(not f["conflicting"] and f["subject_identity"]["resolved"] for f in facts)
    person(store, "P-3", "account-a", name="Rossi, Mario", value="inactive")
    facts = store.facts(known_at="2026-02-01")["items"]
    same = [f for f in facts if f["subject_identity"]["identifier"] == "account-a"]
    assert len(same) == 2 and len({f["subject_identity"]["key"] for f in same}) == 1
    assert all(f["conflicting"] and not f["definitive"] for f in same)
    history = store.history(same[0]["id"])["items"]
    assert len(history) == 2
    assert {f["subject_identity"]["identifier"] for f in history} == {"account-a"}


def test_provider_namespaces_and_unresolved_names_are_never_merged(tmp_path):
    store = Store(tmp_path)
    person(store, "P-1", "account-a", host="one.atlassian.net")
    person(store, "P-2", "account-a", host="two.atlassian.net")
    person(store, "P-3", None)
    items = store.facts(known_at="2026-02-01")["items"]
    assert len({f["subject_identity"]["key"] for f in items}) == 3
    unknown = next(f for f in items if not f["subject_identity"]["resolved"])
    assert (
        not unknown["definitive"]
        and unknown["subject_identity"]["resolution"] == "source_scoped_label"
    )
    assert all("identity_assignee" not in f and "identity_actor" not in f for f in items)


def test_new_unprocessed_evidence_does_not_leak_to_past_knowledge(tmp_path):
    store = Store(tmp_path)
    old = Observation(
        source="fixture",
        source_id="one",
        project="pilot",
        source_url="fixture://one",
        title="Plan",
        content="Monday deadline",
    )
    oid = store.ingest(old, now="2026-01-01")["id"]
    store.add_facts(
        oid,
        [
            FactInput(
                subject="pilot",
                predicate="deadline",
                value="Monday",
                quote="Monday deadline",
                certainty="explicit",
                valid_from="2026-01-01",
            )
        ],
        "test",
        now="2026-01-02",
    )
    new = store.ingest(
        old.model_copy(update={"content": "Tuesday proposed deadline"}), now="2026-02-01"
    )["id"]
    before = store.facts(known_at="2026-01-15")["items"][0]
    assert before["definitive"] and not before["pending_source_analysis"]
    after = store.facts(known_at="2026-02-02")["items"][0]
    assert not after["definitive"] and after["pending_source_analysis"]
    store.add_facts(new, [], "test", now="2026-02-03")
    latest = store.facts(known_at="2026-02-04")["items"][0]
    assert latest["value"] == "Monday" and not latest["pending_source_analysis"]
    # Processing is not evidence that an omitted old fact became false.
    assert latest["definitive"]


def test_publication_time_and_scope_do_not_rewrite_past_analysis_state(tmp_path):
    from work_memory.jira_fields import derive_fields

    store = Store(tmp_path)
    original = Observation(
        source="jira",
        source_id="P-1",
        project="pilot",
        source_url="https://example.atlassian.net/browse/P-1",
        title="Release",
        content=json.dumps(
            {
                "summary": "Release",
                "description": "From 2026-01-01, release deadline is Monday.",
                "status": {"name": "Open"},
            }
        ),
        metadata={"tool": "mcp__claude_ai_Atlassian_Rovo__getJiraIssue"},
    )
    old = store.ingest(original, now="2026-01-01")["id"]
    store.add_facts(
        old,
        [
            FactInput(
                subject="P-1",
                predicate="deadline",
                value="Monday",
                quote="From 2026-01-01, release deadline is Monday.",
                certainty="explicit",
                valid_from="2026-01-01",
            )
        ],
        "evidence-v2:haiku",
        now="2026-01-02",
    )
    candidate = store.facts(known_at="2026-01-02")["items"][0]
    assert candidate["needs_semantic_validation"] and not candidate["definitive"]
    store.review_fact(
        candidate["id"], "supported", "Explicit dated commitment in the source.", now="2026-01-03"
    )
    newer = store.ingest(
        original.model_copy(
            update={
                "content": json.dumps(
                    {
                        "summary": "Release",
                        "description": "Proposed Tuesday deadline, pending approval.",
                        "status": {"name": "Open"},
                    }
                )
            }
        ),
        now="2026-02-01",
    )["id"]
    derive_fields(store, newer)

    def old_fact(when):
        return next(
            f
            for f in store.facts(known_at=when, limit=100)["items"]
            if f["predicate"] == "deadline"
        )

    assert old_fact("2026-02-02")["pending_source_analysis"]  # Native fields do not analyze prose.
    store.add_facts(newer, [], "evidence-v2:haiku", now="2026-03-01")
    assert old_fact("2026-02-02")[
        "pending_source_analysis"
    ]  # A later publication is not retroactive.
    assert not old_fact("2026-03-02")["pending_source_analysis"]
    assert old_fact("2026-03-02")["definitive"]  # A proposal did not replace the commitment.


def test_identity_key_retrieves_verified_aliases(tmp_path):
    store = Store(tmp_path)
    person(store, "P-1", "account-a")
    person(store, "P-2", "account-a", name="Rossi, Mario")
    key = store.facts(known_at="2026-02-01")["items"][0]["subject_identity"]["key"]
    assert store.facts(identity_key=key, known_at="2026-02-01")["total"] == 2
