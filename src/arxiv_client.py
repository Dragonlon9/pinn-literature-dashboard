"""arXiv API 检索、Atom 解析、canonical ID 与有限重试。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import time
from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import requests

from .config import Settings
from .models import (
    FetchResult,
    Paper,
    content_hash,
    ensure_utc,
    normalize_text,
)

ATOM_NS = "http://www.w3.org/2005/Atom"
OPENSEARCH_NS = "http://a9.com/-/spec/opensearch/1.1/"
ARXIV_NS = "http://arxiv.org/schemas/atom"

NS = {"atom": ATOM_NS, "opensearch": OPENSEARCH_NS, "arxiv": ARXIV_NS}


class ArxivError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after

    def __str__(self) -> str:
        return self.message


# 与设计文档 §5.2 一致的“召回声”；最终纳入由 relevance.py 决定。
_QUERY_TEMPLATE = """(
  abs:"physics-informed neural network" OR abs:"physics-informed neural networks" OR
  abs:"PINN" OR abs:"PINNs" OR abs:"physics-informed deep learning" OR
  abs:"physics-constrained neural network" OR abs:"physics-informed machine learning" OR
  ti:"PINN" OR ti:"PINNs"
)
AND
(
  abs:"partial differential equation" OR abs:"partial differential equations" OR
  abs:"PDE" OR abs:"PDEs" OR abs:"Navier-Stokes" OR abs:"Burgers" OR
  abs:"heat equation" OR abs:"wave equation" OR abs:"inverse problem" OR
  abs:"forward problem"
)
AND submittedDate:[{start_utc} TO {end_utc}]"""

_NEW_ID_RE = re.compile(r"\d{4}\.\d{4,5}")
_OLD_ID_RE = re.compile(r"[a-z][a-z\-]*(?:\.[a-z\-]+)?/\d{7}")


def canonical_arxiv_id(raw: str) -> str | None:
    """把 Atom id / abs URL 归一为不带版本号的小写 arXiv ID。"""
    if not raw:
        return None
    value = raw.strip()
    if value.lower().startswith("arxiv:"):
        value = value[6:].strip()
    value = value.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    lowered = value.lower()
    for prefix in (
        "https://arxiv.org/abs/",
        "http://arxiv.org/abs/",
        "https://export.arxiv.org/abs/",
        "http://export.arxiv.org/abs/",
        "https://www.arxiv.org/abs/",
        "http://www.arxiv.org/abs/",
        "arxiv.org/abs/",
    ):
        if lowered.startswith(prefix):
            value = value[len(prefix) :]
            lowered = value.lower()
            break
    value = re.sub(r"v\d+$", "", value, flags=re.IGNORECASE).lower()
    if _NEW_ID_RE.fullmatch(value):
        return value
    if _OLD_ID_RE.fullmatch(value):
        return value
    return None


def arxiv_abs_url(arxiv_id: str) -> str:
    return f"https://arxiv.org/abs/{arxiv_id}"


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return ensure_utc(parsed)


def _element_text(parent: ET.Element, tag: str) -> str | None:
    child = parent.find(tag, NS)
    if child is None or child.text is None:
        return None
    return child.text


def _retry_after_seconds(response: requests.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return min(30.0, max(0.0, float(raw.strip())))
    except ValueError:
        return None


class ArxivClient:
    def __init__(
        self,
        settings: Settings,
        session: requests.Session | None = None,
        sleep=time.sleep,
    ):
        self.settings = settings
        self.session = session or requests.Session()
        self._sleep = sleep

    def build_query(self, window_start: datetime, window_end: datetime) -> str:
        start = ensure_utc(window_start).strftime("%Y%m%d%H%M")
        end = ensure_utc(window_end).strftime("%Y%m%d%H%M")
        return _QUERY_TEMPLATE.format(start_utc=start, end_utc=end)

    def fetch(
        self, window_start: datetime, window_end: datetime
    ) -> FetchResult:
        query = self.build_query(window_start, window_end)
        papers: list[Paper] = []
        total_results: int | None = None
        attempts = 0
        pages_fetched = 0
        truncated = False

        for page_index in range(self.settings.arxiv_max_pages):
            if page_index > 0 and self.settings.arxiv_page_delay_seconds > 0:
                self._sleep(self.settings.arxiv_page_delay_seconds)
            page_papers, page_total, page_attempts = self._fetch_page(
                query, page_index
            )
            attempts += page_attempts
            pages_fetched += 1
            if total_results is None:
                total_results = page_total
            if not page_papers:
                break
            papers.extend(page_papers)

            if len(papers) >= self.settings.arxiv_hard_cap:
                papers = papers[: self.settings.arxiv_hard_cap]
                if total_results is None or total_results > len(papers):
                    truncated = True
                break
            if total_results is not None and len(papers) >= total_results:
                break
        else:
            # 达到最大页数仍未取完。
            if total_results is None or total_results > len(papers):
                truncated = True

        if total_results is None and not truncated:
            # 无法确认总数时，只有在空页结束才敢说“取完了”。
            if pages_fetched >= self.settings.arxiv_max_pages:
                truncated = True

        return FetchResult(
            papers=papers,
            total_results=total_results,
            truncated=truncated,
            attempts=attempts,
            pages_fetched=pages_fetched,
        )

    def _fetch_page(
        self, query: str, page_index: int
    ) -> tuple[list[Paper], int | None, int]:
        params: dict[str, Any] = {
            "search_query": query,
            "start": page_index * self.settings.arxiv_page_size,
            "max_results": self.settings.arxiv_page_size,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        headers = {
            "User-Agent": self.settings.user_agent,
            "Accept": "application/atom+xml",
        }
        last_error: ArxivError | None = None
        for attempt in range(1, self.settings.arxiv_max_attempts + 1):
            try:
                response = self.session.get(
                    self.settings.arxiv_api_url,
                    params=params,
                    headers=headers,
                    timeout=(
                        self.settings.connect_timeout_seconds,
                        self.settings.read_timeout_seconds,
                    ),
                )
            except requests.Timeout:
                last_error = ArxivError(
                    "arxiv_timeout", "arXiv 请求超时", retryable=True
                )
            except requests.ConnectionError:
                last_error = ArxivError(
                    "arxiv_connection_error", "arXiv 连接失败", retryable=True
                )
            except requests.RequestException as exc:
                last_error = ArxivError(
                    "arxiv_request_error",
                    f"arXiv 请求异常：{type(exc).__name__}",
                    retryable=True,
                )
            else:
                if response.status_code == 200:
                    if not response.content:
                        last_error = ArxivError(
                            "arxiv_empty_response",
                            "arXiv 返回了空响应",
                            retryable=True,
                        )
                    else:
                        try:
                            papers, total = self._parse_feed(response.content)
                        except ArxivError as exc:
                            last_error = exc
                            if not exc.retryable:
                                raise
                        else:
                            return papers, total, attempt
                elif response.status_code == 429:
                    last_error = ArxivError(
                        "arxiv_rate_limited",
                        "arXiv 返回 429（请求过多）",
                        retryable=True,
                        retry_after=_retry_after_seconds(response),
                    )
                elif response.status_code in {500, 502, 503, 504}:
                    last_error = ArxivError(
                        "arxiv_server_error",
                        f"arXiv 返回 HTTP {response.status_code}",
                        retryable=True,
                    )
                elif response.status_code in {400, 403, 404}:
                    raise ArxivError(
                        "arxiv_request_rejected",
                        f"arXiv 拒绝了请求（HTTP {response.status_code}）",
                        retryable=False,
                    )
                else:
                    raise ArxivError(
                        "arxiv_http_error",
                        f"arXiv 返回 HTTP {response.status_code}",
                        retryable=response.status_code >= 500,
                    )

            if attempt >= self.settings.arxiv_max_attempts or not (
                last_error and last_error.retryable
            ):
                break
            delay = (
                last_error.retry_after
                if last_error and last_error.retry_after is not None
                else float(2 ** attempt)
            )
            self._sleep(delay)

        if last_error is None:
            last_error = ArxivError(
                "arxiv_request_error", "arXiv 请求失败", retryable=False
            )
        raise last_error

    def _parse_feed(self, content: bytes) -> tuple[list[Paper], int | None]:
        try:
            root = ET.fromstring(content)
        except ET.ParseError as exc:
            raise ArxivError(
                "arxiv_parse_error",
                f"arXiv XML 解析失败：{exc}",
                retryable=True,
            ) from exc

        entries = root.findall("atom:entry", NS)
        for entry in entries:
            entry_id = _element_text(entry, "atom:id") or ""
            title = normalize_text(_element_text(entry, "atom:title"))
            if "/api/errors#" in entry_id or title.lower() == "error":
                summary = normalize_text(_element_text(entry, "atom:summary"))
                detail = summary[:300] if summary else "arXiv 返回错误 Atom"
                raise ArxivError(
                    "arxiv_error_feed",
                    f"arXiv 查询被拒绝：{detail}",
                    retryable=False,
                )

        total: int | None = None
        total_text = root.findtext("opensearch:totalResults", default=None, namespaces=NS)
        if total_text is not None and total_text.strip():
            try:
                total = int(total_text.strip())
            except ValueError:
                total = None

        papers: list[Paper] = []
        for entry in entries:
            raw_id = _element_text(entry, "atom:id")
            arxiv_id = canonical_arxiv_id(raw_id or "")
            if arxiv_id is None:
                raise ArxivError(
                    "arxiv_missing_field",
                    "arXiv entry 缺少可识别的 id",
                    retryable=False,
                )
            title = normalize_text(_element_text(entry, "atom:title"))
            abstract = normalize_text(_element_text(entry, "atom:summary"))
            published = _parse_datetime(_element_text(entry, "atom:published"))
            updated = _parse_datetime(_element_text(entry, "atom:updated"))
            if not title or not abstract or published is None:
                raise ArxivError(
                    "arxiv_missing_field",
                    f"arXiv entry {arxiv_id} 缺少标题、摘要或 published",
                    retryable=False,
                )
            papers.append(
                Paper(
                    arxiv_id=arxiv_id,
                    title=title,
                    abstract=abstract,
                    first_submitted_at=published,
                    updated_at=updated,
                    arxiv_url=arxiv_abs_url(arxiv_id),
                    content_hash=content_hash(title, abstract),
                )
            )
        return papers, total
