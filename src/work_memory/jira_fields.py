"""Literal provider fields, never completion claims inferred from task descriptions."""

import json

from .models import FactInput, instant

EXTRACTOR = "jira-fields-v1"
PERSON_EXTRACTOR = "jira-person-ref-v1"
TOOLS = {
    "mcp__claude_ai_Atlassian_Rovo__getJiraIssue",
    "mcp__claude_ai_Atlassian_Rovo__searchJiraIssuesUsingJql",
}


def supported(observation):
    return (
        observation["source"] == "jira"
        and observation["metadata"].get("projection", "content") == "content"
        and observation["metadata"].get("tool") in TOOLS
    )


def property_spans(text):
    if not isinstance(json.loads(text), dict):
        raise ValueError("Expected a normalized Jira object")
    decoder = json.JSONDecoder()
    index = 0

    def whitespace(i):
        while i < len(text) and text[i].isspace():
            i += 1
        return i

    index = whitespace(index)
    if index >= len(text) or text[index] != "{":
        raise ValueError("Expected a normalized Jira object")
    index = whitespace(index + 1)
    result = {}
    while index < len(text) and text[index] != "}":
        start = index
        key, index = decoder.raw_decode(text, index)
        index = whitespace(index)
        if not isinstance(key, str) or key in result or text[index] != ":":
            raise ValueError("Ambiguous Jira field")
        index = whitespace(index + 1)
        value, index = decoder.raw_decode(text, index)
        result[key] = (value, text[start:index])
        index = whitespace(index)
        if index < len(text) and text[index] == ",":
            index = whitespace(index + 1)
        elif index < len(text) and text[index] == "}":
            break
        else:
            raise ValueError("Malformed Jira fields")
    if index >= len(text) or text[index] != "}" or text[index + 1 :].strip():
        raise ValueError("Incomplete Jira fields")
    return result


def field_facts(observation):
    fields = property_spans(observation["content"])
    facts = []
    people = []

    def add(key, predicate, value):
        if (
            key in fields
            and isinstance(value, str)
            and 0 < len(value) <= 2000
            and len(fields[key][1]) <= 5000
        ):
            facts.append(
                FactInput(
                    subject=observation["source_id"],
                    predicate=predicate,
                    value=value,
                    kind="activity",
                    certainty="explicit",
                    quote=fields[key][1],
                )
            )

    summary = fields.get("summary", (None, None))[0]
    add("summary", "jira_reported_summary", summary)
    status = fields.get("status", (None, None))[0]
    if isinstance(status, dict):
        add("status", "jira_reported_status", status.get("name"))
    assignee = fields.get("assignee", (None, None))[0]
    if isinstance(assignee, dict):
        account = assignee.get("accountId")
        label = assignee.get("displayName")
        add("assignee", "jira_reported_assignee_id", account)
        if not account:
            add("assignee", "jira_reported_assignee_label", label)
        if (
            isinstance(account, str)
            and account
            and isinstance(label, str)
            and 0 < len(label) <= 300
            and len(fields["assignee"][1]) <= 5000
        ):
            people.append(
                FactInput(
                    subject=label,
                    predicate="jira_provider_account_id",
                    value=account,
                    kind="person",
                    certainty="explicit",
                    quote=fields["assignee"][1],
                )
            )
    add("duedate", "jira_reported_due_date", fields.get("duedate", (None, None))[0])
    # Old normalizers mapped missing fields to null. Only a recorded presence list
    # permits distinguishing an explicit provider null from an unavailable field.
    present = observation["metadata"].get("provider_fields_present", [])
    for key, predicate in [
        ("assignee", "jira_reported_assignee_id"),
        ("duedate", "jira_reported_due_date"),
    ]:
        if key in present and key in fields and fields[key][0] is None:
            add(key, predicate, "null")
    return facts, people


def derive_fields(store, observation_id=None):
    total = 0
    for oid in [observation_id] if observation_id else store.active_observation_ids():
        observation = store.observation(oid)
        if not supported(observation):
            continue
        done = store.extraction_state(oid, EXTRACTOR) == "complete"
        people_done = store.extraction_state(oid, PERSON_EXTRACTOR) == "complete"
        if done and people_done:
            continue
        facts, people = field_facts(observation)
        if not done:
            with store.connect() as db:
                position = db.execute(
                    "SELECT storage_rowid FROM available_observations WHERE id=?", (oid,)
                ).fetchone()[0]
                previous = db.execute(
                    "SELECT n.observed_at,n.storage_rowid,n.source_updated_at "
                    "FROM available_observations n JOIN extraction_jobs j "
                    "ON j.observation_id=n.id WHERE n.source='jira' AND n.source_id=? "
                    "AND n.project=? AND j.extractor=? AND j.state='complete' "
                    "AND n.id!=?",
                    (
                        observation["source_id"],
                        observation["project"],
                        EXTRACTOR,
                        oid,
                    ),
                ).fetchall()
            newer = any(
                (r["observed_at"], r["storage_rowid"]) > (observation["observed_at"], position)
                for r in previous
            )
            try:
                stale = bool(observation["source_updated_at"]) and any(
                    r["source_updated_at"]
                    and instant(r["source_updated_at"]) > instant(observation["source_updated_at"])
                    for r in previous
                )
            except ValueError:
                stale = True
            if stale:
                facts = [f.model_copy(update={"certainty": "uncertain"}) for f in facts]
            total += len(
                store.add_facts(
                    oid,
                    [] if newer else facts,
                    EXTRACTOR,
                    replace_native_projection=not bool(newer) and not stale,
                )
            )
        if not people_done:
            total += len(store.add_facts(oid, people, PERSON_EXTRACTOR))
    return total
