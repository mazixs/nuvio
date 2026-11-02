"""HTTP API загрузки видео и аудио по ссылке.

Ключи выдаёт администратор в WebUI, здесь они только проверяются. Загрузка
асинхронная: POST создаёт задачу и сразу отвечает 202 со ссылкой на статус,
клиент опрашивает статус и забирает готовый файл, пока тот не истёк. Так
многоминутная загрузка не упирается в таймауты обратного прокси.

API живёт в отдельном процессе от WebUI: админку можно держать закрытой,
а API открыть наружу.
"""

import asyncio
import re
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.jobs import ACTIVE_STATUSES, Job, JobManager, TooManyActiveJobsError
from api.schemas import (
    COMMON_ERRORS,
    FILE_CONTENT,
    NOT_FOUND,
    RATE_LIMIT,
    RETRY_AFTER,
    Download,
    DownloadList,
    KeyInfo,
    error_doc,
    header_doc,
)
from config import (
    API_MAX_ACTIVE_JOBS,
    API_MAX_FILE_SIZE_MB,
    API_PORT,
    API_RATE_LIMIT_PER_MINUTE,
    API_RESULT_TTL_MINUTES,
)
from utils.analytics_db import close_connection, init_db
from utils.api_keys import (
    ApiKeyIdentity,
    ExpiredApiKeyError,
    InvalidApiKeyError,
    authenticate_api_key,
    inactive_api_key_ids,
    is_well_formed,
    record_api_download,
)
from utils.db_worker import run_db
from utils.logger import setup_logger
from utils.media_fetch import URL_MAX_LENGTH, detect_platform

logger = setup_logger("nuvio.api")

API_PREFIX = "/api/v1"
DOCS_URL = "/api/docs"
SWAGGER_UI_VERSION = "5.34.1"
POLL_INTERVAL_SECONDS = 2
SWEEP_INTERVAL_SECONDS = 60
# Неверные ключи с одного адреса. Ключ на ~190 бит не подобрать, поэтому лимит
# лишь гасит сканеры: верный ключ с того же адреса не блокируется никогда,
# иначе чужой мусор за общим прокси отрезал бы честных клиентов.
AUTH_FAILURE_LIMIT = 20
AUTH_FAILURE_WINDOW_SECONDS = 600
_MAX_TRACKED_CLIENTS = 10_000
# В теле только ссылка до 2 КБ и тип, остальное — запас на экранирование в JSON.
MAX_BODY_BYTES = 8 * 1024


class ApiError(Exception):
    """Ответ об ошибке в едином формате `{"error": {"code", "message"}}`."""

    def __init__(
        self, status: int, code: str, message: str, *, retry_after: int | None = None
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


class SlidingWindowLimiter:
    """Счётчик событий в скользящем окне на ключ; память ограничена."""

    def __init__(self, limit: int, window_seconds: float):
        self.limit = limit
        self.window = window_seconds
        self._events: dict[object, deque[float]] = defaultdict(deque)

    def _trim(self, subject: object, now: float) -> deque[float]:
        events = self._events[subject]
        while events and now - events[0] >= self.window:
            events.popleft()
        return events

    def _bound_memory(self, now: float) -> None:
        if len(self._events) <= _MAX_TRACKED_CLIENTS:
            return
        for subject in list(self._events):
            if not self._trim(subject, now):
                del self._events[subject]
        while len(self._events) > _MAX_TRACKED_CLIENTS:
            del self._events[next(iter(self._events))]

    def retry_after(self, subject: object, now: float | None = None) -> int:
        """Секунды до освобождения места; 0 — лимит не исчерпан."""
        now = time.monotonic() if now is None else now
        events = self._trim(subject, now)
        if len(events) < self.limit:
            return 0
        return max(1, int(events[0] + self.window - now + 0.999))

    def hit(self, subject: object, now: float | None = None) -> tuple[bool, int]:
        """Регистрирует событие, если есть место. Возвращает (принято, осталось)."""
        now = time.monotonic() if now is None else now
        events = self._trim(subject, now)
        if len(events) >= self.limit:
            return False, 0
        events.append(now)
        self._bound_memory(now)
        return True, self.limit - len(events)

    def clear(self) -> None:
        self._events.clear()


_rate_limiter = SlidingWindowLimiter(API_RATE_LIMIT_PER_MINUTE, 60)
_auth_failures = SlidingWindowLimiter(AUTH_FAILURE_LIMIT, AUTH_FAILURE_WINDOW_SECONDS)


async def sweep_once(jobs: JobManager) -> None:
    """Завершает зависшие задачи, убирает истёкшие результаты и задачи снятых ключей.

    Ключи отзывают в WebUI, другом процессе, поэтому API узнаёт об этом из базы.
    """
    await asyncio.to_thread(jobs.fail_stuck_jobs)
    await asyncio.to_thread(jobs.sweep)
    key_ids = jobs.key_ids()
    if key_ids:
        revoked = await run_db(inactive_api_key_ids, key_ids)
        if revoked:
            await asyncio.to_thread(jobs.drop_keys, revoked)


async def _sweep_forever(jobs: JobManager) -> None:
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            await sweep_once(jobs)
        except Exception:
            logger.exception("Не удалось очистить результаты API")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await run_db(init_db)
    jobs = JobManager()
    await asyncio.to_thread(jobs.purge_orphans)
    app.state.jobs = jobs
    sweeper = asyncio.create_task(_sweep_forever(jobs))
    try:
        yield
    finally:
        sweeper.cancel()
        jobs.shutdown()
        close_connection()


app = FastAPI(
    title="Nuvio API",
    version="1",
    description=(
        "Download video or audio by link from YouTube, TikTok, Instagram, Rutube "
        "and VK Video. Create a job, poll its status, then fetch the file."
    ),
    # Страница описания собрана ниже: своя закрепляет версию Swagger UI.
    docs_url=None,
    redoc_url=None,
    openapi_url=f"{API_PREFIX}/openapi.json",
    lifespan=lifespan,
    # Имя функции вместо длинного значения FastAPI: по нему называются методы
    # сгенерированных клиентов, и переименование их сломает.
    generate_unique_id_function=lambda route: route.name,
)


def _error_response(
    status: int, code: str, message: str, *, retry_after: int | None = None, details: list | None = None
) -> JSONResponse:
    error: dict = {"code": code, "message": message}
    if details:
        error["details"] = details
    headers = {"Retry-After": str(retry_after)} if retry_after else None
    if status == 401:
        headers = {**(headers or {}), "WWW-Authenticate": 'Bearer realm="nuvio"'}
    return JSONResponse({"error": error}, status_code=status, headers=headers)


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    return _error_response(exc.status, exc.code, exc.message, retry_after=exc.retry_after)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    # Введённое значение не возвращается: в теле может оказаться что угодно.
    details = [
        {"field": ".".join(str(part) for part in error["loc"][1:]) or "body", "message": error["msg"]}
        for error in exc.errors()
    ]
    return _error_response(422, "invalid_request", "The request body is invalid.", details=details)


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    code = {404: "not_found", 405: "method_not_allowed", 413: "request_too_large"}.get(exc.status_code, "http_error")
    return _error_response(exc.status_code, code, str(exc.detail))


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception):
    logger.error("Сбой обработчика %s %s", request.method, request.url.path, exc_info=exc)
    response = _error_response(500, "internal_error", "Something went wrong on the server.")
    # Этот обработчик работает снаружи middleware: общие заголовки сюда не доходят.
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.exception_handler(sqlite3.OperationalError)
async def database_error_handler(request: Request, exc: sqlite3.OperationalError):
    logger.error("База ключей недоступна: %s", exc)
    return _error_response(503, "service_unavailable", "Try again shortly.", retry_after=5)


class SecurityHeadersMiddleware:
    """Общие заголовки ответа.

    Чистый ASGI, а не `@app.middleware("http")`: тот прогоняет тело ответа,
    включая файлы до 2 ГБ, через собственный поток в памяти.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("Cache-Control", "no-store")
                headers.setdefault("X-Content-Type-Options", "nosniff")
                # `request.state` хранится в scope["state"].
                remaining = scope.get("state", {}).get("rate_limit_remaining")
                if remaining is not None:
                    headers["X-RateLimit-Limit"] = str(_rate_limiter.limit)
                    headers["X-RateLimit-Remaining"] = str(remaining)
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BodyLimitMiddleware:
    """Лимит тела запроса.

    FastAPI читает тело раньше проверки ключа, поэтому без лимита запрос без
    ключа занял бы память процесса. Считаются фактические байты: при chunked
    `Content-Length` нет, а заявленному верить нельзя. `HTTPException` FastAPI
    пропускает из чтения тела как есть, остальное превратил бы в 400.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        received = 0

        async def receive_limited():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_BODY_BYTES:
                    raise HTTPException(413, "The request body is too large.")
            return message

        await self.app(scope, receive_limited, send)


app.add_middleware(BodyLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)


_bearer = HTTPBearer(auto_error=False, description="API key issued in the Nuvio WebUI")
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="The same API key")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _invalid_key_error(client_ip: str) -> ApiError:
    """Учитывает неверный ключ с адреса; после лимита — 429 вместо 401.

    Сам двадцатый неверный ключ ещё получает 401, блокируются следующие.
    """
    retry_after = _auth_failures.retry_after(client_ip)
    if retry_after:
        return ApiError(429, "too_many_failed_attempts", "Too many invalid API keys.", retry_after=retry_after)
    _auth_failures.hit(client_ip)
    if _auth_failures.retry_after(client_ip):
        # Одна строка на блокировку, а не на каждую попытку: сканер иначе
        # прокрутил бы ротацию журнала и вытеснил записи API_DL_FAIL.
        logger.warning("Много неверных API-ключей с адреса %s, адрес заблокирован", client_ip)
    return ApiError(401, "invalid_api_key", "The API key is invalid or revoked.")


async def require_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    header_key: str | None = Depends(_api_key_header),
) -> ApiKeyIdentity:
    """Проверяет ключ из `Authorization: Bearer` или `X-API-Key`.

    Ключ в строке запроса не принимается: он оседал бы в журналах прокси.
    """
    token = (credentials.credentials if credentials else header_key or "").strip()
    if not token:
        raise ApiError(401, "missing_api_key", "Send the API key in the Authorization: Bearer header.")
    client_ip = _client_ip(request)
    # Формат и контрольная сумма проверяются до базы: мусор не занимает её пул.
    if not is_well_formed(token):
        raise _invalid_key_error(client_ip)
    try:
        identity = await run_db(authenticate_api_key, token)
    except InvalidApiKeyError:
        raise _invalid_key_error(client_ip) from None
    except ExpiredApiKeyError:
        raise ApiError(401, "api_key_expired", "The API key has expired. Ask the administrator to extend it.") from None
    allowed, remaining = _rate_limiter.hit(identity.id)
    request.state.rate_limit_remaining = remaining
    if not allowed:
        raise ApiError(
            429, "rate_limited", "Too many requests.",
            retry_after=_rate_limiter.retry_after(identity.id),
        )
    return identity


def get_jobs(request: Request) -> JobManager:
    return request.app.state.jobs


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds")


def _job_payload(job: Job, jobs: JobManager) -> dict:
    location = f"{API_PREFIX}/downloads/{job.id}"
    payload = {
        "id": job.id,
        "status": job.status,
        "type": job.media_type,
        "platform": job.platform,
        "url": job.url,
        "created_at": _iso(job.created_at),
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
        "file": None,
        "error": job.error,
        "links": {"self": location, "file": None},
    }
    file_path = job.file_path
    if job.status == "succeeded" and file_path is not None:
        payload["file"] = {
            "name": file_path.name,
            "size": job.file_size,
            "content_type": job.content_type,
            "expires_at": _iso(jobs.result_expires_at(job)),
        }
        payload["links"]["file"] = f"{location}/file"
    return payload


def _content_disposition(name: str) -> str:
    """Заголовок с `filename*` и запасным ASCII-именем для простых клиентов.

    Одного `filename*`, как у Starlette для кириллицы, мало: `curl -OJ` его не
    читает и сохраняет файл без расширения.
    """
    path = PurePosixPath(name)
    stem = re.sub(r"[^A-Za-z0-9._ -]", "", path.stem).strip(" .") or "download"
    suffix = re.sub(r"[^A-Za-z0-9.]", "", path.suffix)
    return f"attachment; filename=\"{stem}{suffix}\"; filename*=UTF-8''{quote(name, safe='')}"


def _download_not_found() -> ApiError:
    return ApiError(404, "not_found", "Download not found.")


def _owned_job(jobs: JobManager, identity: ApiKeyIdentity, job_id: str) -> Job:
    job = jobs.get(identity.id, job_id)
    if job is None:
        raise _download_not_found()
    return job


class DownloadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(
        min_length=1, max_length=URL_MAX_LENGTH,
        description="Link to a YouTube, TikTok, Instagram, Rutube or VK Video post.",
        examples=["https://www.youtube.com/watch?v=6WFj-CKldv4"],
    )
    type: Literal["video", "audio"] = Field("video", description="What to download.")


@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok"}


@app.get(DOCS_URL, include_in_schema=False)
async def swagger_ui() -> HTMLResponse:
    base = f"https://cdn.jsdelivr.net/npm/swagger-ui-dist@{SWAGGER_UI_VERSION}"
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=f"{app.title} reference",
        swagger_js_url=f"{base}/swagger-ui-bundle.js",
        swagger_css_url=f"{base}/swagger-ui.css",
        swagger_favicon_url="data:,",
    )


@app.get(
    f"{API_PREFIX}/me",
    summary="Current API key and limits",
    tags=["key"],
    response_model=KeyInfo,
    responses={200: {"headers": RATE_LIMIT}, **COMMON_ERRORS},
)
async def get_me(identity: ApiKeyIdentity = Depends(require_api_key)):
    expires_at = identity.expires_at.isoformat(timespec="seconds") if identity.expires_at else None
    return JSONResponse({
        "name": identity.name,
        "key_prefix": identity.prefix,
        "expires_at": expires_at,
        "limits": {
            "requests_per_minute": API_RATE_LIMIT_PER_MINUTE,
            "active_downloads": API_MAX_ACTIVE_JOBS,
            "result_ttl_minutes": API_RESULT_TTL_MINUTES,
            "max_file_size_mb": API_MAX_FILE_SIZE_MB,
        },
    })


@app.post(
    f"{API_PREFIX}/downloads",
    status_code=202,
    summary="Start a download",
    tags=["downloads"],
    response_model=Download,
    responses={
        202: {"headers": {"Location": header_doc("URL of the job."), **RETRY_AFTER, **RATE_LIMIT}},
        413: error_doc("The request body is too large."),
        422: error_doc("The body is invalid or the link is not supported."),
        **COMMON_ERRORS,
    },
)
async def create_download(
    payload: DownloadRequest,
    identity: ApiKeyIdentity = Depends(require_api_key),
    jobs: JobManager = Depends(get_jobs),
):
    url = payload.url.strip()
    platform = detect_platform(url)
    if platform is None:
        raise ApiError(
            422, "unsupported_url",
            "Use an http(s) link to YouTube, TikTok, Instagram, Rutube or VK Video.",
        )
    try:
        job = jobs.submit(identity.id, url, platform, payload.type)
    except TooManyActiveJobsError:
        raise ApiError(
            429, "too_many_active_downloads",
            f"Up to {jobs.max_active_per_key} downloads can run at once for this key.",
            retry_after=POLL_INTERVAL_SECONDS,
        )
    try:
        await run_db(record_api_download, identity.id)
    except sqlite3.Error:
        logger.warning("Не удалось обновить счётчик загрузок API-ключа %s", identity.id)
    body = _job_payload(job, jobs)
    return JSONResponse(
        body,
        status_code=202,
        headers={
            "Location": body["links"]["self"],
            "Retry-After": str(POLL_INTERVAL_SECONDS),
        },
    )


@app.get(
    f"{API_PREFIX}/downloads",
    summary="Recent downloads of this key",
    tags=["downloads"],
    response_model=DownloadList,
    responses={200: {"headers": RATE_LIMIT}, **COMMON_ERRORS},
)
async def list_downloads(
    identity: ApiKeyIdentity = Depends(require_api_key),
    jobs: JobManager = Depends(get_jobs),
):
    return JSONResponse({"items": [_job_payload(job, jobs) for job in jobs.list(identity.id)]})


@app.get(
    f"{API_PREFIX}/downloads/{{job_id}}",
    summary="Download status",
    tags=["downloads"],
    response_model=Download,
    responses={200: {"headers": {**RETRY_AFTER, **RATE_LIMIT}}, **NOT_FOUND, **COMMON_ERRORS},
)
async def get_download(
    job_id: str,
    identity: ApiKeyIdentity = Depends(require_api_key),
    jobs: JobManager = Depends(get_jobs),
):
    job = _owned_job(jobs, identity, job_id)
    headers = {"Retry-After": str(POLL_INTERVAL_SECONDS)} if job.status in ACTIVE_STATUSES else None
    return JSONResponse(_job_payload(job, jobs), headers=headers)


@app.get(
    f"{API_PREFIX}/downloads/{{job_id}}/file",
    summary="Fetch the downloaded file",
    tags=["downloads"],
    response_class=FileResponse,
    responses={
        200: FILE_CONTENT,
        **NOT_FOUND,
        409: error_doc("The job is still active, failed, or was cancelled.", RETRY_AFTER),
        410: error_doc("The file has expired."),
        **COMMON_ERRORS,
    },
)
async def get_download_file(
    job_id: str,
    identity: ApiKeyIdentity = Depends(require_api_key),
    jobs: JobManager = Depends(get_jobs),
):
    job = _owned_job(jobs, identity, job_id)
    if job.status in ACTIVE_STATUSES:
        raise ApiError(409, "not_ready", "The download is not finished yet.", retry_after=POLL_INTERVAL_SECONDS)
    if job.status in {"failed", "cancelled"}:
        raise ApiError(409, "no_file", "The download did not produce a file.")
    file_path = job.file_path
    expires_at = jobs.result_expires_at(job)
    # Sweep идёт раз в минуту, поэтому срок сверяется и здесь.
    # У истёкшей задачи file_path уже None.
    if (
        file_path is None
        or (expires_at is not None and time.time() >= expires_at)
        or not file_path.is_file()
    ):
        raise ApiError(410, "file_expired", "The file has expired. Start a new download.")
    return FileResponse(
        file_path,
        media_type=job.content_type,
        headers={"Content-Disposition": _content_disposition(file_path.name)},
    )


@app.delete(
    f"{API_PREFIX}/downloads/{{job_id}}",
    status_code=204,
    summary="Cancel or delete a download",
    tags=["downloads"],
    responses={204: {"headers": RATE_LIMIT}, **NOT_FOUND, **COMMON_ERRORS},
)
async def delete_download(
    job_id: str,
    identity: ApiKeyIdentity = Depends(require_api_key),
    jobs: JobManager = Depends(get_jobs),
):
    # Удаление файла на 2 ГБ не должно останавливать event loop.
    if not await asyncio.to_thread(jobs.delete, identity.id, job_id):
        raise _download_not_found()
    return Response(status_code=204)


def run():
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=API_PORT)
