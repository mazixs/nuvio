"""
Shared utilities and configuration for yt-dlp downloaders.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import yt_dlp
from config import MAX_FILE_SIZE
from utils import download_report, work_budget
from utils.cancellation import cancellation_hook
from utils.logger import setup_logger
from utils.public_errors import youtube_error_code

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
    options["socket_timeout"] = work_budget.remaining(options["socket_timeout"])
    options.update(output_capture_opts(session_id))
    if session_id:
        hooks = list(options.get("progress_hooks") or [])
        hooks.append(cancellation_hook(session_id))
        options["progress_hooks"] = hooks


def execute_with_backoff(
    description: str, func: Callable[[], Path | str], max_attempts: int = 3
) -> Path | str:
    """Executes a downloader function with exponential backoff on network timeouts."""
    for attempt in range(1, max_attempts + 1):
        try:
            work_budget.check()
            return func()
        except yt_dlp.utils.DownloadError as e:
            message = str(e)
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
                delay = min(2**attempt, 30)
                logger.warning(
                    "%s: %s (attempt %s/%s). Retrying in %ss",
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
    """Файл превышает предел выбранного Telegram Bot API."""


def finalize_downloaded_file(downloaded_file: Path, force_local: bool) -> Path:
    """Возвращает локальный файл или удаляет его при превышении лимита."""
    file_size = downloaded_file.stat().st_size
    if force_local or file_size <= MAX_FILE_SIZE:
        return downloaded_file

    logger.warning(
        "Файл %s (%s байт) превышает лимит Telegram %s байт.",
        downloaded_file,
        file_size,
        MAX_FILE_SIZE,
    )
    downloaded_file.unlink(missing_ok=True)
    raise FileSizeLimitError(
        f"Файл превышает допустимый размер: {file_size} > {MAX_FILE_SIZE}"
    )
