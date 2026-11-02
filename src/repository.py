"""领域查询与写入。不调用任何外部 API。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
from typing import Any, Iterable

from .db import Database
from .models import (
    ExtractionFields,
    ExtractionRecord,
    Paper,
    PaperView,
    RunRecord,
    from_iso,
    to_iso,
    utc_now,
)

STALE_RUN_MINUTES = 30
MAX_ERROR_MESSAGE = 1000
MAX_RAW_RESPONSE = 8192


def _truncate(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value[:limit]


class Repository:
    def __init__(self, database: Database | Path | str):
        if isinstance(database, Database):
            self.db = database
        else:
            self.db = Database(database)

    def init(self) -> None:
        self.db.init_schema()

    # ---------------------------------------------------------------- meta
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT value FROM app_meta WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row is not None else default

    def set_meta(self, key: str, value: str | None) -> None:
        now = to_iso(utc_now())
        with self.db.transaction() as connection:
            self._set_meta(connection, key, value, now)

    @staticmethod
    def _set_meta(
        connection: sqlite3.Connection, key: str, value: str | None, now_iso: str
    ) -> None:
        connection.execute(
            """
            INSERT INTO app_meta(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, now_iso),
        )

    def current_run_id(self) -> int | None:
        value = self.get_meta("current_run_id")
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def last_update_success_at(self) -> str | None:
        return self.get_meta("last_update_success_at")

    def last_retrieval_success_at(self) -> str | None:
        return self.get_meta("last_retrieval_success_at")

    # ---------------------------------------------------------------- runs
    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> RunRecord:
        started = from_iso(row["started_at"])
        finished = from_iso(row["finished_at"])
        window_start = from_iso(row["window_start_utc"])
        window_end = from_iso(row["window_end_utc"])
        assert started is not None
        assert window_start is not None
        assert window_end is not None
        return RunRecord(
            id=int(row["id"]),
            trigger=row["trigger"],
            requested_days=int(row["requested_days"]),
            window_start_utc=window_start,
            window_end_utc=window_end,
            status=row["status"],
            result_count=int(row["result_count"]),
            extraction_failures=int(row["extraction_failures"]),
            truncated=bool(row["truncated"]),
            error_code=row["error_code"],
            error_message=row["error_message"],
            started_at=started,
            finished_at=finished,
        )

    def create_run(
        self,
        trigger: str,
        requested_days: int,
        window_start: datetime,
        window_end: datetime,
        started_at: datetime | None = None,
    ) -> int:
        now = started_at or utc_now()
        with self.db.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO runs(
                    trigger, requested_days, window_start_utc, window_end_utc,
                    status, result_count, extraction_failures, truncated,
                    started_at
                ) VALUES (?, ?, ?, ?, 'running', 0, 0, 0, ?)
                """,
                (
                    trigger,
                    requested_days,
                    to_iso(window_start),
                    to_iso(window_end),
                    to_iso(now),
                ),
            )
            return int(cursor.lastrowid)

    def finalize_run(
        self,
        run_id: int,
        status: str,
        result_count: int,
        extraction_failures: int,
        truncated: bool,
        error_code: str | None = None,
        error_message: str | None = None,
        finished_at: datetime | None = None,
        update_last_retrieval: bool = True,
    ) -> None:
        now = finished_at or utc_now()
        now_iso = to_iso(now)
        assert now_iso is not None
        with self.db.transaction() as connection:
            connection.execute(
                """
                UPDATE runs SET
                    status = ?,
                    result_count = ?,
                    extraction_failures = ?,
                    truncated = ?,
                    error_code = ?,
                    error_message = ?,
                    finished_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    int(result_count),
                    int(extraction_failures),
                    1 if truncated else 0,
                    error_code,
                    _truncate(error_message, MAX_ERROR_MESSAGE),
                    now_iso,
                    run_id,
                ),
            )
            if status in {"success", "empty", "partial"}:
                self._set_meta(connection, "current_run_id", str(run_id), now_iso)
            if status in {"success", "empty"}:
                self._set_meta(
                    connection, "last_update_success_at", now_iso, now_iso
                )
            if update_last_retrieval and status in {
                "success",
                "empty",
                "partial",
            }:
                self._set_meta(
                    connection, "last_retrieval_success_at", now_iso, now_iso
                )

    def get_run(self, run_id: int | None) -> RunRecord | None:
        if run_id is None:
            return None
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        return self._run_from_row(row) if row is not None else None

    def latest_run(self) -> RunRecord | None:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return self._run_from_row(row) if row is not None else None

    def current_run(self) -> RunRecord | None:
        return self.get_run(self.current_run_id())

    def recover_stale_runs(self, now: datetime | None = None) -> int:
        now = now or utc_now()
        cutoff = to_iso(now - timedelta(minutes=STALE_RUN_MINUTES))
        with self.db.transaction() as connection:
            rows = connection.execute(
                """
                SELECT id FROM runs
                WHERE status = 'running' AND started_at < ?
                """,
                (cutoff,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE runs SET
                        status = 'failed',
                        error_code = 'stale_run',
                        error_message = 'run did not finish within 30 minutes',
                        finished_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (to_iso(now), row["id"]),
                )
        return len(rows)

    # --------------------------------------------------------------- papers
    def upsert_paper(self, paper: Paper, seen_at: datetime | None = None) -> None:
        now = to_iso(seen_at or utc_now())
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO papers(
                    arxiv_id, title, abstract, first_submitted_at, updated_at,
                    arxiv_url, content_hash, created_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(arxiv_id) DO UPDATE SET
                    title = excluded.title,
                    abstract = excluded.abstract,
                    first_submitted_at = excluded.first_submitted_at,
                    updated_at = excluded.updated_at,
                    arxiv_url = excluded.arxiv_url,
                    content_hash = excluded.content_hash,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    paper.arxiv_id,
                    paper.title,
                    paper.abstract,
                    to_iso(paper.first_submitted_at),
                    to_iso(paper.updated_at),
                    paper.arxiv_url,
                    paper.content_hash,
                    now,
                    now,
                ),
            )

    def get_paper(self, arxiv_id: str) -> Paper | None:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM papers WHERE arxiv_id = ?", (arxiv_id,)
            ).fetchone()
        if row is None:
            return None
        first = from_iso(row["first_submitted_at"])
        assert first is not None
        return Paper(
            arxiv_id=row["arxiv_id"],
            title=row["title"],
            abstract=row["abstract"],
            first_submitted_at=first,
            updated_at=from_iso(row["updated_at"]),
            arxiv_url=row["arxiv_url"],
            content_hash=row["content_hash"],
        )

    # ----------------------------------------------------------- extractions
    @staticmethod
    def _extraction_from_row(row: sqlite3.Row) -> ExtractionRecord:
        return ExtractionRecord(
            id=int(row["id"]),
            arxiv_id=row["arxiv_id"],
            content_hash=row["content_hash"],
            status=row["status"],
            contribution_zh=row["contribution_zh"],
            dataset_or_method_zh=row["dataset_or_method_zh"],
            code_open_source=row["code_open_source"],
            code_evidence_en=row["code_evidence_en"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            attempts=int(row["attempts"]),
            model=row["model"],
            prompt_version=row["prompt_version"],
            raw_response=row["raw_response"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_extraction(
        self, arxiv_id: str, paper_hash: str
    ) -> ExtractionRecord | None:
        with self.db.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM extractions
                WHERE arxiv_id = ? AND content_hash = ?
                """,
                (arxiv_id, paper_hash),
            ).fetchone()
        return self._extraction_from_row(row) if row is not None else None

    def ensure_pending(
        self,
        paper: Paper,
        model: str,
        prompt_version: str,
        updated_at: datetime | None = None,
    ) -> None:
        now = to_iso(updated_at or utc_now())
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO extractions(
                    arxiv_id, content_hash, status, attempts, model,
                    prompt_version, created_at, updated_at
                ) VALUES (?, ?, 'pending', 0, ?, ?, ?, ?)
                ON CONFLICT(arxiv_id, content_hash) DO UPDATE SET
                    status = 'pending',
                    contribution_zh = NULL,
                    dataset_or_method_zh = NULL,
                    code_open_source = NULL,
                    code_evidence_en = NULL,
                    error_code = NULL,
                    error_message = NULL,
                    attempts = 0,
                    model = excluded.model,
                    prompt_version = excluded.prompt_version,
                    raw_response = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    paper.arxiv_id,
                    paper.content_hash,
                    model,
                    prompt_version,
                    now,
                    now,
                ),
            )

    def save_extraction_success(
        self,
        paper: Paper,
        fields: ExtractionFields,
        attempts: int,
        model: str,
        prompt_version: str,
        raw_response: str,
        updated_at: datetime | None = None,
    ) -> None:
        now = to_iso(updated_at or utc_now())
        with self.db.transaction() as connection:
            connection.execute(
                """
                UPDATE extractions SET
                    status = 'success',
                    contribution_zh = ?,
                    dataset_or_method_zh = ?,
                    code_open_source = ?,
                    code_evidence_en = ?,
                    error_code = NULL,
                    error_message = NULL,
                    attempts = ?,
                    model = ?,
                    prompt_version = ?,
                    raw_response = ?,
                    updated_at = ?
                WHERE arxiv_id = ? AND content_hash = ?
                """,
                (
                    fields.contribution_zh,
                    fields.dataset_or_method_zh,
                    fields.code_open_source,
                    fields.code_evidence_en,
                    int(attempts),
                    model,
                    prompt_version,
                    _truncate(raw_response, MAX_RAW_RESPONSE),
                    now,
                    paper.arxiv_id,
                    paper.content_hash,
                ),
            )

    def save_extraction_failure(
        self,
        paper: Paper,
        error_code: str,
        error_message: str,
        attempts: int,
        model: str,
        prompt_version: str,
        raw_response: str = "",
        updated_at: datetime | None = None,
    ) -> None:
        now = to_iso(updated_at or utc_now())
        with self.db.transaction() as connection:
            connection.execute(
                """
                UPDATE extractions SET
                    status = 'failed',
                    contribution_zh = NULL,
                    dataset_or_method_zh = NULL,
                    code_open_source = NULL,
                    code_evidence_en = NULL,
                    error_code = ?,
                    error_message = ?,
                    attempts = ?,
                    model = ?,
                    prompt_version = ?,
                    raw_response = ?,
                    updated_at = ?
                WHERE arxiv_id = ? AND content_hash = ?
                """,
                (
                    error_code,
                    _truncate(error_message, MAX_ERROR_MESSAGE),
                    int(attempts),
                    model,
                    prompt_version,
                    _truncate(raw_response, MAX_RAW_RESPONSE),
                    now,
                    paper.arxiv_id,
                    paper.content_hash,
                ),
            )

    # -------------------------------------------------------------- run rows
    def add_run_paper(self, run_id: int, arxiv_id: str, rank: int) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO run_papers(run_id, arxiv_id, rank)
                VALUES (?, ?, ?)
                ON CONFLICT(run_id, arxiv_id) DO UPDATE SET rank = excluded.rank
                """,
                (run_id, arxiv_id, rank),
            )

    def add_exclusion(
        self,
        run_id: int,
        arxiv_id: str,
        title: str,
        reason: str,
        matched_terms: Iterable[str] = (),
        created_at: datetime | None = None,
    ) -> None:
        now = to_iso(created_at or utc_now())
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO exclusions(
                    run_id, arxiv_id, title, reason, matched_terms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    arxiv_id,
                    title[:500],
                    reason,
                    ", ".join(matched_terms)[:500],
                    now,
                ),
            )

    def run_paper_ids(self, run_id: int) -> list[str]:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT arxiv_id FROM run_papers WHERE run_id = ? ORDER BY rank",
                (run_id,),
            ).fetchall()
        return [row["arxiv_id"] for row in rows]

    def failed_extractions_for_run(
        self, run_id: int
    ) -> list[tuple[Paper, ExtractionRecord]]:
        with self.db.connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    p.arxiv_id, p.title, p.abstract, p.first_submitted_at,
                    p.updated_at, p.arxiv_url, p.content_hash,
                    e.id, e.content_hash AS extraction_hash, e.status,
                    e.contribution_zh, e.dataset_or_method_zh,
                    e.code_open_source, e.code_evidence_en,
                    e.error_code, e.error_message, e.attempts, e.model,
                    e.prompt_version, e.raw_response, e.created_at, e.updated_at
                FROM run_papers rp
                JOIN papers p ON p.arxiv_id = rp.arxiv_id
                JOIN extractions e
                    ON e.arxiv_id = p.arxiv_id
                   AND e.content_hash = p.content_hash
                WHERE rp.run_id = ? AND e.status = 'failed'
                ORDER BY rp.rank
                """,
                (run_id,),
            ).fetchall()
        results: list[tuple[Paper, ExtractionRecord]] = []
        for row in rows:
            first = from_iso(row["first_submitted_at"])
            assert first is not None
            paper = Paper(
                arxiv_id=row["arxiv_id"],
                title=row["title"],
                abstract=row["abstract"],
                first_submitted_at=first,
                updated_at=from_iso(row["updated_at"]),
                arxiv_url=row["arxiv_url"],
                content_hash=row["content_hash"],
            )
            extraction = ExtractionRecord(
                id=int(row["id"]),
                arxiv_id=row["arxiv_id"],
                content_hash=row["extraction_hash"],
                status=row["status"],
                contribution_zh=row["contribution_zh"],
                dataset_or_method_zh=row["dataset_or_method_zh"],
                code_open_source=row["code_open_source"],
                code_evidence_en=row["code_evidence_en"],
                error_code=row["error_code"],
                error_message=row["error_message"],
                attempts=int(row["attempts"]),
                model=row["model"],
                prompt_version=row["prompt_version"],
                raw_response=row["raw_response"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            results.append((paper, extraction))
        return results

    def run_counts(self, run_id: int) -> tuple[int, int]:
        with self.db.connection() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM(CASE WHEN e.status = 'failed' THEN 1 ELSE 0 END), 0)
                        AS failures
                FROM run_papers rp
                JOIN papers p ON p.arxiv_id = rp.arxiv_id
                LEFT JOIN extractions e
                    ON e.arxiv_id = p.arxiv_id
                   AND e.content_hash = p.content_hash
                WHERE rp.run_id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return 0, 0
        return int(row["total"]), int(row["failures"])

    # --------------------------------------------------------------- queries
    @staticmethod
    def _like_pattern(keyword: str) -> str:
        escaped = (
            keyword.strip()
            .lower()
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        return f"%{escaped}%"

    def _batch_where(
        self, run_id: int, keyword: str, code_filter: str
    ) -> tuple[str, list[Any]]:
        conditions = ["rp.run_id = ?"]
        params: list[Any] = [run_id]
        if keyword.strip():
            pattern = self._like_pattern(keyword)
            conditions.append(
                "(LOWER(p.title) LIKE ? ESCAPE '\\' "
                "OR LOWER(p.abstract) LIKE ? ESCAPE '\\' "
                "OR LOWER(COALESCE(e.contribution_zh, '')) LIKE ? ESCAPE '\\' "
                "OR LOWER(COALESCE(e.dataset_or_method_zh, '')) LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern, pattern, pattern, pattern])
        normalized = (code_filter or "all").lower()
        if normalized in {"yes", "no"}:
            conditions.append(
                "e.status = 'success' AND e.code_open_source = ?"
            )
            params.append("Yes" if normalized == "yes" else "No")
        return " AND ".join(conditions), params

    def batch_rows(
        self, run_id: int | None, keyword: str = "", code_filter: str = "all"
    ) -> list[PaperView]:
        if run_id is None:
            return []
        where, params = self._batch_where(run_id, keyword, code_filter)
        with self.db.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    p.arxiv_id, p.title, p.abstract, p.first_submitted_at,
                    p.arxiv_url, rp.rank,
                    e.contribution_zh, e.dataset_or_method_zh,
                    e.code_open_source, e.code_evidence_en,
                    COALESCE(e.status, 'pending') AS extraction_status,
                    e.error_code AS extraction_error
                FROM run_papers rp
                JOIN papers p ON p.arxiv_id = rp.arxiv_id
                LEFT JOIN extractions e
                    ON e.arxiv_id = p.arxiv_id
                   AND e.content_hash = p.content_hash
                WHERE {where}
                ORDER BY rp.rank ASC
                """,
                params,
            ).fetchall()
        views: list[PaperView] = []
        for row in rows:
            first = from_iso(row["first_submitted_at"])
            assert first is not None
            views.append(
                PaperView(
                    arxiv_id=row["arxiv_id"],
                    title=row["title"],
                    abstract=row["abstract"],
                    first_submitted_at=first,
                    arxiv_url=row["arxiv_url"],
                    rank=int(row["rank"]),
                    contribution_zh=row["contribution_zh"],
                    dataset_or_method_zh=row["dataset_or_method_zh"],
                    code_open_source=row["code_open_source"],
                    code_evidence_en=row["code_evidence_en"],
                    extraction_status=row["extraction_status"],
                    extraction_error=row["extraction_error"],
                )
            )
        return views

    def count_batch(
        self, run_id: int | None, keyword: str = "", code_filter: str = "all"
    ) -> int:
        if run_id is None:
            return 0
        where, params = self._batch_where(run_id, keyword, code_filter)
        with self.db.connection() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS n
                FROM run_papers rp
                JOIN papers p ON p.arxiv_id = rp.arxiv_id
                LEFT JOIN extractions e
                    ON e.arxiv_id = p.arxiv_id
                   AND e.content_hash = p.content_hash
                WHERE {where}
                """,
                params,
            ).fetchone()
        return int(row["n"]) if row is not None else 0

    def count_runs(self) -> int:
        with self.db.connection() as connection:
            row = connection.execute("SELECT COUNT(*) AS n FROM runs").fetchone()
        return int(row["n"]) if row is not None else 0
