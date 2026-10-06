"""
Модуль для работы с YouTube с использованием yt-dlp.
"""

import re
import sys
from pathlib import Path
from typing import Any, TypedDict, NotRequired

import yt_dlp
from config import (
    MAX_FILE_SIZE,
    MAX_VIDEO_DURATION,
    YOUTUBE_COOKIES_FILE,
    YTDLP_CLI_FALLBACK,
)
from utils.cookie_workfile import working_cookie_file
from utils.cancellation import CancelledByUser, is_cancelled
from utils.download_report import record_delivered_format
from utils.logger import setup_logger
from utils.media_errors import DurationLimitError
from utils.temp_file_manager import get_temp_file_path
from utils.media_processor import ensure_ios_compatible_video
from utils.ytdlp_runtime import extract_cli_output_path, run_yt_dlp_cli
from utils.subtitles import srt_to_text
from utils.ytdlp_common import (
    DEFAULT_YTDLP_NETWORK_OPTS,
    apply_network_opts,
    classify_download_error_kind,
    execute_with_backoff,
    finalize_downloaded_file,
)

logger = setup_logger(__name__)

# Регулярное выражение для проверки YouTube URL (включая Shorts)
YOUTUBE_URL_PATTERN = r"(?:https?:\/\/)?(?:www\.)?(?:youtube\.com|youtu\.be)\/(?:watch\?v=|shorts\/)?([a-zA-Z0-9_-]{11})"

# Форматы, которые yt-dlp показывает, а YouTube отдавать отказывается без
# GVS PO-токена. Прогрессивный itag 18 yt-dlp освобождал от проверки токена, и
# замерено: без токена он давал `HTTP Error 403` на первом байте всегда, на любом
# видео и с любого адреса. Клиент `visionos` его больше не отдаёт, но проверка
# остаётся страховкой на случай смены клиента — DASH-пара «видео + аудио» даёт то
# же разрешение и скачивается.
PO_TOKEN_ONLY_FORMAT_IDS = frozenset({"18"})


class FormatInfoDict(TypedDict, total=False):
    format_id: str
    format: str
    ext: str
    height: NotRequired[int]
    width: NotRequired[int]
    filesize: NotRequired[int]
    audio_channels: NotRequired[int]
    vcodec: NotRequired[str]
    acodec: NotRequired[str]
    language: NotRequired[str]
    language_preference: NotRequired[int]
    format_note: NotRequired[str]
    type: NotRequired[str]


def is_valid_youtube_url(url: str) -> bool:
    """
    Проверяет, является ли URL действительной ссылкой на YouTube видео.

    Args:
        url (str): URL для проверки.

    Returns:
        bool: True, если URL является допустимой ссылкой на YouTube, иначе False.
    """
    return bool(re.match(YOUTUBE_URL_PATTERN, url))


def get_video_info(url: str, session_id: str | None = None) -> dict[str, Any]:
    """
    Получает информацию о видео.
    Сначала пробует без cookies, при ошибке — повторяет с cookies (если файл есть).
    """
    logger.info(f"Получение информации о видео: {url}")

    def _get_info(use_cookies: bool) -> dict[str, Any]:
        ydl_opts = {
            "quiet": True,
            "skip_download": True,
        }
        if session_id and is_cancelled(session_id):
            raise CancelledByUser(f"разбор сессии {session_id} отменен")
        apply_network_opts(ydl_opts, session_id)
        cookiefile = _cookiefile_if_available(use_cookies)
        if cookiefile:
            logger.info(f"Использование файла cookies: {cookiefile}")
            ydl_opts["cookiefile"] = cookiefile
        elif use_cookies:
            logger.warning(
                f"Файл cookies указан ({YOUTUBE_COOKIES_FILE}), но не найден. Запрос будет выполнен без cookies."
            )
        else:
            logger.info("Пробуем получить информацию о видео без cookies.")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if session_id and is_cancelled(session_id):
                raise CancelledByUser(f"разбор сессии {session_id} отменен")
            duration = info.get("duration")
            if duration and duration > MAX_VIDEO_DURATION:
                logger.warning(f"Видео слишком длинное: {duration} секунд")
                raise DurationLimitError(duration, MAX_VIDEO_DURATION)
            logger.info("Информация о видео успешно получена.")
            return info

    # Сначала без cookies. Авторизованная сессия переводит yt-dlp на клиентов
    # `tv_downgraded` и `web_safari`, которым нужен PO-токен, — без токена и без
    # JS-рантайма форматов не остаётся вовсе. Клиент `android_vr`, работающий без
    # токена, yt-dlp вычёркивает при наличии auth-cookies, потому что тот cookies
    # не поддерживает. Cookies нужны только на возрастные ограничения и приватные
    # видео, поэтому уходят во вторую попытку — как в загрузчиках аудио.
    have_cookies = bool(YOUTUBE_COOKIES_FILE and Path(YOUTUBE_COOKIES_FILE).is_file())
    try:
        return _get_info(False)
    except (yt_dlp.utils.DownloadError, yt_dlp.cookies.CookieLoadError) as e:
        if not have_cookies:
            logger.error(
                f"Ошибка при получении информации о видео без cookies: {e}",
                exc_info=True,
            )
            raise
        logger.warning(f"Ошибка без cookies: {e}. Пробуем с cookies как fallback...")

    try:
        return _get_info(True)
    except DurationLimitError:
        raise
    except Exception as e:
        logger.error(
            f"Ошибка при получении информации о видео даже с cookies: {e}",
            exc_info=True,
        )
        raise


def _shadowed_drc_format_ids(formats: list[dict[str, Any]]) -> frozenset[str]:
    """Находит `-drc`-дорожки, у которых есть тот же поток без сжатия динамики.

    YouTube отдаёт звук парами: `140` и `140-drc`, где второй прошёл выравнивание
    громкости. Размер, расширение и кодек у них совпадают, поэтому в меню
    получаются две неразличимые кнопки «M4A · 107 МБ», а подбор пары по размеру
    может взять сжатую дорожку вместо оригинала. Оригинал ближе к исходнику, так
    что двойник убирается — но только когда оригинал действительно есть.
    """
    available = {
        str(fmt.get("format_id")) for fmt in formats if fmt.get("format_id") is not None
    }
    return frozenset(
        format_id
        for format_id in available
        if format_id.endswith("-drc") and format_id.removesuffix("-drc") in available
    )


def get_available_formats(
    video_info: dict[str, Any], filter_by_size: bool = True
) -> dict[str, list[FormatInfoDict]]:
    """
    Получает список доступных форматов видео с опциональной фильтрацией по размеру.

    Args:
        video_info (Dict[str, Any]): Информация о видео.
        filter_by_size (bool): Фильтровать форматы по MAX_FILE_SIZE (по умолчанию True).

    Returns:
        Dict[str, List[Dict[str, Any]]]: Словарь с группами форматов.
    """
    formats = video_info.get("formats", [])
    video_formats: list[FormatInfoDict] = []
    audio_formats: list[FormatInfoDict] = []
    combined_formats: list[FormatInfoDict] = []
    shadowed_drc_ids = _shadowed_drc_format_ids(formats)

    for format_info in formats:
        # Логируем, что получили по filesize
        filesize = format_info.get("filesize") or format_info.get("filesize_approx")
        logger.debug(
            f"FormatID={format_info.get('format_id')}, ext={format_info.get('ext')}, height={format_info.get('height')}, filesize={filesize}"
        )

        is_audio_only = (
            format_info.get("vcodec") == "none"
            and format_info.get("acodec") not in (None, "none")
        )
        if (
            not format_info.get("height")
            and not format_info.get("audio_channels")
            and not is_audio_only
        ):
            continue

        # Применяем фильтрацию по размеру файла (если включена)
        if filter_by_size and filesize:
            if filesize > MAX_FILE_SIZE:
                logger.debug(
                    f"Формат {format_info.get('format_id')} пропущен: размер {filesize} превышает {MAX_FILE_SIZE}"
                )
                continue

        format_id = format_info.get("format_id")

        if format_id in PO_TOKEN_ONLY_FORMAT_IDS:
            logger.debug(
                "Формат %s пропущен: YouTube отдаёт его только по PO-токену", format_id
            )
            continue

        if format_id in shadowed_drc_ids:
            logger.debug(
                "Формат %s пропущен: есть тот же поток без сжатия динамики", format_id
            )
            continue

        if format_info.get("vcodec") != "none" and format_info.get("acodec") == "none":
            video_formats.append(
                {
                    "format_id": format_id,
                    "format": format_info.get("format"),
                    "ext": format_info.get("ext"),
                    "height": format_info.get("height"),
                    "width": format_info.get("width"),
                    "filesize": filesize,
                    # Кодек нужен выбору формата для Telegram: H.264 и AAC
                    # проигрываются везде, остальное — на свой риск (ADR-001).
                    "vcodec": format_info.get("vcodec"),
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
                    "acodec": format_info.get("acodec"),
                    "language": format_info.get("language"),
                    "language_preference": format_info.get("language_preference"),
                    "format_note": format_info.get("format_note"),
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
                    "vcodec": format_info.get("vcodec"),
                    "acodec": format_info.get("acodec"),
                    "language": format_info.get("language"),
                    "language_preference": format_info.get("language_preference"),
                    "format_note": format_info.get("format_note"),
                    "type": "combined",
                }
            )

    video_formats.sort(key=lambda x: x.get("height", 0), reverse=True)
    audio_formats.sort(key=lambda x: x.get("filesize", 0) or 0, reverse=True)
    combined_formats.sort(key=lambda x: x.get("height", 0), reverse=True)

    logger.info(
        f"Найдено форматов: video_only={len(video_formats)}, audio_only={len(audio_formats)}, combined={len(combined_formats)}"
    )

    return {
        "video_only": video_formats,
        "audio_only": audio_formats,
        "combined": combined_formats,
    }


def _ensure_ios_compatible(downloaded_file: Path, session_id: str) -> Path:
    """Проверяет кодек готового файла и перекодирует непригодный.

    Стоявшая здесь раньше проверка расширения `.webm` не срабатывала никогда:
    `merge_output_format: "mp4"` заставляет yt-dlp класть VP9 и AV1 в MP4, и
    файл приезжал с расширением `.mp4`. Смотреть надо на сам поток — ADR-002.
    """
    return ensure_ios_compatible_video(downloaded_file, session_id, "youtube")


def _resolve_output_template(session_id: str, output_dir: Path | None) -> Path:
    """Возвращает шаблон пути для yt-dlp."""
    if output_dir is None:
        return get_temp_file_path(session_id, "%(title)s.%(ext)s")
    return output_dir / "%(title)s.%(ext)s"


def _cookiefile_if_available(use_cookies: bool) -> str | None:
    """Возвращает путь к cookies, если он реально доступен.

    Отдаётся рабочая копия: yt-dlp перезаписывает переданный файл и теряет при
    этом сессионные cookies, а загруженный админом набор должен остаться целым.
    """
    if not use_cookies:
        return None
    working = working_cookie_file(YOUTUBE_COOKIES_FILE)
    return str(working) if working else None


def _build_cli_download_command(
    *,
    url: str,
    output_path_template: Path,
    format_selector: str,
    cookiefile: str | None = None,
    merge_output_format: str | None = None,
    extract_audio_codec: str | None = None,
    max_file_size: int | None = None,
) -> list[str]:
    """Собирает локальную CLI-команду yt-dlp для fallback-сценария."""
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-progress",
        "--newline",
        "--no-playlist",
        "--retries",
        str(DEFAULT_YTDLP_NETWORK_OPTS["retries"]),
        "--fragment-retries",
        str(DEFAULT_YTDLP_NETWORK_OPTS["fragment_retries"]),
        "--socket-timeout",
        str(DEFAULT_YTDLP_NETWORK_OPTS["socket_timeout"]),
        "--concurrent-fragments",
        str(DEFAULT_YTDLP_NETWORK_OPTS["concurrent_fragment_downloads"]),
        "--abort-on-unavailable-fragments",
        "--no-continue",
        "--print",
        "after_move:filepath",
        "-o",
        str(output_path_template),
        "-f",
        format_selector,
    ]
    if cookiefile:
        command.extend(["--cookies", cookiefile])
    if max_file_size is not None:
        command.extend(["--max-filesize", str(max_file_size)])
    if merge_output_format:
        command.extend(["--merge-output-format", merge_output_format])
    if extract_audio_codec in {"mp3", "m4a"}:
        command.extend(["-x", "--audio-format", extract_audio_codec])
        if extract_audio_codec == "mp3":
            command.extend(["--audio-quality", "192K"])
    command.append(url)
    return command


def _download_with_cli_fallback(
    *,
    url: str,
    session_id: str,
    format_selector: str,
    use_cookies: bool,
    output_dir: Path | None = None,
    force_local: bool = False,
    merge_output_format: str | None = None,
    extract_audio_codec: str | None = None,
    max_file_size: int | None = None,
) -> Path | str:
    """Локальный fallback на `python -m yt_dlp`, если встроенный API дал сбой.

    Фактический формат отсюда в отчёт не пишется: CLI печатает только путь к файлу
    (`--print after_move:filepath`), а записать вместо принесённого формата
    запрошенный селектор хуже, чем не записать ничего, — кэш поверил бы ключу,
    которого никто не проверял.
    """
    output_path_template = _resolve_output_template(session_id, output_dir)
    cookiefile = _cookiefile_if_available(use_cookies)
    command = _build_cli_download_command(
        url=url,
        output_path_template=output_path_template,
        format_selector=format_selector,
        cookiefile=cookiefile,
        merge_output_format=merge_output_format,
        extract_audio_codec=extract_audio_codec,
        max_file_size=max_file_size,
    )
    result = run_yt_dlp_cli(command)
    if result.returncode != 0:
        raise RuntimeError(
            "CLI fallback yt-dlp завершился ошибкой: "
            f"{(result.stderr or result.stdout).strip()[:1000]}"
        )

    downloaded_file = extract_cli_output_path(result.stdout)
    if not downloaded_file:
        raise RuntimeError("CLI fallback yt-dlp не вернул путь к итоговому файлу.")

    if merge_output_format:
        downloaded_file = _ensure_ios_compatible(downloaded_file, session_id)
    return finalize_downloaded_file(downloaded_file, force_local)


def download_video(
    url: str,
    format_id: str,
    session_id: str,
    output_dir: Path | None = None,
    force_local: bool = False,
    max_file_size: int | None = None,
) -> Path | str:
    logger.info(f"Скачивание видео: {url}, формат: {format_id}")
    output_path_template = _resolve_output_template(session_id, output_dir)
    # `format_id!=18` — тот же PO-токен: без него прогрессивный itag 18 отдаёт 403,
    # поэтому запасной селектор не должен на него сваливаться.
    fallback_non_hls = (
        "bestvideo[protocol!=m3u8_dash][protocol!=http_dash_segments]"
        "+(bestaudio[language^=ru][protocol!=m3u8_dash]"
        "[protocol!=http_dash_segments]/"
        "bestaudio[protocol!=m3u8_dash][protocol!=http_dash_segments])/"
        "best[protocol!=m3u8_dash][protocol!=http_dash_segments][ext=mp4]"
        f"[format_id!=18][filesize<=?{max_file_size or MAX_FILE_SIZE}]"
    )

    def _resolve_format_selector(
        *,
        prefer_non_hls: bool = False,
        override_format: str | None = None,
    ) -> str:
        format_to_use = override_format or format_id
        if prefer_non_hls:
            logger.info("Фолбек на non-HLS формат: %s", fallback_non_hls)
            return fallback_non_hls

        if (
            not override_format
            and not force_local
            and "[" not in format_to_use
            and "+" not in format_to_use
        ):
            format_to_use = f"{format_to_use}[filesize<=?{max_file_size or MAX_FILE_SIZE}]"
            logger.info("Применяем фильтр по размеру: %s", format_to_use)

        return format_to_use

    def _download(
        use_cookies: bool,
        prefer_non_hls: bool = False,
        override_format: str | None = None,
    ) -> Path | str:
        ydl_opts = {
            "format": _resolve_format_selector(
                prefer_non_hls=prefer_non_hls,
                override_format=override_format,
            ),
            "outtmpl": str(output_path_template),
            "quiet": False,
            "progress_hooks": [
                lambda d: logger.debug(
                    f"Скачивание: {d['status']} - {d.get('_percent_str', '0%')}"
                )
            ],
            "merge_output_format": "mp4",
        }
        if max_file_size is not None:
            ydl_opts["max_filesize"] = max_file_size
        apply_network_opts(ydl_opts, session_id=session_id)
        cookiefile = _cookiefile_if_available(use_cookies)
        if cookiefile:
            logger.info("Использование файла cookies для скачивания: %s", cookiefile)
            ydl_opts["cookiefile"] = cookiefile
        elif use_cookies:
            logger.warning(
                "Файл cookies указан (%s), но не найден. Скачивание будет без cookies.",
                YOUTUBE_COOKIES_FILE,
            )
        else:
            logger.info("Пробуем скачать видео без cookies.")

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # После объединения дорожек итоговый путь может отличаться от
            # prepare_filename (например, исходный webm стал mp4).
            downloaded_file = Path(info.get("filepath") or ydl.prepare_filename(info))
            if not downloaded_file.exists():
                raise FileNotFoundError(
                    f"yt-dlp завершил скачивание, но итоговый файл не найден: {downloaded_file}"
                )
            logger.info("Видео успешно скачано. Файл: %s", downloaded_file)
            # Формат берётся из info-dict, а не из запроса: каскад фолбеков ниже
            # молча подменяет селектор, и под запрошенным `format_id` в кэш мог
            # осесть файл совсем другого разрешения.
            record_delivered_format(session_id, info.get("format_id"))
            downloaded_file = _ensure_ios_compatible(downloaded_file, session_id)
            result = finalize_downloaded_file(downloaded_file, force_local)
            logger.info("Видео готово к выдаче: %s", result)
            return result

    def _try_with_fallback(use_cookies: bool) -> Path | str:
        try:
            return _download(use_cookies)
        except yt_dlp.utils.DownloadError as e:
            message = str(e)
            if "403" in message or "HTTP Error 403" in message or "fragment" in message:
                logger.warning("Пробуем non-HLS фолбек после 403/fragment ошибки")
                return _download(use_cookies, prefer_non_hls=True)
            if "Requested format is not available" in message:
                logger.warning(
                    "Формат недоступен, пробуем generic bestvideo+bestaudio/best"
                )
                return _download(
                    use_cookies,
                    override_format="bestvideo+(bestaudio[language^=ru]/bestaudio)/best",
                )
            raise

    # Порядок такой же, как в загрузчиках аудио: без cookies — первым, потому что
    # только анонимный запрос получает клиента `android_vr`, которому не нужен
    # PO-токен. Cookies остаются на видео с ограничениями.
    final_error: Exception | None = None
    try:
        return execute_with_backoff(
            "Скачивание видео без cookies",
            lambda: _try_with_fallback(False),
        )
    except (
        yt_dlp.utils.DownloadError,
        yt_dlp.cookies.CookieLoadError,
        FileNotFoundError,
        PermissionError,
    ) as e:
        final_error = e
        logger.warning(
            "Ошибка скачивания видео без cookies: %s. Пробуем с cookies...", e
        )

    if YOUTUBE_COOKIES_FILE and Path(YOUTUBE_COOKIES_FILE).is_file():
        try:
            return execute_with_backoff(
                "Скачивание видео с cookies",
                lambda: _try_with_fallback(True),
            )
        except (
            yt_dlp.utils.DownloadError,
            yt_dlp.cookies.CookieLoadError,
            FileNotFoundError,
            PermissionError,
        ) as e:
            final_error = e
            logger.error(
                "Ошибка скачивания видео даже с cookies: %s", e, exc_info=True
            )

    if YTDLP_CLI_FALLBACK:
        error_kind = classify_download_error_kind(str(final_error or ""))
        if error_kind not in {"ACCESS_RESTRICTED", "RATE_LIMIT"}:
            logger.warning("Переключаемся на локальный CLI fallback yt-dlp")
            cli_overrides: list[tuple[bool, str | None, bool]] = [
                (False, None, False),
                (True, None, False),
                (False, "bestvideo+(bestaudio[language^=ru]/bestaudio)/best", False),
                (True, "bestvideo+(bestaudio[language^=ru]/bestaudio)/best", False),
                (False, None, True),
                (True, None, True),
            ]
            for use_cookies, override_format, prefer_non_hls in cli_overrides:
                if use_cookies and not (
                    YOUTUBE_COOKIES_FILE and Path(YOUTUBE_COOKIES_FILE).is_file()
                ):
                    continue
                try:
                    return _download_with_cli_fallback(
                        url=url,
                        session_id=session_id,
                        format_selector=_resolve_format_selector(
                            prefer_non_hls=prefer_non_hls,
                            override_format=override_format,
                        ),
                        use_cookies=use_cookies,
                        output_dir=output_dir,
                        force_local=force_local,
                        merge_output_format="mp4",
                        max_file_size=max_file_size,
                    )
                except Exception as cli_error:
                    logger.warning("CLI fallback не удался: %s", cli_error)

    if final_error:
        raise final_error
    raise RuntimeError("Не удалось скачать видео: неизвестная ошибка")


def download_audio_native(
    url: str,
    format_id: str,
    session_id: str,
    force_local: bool = False,
    output_dir: Path | None = None,
) -> Path | str:
    """
    Скачивает только аудио в оригинальном формате (m4a/ogg) БЕЗ конвертации.
    Для нативного воспроизведения в Telegram.

    Args:
        url: URL YouTube видео
        format_id: ID формата аудио
        session_id: ID сессии
        force_local: Принудительное локальное сохранение
        output_dir: Директория для сохранения

    Returns:
        Путь к локальному аудиофайлу.
    """
    logger.info(f"Скачивание нативного аудио: {url}, формат: {format_id}")
    output_path_template = _resolve_output_template(session_id, output_dir)

    def _resolve_native_audio_selector(override_format: str | None = None) -> str:
        effective_format = override_format or format_id
        if (
            not override_format
            and not force_local
            and "[" not in effective_format
            and "+" not in effective_format
        ):
            filtered = f"{effective_format}[filesize<=?{MAX_FILE_SIZE}]"
            logger.info("Применяем фильтр по размеру для нативного аудио: %s", filtered)
            return filtered
        return effective_format

    def _download_audio_native(
        use_cookies: bool, override_format: str | None = None
    ) -> Path | str:
        ydl_opts = {
            "format": _resolve_native_audio_selector(override_format),
            "outtmpl": str(output_path_template),
            "quiet": False,
            "progress_hooks": [
                lambda d: logger.debug(
                    f"Скачивание нативного аудио: {d['status']} - {d.get('_percent_str', '0%')}"
                )
            ],
        }
        apply_network_opts(ydl_opts, session_id=session_id)

        cookiefile = _cookiefile_if_available(use_cookies)
        if cookiefile:
            logger.info(
                "Использование файла cookies для скачивания нативного аудио: %s",
                cookiefile,
            )
            ydl_opts["cookiefile"] = cookiefile
        elif use_cookies:
            logger.warning(
                "Файл cookies указан (%s), но не найден.", YOUTUBE_COOKIES_FILE
            )
        else:
            logger.info("Пробуем скачать нативное аудио без cookies.")

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            downloaded_file = Path(ydl.prepare_filename(info))
            if not downloaded_file.exists():
                raise Exception("Аудио файл не был создан.")
            logger.info("Нативное аудио успешно скачано: %s", downloaded_file)
            record_delivered_format(session_id, info.get("format_id"))
            return finalize_downloaded_file(downloaded_file, force_local)

    def _try_audio_native(use_cookies: bool) -> Path | str:
        try:
            return _download_audio_native(use_cookies)
        except yt_dlp.utils.DownloadError as e:
            if "Requested format is not available" in str(e):
                logger.warning(
                    "Формат нативного аудио недоступен, пробуем generic bestaudio"
                )
                return _download_audio_native(
                    use_cookies,
                    override_format="bestaudio[language^=ru]/bestaudio",
                )
            raise

    final_error: Exception | None = None
    try:
        return execute_with_backoff(
            "Скачивание нативного аудио без cookies",
            lambda: _try_audio_native(False),
        )
    except (
        yt_dlp.utils.DownloadError,
        yt_dlp.cookies.CookieLoadError,
        FileNotFoundError,
        PermissionError,
    ) as e:
        final_error = e
        logger.warning(
            "Ошибка скачивания нативного аудио без cookies: %s. Пробуем с cookies...", e
        )

    if YOUTUBE_COOKIES_FILE and Path(YOUTUBE_COOKIES_FILE).is_file():
        try:
            return execute_with_backoff(
                "Скачивание нативного аудио с cookies",
                lambda: _try_audio_native(True),
            )
        except (
            yt_dlp.utils.DownloadError,
            yt_dlp.cookies.CookieLoadError,
            FileNotFoundError,
            PermissionError,
        ) as e:
            final_error = e
            logger.error(
                "Ошибка при скачивании нативного аудио даже с cookies: %s",
                e,
                exc_info=True,
            )

    if YTDLP_CLI_FALLBACK and classify_download_error_kind(
        str(final_error or "")
    ) not in {"ACCESS_RESTRICTED", "RATE_LIMIT"}:
        logger.warning("Переключаемся на CLI fallback для нативного аудио")
        for use_cookies, override_format in (
            (False, None),
            (True, None),
            (False, "bestaudio[language^=ru]/bestaudio"),
            (True, "bestaudio[language^=ru]/bestaudio"),
        ):
            if use_cookies and not (
                YOUTUBE_COOKIES_FILE and Path(YOUTUBE_COOKIES_FILE).is_file()
            ):
                continue
            try:
                return _download_with_cli_fallback(
                    url=url,
                    session_id=session_id,
                    format_selector=_resolve_native_audio_selector(override_format),
                    use_cookies=use_cookies,
                    output_dir=output_dir,
                    force_local=force_local,
                )
            except Exception as cli_error:
                logger.warning("CLI fallback нативного аудио не удался: %s", cli_error)

    if final_error:
        raise final_error
    raise RuntimeError("Не удалось скачать нативное аудио: неизвестная ошибка")


def download_audio(
    url: str,
    format_id: str,
    session_id: str,
    force_local: bool = False,
    output_dir: Path | None = None,
    preferred_codec: str = "mp3",
) -> Path | str:
    """
    Скачивает только аудио и конвертирует через FFmpegExtractAudio.
    Оптимизировано: использует yt-dlp postprocessor для прямого извлечения аудио.

    Args:
        url: URL YouTube видео
        format_id: ID формата аудио (обычно 'bestaudio' или конкретный ID)
        session_id: ID сессии
        force_local: Принудительное локальное сохранение
        output_dir: Директория для сохранения
        preferred_codec: Выходной аудио-кодек (по умолчанию 'mp3')

    Returns:
        Путь к локальному MP3-файлу.
    """
    logger.info(f"Скачивание аудио с конвертацией в MP3: {url}, формат: {format_id}")
    output_path_template = _resolve_output_template(session_id, output_dir)

    def _resolve_audio_selector(override_format: str | None = None) -> str:
        effective_format = override_format or format_id
        if (
            not override_format
            and not force_local
            and "[" not in effective_format
            and "+" not in effective_format
        ):
            filtered = f"{effective_format}[filesize<=?{MAX_FILE_SIZE}]"
            logger.info("Применяем фильтр по размеру для аудио: %s", filtered)
            return filtered
        if "+" in effective_format:
            logger.info(
                "Комбинированный формат аудио %s - фильтр не применяется",
                effective_format,
            )
        return effective_format

    def _download_audio(
        use_cookies: bool, override_format: str | None = None
    ) -> Path | str:
        ydl_opts = {
            "format": _resolve_audio_selector(override_format),
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

        cookiefile = _cookiefile_if_available(use_cookies)
        if cookiefile:
            logger.info(
                "Использование файла cookies для скачивания аудио: %s", cookiefile
            )
            ydl_opts["cookiefile"] = cookiefile
        elif use_cookies:
            logger.warning(
                "Файл cookies указан (%s), но не найден. Скачивание аудио будет без cookies.",
                YOUTUBE_COOKIES_FILE,
            )
        else:
            logger.info("Пробуем скачать аудио без cookies.")

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            base_filename = Path(ydl.prepare_filename(info))
            downloaded_file = base_filename.with_suffix(f".{preferred_codec}")
            if not downloaded_file.exists():
                raise Exception("Аудио файл не был создан после postprocessing.")
            logger.info(
                "Аудио успешно извлечено и конвертировано в %s: %s",
                preferred_codec,
                downloaded_file,
            )
            record_delivered_format(session_id, info.get("format_id"))
            return finalize_downloaded_file(downloaded_file, force_local)

    def _try_audio(use_cookies: bool) -> Path | str:
        try:
            return _download_audio(use_cookies)
        except yt_dlp.utils.DownloadError as e:
            if "Requested format is not available" in str(e):
                logger.warning("Формат аудио недоступен, пробуем generic bestaudio")
                return _download_audio(
                    use_cookies,
                    override_format="bestaudio[language^=ru]/bestaudio",
                )
            raise

    final_error: Exception | None = None
    try:
        return execute_with_backoff(
            "Скачивание аудио без cookies",
            lambda: _try_audio(False),
        )
    except (
        yt_dlp.utils.DownloadError,
        yt_dlp.cookies.CookieLoadError,
        FileNotFoundError,
        PermissionError,
    ) as e:
        final_error = e
        logger.warning(
            "Ошибка скачивания аудио без cookies: %s. Пробуем с cookies...", e
        )

    if YOUTUBE_COOKIES_FILE and Path(YOUTUBE_COOKIES_FILE).is_file():
        try:
            return execute_with_backoff(
                "Скачивание аудио с cookies",
                lambda: _try_audio(True),
            )
        except (
            yt_dlp.utils.DownloadError,
            yt_dlp.cookies.CookieLoadError,
            FileNotFoundError,
            PermissionError,
        ) as e:
            final_error = e
            logger.error(
                "Ошибка при скачивании аудио даже с cookies: %s", e, exc_info=True
            )

    if YTDLP_CLI_FALLBACK and classify_download_error_kind(
        str(final_error or "")
    ) not in {"ACCESS_RESTRICTED", "RATE_LIMIT"}:
        logger.warning("Переключаемся на CLI fallback для %s-аудио", preferred_codec)
        for use_cookies, override_format in (
            (False, None),
            (True, None),
            (False, "bestaudio[language^=ru]/bestaudio"),
            (True, "bestaudio[language^=ru]/bestaudio"),
        ):
            if use_cookies and not (
                YOUTUBE_COOKIES_FILE and Path(YOUTUBE_COOKIES_FILE).is_file()
            ):
                continue
            try:
                return _download_with_cli_fallback(
                    url=url,
                    session_id=session_id,
                    format_selector=_resolve_audio_selector(override_format),
                    use_cookies=use_cookies,
                    output_dir=output_dir,
                    force_local=force_local,
                    extract_audio_codec=preferred_codec,
                )
            except Exception as cli_error:
                logger.warning(
                    "CLI fallback %s-аудио не удался: %s", preferred_codec, cli_error
                )

    if final_error:
        raise final_error
    raise RuntimeError("Не удалось скачать аудио: неизвестная ошибка")


def download_subtitles(
    url: str,
    session_id: str,
    language: str = "ru",
    subtitle_format: str = "srt",
    output_dir: Path | None = None,
) -> Path | None:
    """Скачивает субтитры выбранного языка в выбранном формате.

    Args:
        language: код языка, например ``ru``. Региональные варианты вида
            ``en-US`` и ``ru-orig`` подбираются автоматически: YouTube отдаёт
            автоперевод именно так.
        subtitle_format: ``srt``, ``vtt`` или ``txt``. TXT yt-dlp не отдаёт,
            поэтому он собирается из SRT — таймкоды убираются здесь.

    Returns:
        Path к файлу субтитров или None, если дорожки на этом языке нет.
    """
    logger.info("Скачивание субтитров (%s, %s): %s", language, subtitle_format, url)
    # TXT — наш формат, а не yt-dlp: запрашиваем SRT и разбираем его сами.
    requested_format = "srt" if subtitle_format == "txt" else subtitle_format

    def _download_subs(use_cookies: bool) -> Path | None:
        if output_dir is None:
            output_path_template = get_temp_file_path(session_id, "%(title)s.%(ext)s")
        else:
            output_path_template = output_dir / "%(title)s.%(ext)s"

        ydl_opts = {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitlesformat": requested_format,
            "outtmpl": str(output_path_template),
            "quiet": False,
        }
        apply_network_opts(ydl_opts, session_id=session_id)

        subtitle_cookiefile = _cookiefile_if_available(use_cookies)
        if subtitle_cookiefile:
            logger.info(f"Использование cookies для субтитров: {subtitle_cookiefile}")
            ydl_opts["cookiefile"] = subtitle_cookiefile

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

            available = {
                **(info.get("automatic_captions") or {}),
                **(info.get("subtitles") or {}),
            }
            track = _match_subtitle_track(available, language)
            if not track:
                logger.warning("Субтитры на языке %s недоступны", language)
                return None

            ydl_opts["subtitleslangs"] = [track]

            with yt_dlp.YoutubeDL(ydl_opts) as ydl_download:
                ydl_download.download([url])

            base_filename = Path(ydl.prepare_filename(info))
            subtitle_file = base_filename.with_suffix(f".{track}.{requested_format}")

            if not subtitle_file.exists():
                raise FileNotFoundError("Скачанный файл субтитров не найден")

            if subtitle_format == "txt":
                subtitle_file = _convert_subtitles_to_text(subtitle_file)

            logger.info(f"Субтитры успешно скачаны: {subtitle_file}")
            return subtitle_file

    try:
        return _download_subs(False)
    except Exception as e:
        logger.warning(
            f"Ошибка скачивания субтитров без cookies: {e}. Пробуем с cookies..."
        )
        if not (YOUTUBE_COOKIES_FILE and Path(YOUTUBE_COOKIES_FILE).is_file()):
            raise
        try:
            return _download_subs(True)
        except Exception as e2:
            logger.error(
                f"Ошибка скачивания субтитров даже с cookies: {e2}", exc_info=True
            )
            raise


def _match_subtitle_track(available: dict[str, Any], language: str) -> str | None:
    """Находит код дорожки под нужный язык, включая региональные варианты."""
    if language in available:
        return language
    prefix = f"{language}-"
    return next(
        (code for code in available if str(code).lower().startswith(prefix)), None
    )


def _convert_subtitles_to_text(subtitle_file: Path) -> Path:
    """Превращает SRT в обычный текст, оставляя только реплики."""
    text_file = subtitle_file.with_suffix(".txt")
    text_file.write_text(
        srt_to_text(subtitle_file.read_text(encoding="utf-8", errors="replace")),
        encoding="utf-8",
    )
    subtitle_file.unlink(missing_ok=True)
    return text_file
