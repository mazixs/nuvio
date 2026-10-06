"""Безопасная классификация ошибок без раскрытия внутреннего состояния."""

import errno

import httpx
from telegram import error as telegram_error

from config import MAX_VIDEO_DURATION
from utils.media_errors import DurationLimitError
from messages import (
    USER_DURATION_LIMIT_WITH_CODE,
    USER_ERROR_WITH_CODE,
    USER_FILE_TOO_LARGE_WITH_CODE,
    USER_MEDIA_PROCESSING_ERROR_WITH_CODE,
    USER_NETWORK_ERROR_WITH_CODE,
    USER_RATE_LIMIT_ERROR_WITH_CODE,
    USER_PLATFORM_ERROR_WITH_CODE,
    USER_STORY_UNSUPPORTED_WITH_CODE,
    USER_FORMAT_UNAVAILABLE_WITH_CODE,
    USER_MEDIA_FORBIDDEN_WITH_CODE,
    USER_SERVICE_ERROR_WITH_CODE,
    USER_TIMEOUT_ERROR_WITH_CODE,
    USER_FILE_ERROR_WITH_CODE,
    USER_TELEGRAM_ERROR_WITH_CODE,
    USER_MEDIA_UNAVAILABLE_WITH_CODE,
    USER_BLOCKED_MESSAGE,
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


def is_video_unavailable_error(error_msg: str) -> bool:
    """Распознает ответ о недоступности без предположений о его причине."""
    msg_lower = _confirmed_message(error_msg)
    return any(
        marker in msg_lower
        for marker in (
            "video unavailable",
            "this video is unavailable",
            "this video is not available",
        )
    )


def youtube_error_code(error_msg: str | BaseException) -> str:
    """Возвращает категорию частой ошибки YouTube/yt-dlp."""
    if type(error_msg).__name__ == "UserBlockedError":
        return "BLOCKED"
    if isinstance(error_msg, (DurationLimitError, OSError, httpx.HTTPError)):
        return _category_from_exception(error_msg, youtube=True)
    if type(error_msg).__name__ == "CookieLoadError":
        return "COOKIE"
    if type(error_msg).__name__ == "FileSizeLimitError":
        return "LARGE"
    if type(error_msg).__name__ == "RateLimitError":
        return "RATE_LIMIT"
    if isinstance(error_msg, telegram_error.Forbidden):
        return "TELEGRAM_ACCESS"
    if isinstance(error_msg, telegram_error.RetryAfter):
        return "RATE_LIMIT"
    if isinstance(error_msg, telegram_error.TimedOut | TimeoutError):
        return "NETWORK_TIMEOUT"
    if isinstance(error_msg, telegram_error.BadRequest):
        category = _category_from_message(str(error_msg).lower(), youtube=True)
        return category if category in {"RENDER", "LARGE"} else "API"
    if isinstance(error_msg, telegram_error.NetworkError):
        return "NETWORK"
    if category := _speculative_wrapper_category("youtube", error_msg):
        return category
    msg_lower = _confirmed_message(error_msg)
    if "requested format is not available" in msg_lower:
        return "FORMAT_UNAVAILABLE"
    if "http error 429" in msg_lower or "too many requests" in msg_lower:
        return "RATE_LIMIT"
    if is_media_forbidden_error(msg_lower):
        return "MEDIA_FORBIDDEN"
    if "can't parse entities" in msg_lower or "end of entity" in msg_lower:
        return "RENDER"
    if any(
        signature in msg_lower
        for signature in (
            "http error 403",
            "403 forbidden",
            "sign in to confirm your age",
            "login required",
            "private video",
            "sign in to confirm you",
        )
    ):
        return "ACCESS_RESTRICTED"
    if is_video_unavailable_error(msg_lower):
        return "UNAVAILABLE"
    if any(
        signature in msg_lower
        for signature in (
            "read timed out",
            "connection timed out",
            "timed out",
        )
    ):
        return "NETWORK_TIMEOUT"
    if "ffmpeg" in msg_lower and any(
        signature in msg_lower
        for signature in ("not found", "is not installed")
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
    if "видео слишком длинное" in msg_lower:
        return "DURATION"
    if "превысил лимит времени" in msg_lower:
        return "TIMEOUT"
    if "no space left on device" in msg_lower or "disk quota exceeded" in msg_lower:
        return "STORAGE"
    if "[errno 13]" in msg_lower and "permission denied" in msg_lower:
        return "FILE_ACCESS"
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
            "network is unreachable",
            "connection reset",
            "connection refused",
            "connection aborted",
            "temporary failure in name resolution",
            "name or service not known",
            "ssl: certificate_verify_failed",
            "ssl: unexpected_eof_while_reading",
            "eof occurred in violation of protocol",
            "unexpected eof while reading",
        )
    ):
        return "NETWORK_TIMEOUT" if youtube and "timed out" in msg_lower else "NETWORK"
    if "ffmpeg" in msg_lower or "ffprobe" in msg_lower:
        if any(
            marker in msg_lower
            for marker in ("not found", "not installed", "не найден", "не установлен")
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
            "размер удаленного файла превышает лимит в",
            "размер файла превысил лимит в",
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
            "empty media response",
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
    if isinstance(error, DurationLimitError):
        return "DURATION"
    if isinstance(error, telegram_error.RetryAfter):
        return "RATE_LIMIT"
    if isinstance(error, telegram_error.Forbidden):
        return "TELEGRAM_ACCESS"
    if isinstance(error, telegram_error.TimedOut | TimeoutError):
        return "NETWORK_TIMEOUT" if youtube else "TIMEOUT"
    if isinstance(error, telegram_error.BadRequest):
        return "API"
    if isinstance(error, telegram_error.NetworkError):
        return "NETWORK"
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
        return "NETWORK"
    if isinstance(error, PermissionError):
        return "FILE_ACCESS"
    if isinstance(error, FileNotFoundError):
        return "FILE"
    if isinstance(error, OSError):
        if error.errno in {errno.ENOSPC, errno.EDQUOT}:
            return "STORAGE"
        if error.errno in {errno.ECONNRESET, errno.ENETUNREACH, errno.ETIMEDOUT}:
            if error.errno == errno.ETIMEDOUT:
                return "NETWORK_TIMEOUT" if youtube else "TIMEOUT"
            return "NETWORK"
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


def _confirmed_message(error: str | BaseException) -> str:
    """Не принимает список предположений загрузчика за подтвержденную причину."""
    return str(error).lower().split("возможные причины:", 1)[0]


def _speculative_wrapper_category(
    platform: str, error: str | BaseException
) -> str | None:
    """В диагностической обертке сохраняет причину исходного исключения."""
    if (
        isinstance(error, BaseException)
        and error.__cause__ is not None
        and "возможные причины:" in str(error).lower()
    ):
        return classify_internal_error_category(platform, error.__cause__)
    return None


def classify_internal_error_category(
    platform: str, error_msg: str | BaseException
) -> str:
    """Классифицирует внутреннюю ошибку без возврата её текста пользователю."""
    if type(error_msg).__name__ == "UserBlockedError":
        return "BLOCKED"
    if platform == "youtube":
        return youtube_error_code(error_msg)
    if isinstance(error_msg, (DurationLimitError, OSError, httpx.HTTPError)):
        return _category_from_exception(error_msg)
    if type(error_msg).__name__ == "CookieLoadError":
        return "COOKIE"
    if type(error_msg).__name__ == "FileSizeLimitError":
        return "LARGE"
    if type(error_msg).__name__ == "RateLimitError":
        return "RATE_LIMIT"
    if isinstance(error_msg, telegram_error.RetryAfter):
        return "RATE_LIMIT"
    if isinstance(error_msg, telegram_error.TimedOut | TimeoutError):
        return "TIMEOUT"
    if isinstance(error_msg, telegram_error.Forbidden):
        return "TELEGRAM_ACCESS"
    if category := _speculative_wrapper_category(platform, error_msg):
        return category
    msg_lower = _confirmed_message(error_msg)
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
            "sign in to",
            "private video",
            "private account",
            "private post",
            "403 forbidden",
            "video unavailable",
            "video is unavailable",
            "post is unavailable",
            "this content isn't available",
            # Для недоступного поста нужен явный ответ платформы.
            # Один HTTP 400 не подтверждает удаление или приватность.
            "not granting access",
            "media not found",
            "ограничил доступ",
            "видео недоступно",
            "пост недоступен",
            "материал недоступен",
            "http error 401",
            "http error 403",
        )
    ):
        return "ACCESS"
    if platform == "instagram" and "http error 400" in msg_lower:
        return "API"
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
    if category == "BLOCKED":
        return USER_BLOCKED_MESSAGE
    if category == "DURATION":
        max_duration = (
            error_msg.max_duration
            if isinstance(error_msg, DurationLimitError)
            else MAX_VIDEO_DURATION
        )
        return USER_DURATION_LIMIT_WITH_CODE.format(
            max_minutes=max_duration // 60, error_code=error_code
        )
    if category == "FORMAT_UNAVAILABLE":
        return USER_FORMAT_UNAVAILABLE_WITH_CODE.format(error_code=error_code)
    if category == "MEDIA_FORBIDDEN":
        return USER_MEDIA_FORBIDDEN_WITH_CODE.format(error_code=error_code)
    if category == "UNAVAILABLE":
        return USER_MEDIA_UNAVAILABLE_WITH_CODE.format(error_code=error_code)
    if category in {"NETWORK_TIMEOUT", "TIMEOUT"}:
        return USER_TIMEOUT_ERROR_WITH_CODE.format(error_code=error_code)
    if category == "NETWORK":
        return USER_NETWORK_ERROR_WITH_CODE.format(error_code=error_code)
    if category == "RATE_LIMIT":
        return USER_RATE_LIMIT_ERROR_WITH_CODE.format(error_code=error_code)
    if category == "LARGE":
        return USER_FILE_TOO_LARGE_WITH_CODE.format(error_code=error_code)
    if category == "STORY_UNSUPPORTED":
        return USER_STORY_UNSUPPORTED_WITH_CODE.format(error_code=error_code)
    if category in {
        "STORAGE", "IO", "FILE_ACCESS", "COOKIE", "FFMPEG", "FFMPEG_MISSING",
        "EXTRACTOR_RUNTIME", "RENDER", "ROUTE",
    }:
        return USER_SERVICE_ERROR_WITH_CODE.format(error_code=error_code)
    if category == "FILE":
        return USER_FILE_ERROR_WITH_CODE.format(error_code=error_code)
    if category == "TELEGRAM_ACCESS" or (
        category == "API" and (
            platform == "telegram" or isinstance(error_msg, telegram_error.TelegramError)
        )
    ):
        return USER_TELEGRAM_ERROR_WITH_CODE.format(error_code=error_code)
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
