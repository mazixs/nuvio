"""
Модуль для работы с Rutube и VK Video с использованием yt-dlp.
"""

import re
from pathlib import Path
from typing import Any

import httpx
import yt_dlp
from config import MAX_FILE_SIZE, MAX_VIDEO_DURATION
from utils.download_report import record_delivered_format
from utils.logger import setup_logger
from utils.media_errors import DurationLimitError
from utils.media_processor import ensure_ios_compatible_video
from utils.temp_file_manager import get_temp_file_path
from utils.ytdlp_common import (
    FileSizeLimitError,
    apply_network_opts,
    execute_with_backoff,
    finalize_downloaded_file,
)

logger = setup_logger(__name__)

RUTUBE_URL_PATTERN = (
    r"(?:https?:\/\/)?(?:www\.)?(?:rutube\.ru|ru\.tube|rutu\.be)"
    r"\/(?:video|(?:embed|play)\/|shorts\/|y\/|a\/)?[a-zA-Z0-9_-]+"
)
VK_URL_PATTERN = (
    r"(?:https?:\/\/)?(?:www\.)?(?:vk\.com|vk\.ru|vkvideo\.ru|m\.vk\.com)"
    r"\/(?:video|clip|@[\w.]+|wall-?\d+_\d+|live-?\d+_\d+)(?:[?#\/].*)?"
)
VK_LIVE_URL_PATTERN = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:vk\.com|vk\.ru|vkvideo\.ru)/"
    r"live(?P<id>-\d+_\d+)(?:[/?#].*)?$",
    re.IGNORECASE,
)


class VkLiveActiveError(ValueError):
    """Текущая трансляция еще не стала конечным файлом."""


def is_valid_rutube_url(url: str) -> bool:
    return bool(re.match(RUTUBE_URL_PATTERN, url))


def is_valid_vk_url(url: str) -> bool:
    return bool(re.match(VK_URL_PATTERN, url))


def normalize_vk_url(url: str) -> str:
    """Переводит новый адрес архива трансляции в понятный yt-dlp адрес видео."""
    match = VK_LIVE_URL_PATTERN.match(url)
    return f"https://vk.com/video{match['id']}" if match else url


def _get_info(
    url: str, platform: str, *, enforce_duration: bool = True,
    session_id: str | None = None,
) -> dict[str, Any]:
    logger.info(f"Получение информации о {platform} видео: {url}")
    ydl_opts = {
        "quiet": True,
        "skip_download": True,
    }
    apply_network_opts(ydl_opts, session_id)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        duration = info.get("duration")
        if enforce_duration and duration and duration > MAX_VIDEO_DURATION:
            logger.warning(f"Видео слишком длинное: {duration} секунд")
            raise DurationLimitError(duration, MAX_VIDEO_DURATION)
        logger.info(f"Информация о {platform} видео успешно получена.")
        return info


def get_rutube_info(url: str, session_id: str | None = None) -> dict[str, Any]:
    return _get_info(url, "Rutube", session_id=session_id)


def get_vk_info(url: str, session_id: str | None = None) -> dict[str, Any]:
    info = _get_info(
        normalize_vk_url(url), "VK", enforce_duration=False, session_id=session_id
    )
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
        raise VkLiveActiveError(
            "Активная трансляция VK не имеет конечного файла для отправки."
        )

    selected = _select_vk_direct_format(info)
    if selected:
        format_id, size, height = selected
        info["_nuvio_vk_format_id"] = format_id
        info["_nuvio_vk_format_size"] = size
        info["_nuvio_vk_format_height"] = height
    elif (info.get("duration") or 0) > MAX_VIDEO_DURATION:
        raise FileSizeLimitError(
            "Для длинной записи VK не найден прямой файл, помещающийся в лимит Telegram."
        )
    return info


def _vk_direct_size(client: httpx.Client, fmt: dict[str, Any]) -> int | None:
    """Читает размер из HEAD или заголовка короткого Range-запроса."""
    headers = fmt.get("http_headers") or {}
    try:
        response = client.head(fmt["url"], headers=headers)
        if response.status_code == 200:
            size = int(response.headers.get("content-length") or 0)
            if size > 0:
                return size
    except httpx.TimeoutException:
        return None
    except (httpx.HTTPError, ValueError):
        pass

    # stream закрывается после заголовков: тело файла не скачивается, даже если
    # сервер проигнорировал Range и ответил обычным 200.
    try:
        with client.stream(
            "GET", fmt["url"], headers={**headers, "Range": "bytes=0-0"}
        ) as response:
            if response.status_code == 206:
                match = re.search(
                    r"/(\d+)$", response.headers.get("content-range") or ""
                )
                return int(match.group(1)) if match else None
            if response.status_code == 200:
                size = int(response.headers.get("content-length") or 0)
                return size or None
    except (httpx.HTTPError, ValueError):
        pass
    return None


def _select_vk_direct_format(info: dict[str, Any]) -> tuple[str, int | None, int] | None:
    """Выбирает наиболее четкий прямой MP4, пригодный для проверки лимита.

    Если CDN не сообщает размер, выбирается самый маленький прямой формат.
    Его загрузка останавливается при превышении лимита Telegram.
    """
    candidates = sorted(
        (
            fmt for fmt in info.get("formats", [])
            if str(fmt.get("format_id") or "").startswith("url")
            and fmt.get("protocol") == "https"
            and fmt.get("ext") == "mp4"
            and fmt.get("url")
            and fmt.get("height")
        ),
        key=lambda fmt: int(fmt["height"]),
        reverse=True,
    )
    if not candidates:
        return None

    limit = int(MAX_FILE_SIZE * 0.95)
    unknown: dict[str, Any] | None = None
    with httpx.Client(follow_redirects=True, timeout=8) as client:
        for fmt in candidates:
            size = fmt.get("filesize")
            if not size:
                size = _vk_direct_size(client, fmt)
            if size and int(size) <= limit:
                return str(fmt["format_id"]), int(size), int(fmt["height"])
            if not size:
                unknown = fmt
    if unknown:
        return str(unknown["format_id"]), None, int(unknown["height"])
    return None


def _get_available_formats(
    video_info: dict[str, Any], filter_by_size: bool = True
) -> dict[str, list[dict[str, Any]]]:
    formats = video_info.get("formats", [])
    video_formats: list[dict[str, Any]] = []
    audio_formats: list[dict[str, Any]] = []
    combined_formats: list[dict[str, Any]] = []

    for format_info in formats:
        filesize = format_info.get("filesize") or format_info.get("filesize_approx")
        if not format_info.get("height") and not format_info.get("audio_channels"):
            continue
        if filter_by_size and filesize and filesize > MAX_FILE_SIZE:
            continue

        format_id = format_info.get("format_id")
        if format_info.get("vcodec") != "none" and format_info.get("acodec") == "none":
            video_formats.append(
                {
                    "format_id": format_id,
                    "format": format_info.get("format"),
                    "ext": format_info.get("ext"),
                    "height": format_info.get("height"),
                    "width": format_info.get("width"),
                    "filesize": filesize,
                    "type": "video_only",
                }
            )
        elif (
            format_info.get("vcodec") == "none" and format_info.get("acodec") != "none"
        ):
            audio_formats.append(
                {
                    "format_id": format_id,
                    "format": format_info.get("format"),
                    "ext": format_info.get("ext"),
                    "filesize": filesize,
                    "type": "audio_only",
                }
            )
        elif (
            format_info.get("vcodec") != "none" and format_info.get("acodec") != "none"
        ):
            combined_formats.append(
                {
                    "format_id": format_id,
                    "format": format_info.get("format"),
                    "ext": format_info.get("ext"),
                    "height": format_info.get("height"),
                    "width": format_info.get("width"),
                    "filesize": filesize,
                    "type": "combined",
                }
            )

    video_formats.sort(key=lambda x: x.get("height", 0), reverse=True)
    audio_formats.sort(key=lambda x: x.get("filesize", 0) or 0, reverse=True)
    combined_formats.sort(key=lambda x: x.get("height", 0), reverse=True)

    logger.info(
        f"Найдено форматов: video_only={len(video_formats)}, "
        f"audio_only={len(audio_formats)}, combined={len(combined_formats)}"
    )
    return {
        "video_only": video_formats,
        "audio_only": audio_formats,
        "combined": combined_formats,
    }


def get_available_formats_rutube(
    video_info: dict[str, Any], filter_by_size: bool = True
) -> dict[str, list[dict[str, Any]]]:
    return _get_available_formats(video_info, filter_by_size)


def get_available_formats_vk(
    video_info: dict[str, Any], filter_by_size: bool = True
) -> dict[str, list[dict[str, Any]]]:
    return _get_available_formats(video_info, filter_by_size)


def _resolve_output_template(session_id: str, output_dir: Path | None) -> Path:
    if output_dir is None:
        return get_temp_file_path(session_id, "%(title)s.%(ext)s")
    return output_dir / "%(title)s.%(ext)s"


def _ensure_ios_compatible(downloaded_file: Path, session_id: str) -> Path:
    """Проверяет кодек готового файла и перекодирует непригодный (ADR-002).

    Проверки расширения `.webm`, стоявшей здесь раньше, недостаточно: кодек
    определяется потоком, а не именем файла.
    """
    return ensure_ios_compatible_video(downloaded_file, session_id, "rutube_vk")


def _download_video(
    url: str,
    session_id: str,
    output_dir: Path | None = None,
    force_local: bool = False,
    format_selector: str = "bestvideo+bestaudio/best",
) -> Path | str:
    output_path_template = _resolve_output_template(session_id, output_dir)

    def _download() -> Path | str:
        ydl_opts = {
            "format": format_selector,
            "outtmpl": str(output_path_template),
            "quiet": False,
            "merge_output_format": "mp4",
            "progress_hooks": [
                lambda d: logger.debug(
                    f"Скачивание: {d['status']} - {d.get('_percent_str', '0%')}"
                )
            ],
        }
        apply_network_opts(ydl_opts, session_id=session_id)
        if format_selector.startswith("url"):
            # У прямого MP4 есть Range: повтор после сетевого сбоя продолжит
            # .part вместо повторной загрузки гигабайтного файла с нуля.
            ydl_opts["continuedl"] = True
            ydl_opts["max_filesize"] = MAX_FILE_SIZE

            def _stop_oversize(progress: dict[str, Any]) -> None:
                if (progress.get("downloaded_bytes") or 0) > MAX_FILE_SIZE:
                    raise FileSizeLimitError("Прямой файл VK превышает лимит Telegram")

            ydl_opts["progress_hooks"].append(_stop_oversize)
        logger.info(
            f"Скачивание {url} через yt-dlp без cookies, формат: {format_selector}"
        )
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            downloaded_file = Path(ydl.prepare_filename(info))
            if not downloaded_file.exists():
                raise Exception("Файл не был загружен.")
            logger.info(f"Видео успешно скачано: {downloaded_file}")
            record_delivered_format(session_id, info.get("format_id"))
            downloaded_file = _ensure_ios_compatible(downloaded_file, session_id)
            return finalize_downloaded_file(downloaded_file, force_local)

    return execute_with_backoff(f"Скачивание видео {url}", _download)


def download_rutube_video(
    url: str,
    session_id: str,
    output_dir: Path | None = None,
    force_local: bool = False,
) -> Path | str:
    logger.info(f"Скачивание Rutube видео: {url}")
    return _download_video(
        url,
        session_id,
        output_dir,
        force_local,
        format_selector="bestvideo+bestaudio/best",
    )


def download_vk_video(
    url: str,
    session_id: str,
    output_dir: Path | None = None,
    force_local: bool = False,
    format_id: str | None = None,
) -> Path | str:
    logger.info(f"Скачивание VK видео: {url}")
    return _download_video(
        normalize_vk_url(url),
        session_id,
        output_dir,
        force_local,
        format_selector=format_id or "best[protocol=https]/best",
    )


def _download_audio(
    url: str,
    session_id: str,
    output_dir: Path | None = None,
    force_local: bool = False,
    preferred_codec: str = "mp3",
    format_selector: str = "bestaudio/best",
) -> Path | str:
    output_path_template = _resolve_output_template(session_id, output_dir)

    def _download() -> Path | str:
        ydl_opts = {
            "format": format_selector,
            "outtmpl": str(output_path_template),
            "quiet": False,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": preferred_codec,
                    "preferredquality": "192",
                }
            ],
            "progress_hooks": [
                lambda d: logger.debug(
                    f"Скачивание аудио: {d['status']} - {d.get('_percent_str', '0%')}"
                )
            ],
        }
        apply_network_opts(ydl_opts, session_id=session_id)
        logger.info(
            f"Скачивание аудио {url} через yt-dlp без cookies, формат: {format_selector}"
        )
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            base_filename = Path(ydl.prepare_filename(info))
            downloaded_file = base_filename.with_suffix(f".{preferred_codec}")
            if not downloaded_file.exists():
                raise Exception("Аудио файл не был создан после postprocessing.")
            logger.info(f"Аудио успешно извлечено: {downloaded_file}")
            record_delivered_format(session_id, info.get("format_id"))
            return finalize_downloaded_file(downloaded_file, force_local)

    return execute_with_backoff(f"Скачивание аудио {url}", _download)


def download_rutube_audio(
    url: str,
    session_id: str,
    output_dir: Path | None = None,
    force_local: bool = False,
) -> Path | str:
    logger.info(f"Скачивание Rutube аудио: {url}")
    return _download_audio(
        url, session_id, output_dir, force_local, format_selector="bestaudio/best"
    )


def download_vk_audio(
    url: str,
    session_id: str,
    output_dir: Path | None = None,
    force_local: bool = False,
) -> Path | str:
    logger.info(f"Скачивание VK аудио: {url}")
    return _download_audio(
        normalize_vk_url(url),
        session_id,
        output_dir,
        force_local,
        format_selector="bestaudio[protocol=https]/bestaudio/best",
    )
