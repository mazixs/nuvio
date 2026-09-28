"""Безопасная классификация ошибок без раскрытия внутреннего состояния."""

import errno

import httpx
from telegram import error as telegram_error

from messages import (
    CHOOSE_ANOTHER_FORMAT,
    USER_ERROR_WITH_CODE,
    USER_FILE_TOO_LARGE_WITH_CODE,
    USER_MEDIA_PROCESSING_ERROR_WITH_CODE,
    USER_NETWORK_ERROR_WITH_CODE,
    USER_RATE_LIMIT_ERROR_WITH_CODE,
    USER_PLATFORM_ERROR_WITH_CODE,
    USER_STORY_UNSUPPORTED_WITH_CODE,
)


# Маркеры, по которым 403 на медиафайле отличается от запрета доступа к видео.
# CDN отказал по уже выданной ссылке, то есть видео не закрыто, а не отдаётся
# конкретный поток: так выглядит и протухшая ссылка, и смена правил выдачи на
# стороне платформы. Первое лечится повторным разбором, второе — обновлением
# yt-dlp, и различает их только серия таких отказов, поэтому категория обязана
# доходить до админов (см. `_should_notify_admins_platform_failure`).
_MEDIA_FORBIDDEN_MARKERS = (
    "unable to download video data",
    "fragment",
    "giving up after",
)


def is_media_forbidden_error(error_msg: str) -> bool:
    """Отличает 403 на самом медиафайле от запрета доступа к видео."""
    msg_lower = error_msg.lower()
    if "http error 403" not in msg_lower:
        return False
    return any(marker in msg_lower for marker in _MEDIA_FORBIDDEN_MARKERS)


def youtube_error_code(error_msg: str | BaseException) -> str:
    """Возвращает категорию частой ошибки YouTube/yt-dlp."""
    if isinstance(error_msg, telegram_error.RetryAfter):
        return "RATE_LIMIT"
    if isinstance(error_msg, telegram_error.TimedOut | TimeoutError):
        return "NETWORK_TIMEOUT"
    msg_lower = str(error_msg).lower()
    if "requested format is not available" in msg_lower:
        return "FORMAT_UNAVAILABLE"
    if "http error 429" in msg_lower or "too many requests" in msg_lower:
        return "RATE_LIMIT"
    if is_media_forbidden_error(str(error_msg)):
        return "MEDIA_FORBIDDEN"
    if "can't parse entities" in msg_lower or "end of entity" in msg_lower:
        return "RENDER"
    if any(
        signature in msg_lower
        for signature in (
            "http error 403",
            "forbidden",
            "sign in to confirm your age",
            "login required",
            "this video is unavailable",
            "private video",
            "sign in to confirm you",
        )
    ):
        return "ACCESS_RESTRICTED"
    if any(
        signature in msg_lower
        for signature in (
            "read timed out",
            "connection timed out",
            "timed out",
            "connection reset by peer",
            "unexpected_eof_while_reading",
            "eof occurred in violation of protocol",
            "network is unreachable",
        )
    ):
        return "NETWORK_TIMEOUT"
    if "ffmpeg" in msg_lower and any(
        signature in msg_lower
        for signature in ("not found", "is not installed", "ffprobe")
    ):
        return "FFMPEG_MISSING"
    if any(
        signature in msg_lower
        for signature in (
            "requires a javascript runtime",
            "nsig extraction failed",
            "signature extraction failed",
            "unable to extract initial player response",
            "remote components",
        )
    ):
        return "EXTRACTOR_RUNTIME"
    common_category = _category_from_message(msg_lower, youtube=True)
    if common_category != "UNEXPECT":
        return common_category
    if isinstance(error_msg, BaseException):
        return _category_from_exception(error_msg, youtube=True)
    return "UNEXPECT"


def _category_from_message(msg_lower: str, *, youtube: bool = False) -> str:
    """Разбирает общие подтвержденные признаки без догадок о причине сбоя."""
    if "can't parse entities" in msg_lower or "end of entity" in msg_lower:
        return "RENDER"
    if any(
        marker in msg_lower
        for marker in (
            "http error 429",
            "rate-limit",
            "too many requests",
            "лимит запросов",
        )
    ):
        return "RATE_LIMIT"
    if any(
        marker in msg_lower
        for marker in (
            "timed out",
            "network",
            "connection reset",
            "ssl",
            "eof",
        )
    ):
        return "NETWORK_TIMEOUT" if youtube else "NETWORK"
    if "ffmpeg" in msg_lower or "ffprobe" in msg_lower:
        if any(
            marker in msg_lower
            for marker in ("not found", "not installed", "не найден")
        ):
            return "FFMPEG_MISSING"
        return "FFMPEG"
    if any(
        marker in msg_lower
        for marker in (
            "requested format is not available",
            "no video formats",
            "no formats found",
            "не найдено подходящих аудио форматов",
        )
    ):
        return "FORMAT_UNAVAILABLE"
    if any(
        marker in msg_lower
        for marker in (
            "file too large",
            "file is too big",
            "превышает допустимый размер",
            "превышает лимит telegram",
            "превышает лимит в",
            "превысил лимит в",
        )
    ):
        return "LARGE"
    if any(
        marker in msg_lower
        for marker in (
            "не удалось подтвердить полный состав",
            "не удалось проверить все элементы",
            "не вернул данные",
            "не вернул изображение",
            "не вернул изображения",
            "не был создан",
            "не удалось определить shortcode",
            "не удалось получить информацию о",
            "неизвестный тип элемента карусели",
        )
    ):
        return "DATA"
    if "не содержит аудиодорожки" in msg_lower:
        return "FORMAT_UNAVAILABLE"
    if "не удалось извлечь аудио" in msg_lower:
        return "FFMPEG"
    if "фото-пост нужно отправлять как набор изображений" in msg_lower:
        return "ROUTE"
    if any(
        marker in msg_lower
        for marker in (
            "не удалось скачать",
            "unable to download",
            "не был загружен",
            "не удалось получить изображения",
        )
    ):
        return "DOWNLOAD"
    return "UNEXPECT"


def _category_from_exception(error: BaseException, *, youtube: bool = False) -> str:
    """Использует тип исключения, когда сообщение не раскрывает причину."""
    if isinstance(error, telegram_error.RetryAfter):
        return "RATE_LIMIT"
    if isinstance(error, telegram_error.Forbidden):
        return "ACCESS_RESTRICTED" if youtube else "ACCESS"
    if isinstance(error, telegram_error.TimedOut | TimeoutError):
        return "NETWORK_TIMEOUT" if youtube else "TIMEOUT"
    if isinstance(error, telegram_error.BadRequest):
        return "API"
    if isinstance(error, telegram_error.NetworkError):
        return "NETWORK_TIMEOUT" if youtube else "NETWORK"
    if isinstance(error, httpx.TimeoutException):
        return "NETWORK_TIMEOUT" if youtube else "TIMEOUT"
    if isinstance(error, httpx.HTTPStatusError):
        status_code = error.response.status_code
        if status_code == 429:
            return "RATE_LIMIT"
        if status_code in {401, 403, 404}:
            return "ACCESS_RESTRICTED" if youtube else "ACCESS"
        if status_code == 413:
            return "LARGE"
        return "API"
    if isinstance(error, httpx.RequestError):
        return "NETWORK_TIMEOUT" if youtube else "NETWORK"
    if isinstance(error, PermissionError):
        return "ACCESS_RESTRICTED" if youtube else "ACCESS"
    if isinstance(error, FileNotFoundError):
        return "FILE"
    if isinstance(error, OSError):
        if error.errno in {errno.ENOSPC, errno.EDQUOT}:
            return "STORAGE"
        if error.errno in {errno.ECONNRESET, errno.ENETUNREACH, errno.ETIMEDOUT}:
            return "NETWORK_TIMEOUT" if youtube else "NETWORK"
        return "IO"
    if type(error).__name__ == "RateLimitError":
        return "RATE_LIMIT"
    if type(error).__name__ == "CriticalExtractorError":
        return "EXTRACTOR_RUNTIME"
    if type(error).__name__ == "FileSizeLimitError":
        return "LARGE"
    if type(error).__name__ == "DownloadError":
        return "DOWNLOAD"
    if type(error).__name__ == "CookieLoadError":
        return "COOKIE"
    if isinstance(error, (ValueError, KeyError)):
        return "DATA"
    return "UNEXPECT"


def classify_internal_error_category(
    platform: str, error_msg: str | BaseException
) -> str:
    """Классифицирует внутреннюю ошибку без возврата её текста пользователю."""
    if platform == "youtube":
        return youtube_error_code(error_msg)
    if isinstance(error_msg, telegram_error.RetryAfter):
        return "RATE_LIMIT"
    if isinstance(error_msg, telegram_error.TimedOut | TimeoutError):
        return "TIMEOUT"
    if isinstance(error_msg, telegram_error.Forbidden):
        return "ACCESS"
    msg_lower = str(error_msg).lower()
    if (
        platform == "instagram"
        and "story" in msg_lower
        and "не поддерживается" in msg_lower
    ):
        return "STORY_UNSUPPORTED"
    if isinstance(error_msg, telegram_error.BadRequest):
        if "can't parse entities" in msg_lower or "end of entity" in msg_lower:
            return "RENDER"
        if "file too large" in msg_lower or "file is too big" in msg_lower:
            return "LARGE"
        return "API"
    if isinstance(error_msg, telegram_error.NetworkError):
        return "NETWORK"
    common_category = _category_from_message(msg_lower)
    if common_category != "UNEXPECT":
        return common_category
    if any(
        signature in msg_lower
        for signature in (
            "login required",
            "sign in",
            "private",
            "forbidden",
            "blocked",
            "unavailable",
            # Формулировки yt-dlp для недоступного поста Instagram. Настоящий
            # ответ платформы (`{"message":"Media not found or unavailable"}`)
            # yt-dlp проглатывает, наружу отдавая только голый `HTTP Error 400`,
            # в котором ни одного признака выше нет. Из-за этого удалённый или
            # закрытый рил уходил в UNKNOWN, то есть в краш-репорт с трейсбеком
            # и побудкой админов: замерено пять `IG-UNKNOWN` при нуле
            # `IG-ACCESS` (ADR-002).
            "empty media response",
            "not granting access",
            "video info extraction failed",
            "media not found",
            "ограничил доступ",
            "ограничения",
            "блокировки",
            "авторизац",
            "недоступ",
            "http error 401",
            "http error 403",
        )
    ):
        return "ACCESS"
    if platform == "instagram" and "http error 400" in msg_lower:
        return "ACCESS"
    if isinstance(error_msg, BaseException):
        return _category_from_exception(error_msg)
    return "UNEXPECT"


def build_public_error_message(
    platform: str,
    error_code: str,
    error_msg: str | BaseException,
) -> str:
    """Строит безопасное пользовательское сообщение с диагностическим кодом."""
    category = classify_internal_error_category(platform, error_msg)
    if platform == "youtube" and category == "FORMAT_UNAVAILABLE":
        return CHOOSE_ANOTHER_FORMAT.format(error="Выбранный формат сейчас недоступен.")
    if category in {"NETWORK", "NETWORK_TIMEOUT", "TIMEOUT", "MEDIA_FORBIDDEN"}:
        return USER_NETWORK_ERROR_WITH_CODE.format(error_code=error_code)
    if category == "RATE_LIMIT":
        return USER_RATE_LIMIT_ERROR_WITH_CODE.format(error_code=error_code)
    if category == "LARGE":
        return USER_FILE_TOO_LARGE_WITH_CODE.format(error_code=error_code)
    if category == "STORY_UNSUPPORTED":
        return USER_STORY_UNSUPPORTED_WITH_CODE.format(error_code=error_code)
    platform_name = {
        "youtube": "YouTube",
        "tiktok": "TikTok",
        "instagram": "Instagram",
        "rutube": "Rutube",
        "vk": "VK Video",
    }.get(platform)
    if platform_name:
        template = (
            USER_PLATFORM_ERROR_WITH_CODE
            if category in {"ACCESS", "ACCESS_RESTRICTED"}
            else USER_MEDIA_PROCESSING_ERROR_WITH_CODE
        )
        return template.format(platform=platform_name, error_code=error_code)
    return USER_ERROR_WITH_CODE.format(error_code=error_code)
