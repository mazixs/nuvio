"""
Shared utilities and configuration for yt-dlp downloaders.
"""

import random
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yt_dlp
from config import MAX_FILE_SIZE
from utils import download_report, work_budget
from utils.cancellation import cancellation_hook
from utils.logger import setup_logger
from utils.public_errors import is_connectivity_failure, youtube_error_code

logger = setup_logger(__name__)

DEFAULT_YTDLP_NETWORK_OPTS: dict[str, Any] = {
    "retries": 5,
    "socket_timeout": 40,
    "http_chunk_size": 10_485_760,  # 10 MB
    "fragment_retries": 5,
    "skip_unavailable_fragments": False,
    "abort_on_unavailable_fragments": True,
    "concurrent_fragment_downloads": 4,
    "continuedl": False,
    "noplaylist": True,
}

# Значение yt-dlp для настроек, где `socket_timeout` не задан.
_YTDLP_SOCKET_TIMEOUT = 20

# Название режется по байтам. Имя файла на ext4 ограничено 255 байтами, а символ
# кириллицы занимает 2 байта и CJK — 3: длинное название вместе с `.f140.m4a.part`
# давало «File name too long».
OUTPUT_FILENAME_TEMPLATE = "%(title).150B.%(ext)s"

# Префикс строк прогресса. yt-dlp гонит их тем же каналом, что и полезный вывод,
# по несколько штук в секунду, а многострочный прогресс ещё и нумерует строки
# («1: [download] ...»), поэтому номер снимается перед сравнением.
_PROGRESS_LINE_PREFIX = "[download]"


def is_progress_line(line: str) -> bool:
    """Определяет, что строка вывода — это счётчик прогресса, а не сообщение."""
    text = line.strip()
    if any(marker in text.lower() for marker in ("skipping fragment", "fragment not found")):
        return False
    number, separator, rest = text.partition(": ")
    if separator and number.isdigit():
        text = rest.lstrip()
    return text.startswith(_PROGRESS_LINE_PREFIX)


class YtdlpOutputLogger:
    """Приёмник вывода yt-dlp: в лог целиком, в отчёт — без прогресса.

    Без логгера yt-dlp пишет предупреждения в stderr, а `no_warnings` глушит их
    вовсе — именно так 18.08.2026 потерялись объяснения поломки YouTube («cookies
    are no longer valid», «formats require a GVS PO Token», «forcing SABR
    streaming»), и диагноз приходилось добывать по SSH. С логгером тот же текст
    попадает и в админский лог, и в хвост краш-репорта по сессии.

    Строки прогресса в отчёт не идут: их десятки в секунду, и кольцо на 60 строк
    они вытеснят целиком. Ни один метод не имеет права бросить исключение —
    сломанная диагностика не должна ронять загрузку.
    """

    __slots__ = ("session_id",)

    def __init__(self, session_id: str | None = None) -> None:
        self.session_id = session_id

    def debug(self, msg: object) -> None:
        self._keep(msg, logger.debug)

    def warning(self, msg: object) -> None:
        self._keep(msg, logger.warning)

    def error(self, msg: object) -> None:
        self._keep(msg, logger.error)

    def _keep(self, msg: object, log: Callable[..., None]) -> None:
        try:
            text = msg if isinstance(msg, str) else str(msg)
            if not is_progress_line(text):
                download_report.record_output(self.session_id, text)
            log("yt-dlp: %s", text)
        except Exception:
            # Молча: попытка пожаловаться на сломанную диагностику пойдёт тем же
            # путём и упадёт так же.
            pass


def output_capture_opts(session_id: str | None = None) -> dict[str, Any]:
    """Опции yt-dlp, которыми его вывод перестаёт пропадать втуне.

    `no_warnings: False` идёт вместе с логгером намеренно: сейчас yt-dlp смотрит
    на логгер раньше флага, но флаг остаётся глушилкой для тех путей и версий,
    где порядок обратный, а держать его включённым всё равно незачем.
    """
    return {"logger": YtdlpOutputLogger(session_id), "no_warnings": False}


def classify_download_error_kind(message: str) -> str:
    """Использует общую классификацию YouTube для ответов, логов и повторов."""
    category = youtube_error_code(message)
    return "DOWNLOAD" if category == "UNEXPECT" else category


def apply_network_opts(options: dict[str, Any], session_id: str | None = None) -> None:
    """Applies default network options to yt-dlp config dict.

    Args:
        session_id: сессия, чья отмена должна прерывать загрузку, и она же —
            адрес хвоста вывода в отчёте. Без него хук отмены не ставится:
            разбор информации о видео идёт до появления сессии и отменять там
            нечего, а вывод копится под общим ключом.
    """
    work_budget.check()
    options.update(DEFAULT_YTDLP_NETWORK_OPTS)
    options.update(output_capture_opts(session_id))
    apply_work_limits(options, session_id)


def apply_work_limits(options: dict[str, Any], session_id: str | None = None) -> None:
    """Подчиняет yt-dlp отмене, сроку и явному лимиту размера текущей работы.

    Остальные сетевые настройки не трогает: TikTok и Instagram работают без них.
    Явный лимит есть только у задач HTTP API. Бот проверяет размер готового файла,
    потому что файл может ужаться при перекодировании.
    """
    options["socket_timeout"] = work_budget.remaining(
        options.get("socket_timeout", _YTDLP_SOCKET_TIMEOUT)
    )
    hooks = list(options.get("progress_hooks") or [])
    if session_id:
        hooks.append(cancellation_hook(session_id))
    limit = work_budget.job_size_limit()
    if limit is not None:
        hooks.append(size_limit_hook(limit))
    if hooks:
        options["progress_hooks"] = hooks


def size_limit_hook(limit: int) -> Callable[[dict[str, Any]], None]:
    """Progress hook, останавливающий загрузку, когда её файлы вместе превысили лимит.

    `max_filesize` yt-dlp не действует на HLS и DASH. Лимит передаётся явно, а счётчик
    под замком: хук вызывают и потоки фрагментов, у которых нет контекста задачи.
    """
    lock = threading.Lock()
    downloaded: dict[str | None, int] = {}

    def _hook(status: dict[str, Any]) -> None:
        with lock:
            downloaded[status.get("filename")] = status.get("downloaded_bytes") or 0
            total = sum(downloaded.values())
        if total > limit:
            raise FileSizeLimitError(f"Загрузка превысила лимит: {total} > {limit}")

    return _hook


def execute_with_backoff(
    description: str, func: Callable[[], Path | str], max_attempts: int = 3
) -> Path | str:
    """Executes a downloader function with exponential backoff on network timeouts.

    Пауза — full jitter: случайная в пределах экспоненты. Одновременно упавшие
    загрузки не возвращаются к площадке одной волной.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            work_budget.check()
            return func()
        except yt_dlp.utils.DownloadError as e:
            message = str(e)
            if is_connectivity_failure(e):
                # yt-dlp уже повторил запрос сам. DNS и маршрут за секунды
                # паузы не вернутся, а внешний повтор лишь умножает ожидание.
                logger.warning(
                    "%s: network unavailable, not retrying: %s", description, message
                )
                raise
            error_kind = classify_download_error_kind(message)
            # MEDIA_FORBIDDEN повторяется наравне с таймаутом: каждая попытка
            # заново разбирает ссылку и получает свежую подпись, а именно этого
            # 403 на медиафайле и требует.
            if error_kind in {"NETWORK", "NETWORK_TIMEOUT", "MEDIA_FORBIDDEN"}:
                if attempt == max_attempts:
                    logger.error(
                        "%s failed after %s attempts (%s): %s",
                        description,
                        attempt,
                        error_kind,
                        message,
                        exc_info=True,
                    )
                    raise
                delay = random.uniform(0, min(2**attempt, 30))
                logger.warning(
                    "%s: %s (attempt %s/%s). Retrying in %.1fs",
                    description,
                    error_kind,
                    attempt,
                    max_attempts,
                    delay,
                )
                work_budget.pause(delay)
                continue
            if error_kind in {
                "FORMAT_UNAVAILABLE", "ACCESS_RESTRICTED", "RATE_LIMIT", "UNAVAILABLE",
            }:
                logger.warning(
                    "%s: expected yt-dlp error (%s): %s",
                    description,
                    error_kind,
                    message,
                )
            else:
                logger.error(
                    "%s: download error: %s", description, message, exc_info=True
                )
            raise


class FileSizeLimitError(Exception):
    """Файл превышает лимит размера: Telegram Bot API у бота, свой у HTTP API."""


def finalize_downloaded_file(downloaded_file: Path, force_local: bool) -> Path:
    """Возвращает локальный файл или удаляет его при превышении лимита."""
    file_size = downloaded_file.stat().st_size
    size_limit = work_budget.size_limit(MAX_FILE_SIZE)
    if force_local or file_size <= size_limit:
        return downloaded_file

    logger.warning(
        "Файл %s (%s байт) превышает лимит %s байт.",
        downloaded_file,
        file_size,
        size_limit,
    )
    downloaded_file.unlink(missing_ok=True)
    raise FileSizeLimitError(
        f"Файл превышает допустимый размер: {file_size} > {size_limit}"
    )
