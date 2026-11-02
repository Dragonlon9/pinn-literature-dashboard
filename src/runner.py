"""组装运行时；手动刷新、CLI 与定时任务共用同一个 Pipeline.refresh。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .arxiv_client import ArxivClient
from .config import Settings
from .extractor import Extractor
from .models import RefreshResult, utc_now
from .pipeline import Pipeline
from .repository import Repository
from .scheduler import Scheduler


@dataclass
class Runtime:
    settings: Settings
    repo: Repository
    pipeline: Pipeline
    scheduler: Scheduler


def build_pipeline(settings: Settings | None = None) -> tuple[Repository, Pipeline]:
    settings = settings or Settings.from_env()
    repo = Repository(settings.db_path)
    repo.init()
    repo.recover_stale_runs()
    pipeline = Pipeline(
        settings,
        repo,
        ArxivClient(settings),
        Extractor(settings),
    )
    return repo, pipeline


def build_runtime(
    settings: Settings | None = None, *, start_scheduler: bool = True
) -> Runtime:
    settings = settings or Settings.from_env()
    repo, pipeline = build_pipeline(settings)
    scheduler = Scheduler(pipeline, repo, settings)
    if start_scheduler:
        scheduler.start()
    return Runtime(
        settings=settings, repo=repo, pipeline=pipeline, scheduler=scheduler
    )


def run_update(
    days: int = 7,
    trigger: str = "manual",
    settings: Settings | None = None,
) -> RefreshResult:
    settings = settings or Settings.from_env()
    repo, pipeline = build_pipeline(settings)
    valid_days = (
        isinstance(days, int)
        and not isinstance(days, bool)
        and 1 <= days <= 30
    )
    if trigger in {"scheduled", "startup_catchup"} and valid_days:
        local_today = utc_now().astimezone(settings.tz).date().isoformat()
        repo.set_meta("last_scheduled_date", local_today)
    return pipeline.refresh(days, trigger)


def resolve_db_override(path: str) -> Path:
    return Path(path).expanduser().resolve()
