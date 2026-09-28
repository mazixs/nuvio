"""Проверки категорий ошибок по причинам, типам исключений и безопасному ответу."""

import errno
import re
from datetime import timedelta

import pytest
import httpx
import telegram
import yt_dlp

from utils.public_errors import (
    build_public_error_message,
    classify_internal_error_category,
    youtube_error_code,
)
from utils.telegram_utils import (
    _make_error_code,
    _make_error_code_for_exception,
    _should_notify_admins_platform_failure,
)
from utils.tiktok_instagram_utils import CriticalExtractorError, RateLimitError
from utils.ytdlp_common import FileSizeLimitError, classify_download_error_kind


@pytest.mark.parametrize(
    ("platform", "error", "expected"),
    [
        ("instagram", "HTTP Error 400", "ACCESS"),
        ("instagram", "Instagram story не поддерживается", "STORY_UNSUPPORTED"),
        ("tiktok", "HTTP Error 429: Too Many Requests", "RATE_LIMIT"),
        ("tiktok", "FFmpeg is not installed", "FFMPEG_MISSING"),
        ("instagram", "не удалось подтвердить полный состав карусели", "DATA"),
        ("instagram", "file is too big", "LARGE"),
        ("rutube", "unrelated future failure", "UNEXPECT"),
    ],
)
def test_known_platform_failures_have_stable_categories(platform, error, expected):
    assert classify_internal_error_category(platform, error) == expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (telegram.error.RetryAfter(timedelta(seconds=5)), "RATE_LIMIT"),
        (telegram.error.TimedOut(), "TIMEOUT"),
        (telegram.error.Forbidden("chat blocked"), "ACCESS"),
        (telegram.error.BadRequest("Chat not found"), "API"),
        (telegram.error.BadRequest("Can't parse entities"), "RENDER"),
        (telegram.error.NetworkError("connection failed"), "NETWORK"),
        (FileNotFoundError("missing asset"), "FILE"),
        (PermissionError("access denied"), "ACCESS"),
        (OSError(errno.ENOSPC, "disk full"), "STORAGE"),
        (ValueError("malformed metadata"), "DATA"),
        (FileSizeLimitError("oversize"), "LARGE"),
        (CriticalExtractorError("opaque extractor failure"), "EXTRACTOR_RUNTIME"),
        (RateLimitError("opaque resolver failure"), "RATE_LIMIT"),
        (RuntimeError("future failure"), "UNEXPECT"),
    ],
)
def test_exception_types_preserve_known_error_class(error, expected):
    assert classify_internal_error_category("tiktok", error) == expected


def test_youtube_and_downloader_do_not_emit_unknown_category():
    assert youtube_error_code(yt_dlp.utils.DownloadError("opaque")) == "DOWNLOAD"
    assert youtube_error_code(yt_dlp.cookies.CookieLoadError("unreadable")) == "COOKIE"
    assert youtube_error_code(TimeoutError()) == "NETWORK_TIMEOUT"
    assert youtube_error_code(RuntimeError("future failure")) == "UNEXPECT"
    assert classify_download_error_kind("future failure") == "DOWNLOAD"


@pytest.mark.parametrize(
    ("platform", "message", "expected"),
    [
        ("tiktok", "Размер удаленного файла превышает лимит в 50 МБ", "LARGE"),
        ("instagram", "Файл не был загружен", "DOWNLOAD"),
        ("instagram", "Не удалось определить shortcode Instagram поста", "DATA"),
        ("tiktok", "Скачанный файл не содержит аудиодорожки", "FORMAT_UNAVAILABLE"),
        ("instagram", "Не удалось извлечь аудио даже в MP3", "FFMPEG"),
        ("tiktok", "Фото-пост нужно отправлять как набор изображений", "ROUTE"),
    ],
)
def test_existing_download_failures_have_specific_categories(
    platform, message, expected
):
    assert classify_internal_error_category(platform, Exception(message)) == expected


def test_httpx_status_and_transport_failures_keep_their_cause():
    request = httpx.Request("GET", "https://example.test/post")
    forbidden = httpx.HTTPStatusError(
        "request refused",
        request=request,
        response=httpx.Response(403, request=request),
    )
    rate_limit = httpx.HTTPStatusError(
        "request refused",
        request=request,
        response=httpx.Response(429, request=request),
    )
    assert classify_internal_error_category("instagram", forbidden) == "ACCESS"
    assert classify_internal_error_category("tiktok", rate_limit) == "RATE_LIMIT"
    assert (
        classify_internal_error_category("tiktok", httpx.ConnectError("refused"))
        == "NETWORK"
    )
    assert (
        classify_internal_error_category("youtube", httpx.ReadTimeout("slow"))
        == "NETWORK_TIMEOUT"
    )


def test_error_code_maps_legacy_unknown_to_explicit_unexpected_class():
    code = _make_error_code("bot", "UNKNOWN")
    assert re.fullmatch(r"BOT-UNEXPECT-[A-F0-9]{6}", code)


def test_delivery_code_uses_the_same_category_as_the_platform_log():
    youtube_timeout = TimeoutError("slow response")
    telegram_timeout = telegram.error.TimedOut()
    missing_file = FileNotFoundError("missing asset")

    assert re.fullmatch(
        r"TG-NETWORK_-[A-F0-9]{6}",
        _make_error_code_for_exception(
            "youtube", youtube_timeout, prefix_platform="telegram"
        ),
    )
    assert re.fullmatch(
        r"TG-TIMEOUT-[A-F0-9]{6}",
        _make_error_code_for_exception(
            "tiktok", telegram_timeout, prefix_platform="telegram"
        ),
    )
    assert re.fullmatch(
        r"FILE-FILE-[A-F0-9]{6}",
        _make_error_code_for_exception(
            "instagram", missing_file, prefix_platform="file"
        ),
    )
    assert not _should_notify_admins_platform_failure("tiktok", "TIMEOUT", "send_file")


def test_youtube_telegram_size_error_is_not_treated_as_api_failure():
    error = telegram.error.BadRequest("File is too big")
    assert classify_internal_error_category("youtube", error) == "LARGE"


def test_user_message_for_unexpected_error_does_not_invent_access_reason():
    secret = "cookie expired; internal path /app/.secrets/token"
    message = build_public_error_message(
        "instagram", "IG-UNEXPECT-ABC123", RuntimeError(secret)
    )
    assert "Instagram" in message
    assert "IG-UNEXPECT-ABC123" in message
    assert "авторизации" not in message
    assert "cookie" not in message.lower()
    assert "/app/" not in message


def test_known_access_and_size_errors_still_have_specific_user_messages():
    access = build_public_error_message(
        "instagram", "IG-ACCESS-ABC123", "login required"
    )
    oversize = build_public_error_message(
        "tiktok", "TT-LARGE-ABC123", FileSizeLimitError("file too large")
    )
    assert "авторизации" in access
    assert "лимит" in oversize
    assert "TT-LARGE-ABC123" in oversize
