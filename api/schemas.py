"""Контракт ответов HTTP API для схемы OpenAPI.

Обработчики сами возвращают `JSONResponse`, поэтому FastAPI к этим моделям их не
приводит: так одинаково записываются даты и ставятся заголовки. Совпадение настоящих
ответов с моделями проверяет `tests/test_api_downloads.py`.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

DownloadStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "expired"]
Platform = Literal["youtube", "tiktok", "instagram", "rutube", "vk"]
DownloadErrorCode = Literal[
    "unsupported_media",
    "format_unavailable",
    "duration_limit",
    "file_too_large",
    "media_unavailable",
    "media_forbidden",
    "platform_rate_limited",
    "network_error",
    "timeout",
    "download_failed",
]
ErrorCode = Literal[
    "missing_api_key",
    "invalid_api_key",
    "api_key_expired",
    "not_found",
    "method_not_allowed",
    "not_ready",
    "no_file",
    "file_expired",
    "request_too_large",
    "invalid_request",
    "unsupported_url",
    "rate_limited",
    "too_many_active_downloads",
    "too_many_failed_attempts",
    "http_error",
    "internal_error",
    "service_unavailable",
]


class DownloadFile(BaseModel):
    name: str
    size: int | None = Field(description="Size in bytes.")
    content_type: str
    expires_at: datetime = Field(description="The file is removed after this moment.")


class DownloadError(BaseModel):
    code: DownloadErrorCode
    message: str
    reference: str | None = Field(
        None, description="Code that administrators use to find the failure in the log."
    )


class DownloadLinks(BaseModel):
    self: str = Field(description="This job.")
    file: str | None = Field(description="The finished file; null until the job succeeds.")


class Download(BaseModel):
    id: str
    status: DownloadStatus
    type: Literal["video", "audio"]
    platform: Platform
    url: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    file: DownloadFile | None
    error: DownloadError | None
    links: DownloadLinks


class DownloadList(BaseModel):
    items: list[Download] = Field(description="Jobs of this key, newest first.")


class KeyLimits(BaseModel):
    requests_per_minute: int
    active_downloads: int
    result_ttl_minutes: int
    max_file_size_mb: int


class KeyInfo(BaseModel):
    name: str
    key_prefix: str
    expires_at: datetime | None
    limits: KeyLimits


class ErrorDetail(BaseModel):
    field: str
    message: str


class Error(BaseModel):
    code: ErrorCode
    message: str
    details: list[ErrorDetail] | None = Field(None, description="Invalid fields of the request.")


class ErrorEnvelope(BaseModel):
    error: Error


def header_doc(description: str, schema_type: str = "string") -> dict:
    return {"description": description, "schema": {"type": schema_type}}


def error_doc(description: str, headers: dict | None = None) -> dict:
    return {"model": ErrorEnvelope, "description": description, "headers": headers or {}}


RETRY_AFTER = {"Retry-After": header_doc("Seconds to wait before the next request.", "integer")}
RATE_LIMIT = {
    "X-RateLimit-Limit": header_doc("Requests allowed per minute for this key.", "integer"),
    "X-RateLimit-Remaining": header_doc("Requests left in the current window.", "integer"),
}
# `default` заодно убирает из схемы модель ошибки 422 от FastAPI: у нас формат другой.
COMMON_ERRORS: dict[int | str, dict] = {
    401: error_doc(
        "The key is missing, invalid, revoked, or expired.",
        {"WWW-Authenticate": header_doc("Authentication scheme.")},
    ),
    429: error_doc("Too many requests, active downloads, or invalid keys.", RETRY_AFTER),
    503: error_doc("The key database is busy.", RETRY_AFTER),
    "default": error_doc("Any other error, in the same format."),
}
NOT_FOUND: dict[int | str, dict] = {404: error_doc("Unknown job or a job of another key.")}
FILE_CONTENT = {
    "description": "The file; `Range` requests are supported.",
    "headers": {
        "Content-Disposition": header_doc("File name as `filename` and `filename*`."),
        **RATE_LIMIT,
    },
    "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
}
