"""
Модуль для работы с TikTok и Instagram с использованием yt-dlp.
"""

import json
import mimetypes
import re
import threading
import time
from html import unescape
from pathlib import Path
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Sequence
from urllib.parse import urlparse

import copy
import httpx
import yt_dlp
from yt_dlp.extractor.instagram import _id_to_pk as _instagram_shortcode_to_pk
from yt_dlp.networking.impersonate import ImpersonateTarget
from utils.cookie_workfile import working_cookie_file
from utils.download_report import record_delivered_format
from utils.logger import setup_logger
from utils.temp_file_manager import get_temp_file_path
from utils.ytdlp_common import (
    FileSizeLimitError,
    finalize_downloaded_file,
    output_capture_opts,
)
from utils.media_processor import (
    ensure_ios_compatible_video,
    extract_audio_copy,
    has_audio_stream,
)
from utils.fast_path import FastPathUnavailable
from utils.instagram_fast_path import InstagramFastMedia, parse_instagram_fast_media
from utils.tiktok_fast_path import FastMedia, parse_fast_media
from utils.url_delivery import (
    PhotoPostHandoff,
    UrlHandoff,
    is_handoff_url,
    plan_url_handoff,
)
from config import (
    INSTAGRAM_COOKIES_PATH,
    INSTAGRAM_FAST_PATH,
    MAX_FILE_SIZE,
    TIKTOK_COOKIES_PATH,
    TIKTOK_FAST_PATH,
)

logger = setup_logger(__name__)

TIKTOK_URL_PATTERN = (
    r"(?:https?:\/\/)?(?:(?:www\.|vt\.)?tiktok\.com|vm\.tiktok\.com)\/.+"
)
# Адрес публикации, за которым редиректа нет: разворачивать его не нужно.
_TIKTOK_CANONICAL_MEDIA_PATTERN = re.compile(
    r"^https?://(?:www\.)?tiktok\.com/@[^/]+/(?:video|photo)/\d+",
    re.IGNORECASE,
)
INSTAGRAM_URL_PATTERN = r"(?:https?:\/\/)?(?:www\.)?instagram\.com\/.+"
INSTAGRAM_AUDIO_URL_PATTERN = (
    r"(?:https?:\/\/)?(?:www\.)?instagram\.com\/reels\/audio\/\d+\/?"
)
INSTAGRAM_STORY_URL_PATTERN = r"(?:https?:\/\/)?(?:www\.)?instagram\.com\/stories\/.+"

# Пути к файлам cookies
INSTAGRAM_COOKIES_FILE = INSTAGRAM_COOKIES_PATH
TIKTOK_COOKIES_FILE = TIKTOK_COOKIES_PATH


def _tiktok_cookiefile(use_cookies: bool = True) -> str | None:
    """Путь к cookies TikTok для yt-dlp — рабочая копия, не оригинал."""
    if not use_cookies:
        return None
    working = working_cookie_file(TIKTOK_COOKIES_FILE)
    return str(working) if working else None


def _instagram_cookiefile(use_cookies: bool = True) -> str | None:
    """Путь к cookies Instagram для yt-dlp — рабочая копия, не оригинал."""
    if not use_cookies:
        return None
    working = working_cookie_file(INSTAGRAM_COOKIES_FILE)
    return str(working) if working else None

# Константы для retry механизма
MAX_RETRY_ATTEMPTS = 3
RETRY_DELAY_BASE = 1  # секунды
HTTP_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
INSTAGRAM_PUBLIC_PAGE_USER_AGENT = HTTP_USER_AGENT
# У TikWM два публичных host-фронтенда. Cloudflare может отказать одному из
# них для конкретного IP, хотя второй обслуживает тот же запрос.
TIKWM_API_URL = "https://tikwm.com/api/"
TIKWM_FALLBACK_API_URL = "https://www.tikwm.com/api/"

# === Бюджет ожидания на пути быстрой доставки ===
# Пока быстрый путь ждёт сокеты, воркер занят, а пользователь видит
# «Скачиваю…». Поэтому таймауты здесь короткие: при недоступном резолвере
# суммарная добавленная задержка до откатa на yt-dlp не должна превышать ~15 с.

# Один запрос на развёртывание короткой ссылки (HEAD, при отказе — GET).
TIKTOK_REDIRECT_TIMEOUT_SECONDS = 6
# Один запрос к стороннему резолверу прямых ссылок.
TIKTOK_RESOLVER_TIMEOUT_SECONDS = 6
# Один запрос к странице TikTok, содержащей JSON фото-поста.
TIKTOK_PHOTO_PAGE_TIMEOUT_SECONDS = 10
# Попыток к резолверу до откатa на yt-dlp: 2 × 6 с = 12 с в худшем случае.
TIKTOK_RESOLVER_MAX_ATTEMPTS = 2
# Один запрос к GraphQL Instagram за прямой ссылкой. На реальных рилсах ответ
# приходил за 0.9–1.1 с, поэтому 6 с — запас, а не рабочее время.
INSTAGRAM_FAST_PATH_TIMEOUT_SECONDS = 6
# Попыток к GraphQL до откатa на yt-dlp: 2 × 6 с = 12 с в худшем случае.
INSTAGRAM_FAST_PATH_MAX_ATTEMPTS = 2
# Скачивание самого медиафайла — здесь большой таймаут оправдан: холодный edge
# CDN отдавал 19.5 МБ за 14.3 с (docs/technical/latency-disk-network-research.md).
REMOTE_DOWNLOAD_TIMEOUT_SECONDS = 60

# Замер размера — это один байт по сети, но он стоит на пути пользователя,
# поэтому ждать его долго незачем: не ответил быстро — идём обычным путём.
REMOTE_SIZE_PROBE_TIMEOUT_SECONDS = 5

# Замеры для фото-поста идут параллельно: последовательно 13 кадров карусели
# стоили 9.95 с — больше, чем всё скачивание, которое доставка ссылками должна
# была удешевить. Каждый запрос — один байт, поэтому окно можно держать шире.
PHOTO_POST_PROBE_WORKERS = 6

# Сколько секунд ответ резолвера TikTok считается свежим. Ссылки в нём
# подписаны на часы, поэтому окно выбрано коротким: оно нужно лишь для того,
# чтобы два обращения подряд не упёрлись в лимит «1 request/second».
TIKTOK_RESOLVER_MEMO_TTL_SECONDS = 30
_RESOLVER_MEMO: dict[str, tuple[float, dict]] = {}
_RESOLVER_MEMO_LOCK = threading.Lock()
# Фото-пост идёт только через резолвер, откатa на yt-dlp у него нет, поэтому
# ожидание там остаётся щедрым.
TIKTOK_PHOTO_RESOLVER_TIMEOUT_SECONDS = 20
INSTAGRAM_GRAPHQL_URL = "https://www.instagram.com/graphql/query/"
INSTAGRAM_GRAPHQL_WEB_INFO_DOC_ID = "26072308439129654"


def create_tiktok_ytdl(opts: dict) -> yt_dlp.YoutubeDL:
    """
    Создает экземпляр YoutubeDL с модифицированным поведением для TikTok.
    Использует динамическое наследование от yt_dlp.YoutubeDL,
    чтобы корректно работать с моками в интеграционных тестах.
    """
    class NuvioTikTokYoutubeDL(yt_dlp.YoutubeDL):
        def process_video_result(self, info_dict, download=True):
            self._modify_formats(info_dict)
            return super().process_video_result(info_dict, download)

        def process_info(self, info_dict):
            self._modify_formats(info_dict)
            return super().process_info(info_dict)

        def _modify_formats(self, info_dict):
            if not info_dict:
                return

            if info_dict.get('_type') == 'playlist':
                for entry in info_dict.get('entries', []):
                    self._modify_formats(entry)
                return

            # Нам нужно модифицировать только форматы TikTok
            extractor = info_dict.get("extractor", "").lower() if info_dict.get("extractor") else ""
            if "tiktok" not in extractor:
                return

            formats = info_dict.get("formats", [])
            if not formats:
                return

            # 1. Ищем формат H264 со звуком (обычно 540p), чтобы сделать из него виртуальный аудио-формат
            audio_fmt = None
            for fmt in formats:
                if "h264" in fmt.get("vcodec", "") and fmt.get("acodec") != "none":
                    audio_fmt = fmt
                    break
            if not audio_fmt:
                for fmt in formats:
                    if fmt.get("acodec") != "none" and "media-video" not in fmt.get("url", ""):
                        audio_fmt = fmt
                        break

            # 2. Устанавливаем acodec = 'none' для HEVC форматов, так как звука в них на самом деле нет
            for fmt in formats:
                url_str = fmt.get("url", "")
                if "media-video" in url_str or fmt.get("vcodec") == "h265" or "bytevc1" in fmt.get("format_id", ""):
                    fmt["acodec"] = "none"
                    fmt["audio_ext"] = "none"

            # 3. Добавляем виртуальный аудио-формат, чтобы заставить yt-dlp склеить потоки
            if audio_fmt:
                has_virtual = any(f.get("format_id") == "virtual_audio_from_muxed" for f in formats)
                if not has_virtual:
                    virt_audio = copy.deepcopy(audio_fmt)
                    virt_audio["format_id"] = "virtual_audio_from_muxed"
                    virt_audio["vcodec"] = "none"
                    virt_audio["video_ext"] = "none"
                    virt_audio["ext"] = "m4a"
                    virt_audio["resolution"] = "multiple"
                    virt_audio.pop("width", None)
                    virt_audio.pop("height", None)
                    formats.append(virt_audio)
                    logger.info(f"Добавлен виртуальный аудиоформат для TikTok видео {info_dict.get('id')} на основе {audio_fmt.get('format_id')}")

    return NuvioTikTokYoutubeDL(opts)


class PhotoPostAudioMissingError(Exception):
    """У фото-поста нет отдельной аудиодорожки."""


class CriticalExtractorError(Exception):
    """Критическая ошибка экстрактора (блокировка, приватный контент, необходимость авторизации)."""


class RateLimitError(Exception):
    """Лимит запросов превышен и не был сброшен после попыток повтора."""


def is_valid_tiktok_url(url: str) -> bool:
    return bool(re.match(TIKTOK_URL_PATTERN, url))


def is_tiktok_photo_url(url: str) -> bool:
    """Проверяет, указывает ли ссылка на TikTok-фото-пост."""
    return "/photo/" in (url or "").lower()


def is_valid_instagram_url(url: str) -> bool:
    """Проверяет, является ли URL валидной ссылкой Instagram (исключая аудио ссылки)."""
    return bool(re.match(INSTAGRAM_URL_PATTERN, url)) and not is_instagram_audio_url(
        url
    )


def is_instagram_audio_url(url: str) -> bool:
    """Проверяет, является ли URL ссылкой на Instagram аудио."""
    return bool(re.match(INSTAGRAM_AUDIO_URL_PATTERN, url))


def is_instagram_story_url(url: str) -> bool:
    """Проверяет, является ли URL ссылкой на Instagram Story."""
    return bool(re.match(INSTAGRAM_STORY_URL_PATTERN, url))


def _smart_retry(
    func: Callable, max_attempts: int = MAX_RETRY_ATTEMPTS, context: str = ""
) -> Any:
    """
    Умный retry механизм с экспоненциальной задержкой.

    Args:
        func: Функция для выполнения
        max_attempts: Максимальное количество попыток
        context: Контекст для логирования

    Returns:
        Результат выполнения функции
    """
    last_exception = None

    for attempt in range(1, max_attempts + 1):
        try:
            return func()
        except Exception as e:
            last_exception = e
            error_msg = str(e).lower()

            # Проверяем тип ошибки
            if "rate-limit" in error_msg or "too many requests" in error_msg:
                if attempt < max_attempts:
                    delay = RETRY_DELAY_BASE * (2 ** (attempt - 1))
                    logger.warning(
                        f"{context} - Rate-limit обнаружен, ожидание {delay}s перед попыткой {attempt + 1}/{max_attempts}"
                    )
                    time.sleep(delay)
                    continue
            # SSL/EOF ошибки — не retry, сразу пробрасываем (вызывающий код перейдёт к след. конфигурации)
            elif "ssl" in error_msg or "unexpected eof" in error_msg:
                logger.warning(
                    f"{context} - SSL/EOF ошибка, пропускаем конфигурацию: {e}"
                )
                raise
            elif any(
                keyword in error_msg
                for keyword in [
                    "blocked",
                    "forbidden",
                    "unavailable",
                    "login required",
                    "sign in",
                ]
            ):
                logger.error(
                    f"{context} - Критическая ошибка: {e}. Дальнейшие попытки бесполезны."
                )
                raise CriticalExtractorError(str(e)) from e

            if attempt < max_attempts:
                logger.warning(
                    f"{context} - Попытка {attempt}/{max_attempts} неудачна: {e}"
                )
            else:
                logger.error(f"{context} - Все {max_attempts} попытки неудачны")

    if last_exception and (
        "rate-limit" in str(last_exception).lower()
        or "too many requests" in str(last_exception).lower()
    ):
        raise RateLimitError(str(last_exception)) from last_exception
    raise last_exception


def _get_tiktok_base_configs() -> list[dict]:
    """
    Возвращает оптимизированный список конфигураций для TikTok.
    yt-dlp >=2026.03 использует impersonation для обхода защиты TikTok.
    Старые api_hostname больше неактуальны и могут конфликтовать.
    """
    return [
        # Конфигурация 1: impersonation через curl_cffi (предпочтительная)
        {
            "quiet": True,
            "impersonate": ImpersonateTarget.from_str("chrome"),
        },
        # Конфигурация 2: Без impersonation, с актуальным User-Agent
        {
            "quiet": True,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        },
        # Конфигурация 3: Базовая (последний fallback)
        {
            "quiet": True,
        },
    ]


def _is_canonical_tiktok_media_url(url: str) -> bool:
    """Проверяет, что адрес уже указывает на публикацию напрямую."""
    return bool(_TIKTOK_CANONICAL_MEDIA_PATTERN.match(url or ""))


def _resolve_tiktok_url(url: str) -> str:
    """Разворачивает короткие TikTok-ссылки до конечного адреса.

    Полный адрес возвращается сразу: редиректа за ним нет, а запрос к нему
    измеримо стоил 0.84 с внутри «Обрабатываю ссылку...» на каждой публикации.

    Для прохождения редиректов достаточно заголовков ответа, поэтому сначала
    выполняется HEAD. Тело страницы скачивается только если сервер HEAD не
    принял — иначе на каждый запрос уходил бы лишний HTML.
    """
    if _is_canonical_tiktok_media_url(url):
        return url

    headers = {"User-Agent": HTTP_USER_AGENT}

    try:
        response = httpx.head(
            url,
            headers=headers,
            follow_redirects=True,
            timeout=TIKTOK_REDIRECT_TIMEOUT_SECONDS,
        )
        if response.status_code < 400:
            return str(response.url)
        logger.debug(
            "HEAD для %s вернул %s, повторяем полным запросом",
            url,
            response.status_code,
        )
    except Exception as e:
        logger.debug("HEAD для %s не удался (%s), повторяем полным запросом", url, e)

    try:
        response = httpx.get(
            url,
            headers=headers,
            follow_redirects=True,
            timeout=TIKTOK_REDIRECT_TIMEOUT_SECONDS,
        )
        return str(response.url)
    except Exception as e:
        logger.warning("Не удалось развернуть TikTok URL %s: %s", url, e)
        return url


def _tiktok_resolver_url(url: str) -> str:
    """Оставляет для внешнего резолвера только адрес публикации.

    Параметры TikTok вроде ``is_from_webapp``, ``sender_device``, ``_r`` и
    ``_t`` не нужны для поиска публикации и иногда приводят к 403 у tikwm.
    """
    parsed = urlparse(url)
    return parsed._replace(query="", fragment="").geturl()


def _tiktok_resolver_api_urls() -> tuple[str, ...]:
    """Возвращает адреса TikWM в порядке отказоустойчивого обращения."""
    return tuple(dict.fromkeys((TIKWM_API_URL, TIKWM_FALLBACK_API_URL)))


def _tiktok_photo_page_url(url: str) -> str | None:
    """Преобразует URL фото-поста в страницу TikTok с данными публикации.

    Для фото-поста TikTok не кладёт ``itemStruct`` в HTML страницы ``/photo/``.
    Тот же идентификатор в маршруте ``/video/`` возвращает его в
    ``__UNIVERSAL_DATA_FOR_REHYDRATION__``; это официальный HTML TikTok, а не
    сторонний резолвер.
    """
    parsed = urlparse(_tiktok_resolver_url(url))
    if (parsed.hostname or "").lower() not in {"tiktok.com", "www.tiktok.com"}:
        return None

    parts = parsed.path.strip("/").split("/")
    if len(parts) != 3 or parts[1].lower() != "photo" or not parts[2].isdigit():
        return None
    if not parts[0].startswith("@") or len(parts[0]) == 1:
        return None

    return parsed._replace(
        path=f"/{parts[0]}/video/{parts[2]}",
        query="",
        fragment="",
    ).geturl()


def _parse_tiktok_photo_page_data(html: str) -> dict[str, Any] | None:
    """Извлекает нормализованные данные фото-поста из HTML TikTok."""
    match = re.search(
        r'<script[^>]+id=["\']__UNIVERSAL_DATA_FOR_REHYDRATION__["\'][^>]*>'
        r"(?P<data>.*?)</script>",
        html,
        re.DOTALL,
    )
    if not match:
        return None

    payload = json.loads(match.group("data"))
    scope = payload.get("__DEFAULT_SCOPE__") or {}
    detail = scope.get("webapp.video-detail") or {}
    item = (detail.get("itemInfo") or {}).get("itemStruct") or {}
    if not isinstance(item, dict):
        return None

    image_post = item.get("imagePost") or {}
    images: list[str] = []
    for image in image_post.get("images") or []:
        urls = ((image or {}).get("imageURL") or {}).get("urlList") or []
        image_url = next(
            (
                candidate
                for candidate in urls
                if isinstance(candidate, str) and candidate.startswith("https://")
            ),
            None,
        )
        if image_url and image_url not in images:
            images.append(image_url)
    if not images:
        return None

    cover_urls = ((image_post.get("cover") or {}).get("imageURL") or {}).get(
        "urlList"
    ) or []
    cover = next(
        (
            candidate
            for candidate in cover_urls
            if isinstance(candidate, str) and candidate.startswith("https://")
        ),
        images[0],
    )
    music = item.get("music") or {}
    author = item.get("author") or {}
    music_url = music.get("playUrl")

    return {
        "id": item.get("id"),
        "title": str(item.get("desc") or image_post.get("title") or "").strip(),
        "description": str(item.get("desc") or ""),
        "_nuvio_description_status": (
            "available" if str(item.get("desc") or "").strip() else "empty"
        ),
        "_nuvio_description_source": "tiktok_item_struct",
        "cover": cover,
        "origin_cover": cover,
        "music": music_url,
        "images": images,
        "author": {
            "unique_id": author.get("uniqueId"),
            "nickname": author.get("nickname"),
        },
        "music_info": {
            "duration": music.get("duration") or 0,
            "play": music_url,
        },
    }


def _fetch_tiktok_photo_post_from_page(url: str) -> dict[str, Any] | None:
    """Получает фото-пост из встроенных данных страницы TikTok."""
    page_url = _tiktok_photo_page_url(url)
    if not page_url:
        return None

    def _request() -> dict[str, Any]:
        response = httpx.get(
            page_url,
            headers={
                "User-Agent": HTTP_USER_AGENT,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
            follow_redirects=True,
            timeout=TIKTOK_PHOTO_PAGE_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = _parse_tiktok_photo_page_data(response.text)
        if data is None:
            raise ValueError("TikTok не вернул данные фото-поста в HTML страницы.")
        return data

    return _smart_retry(
        _request,
        max_attempts=2,
        context="TikTok photo page",
    )


def _audio_format_sort_key(fmt: dict[str, Any]) -> tuple[float, float, int]:
    """Ключ сортировки аудиокандидатов TikTok. Применять с ``reverse=True``.

    При извлечении звука видеодорожка выбрасывается, поэтому лёгкий формат
    скачивается быстрее и предпочтительнее. При равных битрейте и размере
    предпочитаем формат без видео.
    """
    return (
        -(fmt.get("tbr") or 999999),
        -(fmt.get("filesize") or 999999999),
        1 if fmt.get("vcodec") == "none" else 0,
    )


def _is_tiktok_photo_post_info(info: dict[str, Any] | None) -> bool:
    return bool(info and info.get("_nuvio_tiktok_photo_post"))


def _is_instagram_photo_post_info(info: dict[str, Any] | None) -> bool:
    return bool(info and info.get("_nuvio_instagram_photo_post"))


def _is_instagram_empty_playlist_result(info: dict[str, Any] | None) -> bool:
    if not info:
        return False
    if info.get("_type") != "playlist" and "entries" not in info:
        return False

    entries = [entry for entry in (info.get("entries") or []) if entry]
    if entries:
        return False

    return not bool(info.get("formats"))


def _normalize_filename_component(value: str, fallback: str) -> str:
    cleaned = re.sub(r'[<>:"/\\\\|?*]+', "_", (value or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:80] or fallback


def _guess_extension(url: str, default_ext: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix and len(suffix) <= 5:
        return suffix
    guessed = mimetypes.guess_extension(mimetypes.guess_type(url)[0] or "")
    if guessed:
        return guessed
    return default_ext


def _ensure_ios_compatible_video(
    video_path: Path, session_id: str, source: str
) -> Path:
    """Приводит видео к виду, который проигрывает плеер Telegram на iOS.

    Требование ADR-001 (HEVC ломает пропорции и звук) осталось в силе, но
    расширено до ADR-002: непригодны также VP9, AV1 и H.264 профиля High 10.
    Решение принимает общая для всех платформ функция — расходиться проверкам
    нельзя. Имя оставлено прежним: на него опираются существующие тесты.
    """
    return ensure_ios_compatible_video(video_path, session_id, source)


def _download_remote_file(
    url: str,
    destination: Path,
    referer: str | None = None,
    expected_content_type: str | None = None,
) -> Path:
    """Скачивает файл по прямой ссылке.

    Args:
        expected_content_type: требуемый префикс ``content-type`` (например
            ``"video/"``). Если ответ ему не соответствует, поднимается
            ``FastPathUnavailable`` — вызывающий код откатится на yt-dlp вместо
            того, чтобы отдать пользователю чужое тело под видом медиа.
    """
    with httpx.stream(
        "GET",
        url,
        headers={
            "User-Agent": HTTP_USER_AGENT,
            "Referer": referer or "https://www.tiktok.com/",
        },
        follow_redirects=True,
        timeout=REMOTE_DOWNLOAD_TIMEOUT_SECONDS,
    ) as response:
        response.raise_for_status()

        if expected_content_type:
            content_type = (response.headers.get("content-type") or "").lower()
            if not content_type.startswith(expected_content_type):
                raise FastPathUnavailable(
                    f"ответ отдан с content-type "
                    f"{content_type or 'без content-type'}, "
                    f"ожидался {expected_content_type}*"
                )

        # Проверяем Content-Length перед скачиванием
        content_length_str = response.headers.get("content-length")
        if content_length_str:
            try:
                if int(content_length_str) > MAX_FILE_SIZE:
                    raise ValueError(
                        f"Размер удаленного файла превышает лимит в {MAX_FILE_SIZE // 1024 // 1024} МБ."
                    )
            except ValueError as val_err:
                if "превышает лимит" in str(val_err):
                    raise

        total_downloaded = 0
        try:
            with destination.open("wb") as file:
                for chunk in response.iter_bytes():
                    if chunk:
                        total_downloaded += len(chunk)
                        if total_downloaded > MAX_FILE_SIZE:
                            raise ValueError(
                                f"Размер скачанного удаленного файла превысил лимит в {MAX_FILE_SIZE // 1024 // 1024} МБ."
                            )
                        file.write(chunk)
        except Exception:
            if destination.exists():
                destination.unlink()
            raise
    return destination


def _fetch_tiktok_photo_post_data(
    url: str, *, original_url: str | None = None
) -> dict[str, Any]:
    resolved_url = _tiktok_resolver_url(_resolve_tiktok_url(url))

    try:
        page_data = _fetch_tiktok_photo_post_from_page(resolved_url)
    except Exception as error:
        logger.warning(
            "TikTok не обработал фото-пост через собственную страницу %s: %s",
            resolved_url,
            error,
        )
    else:
        if page_data is not None:
            logger.info("TikTok фото-пост получен из HTML TikTok: %s", resolved_url)
            return page_data

    resolver_urls = [resolved_url]
    fallback_url = _tiktok_resolver_url(original_url or url)
    if fallback_url not in resolver_urls:
        resolver_urls.append(fallback_url)

    last_exception: Exception | None = None
    for api_url in _tiktok_resolver_api_urls():
        for resolver_url in resolver_urls:
            def _request(
                api_url: str = api_url, resolver_url: str = resolver_url
            ) -> dict[str, Any]:
                response = httpx.get(
                    api_url,
                    params={"url": resolver_url},
                    headers={
                        "User-Agent": HTTP_USER_AGENT,
                        "Accept": "application/json, text/plain, */*",
                    },
                    follow_redirects=True,
                    timeout=TIKTOK_PHOTO_RESOLVER_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("code") != 0:
                    raise Exception(
                        f"TikTok фото-пост недоступен: {payload.get('msg') or 'неизвестная ошибка'}"
                    )
                data = payload.get("data") or {}
                if not data.get("images"):
                    raise Exception(
                        "Сервис не вернул изображения для TikTok фото-поста."
                    )
                data["_nuvio_description_source"] = "tikwm_title"
                data["description"] = str(data.get("title") or "").strip()
                return data

            try:
                return _smart_retry(
                    _request,
                    max_attempts=3,
                    context=f"TikTok photo fallback ({api_url})",
                )
            except Exception as error:
                last_exception = error
                logger.warning(
                    "TikWM не обработал URL фото-поста %s через %s: %s",
                    resolver_url,
                    api_url,
                    error,
                )

    if last_exception is not None:
        raise last_exception
    raise ValueError("Не удалось подготовить URL TikTok фото-поста для резолвера.")


def reset_tiktok_resolver_memo() -> None:
    """Забывает запомненные ответы резолвера."""
    with _RESOLVER_MEMO_LOCK:
        _RESOLVER_MEMO.clear()


def _remembered_resolver_answer(url: str) -> dict[str, Any] | None:
    """Возвращает недавний ответ резолвера по этой ссылке, если он ещё свеж."""
    now = time.monotonic()
    with _RESOLVER_MEMO_LOCK:
        for key, (stored_at, _) in list(_RESOLVER_MEMO.items()):
            if now - stored_at > TIKTOK_RESOLVER_MEMO_TTL_SECONDS:
                del _RESOLVER_MEMO[key]
        remembered = _RESOLVER_MEMO.get(url)
    return remembered[1] if remembered else None


def _call_tiktok_resolver(url: str) -> dict[str, Any]:
    """Запрашивает у резолвера прямые ссылки для TikTok-публикации.

    Ответ короткое время переиспользуется: резолвер разрешает один запрос в
    секунду, а решение «отдать ссылкой» и последующее скачивание файла идут
    подряд. Без переиспользования откат на скачивание получал бы отказ
    «Free Api Limit» и уходил на медленный путь через yt-dlp.
    """
    remembered = _remembered_resolver_answer(url)
    if remembered is not None:
        logger.debug("Ответ резолвера TikTok переиспользован")
        return remembered

    last_exception: Exception | None = None
    for api_url in _tiktok_resolver_api_urls():
        def _request(api_url: str = api_url) -> dict[str, Any]:
            response = httpx.get(
                api_url,
                params={"url": url},
                headers={
                    "User-Agent": HTTP_USER_AGENT,
                    "Accept": "application/json, text/plain, */*",
                },
                follow_redirects=True,
                timeout=TIKTOK_RESOLVER_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return response.json()

        try:
            payload = _smart_retry(
                _request,
                max_attempts=TIKTOK_RESOLVER_MAX_ATTEMPTS,
                context=f"TikTok resolver ({api_url})",
            )
            break
        except CriticalExtractorError as error:
            last_exception = error
            logger.warning(
                "TikWM не обработал URL %s через %s, пробуем следующий host: %s",
                url,
                api_url,
                error,
            )
    else:
        if last_exception is not None:
            raise last_exception
        raise ValueError("Не удалось подготовить URL TikTok для резолвера.")

    # Запоминается только пригодный ответ: отказ резолвера залипать не должен.
    if payload.get("code") == 0:
        with _RESOLVER_MEMO_LOCK:
            _RESOLVER_MEMO[url] = (time.monotonic(), payload)
    return payload


def fetch_tiktok_fast_media(url: str) -> FastMedia:
    """Возвращает прямые ссылки быстрого пути. Ожидает уже развёрнутый URL."""
    return parse_fast_media(_call_tiktok_resolver(url))


def fetch_instagram_fast_media(url: str) -> InstagramFastMedia:
    """Возвращает прямую ссылку на видео Instagram из ответа GraphQL.

    Raises:
        FastPathUnavailable: shortcode не разобран, GraphQL не дал прямой
            ссылки, это карусель или ссылка вне allowlist.
    """
    shortcode = _extract_instagram_shortcode(url)
    if not shortcode:
        raise FastPathUnavailable(
            "не удалось определить shortcode Instagram-публикации"
        )

    canonical_url = f"https://www.instagram.com/p/{shortcode}/"
    media = _smart_retry(
        lambda: _fetch_public_instagram_graphql_media(
            canonical_url,
            shortcode,
            timeout=INSTAGRAM_FAST_PATH_TIMEOUT_SECONDS,
        ),
        max_attempts=INSTAGRAM_FAST_PATH_MAX_ATTEMPTS,
        context="Instagram fast path",
    )
    return parse_instagram_fast_media(media)


def probe_remote_size(url: str, referer: str | None = None) -> int | None:
    """Узнаёт размер файла по ссылке, не скачивая его.

    Запрашивается один байт: CDN отвечает ``206`` и сообщает полный размер в
    ``Content-Range``. Ответ с кодом ошибки размером не считается — VK, к
    примеру, отдаёт на такой запрос ``400`` с двухбайтовым телом.

    Returns:
        Размер в байтах либо ``None``, если CDN его не сообщил.
    """
    try:
        response = httpx.get(
            url,
            headers={
                "User-Agent": HTTP_USER_AGENT,
                "Referer": referer or "https://www.tiktok.com/",
                "Range": "bytes=0-0",
            },
            follow_redirects=True,
            timeout=REMOTE_SIZE_PROBE_TIMEOUT_SECONDS,
        )
    except Exception as error:  # noqa: BLE001 — размер не критичен, откат есть
        logger.debug("Размер по ссылке узнать не удалось: %s", error)
        return None

    if response.status_code >= 400:
        logger.debug("CDN ответил %s на запрос размера", response.status_code)
        return None

    total = (response.headers.get("content-range") or "").rpartition("/")[2]
    if total.isdigit():
        return int(total)

    length = response.headers.get("content-length") or ""
    return int(length) if length.isdigit() else None


def resolve_tiktok_video_handoff(
    url: str, resolved_url: str | None = None
) -> UrlHandoff | None:
    """Готовит доставку TikTok-видео прямой ссылкой.

    Returns:
        Решение о доставке ссылкой либо ``None`` — тогда видео скачивается
        обычным путём.
    """
    try:
        media = fetch_tiktok_fast_media(resolved_url or _resolve_tiktok_url(url))
    except Exception as error:  # noqa: BLE001 — любой сбой означает обычный путь
        logger.debug("Доставка TikTok ссылкой недоступна: %s", error)
        return None

    size = media.size or probe_remote_size(
        media.video_url, referer="https://www.tiktok.com/"
    )
    return plan_url_handoff(media.video_url, "video", size)


def resolve_tiktok_audio_handoff(
    url: str, resolved_url: str | None = None
) -> UrlHandoff | None:
    """Готовит доставку звука TikTok прямой ссылкой.

    Ссылка на звук есть только тогда, когда это звук самого видео: для
    лицензированного трека ``music`` содержит полную песню, и её отдавать нельзя
    — такой звук вырезается из файла обычным путём.
    """
    try:
        media = fetch_tiktok_fast_media(resolved_url or _resolve_tiktok_url(url))
    except Exception as error:  # noqa: BLE001
        logger.debug("Доставка звука TikTok ссылкой недоступна: %s", error)
        return None

    if not media.audio_url:
        return None

    size = probe_remote_size(media.audio_url, referer="https://www.tiktok.com/")
    return plan_url_handoff(media.audio_url, "audio", size)


def resolve_photo_post_handoff(
    image_urls: Sequence[str],
    audio_url: str | None,
    referer: str,
) -> PhotoPostHandoff | None:
    """Готовит доставку фото-поста ссылками — целиком или никак.

    Половина картинок ссылкой, а половина файлом означала бы разный порядок
    отправки внутри одного поста, поэтому непригодность любой ссылки отменяет
    весь замысел. Звук без картинок или картинки без звука — тоже не тот пост,
    который ожидает пользователь.
    """
    targets = [*image_urls, audio_url] if audio_url else list(image_urls)
    # Ссылку вне allowlist не замеряем вовсе: запрос ушел бы на чужой адрес.
    if not image_urls or not all(is_handoff_url(target) for target in targets):
        return None

    with ThreadPoolExecutor(
        max_workers=min(PHOTO_POST_PROBE_WORKERS, len(targets))
    ) as pool:
        sizes = list(
            pool.map(lambda url: probe_remote_size(url, referer=referer), targets)
        )

    images = []
    for image_url, size in zip(image_urls, sizes):
        plan = plan_url_handoff(image_url, "photo", size)
        if plan is None:
            return None
        images.append(plan)

    audio = None
    if audio_url:
        audio = plan_url_handoff(audio_url, "audio", sizes[-1])
        if audio is None:
            return None

    return PhotoPostHandoff(images=tuple(images), audio=audio)


def resolve_instagram_video_handoff(url: str) -> UrlHandoff | None:
    """Готовит доставку видео Instagram прямой ссылкой.

    GraphQL размера не сообщает, поэтому он всегда уточняется отдельным
    запросом.
    """
    try:
        media = fetch_instagram_fast_media(url)
    except Exception as error:  # noqa: BLE001
        logger.debug("Доставка Instagram ссылкой недоступна: %s", error)
        return None

    size = probe_remote_size(media.video_url, referer="https://www.instagram.com/")
    return plan_url_handoff(
        media.video_url,
        "video",
        size,
        geometry={
            "width": media.width,
            "height": media.height,
            "duration": media.duration,
        },
    )


def download_instagram_video_fast(
    url: str,
    session_id: str,
    output_dir: Path | None = None,
    force_local: bool = False,
) -> Path:
    """Скачивает видео Instagram по прямой ссылке из GraphQL.

    Instagram отдаёт H.264 + AAC, поэтому ни мультиплексирование, ни
    перекодирование не требуются.

    Raises:
        FastPathUnavailable: GraphQL не дал пригодной прямой ссылки.
    """
    media = fetch_instagram_fast_media(url)

    title_seed = _normalize_filename_component(media.title, "instagram")
    filename = f"{title_seed}.mp4"
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        destination = output_dir / filename
    else:
        destination = get_temp_file_path(session_id, filename)

    downloaded = _download_remote_file(
        media.video_url,
        destination,
        referer="https://www.instagram.com/",
        expected_content_type="video/",
    )
    logger.info("Быстрый путь Instagram: файл получен по прямой ссылке")
    # Состав ссылки — не наш контракт, поэтому кодек проверяется так же, как на
    # пути через yt-dlp (ADR-001).
    downloaded = _ensure_ios_compatible_video(
        downloaded, session_id, "быстрый путь Instagram"
    )
    return finalize_downloaded_file(downloaded, force_local)


def _fast_destination(
    media: FastMedia,
    session_id: str,
    output_dir: Path | None,
    extension: str,
    suffix: str = "",
) -> Path:
    """Строит путь для файла, скачиваемого по прямой ссылке."""
    title_seed = _normalize_filename_component(media.title, "tiktok")
    filename = f"{title_seed}{suffix}{extension}"
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir / filename
    return get_temp_file_path(session_id, filename)


def download_tiktok_video_fast(
    url: str,
    session_id: str,
    output_dir: Path | None = None,
    force_local: bool = False,
    resolved_url: str | None = None,
) -> Path:
    """Скачивает TikTok-видео по прямой ссылке резолвера.

    Резолвер отдаёт H.264 со звуковой дорожкой, поэтому ни мультиплексирование,
    ни перекодирование HEVC не требуются.

    Raises:
        FastPathUnavailable: резолвер не дал пригодной прямой ссылки.
    """
    media = fetch_tiktok_fast_media(resolved_url or _resolve_tiktok_url(url))
    destination = _fast_destination(media, session_id, output_dir, ".mp4")
    downloaded = _download_remote_file(
        media.video_url,
        destination,
        referer="https://www.tiktok.com/",
        expected_content_type="video/",
    )
    logger.info("Быстрый путь TikTok: получено %s байт", media.size)
    # Резолвер обычно отдаёт H.264, но состав `play` — не наш контракт, поэтому
    # кодек проверяется так же, как на пути через yt-dlp (ADR-001).
    downloaded = _ensure_ios_compatible_video(
        downloaded, session_id, "быстрый путь TikTok"
    )
    return finalize_downloaded_file(downloaded, force_local)


def download_tiktok_audio_fast(
    url: str,
    session_id: str,
    output_dir: Path | None = None,
    force_local: bool = False,
    resolved_url: str | None = None,
) -> Path:
    """Скачивает звук TikTok-публикации без перекодирования.

    Если ``music`` резолвера — это звук самого видео, он забирается напрямую.
    Для лицензированного трека ``music`` содержит полную песню, поэтому звук
    извлекается из видео копированием потока.

    Raises:
        FastPathUnavailable: резолвер не дал пригодной прямой ссылки.
    """
    media = fetch_tiktok_fast_media(resolved_url or _resolve_tiktok_url(url))

    if media.audio_url:
        destination = _fast_destination(
            media,
            session_id,
            output_dir,
            _guess_extension(media.audio_url, ".mp3"),
            suffix="_audio",
        )
        downloaded = _download_remote_file(
            media.audio_url,
            destination,
            referer="https://www.tiktok.com/",
            expected_content_type="audio/",
        )
        return finalize_downloaded_file(downloaded, force_local)

    logger.info(
        "Звук публикации — библиотечный трек, извлекаем дорожку из видео"
    )
    video_destination = _fast_destination(media, session_id, output_dir, ".mp4")
    video_path = _download_remote_file(
        media.video_url,
        video_destination,
        referer="https://www.tiktok.com/",
        expected_content_type="video/",
    )
    # Видео нужно только как источник звука. Удаляем его и при сбое извлечения,
    # иначе откат на yt-dlp скачает видео повторно в тот же каталог и удвоит
    # пиковый расход диска.
    try:
        audio_path = extract_audio_copy(video_path, session_id)
    finally:
        video_path.unlink(missing_ok=True)
    return finalize_downloaded_file(audio_path, force_local)


def _build_tiktok_photo_info(url: str, data: dict[str, Any]) -> dict[str, Any]:
    author = data.get("author") or {}
    music_info = data.get("music_info") or {}
    duration = music_info.get("duration") or 0
    title = (
        data.get("title") or ""
    ).strip() or f"TikTok фото-пост {data.get('id', '')}".strip()
    description_candidate = str(data.get("description") or "")
    tikwm_candidate = data.get("_nuvio_description_source") == "tikwm_title"
    description = "" if tikwm_candidate else description_candidate
    return {
        "id": data.get("id"),
        "title": title,
        "description": description or None,
        "_nuvio_description_status": (
            "unavailable"
            if tikwm_candidate
            else "available"
            if description.strip()
            else "empty"
        ),
        "_nuvio_description_candidate": description_candidate if tikwm_candidate else None,
        "_nuvio_description_source": data.get(
            "_nuvio_description_source", "tiktok_item_struct"
        ),
        "uploader": author.get("unique_id") or author.get("nickname") or "TikTok",
        "duration": int(duration or 0),
        "thumbnail": data.get("cover") or data.get("origin_cover"),
        "webpage_url": _resolve_tiktok_url(url),
        "extractor": "nuvio_tiktok_photo",
        "_nuvio_tiktok_photo_post": True,
        "_nuvio_tiktok_photo_data": data,
        "_nuvio_tiktok_audio_url": data.get("music") or music_info.get("play"),
        "_nuvio_tiktok_images": list(data.get("images") or []),
        "formats": [],
    }


def _photo_title_seed(info: dict[str, Any], fallback: str) -> str:
    return _normalize_filename_component(str(info.get("title") or fallback), fallback)


def _download_photo_images(
    image_urls: Sequence[str],
    session_id: str,
    title_seed: str,
    referer: str | None = None,
) -> list[Path]:
    return [
        _download_remote_file(
            image_url,
            get_temp_file_path(
                session_id,
                f"{title_seed}_{index:02d}{_guess_extension(image_url, '.jpg')}",
            ),
            referer=referer,
        )
        for index, image_url in enumerate(image_urls, start=1)
    ]


def _download_photo_audio(
    audio_url: str | None,
    session_id: str,
    title_seed: str,
    default_ext: str,
    referer: str | None = None,
) -> Path | None:
    if not audio_url:
        return None
    audio_path = get_temp_file_path(
        session_id, f"{title_seed}_audio{_guess_extension(str(audio_url), default_ext)}"
    )
    return _download_remote_file(str(audio_url), audio_path, referer=referer)


def _tiktok_photo_info(url: str, cached_info: dict[str, Any] | None) -> dict[str, Any]:
    if _is_tiktok_photo_post_info(cached_info):
        return cached_info
    return _build_tiktok_photo_info(url, _fetch_tiktok_photo_post_data(url))


def _download_tiktok_photo_audio_file(
    info: dict[str, Any], session_id: str
) -> Path | None:
    return _download_photo_audio(
        info.get("_nuvio_tiktok_audio_url"),
        session_id,
        _photo_title_seed(info, "tiktok_photo_post"),
        ".mp3",
    )


def _extract_instagram_shortcode(url: str) -> str | None:
    match = re.search(
        r"instagram\.com/(?:[^/?#]+/)?(?:p|tv|reels?)/([^/?#&]+)", url, re.IGNORECASE
    )
    return match.group(1) if match else None


def _search_html_meta(webpage: str, *, attribute: str, name: str) -> str | None:
    patterns = (
        rf'<meta[^>]+{attribute}="{re.escape(name)}"[^>]+content="([^"]+)"',
        rf'<meta[^>]+content="([^"]+)"[^>]+{attribute}="{re.escape(name)}"',
    )
    for pattern in patterns:
        match = re.search(pattern, webpage, re.IGNORECASE)
        if match:
            return unescape(match.group(1))
    return None


def _extract_instagram_username_from_meta(*values: str | None) -> str | None:
    for value in values:
        if not value:
            continue
        match = re.search(
            r"-\s*([A-Za-z0-9._]+)\s+on\s+[A-Za-z]+\s+\d{1,2},\s+\d{4}:", value
        )
        if match:
            return match.group(1)
        match = re.search(r"\(@([A-Za-z0-9._]+)\)", value)
        if match:
            return match.group(1)
    return None


def _extract_instagram_media_id_from_meta(webpage: str) -> str | None:
    app_url = _search_html_meta(webpage, attribute="property", name="al:ios:url")
    if not app_url:
        return None
    match = re.search(r"instagram://media\?id=(\d+)", app_url)
    return match.group(1) if match else None


def _fetch_instagram_photo_page_media(
    canonical_url: str, shortcode: str
) -> dict[str, Any]:
    response = httpx.get(
        canonical_url,
        headers={
            "User-Agent": INSTAGRAM_PUBLIC_PAGE_USER_AGENT,
            "Referer": "https://www.instagram.com/",
        },
        follow_redirects=True,
        timeout=20,
    )
    response.raise_for_status()
    webpage = response.text

    image_url = _search_html_meta(
        webpage, attribute="property", name="og:image"
    ) or _search_html_meta(webpage, attribute="name", name="twitter:image")
    if not image_url:
        raise Exception("Instagram не вернул изображение фото-поста.")

    description = (
        _search_html_meta(webpage, attribute="property", name="og:description")
        or _search_html_meta(webpage, attribute="name", name="description")
        or _search_html_meta(webpage, attribute="property", name="og:title")
    )
    title = _search_html_meta(
        webpage, attribute="property", name="og:title"
    ) or _search_html_meta(webpage, attribute="name", name="twitter:title")
    username = _extract_instagram_username_from_meta(description, title)
    media_id = _extract_instagram_media_id_from_meta(webpage)

    media: dict[str, Any] = {
        "shortcode": shortcode,
        "display_url": image_url,
        "owner": {"username": username} if username else {},
        "caption": description,
        "_nuvio_description_source": "instagram_html_meta",
    }
    if title:
        media["title"] = title
    if media_id:
        media["id"] = media_id
    return media


def _is_instagram_no_video_error(error_msg: str) -> bool:
    msg = (error_msg or "").lower()
    return any(
        signature in msg
        for signature in (
            "there is no video in this post",
            "no video formats found",
            "фото-пост нужно отправлять",
        )
    )


def _extract_instagram_description(media: dict[str, Any]) -> str | None:
    caption = media.get("caption")
    if isinstance(caption, dict):
        text = caption.get("text")
        if isinstance(text, str) and text.strip():
            return text
    elif isinstance(caption, str) and caption.strip():
        return caption

    edges = (media.get("edge_media_to_caption") or {}).get("edges") or []
    for edge in edges:
        node = edge.get("node") or {}
        text = node.get("text")
        if isinstance(text, str) and text.strip():
            return text
    return None


def _build_instagram_photo_title(media: dict[str, Any], shortcode: str | None) -> str:
    title = media.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()

    description = _extract_instagram_description(media)
    if description:
        first_line = next(
            (line.strip() for line in description.splitlines() if line.strip()), ""
        )
        if first_line:
            return first_line[:120]

    return f"Instagram пост {shortcode or 'photo'}".strip()


def _choose_best_instagram_image_url(media: dict[str, Any]) -> str | None:
    candidates = list(((media.get("image_versions2") or {}).get("candidates") or []))
    if not candidates:
        candidates = list(media.get("display_resources") or [])

    if candidates:
        best = max(
            candidates,
            key=lambda item: item.get("width") or item.get("config_width") or 0,
        )
        return best.get("url") or best.get("src")

    for key in ("display_url", "thumbnail_src", "thumbnail"):
        value = media.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _instagram_image_identity(image_url: str) -> str:
    parsed = urlparse(image_url)
    return f"{parsed.netloc}{parsed.path}".lower()


def _iter_instagram_photo_nodes(media: dict[str, Any]) -> list[dict[str, Any]]:
    carousel_media = media.get("carousel_media")
    if isinstance(carousel_media, list) and carousel_media:
        return [
            node
            for node in carousel_media
            if isinstance(node, dict)
            and not node.get("is_video")
            and not node.get("video_versions")
            and not node.get("video_url")
        ]

    edges = (media.get("edge_sidecar_to_children") or {}).get("edges") or []
    if edges:
        return [
            node
            for edge in edges
            if isinstance(edge, dict)
            for node in [edge.get("node") or {}]
            if isinstance(node, dict)
            and not node.get("is_video")
            and not node.get("video_url")
        ]

    return [media]


def _iter_instagram_carousel_nodes(media: dict[str, Any]) -> list[dict[str, Any]]:
    """Возвращает все элементы карусели Instagram в исходном порядке."""
    carousel_media = media.get("carousel_media")
    if isinstance(carousel_media, list) and carousel_media:
        return [node for node in carousel_media if isinstance(node, dict)]
    edges = (media.get("edge_sidecar_to_children") or {}).get("edges") or []
    if edges:
        return [
            node
            for edge in edges
            if isinstance(edge, dict)
            for node in [edge.get("node")]
            if isinstance(node, dict)
        ]
    return [media]


def _instagram_video_url(media: dict[str, Any]) -> str | None:
    """Выбирает прямую ссылку на видео-элемент sidecar, без превью-кадра."""
    direct = media.get("video_url")
    if isinstance(direct, str) and direct.startswith("https://"):
        return direct
    versions = [
        item
        for item in (media.get("video_versions") or [])
        if isinstance(item, dict) and isinstance(item.get("url"), str)
    ]
    if versions:
        best = max(
            versions,
            key=lambda item: (
                item.get("width") or 0,
                item.get("height") or 0,
                item.get("bandwidth") or 0,
            ),
        )
        return best.get("url")
    return None


def _instagram_carousel_items(media: dict[str, Any]) -> list[dict[str, Any]]:
    """Нормализует упорядоченные URL всех фото- и видео-элементов sidecar."""
    items = []
    for node in _iter_instagram_carousel_nodes(media):
        is_video = bool(
            node.get("is_video")
            or node.get("video_versions")
            or node.get("video_url")
            or node.get("video_dash_manifest")
        )
        if is_video:
            items.append(
                {
                    "kind": "video",
                    "url": _instagram_video_url(node),
                    "width": node.get("original_width") or node.get("width"),
                    "height": node.get("original_height") or node.get("height"),
                    "duration": node.get("video_duration"),
                }
            )
            continue
        items.append(
            {
                "kind": "photo",
                "url": _choose_best_instagram_image_url(node),
            }
        )
    return items


def _extract_instagram_photo_images(media: dict[str, Any]) -> list[str]:
    image_urls: list[str] = []
    seen_urls: set[str] = set()

    for node in _iter_instagram_photo_nodes(media):
        image_url = _choose_best_instagram_image_url(node)
        identity = _instagram_image_identity(image_url) if image_url else None
        if image_url and identity and identity not in seen_urls:
            seen_urls.add(identity)
            image_urls.append(image_url)

    return image_urls


def _iter_nested_leaves(
    value: Any, path: tuple[str, ...] = ()
) -> Generator[tuple[tuple[str, ...], Any], None, None]:
    if isinstance(value, dict):
        for key, nested_value in value.items():
            yield from _iter_nested_leaves(nested_value, (*path, str(key)))
    elif isinstance(value, list):
        for index, nested_value in enumerate(value):
            yield from _iter_nested_leaves(nested_value, (*path, str(index)))
    else:
        yield path, value


def _extract_instagram_audio_url(media: dict[str, Any]) -> str | None:
    direct_paths = (
        (
            "clips_metadata",
            "music_info",
            "music_asset_info",
            "progressive_download_url",
        ),
        ("clips_metadata", "music_info", "music_asset_info", "url"),
        ("clips_metadata", "original_sound_info", "progressive_download_url"),
        ("clips_metadata", "original_sound_info", "url"),
        ("music_info", "music_asset_info", "progressive_download_url"),
        ("music_info", "music_asset_info", "url"),
        (
            "music_metadata",
            "music_info",
            "music_asset_info",
            "progressive_download_url",
        ),
        ("audio_asset_info", "progressive_download_url"),
        ("audio_asset_info", "url"),
        ("audio_url",),
    )

    for path in direct_paths:
        current: Any = media
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if isinstance(current, str) and current.startswith("http"):
            return current

    for path, value in _iter_nested_leaves(media):
        if not isinstance(value, str) or not value.startswith("http"):
            continue
        normalized_path = ".".join(path).lower()
        if "progressive_download_url" in normalized_path:
            return value
        if any(
            token in normalized_path
            for token in ("audio", "music", "sound", "song", "track")
        ) and not any(
            token in normalized_path
            for token in ("display", "image", "thumbnail", "profile_pic")
        ):
            return value

    return None


def _fetch_public_instagram_graphql_media(
    canonical_url: str, shortcode: str, timeout: float | None = None
) -> dict[str, Any]:
    """Запрашивает у GraphQL Instagram данные публикации.

    ``timeout`` задаётся быстрым путём, которому нужен короткий бюджет ожидания.
    Сбор фото-постов оставлен на прежних 20 секундах: там откатa на yt-dlp нет,
    и обрыв запроса означал бы отказ пользователю.
    """
    with httpx.Client(
        headers={
            "User-Agent": INSTAGRAM_PUBLIC_PAGE_USER_AGENT,
            "X-IG-App-ID": "936619743392459",
            "X-ASBD-ID": "359341",
            "Accept": "*/*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": canonical_url,
        },
        follow_redirects=True,
        timeout=timeout if timeout is not None else 20,
    ) as client:
        response = client.get(
            INSTAGRAM_GRAPHQL_URL,
            params={
                "doc_id": INSTAGRAM_GRAPHQL_WEB_INFO_DOC_ID,
                "variables": json.dumps(
                    {"shortcode": shortcode}, separators=(",", ":")
                ),
            },
        )
        response.raise_for_status()
        payload = response.json()

    web_info = (payload.get("data") or {}).get(
        "xdt_api__v1__media__shortcode__web_info"
    ) or {}
    items = web_info.get("items") or []
    if not items:
        raise Exception("Instagram не вернул данные фото-поста.")
    return items[0]


def _fetch_instagram_photo_post_media(url: str) -> dict[str, Any]:
    shortcode = _extract_instagram_shortcode(url)
    if not shortcode:
        raise Exception("Не удалось определить shortcode Instagram поста.")
    canonical_url = f"https://www.instagram.com/p/{shortcode}/"

    product_media = None
    instagram_cookiefile = _instagram_cookiefile()
    if instagram_cookiefile:
        try:
            with yt_dlp.YoutubeDL(
                {
                    "quiet": True,
                    "cookiefile": instagram_cookiefile,
                    **output_capture_opts(),
                }
            ) as ydl:
                ie = ydl.get_info_extractor("Instagram")
                if ie._get_cookies(canonical_url).get("sessionid"):
                    payload = (
                        ie._download_json(
                            f"{ie._API_BASE_URL}/media/{_instagram_shortcode_to_pk(shortcode)}/info/",
                            shortcode,
                            fatal=False,
                            errnote=False,
                            note="Downloading Instagram photo post info",
                            headers=ie._api_headers,
                        )
                        or {}
                    )
                    items = payload.get("items") or []
                    if items:
                        product_media = items[0]
        except Exception as e:
            logger.debug(
                "Не удалось получить Instagram media/info для фото-поста %s: %s", url, e
            )

    if product_media and _extract_instagram_photo_images(product_media):
        return product_media

    try:
        media = _smart_retry(
            lambda: _fetch_public_instagram_graphql_media(canonical_url, shortcode),
            max_attempts=3,
            context="Instagram photo metadata",
        )
        if _extract_instagram_photo_images(media):
            return media
    except Exception as graph_error:
        logger.warning("Instagram GraphQL недоступен для %s: %s", url, graph_error)

    media = _smart_retry(
        lambda: _fetch_instagram_photo_page_media(canonical_url, shortcode),
        max_attempts=2,
        context="Instagram photo page",
    )
    if _extract_instagram_photo_images(media):
        return media
    raise Exception("Instagram не вернул изображение фото-поста.")


def _build_instagram_photo_info(url: str, media: dict[str, Any]) -> dict[str, Any]:
    shortcode = _extract_instagram_shortcode(url) or str(
        media.get("shortcode") or media.get("code") or ""
    )
    owner = media.get("owner") or media.get("user") or {}
    description_source = media.get("_nuvio_description_source") or "instagram_graphql"
    description = (
        None
        if description_source == "instagram_html_meta"
        else _extract_instagram_description(media)
    )
    images = _extract_instagram_photo_images(media)
    carousel_items = _instagram_carousel_items(media)
    has_photo = any(item.get("kind") == "photo" for item in carousel_items)
    has_video = any(item.get("kind") == "video" for item in carousel_items)
    is_mixed = has_photo and has_video
    source_nodes = media.get("carousel_media") or (
        media.get("edge_sidecar_to_children") or {}
    ).get("edges")
    complete = (
        description_source != "instagram_html_meta"
        and bool(carousel_items)
        and all(item.get("url") for item in carousel_items)
        and (not source_nodes or len(source_nodes) == len(carousel_items))
    )
    if not images:
        raise Exception("Не удалось получить изображения для Instagram фото-поста.")

    duration = (
        media.get("video_duration")
        or media.get("music_metadata", {}).get("music_duration_in_ms")
        or 0
    )
    duration = (
        int(float(duration or 0) / 1000)
        if isinstance(duration, (int, float)) and duration > 1000
        else int(float(duration or 0))
    )

    return {
        "id": media.get("id") or shortcode,
        "title": _build_instagram_photo_title(media, shortcode),
        "uploader": owner.get("username") or owner.get("full_name") or "Instagram",
        "duration": duration,
        "thumbnail": images[0],
        "webpage_url": url,
        "description": description,
        "_nuvio_description_status": (
            "available"
            if description
            else "unavailable"
            if description_source == "instagram_html_meta"
            else "empty"
        ),
        "_nuvio_description_source": description_source,
        "extractor": "nuvio_instagram_photo",
        "_nuvio_instagram_photo_post": True,
        "_nuvio_instagram_mixed_post": is_mixed,
        "_nuvio_instagram_carousel_items": carousel_items,
        "_nuvio_instagram_carousel_incomplete": not complete,
        "_nuvio_instagram_carousel_complete": complete,
        "_nuvio_instagram_photo_data": media,
        "_nuvio_instagram_images": images,
        "_nuvio_instagram_audio_url": _extract_instagram_audio_url(media),
        "formats": [],
    }


def _try_get_instagram_photo_info(url: str) -> dict[str, Any] | None:
    try:
        media = _fetch_instagram_photo_post_media(url)
    except Exception as e:
        logger.debug(
            "Не удалось собрать запасные данные Instagram фото-поста %s: %s", url, e
        )
        return None

    if not _extract_instagram_photo_images(media):
        return None
    info = _build_instagram_photo_info(url, media)
    if info.get("_nuvio_instagram_mixed_post"):
        return info
    if media.get("is_video") is True or media.get("video_url"):
        return None
    return info


def _enrich_instagram_carousel_info(url: str, info: dict[str, Any]) -> dict[str, Any]:
    """Предпочитает полный sidecar, если yt-dlp распознал несколько записей."""
    entries = [entry for entry in (info.get("entries") or []) if entry is not None]
    if len(entries) < 2:
        return info
    try:
        carousel_info = _try_get_instagram_photo_info(url)
    except Exception as error:  # noqa: BLE001
        logger.debug("Не удалось уточнить состав Instagram-карусели: %s", error)
        return {**info, "_nuvio_instagram_carousel_incomplete": True}
    if not carousel_info:
        return {**info, "_nuvio_instagram_carousel_incomplete": True}
    items = carousel_info.get("_nuvio_instagram_carousel_items") or []
    if len(items) != len(entries):
        logger.warning(
            "Instagram sidecar не совпал с yt-dlp: %s элементов вместо %s",
            len(items),
            len(entries),
        )
        return {
            **carousel_info,
            "_nuvio_instagram_carousel_complete": False,
            "_nuvio_instagram_carousel_incomplete": True,
            "_nuvio_instagram_expected_items": len(entries),
        }
    entry_kinds = [
        "video" if entry.get("formats") else "photo" for entry in entries
    ]
    sidecar_kinds = [item.get("kind") for item in items]
    if sidecar_kinds != entry_kinds:
        logger.warning(
            "Instagram sidecar и yt-dlp расходятся по типу элемента: %s против %s",
            sidecar_kinds,
            entry_kinds,
        )
        return {
            **carousel_info,
            "_nuvio_instagram_carousel_complete": False,
            "_nuvio_instagram_carousel_incomplete": True,
            "_nuvio_instagram_expected_kinds": entry_kinds,
        }
    return carousel_info


def _instagram_photo_info(
    url: str, cached_info: dict[str, Any] | None
) -> dict[str, Any]:
    if _is_instagram_photo_post_info(cached_info):
        return cached_info
    return _build_instagram_photo_info(url, _fetch_instagram_photo_post_media(url))


def _download_instagram_photo_audio_file(
    info: dict[str, Any], session_id: str, title_seed: str
) -> Path | None:
    return _download_photo_audio(
        info.get("_nuvio_instagram_audio_url"),
        session_id,
        title_seed,
        ".m4a",
        referer="https://www.instagram.com/",
    )


def _collect_instagram_photo_assets(
    session_id: str, info: dict[str, Any]
) -> tuple[list[Path], Path | None]:
    title_seed = _photo_title_seed(info, "instagram_photo_post")
    image_paths = _download_photo_images(
        info.get("_nuvio_instagram_images") or [],
        session_id,
        title_seed,
        referer="https://www.instagram.com/",
    )
    return image_paths, _download_instagram_photo_audio_file(info, session_id, title_seed)


def _collect_instagram_mixed_carousel_assets(
    url: str,
    session_id: str,
    cached_info: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], Path | None]:
    """Скачивает полный sidecar, сохраняя порядок фото и видео."""
    info = cached_info
    items = list(info.get("_nuvio_instagram_carousel_items") or [])
    if (
        not info.get("_nuvio_instagram_carousel_complete")
        or not items
        or any(not item.get("url") for item in items)
    ):
        raise ValueError("Не удалось подтвердить полный состав карусели Instagram.")

    title_seed = _photo_title_seed(info, "instagram_carousel")
    downloaded: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        kind = item["kind"]
        if kind not in {"photo", "video"}:
            raise ValueError("Instagram вернул неизвестный тип элемента карусели.")
        extension = _guess_extension(
            str(item["url"]), ".mp4" if kind == "video" else ".jpg"
        )
        media_path = get_temp_file_path(
            session_id, f"{title_seed}_{index:02d}{extension}"
        )
        media_path = _download_remote_file(
            str(item["url"]),
            media_path,
            referer="https://www.instagram.com/",
            expected_content_type="video/" if kind == "video" else "image/",
        )
        if kind == "video":
            media_path = _ensure_ios_compatible_video(
                media_path, session_id, "Instagram carousel"
            )
        downloaded.append({"kind": kind, "path": media_path})

    audio_path = _download_instagram_photo_audio_file(info, session_id, title_seed)
    info["_nuvio_instagram_images"] = [
        item.get("url")
        for item in items
        if item.get("kind") == "photo" and item.get("url")
    ]
    return info, downloaded, audio_path


def _photo_assets(
    info: dict[str, Any], image_paths: list[Path], audio_path: Path | None
) -> dict[str, Any]:
    return {
        "info": info,
        "items": [{"kind": "photo", "path": path} for path in image_paths],
        "audio": audio_path,
    }


def download_tiktok_photo_post_assets(
    url: str,
    session_id: str,
    cached_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Скачивает изображения и звук TikTok-фото-поста для поэтапной отправки."""
    info = _tiktok_photo_info(url, cached_info)
    image_paths = _download_photo_images(
        info.get("_nuvio_tiktok_images") or [],
        session_id,
        _photo_title_seed(info, "tiktok_photo_post"),
    )
    return _photo_assets(
        info, image_paths, _download_tiktok_photo_audio_file(info, session_id)
    )


def download_tiktok_photo_audio(
    url: str,
    session_id: str,
    output_dir: Path | None = None,
    force_local: bool = False,
    cached_info: dict[str, Any] | None = None,
) -> Path | str:
    """Скачивает аудиодорожку TikTok-фото-поста без его изображений."""
    info = _tiktok_photo_info(url, cached_info)
    audio_path = _download_tiktok_photo_audio_file(info, session_id)
    if output_dir is not None:
        logger.debug(
            "output_dir=%s передан для аудио фото-поста, используется временная директория сессии",
            output_dir,
        )
    if audio_path is None:
        raise PhotoPostAudioMissingError(
            f"У TikTok фото-поста «{info.get('title') or 'без названия'}» нет отдельной аудиодорожки."
        )
    return finalize_downloaded_file(audio_path, force_local)


def download_instagram_photo_post_assets(
    url: str,
    session_id: str,
    cached_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Скачивает изображения и звук Instagram фото-поста для поэтапной отправки."""
    info = _instagram_photo_info(url, cached_info)
    if info.get("_nuvio_instagram_carousel_incomplete"):
        raise ValueError("Не удалось подтвердить полный состав карусели Instagram.")
    if info.get("_nuvio_instagram_mixed_post"):
        info, items, audio_path = _collect_instagram_mixed_carousel_assets(
            url, session_id, info
        )
        return {"info": info, "items": items, "audio": audio_path}
    return _photo_assets(info, *_collect_instagram_photo_assets(session_id, info))


def download_instagram_photo_audio(
    url: str,
    session_id: str,
    output_dir: Path | None = None,
    force_local: bool = False,
    cached_info: dict[str, Any] | None = None,
) -> Path | str:
    """Скачивает аудиодорожку Instagram фото-поста без его изображений."""
    info = _instagram_photo_info(url, cached_info)
    audio_path = _download_instagram_photo_audio_file(
        info, session_id, _photo_title_seed(info, "instagram_photo_post")
    )
    if output_dir is not None:
        logger.debug(
            "output_dir=%s передан для аудио фото-поста Instagram, используется временная директория сессии",
            output_dir,
        )
    if audio_path is None:
        raise PhotoPostAudioMissingError(
            f"У Instagram фото-поста «{info.get('title') or 'без названия'}» нет отдельной аудиодорожки."
        )
    return finalize_downloaded_file(audio_path, force_local)


def get_tiktok_info(url: str) -> dict[str, Any]:
    """
    Получает информацию о TikTok видео с умным retry механизмом.

    Args:
        url: URL TikTok видео

    Returns:
        Dict с метаданными видео
    """
    logger.info(f"Получение информации о TikTok видео: {url}")

    resolved_url = _resolve_tiktok_url(url)
    if is_tiktok_photo_url(resolved_url):
        logger.info("Определён TikTok фото-пост: %s", resolved_url)
        return _build_tiktok_photo_info(
            resolved_url,
            _fetch_tiktok_photo_post_data(resolved_url, original_url=url),
        )

    def _try_get_info(use_cookies: bool, config: dict) -> dict[str, Any]:
        """Внутренняя функция для получения информации"""
        opts = config.copy()
        opts["skip_download"] = True
        opts.update(output_capture_opts())

        cookiefile = _tiktok_cookiefile(use_cookies)
        if cookiefile:
            opts["cookiefile"] = cookiefile
            logger.info(f"Использование cookies: {cookiefile}")

        with create_tiktok_ytdl(opts) as ydl:
            return ydl.extract_info(resolved_url, download=False)

    # Получаем оптимизированные конфигурации
    configurations = _get_tiktok_base_configs()

    # Стратегия: сначала пробуем с cookies (если есть), затем без
    use_cookies_first = TIKTOK_COOKIES_FILE.exists()

    for attempt, config in enumerate(configurations, 1):
        try:
            # Сначала пробуем с cookies, если файл существует
            if use_cookies_first:
                try:
                    logger.info(
                        f"Конфигурация {attempt}/{len(configurations)} с cookies"
                    )
                    return _smart_retry(
                        lambda: _try_get_info(True, config),
                        max_attempts=2,
                        context=f"TikTok info (config {attempt}, с cookies)",
                    )
                except (CriticalExtractorError, RateLimitError):
                    raise
                except Exception as e:
                    logger.warning(f"Конфигурация {attempt} с cookies неудачна: {e}")

            # Затем пробуем без cookies
            logger.info(f"Конфигурация {attempt}/{len(configurations)} без cookies")
            return _smart_retry(
                lambda: _try_get_info(False, config),
                max_attempts=2,
                context=f"TikTok info (config {attempt}, без cookies)",
            )
        except (CriticalExtractorError, RateLimitError) as e:
            logger.error(f"Прерываем обход конфигураций из-за критической ошибки: {e}")
            raise
        except Exception as e:
            error_msg = str(e).lower()
            logger.warning(f"Конфигурация {attempt} неудачна: {e}")

            if "unsupported url" in error_msg and is_tiktok_photo_url(resolved_url):
                logger.info(
                    "yt-dlp не поддержал TikTok фото-пост, используем запасной путь"
                )
                return _build_tiktok_photo_info(
                    resolved_url,
                    _fetch_tiktok_photo_post_data(resolved_url, original_url=url),
                )

            # Если это последняя конфигурация, выдаем детальную ошибку
            if attempt == len(configurations):
                if any(
                    keyword in error_msg
                    for keyword in [
                        "unable to extract",
                        "login required",
                        "blocked",
                        "unavailable",
                    ]
                ):
                    if not TIKTOK_COOKIES_FILE.exists():
                        raise Exception(
                            "TikTok ограничил доступ к этому контенту.\n\n"
                            "Возможные причины:\n"
                            "• Превышен лимит запросов (rate-limit)\n"
                            "• Региональные ограничения\n"
                            "• Контент требует авторизации\n\n"
                            "Рекомендации:\n"
                            "• Подождите 5-10 минут перед повторной попыткой\n"
                            "• Добавьте cookies файл в `.secrets/www.tiktok.com_cookies.txt`\n"
                            "• Проверьте, что контент публичный"
                        ) from e
                    else:
                        raise Exception(
                            "TikTok ограничил доступ даже с авторизацией.\n\n"
                            "Возможные причины:\n"
                            "• Превышен лимит запросов\n"
                            "• Региональные блокировки\n\n"
                            "Рекомендации:\n"
                            "• Подождите 10-15 минут\n"
                            "• Обновите cookies файл\n"
                            "• Используйте VPN"
                        ) from e
                raise
            continue

    # Этот код не должен достигаться
    raise Exception("Не удалось получить информацию о TikTok видео после всех попыток")


def get_instagram_info(url: str) -> dict[str, Any]:
    logger.info(f"Получение информации об Instagram видео: {url}")

    def _get_info(use_cookies: bool) -> dict[str, Any]:
        """Внутренняя функция для получения информации с/без cookies"""
        ydl_opts = {
            "quiet": True,
            "skip_download": True,
            **output_capture_opts(),
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "X-IG-App-ID": "936619743392459",  # Instagram Web App ID
                "DNT": "1",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
            },
        }

        cookiefile = _instagram_cookiefile(use_cookies)
        if cookiefile:
            ydl_opts["cookiefile"] = cookiefile
            logger.info(f"Использование файла cookies для Instagram: {cookiefile}")

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    # Сначала пробуем без cookies
    try:
        logger.info("Пробуем получить информацию об Instagram видео без cookies.")
        info = _get_info(False)
        info = _enrich_instagram_carousel_info(url, info)
        if _is_instagram_photo_post_info(info):
            return info
        if _is_instagram_empty_playlist_result(info):
            if photo_info := _try_get_instagram_photo_info(url):
                logger.info(
                    "Instagram вернул пустой плейлист, переключаемся на фото-пост: %s",
                    url,
                )
                return photo_info
        logger.info("Информация об Instagram видео успешно получена.")
        return info
    except Exception as e:
        error_msg = str(e).lower()
        logger.warning(f"Ошибка получения информации без cookies: {e}")
        if photo_info := _try_get_instagram_photo_info(url):
            logger.info(
                "Определён Instagram фото-пост, используем запасной путь: %s", url
            )
            return photo_info

        # Проверяем на специфичные ошибки Instagram, требующие авторизации
        if any(
            keyword in error_msg
            for keyword in [
                "rate-limit",
                "login required",
                "not available",
                "sign in",
                "private",
            ]
        ):
            # Пробуем с файлом cookies
            if INSTAGRAM_COOKIES_FILE.exists():
                try:
                    logger.info("Пробуем с cookies файлом...")
                    info = _get_info(True)
                    info = _enrich_instagram_carousel_info(url, info)
                    if _is_instagram_photo_post_info(info):
                        return info
                    if _is_instagram_empty_playlist_result(info):
                        if photo_info := _try_get_instagram_photo_info(url):
                            logger.info(
                                "Instagram вернул пустой плейлист после попытки с cookies, переключаемся на фото-пост: %s",
                                url,
                            )
                            return photo_info
                    logger.info(
                        "Информация об Instagram видео успешно получена с cookies."
                    )
                    return info
                except Exception as e_cookie:
                    if photo_info := _try_get_instagram_photo_info(url):
                        logger.info(
                            "Определён Instagram фото-пост после попытки с cookies: %s",
                            url,
                        )
                        return photo_info
                    logger.error(f"Ошибка даже с cookies: {e_cookie}")
                    raise Exception(
                        "Instagram ограничил доступ к этому контенту даже с авторизацией. "
                        "Возможные причины:\n"
                        "• Превышен лимит запросов (rate-limit)\n"
                        "• Контент требует специальной авторизации\n"
                        "• Региональные ограничения\n"
                        "• Приватный аккаунт\n\n"
                        "Попробуйте:\n"
                        "• Подождать 5-10 минут перед повторной попыткой\n"
                        "• Обновить файл cookies в `.secrets/www.instagram.com_cookies.txt`\n"
                        "• Использовать другую ссылку\n"
                        "• Проверить, что контент публичный"
                    ) from e_cookie
            else:
                raise Exception(
                    "Instagram ограничил доступ к этому контенту. "
                    "Возможные причины:\n"
                    "• Превышен лимит запросов (rate-limit)\n"
                    "• Контент требует авторизации\n"
                    "• Региональные ограничения\n"
                    "• Приватный аккаунт\n\n"
                    "Попробуйте:\n"
                    "• Подождать некоторое время перед повторной попыткой\n"
                    "• Добавить файл cookies в `.secrets/www.instagram.com_cookies.txt`\n"
                    "• Использовать другую ссылку\n"
                    "• Проверить, что контент публичный"
                ) from e
        else:
            # Для других ошибок пробуем с файлом cookies
            if INSTAGRAM_COOKIES_FILE.exists():
                try:
                    logger.info("Пробуем с cookies файлом для других ошибок...")
                    info = _get_info(True)
                    info = _enrich_instagram_carousel_info(url, info)
                    if _is_instagram_photo_post_info(info):
                        return info
                    if _is_instagram_empty_playlist_result(info):
                        if photo_info := _try_get_instagram_photo_info(url):
                            logger.info(
                                "Instagram вернул пустой плейлист после запасной попытки с cookies, переключаемся на фото-пост: %s",
                                url,
                            )
                            return photo_info
                    logger.info(
                        "Информация об Instagram видео успешно получена с cookies."
                    )
                    return info
                except Exception as e_cookie:
                    if photo_info := _try_get_instagram_photo_info(url):
                        logger.info(
                            "Определён Instagram фото-пост после запасной попытки с cookies: %s",
                            url,
                        )
                        return photo_info
                    logger.error(f"Ошибка даже с cookies: {e_cookie}")
                    raise
            else:
                raise


def download_tiktok_video(
    url: str,
    session_id: str,
    output_dir: Path | None = None,
    force_local: bool = False,
    cached_info: dict[str, Any] | None = None,
) -> Path | str:
    """
    Скачивает TikTok видео с оптимизированной логикой.

    Args:
        url: URL TikTok видео
        session_id: ID сессии
        output_dir: Директория для сохранения
        force_local: Принудительное локальное сохранение
        cached_info: Кэшированные метаданные (для пропуска повторного запроса)

    Returns:
        Путь к локальному файлу.
    """
    logger.info(f"Скачивание TikTok видео: {url}")

    resolved_url = _resolve_tiktok_url(url)
    if _is_tiktok_photo_post_info(cached_info) or is_tiktok_photo_url(resolved_url):
        raise Exception(
            "TikTok фото-пост нужно отправлять как набор изображений и отдельное аудио."
        )

    if TIKTOK_FAST_PATH:
        # Гейт по размеру ниже считается по HQ-формату (1080p/HEVC), который
        # быстрый путь не скачивает вовсе, поэтому он проверяется только перед
        # yt-dlp. Реальный размер `play` контролируют Content-Length в
        # _download_remote_file и finalize_downloaded_file.
        try:
            return download_tiktok_video_fast(
                url, session_id, output_dir, force_local, resolved_url=resolved_url
            )
        except FileSizeLimitError:
            raise
        except Exception as fast_error:
            logger.warning(
                "Быстрый путь TikTok не сработал (%s), используем yt-dlp",
                fast_error,
            )

    # Предварительная проверка известного размера до скачивания через yt-dlp.
    if cached_info and not force_local:
        filesize = cached_info.get("filesize") or cached_info.get("filesize_approx", 0)
        if filesize and filesize > MAX_FILE_SIZE:
            raise FileSizeLimitError(
                f"Файл превышает допустимый размер "
                f"{MAX_FILE_SIZE // 1024 // 1024} МБ"
            )

    if output_dir is None:
        output_path_template = get_temp_file_path(session_id, "%(title)s.%(ext)s")
    else:
        output_path_template = output_dir / "%(title)s.%(ext)s"

    def _download_with_config(use_cookies: bool, config: dict) -> Path | str:
        """Внутренняя функция для скачивания"""
        # Сначала получаем метаданные без скачивания
        meta_opts = config.copy()
        meta_opts["quiet"] = True
        meta_opts.update(output_capture_opts(session_id))
        meta_cookiefile = _tiktok_cookiefile(use_cookies)
        if meta_cookiefile:
            meta_opts["cookiefile"] = meta_cookiefile

        with create_tiktok_ytdl(meta_opts) as ydl:
            info = ydl.extract_info(resolved_url, download=False)

        formats = info.get("formats", [])
        # Для видео берем форматы, содержащие видеодорожку
        video_formats = [
            f for f in formats
            if f.get("vcodec") != "none" and f.get("format_id")
        ]

        # Сортируем форматы по высоте разрешения (height), tbr и filesize
        def format_sort_key(f):
            return (
                f.get("height") or 0,
                f.get("tbr") or 0,
                f.get("filesize") or 0
            )

        video_formats.sort(key=format_sort_key, reverse=True)

        if not video_formats:
            video_formats = [{"format_id": "bestvideo+bestaudio/best"}]

        last_err = None
        failed_heights = set()
        for fmt in video_formats:
            fmt_id = fmt["format_id"]
            height = fmt.get("height")
            if height and height in failed_heights:
                logger.info(f"Пропускаем формат {fmt_id}, так как разрешение {height}p уже признано беззвучным.")
                continue

            logger.info(f"Попытка скачать TikTok видео формат: {fmt_id}")

            opts = config.copy()
            opts["outtmpl"] = str(output_path_template)
            opts["format"] = fmt_id
            opts["overwrites"] = True
            opts["merge_output_format"] = "mp4"
            opts["quiet"] = False
            opts.update(output_capture_opts(session_id))

            download_cookiefile = _tiktok_cookiefile(use_cookies)
            if download_cookiefile:
                opts["cookiefile"] = download_cookiefile

            downloaded_file = None
            try:
                with create_tiktok_ytdl(opts) as ydl:
                    info_download = ydl.extract_info(resolved_url, download=True)
                    downloaded_file = Path(ydl.prepare_filename(info_download))

                    if not downloaded_file.exists():
                        raise Exception("Файл не был загружен.")

                    # Проверяем наличие аудиодорожки с помощью ffprobe
                    if not has_audio_stream(downloaded_file):
                        logger.warning(
                            f"Формат {fmt_id} скачался без аудиопотока! Пробуем альтернативный формат."
                        )
                        raise Exception("Скачанный файл не содержит аудиодорожки.")

                    logger.info(f"Видео успешно скачано и проверено: {downloaded_file}")
                    record_delivered_format(session_id, info_download.get("format_id"))

                    # Отдельной ветки для webm здесь нет намеренно: в этом
                    # контейнере всегда лежит VP8, VP9 или AV1, и проверка
                    # кодека ниже покрывает этот случай целиком.
                    downloaded_file = _ensure_ios_compatible_video(
                        downloaded_file, session_id, "TikTok"
                    )

                    return finalize_downloaded_file(downloaded_file, force_local)

            except Exception as e:
                if "не содержит аудиодорожки" in str(e):
                    last_err = e
                    if height:
                        failed_heights.add(height)
                    if downloaded_file and downloaded_file.exists():
                        try:
                            downloaded_file.unlink()
                        except Exception as ue:
                            logger.warning(f"Не удалось удалить временный файл {downloaded_file}: {ue}")
                    continue
                else:
                    raise

        if last_err:
            raise last_err
        raise Exception("Не удалось скачать видео с рабочим аудиопотоком.")


    # Получаем оптимизированные конфигурации
    configurations = _get_tiktok_base_configs()
    use_cookies_first = TIKTOK_COOKIES_FILE.exists()

    # Пробуем каждую конфигурацию
    for attempt, config in enumerate(configurations, 1):
        try:
            # Сначала с cookies (если есть)
            if use_cookies_first:
                try:
                    logger.info(
                        f"Скачивание: конфигурация {attempt}/{len(configurations)} с cookies"
                    )
                    return _smart_retry(
                        lambda: _download_with_config(True, config),
                        max_attempts=2,
                        context=f"TikTok download (config {attempt}, с cookies)",
                    )
                except (CriticalExtractorError, RateLimitError):
                    raise
                except Exception as e:
                    logger.warning(f"Конфигурация {attempt} с cookies неудачна: {e}")

            # Затем без cookies
            logger.info(
                f"Скачивание: конфигурация {attempt}/{len(configurations)} без cookies"
            )
            return _smart_retry(
                lambda: _download_with_config(False, config),
                max_attempts=2,
                context=f"TikTok download (config {attempt}, без cookies)",
            )
        except (CriticalExtractorError, RateLimitError) as e:
            logger.error(
                f"Прерываем обход конфигураций из-за критической ошибки при скачивании: {e}"
            )
            raise
        except Exception as e:
            logger.warning(f"Конфигурация {attempt} неудачна: {e}")
            if attempt == len(configurations):
                raise Exception(
                    f"Не удалось скачать TikTok видео после всех попыток. Последняя ошибка: {e}"
                ) from e
            continue

    raise Exception("Не удалось скачать TikTok видео")


def download_instagram_video(
    url: str,
    session_id: str,
    output_dir: Path | None = None,
    force_local: bool = False,
    cached_info: dict[str, Any] | None = None,
) -> Path | str:
    logger.info(f"Скачивание Instagram видео: {url}")

    if _is_instagram_photo_post_info(
        cached_info
    ) or _is_instagram_empty_playlist_result(cached_info):
        raise Exception(
            "Instagram фото-пост нужно отправлять как набор изображений и отдельное аудио."
        )

    if INSTAGRAM_FAST_PATH:
        try:
            return download_instagram_video_fast(
                url, session_id, output_dir, force_local
            )
        except FileSizeLimitError:
            raise
        except Exception as fast_error:
            logger.warning(
                "Быстрый путь Instagram не сработал (%s), используем yt-dlp",
                fast_error,
            )

    if output_dir is None:
        output_path_template = get_temp_file_path(session_id, "%(title)s.%(ext)s")
    else:
        output_path_template = output_dir / "%(title)s.%(ext)s"

    def _download(use_cookies: bool) -> Path | str:
        """Внутренняя функция для скачивания с/без cookies"""
        ydl_opts = {
            "outtmpl": str(output_path_template),
            "quiet": False,
            **output_capture_opts(session_id),
            "progress_hooks": [
                lambda d: logger.debug(
                    f"Скачивание: {d['status']} - {d.get('_percent_str', '0%')}"
                )
            ],
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "X-IG-App-ID": "936619743392459",  # Instagram Web App ID
                "DNT": "1",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
            },
        }

        download_cookiefile = _instagram_cookiefile(use_cookies)
        if download_cookiefile:
            ydl_opts["cookiefile"] = download_cookiefile
            logger.info(
                f"Использование файла cookies для скачивания Instagram: {download_cookiefile}"
            )

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

            # Для stories и плейлистов yt-dlp может вернуть entries
            if _is_instagram_empty_playlist_result(info):
                raise Exception(
                    "Instagram фото-пост нужно отправлять как набор изображений и отдельное аудио."
                )
            actual_info = info
            if info.get("_type") == "playlist" or "entries" in info:
                entries = list(info.get("entries", []))
                if entries:
                    actual_info = entries[0]
                    logger.info(
                        f"Instagram вернул playlist с {len(entries)} записями, используем первую."
                    )

            downloaded_file = Path(ydl.prepare_filename(actual_info))

            # Если файл не найден по prepare_filename, ищем в директории
            if not downloaded_file.exists():
                parent_dir = downloaded_file.parent
                found_files = (
                    sorted(
                        parent_dir.glob("*.*"),
                        key=lambda f: f.stat().st_mtime,
                        reverse=True,
                    )
                    if parent_dir.exists()
                    else []
                )
                media_files = [
                    f
                    for f in found_files
                    if f.suffix.lower()
                    in (".mp4", ".webm", ".mkv", ".mov", ".avi", ".flv")
                ]
                if media_files:
                    downloaded_file = media_files[0]
                    logger.info(
                        f"Файл найден через поиск в директории: {downloaded_file}"
                    )
                else:
                    if is_instagram_story_url(url):
                        raise Exception(
                            "Не удалось скачать Instagram Story. "
                            "Stories — это временный контент (24 часа), "
                            "и Instagram ограничивает их загрузку через API. "
                            "К сожалению, скачивание Stories в данный момент не поддерживается."
                        )
                    raise Exception(
                        "Файл не был загружен, хотя ydl.extract_info завершился."
                    )

            downloaded_file = _ensure_ios_compatible_video(
                downloaded_file, session_id, "Instagram"
            )

            record_delivered_format(session_id, actual_info.get("format_id"))
            return finalize_downloaded_file(downloaded_file, force_local)

    # Сначала пробуем без cookies
    try:
        logger.info("Пробуем скачать Instagram видео без cookies.")
        return _download(False)
    except Exception as e:
        error_msg = str(e).lower()
        logger.warning(f"Ошибка скачивания без cookies: {e}")
        if _is_instagram_no_video_error(error_msg):
            raise Exception(
                "Instagram фото-пост нужно отправлять как набор изображений и отдельное аудио."
            ) from e

        # Проверяем на специфичные ошибки Instagram, требующие авторизации
        if any(
            keyword in error_msg
            for keyword in [
                "rate-limit",
                "login required",
                "not available",
                "sign in",
                "private",
            ]
        ):
            # Пробуем с файлом cookies
            if INSTAGRAM_COOKIES_FILE.exists():
                try:
                    logger.info("Пробуем скачать с cookies файлом...")
                    return _download(True)
                except Exception as e_cookie:
                    if _is_instagram_no_video_error(str(e_cookie)):
                        raise Exception(
                            "Instagram фото-пост нужно отправлять как набор изображений и отдельное аудио."
                        ) from e_cookie
                    logger.error(f"Ошибка скачивания даже с cookies: {e_cookie}")
                    raise Exception(
                        "Instagram ограничил доступ к этому контенту даже с авторизацией. "
                        "Возможные причины:\n"
                        "• Превышен лимит запросов (rate-limit)\n"
                        "• Контент требует специальной авторизации\n"
                        "• Региональные ограничения\n"
                        "• Приватный аккаунт\n\n"
                        "Попробуйте:\n"
                        "• Подождать 5-10 минут перед повторной попыткой\n"
                        "• Обновить файл cookies в `.secrets/www.instagram.com_cookies.txt`\n"
                        "• Использовать другую ссылку\n"
                        "• Проверить, что контент публичный"
                    ) from e_cookie
            else:
                raise Exception(
                    "Instagram ограничил доступ к этому контенту. "
                    "Для скачивания требуется авторизация. "
                    "Добавьте файл cookies в `.secrets/www.instagram.com_cookies.txt`."
                ) from e
        else:
            # Для других ошибок пробуем с файлом cookies
            if INSTAGRAM_COOKIES_FILE.exists():
                try:
                    logger.info("Пробуем скачать с cookies файлом для других ошибок...")
                    return _download(True)
                except Exception as e_cookie:
                    if _is_instagram_no_video_error(str(e_cookie)):
                        raise Exception(
                            "Instagram фото-пост нужно отправлять как набор изображений и отдельное аудио."
                        ) from e_cookie
                    logger.error(f"Ошибка скачивания даже с cookies: {e_cookie}")
                    raise
            else:
                raise


def get_available_formats_tiktok(video_info: dict) -> dict:
    """
    Получает список доступных форматов TikTok-видео.
    Args:
        video_info (dict): Информация о видео (yt-dlp extract_info).
    Returns:
        dict: Словарь с группами форматов (video_only, audio_only, combined).
    """
    if _is_tiktok_photo_post_info(video_info):
        return {
            "video_only": [],
            "audio_only": [],
            "combined": [],
        }

    formats = video_info.get("formats", [])
    video_formats = []
    audio_formats = []
    combined_formats = []
    for format_info in formats:
        if not format_info.get("height") and not format_info.get("audio_channels"):
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
                    "filesize": format_info.get("filesize"),
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
                    "filesize": format_info.get("filesize"),
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
                    "filesize": format_info.get("filesize"),
                    "type": "combined",
                }
            )
    video_formats.sort(key=lambda x: x.get("height", 0), reverse=True)
    audio_formats.sort(key=lambda x: x.get("filesize", 0), reverse=True)
    combined_formats.sort(key=lambda x: x.get("height", 0), reverse=True)
    return {
        "video_only": video_formats,
        "audio_only": audio_formats,
        "combined": combined_formats,
    }


def download_tiktok_audio(
    url: str,
    session_id: str,
    output_dir: Path | None = None,
    force_local: bool = False,
    cached_info: dict[str, Any] | None = None,
) -> Path | str:
    """
    Скачивает только аудио из TikTok видео в нативном формате M4A (AAC).
    Приоритет: M4A (нативный) > MP3 (fallback при конвертации).

    Args:
        url: URL TikTok видео
        session_id: ID сессии
        output_dir: Директория для сохранения
        force_local: Принудительное локальное сохранение
        cached_info: Кэшированные метаданные

    Returns:
        Путь к локальному M4A-файлу.
    """
    logger.info(f"Скачивание нативного аудио (M4A) из TikTok: {url}")

    resolved_url = _resolve_tiktok_url(url)
    if _is_tiktok_photo_post_info(cached_info) or is_tiktok_photo_url(resolved_url):
        logger.info("Определён TikTok фото-пост, скачиваем только аудио")
        return download_tiktok_photo_audio(
            url, session_id, output_dir, force_local, cached_info
        )

    if TIKTOK_FAST_PATH:
        try:
            return download_tiktok_audio_fast(
                url, session_id, output_dir, force_local, resolved_url=resolved_url
            )
        except FileSizeLimitError:
            raise
        except Exception as fast_error:
            logger.warning(
                "Быстрый путь аудио TikTok не сработал (%s), используем yt-dlp",
                fast_error,
            )

    if output_dir is None:
        output_path_template = get_temp_file_path(session_id, "%(title)s.%(ext)s")
    else:
        output_path_template = output_dir / "%(title)s.%(ext)s"

    def _download_audio_with_config(use_cookies: bool, config: dict) -> Path | str:
        """Скачивание аудио с указанной конфигурацией"""
        # Сначала получаем метаданные без скачивания для анализа форматов
        meta_opts = config.copy()
        meta_opts["quiet"] = True
        meta_opts.update(output_capture_opts(session_id))
        meta_cookiefile = _tiktok_cookiefile(use_cookies)
        if meta_cookiefile:
            meta_opts["cookiefile"] = meta_cookiefile

        with yt_dlp.YoutubeDL(meta_opts) as ydl:
            info = ydl.extract_info(resolved_url, download=False)

        formats = info.get("formats", [])
        audio_formats = [
            f for f in formats
            if f.get("acodec") != "none" and f.get("format_id")
        ]

        audio_formats.sort(key=_audio_format_sort_key, reverse=True)

        # Если форматы не обнаружены, используем дефолтный bestaudio/best
        if not audio_formats:
            audio_formats = [{"format_id": "bestaudio/best"}]

        last_err = None
        failed_tbrs = set()
        for fmt in audio_formats:
            fmt_id = fmt["format_id"]
            tbr = fmt.get("tbr")
            if tbr and tbr in failed_tbrs:
                logger.info(f"Пропускаем дубликат формата TikTok аудио {fmt_id} с битрейтом {tbr}, так как он уже признан беззвучным.")
                continue

            logger.info(f"Попытка скачать TikTok аудио формат: {fmt_id}")

            opts = config.copy()
            opts["outtmpl"] = str(output_path_template)
            opts["format"] = fmt_id
            opts["overwrites"] = True
            opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "m4a",  # M4A для нативного AAC
                    "preferredquality": "192",
                }
            ]
            opts["quiet"] = False
            opts.update(output_capture_opts(session_id))

            download_cookiefile = _tiktok_cookiefile(use_cookies)
            if download_cookiefile:
                opts["cookiefile"] = download_cookiefile

            downloaded_file = None
            base_filename = None
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info_download = ydl.extract_info(resolved_url, download=True)
                    base_filename = Path(ydl.prepare_filename(info_download))
                    downloaded_file = base_filename.with_suffix(".m4a")

                    if not downloaded_file.exists():
                        raise Exception("Аудио файл не был создан.")

                    record_delivered_format(session_id, info_download.get("format_id"))
                    return finalize_downloaded_file(downloaded_file, force_local)
            except Exception as e:
                err_str = str(e)
                if "unable to obtain file audio codec" in err_str or "Postprocessing" in err_str:
                    logger.warning(
                        f"Формат {fmt_id} не содержит аудио или вызвал ошибку постпроцессора: {e}. "
                        "Пробуем следующий формат."
                    )
                    last_err = e
                    if tbr:
                        failed_tbrs.add(tbr)
                    # Очищаем временные файлы
                    if base_filename:
                        for ext in (".mp4", ".webm", ".m4a", ".temp", ".part"):
                            p = base_filename.with_suffix(ext)
                            if p.exists():
                                try:
                                    p.unlink()
                                except Exception as ue:
                                    logger.warning(f"Не удалось удалить временный файл {p}: {ue}")
                    continue
                else:
                    raise

        if last_err:
            raise last_err
        raise Exception("Не найдено подходящих аудио форматов для скачивания.")

    # Получаем конфигурации и пробуем скачать
    configurations = _get_tiktok_base_configs()
    use_cookies_first = TIKTOK_COOKIES_FILE.exists()

    for attempt, config in enumerate(configurations, 1):
        try:
            # Сначала с cookies
            if use_cookies_first:
                try:
                    logger.info(
                        f"Аудио M4A: конфигурация {attempt}/{len(configurations)} с cookies"
                    )
                    return _smart_retry(
                        lambda: _download_audio_with_config(True, config),
                        max_attempts=2,
                        context=f"TikTok audio M4A (config {attempt}, с cookies)",
                    )
                except (CriticalExtractorError, RateLimitError):
                    raise
                except Exception as e:
                    logger.warning(f"Конфигурация {attempt} с cookies неудачна: {e}")

            # Затем без cookies
            logger.info(
                f"Аудио M4A: конфигурация {attempt}/{len(configurations)} без cookies"
            )
            return _smart_retry(
                lambda: _download_audio_with_config(False, config),
                max_attempts=2,
                context=f"TikTok audio M4A (config {attempt}, без cookies)",
            )
        except (CriticalExtractorError, RateLimitError) as e:
            logger.error(
                f"Прерываем обход конфигураций из-за критической ошибки при скачивании аудио: {e}"
            )
            raise
        except Exception as e:
            logger.warning(f"Конфигурация {attempt} неудачна: {e}")
            if attempt == len(configurations):
                raise Exception(
                    f"Не удалось скачать аудио после всех попыток. Последняя ошибка: {e}"
                ) from e
            continue

    raise Exception("Не удалось скачать TikTok аудио")


def download_instagram_audio(
    url: str,
    session_id: str,
    output_dir: Path | None = None,
    force_local: bool = False,
    cached_info: dict[str, Any] | None = None,
) -> Path | str:
    """
    Скачивает только аудио из Instagram видео в нативном формате M4A (AAC).
    Приоритет: M4A с copy (без перекодирования) > MP3 (fallback).

    Args:
        url (str): URL Instagram видео.
        session_id (str): Идентификатор сессии.
        output_dir (Optional[Path]): Директория для сохранения.
        force_local (bool): Принудительное локальное сохранение.

    Returns:
        Path: Путь к локальному M4A-файлу.
    """
    import subprocess

    logger.info(f"Скачивание нативного аудио (M4A) из Instagram: {url}")

    if _is_instagram_photo_post_info(
        cached_info
    ) or _is_instagram_empty_playlist_result(cached_info):
        logger.info("Определён Instagram фото-пост, скачиваем только аудио")
        return download_instagram_photo_audio(
            url, session_id, output_dir, force_local, cached_info
        )

    # Сначала скачиваем видео
    video_file = download_instagram_video(
        url, session_id, output_dir, force_local=True, cached_info=cached_info
    )

    # Если получили ссылку вместо файла, возвращаем её
    if isinstance(video_file, str) and video_file.startswith("http"):
        return video_file

    # Извлекаем аудио с помощью ffmpeg
    video_path = Path(video_file)
    audio_path_m4a = video_path.with_suffix(".m4a")

    try:
        logger.info(
            f"Извлечение нативного AAC аудио из {video_path} в {audio_path_m4a}"
        )

        # Сначала пробуем извлечь AAC без перекодирования (copy)
        cmd_copy = [
            "ffmpeg",
            "-i",
            str(video_path),
            "-vn",  # Без видео
            "-acodec",
            "copy",  # Копируем аудио без перекодирования
            "-y",  # Перезаписать файл если существует
            str(audio_path_m4a),
        ]

        result = subprocess.run(cmd_copy, capture_output=True, text=True)

        # Если copy не сработал (не AAC кодек), конвертируем в AAC
        if result.returncode != 0:
            logger.warning(
                f"Извлечение AAC через copy не удалось, конвертируем в AAC: {result.stderr}"
            )
            cmd_convert = [
                "ffmpeg",
                "-i",
                str(video_path),
                "-vn",  # Без видео
                "-acodec",
                "aac",  # Кодек AAC
                "-b:a",
                "192k",  # Битрейт 192k
                "-y",  # Перезаписать файл если существует
                str(audio_path_m4a),
            ]

            result_convert = subprocess.run(cmd_convert, capture_output=True, text=True)

            if result_convert.returncode != 0:
                logger.error(f"Ошибка конвертации в AAC: {result_convert.stderr}")
                # Fallback на MP3
                logger.warning("Fallback на MP3...")
                audio_path_mp3 = video_path.with_suffix(".mp3")
                cmd_mp3 = [
                    "ffmpeg",
                    "-i",
                    str(video_path),
                    "-vn",
                    "-acodec",
                    "mp3",
                    "-ab",
                    "192k",
                    "-y",
                    str(audio_path_mp3),
                ]
                result_mp3 = subprocess.run(cmd_mp3, capture_output=True, text=True)
                if result_mp3.returncode != 0:
                    raise Exception(
                        f"Не удалось извлечь аудио даже в MP3: {result_mp3.stderr}"
                    )
                audio_path = audio_path_mp3
                logger.info(f"Аудио извлечено в MP3 (fallback): {audio_path}")
            else:
                audio_path = audio_path_m4a
                logger.info(f"Аудио сконвертировано в AAC M4A: {audio_path}")
        else:
            audio_path = audio_path_m4a
            logger.info(f"Нативное AAC аудио извлечено (copy): {audio_path}")

        # Удаляем исходное видео
        try:
            video_path.unlink()
            logger.info(f"Исходное видео {video_path} удалено")
        except Exception as e:
            logger.warning(f"Не удалось удалить исходное видео: {e}")

        if not audio_path.exists():
            raise Exception("Аудио файл не был создан.")

        return finalize_downloaded_file(audio_path, force_local)

    except Exception as e:
        # Если что-то пошло не так, удаляем временные файлы
        try:
            if video_path.exists():
                video_path.unlink()
            if audio_path_m4a.exists():
                audio_path_m4a.unlink()
        except Exception:
            pass

        # Проверяем на специфичные ошибки Instagram
        error_msg = str(e).lower()
        if any(
            keyword in error_msg
            for keyword in ["rate-limit", "login required", "not available", "sign in"]
        ):
            raise Exception(
                "Instagram ограничил доступ к этому контенту. "
                "Возможные причины:\n"
                "• Превышен лимит запросов (rate-limit)\n"
                "• Контент требует авторизации\n"
                "• Региональные ограничения\n"
                "• Приватный аккаунт\n\n"
                "Попробуйте:\n"
                "• Подождать некоторое время перед повторной попыткой\n"
                "• Использовать другую ссылку\n"
                "• Проверить, что контент публичный"
            ) from e
        else:
            raise


def handle_instagram_audio_url(url: str) -> str:
    """
    Обрабатывает Instagram аудио ссылки и возвращает информативное сообщение.

    Args:
        url (str): URL Instagram аудио.

    Returns:
        str: Информативное сообщение об ограничениях.
    """
    logger.info(f"Обработка Instagram аудио ссылки: {url}")

    # Извлекаем ID аудио из URL
    audio_id_match = re.search(r"/audio/(\d+)", url)
    audio_id = audio_id_match.group(1) if audio_id_match else "неизвестен"

    return f"""🎵 **Instagram Audio - Ограничения**

К сожалению, прямое скачивание аудио по ссылкам вида `/reels/audio/` не поддерживается.

**ID аудио:** `{audio_id}`

**Что можно сделать:**
1. 🔍 Найдите конкретный пост/reel, который использует это аудио
2. 📱 Отправьте мне ссылку на пост (например: `instagram.com/p/...` или `instagram.com/reel/...`)
3. 🎧 Я смогу извлечь аудио из видео поста

**Поддерживаемые форматы ссылок:**
• `instagram.com/p/ABC123/` - обычные посты
• `instagram.com/reel/ABC123/` - reels
• `instagram.com/username/reel/ABC123/` - reels пользователя

**Альтернативные инструменты:**
Для скачивания аудио по таким ссылкам можно использовать специализированные инструменты, такие как gallery-dl или instaloader."""
