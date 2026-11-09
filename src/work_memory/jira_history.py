"""Native Jira field history, with no inferred business dates or AI calls."""

import json
from datetime import date

from .collectors import json_blocks
from .models import FactInput, Observation, Target, instant
from .store import Store, digest

PROJECTION = "jira-changelog-v1"
EXTRACTOR = "jira-history-v1"
FIELDS = {"status": "jira_status_id", "duedate": "jira_due_date"}


def _value(value):
    if value is None:
        return "null"
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Unsupported native field value")
    return str(value)


def normalize_history(entry: dict, target: Target) -> Observation:
    """Bind exact read-only request and issue; discard authors and unrelated changes."""
    request = entry.get("input", {})
    if (
        target.source != "jira"
        or request.get("cloudId") != target.cloud_id
        or request.get("issueIdOrKey") != target.source_id
        or request.get("updateHistory") is not False
        or request.get("expand") != "changelog"
        or not entry.get("tool", "").endswith("getJiraIssue")
    ):
        raise ValueError("History request does not match selected Jira issue")
    blocks = json_blocks(entry)
    if len(blocks) != 1:
        raise ValueError("Ambiguous history response")
    nodes = blocks[0].get("issues", {}).get("nodes", [])
    if len(nodes) != 1 or nodes[0].get("key") != target.source_id:
        raise ValueError("Unexpected Jira issue")
    issue = nodes[0]
    fields = issue["fields"]
    history = issue.get("changelog", {})
    events = history.get("histories")
    if not isinstance(events, list) or not isinstance(history.get("total"), int):
        raise ValueError("Missing explicit history coverage")
    updated = instant(fields["updated"])
    current = {}
    if isinstance(fields.get("status"), dict) and "id" in fields["status"]:
        current["status"] = _value(fields["status"]["id"])
    if "duedate" in fields:
        current["duedate"] = _value(fields["duedate"])
    records = []
    ids = []
    for event in events:
        ids.append(str(event["id"]))
        for index, change in enumerate(event["items"]):
            # fieldId avoids confusing custom fields with a similar display name.
            field = change.get("fieldId")
            if field not in FIELDS:
                continue
            if "from" not in change or "to" not in change:
                raise ValueError("Native transition requires both endpoints")
            records.append(
                {
                    "history_id": str(event["id"]),
                    "item": index,
                    "created": event["created"],
                    "at": instant(event["created"]),
                    "fieldId": field,
                    "from": _value(change.get("from")),
                    "to": _value(change.get("to")),
                    "fromString": change.get("fromString"),
                    "toString": change.get("toString"),
                }
            )
    records.sort(key=lambda r: (r["at"], r["history_id"], r["item"]))
    complete = (
        history.get("startAt") == 0
        and len(events) == history["total"]
        and len(ids) == len(set(ids))
    )
    header = {"key": target.source_id, "current": current, "updated": updated}
    lines = [json.dumps(header, ensure_ascii=False, sort_keys=True)]
    lines += [json.dumps(r, ensure_ascii=False, sort_keys=True) for r in records]
    return Observation(
        source="jira",
        source_id=target.source_id,
        source_url=target.source_url,
        project=target.project,
        title=target.source_id + " — native field history",
        content="\n".join(lines),
        source_updated_at=updated,
        metadata={
            "projection": PROJECTION,
            "kind": "jira_issue_history",
            "coverage": "complete_history" if complete else "partial_history",
            "histories_read": len(events),
            "histories_total": history["total"],
            "record_hashes": [digest(line) for line in lines[1:]],
            "normalizer": PROJECTION,
        },
    )


def _facts(observation: dict, *, consistent_revision: bool = True) -> list[FactInput]:
    lines = observation["content"].splitlines()
    header = json.loads(lines[0])
    records = [json.loads(line) for line in lines[1:]]
    complete = observation["metadata"]["coverage"] == "complete_history"
    facts = []
    for field, current in header["current"].items():
        changes = [r for r in records if r["fieldId"] == field]
        chain = bool(changes) and complete and consistent_revision
        chain = chain and len({r["at"] for r in changes}) == len(changes)
        chain = chain and all(
            a["to"] == b["from"] for a, b in zip(changes, changes[1:], strict=False)
        )
        chain = chain and changes[-1]["to"] == current
        chain = chain and all(r["at"] <= header["updated"] for r in changes)
        if field == "duedate":
            try:
                for value in [current, *(r[k] for r in changes for k in ("from", "to"))]:
                    if value != "null":
                        date.fromisoformat(value)
            except ValueError:
                chain = False
        if not chain:
            # Literal current field; validity remains unknown even for complete empty history.
            facts.append(
                FactInput(
                    subject=header["key"],
                    predicate=FIELDS[field],
                    value=current,
                    certainty="explicit"
                    if complete and consistent_revision and not changes
                    else "uncertain",
                    quote=lines[0],
                )
            )
            continue
        for index, change in enumerate(changes):
            next_change = changes[index + 1] if index + 1 < len(changes) else None
            start = records.index(change) + 1
            end = records.index(next_change) + 1 if next_change else start
            quote = "\n".join(lines[start : end + 1])
            if len(quote) > 5000:
                # Do not assert an interval whose two boundaries cannot be cited together.
                continue
            facts.append(
                FactInput(
                    subject=header["key"],
                    predicate=FIELDS[field],
                    value=change["to"],
                    certainty="explicit",
                    valid_from=change["at"],
                    valid_to=next_change["at"] if next_change else None,
                    quote=quote,
                )
            )
    return facts


def derive_history(
    store: Store, observation_id: str | None = None, *, now: str | None = None
) -> int:
    count = 0
    ids = [observation_id] if observation_id else store.active_observation_ids()
    for oid in ids:
        observation = store.observation(oid)
        if observation["metadata"].get("projection") != PROJECTION:
            continue
        if store.extraction_state(oid, EXTRACTOR) == "complete":
            continue
        # Only append-consistent histories produce definite intervals after a revision.
        with store.connect() as db:
            previous = db.execute(
                "SELECT o.metadata,o.source_updated_at FROM available_observations o "
                "WHERE o.source='jira' AND o.source_id=? AND o.project=? AND o.id!=? "
                "AND json_extract(o.metadata,'$.projection')=? "
                "AND EXISTS (SELECT 1 FROM extraction_jobs e WHERE e.observation_id=o.id "
                "AND e.extractor=? AND e.state='complete') "
                "ORDER BY o.observed_at DESC,o.storage_rowid DESC LIMIT 1",
                (observation["source_id"], observation["project"], oid, PROJECTION, EXTRACTOR),
            ).fetchone()
        consistent = not previous or (
            set(json.loads(previous["metadata"])["record_hashes"])
            <= set(observation["metadata"]["record_hashes"])
            and previous["source_updated_at"] <= observation["source_updated_at"]
        )
        facts = _facts(observation, consistent_revision=consistent)
        count += len(
            store.add_facts(
                oid,
                facts,
                EXTRACTOR,
                now=now,
                replace_native_projection=True,
            )
        )
    return count
