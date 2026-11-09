from datetime import UTC, datetime
from typing import Literal
from urllib.parse import parse_qs, quote, unquote, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def instant(value: str) -> str:
    """Canonical UTC instant; a plain date means start of that UTC day."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        if len(value) != 10:
            raise ValueError("Datetime requires a timezone")
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Observation(Model):
    source: Literal["microsoft365", "jira", "local", "fixture"]
    source_id: str = Field(min_length=1, max_length=2048)
    project: str = Field(min_length=1, max_length=200)
    source_url: str
    title: str
    content: str = Field(min_length=1, max_length=2_000_000)
    source_updated_at: str | None = None
    metadata: dict = Field(default_factory=dict)


class FactInput(Model):
    subject: str = Field(min_length=1, max_length=300)
    predicate: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=2000)
    kind: Literal["person", "project", "decision", "activity", "fact"] = "fact"
    certainty: Literal["explicit", "proposed", "inferred", "uncertain"] = "uncertain"
    valid_from: str | None = None
    valid_to: str | None = None
    quote: str = Field(min_length=1, max_length=5000)
    supersedes: str | None = None

    @model_validator(mode="after")
    def dates(self):
        if self.valid_from is not None:
            self.valid_from = instant(self.valid_from)
        if self.valid_to is not None:
            self.valid_to = instant(self.valid_to)
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")
        if self.supersedes and not self.valid_from:
            raise ValueError("An explicit replacement needs a known valid_from")
        return self


class Extraction(Model):
    facts: list[FactInput] = Field(default_factory=list, max_length=60)


class Target(Model):
    source: Literal["microsoft365", "jira"]
    source_id: str
    source_url: str
    project: str
    cloud_id: str | None = None


def folder_url(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or not parts.hostname.endswith(".sharepoint.com")
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
    ):
        raise ValueError("Expected an HTTPS SharePoint folder URL")
    selected = parse_qs(parts.query).get("id")
    path = selected[0] if selected else unquote(parts.path)
    if not path.startswith("/sites/") or any(p in {"..", "."} for p in path.split("/")):
        raise ValueError("Expected a canonical SharePoint site folder path")
    if "/Forms/" in path or "/:f:/" in path or '"' in path:
        raise ValueError("Folder link needs its selected id parameter or canonical folder path")
    return urlunsplit(("https", parts.hostname, quote(path.rstrip("/"), safe="/"), "", ""))


class CollectionScope(Model):
    source: Literal["jira", "sharepoint"]
    project: str = Field(min_length=1, max_length=200)
    project_key: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    cloud_id: str | None = None
    native_history: bool = False
    base_url: str | None = None
    folder_url: str | None = None
    since: str | None = None
    page_size: int = Field(default=10, ge=1, le=50)
    max_pages_per_sync: int = Field(default=2, ge=1, le=20)
    overlap_seconds: int = Field(default=86400, ge=60, le=604800)
    reconcile_seconds: int = Field(default=604800, ge=86400, le=2592000)

    @model_validator(mode="after")
    def scope(self):
        if self.source == "jira":
            if not self.project_key or not self.cloud_id or not self.base_url:
                raise ValueError("Jira scope requires project_key, cloud_id and base_url")
            parts = urlsplit(self.base_url)
            if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
                raise ValueError("Expected an HTTPS Jira base URL")
            self.base_url = urlunsplit(("https", parts.netloc, "", "", ""))
        else:
            if not self.folder_url:
                raise ValueError("SharePoint scope requires folder_url")
            self.folder_url = folder_url(self.folder_url)
        if self.since:
            self.since = instant(self.since)
        return self


class LocalScope(Model):
    root: str = Field(min_length=1, max_length=4096)
    project: str = Field(min_length=1, max_length=200)
    include_globs: list[str] = Field(
        default_factory=lambda: ["*.txt", "*.md", "*.pdf", "*.docx"], max_length=30
    )
    exclude_globs: list[str] = Field(default_factory=lambda: ["~$*", "**/~$*"], max_length=30)
    max_files_per_sync: int = Field(default=50, ge=1, le=1000)
    reconcile_seconds: int = Field(default=604800, ge=60, le=2592000)


class MailScope(Model):
    source: Literal["outlook"] = "outlook"
    project: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1, max_length=1000)
    include_terms: list[str] = Field(min_length=1, max_length=20)
    since: str | None = None
    page_size: int = Field(default=5, ge=1, le=25)
    max_pages_per_sync: int = Field(default=1, ge=1, le=20)
    overlap_seconds: int = Field(default=86400, ge=60, le=604800)
    reconcile_seconds: int = Field(default=604800, ge=86400, le=2592000)

    @model_validator(mode="after")
    def scoped_query(self):
        if any(not term.strip() or len(term) > 100 for term in self.include_terms):
            raise ValueError("Explicit, bounded project terms are required")
        if self.query.casefold() not in {term.casefold() for term in self.include_terms}:
            raise ValueError("Mail query must be one explicit project term; use separate scopes")
        if self.since:
            self.since = instant(self.since)
        return self


class CalendarScope(MailScope):
    source: Literal["calendar"] = "calendar"


class TeamsScope(MailScope):
    source: Literal["teams"] = "teams"


class Settings(Model):
    targets: list[Target] = Field(default_factory=list, max_length=30)
    local_paths: list[str] = Field(default_factory=list, max_length=30)
    model: Literal["haiku", "sonnet"] = "haiku"
    local_model: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,100}$")
    max_local_sections_per_sync: int = Field(default=2, ge=1, le=20)
    jira_semantics: bool = False
    interval_seconds: int = Field(default=900, ge=60)
    timeout_seconds: int = Field(default=240, ge=10, le=900)
    scopes: list[CollectionScope] = Field(default_factory=list, max_length=20)
    mail_scopes: list[MailScope] = Field(default_factory=list, max_length=20)
    local_scopes: list[LocalScope] = Field(default_factory=list, max_length=20)
    calendar_scopes: list[CalendarScope] = Field(default_factory=list, max_length=20)
    teams_scopes: list[TeamsScope] = Field(default_factory=list, max_length=20)
    max_cloud_calls_per_sync: int = Field(default=6, ge=1, le=100)
