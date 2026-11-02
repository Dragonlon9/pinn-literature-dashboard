"""DeepSeek 字段抽取：结构化 Prompt、JSON 校验、证据核验与有限重试。"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import time
import unicodedata
from typing import Any

import requests

from .config import Settings
from .models import (
    ExtractionFields,
    ExtractionResult,
    Paper,
    normalize_text,
)

PROMPT_VERSION = "extract_v1"
_REQUIRED_KEYS = {
    "contribution_zh",
    "dataset_or_method_zh",
    "code_open_source",
    "code_evidence_en",
}
_STOP_BATCH_CODES = {"missing_api_key", "auth_error", "insufficient_balance"}


class ExtractionError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        stop_batch: bool = False,
        raw: str = "",
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.stop_batch = stop_batch
        self.raw = raw
        self.retry_after = retry_after


def _sanitize(text: str | None, api_key: str | None) -> str:
    value = text or ""
    if api_key:
        value = value.replace(api_key, "[REDACTED]")
    value = re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "[REDACTED]", value)
    return value[:1000]


def _normalize_for_match(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"\s+", " ", value).strip()


def _fill_template(template: str, paper: Paper) -> str:
    """一次替换两个占位符，避免摘要中的花括号或占位符被二次解释。"""

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return paper.title if key == "title" else paper.abstract

    return re.sub(r"\{\{(title|abstract)\}\}", replace, template)


@dataclass(frozen=True)
class _RawCall:
    content: str
    raw: str


class Extractor:
    def __init__(
        self,
        settings: Settings,
        session: requests.Session | None = None,
        sleep=time.sleep,
    ):
        self.settings = settings
        self.session = session or requests.Session()
        self._sleep = sleep
        prompt_dir = Path(settings.prompt_dir)
        self.system_prompt = (prompt_dir / "extract_v1_system.txt").read_text(
            encoding="utf-8"
        )
        self.user_template = (prompt_dir / "extract_v1_user.txt").read_text(
            encoding="utf-8"
        )

    def extract(self, paper: Paper) -> ExtractionResult:
        if not self.settings.has_api_key:
            return ExtractionResult(
                status="failed",
                error_code="missing_api_key",
                error_message="未配置 DEEPSEEK_API_KEY 环境变量；未调用 DeepSeek。",
                attempts=0,
                model=self.settings.deepseek_model,
                prompt_version=PROMPT_VERSION,
            )

        user_prompt = _fill_template(self.user_template, paper)
        last_error: ExtractionError | None = None
        last_raw = ""

        for attempt in range(1, self.settings.deepseek_max_attempts + 1):
            try:
                call = self._call(paper, user_prompt)
                last_raw = call.raw
                fields = self._validate(call.content, paper)
            except ExtractionError as exc:
                last_error = exc
                last_raw = exc.raw or last_raw
                if exc.stop_batch or not exc.retryable:
                    break
                if attempt >= self.settings.deepseek_max_attempts:
                    break
                self._sleep(exc.retry_after if exc.retry_after is not None else 2.0)
                continue
            except Exception as exc:  # 防御性兜底：不让单篇异常打断整批
                last_error = ExtractionError(
                    "internal_error",
                    f"抽取器内部异常：{type(exc).__name__}",
                    retryable=False,
                )
                break

            return ExtractionResult(
                status="success",
                fields=fields,
                attempts=attempt,
                raw_response=_sanitize(last_raw, self.settings.deepseek_api_key),
                model=self.settings.deepseek_model,
                prompt_version=PROMPT_VERSION,
            )

        if last_error is None:
            last_error = ExtractionError(
                "internal_error", "抽取失败但未记录到错误", retryable=False
            )
        return ExtractionResult(
            status="failed",
            error_code=last_error.code,
            error_message=_sanitize(
                last_error.message, self.settings.deepseek_api_key
            ),
            raw_response=_sanitize(last_raw, self.settings.deepseek_api_key),
            attempts=attempt,
            model=self.settings.deepseek_model,
            prompt_version=PROMPT_VERSION,
        )

    def _call(self, paper: Paper, user_prompt: str) -> _RawCall:
        payload: dict[str, Any] = {
            "model": self.settings.deepseek_model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 600,
            "thinking": {"type": "disabled"},
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.settings.deepseek_api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = self.session.post(
                self.settings.deepseek_endpoint,
                headers=headers,
                json=payload,
                timeout=(
                    self.settings.connect_timeout_seconds,
                    self.settings.read_timeout_seconds,
                ),
            )
        except requests.Timeout:
            raise ExtractionError(
                "timeout", "DeepSeek 请求超时", retryable=True
            ) from None
        except requests.ConnectionError:
            raise ExtractionError(
                "connection_error", "DeepSeek 连接失败", retryable=True
            ) from None
        except requests.RequestException as exc:
            raise ExtractionError(
                "request_error",
                f"DeepSeek 请求异常：{type(exc).__name__}",
                retryable=True,
            ) from None

        status = int(response.status_code)
        if status == 401:
            raise ExtractionError(
                "auth_error",
                "DeepSeek 鉴权失败（HTTP 401）；未重试，未输出密钥。",
                retryable=False,
                stop_batch=True,
            )
        if status == 402:
            raise ExtractionError(
                "insufficient_balance",
                "DeepSeek 余额不足（HTTP 402）；未重试。",
                retryable=False,
                stop_batch=True,
            )
        if status == 429:
            raise ExtractionError(
                "rate_limited",
                "DeepSeek 返回 429（请求过多）",
                retryable=True,
                retry_after=_retry_after(response),
            )
        if status in {500, 502, 503, 504}:
            raise ExtractionError(
                "server_error",
                f"DeepSeek 返回 HTTP {status}",
                retryable=True,
            )
        if status == 408:
            raise ExtractionError(
                "timeout", "DeepSeek 返回 HTTP 408", retryable=True
            )
        if status != 200:
            body = _sanitize(response.text, self.settings.deepseek_api_key)
            raise ExtractionError(
                "bad_request" if status in {400, 422} else "http_error",
                f"DeepSeek 返回 HTTP {status}：{body[:300]}",
                retryable=False,
                raw=body,
            )

        raw_text = _sanitize(response.text, self.settings.deepseek_api_key)
        try:
            data = response.json()
        except ValueError:
            raise ExtractionError(
                "bad_response",
                "DeepSeek 响应不是合法 JSON",
                retryable=True,
                raw=raw_text,
            ) from None

        try:
            choice = data["choices"][0]
            message = choice["message"]
            content = message["content"]
            finish_reason = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError):
            raise ExtractionError(
                "bad_response",
                "DeepSeek 响应缺少 choices[0].message.content",
                retryable=True,
                raw=raw_text,
            ) from None

        if finish_reason == "length":
            raise ExtractionError(
                "output_truncated",
                "DeepSeek 输出被 max_tokens 截断",
                retryable=True,
                raw=raw_text,
            )
        if not isinstance(content, str) or not content.strip():
            raise ExtractionError(
                "empty_content",
                "DeepSeek 返回空白内容",
                retryable=True,
                raw=raw_text,
            )
        return _RawCall(content=content, raw=_sanitize(content, self.settings.deepseek_api_key))

    def _validate(self, content: str, paper: Paper) -> ExtractionFields:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ExtractionError(
                "json_decode_error",
                f"模型输出不是合法 JSON：{exc.msg}",
                retryable=True,
                raw=_sanitize(content, self.settings.deepseek_api_key),
            ) from None
        if not isinstance(payload, dict):
            raise ExtractionError(
                "schema_error", "模型输出 JSON 不是对象", retryable=True
            )
        if set(payload.keys()) != _REQUIRED_KEYS:
            raise ExtractionError(
                "schema_error",
                "模型输出键集合不是恰好四个",
                retryable=True,
            )
        for key in _REQUIRED_KEYS:
            if not isinstance(payload[key], str):
                raise ExtractionError(
                    "schema_error", f"字段 {key} 不是字符串", retryable=True
                )

        contribution = payload["contribution_zh"].strip()
        dataset = payload["dataset_or_method_zh"].strip()
        code = payload["code_open_source"].strip()
        evidence = payload["code_evidence_en"].strip()

        if not contribution:
            raise ExtractionError(
                "schema_error", "contribution_zh 为空", retryable=True
            )
        if len(contribution) > 100:
            raise ExtractionError(
                "schema_error", "contribution_zh 超过 100 字", retryable=True
            )
        if not dataset:
            raise ExtractionError(
                "schema_error", "dataset_or_method_zh 为空", retryable=True
            )
        if code not in {"Yes", "No"}:
            raise ExtractionError(
                "schema_error",
                "code_open_source 不是 Yes/No",
                retryable=True,
            )
        if code == "Yes" and not evidence:
            raise ExtractionError(
                "evidence_mismatch",
                "code_open_source 为 Yes 但 code_evidence_en 为空",
                retryable=True,
            )
        if evidence:
            source = _normalize_for_match(
                f"{normalize_text(paper.title)}\n{normalize_text(paper.abstract)}"
            )
            if _normalize_for_match(evidence) not in source:
                raise ExtractionError(
                    "evidence_mismatch",
                    "code_evidence_en 不是标题/摘要中的连续原句",
                    retryable=True,
                )

        return ExtractionFields(
            contribution_zh=contribution,
            dataset_or_method_zh=dataset,
            code_open_source=code,
            code_evidence_en=evidence,
        )


def _retry_after(response: requests.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return min(30.0, max(0.0, float(raw.strip())))
    except ValueError:
        return None
