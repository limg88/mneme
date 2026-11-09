"""Conservative subject references: provider IDs resolve; names remain scoped mentions."""

import hashlib
import json
from urllib.parse import parse_qs, urlsplit


def subject_identity(row):
    label = row["subject"]
    try:
        url = urlsplit(row["source_url"])
    except ValueError:
        url = urlsplit("")
    source_ref = (
        row["source_id"].removeprefix("file:///")
        if row["source"] == "microsoft365"
        else row["source_id"]
    )
    namespace = f"{row['project']}:{row['source']}:{source_ref}"
    identifier = label
    resolution = "source_scoped_label"
    if row["source"] == "jira" and label == row["source_id"] and row["kind"] != "person":
        namespace = "jira-issue:" + (url.hostname or namespace)
        resolution = "source_identifier"
    elif (
        row["source"] == "jira"
        and row["kind"] == "project"
        and row["source_id"].rsplit("-", 1)[0] == label
    ):
        namespace = "jira-project:" + (url.hostname or namespace)
        resolution = "source_identifier"
    elif row["kind"] == "person":
        assignee = json.loads(row.get("identity_assignee") or "{}")
        actor = json.loads(row.get("identity_actor") or "{}")
        assignee = assignee if isinstance(assignee, dict) else {}
        actor = actor if isinstance(actor, dict) else {}
        if (
            row["source"] == "jira"
            and label in (assignee.get("displayName"), assignee.get("accountId"))
            and isinstance(assignee.get("accountId"), str)
            and assignee["accountId"]
        ):
            namespace = "jira-account:" + (url.hostname or namespace)
            identifier = assignee["accountId"]
            resolution = "provider_identifier"
        elif (
            row["source"] == "microsoft365"
            and actor.get("provider") == "microsoft365"
            and actor.get("label") == label
            and isinstance(actor.get("id"), str)
            and actor["id"]
        ):
            tenant = parse_qs(url.query).get("tenantId", [])
            namespace = "microsoft-account:" + (tenant[0] if len(tenant) == 1 else namespace)
            identifier = actor["id"]
            resolution = "provider_identifier" if len(tenant) == 1 else "source_scoped_identifier"
    key = hashlib.sha256(
        json.dumps([namespace, identifier], ensure_ascii=False).encode()
    ).hexdigest()
    return {
        "key": key,
        "namespace": namespace,
        "identifier": identifier,
        "label": label,
        "resolution": resolution,
        "cross_source_link": "not_inferred",
        "resolved": resolution in {"provider_identifier", "source_identifier"},
    }
