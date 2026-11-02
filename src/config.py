"""集中配置：只从环境变量读取，不自动加载 .env。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "pinn.db"
DEFAULT_PROMPT_DIR = PROJECT_ROOT / "prompts"
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs"


def _env_text(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default.resolve()
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


@dataclass(frozen=True)
class Settings:
    """运行配置。

    ``deepseek_api_key`` 使用 ``repr=False``，避免 dataclass 调试输出或日志意外带出密钥。
    """

    project_root: Path = PROJECT_ROOT
    db_path: Path = DEFAULT_DB_PATH
    prompt_dir: Path = DEFAULT_PROMPT_DIR
    log_dir: Path = DEFAULT_LOG_DIR
    deepseek_api_key: str | None = field(default=None, repr=False)
    deepseek_model: str = "deepseek-flash"
    deepseek_base_url: str = "https://api.deepseek.com"
    arxiv_api_url: str = "https://export.arxiv.org/api/query"
    timezone_name: str = "Asia/Shanghai"
    scheduled_hour: int = 8
    scheduled_minute: int = 0
    scheduler_interval_seconds: int = 20
    scheduler_enabled: bool = True
    arxiv_page_size: int = 100
    arxiv_max_pages: int = 5
    arxiv_hard_cap: int = 500
    arxiv_page_delay_seconds: float = 3.0
    arxiv_max_attempts: int = 3
    deepseek_max_attempts: int = 2
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 30.0
    raw_response_limit: int = 8192
    user_agent: str = "PINN-Literature-Dashboard/1.0 (local academic project)"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            project_root=PROJECT_ROOT,
            db_path=_env_path("PINN_DB_PATH", DEFAULT_DB_PATH),
            prompt_dir=_env_path("PINN_PROMPT_DIR", DEFAULT_PROMPT_DIR),
            log_dir=_env_path("PINN_LOG_DIR", DEFAULT_LOG_DIR),
            deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY") or None,
            deepseek_model=_env_text("DEEPSEEK_MODEL", "deepseek-flash"),
            deepseek_base_url=_env_text(
                "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
            ).rstrip("/"),
            arxiv_api_url=_env_text(
                "ARXIV_API_URL", "https://export.arxiv.org/api/query"
            ),
            timezone_name=_env_text("PINN_TIMEZONE", "Asia/Shanghai"),
            scheduled_hour=max(0, min(23, _env_int("PINN_SCHEDULED_HOUR", 8))),
            scheduled_minute=max(0, min(59, _env_int("PINN_SCHEDULED_MINUTE", 0))),
            scheduler_interval_seconds=max(
                5, _env_int("PINN_SCHEDULER_INTERVAL_SECONDS", 20)
            ),
            scheduler_enabled=_env_bool("PINN_SCHEDULER_ENABLED", True),
            arxiv_page_size=max(1, min(200, _env_int("PINN_ARXIV_PAGE_SIZE", 100))),
            arxiv_max_pages=max(1, _env_int("PINN_ARXIV_MAX_PAGES", 5)),
            arxiv_hard_cap=max(1, _env_int("PINN_ARXIV_HARD_CAP", 500)),
            arxiv_page_delay_seconds=max(
                0.0, _env_float("PINN_ARXIV_PAGE_DELAY_SECONDS", 3.0)
            ),
            arxiv_max_attempts=max(1, _env_int("PINN_ARXIV_MAX_ATTEMPTS", 3)),
            deepseek_max_attempts=max(
                1, _env_int("PINN_DEEPSEEK_MAX_ATTEMPTS", 2)
            ),
            connect_timeout_seconds=max(
                1.0, _env_float("PINN_CONNECT_TIMEOUT_SECONDS", 5.0)
            ),
            read_timeout_seconds=max(
                1.0, _env_float("PINN_READ_TIMEOUT_SECONDS", 30.0)
            ),
            raw_response_limit=max(512, _env_int("PINN_RAW_RESPONSE_LIMIT", 8192)),
        )

    @property
    def tz(self) -> timezone | ZoneInfo:
        try:
            return ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError:
            # Windows 未安装 tzdata 时的兜底；README 要求安装 tzdata。
            return timezone(timedelta(hours=8), name="Asia/Shanghai")

    @property
    def scheduled_time(self) -> time:
        return time(self.scheduled_hour, self.scheduled_minute)

    @property
    def deepseek_endpoint(self) -> str:
        return f"{self.deepseek_base_url}/chat/completions"

    @property
    def lock_path(self) -> Path:
        return self.db_path.with_suffix(self.db_path.suffix + ".lock")

    @property
    def has_api_key(self) -> bool:
        return bool(self.deepseek_api_key and self.deepseek_api_key.strip())

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        key_state = "configured" if self.has_api_key else "missing"
        return (
            "Settings("
            f"db_path={self.db_path!r}, "
            f"model={self.deepseek_model!r}, "
            f"api_key={key_state}, "
            f"timezone={self.timezone_name!r}"
            ")"
        )
