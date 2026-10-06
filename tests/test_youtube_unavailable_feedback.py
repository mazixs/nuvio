"""Ответ о недоступности видео не подменяет причину предположением."""

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yt_dlp

from utils import telegram_utils, youtube_utils
from utils.public_errors import build_public_error_message, youtube_error_code
from utils.ytdlp_common import classify_download_error_kind


@pytest.mark.parametrize("reason", [
    "Video unavailable", "This video is unavailable", "This video is not available",
])
@pytest.mark.parametrize("as_exception", [False, True])
def test_reported_unavailability_has_specific_safe_feedback(reason, as_exception):
    text = f"ERROR: [youtube] abc123def45: {reason}"
    error = yt_dlp.utils.DownloadError(text) if as_exception else text
    assert youtube_error_code(error) == "UNAVAILABLE"
    assert classify_download_error_kind(text) == "UNAVAILABLE"
    message = build_public_error_message("youtube", "YT-UNAVAILA-ABC123", error)
    assert "YouTube сообщил" in message
    assert "недоступно для загрузки" in message
    assert "Точная причина недоступности не указана" in message
    assert "YT-UNAVAILA-ABC123" in message
    for unsupported_claim in ["позже", "удален", "приват", "авторизации", "cookies", "abc123def45"]:
        assert unsupported_claim not in message


@pytest.mark.parametrize(("reason", "category"), [
    ("Video unavailable. Private video. Sign in if granted access", "ACCESS_RESTRICTED"),
    ("Video unavailable. HTTP Error 429: Too Many Requests", "RATE_LIMIT"),
    ("Video unavailable. Sign in to confirm your age", "ACCESS_RESTRICTED"),
    ("Unable to download video data: HTTP Error 403: Forbidden. Video unavailable", "MEDIA_FORBIDDEN"),
    ("Requested format is not available", "FORMAT_UNAVAILABLE"),
    ("Future unknown download failure", "DOWNLOAD"),
])
def test_generic_unavailability_does_not_override_more_specific_causes(reason, category):
    assert youtube_error_code(yt_dlp.utils.DownloadError(reason)) == category
    assert classify_download_error_kind(reason) == category


def test_process_url_keeps_cookie_recovery_and_emits_matching_diagnostics(monkeypatch, tmp_path):
    error = yt_dlp.utils.DownloadError("ERROR: [youtube] abc123def45: Video unavailable")
    calls = []
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setattr(youtube_utils, "YOUTUBE_COOKIES_FILE", cookies)
    monkeypatch.setattr(youtube_utils, "_cookiefile_if_available", lambda enabled: str(cookies) if enabled else None)

    class FakeYDL:
        def __init__(self, options):
            calls.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, *args, **kwargs):
            raise error

    monkeypatch.setattr(youtube_utils.yt_dlp, "YoutubeDL", FakeYDL)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1), message=SimpleNamespace())
    message = SimpleNamespace(edit_text=AsyncMock())
    context = SimpleNamespace(user_data={})
    monkeypatch.setattr(telegram_utils, "_check_spam", lambda *args: False)
    monkeypatch.setattr(telegram_utils, "_track_tg_user", AsyncMock())
    monkeypatch.setattr(telegram_utils, "track_event", Mock())
    monkeypatch.setattr(telegram_utils, "_begin_processing", AsyncMock(return_value=(message, "token", "test-session")))

    async def direct_run_blocking(func, *args, **kwargs):
        return func(*args)

    monkeypatch.setattr(telegram_utils, "run_blocking", direct_run_blocking)
    monkeypatch.setattr(telegram_utils, "_cleanup_session_when_idle", Mock())
    failures = []
    monkeypatch.setattr(telegram_utils, "_schedule_platform_failure_log", lambda **kwargs: failures.append(kwargs))
    asyncio.run(telegram_utils.process_url(update, context, "https://youtube.com/shorts/abc123def45"))

    assert len(calls) == 2
    assert "cookiefile" not in calls[0]
    assert calls[1]["cookiefile"] == str(cookies)
    assert len(failures) == 1
    code = failures[0]["error_code"]
    assert re.fullmatch(r"YT-UNAVAILA-[A-F0-9]{6}", code)
    assert code in message.edit_text.call_args.args[0]
    assert "позже" not in message.edit_text.call_args.args[0]
    assert telegram_utils._should_notify_admins_platform_failure("youtube", "UNAVAILABLE", "process_url")
