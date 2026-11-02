"""领域模型与时间/文本工具。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import re

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return ensure_utc(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def from_iso(value: str | None) -> datetime | None:
    if value is None or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return ensure_utc(parsed)


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def content_hash(title: str, abstract: str) -> str:
    payload = f"{normalize_text(title)}\n{normalize_text(abstract)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Paper:
    arxiv_id: str
    title: str
    abstract: str
    first_submitted_at: datetime
    updated_at: datetime | None
    arxiv_url: str
    content_hash: str


@dataclass(frozen=True)
class PaperView:
    """SQLite 查询后直接用于页面的行。"""

    arxiv_id: str
    title: str
    abstract: str
    first_submitted_at: datetime
    arxiv_url: str
    rank: int
    contribution_zh: str | None
    dataset_or_method_zh: str | None
    code_open_source: str | None
    code_evidence_en: str | None
    extraction_status: str
    extraction_error: str | None


@dataclass(frozen=True)
class ExtractionRecord:
    id: int
    arxiv_id: str
    content_hash: str
    status: str
    contribution_zh: str | None
    dataset_or_method_zh: str | None
    code_open_source: str | None
    code_evidence_en: str | None
    error_code: str | None
    error_message: str | None
    attempts: int
    model: str | None
    prompt_version: str | None
    raw_response: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class RunRecord:
    id: int
    trigger: str
    requested_days: int
    window_start_utc: datetime
    window_end_utc: datetime
    status: str
    result_count: int
    extraction_failures: int
    truncated: bool
    error_code: str | None
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class Decision:
    include: bool
    reason: str
    matched_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class FetchResult:
    papers: list[Paper]
    total_results: int | None
    truncated: bool
    attempts: int
    pages_fetched: int


@dataclass(frozen=True)
class ExtractionFields:
    contribution_zh: str
    dataset_or_method_zh: str
    code_open_source: str
    code_evidence_en: str


@dataclass(frozen=True)
class ExtractionResult:
    status: str
    fields: ExtractionFields | None = None
    error_code: str | None = None
    error_message: str | None = None
    raw_response: str = ""
    attempts: int = 0
    model: str = ""
    prompt_version: str = "extract_v1"


@dataclass(frozen=True)
class RefreshResult:
    run_id: int | None
    status: str
    result_count: int = 0
    extraction_failures: int = 0
    busy: bool = False
    error_code: str | None = None
    error_message: str | None = None
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.status in {"success", "empty"}
