"""Очередь загрузок HTTP API.

Задачи живут в памяти процесса API, как сессии бота: перезапуск их теряет, а
файлы прежних задач удаляются при старте. Каждая задача принадлежит одному
ключу, и чужой ключ её не видит — ни статуса, ни файла.

Диск ограничен четырьмя способами: число активных задач на ключ, срок хранения
готового файла, число хранимых результатов на ключ и общий объём результатов.
"""

import copy
import mimetypes
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from api import alerts
from config import (
    API_DOWNLOAD_WORKERS,
    API_MAX_ACTIVE_JOBS,
    API_MAX_FILE_SIZE_MB,
    API_RESULT_TTL_MINUTES,
    API_STORAGE_LIMIT_MB,
    BLOCKING_TASK_TIMEOUT,
    TEMP_DIR,
)
from utils import download_report, media_fetch, work_budget
from utils.cancellation import CancelledByUser, forget_cancellation, request_cancellation
from utils.logger import setup_logger
from utils.public_errors import classify_internal_error_category, make_error_code
from utils.temp_file_manager import cleanup_temp_files

logger = setup_logger(__name__)

# Каталоги задач API отличаются от каталогов бота, поэтому при общем TEMP_DIR
# чистка на старте не заденет загрузки бота.
SESSION_PREFIX = "api-"
STORED_RESULTS_PER_KEY = 20
# Записи завершённых задач без файла тоже копятся: быстро падающие ссылки в
# цикле дали бы ключу десятки тысяч записей за сутки, а их перебирают под
# общим замком и целиком отдают в списке.
JOB_RECORDS_PER_KEY = 100
JOB_RECORD_TTL_SECONDS = 24 * 60 * 60
ACTIVE_STATUSES = frozenset({"queued", "running"})
# Сколько поток может работать сверх срока шага, прежде чем считается зависшим.
# Срок проверяется только в хуках: постобработка FFmpeg внутри yt-dlp его не видит.
STUCK_GRACE_SECONDS = 300
_GENERIC_FAILURE = ("download_failed", "The media could not be downloaded.")

# Категории классификатора → код и текст для клиента. Внутренний текст
# исключения клиенту не уходит: только код и ссылка на запись в журнале.
_PUBLIC_FAILURES = {
    "DURATION": ("duration_limit", "The media is longer than the duration limit."),
    "LARGE": ("file_too_large", "The file exceeds the size limit."),
    "ACCESS": ("media_unavailable", "The platform refused access: the media may be private, removed or restricted."),
    "ACCESS_RESTRICTED": ("media_unavailable", "The platform restricted access to the media."),
    "UNAVAILABLE": ("media_unavailable", "The platform reported the media as unavailable."),
    "STORY_UNSUPPORTED": ("unsupported_media", "Instagram Stories are not supported."),
    "FORMAT_UNAVAILABLE": ("format_unavailable", "No suitable format is available."),
    "MEDIA_FORBIDDEN": ("media_forbidden", "The platform's media server refused the download."),
    "RATE_LIMIT": ("platform_rate_limited", "The platform is limiting requests. Try again later."),
    "NETWORK": ("network_error", "The server could not reach the platform."),
    "NETWORK_TIMEOUT": ("timeout", "The download took too long."),
    "TIMEOUT": ("timeout", "The download took too long."),
}


class TooManyActiveJobsError(Exception):
    """У ключа уже максимум задач в очереди и в работе."""


@dataclass
class Job:
    id: str
    key_id: int
    url: str
    platform: str
    media_type: str
    created_at: float = field(default_factory=time.time)
    status: str = "queued"
    started_at: float | None = None
    finished_at: float | None = None
    file_path: Path | None = None
    file_size: int | None = None
    error: dict | None = None
    # Клиент удалил задачу, а её поток ещё работает: запись скрыта от клиента,
    # но держит место в лимите активных задач, пока поток не остановится.
    deleted: bool = False
    # Срок текущего шага по `time.monotonic`; по нему сторож ищет зависшие потоки.
    deadline: float = float("inf")
    future: Future | None = field(default=None, repr=False)

    @property
    def session_id(self) -> str:
        return f"{SESSION_PREFIX}{self.id}"

    @property
    def content_type(self) -> str:
        if self.file_path is None:
            return "application/octet-stream"
        return mimetypes.guess_type(self.file_path.name)[0] or "application/octet-stream"


def describe_failure(platform: str, error: BaseException, session_id: str) -> tuple[dict, str | None]:
    """Безопасное описание отказа для клиента и категория для уведомления админов.

    Подробности уходят в журнал. Отказ, понятный без журнала, категории не
    получает: о нём админов не зовут.
    """
    if isinstance(error, media_fetch.UnsupportedMediaError):
        return {"code": "unsupported_media", "message": "Only a single video or audio track can be downloaded from this link."}, None
    if isinstance(error, media_fetch.NoSuitableFormatError):
        return {"code": "format_unavailable", "message": "No format fits the size limit."}, None
    category = classify_internal_error_category(platform, error)
    reference = make_error_code(platform, category)
    logger.error(
        "API_DL_FAIL ref=%s platform=%s category=%s type=%s error=%s tail=%s",
        reference, platform, category, type(error).__name__, error,
        download_report.output_tail(session_id)[-10:],
        exc_info=(type(error), error, error.__traceback__),
    )
    code, message = _PUBLIC_FAILURES.get(category, _GENERIC_FAILURE)
    return {"code": code, "message": message, "reference": reference}, category


def _expire(job: Job) -> None:
    """Результат больше не отдаётся; файл удаляет вызывающий код."""
    job.status = "expired"
    job.file_path = None


def _log_crash(future: Future) -> None:
    """Сбой самого обработчика задачи; задача к этому моменту уже завершена."""
    if not future.cancelled() and future.exception() is not None:
        logger.error("Сбой обработчика задачи API", exc_info=future.exception())


class JobManager:
    """Пул загрузок и реестр задач с проверкой владельца на каждом доступе."""

    def __init__(
        self,
        *,
        workers: int = API_DOWNLOAD_WORKERS,
        max_active_per_key: int = API_MAX_ACTIVE_JOBS,
        result_ttl_seconds: int = API_RESULT_TTL_MINUTES * 60,
        storage_limit_bytes: int = API_STORAGE_LIMIT_MB * 1024 * 1024,
        temp_dir: Path = TEMP_DIR,
    ):
        self.max_active_per_key = max_active_per_key
        self.result_ttl_seconds = result_ttl_seconds
        self.storage_limit_bytes = storage_limit_bytes
        self._temp_dir = temp_dir
        self._workers = workers
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="nuvio-api")
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._stuck_workers = 0

    def result_expires_at(self, job: Job) -> float | None:
        if job.status != "succeeded" or job.finished_at is None:
            return None
        return job.finished_at + self.result_ttl_seconds

    def submit(self, key_id: int, url: str, platform: str, media_type: str) -> Job:
        with self._lock:
            active = sum(
                1 for job in self._jobs.values()
                if job.key_id == key_id and job.status in ACTIVE_STATUSES
            )
            if active >= self.max_active_per_key:
                raise TooManyActiveJobsError()
            job = Job(id=uuid.uuid4().hex, key_id=key_id, url=url, platform=platform, media_type=media_type)
            self._jobs[job.id] = job
            job.future = self._executor.submit(self._run, job)
        job.future.add_done_callback(_log_crash)
        logger.info("API задача %s принята: ключ=%s платформа=%s тип=%s", job.id, key_id, platform, media_type)
        return job

    def get(self, key_id: int, job_id: str) -> Job | None:
        """Снимок задачи под замком. Чужая и несуществующая задача неотличимы.

        Поток задачи меняет поля по одному, и без снимка клиент мог бы увидеть
        `succeeded` без файла.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.key_id != key_id or job.deleted:
                return None
            return copy.copy(job)

    def list(self, key_id: int) -> list[Job]:
        with self._lock:
            jobs = [
                copy.copy(job) for job in self._jobs.values()
                if job.key_id == key_id and not job.deleted
            ]
        # Сортировка устойчива: при равном времени новее та, что добавлена позже.
        return sorted(jobs, key=lambda job: job.created_at)[::-1]

    def key_ids(self) -> set[int]:
        """Ключи, у которых есть задачи в памяти."""
        with self._lock:
            return {job.key_id for job in self._jobs.values()}

    def delete(self, key_id: int, job_id: str) -> bool:
        """Отменяет задачу и удаляет её файл.

        Работающая задача сразу пропадает для клиента, но держит место в лимите
        активных задач, пока её поток не остановится: иначе POST и DELETE в
        цикле заняли бы одним ключом все воркеры.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.key_id != key_id or job.deleted:
                return False
            previous = self._discard(job)
        self._release(job, previous)
        logger.info("API задача %s удалена клиентом (статус был %s)", job_id, previous)
        return True

    def drop_keys(self, key_ids: set[int]) -> None:
        """Снимает задачи отозванных и удалённых ключей вместе с файлами."""
        with self._lock:
            dropped = [
                job for job in self._jobs.values()
                if job.key_id in key_ids and not job.deleted
            ]
            released = [(job, self._discard(job)) for job in dropped]
        for job, previous in released:
            self._release(job, previous)
        if released:
            logger.info("Сняты задачи отозванных API-ключей: %s", len(released))

    def _discard(self, job: Job) -> str:
        """Скрывает задачу от клиента и возвращает её прежний статус. Под замком.

        Запись работающей задачи остаётся до конца её потока, остальные
        удаляются сразу.
        """
        previous = job.status
        job.deleted = True
        if previous == "running":
            # Под замком, чтобы `_finish` не успел забыть отмену раньше запроса.
            request_cancellation(job.session_id)
            return previous
        if previous == "queued":
            # `_run` увидит статус и выйдет, даже если поток уже взял задачу.
            job.status = "cancelled"
        del self._jobs[job.id]
        return previous

    @staticmethod
    def _release(job: Job, previous: str) -> None:
        """Освобождает ресурсы снятой задачи. Без замка."""
        if previous == "queued":
            if job.future is not None:
                job.future.cancel()
        elif previous != "running":
            # Файлы работающей задачи уберёт `_finish`, когда поток остановится.
            cleanup_temp_files(job.session_id)

    def _run(self, job: Job) -> None:
        with self._lock:
            if job.status != "queued":
                return
            job.status = "running"
            job.started_at = time.time()
        # Исход по умолчанию — отказ: `finally` завершает задачу при любом
        # исключении, иначе она осталась бы `running` навсегда и держала место
        # в лимите активных задач ключа.
        code, message = _GENERIC_FAILURE
        status, file_path, error = "failed", None, {"code": code, "message": message}
        category: str | None = None
        try:
            file_path = self._download(job)
            status, error = "succeeded", None
        except CancelledByUser:
            status, error = "cancelled", None
        except Exception as exc:
            error, category = describe_failure(job.platform, exc, job.session_id)
            failure = exc
        finally:
            self._finish(job, status, file_path=file_path, error=error)
        if category is not None:
            alerts.submit_download_failure(
                platform=job.platform, category=category,
                reference=error["reference"], url=job.url, error=failure,
            )

    def _download(self, job: Job) -> Path:
        session_id = job.session_id
        max_file_size = API_MAX_FILE_SIZE_MB * 1024 * 1024
        job.deadline = time.monotonic() + BLOCKING_TASK_TIMEOUT
        prepared = work_budget.execute(
            media_fetch.prepare_download,
            (job.url, job.platform, job.media_type, session_id),
            job.deadline,
            session_id,
            max_file_size,
        )
        job.deadline = time.monotonic() + prepared.timeout
        result = work_budget.execute(prepared.run, (), job.deadline, session_id, max_file_size)
        return self._checked_result(result, session_id)

    def _checked_result(self, result: Path | str | None, session_id: str) -> Path:
        """Отдаётся только файл из каталога этой задачи."""
        if not result:
            raise FileNotFoundError("Загрузчик не вернул файл")
        path = Path(result).resolve()
        if not path.is_file() or not path.is_relative_to((self._temp_dir / session_id).resolve()):
            raise FileNotFoundError("Файл загрузки не найден в каталоге задачи")
        return path

    def _finish(self, job: Job, status: str, *, file_path: Path | None = None, error: dict | None = None) -> None:
        session_id = job.session_id
        try:
            file_size = file_path.stat().st_size if file_path is not None else None
        except OSError:
            file_size = None
        evicted: list[str] = []
        with self._lock:
            deleted = job.deleted
            # Задачу уже завершил сторож, а поток вернулся позже: итог не меняется.
            overdue = job.status != "running"
            if overdue:
                self._stuck_workers -= 1
            if deleted:
                self._jobs.pop(job.id, None)
            elif not overdue:
                job.finished_at = time.time()
                job.file_path = file_path
                job.file_size = file_size
                job.error = error
                job.status = status
                if status == "succeeded":
                    evicted = self._evict_stored_results(job)
                evicted += self._prune_records(job.key_id)
        forget_cancellation(session_id)
        download_report.forget(session_id)
        if deleted or overdue or status != "succeeded":
            cleanup_temp_files(session_id)
        for evicted_session in evicted:
            cleanup_temp_files(evicted_session)
        logger.info("API задача %s завершена: %s", job.id, "deleted" if deleted else job.status if overdue else status)

    def _evict_stored_results(self, finished: Job) -> list[str]:
        """Сверх лимитов старые результаты истекают досрочно. Под замком.

        На ключ хранится не больше STORED_RESULTS_PER_KEY файлов, а у всех ключей
        вместе — не больше storage_limit_bytes. Только что готовый файл остаётся
        в любом случае.
        """
        stored = sorted(
            (job for job in self._jobs.values() if job.status == "succeeded"),
            key=lambda job: job.finished_at or 0,
        )
        own = [job for job in stored if job.key_id == finished.key_id]
        evicted = {job.id: job for job in own[:-STORED_RESULTS_PER_KEY]}
        total = sum(job.file_size or 0 for job in stored if job.id not in evicted)
        for job in stored:
            if total <= self.storage_limit_bytes:
                break
            if job is finished or job.id in evicted:
                continue
            evicted[job.id] = job
            total -= job.file_size or 0
        for job in evicted.values():
            _expire(job)
        return [job.session_id for job in evicted.values()]

    def _prune_records(self, key_id: int) -> list[str]:
        """Старые записи завершённых задач сверх JOB_RECORDS_PER_KEY. Под замком."""
        finished = sorted(
            (
                job for job in self._jobs.values()
                if job.key_id == key_id and job.status not in ACTIVE_STATUSES
            ),
            key=lambda job: job.finished_at or 0,
        )
        pruned = finished[:-JOB_RECORDS_PER_KEY]
        for job in pruned:
            del self._jobs[job.id]
        return [job.session_id for job in pruned]

    def fail_stuck_jobs(self, now: float | None = None) -> None:
        """Завершает для клиента задачи, чей поток не вернулся далеко за сроком.

        Живую загрузку это не задевает: после срока она и так падает на проверке
        `work_budget`. Прервать поток нельзя, поэтому файлы остаются до его
        возврата (`_finish`) или до перезапуска (`purge_orphans`). Если зависли все
        воркеры, новые задачи никогда не начнутся, и помогает только перезапуск.
        Нужен `os._exit`: при обычном выходе Python ждёт потоки пула.
        """
        now = time.monotonic() if now is None else now
        stuck = []
        with self._lock:
            for job in self._jobs.values():
                if job.status == "running" and now >= job.deadline + STUCK_GRACE_SECONDS:
                    code, message = _PUBLIC_FAILURES["TIMEOUT"]
                    job.status = "failed"
                    job.finished_at = time.time()
                    job.error = {"code": code, "message": message, "reference": make_error_code(job.platform, "STUCK")}
                    # Если поток оживёт, он остановится на ближайшей проверке.
                    request_cancellation(job.session_id)
                    stuck.append(job)
            self._stuck_workers += len(stuck)
            stuck_workers = self._stuck_workers
        for job in stuck:
            logger.error(
                "API_DL_FAIL ref=%s platform=%s category=STUCK tail=%s",
                job.error["reference"], job.platform, download_report.output_tail(job.session_id)[-10:],
            )
            alerts.submit_download_failure(
                platform=job.platform, category="STUCK", reference=job.error["reference"], url=job.url,
                error=TimeoutError(f"Поток загрузки не вернулся через {STUCK_GRACE_SECONDS} с после срока"),
            )
        if stuck_workers >= self._workers:
            logger.critical("Зависли все воркеры API (%s), процесс завершается", stuck_workers)
            try:
                alerts.notify_restart(stuck_workers)
            finally:
                os._exit(1)

    def sweep(self, now: float | None = None) -> None:
        """Удаляет просроченные файлы и давно завершённые записи задач."""
        now = time.time() if now is None else now
        expired = []
        with self._lock:
            for job in list(self._jobs.values()):
                expires_at = self.result_expires_at(job)
                if expires_at is not None and now >= expires_at:
                    _expire(job)
                    expired.append(job.session_id)
                if (
                    job.status not in ACTIVE_STATUSES
                    and now - (job.finished_at or job.created_at) >= JOB_RECORD_TTL_SECONDS
                ):
                    del self._jobs[job.id]
        for session_id in expired:
            cleanup_temp_files(session_id)

    def purge_orphans(self) -> None:
        """Убирает файлы задач, потерянных при прошлом перезапуске."""
        if not self._temp_dir.exists():
            return
        for item in self._temp_dir.iterdir():
            if item.name.startswith(SESSION_PREFIX) and item.is_dir():
                shutil.rmtree(item, ignore_errors=True)

    def shutdown(self) -> None:
        """Останавливает очередь и просит работающие загрузки прерваться."""
        with self._lock:
            running = [job.session_id for job in self._jobs.values() if job.status == "running"]
        for session_id in running:
            request_cancellation(session_id)
        self._executor.shutdown(wait=False, cancel_futures=True)
