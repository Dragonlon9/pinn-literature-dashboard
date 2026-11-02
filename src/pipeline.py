"""刷新编排：锁、run 生命周期、缓存复用、逐篇提取与状态汇总。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import re
import sqlite3
import threading
from typing import Callable

from .arxiv_client import ArxivClient, ArxivError
from .config import Settings
from .extractor import Extractor
from .locking import InterProcessLock
from .models import (
    Decision,
    ExtractionResult,
    Paper,
    RefreshResult,
    ensure_utc,
    utc_now,
)
from .relevance import decide as default_decide
from .repository import Repository

logger = logging.getLogger(__name__)

_STOP_BATCH_CODES = {"missing_api_key", "auth_error", "insufficient_balance"}


def _redact(text: str | None, api_key: str | None) -> str:
    value = text or ""
    if api_key:
        value = value.replace(api_key, "[REDACTED]")
    value = re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "[REDACTED]", value)
    return value[:1000]


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        arxiv_client: ArxivClient,
        extractor: Extractor,
        *,
        clock: Callable[[], datetime] = utc_now,
        relevance: Callable[[Paper], Decision] = default_decide,
    ):
        self.settings = settings
        self.repo = repository
        self.arxiv = arxiv_client
        self.extractor = extractor
        self._clock = clock
        self._relevance = relevance
        self._thread_lock = threading.Lock()
        self._file_lock = InterProcessLock(settings.lock_path)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return ensure_utc(value).replace(microsecond=0)

    @staticmethod
    def _busy() -> RefreshResult:
        return RefreshResult(
            run_id=None,
            status="busy",
            busy=True,
            error_code="busy",
            error_message="已有更新任务正在运行；本次未请求 arXiv/DeepSeek。",
        )

    def refresh(self, days: int, trigger: str = "manual") -> RefreshResult:
        if (
            isinstance(days, bool)
            or not isinstance(days, int)
            or days < 1
            or days > 30
        ):
            return RefreshResult(
                run_id=None,
                status="invalid_days",
                error_code="invalid_days",
                error_message="“最近 X 天”必须是 1–30 的整数；未请求 arXiv。",
            )
        if not self._thread_lock.acquire(blocking=False):
            return self._busy()
        try:
            try:
                if not self._file_lock.acquire():
                    return self._busy()
            except OSError as exc:
                return RefreshResult(
                    run_id=None,
                    status="failed",
                    error_code="lock_error",
                    error_message=_redact(
                        f"无法创建刷新锁：{exc}", self.settings.deepseek_api_key
                    ),
                )
            try:
                return self._refresh_locked(days, trigger)
            finally:
                self._file_lock.release()
        finally:
            self._thread_lock.release()

    def _refresh_locked(self, days: int, trigger: str) -> RefreshResult:
        now = self._now()
        window_start = now - timedelta(days=days)
        try:
            run_id = self.repo.create_run(
                trigger, days, window_start, now, started_at=now
            )
        except sqlite3.Error as exc:
            logger.exception("create_run failed")
            return RefreshResult(
                run_id=None,
                status="failed",
                error_code="db_error",
                error_message=_redact(
                    f"本地数据库写入失败：{exc}", self.settings.deepseek_api_key
                ),
            )

        try:
            fetch = self.arxiv.fetch(window_start, now)
        except ArxivError as exc:
            message = _redact(exc.message, self.settings.deepseek_api_key)
            self._fail_run(run_id, exc.code, message, now)
            logger.error("arxiv fetch failed run=%s code=%s", run_id, exc.code)
            return RefreshResult(
                run_id=run_id,
                status="failed",
                error_code=exc.code,
                error_message=message,
            )
        except Exception as exc:  # 不让未预期异常把页面打崩
            message = _redact(
                f"arXiv 检索内部异常：{type(exc).__name__}",
                self.settings.deepseek_api_key,
            )
            self._fail_run(run_id, "arxiv_internal_error", message, now)
            logger.exception("unexpected arxiv failure run=%s", run_id)
            return RefreshResult(
                run_id=run_id,
                status="failed",
                error_code="arxiv_internal_error",
                error_message=message,
            )

        logger.info(
            "run=%s trigger=%s fetched=%s total=%s truncated=%s",
            run_id,
            trigger,
            len(fetch.papers),
            fetch.total_results,
            fetch.truncated,
        )

        try:
            included = self._select_papers(run_id, fetch.papers, window_start, now)
        except sqlite3.Error as exc:
            message = _redact(
                f"本地数据库写入失败：{exc}", self.settings.deepseek_api_key
            )
            self._fail_run(run_id, "db_error", message, now)
            logger.exception("exclusion write failed run=%s", run_id)
            return RefreshResult(
                run_id=run_id,
                status="failed",
                error_code="db_error",
                error_message=message,
            )

        if not included:
            try:
                self.repo.finalize_run(
                    run_id,
                    "empty",
                    0,
                    0,
                    fetch.truncated,
                    finished_at=now,
                )
            except sqlite3.Error as exc:
                return self._db_failure(run_id, exc, now)
            logger.info("run=%s empty", run_id)
            return RefreshResult(
                run_id=run_id,
                status="empty",
                result_count=0,
                truncated=fetch.truncated,
            )

        try:
            failures, stop_error = self._extract_included(
                run_id, included, now
            )
            status = "success" if failures == 0 else "partial"
            self.repo.finalize_run(
                run_id,
                status,
                len(included),
                failures,
                fetch.truncated,
                finished_at=now,
            )
        except sqlite3.Error as exc:
            return self._db_failure(run_id, exc, now)

        logger.info(
            "run=%s status=%s included=%s failures=%s cache/stop=%s",
            run_id,
            status,
            len(included),
            failures,
            "stop" if stop_error else "none",
        )
        return RefreshResult(
            run_id=run_id,
            status=status,
            result_count=len(included),
            extraction_failures=failures,
            truncated=fetch.truncated,
        )

    def _select_papers(
        self,
        run_id: int,
        papers: list[Paper],
        window_start: datetime,
        window_end: datetime,
    ) -> list[Paper]:
        included: list[Paper] = []
        seen: set[str] = set()
        for paper in papers:
            if paper.arxiv_id in seen:
                self.repo.add_exclusion(
                    run_id,
                    paper.arxiv_id,
                    paper.title,
                    "duplicate",
                    ("canonical_id",),
                )
                continue
            seen.add(paper.arxiv_id)

            if not (window_start <= paper.first_submitted_at <= window_end):
                self.repo.add_exclusion(
                    run_id,
                    paper.arxiv_id,
                    paper.title,
                    "outside_window",
                )
                continue

            decision = self._relevance(paper)
            if not decision.include:
                self.repo.add_exclusion(
                    run_id,
                    paper.arxiv_id,
                    paper.title,
                    decision.reason,
                    decision.matched_terms,
                )
                continue
            included.append(paper)
        return included

    def _extract_included(
        self, run_id: int, included: list[Paper], now: datetime
    ) -> tuple[int, ExtractionResult | None]:
        failures = 0
        stop_error: ExtractionResult | None = None

        for rank, paper in enumerate(included):
            self.repo.upsert_paper(paper, now)
            existing = self.repo.get_extraction(paper.arxiv_id, paper.content_hash)
            if existing is not None and existing.status == "success":
                self.repo.add_run_paper(run_id, paper.arxiv_id, rank)
                continue

            self.repo.ensure_pending(
                paper,
                self.settings.deepseek_model,
                "extract_v1",
                now,
            )

            if stop_error is not None:
                self.repo.save_extraction_failure(
                    paper,
                    stop_error.error_code or "unknown",
                    stop_error.error_message or "",
                    0,
                    self.settings.deepseek_model,
                    "extract_v1",
                    updated_at=now,
                )
                self.repo.add_run_paper(run_id, paper.arxiv_id, rank)
                failures += 1
                continue

            result = self._safe_extract(paper)
            if result.status == "success" and result.fields is not None:
                self.repo.save_extraction_success(
                    paper,
                    result.fields,
                    result.attempts,
                    result.model,
                    result.prompt_version,
                    result.raw_response,
                    updated_at=now,
                )
            else:
                self.repo.save_extraction_failure(
                    paper,
                    result.error_code or "unknown",
                    result.error_message or "",
                    result.attempts,
                    result.model,
                    result.prompt_version,
                    result.raw_response,
                    updated_at=now,
                )
                failures += 1
                if result.error_code in _STOP_BATCH_CODES:
                    stop_error = result
            self.repo.add_run_paper(run_id, paper.arxiv_id, rank)
        return failures, stop_error

    def _safe_extract(self, paper: Paper) -> ExtractionResult:
        try:
            return self.extractor.extract(paper)
        except Exception as exc:
            logger.exception("extractor crashed for %s", paper.arxiv_id)
            return ExtractionResult(
                status="failed",
                error_code="internal_error",
                error_message=_redact(
                    f"抽取器内部异常：{type(exc).__name__}",
                    self.settings.deepseek_api_key,
                ),
                attempts=0,
                model=self.settings.deepseek_model,
                prompt_version="extract_v1",
            )

    def _fail_run(
        self, run_id: int, code: str, message: str, now: datetime
    ) -> None:
        try:
            self.repo.finalize_run(
                run_id,
                "failed",
                0,
                0,
                False,
                error_code=code,
                error_message=message,
                finished_at=now,
            )
        except sqlite3.Error:
            logger.exception("could not mark run=%s failed", run_id)

    def _db_failure(
        self, run_id: int, exc: sqlite3.Error, now: datetime
    ) -> RefreshResult:
        message = _redact(
            f"本地数据库写入失败：{exc}", self.settings.deepseek_api_key
        )
        self._fail_run(run_id, "db_error", message, now)
        logger.exception("db failure run=%s", run_id)
        return RefreshResult(
            run_id=run_id,
            status="failed",
            error_code="db_error",
            error_message=message,
        )

    def retry_failed(self, run_id: int | None = None) -> RefreshResult:
        """重试当前 run 中已失败的单篇提取；不重新请求 arXiv。"""
        if not self._thread_lock.acquire(blocking=False):
            return self._busy()
        try:
            if not self._file_lock.acquire():
                return self._busy()
            try:
                return self._retry_failed_locked(run_id)
            finally:
                self._file_lock.release()
        finally:
            self._thread_lock.release()

    def _retry_failed_locked(self, run_id: int | None) -> RefreshResult:
        target_id = run_id or self.repo.current_run_id()
        run = self.repo.get_run(target_id)
        if run is None:
            return RefreshResult(
                run_id=None,
                status="failed",
                error_code="no_current_run",
                error_message="当前没有可重试的批次。",
            )
        failed = self.repo.failed_extractions_for_run(run.id)
        if not failed:
            return RefreshResult(
                run_id=run.id,
                status=run.status,
                result_count=run.result_count,
                extraction_failures=run.extraction_failures,
                truncated=run.truncated,
            )

        now = self._now()
        stop_error: ExtractionResult | None = None
        try:
            for paper, _record in failed:
                self.repo.ensure_pending(
                    paper,
                    self.settings.deepseek_model,
                    "extract_v1",
                    now,
                )
                if stop_error is not None:
                    self.repo.save_extraction_failure(
                        paper,
                        stop_error.error_code or "unknown",
                        stop_error.error_message or "",
                        0,
                        self.settings.deepseek_model,
                        "extract_v1",
                        updated_at=now,
                    )
                    continue
                result = self._safe_extract(paper)
                if result.status == "success" and result.fields is not None:
                    self.repo.save_extraction_success(
                        paper,
                        result.fields,
                        result.attempts,
                        result.model,
                        result.prompt_version,
                        result.raw_response,
                        updated_at=now,
                    )
                else:
                    self.repo.save_extraction_failure(
                        paper,
                        result.error_code or "unknown",
                        result.error_message or "",
                        result.attempts,
                        result.model,
                        result.prompt_version,
                        result.raw_response,
                        updated_at=now,
                    )
                    if result.error_code in _STOP_BATCH_CODES:
                        stop_error = result
            total, failures = self.repo.run_counts(run.id)
            status = "success" if failures == 0 else "partial"
            self.repo.finalize_run(
                run.id,
                status,
                total,
                failures,
                run.truncated,
                finished_at=now,
                update_last_retrieval=False,
            )
        except sqlite3.Error as exc:
            message = _redact(
                f"本地数据库写入失败：{exc}", self.settings.deepseek_api_key
            )
            logger.exception("retry db failure run=%s", run.id)
            return RefreshResult(
                run_id=run.id,
                status="failed",
                error_code="db_error",
                error_message=message,
            )

        logger.info(
            "retry run=%s status=%s failures=%s",
            run.id,
            status,
            failures,
        )
        return RefreshResult(
            run_id=run.id,
            status=status,
            result_count=total,
            extraction_failures=failures,
            truncated=run.truncated,
        )
