"""每日 08:00 调度与启动补跑；后台线程不调用任何 Streamlit API。"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import threading
from typing import Callable

from .config import Settings
from .models import RefreshResult, ensure_utc, utc_now
from .pipeline import Pipeline
from .repository import Repository

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(
        self,
        pipeline: Pipeline,
        repository: Repository,
        settings: Settings,
        *,
        clock: Callable[[], datetime] = utc_now,
        interval_seconds: int | None = None,
    ):
        self.pipeline = pipeline
        self.repo = repository
        self.settings = settings
        self._clock = clock
        self._interval = interval_seconds or settings.scheduler_interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_check_at: datetime | None = None
        self.last_result: RefreshResult | None = None

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return ensure_utc(value)

    def _local_now(self) -> datetime:
        return self._now().astimezone(self.settings.tz)

    def start(self) -> None:
        if not self.settings.scheduler_enabled:
            logger.info("scheduler disabled by PINN_SCHEDULER_ENABLED")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="pinn-scheduler", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        try:
            self.startup_check()
        except Exception:
            logger.exception("scheduler startup check failed")
        while not self._stop.wait(self._interval):
            try:
                self.check_due()
            except Exception:
                logger.exception("scheduler loop check failed")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def startup_check(self) -> RefreshResult | None:
        local = self._local_now()
        self.last_check_at = local
        try:
            self.repo.recover_stale_runs(self._now())
        except Exception:
            logger.exception("recover_stale_runs failed")

        today = local.date().isoformat()
        current_id = self.repo.current_run_id()
        scheduled_date = self.repo.get_meta("last_scheduled_date")
        after_schedule = (local.hour, local.minute) >= (
            self.settings.scheduled_hour,
            self.settings.scheduled_minute,
        )

        if current_id is None:
            # 首次打开、无缓存：补跑一次 7 天；若已过 08:00，视为今日自动任务已尝试。
            if after_schedule:
                self.repo.set_meta("last_scheduled_date", today)
            return self._trigger("startup_catchup")

        if after_schedule and scheduled_date != today:
            self.repo.set_meta("last_scheduled_date", today)
            return self._trigger("startup_catchup")
        return None

    def check_due(self, now: datetime | None = None) -> RefreshResult | None:
        local = (now or self._clock())
        if local.tzinfo is None:
            local = local.replace(tzinfo=timezone.utc)
        local = local.astimezone(self.settings.tz)
        self.last_check_at = local

        if (local.hour, local.minute) < (
            self.settings.scheduled_hour,
            self.settings.scheduled_minute,
        ):
            return None
        today = local.date().isoformat()
        if self.repo.get_meta("last_scheduled_date") == today:
            return None
        self.repo.set_meta("last_scheduled_date", today)
        return self._trigger("scheduled")

    def _trigger(self, trigger: str) -> RefreshResult:
        result = self.pipeline.refresh(7, trigger)
        self.last_result = result
        if result.busy:
            logger.warning("scheduler skipped: busy (trigger=%s)", trigger)
        else:
            logger.info(
                "scheduler %s status=%s count=%s failures=%s",
                trigger,
                result.status,
                result.result_count,
                result.extraction_failures,
            )
        return result

    def status(self) -> dict[str, object]:
        return {
            "enabled": self.settings.scheduler_enabled,
            "running": self.is_running,
            "interval_seconds": self._interval,
            "last_check_at": self.last_check_at,
            "last_result": self.last_result,
            "last_scheduled_date": self.repo.get_meta("last_scheduled_date"),
        }
