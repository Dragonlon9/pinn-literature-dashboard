"""SQLite 连接、schema 与事务。每次操作使用短连接，避免跨线程共享连接。"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator

SCHEMA_VERSION = "1"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS papers (
    arxiv_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    abstract TEXT NOT NULL,
    first_submitted_at TEXT NOT NULL,
    updated_at TEXT,
    arxiv_url TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extractions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    arxiv_id TEXT NOT NULL REFERENCES papers(arxiv_id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'success', 'failed')),
    contribution_zh TEXT,
    dataset_or_method_zh TEXT,
    code_open_source TEXT CHECK (
        code_open_source IS NULL OR code_open_source IN ('Yes', 'No')
    ),
    code_evidence_en TEXT,
    error_code TEXT,
    error_message TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    model TEXT,
    prompt_version TEXT,
    raw_response TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (arxiv_id, content_hash)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT NOT NULL,
    requested_days INTEGER NOT NULL,
    window_start_utc TEXT NOT NULL,
    window_end_utc TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('running', 'success', 'empty', 'partial', 'failed')
    ),
    result_count INTEGER NOT NULL DEFAULT 0,
    extraction_failures INTEGER NOT NULL DEFAULT 0,
    truncated INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    error_message TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS run_papers (
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    arxiv_id TEXT NOT NULL REFERENCES papers(arxiv_id) ON DELETE CASCADE,
    rank INTEGER NOT NULL,
    PRIMARY KEY (run_id, arxiv_id)
);

CREATE TABLE IF NOT EXISTS exclusions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    arxiv_id TEXT NOT NULL,
    title TEXT,
    reason TEXT NOT NULL,
    matched_terms TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_extractions_status
    ON extractions(status);
CREATE INDEX IF NOT EXISTS idx_runs_started
    ON runs(started_at);
CREATE INDEX IF NOT EXISTS idx_run_papers_run
    ON run_papers(run_id, rank);
CREATE INDEX IF NOT EXISTS idx_exclusions_run
    ON exclusions(run_id);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def init_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.executescript(SCHEMA_SQL)
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO app_meta(key, value, updated_at)
                VALUES ('schema_version', ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (SCHEMA_VERSION,),
            )
