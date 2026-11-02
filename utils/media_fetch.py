"""Скачивание по ссылке без Telegram для HTTP API.

Решения те же, что у основных кнопок бота «видео» и «аудио»: тот же выбор
формата YouTube, те же быстрые пути TikTok и Instagram, тот же формат VK.
Модуль не знает о сессиях Telegram, кэше file_id и доставке по URL: на входе
ссылка и тип, на выходе локальный файл в `TEMP_DIR/<session_id>`.

Разбор ссылки и загрузка разделены, чтобы у загрузки был свой срок: VK отдаёт
многогигабайтные записи, и бот даёт им больше времени по размеру формата.
"""

import functools
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from config import BLOCKING_TASK_TIMEOUT, MAX_FILE_SIZE
from utils import rutube_vk_utils, tiktok_instagram_utils, work_budget, youtube_utils
from utils.logger import setup_logger
from utils.public_errors import is_connectivity_failure
from utils.tg_video_choice import (
    select_audio_format,
    select_tg_video_format,
    select_unsized_video_format,
)

logger = setup_logger(__name__)

MEDIA_TYPES = ("video", "audio")
URL_MAX_LENGTH = 2048

# Адрес одной публикации: хост и путь целиком. Шаблоны бота привязаны только к
# началу строки и пропускают каналы и профили (`youtube.com/SomeChannel`,
# `tiktok.com/@user`), а их разбор в yt-dlp идёт по всем роликам и занял бы
# воркер API надолго, без точки отмены.
_YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_VK_HOSTS = frozenset({
    "vk.com", "www.vk.com", "m.vk.com", "vk.ru", "www.vk.ru", "vkvideo.ru", "www.vkvideo.ru",
})
_SINGLE_MEDIA_ROUTES: dict[str, tuple[tuple[frozenset[str], re.Pattern[str]], ...]] = {
    "youtube": (
        (frozenset({"youtube.com", "www.youtube.com"}), re.compile(r"/shorts/[A-Za-z0-9_-]{11}")),
        (frozenset({"youtu.be"}), re.compile(r"/[A-Za-z0-9_-]{11}")),
    ),
    "tiktok": (
        (
            frozenset({"tiktok.com", "www.tiktok.com"}),
            re.compile(r"/@[A-Za-z0-9_.]+/(?:video|photo)/[0-9]+|/t/[A-Za-z0-9]+"),
        ),
        (frozenset({"vm.tiktok.com", "vt.tiktok.com"}), re.compile(r"/[A-Za-z0-9]+")),
    ),
    "instagram": (
        (
            frozenset({"instagram.com", "www.instagram.com"}),
            re.compile(
                r"(?:/[A-Za-z0-9_.]+)?/(?:p|reels?|tv)/[A-Za-z0-9_-]+"
                r"|/stories/[A-Za-z0-9_.]+/[0-9]+"
            ),
        ),
    ),
    "rutube": (
        (
            frozenset({"rutube.ru", "www.rutube.ru"}),
            re.compile(r"/video/(?:private/)?[A-Za-z0-9]+|/shorts/[A-Za-z0-9]+|/play/embed/[A-Za-z0-9]+"),
        ),
    ),
    "vk": (
        (_VK_HOSTS, re.compile(r"/(?:video|clip|live|wall)-?[0-9]+_[0-9]+")),
    ),
}
_VK_VIDEO_PARAM = re.compile(r"(?:video|clip)-?[0-9]+_[0-9]+")
_VK_EMBED_ID = re.compile(r"-?[0-9]+")


class UnsupportedMediaError(ValueError):
    """Публикация не является одним видео или аудио: фото, карусель, эфир."""


class NoSuitableFormatError(ValueError):
    """У ролика нет формата в пределах лимитов."""


@dataclass(frozen=True)
class PreparedDownload:
    """Разобранная ссылка: что запустить и сколько ждать."""

    run: Callable[[], Path | str]
    timeout: float = BLOCKING_TASK_TIMEOUT


def _single_media_platform(host: str, path: str, query: str) -> str | None:
    """Платформа, если адрес ведёт на одну публикацию, иначе None."""
    if path.endswith("/"):
        path = path[:-1]
    params = parse_qs(query)
    if host in {"youtube.com", "www.youtube.com"} and path == "/watch":
        video_ids = params.get("v", [])
        return "youtube" if len(video_ids) == 1 and _YOUTUBE_ID.fullmatch(video_ids[0]) else None
    if host in _VK_HOSTS:
        # Ролик в окне поверх страницы (`?z=video-1_2`) и встроенный плеер.
        if any(_VK_VIDEO_PARAM.match(value) for value in params.get("z", [])):
            return "vk"
        if path == "/video_ext.php":
            oid, video_id = params.get("oid", [""])[0], params.get("id", [""])[0]
            return "vk" if _VK_EMBED_ID.fullmatch(oid) and _VK_EMBED_ID.fullmatch(video_id) else None
    for platform, routes in _SINGLE_MEDIA_ROUTES.items():
        if any(host in hosts and pattern.fullmatch(path) for hosts, pattern in routes):
            return platform
    return None


def detect_platform(url: str) -> str | None:
    """Возвращает платформу ссылки либо None, если она не поддерживается.

    Принимается только адрес одной публикации, без каналов, профилей и
    плейлистов. Схема обязательна, учётные данные и порт в адресе запрещены:
    строка уходит в yt-dlp и HTTP-клиенты, и хост, который видят они, должен
    совпадать с тем, что проверен здесь.
    """
    if len(url) > URL_MAX_LENGTH or any(ch.isspace() or ord(ch) < 32 for ch in url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port is not None
    ):
        return None
    return _single_media_platform(parts.hostname, parts.path, parts.query)


def _prepare_youtube(url: str, media_type: str, session_id: str) -> PreparedDownload:
    info = youtube_utils.get_video_info(url, session_id=session_id)
    formats = youtube_utils.get_available_formats(info)
    if media_type == "audio":
        audio_format, is_native = select_audio_format(formats.get("audio_only", []))
        if audio_format is None:
            raise NoSuitableFormatError("У ролика нет аудиодорожки")
        if is_native:
            return PreparedDownload(functools.partial(
                youtube_utils.download_audio_native, url, audio_format["format_id"], session_id,
            ))
        return PreparedDownload(functools.partial(
            youtube_utils.download_audio, url, audio_format["format_id"], session_id,
            preferred_codec="m4a",
        ))

    combined = formats.get("combined", [])
    choice = select_tg_video_format(
        formats.get("video_only", []),
        formats.get("audio_only", []),
        combined,
        work_budget.size_limit(MAX_FILE_SIZE),
    )
    fallback = select_unsized_video_format(combined)
    if choice is None and fallback is None:
        raise NoSuitableFormatError("Нет формата в пределах лимита размера")

    def run() -> Path | str:
        if choice is not None:
            try:
                return youtube_utils.download_video(url, choice.format_id, session_id)
            except Exception as error:
                # Как у бота: при отказе выбранного формата пробуем формат без
                # известного размера. Сбой сети этим не лечится.
                if fallback is None or is_connectivity_failure(error):
                    raise
                logger.warning(
                    "Формат %s не скачался, пробуем резервный %s: %s",
                    choice.format_id, fallback.get("format_id"), error,
                )
        return youtube_utils.download_video(url, fallback["format_id"], session_id)

    return PreparedDownload(run)


def _prepare_tiktok(url: str, media_type: str, session_id: str) -> PreparedDownload:
    # Короткая ссылка может вести на профиль: проверяется её конечный адрес.
    url = tiktok_instagram_utils.resolve_tiktok_url(url)
    if detect_platform(url) != "tiktok":
        raise UnsupportedMediaError("Ссылка TikTok ведёт не на публикацию")
    info = tiktok_instagram_utils.get_tiktok_info(url)
    if media_type == "audio":
        return PreparedDownload(functools.partial(
            tiktok_instagram_utils.download_tiktok_audio, url, session_id, None, False, info,
        ))
    if info.get("_nuvio_tiktok_photo_post"):
        raise UnsupportedMediaError("Фото-пост TikTok не является видео")
    return PreparedDownload(functools.partial(
        tiktok_instagram_utils.download_tiktok_video, url, session_id, None, False, info,
    ))


def _prepare_instagram(url: str, media_type: str, session_id: str) -> PreparedDownload:
    info = tiktok_instagram_utils.get_instagram_info(url)

    def refuse_photo_posts(download) -> Callable[[], Path | str]:
        # Аудио фото-поста загрузчик берёт сам, но пост, который yt-dlp видит
        # без видео, проваливается в видео-путь и отказывается как фото-пост.
        def run() -> Path | str:
            try:
                return download(url, session_id, None, False, info)
            except Exception as error:
                if tiktok_instagram_utils.is_photo_post_error(error):
                    raise UnsupportedMediaError("Пост Instagram не является видео") from error
                raise

        return run

    if media_type == "audio":
        return PreparedDownload(refuse_photo_posts(tiktok_instagram_utils.download_instagram_audio))
    if any(
        info.get(marker)
        for marker in (
            "_nuvio_instagram_photo_post",
            "_nuvio_instagram_mixed_post",
            "_nuvio_instagram_carousel_incomplete",
        )
    ):
        raise UnsupportedMediaError("Пост Instagram не является одним видео")

    return PreparedDownload(refuse_photo_posts(tiktok_instagram_utils.download_instagram_video))


def _prepare_rutube(url: str, media_type: str, session_id: str) -> PreparedDownload:
    rutube_vk_utils.get_rutube_info(url, session_id=session_id)
    download = (
        rutube_vk_utils.download_rutube_audio
        if media_type == "audio"
        else rutube_vk_utils.download_rutube_video
    )
    return PreparedDownload(functools.partial(download, url, session_id))


def _prepare_vk(url: str, media_type: str, session_id: str) -> PreparedDownload:
    try:
        info = rutube_vk_utils.get_vk_info(url, session_id=session_id)
    except rutube_vk_utils.VkLiveActiveError as error:
        raise UnsupportedMediaError("Трансляция VK ещё идёт") from error
    if media_type == "audio":
        return PreparedDownload(functools.partial(
            rutube_vk_utils.download_vk_audio, url, session_id,
        ))
    return PreparedDownload(
        functools.partial(
            rutube_vk_utils.download_vk_video, url, session_id,
            format_id=info.get("_nuvio_vk_format_id"),
        ),
        rutube_vk_utils.vk_download_timeout(info.get("_nuvio_vk_format_size")),
    )


_PREPARERS = {
    "youtube": _prepare_youtube,
    "tiktok": _prepare_tiktok,
    "instagram": _prepare_instagram,
    "rutube": _prepare_rutube,
    "vk": _prepare_vk,
}


def prepare_download(
    url: str, platform: str, media_type: str, session_id: str
) -> PreparedDownload:
    """Разбирает ссылку и выбирает формат. Блокирующий вызов для пула потоков."""
    if media_type not in MEDIA_TYPES:
        raise ValueError(f"Неизвестный тип медиа: {media_type}")
    return _PREPARERS[platform](url, media_type, session_id)
