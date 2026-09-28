"""Unit tests for public error classification in telegram_utils."""

from utils.telegram_utils import (
    _build_public_error_message,
    _classify_youtube_error,
    _youtube_error_code,
)
from utils.public_errors import build_public_error_message


def test_classify_requested_format_not_available():
    message = _classify_youtube_error("ERROR: Requested format is not available")
    assert message is not None
    assert "Выберите другой формат" in message


def test_classify_timeout_error():
    message = _classify_youtube_error("Read timed out while downloading")
    assert message == "🌐 Проблемы с сетью. Попробуйте позже."


def test_classify_ffmpeg_missing():
    message = _classify_youtube_error("ffmpeg is not installed")
    assert (
        message
        == "❌ FFmpeg не найден в системе. Установите FFmpeg и добавьте его в PATH."
    )


def test_classify_extractor_runtime_issue():
    message = _classify_youtube_error(
        "nsig extraction failed: requires a javascript runtime"
    )
    assert message is not None
    assert "extractor" in message.lower()


def test_classify_unknown_error_returns_none():
    assert _classify_youtube_error("some unexpected internal error") is None


def test_youtube_error_code_format_unavailable():
    assert (
        _youtube_error_code("Requested format is not available") == "FORMAT_UNAVAILABLE"
    )


def test_youtube_error_code_access_restricted():
    assert _youtube_error_code("HTTP Error 403: Forbidden") == "ACCESS_RESTRICTED"


def test_youtube_bot_challenge_is_access_restriction():
    error = "Sign in to confirm you’re not a bot"
    assert _youtube_error_code(error) == "ACCESS_RESTRICTED"
    assert "ограниченный доступ" in _classify_youtube_error(error).lower()


def test_youtube_rate_limit_has_accurate_public_message():
    error = "HTTP Error 429: Too Many Requests"
    assert _youtube_error_code(error) == "RATE_LIMIT"
    assert "ограничил запросы" in _classify_youtube_error(error)
    public_message = build_public_error_message(
        "youtube", "YT-RATE_LIMIT-ABC123", error
    )
    assert "ограничила запросы" in public_message
    assert "YT-RATE_LIMIT-ABC123" in public_message


def test_youtube_error_code_media_forbidden_is_not_access_restricted():
    """403 на самом медиафайле — временная ошибка, а не запрет доступа к видео."""
    assert (
        _youtube_error_code(
            "ERROR: unable to download video data: HTTP Error 403: Forbidden"
        )
        == "MEDIA_FORBIDDEN"
    )


def test_media_forbidden_reported_as_network_problem():
    message = _classify_youtube_error(
        "ERROR: unable to download video data: HTTP Error 403: Forbidden"
    )
    assert message == "🌐 Проблемы с сетью. Попробуйте позже."


def test_youtube_error_code_network_timeout():
    assert _youtube_error_code("Connection timed out") == "NETWORK_TIMEOUT"


def test_youtube_error_code_ffmpeg_missing():
    assert _youtube_error_code("ffmpeg is not installed") == "FFMPEG_MISSING"


def test_youtube_error_code_extractor_runtime():
    assert _youtube_error_code("nsig extraction failed") == "EXTRACTOR_RUNTIME"


def test_youtube_unexpected_error_keeps_explicit_category():
    assert _youtube_error_code("totally unrelated message") == "UNEXPECT"


def test_instagram_access_error_is_platform_specific_and_user_actionable():
    message = _build_public_error_message(
        "instagram",
        "IG-ACCESS-ABC123",
        "login required",
    )

    assert "материал из Instagram" in message
    assert "ссылка требует авторизации" in message
    assert "удалён" in message
    assert "cookies" not in message.lower()
    assert "подписан" not in message.lower()


def test_tiktok_unexpected_error_does_not_expose_internal_state():
    message = _build_public_error_message(
        "tiktok",
        "TT-UNEXPECT-ABC123",
        "cookie expired; bot is not subscribed",
    )

    assert "материал из TikTok" in message
    assert "cookie" not in message.lower()
    assert "подписан" not in message.lower()
