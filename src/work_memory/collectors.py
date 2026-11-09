"""Bounded collection with resumable cursors. Search absence never deletes evidence."""

import json
import re
from datetime import datetime, timedelta
from urllib.parse import unquote, urlsplit

from .communications import (
    ConfirmedSourceRevocation,
    calendar_observation,
    calendar_time,
    communication_eligibility,
    mail_observation,
    teams_observation,
)
from .models import (
    CalendarScope,
    CollectionScope,
    MailScope,
    Settings,
    Target,
    TeamsScope,
    instant,
    utcnow,
)
from .store import Store, digest
from .worker import TOOLS, observations_from_evidence


class CalendarWindowLimit(ValueError):
    """The provider explicitly reports incomplete filtering of a broad window."""


class EmptyProviderOutput(ValueError):
    """No structured result is not evidence that a source search is empty."""


CALENDAR_LIMIT_NOTICE = re.compile(
    r"Note: only 1000 events in the date range were checked against the filters, "
    r"so some matches may be missing\. The events checked start at "
    r"[0-9T:.+Z-]+ or earlier\. To check later events, search again keeping the other "
    r"parameters, afterDateTime set to [0-9T:.+Z-]+ and offset 0\."
)
JIRA_SEARCH = "mcp__claude_ai_Atlassian_Rovo__searchJiraIssuesUsingJql"
SP_SEARCH = "mcp__claude_ai_Microsoft_365__sharepoint_search"
MAIL_SEARCH = "mcp__claude_ai_Microsoft_365__outlook_email_search"
CALENDAR_SEARCH = "mcp__claude_ai_Microsoft_365__outlook_calendar_search"
TEAMS_SEARCH = "mcp__claude_ai_Microsoft_365__chat_message_search"
FIELDS = [
    "summary",
    "description",
    "status",
    "assignee",
    "created",
    "updated",
    "duedate",
    "resolutiondate",
]


def scope_id(scope: CollectionScope | MailScope | CalendarScope | TeamsScope) -> str:
    values = scope.model_dump(
        exclude={"page_size", "max_pages_per_sync", "overlap_seconds", "reconcile_seconds"}
    )
    if not values.get("native_history"):
        values.pop("native_history", None)  # Preserve existing scope checkpoints.
    return digest(json.dumps(values, sort_keys=True))


def epoch_ms(value: str) -> int:
    return int(datetime.fromisoformat(instant(value)).timestamp() * 1000)


def year_before(value: str) -> str:
    time = datetime.fromisoformat(value)
    try:
        return time.replace(year=time.year - 1).isoformat()
    except ValueError:
        return time.replace(year=time.year - 1, day=28).isoformat()


def within_folder(url: str, folder: str) -> bool:
    actual, expected = urlsplit(url), urlsplit(folder)
    path = unquote(actual.path)
    if any(p in {"..", "."} for p in path.split("/")):
        return False
    return (
        actual.scheme == "https"
        and actual.netloc.casefold() == expected.netloc.casefold()
        and path.casefold().startswith(unquote(expected.path).rstrip("/").casefold() + "/")
    )


def json_blocks(entry: dict) -> list[dict]:
    response = entry["response"]
    if response.get("is_error"):
        raise ValueError("Collection tool returned an error")
    content = response["content"]
    if (
        content == ""
        or content == []
        or content == f"({entry.get('tool')} completed with no output)"
    ):
        raise EmptyProviderOutput("Provider returned no structured pagination result")
    blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
    result = []
    for block in blocks:
        if block.get("type") != "text":
            continue
        rest = block["text"].strip()
        while rest:
            value, end = json.JSONDecoder().raw_decode(rest)
            if not isinstance(value, dict):
                raise ValueError("Expected JSON object in collection response")
            result.append(value)
            rest = rest[end:].lstrip()
    return result


def jira_page(entry: dict) -> tuple[list[dict], str | None]:
    blocks = json_blocks(entry)
    if len(blocks) != 1:
        raise ValueError("Ambiguous Jira search response")
    issues = blocks[0].get("issues")
    if not isinstance(issues, dict) or not isinstance(issues.get("nodes"), list):
        raise ValueError("Unsupported Jira search envelope")
    info = issues.get("pageInfo", {})
    if not isinstance(info.get("hasNextPage"), bool):
        raise ValueError("Missing explicit Jira pagination status")
    cursor = info.get("endCursor") if info["hasNextPage"] else None
    if info["hasNextPage"] and not isinstance(cursor, str):
        raise ValueError("Missing Jira continuation token")
    return issues["nodes"], cursor


def sharepoint_page(entry: dict) -> tuple[list[dict], int | None]:
    objects = json_blocks(entry)
    footers = [obj for obj in objects if "moreResults" in obj]
    if (
        len(objects) == 1
        and type(objects[0].get("totalResultCount")) is int
        and objects[0]["totalResultCount"] == 0
    ):
        return [], None
    totals = [obj for obj in objects if set(obj) == {"totalResultCount"}]
    if not footers and len(totals) == 1 and type(totals[0]["totalResultCount"]) is int:
        hits = [obj for obj in objects if "uri" in obj]
        offset = entry.get("input", {}).get("offset")
        limit = entry.get("input", {}).get("limit")
        total = totals[0]["totalResultCount"]
        if (
            len(hits) + 1 != len(objects)
            or type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 50
            or len(hits) > limit
            or any(type(h.get("offset")) is not int for h in hits)
            or offset + len(hits) > total
            or [h.get("offset") for h in hits] != list(range(offset, offset + len(hits)))
            or (not hits and offset != total)
        ):
            raise ValueError("Inconsistent SharePoint total and item offsets")
        end = offset + len(hits)
        return hits, end if end < total else None
    if len(footers) != 1 or not isinstance(footers[0]["moreResults"], bool):
        raise ValueError("Missing explicit SharePoint pagination status")
    footer = footers[0]
    hits = [obj for obj in objects if "uri" in obj]
    if len(hits) + 1 != len(objects):
        raise ValueError("Unknown SharePoint response block")
    offset = footer.get("nextOffset") if footer["moreResults"] else None
    if footer["moreResults"] and (type(offset) is not int or offset < 1):
        raise ValueError("Missing SharePoint continuation offset")
    return hits, offset


def mail_page(entry: dict) -> tuple[list[dict], int | str | None]:
    objects = json_blocks(entry)
    if (
        len(objects) == 1
        and type(objects[0].get("totalResultCount")) is int
        and objects[0]["totalResultCount"] == 0
    ):
        return [], None
    footers = [d for d in objects if "moreResults" in d]
    if not footers and any(set(d) == {"totalResultCount"} for d in objects):
        return sharepoint_page(entry)
    hits = [d for d in objects if "uri" in d]
    if (
        len(footers) != 1
        or len(hits) + 1 != len(objects)
        or type(footers[0]["moreResults"]) is not bool
    ):
        raise ValueError("Missing explicit mail pagination status")
    footer = footers[0]
    if not footer["moreResults"]:
        return hits, None
    cursor = footer.get("nextCursor") or footer.get("nextOffset")
    if not ((type(cursor) is str and cursor) or (type(cursor) is int and cursor > 0)):
        raise ValueError("Mail continuation cursor missing")
    return hits, cursor


class Collector:
    def __init__(self, store: Store, client):
        self.store, self.client = store, client
        self.observed, self.created = 0, 0

    def _record(self, item):
        result = self.store.ingest(item)
        self.observed += 1
        self.created += int(result["created"])

    def collect(self, scope: CollectionScope) -> dict:
        sid = scope_id(scope)
        state = self.store.collection_state(sid)
        if not state.get("scan_upper"):
            upper = utcnow()
            full = (
                not state.get("last_full_scan")
                or (
                    datetime.fromisoformat(upper) - datetime.fromisoformat(state["last_full_scan"])
                ).total_seconds()
                >= scope.reconcile_seconds
            )
            lower = None if full else state.get("watermark")
            if lower:
                lower = (
                    datetime.fromisoformat(lower) - timedelta(seconds=scope.overlap_seconds)
                ).isoformat()
            elif scope.source in {"jira", "outlook", "calendar", "teams"}:
                lower = scope.since or year_before(upper)
            else:
                lower = scope.since  # Include current documents regardless of their age.
            state.update(
                scan_upper=upper,
                scan_lower=lower,
                cursor=None,
                seen_cursors=[],
                state="scanning",
                pages=0,
                full_scan=full,
                scan_page_size=scope.page_size,
                page_completed_uris=[],
                calendar_windows=[],
            )
            state.pop("retry_with_fresh_window", None)
        state.update(state="scanning", error=None)
        summary = {"scope_id": sid, "source": scope.source, "observed": 0, "new": 0, "pages": 0}
        try:
            for _ in range(scope.max_pages_per_sync):
                if scope.source == "jira":
                    items, cursor = self._jira(scope, state)
                elif scope.source in {"outlook", "calendar", "teams"}:
                    try:
                        items, cursor = self._outlook(scope, state)
                    except CalendarWindowLimit:
                        windows = state.get("calendar_windows") or [
                            [state["scan_lower"], state["scan_upper"]]
                        ]
                        lower, upper = map(datetime.fromisoformat, windows[0])
                        if (upper - lower).total_seconds() <= 3600:
                            raise ValueError(
                                "Calendar provider cap within one hour; narrow scope"
                            ) from None
                        middle = (lower + (upper - lower) / 2).isoformat()
                        state.update(
                            calendar_windows=[
                                [lower.isoformat(), middle],
                                [middle, upper.isoformat()],
                                *windows[1:],
                            ],
                            cursor=None,
                            seen_cursors=[],
                            page_completed_uris=[],
                        )
                        self.store.save_collection_state(sid, state)
                        continue
                else:
                    items, cursor = self._sharepoint(scope, state)
                for item in items:
                    self._record(item)
                state["page_completed_uris"] = []
                # Save progress only after every object in a page has been safely ingested.
                # A crash before this save repeats that page; ingestion remains idempotent.
                if cursor is not None and cursor in state["seen_cursors"]:
                    raise ValueError("Pagination cursor repeated")
                if cursor is not None:
                    state["seen_cursors"].append(cursor)
                state["cursor"] = cursor
                state["pages"] += 1
                summary["pages"] += 1
                if cursor is None:
                    windows = state.get("calendar_windows", [])
                    if windows:
                        state["calendar_windows"] = windows[1:]
                    if state.get("calendar_windows"):
                        state["seen_cursors"] = []
                    else:
                        if state["full_scan"]:
                            state["last_full_scan"] = state["scan_upper"]
                        state.update(
                            watermark=state["scan_upper"],
                            scan_upper=None,
                            state="complete",
                            error=None,
                        )
                self.store.save_collection_state(sid, state)
                if cursor is None and not state.get("calendar_windows"):
                    break
            summary["state"] = state["state"]
            summary["observed"], summary["new"] = self.observed, self.created
            summary["confirmed_revocations"] = state.get("confirmed_revocations", 0)
            summary["limitations"] = [
                "Search-index coverage; no consistent snapshot "
                "or automatic deletion/revocation feed",
                "Periodic reconciliation reduces offset/index gaps but is not a complete replica",
            ]
            if scope.source in {"outlook", "calendar", "teams"}:
                summary["filtered_metadata"] = state.get("filtered_metadata", 0)
                summary["filtered_bodies"] = state.get("filtered_bodies", 0)
                summary["confirmed_revocations"] = state.get("confirmed_revocations", 0)
                summary["limitations"].append(
                    "Communication dates are receipt/event times, not an edit feed; category "
                    "filters may exclude relevant mail; attachments are not imported"
                )
            return summary
        except Exception as exc:
            state.update(state="incomplete", error=type(exc).__name__)
            if (
                isinstance(exc, EmptyProviderOutput)
                and scope.source in {"outlook", "calendar", "teams"}
                and state["pages"] == 0
                and not state.get("page_completed_uris")
            ):
                # Retry an unconfirmed first page with a fresh upper bound next time.
                # Keep the old watermark: no empty result or completed window is inferred.
                state.update(scan_upper=None, cursor=None, seen_cursors=[], calendar_windows=[])
                state["retry_with_fresh_window"] = True
            state["last_attempt_observed"], state["last_attempt_new"] = self.observed, self.created
            self.store.save_collection_state(sid, state)
            raise

    def _outlook(self, scope, state):
        cursor = state["cursor"]
        if isinstance(cursor, str):
            arguments = {"cursor": cursor}
        else:
            offset = cursor or 0
            if type(offset) is not int or not 0 <= offset <= 1000:
                raise ValueError("Mail offset limit reached; narrow the date range")
            arguments = {
                "query": scope.query,
                "limit": state["scan_page_size"],
                "offset": offset,
                "afterDateTime": state["scan_lower"],
                "beforeDateTime": state["scan_upper"],
            }
            if scope.source == "calendar" and state.get("calendar_windows"):
                arguments["afterDateTime"], arguments["beforeDateTime"] = state["calendar_windows"][
                    0
                ]
        tool = {"calendar": CALENDAR_SEARCH, "outlook": MAIL_SEARCH, "teams": TEAMS_SEARCH}[
            scope.source
        ]
        entry = self.client.request(tool, arguments)
        blocks = entry.get("response", {}).get("content")
        if scope.source == "calendar" and isinstance(blocks, list) and blocks:
            first = blocks[0]
            if first.get("type") == "text" and CALENDAR_LIMIT_NOTICE.fullmatch(
                first.get("text", "")
            ):
                raise CalendarWindowLimit("Calendar filtering limit; split the fixed window")
        hits, next_cursor = mail_page(entry)
        offset_did_not_advance = (
            type(next_cursor) is int
            and not isinstance(cursor, str)
            and next_cursor <= (cursor or 0)
        )
        if len(hits) > state["scan_page_size"] or offset_did_not_advance:
            raise ValueError("Invalid mail page progression")
        for hit in hits:
            if communication_eligibility(hit, scope.include_terms) != "eligible":
                state["filtered_metadata"] = state.get("filtered_metadata", 0) + 1
                continue
            uri = hit.get("uri", "")
            prefix = {"calendar": "calendar:///", "outlook": "mail:///", "teams": "teams:///"}[
                scope.source
            ]
            if not isinstance(uri, str) or not uri.startswith(prefix) or not hit.get("id"):
                raise ValueError("Mail result identity/URI is not supported")
            if scope.source == "teams":
                if uri == hit.get("chatUri") or not unquote(uri).rstrip("/").endswith(
                    "/" + hit["id"]
                ):
                    raise ValueError("Teams collection must address one message, never the chat")
            if scope.source == "calendar":
                lower, upper = calendar_time(hit["start"]), calendar_time(hit["end"])
            elif scope.source == "teams":
                lower = upper = instant(hit["createdDateTime"])
            else:
                lower = upper = instant(hit["receivedDateTime"])
            if upper < instant(state["scan_lower"]) or lower > instant(state["scan_upper"]):
                raise ValueError("Mail search returned a result outside the fixed date window")
            if self.store.is_withdrawn("microsoft365", uri, scope.project):
                continue
            if uri in state.get("page_completed_uris", []):
                continue
            adapter = {
                "calendar": calendar_observation,
                "outlook": mail_observation,
                "teams": teams_observation,
            }[scope.source]
            try:
                entry = self.client.request(TOOLS["microsoft365"], {"uri": uri})
                observation = adapter(entry, hit, scope)
            except ConfirmedSourceRevocation:
                state["confirmed_revocations"] = state.get("confirmed_revocations", 0) + 1
                self.store.revoke_source("microsoft365", uri, "Provider reported message deletion")
                state.setdefault("page_completed_uris", []).append(uri)
                self.store.save_collection_state(scope_id(scope), state)
                continue
            if observation is None:
                state["filtered_bodies"] = state.get("filtered_bodies", 0) + 1
            else:
                self._record(observation)
            state.setdefault("page_completed_uris", []).append(uri)
            self.store.save_collection_state(scope_id(scope), state)
        return [], next_cursor

    def _jira(self, scope, state):
        clauses = [
            f'project = "{scope.project_key}"',
            f"updated <= {epoch_ms(state['scan_upper'])}",
        ]
        if state["scan_lower"]:
            clauses.append(f"updated >= {epoch_ms(state['scan_lower'])}")
        arguments = {
            "cloudId": scope.cloud_id,
            "jql": " AND ".join(clauses) + " ORDER BY updated ASC, key ASC",
            "fields": FIELDS,
            "maxResults": state["scan_page_size"],
            "searchResultMode": "issues",
            "responseContentFormat": "markdown",
        }
        if state["cursor"]:
            arguments["nextPageToken"] = state["cursor"]
        entry = self.client.request(JIRA_SEARCH, arguments)
        issues, cursor = jira_page(entry)
        if len(issues) > state["scan_page_size"]:
            raise ValueError("Jira returned more items than requested")
        items = []
        for issue in issues:
            key = issue.get("key", "")
            if not key.startswith(scope.project_key + "-") or not key.rsplit("-", 1)[-1].isdigit():
                raise ValueError("Jira returned an issue outside the selected project")
            updated = instant(issue.get("fields", {}).get("updated", ""))
            if updated > instant(state["scan_upper"]) or (
                state["scan_lower"] and updated < instant(state["scan_lower"])
            ):
                raise ValueError("Jira returned an issue outside the fixed date window")
            if self.store.is_withdrawn("jira", key, scope.project):
                continue
            target = Target(
                source="jira",
                source_id=key,
                cloud_id=scope.cloud_id,
                project=scope.project,
                source_url=scope.base_url + "/browse/" + key,
            )
            evidence = {
                "tool": TOOLS["jira"],
                "input": {"cloudId": scope.cloud_id, "issueIdOrKey": key},
                "response": {"content": json.dumps({"issues": {"nodes": [issue]}})},
            }
            mapped = observations_from_evidence([evidence], Settings(targets=[target]))
            for item in mapped:
                item.metadata["tool"] = JIRA_SEARCH
                item.metadata["source_tool"] = JIRA_SEARCH
            items.extend(mapped)
            with self.store.connect() as db:
                known_history = db.execute(
                    "SELECT source_updated_at,last_seen_at FROM available_observations "
                    "WHERE source='jira' AND source_id=? AND project=? "
                    "AND json_extract(metadata,'$.projection')='jira-changelog-v1' "
                    "ORDER BY observed_at DESC,storage_rowid DESC LIMIT 1",
                    (key, scope.project),
                ).fetchone()
            history_due = (scope.native_history or known_history is not None) and (
                known_history is None
                or not known_history["source_updated_at"]
                or instant(known_history["source_updated_at"]) < updated
                or (
                    state.get("full_scan")
                    and instant(known_history["last_seen_at"]) < instant(state["scan_upper"])
                )
            )
            if history_due:
                from .jira_history import normalize_history

                # Persist the normal snapshot before any extra call can exhaust budget.
                for item in items:
                    self._record(item)
                items = []
                if key in state.get("page_completed_uris", []):
                    continue
                try:
                    history = self.client.request(
                        TOOLS["jira"],
                        {
                            "cloudId": scope.cloud_id,
                            "issueIdOrKey": key,
                            "updateHistory": False,
                            "fields": ["status", "updated", "duedate"],
                            "expand": "changelog",
                        },
                    )
                except ConfirmedSourceRevocation:
                    self.store.revoke_source("jira", key, "Provider confirmed Jira access loss")
                    state["confirmed_revocations"] = state.get("confirmed_revocations", 0) + 1
                    state.setdefault("page_completed_uris", []).append(key)
                    self.store.save_collection_state(scope_id(scope), state)
                    continue
                self._record(normalize_history(history, target))
                state.setdefault("page_completed_uris", []).append(key)
                self.store.save_collection_state(scope_id(scope), state)
        return items, cursor

    def _sharepoint(self, scope, state):
        offset = state["cursor"] or 0
        if type(offset) is not int or offset > 1000:
            raise ValueError("SharePoint offset limit reached; narrow the scope before proceeding")
        arguments = {
            "query": 'path:"' + scope.folder_url + '/"',
            "limit": state["scan_page_size"],
            "offset": offset,
            "beforeDateTime": state["scan_upper"],
        }
        if state["scan_lower"]:
            arguments["afterDateTime"] = state["scan_lower"]
        entry = self.client.request(SP_SEARCH, arguments)
        hits, cursor = sharepoint_page(entry)
        if len(hits) > state["scan_page_size"] or (cursor is not None and cursor <= offset):
            raise ValueError("Invalid SharePoint page progression")
        items = []
        for hit in hits:
            url, uri = hit.get("webUrl", ""), hit.get("uri", "")
            if not within_folder(url, scope.folder_url) or not uri.startswith("file:///"):
                raise ValueError("SharePoint result is outside the selected folder")
            source_id = uri.removeprefix("file:///")
            if self.store.is_withdrawn("microsoft365", source_id, scope.project):
                continue
            previous = self.store.latest_observation("microsoft365", source_id, scope.project)
            updated = hit.get("lastModifiedDateTime")
            if (
                previous
                and updated
                and previous["source_updated_at"] == updated
                and previous["title"] == hit.get("name")
                and previous["source_url"] == url
                and (
                    not state.get("full_scan")
                    or instant(previous["last_seen_at"]) >= instant(state["scan_upper"])
                )
            ):
                self.store.touch_observation(previous["id"])
                continue
            try:
                content = self.client.request(TOOLS["microsoft365"], {"uri": uri})
            except ConfirmedSourceRevocation:
                self.store.revoke_source(
                    "microsoft365", source_id, "Provider confirmed access loss"
                )
                state["confirmed_revocations"] = state.get("confirmed_revocations", 0) + 1
                self.store.save_collection_state(scope_id(scope), state)
                continue
            target = Target(
                source="microsoft365", source_id=source_id, source_url=url, project=scope.project
            )
            observation = observations_from_evidence([content], Settings(targets=[target]))[0]
            observation.title = hit.get("name") or observation.title
            observation.source_updated_at = updated
            observation.metadata["limitations"].append(
                "Modification date comes from the search index; indexing may be delayed"
            )
            # Persist each document before another cloud read. A budget limit mid-page can
            # then resume without paying to read already acquired unchanged documents again.
            self._record(observation)
        return items, cursor
