"""Conservative policy checks on search metadata, before full-body acquisition."""

import json
import re
from datetime import datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from .models import FactInput, Observation, instant
from .source_errors import ConfirmedSourceRevocation
from .worker import text_response

EXCLUDED = re.compile(
    r"\b(?:HR|risorse umane|cedolino|busta paga|stipendio|retribuzione|ferie|malattia|"
    r"privato|personale)\b",
    re.I,
)
ADMINISTRATIVE = re.compile(r"\b(?:rimborso|nota spese|fattura|fatturazione)\b", re.I)


def communication_eligibility(hit: dict, include_terms: list[str]) -> str:
    if not include_terms or any(not term.strip() for term in include_terms):
        raise ValueError("Explicit project terms are required")
    title = hit.get("subject") or ""
    summary = hit.get("summary") or ""
    if not isinstance(title, str) or not isinstance(summary, str):
        return "unsupported_metadata"
    if hit.get("sensitivity") in {"personal", "private"}:
        return "excluded_category"
    if EXCLUDED.search(title + "\n" + summary):
        return "excluded_category"
    pattern = re.compile(
        r"(?<!\w)(?:" + "|".join(re.escape(t) for t in include_terms) + r")(?!\w)", re.I
    )
    if ADMINISTRATIVE.search(title + "\n" + summary) and not pattern.search(title):
        return "project_not_confirmed"
    # A generic title with a project name only in a preview is deliberately deferred.
    # Empty Teams titles may use the actual matched message preview instead.
    if not pattern.search(title if title else summary):
        return "project_not_confirmed"
    return "eligible"


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if not self.hidden and tag in {"br", "p", "div", "tr", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag in {"p", "div", "tr", "li"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def visible_body(data):
    body = data.get("body")
    if not isinstance(body, dict) or "content" not in body:
        raise ValueError("Full resource body unavailable; preview is not a full body")
    text = body["content"]
    if text is None:
        return None
    if not isinstance(text, str):
        raise ValueError("Unsupported resource body content")
    if body.get("contentType", "").lower() == "html":
        parser = VisibleText()
        parser.feed(text)
        parser.close()
        return "".join(parser.parts)
    if body.get("contentType", "").lower() != "text":
        raise ValueError("Unsupported body format")
    return text


def mail_observation(entry: dict, hit: dict, scope) -> Observation | None:
    if communication_eligibility(hit, scope.include_terms) != "eligible":
        raise ValueError("Mail metadata must pass policy before a body is read")
    if entry["response"].get("is_error"):
        raise ValueError("Mail body read failed")
    data = json.loads(text_response(entry["response"]["content"]))
    if not isinstance(data, dict) or data.get("id") != hit.get("id"):
        raise ValueError("Mail response identity differs from selected search hit")
    text = visible_body(data)
    if (
        communication_eligibility(
            {
                "subject": data.get("subject"),
                "summary": text or "",
                "sensitivity": data.get("sensitivity"),
            },
            scope.include_terms,
        )
        != "eligible"
    ):
        return None
    sender = data.get("sender") or {}
    sender = sender.get("name") or sender.get("emailAddress", {}).get("name")
    title = data["subject"]
    content = f"Subject: {title}\n"
    for label, value in [
        ("Sender", sender),
        ("Received", data.get("receivedDateTime")),
        ("Sent", data.get("sentDateTime")),
    ]:
        if value:
            content += f"{label}: {value}\n"
    if text is not None:
        content += f"\nBody:\n{text}"
    return Observation(
        source="microsoft365",
        source_id=hit["uri"],
        project=scope.project,
        source_url=data.get("webLink") or hit.get("webLink") or hit["uri"],
        title=title,
        content=content,
        metadata={
            "kind": "outlook_email",
            "tool": entry["tool"],
            "uri": hit["uri"],
            "normalizer": "visible-mail-v1",
            "coverage": "metadata_only" if text is None else "body_as_returned",
            "mail_headers": {
                "subject": title,
                "sender": sender,
                "received": data.get("receivedDateTime"),
                "sent": data.get("sentDateTime"),
            },
            "limitations": [
                "HTML converted to visible text; offsets refer to normalized text",
                "Attachments and structured recipient metadata omitted; body text preserved",
                "Received/sent times are not modification times or fact validity",
                *(
                    ["Body is null in connector response; cannot infer empty original mail"]
                    if text is None
                    else []
                ),
            ],
        },
    )


def mail_header_facts(observation: dict) -> list[FactInput]:
    facts = []
    for key, value in observation["metadata"].get("mail_headers", {}).items():
        if not value:
            continue
        label = {"subject": "Subject", "sender": "Sender", "received": "Received", "sent": "Sent"}[
            key
        ]
        facts.append(
            FactInput(
                subject=observation["source_id"],
                predicate="mail_" + key,
                value=value,
                quote=f"{label}: {value}",
                certainty="explicit",
            )
        )
    return facts


def calendar_time(value: dict) -> str:
    time = datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00"))
    if time.tzinfo is None:
        time = time.replace(tzinfo=ZoneInfo(value["timeZone"]))
    return instant(time.isoformat())


def calendar_observation(entry: dict, hit: dict, scope) -> Observation | None:
    if communication_eligibility(hit, scope.include_terms) != "eligible":
        raise ValueError("Calendar metadata must pass policy before body read")
    if entry["response"].get("is_error"):
        raise ValueError("Calendar body read failed")
    data = json.loads(text_response(entry["response"]["content"]))
    if not isinstance(data, dict) or data.get("id") != hit.get("id"):
        raise ValueError("Calendar response identity differs from selected event")
    text = visible_body(data)
    if (
        communication_eligibility(
            {
                "subject": data.get("subject"),
                "summary": text or "",
                "sensitivity": data.get("sensitivity"),
            },
            scope.include_terms,
        )
        != "eligible"
    ):
        return None
    # Validate without converting raw source schedule values into fact validity.
    start, end = calendar_time(data["start"]), calendar_time(data["end"])
    if end < start:
        raise ValueError("Calendar event end precedes start")
    organizer = data.get("organizer") or {}
    organizer = organizer.get("name") or organizer.get("emailAddress", {}).get("name")
    headers = {
        "subject": data["subject"],
        "organizer": organizer,
        "start": json.dumps(data["start"], sort_keys=True, ensure_ascii=False),
        "end": json.dumps(data["end"], sort_keys=True, ensure_ascii=False),
        "cancelled": str(data["isCancelled"]).lower()
        if type(data.get("isCancelled")) is bool
        else None,
    }
    labels = {
        "subject": "Subject",
        "organizer": "Organizer",
        "start": "Start",
        "end": "End",
        "cancelled": "Cancelled",
    }
    content = "\n".join(labels[k] + ": " + v for k, v in headers.items() if v is not None)
    if text is not None:
        content += "\n\nBody:\n" + text
    transcript = data.get("meetingTranscriptUrl")
    return Observation(
        source="microsoft365",
        source_id=hit["uri"],
        project=scope.project,
        source_url=data.get("webLink") or hit.get("webLink") or hit["uri"],
        title=data["subject"],
        content=content,
        source_updated_at=data.get("lastModifiedDateTime"),
        metadata={
            "kind": "outlook_calendar_event",
            "tool": entry["tool"],
            "uri": hit["uri"],
            "normalizer": "calendar-visible-v1",
            "calendar_headers": headers,
            "coverage": "metadata_only" if text is None else "body_as_returned",
            "transcript_uri": transcript
            if isinstance(transcript, str) and transcript.startswith("meeting-transcript:///")
            else None,
            "transcript_state": "not_collected",
            "limitations": [
                "Calendar scheduling and invitation body; occurrence not established",
                "Transcript reference is not transcript content or proof of availability",
                "HTML normalized; structured attendees and onlineMeeting fields omitted",
            ],
        },
    )


def calendar_header_facts(observation: dict) -> list[FactInput]:
    labels = {
        "subject": "Subject",
        "organizer": "Organizer",
        "start": "Start",
        "end": "End",
        "cancelled": "Cancelled",
    }
    return [
        FactInput(
            subject=observation["source_id"],
            predicate="calendar_" + key,
            value=value,
            quote=labels[key] + ": " + value,
            certainty="explicit",
        )
        for key, value in observation["metadata"]["calendar_headers"].items()
        if value is not None
    ]


def teams_observation(entry: dict, hit: dict, scope) -> Observation | None:
    if communication_eligibility(hit, scope.include_terms) != "eligible":
        raise ValueError("Teams metadata must pass policy before body read")
    if entry["response"].get("is_error"):
        raise ValueError("Teams body read failed")
    data = json.loads(text_response(entry["response"]["content"]))
    if (
        not isinstance(data, dict)
        or data.get("id") != hit.get("id")
        or data.get("chatId") != hit.get("chatId")
    ):
        raise ValueError("Teams response identity differs from selected message")
    if data.get("deletedDateTime"):
        instant(data["deletedDateTime"])
        raise ConfirmedSourceRevocation("Provider explicitly reports message deletedDateTime")
    text = visible_body(data)
    if (
        communication_eligibility(
            {
                "subject": data.get("subject"),
                "summary": text if text is not None else (hit.get("summary") or ""),
            },
            scope.include_terms,
        )
        != "eligible"
    ):
        return None
    sender = data.get("from") or {}
    sender = sender.get("user") or sender
    headers = {
        "sender": sender.get("displayName"),
        "created": data.get("createdDateTime"),
        "modified": data.get("lastModifiedDateTime"),
    }
    labels = {"sender": "Sender", "created": "Created", "modified": "Modified"}
    content = "\n".join(labels[k] + ": " + v for k, v in headers.items() if isinstance(v, str))
    if text is not None:
        content += "\n\nBody:\n" + text
    return Observation(
        source="microsoft365",
        source_id=hit["uri"],
        project=scope.project,
        source_url=data.get("webUrl") or hit.get("webUrl") or hit["uri"],
        title=data.get("subject") or "Teams message " + data["id"],
        content=content,
        source_updated_at=data.get("lastModifiedDateTime"),
        metadata={
            "kind": "teams_message",
            "tool": entry["tool"],
            "uri": hit["uri"],
            "chat_id": data["chatId"],
            "normalizer": "teams-visible-v1",
            "coverage": "metadata_only" if text is None else "body_as_returned",
            "teams_headers": headers,
            "actor_ref": {
                "provider": "microsoft365",
                "id": sender.get("id"),
                "label": sender.get("displayName"),
            },
            "limitations": [
                "Single matched message only; not the entire chat",
                "HTML normalized; attachments, reactions and unrelated context omitted",
                "Sent/modified dates are not automatically fact validity",
            ],
        },
    )


def teams_header_facts(observation: dict) -> list[FactInput]:
    labels = {"sender": "Sender", "created": "Created", "modified": "Modified"}
    return [
        FactInput(
            subject=observation["source_id"],
            predicate="teams_" + key,
            value=value,
            quote=labels[key] + ": " + value,
            certainty="explicit",
        )
        for key, value in observation["metadata"]["teams_headers"].items()
        if isinstance(value, str)
    ]
