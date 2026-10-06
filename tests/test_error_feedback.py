"""Проверки обратной связи через загрузчик, обработчики и админский журнал."""

import asyncio
import errno
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import telegram
import yt_dlp

from utils import telegram_utils, youtube_utils, rutube_vk_utils
from utils.media_errors import DurationLimitError
from utils.public_errors import build_public_error_message, classify_internal_error_category


@pytest.mark.parametrize("platform", ["youtube", "rutube"])
@pytest.mark.parametrize("over_limit", [False, True])
def test_metadata_duration_boundary_has_a_typed_limit(monkeypatch, tmp_path, platform, over_limit):
    calls = []
    source = youtube_utils if platform == "youtube" else rutube_vk_utils
    duration = source.MAX_VIDEO_DURATION + int(over_limit)

    class FakeYDL:
        def __init__(self, options):
            calls.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, *args, **kwargs):
            return {"duration": duration}

    monkeypatch.setattr(source.yt_dlp, "YoutubeDL", FakeYDL)
    # Имитируем наличие cookies, не открывая реальные файлы.
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setattr(youtube_utils, "YOUTUBE_COOKIES_FILE", cookies)
    monkeypatch.setattr(youtube_utils, "_cookiefile_if_available", lambda enabled: None)
    loader = source.get_video_info if platform == "youtube" else source.get_rutube_info
    if over_limit:
        with pytest.raises(DurationLimitError) as caught:
            loader("https://example.test/video")
        assert caught.value.max_duration == source.MAX_VIDEO_DURATION
        assert classify_internal_error_category(platform, caught.value) == "DURATION"
    else:
        assert loader("https://example.test/video")["duration"] == duration
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("platform", "url"),
    [("youtube", "https://youtu.be/abc123def45"),
     ("rutube", "https://rutube.ru/video/abc123")],
)
@pytest.mark.parametrize("legacy", [False, True])
def test_process_url_reports_limit_without_crash_or_cookie_probe(
    monkeypatch, caplog, platform, url, legacy
):
    error = (
        Exception("Видео слишком длинное. Максимальная длительность: 180 минут.")
        if legacy else DurationLimitError(10801, 10800)
    )
    message = SimpleNamespace(edit_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1), message=SimpleNamespace())
    context = SimpleNamespace(user_data={})
    failures = []
    monkeypatch.setattr(telegram_utils, "_check_spam", lambda *args: False)
    monkeypatch.setattr(telegram_utils, "_track_tg_user", AsyncMock())
    monkeypatch.setattr(telegram_utils, "track_event", Mock())
    monkeypatch.setattr(telegram_utils, "_begin_processing", AsyncMock(
        return_value=(message, "token", "test-session")
    ))
    monkeypatch.setattr(telegram_utils, "run_blocking", AsyncMock(side_effect=error))
    cleanup = Mock()
    monkeypatch.setattr(telegram_utils, "_cleanup_session_when_idle", cleanup)
    monkeypatch.setattr(telegram_utils, "_schedule_platform_failure_log",
                        lambda **kwargs: failures.append(kwargs))
    probe = Mock(side_effect=AssertionError("Проверка cookies для лимита не нужна"))
    report = AsyncMock()
    monkeypatch.setattr(telegram_utils, "check_cookie_health", probe)
    monkeypatch.setattr(telegram_utils, "_notify_admins_crash", report)

    asyncio.run(telegram_utils.process_url(update, context, url))
    text = message.edit_text.call_args.args[0]
    assert "Максимальная длительность: 180 минут" in text
    assert "более короткое" in text
    assert "позже" not in text
    assert len(failures) == 1
    code = failures[0]["error_code"]
    assert re.fullmatch(r"(?:YT|RU)-DURATION-[A-F0-9]{6}", code)
    assert code in text
    asyncio.run(telegram_utils._log_platform_failure(**failures[0]))
    assert f"code={code}" in caplog.text
    assert "category=DURATION" in caplog.text
    probe.assert_not_called()
    report.assert_not_called()
    cleanup.assert_called_once_with("test-session")


@pytest.mark.parametrize("platform", ["youtube", "tiktok", "instagram", "rutube", "vk", "bot"])
@pytest.mark.parametrize("error", [
    RuntimeError("unexpected internal failure"),
    Exception("FFmpeg не установлен. Установите FFmpeg для сжатия файлов."),
    OSError(errno.ENOSPC, "private server path /srv/private"),
    PermissionError("forbidden /srv/private"),
    yt_dlp.cookies.CookieLoadError("private cookie path"),
])
def test_internal_failures_do_not_promise_recovery_or_expose_details(platform, error):
    text = build_public_error_message(platform, "BOT-TEST-ABC123", error)
    assert "BOT-TEST-ABC123" in text
    assert "администратор" in text
    for detail in ["позже", "временно", "FFmpeg", "cookie", "/srv/", "авторизации"]:
        assert detail not in text


@pytest.mark.parametrize("platform", ["youtube", "instagram", "tiktok"])
def test_guesses_in_wrapper_do_not_override_real_cause(platform):
    cause = yt_dlp.utils.DownloadError("extractor failed without a known reason")
    wrapper = Exception(
        "Платформа ограничила доступ. Возможные причины:\n"
        "Превышен лимит запросов (rate-limit)."
    )
    wrapper.__cause__ = cause
    assert classify_internal_error_category(platform, wrapper) == "DOWNLOAD"
    text = build_public_error_message(platform, "BOT-DOWNLOAD-ABC123", wrapper)
    assert "позже" not in text
    assert "ограничила запросы" not in text


@pytest.mark.parametrize("platform", ["youtube", "instagram", "tiktok"])
@pytest.mark.parametrize("message", ["SSL configuration missing", "EOF in metadata parser", "network setting invalid"])
def test_broad_internal_words_do_not_imply_network_failure(platform, message):
    assert classify_internal_error_category(platform, RuntimeError(message)) == "UNEXPECT"


def test_http_status_type_overrides_words_in_url():
    request = httpx.Request("GET", "https://network.example.test/video")
    error = httpx.HTTPStatusError(
        "Forbidden: network.example.test", request=request,
        response=httpx.Response(503, request=request),
    )
    assert classify_internal_error_category("youtube", error) == "API"
    assert classify_internal_error_category("instagram", error) == "API"


def test_local_permissions_and_telegram_access_are_not_platform_restrictions():
    assert classify_internal_error_category("youtube", PermissionError("forbidden")) == "FILE_ACCESS"
    error = telegram.error.Forbidden("bot was blocked by the user")
    assert classify_internal_error_category("youtube", error) == "TELEGRAM_ACCESS"
    text = build_public_error_message("youtube", "TG-TELEGRAM-ABC123", error)
    assert "Telegram" in text
    assert "YouTube" not in text


@pytest.mark.parametrize("error", [
    Exception("FFprobe процесс превысил лимит времени в 15 секунд."),
    Exception("FFmpeg процесс превысил лимит времени в 600 секунд."),
])
def test_processing_deadline_is_not_file_size_or_network_failure(error):
    assert classify_internal_error_category("youtube", error) == "TIMEOUT"
    text = build_public_error_message("youtube", "YT-TIMEOUT-ABC123", error)
    assert "отведенное время" in text
    assert "соединени" not in text


@pytest.mark.parametrize("error", [
    Exception("ffmpeg is not installed"),
    Exception("nsig extraction failed"),
    Exception("Requested format is not available"),
    RuntimeError("private source path /srv/private"),
])
def test_button_errors_use_public_classifier_and_matching_admin_code(monkeypatch, error):
    session = {"platform": "youtube", "url": "https://youtu.be/abc123def45", "session_id": "test-session"}
    context = SimpleNamespace(user_data={"sessions": {"token": session}})
    query = SimpleNamespace(
        data="s|token|main|back", answer=AsyncMock(),
        message=SimpleNamespace(chat=SimpleNamespace()), from_user=SimpleNamespace(id=1),
    )
    update = SimpleNamespace(callback_query=query, effective_user=query.from_user)
    monkeypatch.setattr(telegram_utils, "_handle_main_callback", AsyncMock(side_effect=error))
    edit = AsyncMock()
    monkeypatch.setattr(telegram_utils, "safe_edit_message_text", edit)
    monkeypatch.setattr(telegram_utils, "_cleanup_user_session", AsyncMock())
    failures = []
    monkeypatch.setattr(telegram_utils, "_schedule_platform_failure_log", lambda **kw: failures.append(kw))

    asyncio.run(telegram_utils.button_callback(update, context))
    assert len(failures) == 1
    code = failures[0]["error_code"]
    assert code.startswith("YT-")
    text = edit.call_args.args[1]
    assert code in text
    assert "позже" not in text
    assert "FFmpeg" not in text
    assert "extractor" not in text
    assert "/srv/" not in text


def test_stage_name_does_not_hide_confirmed_server_failure():
    assert telegram_utils._should_notify_admins_platform_failure("youtube", "STORAGE", "process_url_timeout")
    assert telegram_utils._should_notify_admins_platform_failure("youtube", "MEDIA_FORBIDDEN", "format_download")


@pytest.mark.parametrize(
    ("error", "category", "expected"),
    [
        (telegram.error.BadRequest("chat not found"), "API", "Telegram отклонил"),
        (telegram.error.BadRequest("File is too big"), "LARGE", "превышает лимит"),
        (telegram.error.Forbidden("bot was blocked"), "TELEGRAM_ACCESS", "Telegram отклонил"),
        (telegram.error.TimedOut(), "NETWORK_TIMEOUT", "отведенное время"),
    ],
)
def test_send_file_does_not_confuse_telegram_api_refusal_with_network_failure(
    monkeypatch, error, category, expected
):
    session = {"platform": "youtube", "url": "https://youtu.be/abc123def45", "session_id": "test-session"}
    context = SimpleNamespace(user_data={})
    query = SimpleNamespace(from_user=SimpleNamespace(id=1))
    monkeypatch.setattr(telegram_utils, "send_single_file", AsyncMock(side_effect=error))
    edit = AsyncMock()
    monkeypatch.setattr(telegram_utils, "_edit_delivery_status", edit)
    monkeypatch.setattr(telegram_utils, "_cleanup_session_when_idle", Mock())
    failures = []
    monkeypatch.setattr(telegram_utils, "_schedule_platform_failure_log", lambda **kw: failures.append(kw))

    asyncio.run(telegram_utils.send_file(query, "unused.mp4", "token", session, context))
    text = edit.call_args.args[1]
    assert expected in text
    assert classify_internal_error_category("youtube", error) == category
    assert failures[0]["error_code"] in text
    if category not in {"NETWORK_TIMEOUT"}:
        assert "позже" not in text
        assert "соединение" not in text


@pytest.mark.parametrize("message", [
    "Unable to download: [Errno 28] No space left on device",
    "Unable to download: Disk quota exceeded",
])
def test_downloader_storage_failure_has_service_feedback(message):
    error = yt_dlp.utils.DownloadError(message)
    assert classify_internal_error_category("youtube", error) == "STORAGE"
    text = build_public_error_message("youtube", "YT-STORAGE-ABC123", error)
    assert "администратору" in text
    assert "позже" not in text
