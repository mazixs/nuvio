#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for audit fixes."""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import config
import pytest
from utils import cache_commands
from utils import cookie_health
from utils import cookie_manager
from utils import telegram_utils
from utils import tiktok_instagram_utils


class _CapturingYDL:
    """Minimal yt-dlp stub that records options."""

    captured_options: list[dict] = []
    captured_urls: list[str] = []

    def __init__(self, options):
        self.options = options
        self.__class__.captured_options.append(options.copy())

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, url, download=False):
        self.__class__.captured_urls.append(url)
        return {
            "title": "stub",
            "uploader": "tester",
            "duration": 42,
            "formats": [
                {
                    "format_id": "18",
                    "format": "720p",
                    "ext": "mp4",
                    "height": 720,
                    "width": 1280,
                    "filesize": 1024,
                    "vcodec": "avc1",
                    "acodec": "aac",
                }
            ],
        }


class _DummyMessage:
    def __init__(self, text: str):
        self.text = text
        self.replies: list[str] = []
        self.reply_calls: list[tuple[str, dict]] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        self.reply_calls.append((text, kwargs))
        return None


class _DummyQuery:
    def __init__(self, data: str = "", user_id: int = 7):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = SimpleNamespace()
        self.edits: list[tuple[str, dict]] = []
        self.answers: list[tuple[str | None, bool]] = []

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))
        return None

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))
        return None


class _EditableReply:
    def __init__(self):
        self.edits: list[tuple[str, dict]] = []

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))
        return None


class _DummyDocument:
    def __init__(
        self, file_name: str, *, file_size: int = 128, mime_type: str = "text/plain"
    ):
        self.file_name = file_name
        self.file_size = file_size
        self.mime_type = mime_type
        self.get_file_called = False

    async def get_file(self):
        self.get_file_called = True
        raise AssertionError("get_file must not be called in this test")


class _DummyTelegramFile:
    def __init__(self):
        self.downloaded_to = None

    async def download_to_drive(self, file_path):
        self.downloaded_to = file_path
        Path(file_path).write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")


class _DownloadableDummyDocument(_DummyDocument):
    def __init__(self, file_name: str, telegram_file: _DummyTelegramFile, **kwargs):
        super().__init__(file_name, **kwargs)
        self.telegram_file = telegram_file

    async def get_file(self):
        self.get_file_called = True
        return self.telegram_file


class _DummyBot:
    def __init__(self):
        self.sent_messages: list[tuple[int, str]] = []
        self.sent_documents: list[dict] = []

    async def send_message(self, chat_id, text):
        self.sent_messages.append((chat_id, text))

    async def send_document(self, **kwargs):
        self.sent_documents.append(kwargs)


def test_check_spam_sets_real_timeout():
    context = SimpleNamespace(user_data={})

    assert not telegram_utils._check_spam(1, context, 0.0)
    assert not telegram_utils._check_spam(1, context, 1.0)
    assert not telegram_utils._check_spam(1, context, 2.0)
    assert telegram_utils._check_spam(1, context, 3.0)
    assert context.user_data["spam_blocked_until"] == 13.0

    assert telegram_utils._check_spam(1, context, 8.0)
    assert not telegram_utils._check_spam(1, context, 14.0)


def test_cleanup_user_session_preserves_antispam_state(monkeypatch):
    cleaned_sessions: list[str] = []
    monkeypatch.setattr(
        telegram_utils,
        "cleanup_temp_files",
        lambda session_id: cleaned_sessions.append(session_id),
    )
    context = SimpleNamespace(
        user_data={
            "session_id": "session-1",
            "url": "https://example.com",
            "video_info": {"title": "Example"},
            "formats": {"combined": []},
            "platform": "youtube",
            "recent_requests": [1.0, 2.0],
            "spam_blocked_until": 12.0,
        }
    )

    asyncio.run(telegram_utils._cleanup_user_session(42, context))

    assert cleaned_sessions == ["session-1"]
    assert context.user_data == {
        "recent_requests": [1.0, 2.0],
        "spam_blocked_until": 12.0,
    }


def test_cleanup_specific_session_keeps_other_sessions(monkeypatch):
    cleaned_sessions: list[str] = []
    monkeypatch.setattr(
        telegram_utils,
        "cleanup_temp_files",
        lambda session_id: cleaned_sessions.append(session_id),
    )
    context = SimpleNamespace(user_data={})

    first = telegram_utils._store_session(
        context,
        url="https://example.com/1",
        video_info={"title": "One"},
        session_id="session-1",
        platform="youtube",
        formats={"combined": []},
    )
    second = telegram_utils._store_session(
        context,
        url="https://example.com/2",
        video_info={"title": "Two"},
        session_id="session-2",
        platform="instagram",
        formats={},
    )

    asyncio.run(telegram_utils._cleanup_user_session(42, context, first))

    assert cleaned_sessions == ["session-1"]
    assert telegram_utils._get_session(context, first) is None
    assert (
        telegram_utils._get_session(context, second)["url"] == "https://example.com/2"
    )


def test_process_url_applies_antispam_to_plain_messages(monkeypatch):
    message = _DummyMessage("https://example.com/video")
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=7),
        message=message,
    )
    context = SimpleNamespace(user_data={}, args=[])

    monkeypatch.setattr(telegram_utils, "is_valid_youtube_url", lambda url: False)
    monkeypatch.setattr(telegram_utils, "is_valid_tiktok_url", lambda url: False)
    monkeypatch.setattr(telegram_utils, "is_instagram_audio_url", lambda url: False)
    monkeypatch.setattr(telegram_utils, "is_valid_instagram_url", lambda url: False)

    for _ in range(3):
        asyncio.run(
            telegram_utils.process_url(update, context, "https://example.com/video")
        )

    asyncio.run(
        telegram_utils.process_url(update, context, "https://example.com/video")
    )

    assert message.replies[-1] == telegram_utils.SPAM_WARNING


def test_build_main_menu_uses_platform_specific_callbacks():
    _, instagram_menu = telegram_utils._build_main_menu(
        "instagram",
        {"title": "Clip", "uploader": "author", "duration": 12},
        "sess1234",
    )

    callbacks = [
        button.callback_data for row in instagram_menu.inline_keyboard for button in row
    ]

    assert "s|sess1234|main|instagram_download" in callbacks
    assert "s|sess1234|main|instagram_audio" in callbacks
    assert "s|sess1234|main|tg_video" not in callbacks


def test_navigation_callbacks_are_not_rate_limited():
    assert not telegram_utils._should_rate_limit_callback("s|sess1234|main|more")
    assert not telegram_utils._should_rate_limit_callback("s|sess1234|main|back")
    assert telegram_utils._should_rate_limit_callback("s|sess1234|main|tg_video")
    assert telegram_utils._should_rate_limit_callback("s|sess1234|format|best|best")


def test_validate_config_accepts_required_bot_token(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", "telegram-token")

    assert config.validate_config() is True


def test_parse_log_level_and_admin_ids_are_resilient():
    assert config._parse_log_level("debug") == logging.DEBUG
    assert config._parse_log_level("unknown-level") == logging.INFO
    assert config._parse_admin_ids("1, 2, ,oops,3") == [1, 2, 3]


def test_resolve_secret_path_prefers_canonical_location(monkeypatch, tmp_path, caplog):
    legacy = tmp_path / "www.youtube.com_cookies.txt"
    canonical_dir = tmp_path / ".secrets"
    canonical_dir.mkdir()
    canonical = canonical_dir / legacy.name
    legacy.write_text("legacy", encoding="utf-8")
    canonical.write_text("canonical", encoding="utf-8")

    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config, "SECRETS_DIR", canonical_dir)

    with caplog.at_level(logging.WARNING):
        resolved = config.resolve_secret_path(legacy.name)

    assert resolved == canonical
    assert "Используем" in caplog.text


def test_classify_tiktok_russian_access_error():
    error = (
        "TikTok ограничил доступ даже с авторизацией.\n\n"
        "Возможные причины:\n"
        "• Превышен лимит запросов\n"
        "• Региональные блокировки"
    )

    assert (
        telegram_utils._classify_internal_error_category("tiktok", error)
        == "RATE_LIMIT"
    )


def test_tiktok_rate_limit_failure_notifies_admins(monkeypatch):
    bot = _DummyBot()
    exc = Exception(
        "TikTok ограничил доступ даже с авторизацией.\n\n"
        "Возможные причины:\n"
        "• Превышен лимит запросов"
    )

    monkeypatch.setattr(telegram_utils, "_bot_instance", bot)
    monkeypatch.setattr(telegram_utils, "ADMIN_IDS", [1])
    monkeypatch.setattr(
        telegram_utils,
        "check_cookie_health",
        lambda platform: cookie_health.CookieHealthResult(
            platform, "valid", "auth cookies are active", 0.0, 2, 2
        ),
    )

    asyncio.run(
        telegram_utils._log_platform_failure(
            platform="tiktok",
            stage="process_url",
            url="https://vt.tiktok.com/example/",
            error_code="TT-RATE_LIM-ABC123",
            exc=exc,
        )
    )

    assert len(bot.sent_documents) == 1
    assert bot.sent_documents[0]["filename"] == "crash_TT-RATE_LIM-ABC123.md"


def test_exception_traceback_format_does_not_use_none_type_placeholder():
    exc = Exception("plain failure")

    formatted = telegram_utils._format_exception_traceback(exc)

    assert "NoneType: None" not in formatted
    assert "Traceback unavailable" in formatted


def test_tiktok_info_requests_full_metadata(monkeypatch):
    _CapturingYDL.captured_options.clear()
    _CapturingYDL.captured_urls.clear()
    monkeypatch.setattr(tiktok_instagram_utils.yt_dlp, "YoutubeDL", _CapturingYDL)
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "_smart_retry",
        lambda func, max_attempts=0, context="": func(),
    )
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "_get_tiktok_base_configs",
        lambda: [{"quiet": True, "no_warnings": True}],
    )
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "TIKTOK_COOKIES_FILE",
        Path(r"C:\definitely-missing-tiktok-cookies.txt"),
    )

    info = tiktok_instagram_utils.get_tiktok_info(
        "https://www.tiktok.com/@user/video/1"
    )

    assert info["title"] == "stub"
    assert _CapturingYDL.captured_options
    assert "extract_flat" not in _CapturingYDL.captured_options[0]


def test_tiktok_info_uses_resolved_url(monkeypatch):
    _CapturingYDL.captured_options.clear()
    _CapturingYDL.captured_urls.clear()
    monkeypatch.setattr(tiktok_instagram_utils.yt_dlp, "YoutubeDL", _CapturingYDL)
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "_smart_retry",
        lambda func, max_attempts=0, context="": func(),
    )
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "_get_tiktok_base_configs",
        lambda: [{"quiet": True, "no_warnings": True}],
    )
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "_resolve_tiktok_url",
        lambda url: "https://www.tiktok.com/@user/video/1",
    )
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "TIKTOK_COOKIES_FILE",
        Path(r"C:\definitely-missing-tiktok-cookies.txt"),
    )

    tiktok_instagram_utils.get_tiktok_info("https://vt.tiktok.com/example/")

    assert _CapturingYDL.captured_urls == ["https://www.tiktok.com/@user/video/1"]


def test_tiktok_download_uses_resolved_url(monkeypatch, tmp_path):
    captured_urls = []

    class _FailingDownloadYDL(_CapturingYDL):
        def extract_info(self, url, download=False):
            captured_urls.append(url)
            raise RuntimeError("stop after capture")

    monkeypatch.setattr(tiktok_instagram_utils.yt_dlp, "YoutubeDL", _FailingDownloadYDL)
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "_smart_retry",
        lambda func, max_attempts=0, context="": func(),
    )
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "_get_tiktok_base_configs",
        lambda: [{"quiet": True, "no_warnings": True}],
    )
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "_resolve_tiktok_url",
        lambda url: "https://www.tiktok.com/@user/video/1",
    )
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "TIKTOK_COOKIES_FILE",
        Path(r"C:\definitely-missing-tiktok-cookies.txt"),
    )

    with pytest.raises(Exception, match="stop after capture"):
        tiktok_instagram_utils.download_tiktok_video(
            "https://vt.tiktok.com/example/",
            "session-1",
            tmp_path,
        )

    assert captured_urls == ["https://www.tiktok.com/@user/video/1"]


def test_tiktok_photo_info_uses_fallback_when_yt_dlp_cannot_parse(monkeypatch):
    class _UnsupportedPhotoYDL(_CapturingYDL):
        def extract_info(self, url, download=False):
            raise tiktok_instagram_utils.yt_dlp.utils.DownloadError(
                f"ERROR: Unsupported URL: {url}"
            )

    monkeypatch.setattr(
        tiktok_instagram_utils.yt_dlp, "YoutubeDL", _UnsupportedPhotoYDL
    )
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "_smart_retry",
        lambda func, max_attempts=0, context="": func(),
    )
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "_get_tiktok_base_configs",
        lambda: [{"quiet": True, "no_warnings": True}],
    )
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "_resolve_tiktok_url",
        lambda url: "https://www.tiktok.com/@user/photo/1",
    )
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "_fetch_tiktok_photo_post_data",
        lambda url, original_url=None: {
            "id": "1",
            "title": "Фото-пост",
            "cover": "https://cdn.example/cover.jpg",
            "music": "https://cdn.example/audio.mp3",
            "images": ["https://cdn.example/1.jpg", "https://cdn.example/2.jpg"],
            "author": {"unique_id": "tester"},
            "music_info": {"duration": 12},
        },
    )

    info = tiktok_instagram_utils.get_tiktok_info("https://vt.tiktok.com/example/")

    assert info["_nuvio_tiktok_photo_post"] is True
    assert info["uploader"] == "tester"
    assert len(info["_nuvio_tiktok_images"]) == 2
    assert info["duration"] == 12


def test_tiktok_photo_resolver_removes_tracking_query(monkeypatch):
    resolved_url = (
        "https://www.tiktok.com/@user/photo/1?"
        "is_from_webapp=1&sender_device=pc"
    )
    clean_url = "https://www.tiktok.com/@user/photo/1"
    requested_urls: list[str] = []

    class _Response:
        def raise_for_status(self):
            if requested_urls[-1] != clean_url:
                raise RuntimeError("Client error '403 Forbidden'")

        def json(self):
            return {"code": 0, "data": {"images": ["https://cdn.example/1.jpg"]}}

    def _get(_url, *, params, **_kwargs):
        requested_urls.append(params["url"])
        return _Response()

    monkeypatch.setattr(
        tiktok_instagram_utils,
        "_resolve_tiktok_url",
        lambda _url: resolved_url,
    )
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "_fetch_tiktok_photo_post_from_page",
        lambda _url: None,
    )
    monkeypatch.setattr(tiktok_instagram_utils.httpx, "get", _get)

    data = tiktok_instagram_utils._fetch_tiktok_photo_post_data(
        "https://vt.tiktok.com/ZSVYA4PyM/"
    )

    assert requested_urls == [clean_url]
    assert data["images"] == ["https://cdn.example/1.jpg"]


def test_tiktok_photo_resolver_switches_api_host_after_forbidden(monkeypatch):
    resolved_url = "https://www.tiktok.com/@user/photo/1"
    requested_endpoints: list[str] = []

    class _Response:
        def raise_for_status(self):
            if requested_endpoints[-1] == tiktok_instagram_utils.TIKWM_API_URL:
                raise RuntimeError("Client error '403 Forbidden'")

        def json(self):
            return {"code": 0, "data": {"images": ["https://cdn.example/1.jpg"]}}

    def _get(endpoint, *, params, **_kwargs):
        assert params["url"] == resolved_url
        requested_endpoints.append(endpoint)
        return _Response()

    monkeypatch.setattr(
        tiktok_instagram_utils,
        "_resolve_tiktok_url",
        lambda _url: resolved_url,
    )
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "_fetch_tiktok_photo_post_from_page",
        lambda _url: None,
    )
    monkeypatch.setattr(tiktok_instagram_utils.httpx, "get", _get)

    data = tiktok_instagram_utils._fetch_tiktok_photo_post_data(resolved_url)

    assert requested_endpoints == [
        tiktok_instagram_utils.TIKWM_API_URL,
        tiktok_instagram_utils.TIKWM_FALLBACK_API_URL,
    ]
    assert data["images"] == ["https://cdn.example/1.jpg"]


def test_tiktok_photo_resolver_reads_photo_data_from_tiktok_page(monkeypatch):
    photo_url = "https://www.tiktok.com/@user/photo/1"
    video_page_url = "https://www.tiktok.com/@user/video/1"
    universal_data = {
        "__DEFAULT_SCOPE__": {
            "webapp.video-detail": {
                "itemInfo": {
                    "itemStruct": {
                        "id": "1",
                        "desc": "#тест",
                        "author": {
                            "uniqueId": "tester",
                            "nickname": "Тестер",
                        },
                        "imagePost": {
                            "cover": {
                                "imageURL": {
                                    "urlList": [
                                        "https://p16.tiktokcdn.com/cover.jpg"
                                    ]
                                }
                            },
                            "images": [
                                {
                                    "imageURL": {
                                        "urlList": [
                                            "https://p16.tiktokcdn.com/1.jpg"
                                        ]
                                    }
                                },
                                {
                                    "imageURL": {
                                        "urlList": [
                                            "https://p16.tiktokcdn.com/2.jpg"
                                        ]
                                    }
                                },
                            ],
                        },
                        "music": {
                            "duration": 24,
                            "playUrl": "https://v45.tiktokcdn.com/audio.mp3",
                        },
                    }
                }
            }
        }
    }

    class _Response:
        text = (
            '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">'
            + json.dumps(universal_data)
            + "</script>"
        )

        def raise_for_status(self):
            return None

    requested_urls: list[str] = []

    def _get(url, *, headers, follow_redirects, timeout):
        requested_urls.append(url)
        assert headers["Accept"].startswith("text/html")
        assert follow_redirects is True
        assert timeout == tiktok_instagram_utils.TIKTOK_PHOTO_PAGE_TIMEOUT_SECONDS
        return _Response()

    monkeypatch.setattr(
        tiktok_instagram_utils,
        "_resolve_tiktok_url",
        lambda _url: photo_url,
    )
    monkeypatch.setattr(tiktok_instagram_utils.httpx, "get", _get)

    data = tiktok_instagram_utils._fetch_tiktok_photo_post_data(photo_url)

    assert requested_urls == [video_page_url]
    assert data["id"] == "1"
    assert data["title"] == "#тест"
    assert data["author"] == {"unique_id": "tester", "nickname": "Тестер"}
    assert data["images"] == [
        "https://p16.tiktokcdn.com/1.jpg",
        "https://p16.tiktokcdn.com/2.jpg",
    ]
    assert data["music"] == "https://v45.tiktokcdn.com/audio.mp3"
    assert data["music_info"]["duration"] == 24


def test_tiktok_photo_resolver_falls_back_to_original_short_url(monkeypatch):
    short_url = "https://vt.tiktok.com/ZSVYA4PyM/"
    resolved_url = "https://www.tiktok.com/@user/photo/1"
    requested_urls: list[str] = []

    class _Response:
        def raise_for_status(self):
            if requested_urls[-1] == resolved_url:
                raise RuntimeError("Client error '403 Forbidden'")

        def json(self):
            return {"code": 0, "data": {"images": ["https://cdn.example/1.jpg"]}}

    def _get(_url, *, params, **_kwargs):
        requested_urls.append(params["url"])
        return _Response()

    monkeypatch.setattr(
        tiktok_instagram_utils,
        "_resolve_tiktok_url",
        lambda _url: resolved_url,
    )
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "_fetch_tiktok_photo_post_from_page",
        lambda _url: None,
    )
    monkeypatch.setattr(tiktok_instagram_utils.httpx, "get", _get)

    data = tiktok_instagram_utils._fetch_tiktok_photo_post_data(short_url)

    assert requested_urls == [resolved_url, short_url]
    assert data["images"] == ["https://cdn.example/1.jpg"]


def test_get_tiktok_info_keeps_original_short_url_for_photo_fallback(monkeypatch):
    short_url = "https://vt.tiktok.com/ZSVYA4PyM/"
    resolved_url = "https://www.tiktok.com/@user/photo/1"
    requested_urls: list[str] = []

    class _Response:
        def raise_for_status(self):
            if requested_urls[-1] == resolved_url:
                raise RuntimeError("Client error '403 Forbidden'")

        def json(self):
            return {
                "code": 0,
                "data": {
                    "id": "1",
                    "title": "Фото-пост",
                    "images": ["https://cdn.example/1.jpg"],
                    "author": {"unique_id": "tester"},
                    "music_info": {"duration": 12},
                },
            }

    def _get(_url, *, params, **_kwargs):
        requested_urls.append(params["url"])
        return _Response()

    monkeypatch.setattr(
        tiktok_instagram_utils,
        "_resolve_tiktok_url",
        lambda _url: resolved_url,
    )
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "_fetch_tiktok_photo_post_from_page",
        lambda _url: None,
    )
    monkeypatch.setattr(tiktok_instagram_utils.httpx, "get", _get)

    info = tiktok_instagram_utils.get_tiktok_info(short_url)

    assert requested_urls == [resolved_url, short_url]
    assert info["_nuvio_tiktok_images"] == ["https://cdn.example/1.jpg"]


def test_tiktok_photo_video_download_is_rejected():
    with pytest.raises(Exception, match="набор изображений"):
        tiktok_instagram_utils.download_tiktok_video(
            "https://www.tiktok.com/@user/photo/1",
            "session-1",
            None,
            False,
            {"_nuvio_tiktok_photo_post": True},
        )


def test_tiktok_photo_audio_short_circuits_to_photo_handler(monkeypatch):
    called: list[tuple] = []
    cached_info = {"_nuvio_tiktok_photo_post": True}

    monkeypatch.setattr(
        tiktok_instagram_utils,
        "download_tiktok_photo_audio",
        lambda *args: called.append(args) or Path("photo.m4a"),
    )

    result = tiktok_instagram_utils.download_tiktok_audio(
        "https://www.tiktok.com/@user/photo/1",
        "session-1",
        None,
        False,
        cached_info,
    )

    assert result == Path("photo.m4a")
    assert called


def test_instagram_info_falls_back_to_photo_post(monkeypatch):
    class _NoVideoInstagramYDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, url, download=False):
            raise Exception(
                "ERROR: [Instagram] DWLd9IYDVeD: There is no video in this post"
            )

    monkeypatch.setattr(
        tiktok_instagram_utils.yt_dlp, "YoutubeDL", _NoVideoInstagramYDL
    )
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "_fetch_instagram_photo_post_media",
        lambda url: {
            "id": "ig-photo-1",
            "shortcode": "DWLd9IYDVeD",
            "owner": {"username": "tester"},
            "display_resources": [
                {"src": "https://cdn.example/1-small.jpg", "config_width": 640},
                {"src": "https://cdn.example/1-large.jpg", "config_width": 1080},
            ],
            "edge_media_to_caption": {
                "edges": [{"node": {"text": "Фото-пост Instagram"}}]
            },
            "clips_metadata": {
                "music_info": {
                    "music_asset_info": {
                        "progressive_download_url": "https://cdn.example/audio.m4a",
                    }
                }
            },
        },
    )

    info = tiktok_instagram_utils.get_instagram_info(
        "https://www.instagram.com/p/DWLd9IYDVeD/"
    )

    assert info["_nuvio_instagram_photo_post"] is True
    assert info["uploader"] == "tester"
    assert info["_nuvio_instagram_images"] == ["https://cdn.example/1-large.jpg"]
    assert info["_nuvio_instagram_audio_url"] == "https://cdn.example/audio.m4a"


def test_instagram_info_rate_limit_falls_back_to_photo_post(monkeypatch):
    class _RateLimitedInstagramYDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, url, download=False):
            raise Exception(
                "ERROR: [Instagram] DWLd9IYDVeD: Requested content is not available, "
                "rate-limit reached or login required."
            )

    monkeypatch.setattr(
        tiktok_instagram_utils.yt_dlp, "YoutubeDL", _RateLimitedInstagramYDL
    )
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "INSTAGRAM_COOKIES_FILE",
        Path(r"C:\definitely-missing-instagram-cookies.txt"),
    )
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "_fetch_instagram_photo_post_media",
        lambda url: {
            "id": "ig-photo-2",
            "shortcode": "DWLd9IYDVeD",
            "owner": {"username": "tester"},
            "display_url": "https://cdn.example/cover.jpg",
            "caption": "Фото-пост без звука",
        },
    )

    info = tiktok_instagram_utils.get_instagram_info(
        "https://www.instagram.com/p/DWLd9IYDVeD/"
    )

    assert info["_nuvio_instagram_photo_post"] is True
    assert info["_nuvio_instagram_images"] == ["https://cdn.example/cover.jpg"]
    assert info["_nuvio_instagram_audio_url"] is None


def test_instagram_info_empty_playlist_falls_back_to_photo_post(monkeypatch):
    class _EmptyPlaylistInstagramYDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, url, download=False):
            return {
                "_type": "playlist",
                "entries": [],
                "title": "Post by hamiboom.world",
            }

    monkeypatch.setattr(
        tiktok_instagram_utils.yt_dlp, "YoutubeDL", _EmptyPlaylistInstagramYDL
    )
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "_fetch_instagram_photo_post_media",
        lambda url: {
            "id": "ig-photo-3",
            "shortcode": "DTgni55AKmO",
            "owner": {"username": "tester"},
            "carousel_media": [
                {
                    "image_versions2": {
                        "candidates": [
                            {"url": "https://cdn.example/1.jpg", "width": 1080}
                        ]
                    }
                }
            ],
        },
    )

    info = tiktok_instagram_utils.get_instagram_info(
        "https://www.instagram.com/p/DTgni55AKmO"
    )

    assert info["_nuvio_instagram_photo_post"] is True
    assert info["_nuvio_instagram_images"] == ["https://cdn.example/1.jpg"]


def test_fetch_instagram_photo_post_media_falls_back_to_webpage_meta(monkeypatch):
    class _FakeResponse:
        text = (
            '<meta property="og:image" content="https://cdn.example/post.jpg" />'
            '<meta property="og:description" content="12 likes - tester on March 20, 2026: &quot;Подпись&quot;." />'
            '<meta property="og:title" content="Тестовый пост on Instagram: &quot;Подпись&quot;" />'
            '<meta property="al:ios:url" content="instagram://media?id=1234567890" />'
        )

        def raise_for_status(self):
            return None

    monkeypatch.setattr(
        tiktok_instagram_utils,
        "INSTAGRAM_COOKIES_FILE",
        Path(r"C:\definitely-missing-instagram-cookies.txt"),
    )
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "_fetch_public_instagram_graphql_media",
        lambda canonical_url, shortcode: (_ for _ in ()).throw(
            Exception("401 Unauthorized")
        ),
    )
    monkeypatch.setattr(
        tiktok_instagram_utils.httpx, "get", lambda *args, **kwargs: _FakeResponse()
    )

    media = tiktok_instagram_utils._fetch_instagram_photo_post_media(
        "https://www.instagram.com/p/ABC123/"
    )

    assert media["display_url"] == "https://cdn.example/post.jpg"
    assert media["owner"] == {"username": "tester"}
    assert media["id"] == "1234567890"


def test_fetch_public_instagram_graphql_media_returns_first_item(monkeypatch):
    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": {
                    "xdt_api__v1__media__shortcode__web_info": {
                        "items": [
                            {
                                "id": "123",
                                "carousel_media": [
                                    {
                                        "image_versions2": {
                                            "candidates": [
                                                {
                                                    "url": "https://cdn.example/1.jpg",
                                                    "width": 1080,
                                                }
                                            ]
                                        }
                                    }
                                ],
                            }
                        ]
                    }
                }
            }

    class _FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, params=None):
            assert url == tiktok_instagram_utils.INSTAGRAM_GRAPHQL_URL
            assert (
                params["doc_id"]
                == tiktok_instagram_utils.INSTAGRAM_GRAPHQL_WEB_INFO_DOC_ID
            )
            return _FakeResponse()

    monkeypatch.setattr(tiktok_instagram_utils.httpx, "Client", _FakeClient)

    media = tiktok_instagram_utils._fetch_public_instagram_graphql_media(
        "https://www.instagram.com/p/ABC123/",
        "ABC123",
    )

    assert media["id"] == "123"
    assert (
        media["carousel_media"][0]["image_versions2"]["candidates"][0]["url"]
        == "https://cdn.example/1.jpg"
    )


def test_instagram_photo_audio_short_circuits_to_photo_handler(monkeypatch):
    called: list[tuple] = []
    cached_info = {"_nuvio_instagram_photo_post": True}

    monkeypatch.setattr(
        tiktok_instagram_utils,
        "download_instagram_photo_audio",
        lambda *args: called.append(args) or Path("instagram-photo.m4a"),
    )

    result = tiktok_instagram_utils.download_instagram_audio(
        "https://www.instagram.com/p/DWLd9IYDVeD/",
        "session-ig",
        None,
        False,
        cached_info,
    )

    assert result == Path("instagram-photo.m4a")
    assert called


def test_build_instagram_photo_info_collects_sidecar_images():
    info = tiktok_instagram_utils._build_instagram_photo_info(
        "https://www.instagram.com/p/ABC123/",
        {
            "shortcode": "ABC123",
            "owner": {"username": "tester"},
            "edge_sidecar_to_children": {
                "edges": [
                    {
                        "node": {
                            "display_resources": [
                                {
                                    "src": "https://cdn.example/1-small.jpg",
                                    "config_width": 640,
                                },
                                {
                                    "src": "https://cdn.example/1-large.jpg",
                                    "config_width": 1080,
                                },
                            ],
                            "is_video": False,
                        }
                    },
                    {
                        "node": {
                            "display_url": "https://cdn.example/2.jpg",
                            "is_video": False,
                        }
                    },
                    {
                        "node": {
                            "display_url": "https://cdn.example/video-thumb.jpg",
                            "video_url": "https://cdn.example/video.mp4",
                            "is_video": True,
                        }
                    },
                ]
            },
        },
    )

    assert info["_nuvio_instagram_photo_post"] is True
    assert info["_nuvio_instagram_images"] == [
        "https://cdn.example/1-large.jpg",
        "https://cdn.example/2.jpg",
    ]


def test_build_instagram_photo_info_deduplicates_signed_image_variants():
    info = tiktok_instagram_utils._build_instagram_photo_info(
        "https://www.instagram.com/p/ABC123/",
        {
            "shortcode": "ABC123",
            "owner": {"username": "tester"},
            "edge_sidecar_to_children": {
                "edges": [
                    {
                        "node": {
                            "display_url": "https://cdn.example/photo.jpg?sig=1",
                            "is_video": False,
                        }
                    },
                    {
                        "node": {
                            "display_url": "https://cdn.example/photo.jpg?sig=2",
                            "is_video": False,
                        }
                    },
                    {
                        "node": {
                            "display_url": "https://cdn.example/photo-2.jpg?sig=3",
                            "is_video": False,
                        }
                    },
                ]
            },
        },
    )

    assert info["_nuvio_instagram_images"] == [
        "https://cdn.example/photo.jpg?sig=1",
        "https://cdn.example/photo-2.jpg?sig=3",
    ]


def test_handle_main_callback_routes_tiktok_photo_to_asset_sender(monkeypatch):
    called: list[tuple] = []
    context = SimpleNamespace(user_data={})
    session_token = telegram_utils._store_session(
        context,
        url="https://www.tiktok.com/@user/photo/1",
        video_info={"_nuvio_tiktok_photo_post": True, "title": "Фото-пост"},
        session_id="session-1",
        platform="tiktok",
        formats={},
    )
    query = _DummyQuery()

    async def fake_send_photo_post_assets(*args):
        called.append(args)

    monkeypatch.setattr(
        telegram_utils, "_send_photo_post_assets", fake_send_photo_post_assets
    )

    asyncio.run(
        telegram_utils._handle_main_callback(
            query,
            context,
            7,
            session_token,
            "tiktok_download",
        )
    )

    assert called


def test_handle_main_callback_routes_instagram_photo_to_asset_sender(monkeypatch):
    called: list[tuple] = []
    context = SimpleNamespace(user_data={})
    session_token = telegram_utils._store_session(
        context,
        url="https://www.instagram.com/p/DWLd9IYDVeD/",
        video_info={"_nuvio_instagram_photo_post": True, "title": "Фото-пост"},
        session_id="session-ig",
        platform="instagram",
        formats={},
    )
    query = _DummyQuery()

    async def fake_send_photo_post_assets(*args):
        called.append(args)

    monkeypatch.setattr(
        telegram_utils, "_send_photo_post_assets", fake_send_photo_post_assets
    )

    asyncio.run(
        telegram_utils._handle_main_callback(
            query,
            context,
            7,
            session_token,
            "instagram_download",
        )
    )

    assert called


def test_photo_post_failure_keeps_session_for_back_button(monkeypatch):
    context = SimpleNamespace(user_data={})
    cleaned_session_ids = []
    session_token = telegram_utils._store_session(
        context,
        url="https://www.instagram.com/p/photo/",
        video_info={"_nuvio_instagram_photo_post": True, "title": "Фото-пост"},
        session_id="session-photo",
        platform="instagram",
        formats={},
    )
    session_data = telegram_utils._get_session(context, session_token)
    query = _DummyQuery()

    async def fake_run_blocking(*args, **kwargs):
        return {"items": [], "audio": None}

    monkeypatch.setattr(telegram_utils, "run_blocking", fake_run_blocking)
    monkeypatch.setattr(
        telegram_utils,
        "cleanup_temp_files",
        lambda session_id: cleaned_session_ids.append(session_id),
    )
    monkeypatch.setattr(
        telegram_utils,
        "_schedule_platform_failure_log",
        lambda **kwargs: None,
    )

    asyncio.run(
        telegram_utils._send_photo_post_assets(
            query,
            session_token,
            session_data,
            context,
        )
    )

    assert telegram_utils._get_session(context, session_token) is session_data
    assert cleaned_session_ids == ["session-photo"]
    assert query.edits[-1][1]["reply_markup"] is not None


def test_handle_main_callback_reroutes_instagram_photo_exception_to_asset_sender(
    monkeypatch,
):
    called: list[tuple] = []
    context = SimpleNamespace(user_data={})
    session_token = telegram_utils._store_session(
        context,
        url="https://www.instagram.com/p/DTgni55AKmO",
        video_info={"title": "Пост"},
        session_id="session-ig-empty-playlist",
        platform="instagram",
        formats={},
    )
    query = _DummyQuery()

    async def fake_send_photo_post_assets(*args):
        called.append(args)

    def fake_download_instagram_video(*args, **kwargs):
        raise Exception(
            "Instagram фото-пост нужно отправлять как набор изображений и отдельное аудио."
        )

    monkeypatch.setattr(
        telegram_utils, "_send_photo_post_assets", fake_send_photo_post_assets
    )
    monkeypatch.setattr(
        telegram_utils.telegram_cache, "get", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "download_instagram_video",
        fake_download_instagram_video,
    )

    asyncio.run(
        telegram_utils._handle_main_callback(
            query,
            context,
            7,
            session_token,
            "instagram_download",
        )
    )

    assert called


def test_build_main_menu_marks_tiktok_photo_post():
    text, menu = telegram_utils._build_main_menu(
        "tiktok",
        {
            "title": "Фото-пост",
            "uploader": "tester",
            "duration": 15,
            "_nuvio_tiktok_photo_post": True,
            "_nuvio_tiktok_images": ["1", "2", "3"],
            "_nuvio_tiktok_audio_url": "https://cdn.example/audio.mp3",
        },
        "sessphoto",
    )

    assert "Кадров: 3" in text
    assert menu.inline_keyboard[0][0].text == telegram_utils.BTN_DOWNLOAD_POST


def test_build_main_menu_hides_tiktok_audio_button_without_audio():
    _text, menu = telegram_utils._build_main_menu(
        "tiktok",
        {
            "title": "Фото-пост",
            "uploader": "tester",
            "duration": 15,
            "_nuvio_tiktok_photo_post": True,
            "_nuvio_tiktok_images": ["1", "2", "3"],
            "_nuvio_tiktok_audio_url": None,
        },
        "sessphoto",
    )

    buttons = [button.text for row in menu.inline_keyboard for button in row]
    assert telegram_utils.BTN_AUDIO_ONLY not in buttons


def test_build_main_menu_marks_instagram_photo_post():
    text, menu = telegram_utils._build_main_menu(
        "instagram",
        {
            "title": "Фото-пост",
            "uploader": "tester",
            "duration": 15,
            "_nuvio_instagram_photo_post": True,
            "_nuvio_instagram_images": ["1", "2"],
            "_nuvio_instagram_audio_url": "https://cdn.example/audio.m4a",
        },
        "sessigphoto",
    )

    assert "Кадров: 2" in text
    assert menu.inline_keyboard[0][0].text == telegram_utils.BTN_DOWNLOAD_POST


def test_build_main_menu_hides_instagram_audio_button_without_audio():
    _text, menu = telegram_utils._build_main_menu(
        "instagram",
        {
            "title": "Фото-пост",
            "uploader": "tester",
            "duration": 15,
            "_nuvio_instagram_photo_post": True,
            "_nuvio_instagram_images": ["1", "2"],
            "_nuvio_instagram_audio_url": None,
        },
        "sessigphoto",
    )

    buttons = [button.text for row in menu.inline_keyboard for button in row]
    assert telegram_utils.BTN_AUDIO_ONLY not in buttons


def test_handle_main_callback_reports_missing_instagram_photo_audio_without_failure_log(
    monkeypatch,
):
    logged: list[tuple] = []
    context = SimpleNamespace(user_data={})
    session_token = telegram_utils._store_session(
        context,
        url="https://www.instagram.com/p/DWLd9IYDVeD/",
        video_info={
            "_nuvio_instagram_photo_post": True,
            "_nuvio_instagram_audio_url": None,
            "title": "Фото-пост",
        },
        session_id="session-ig-audio",
        platform="instagram",
        formats={},
    )
    query = _DummyQuery()

    def fake_download_instagram_audio(*args, **kwargs):
        raise tiktok_instagram_utils.PhotoPostAudioMissingError()

    monkeypatch.setattr(
        tiktok_instagram_utils,
        "download_instagram_audio",
        fake_download_instagram_audio,
    )
    monkeypatch.setattr(
        telegram_utils,
        "_schedule_platform_failure_log",
        lambda *args, **kwargs: logged.append((args, kwargs)),
    )
    # Изолируем тест от реального telegram_cache.db: обработчик "instagram_audio"
    # теперь читает кэш перед скачиванием, а _DummyQuery.message не умеет reply_audio.
    monkeypatch.setattr(
        telegram_utils.telegram_cache, "get", lambda *args, **kwargs: None
    )

    asyncio.run(
        telegram_utils._handle_main_callback(
            query,
            context,
            7,
            session_token,
            "instagram_audio",
        )
    )

    assert not logged
    assert query.edits[-1][0] == telegram_utils.PHOTO_POST_AUDIO_UNAVAILABLE


def test_instagram_info_requests_full_metadata(monkeypatch):
    _CapturingYDL.captured_options.clear()
    monkeypatch.setattr(tiktok_instagram_utils.yt_dlp, "YoutubeDL", _CapturingYDL)
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "INSTAGRAM_COOKIES_FILE",
        Path(r"C:\definitely-missing-instagram-cookies.txt"),
    )

    info = tiktok_instagram_utils.get_instagram_info(
        "https://www.instagram.com/reel/abc123/"
    )

    assert info["title"] == "stub"
    assert _CapturingYDL.captured_options
    assert "extract_flat" not in _CapturingYDL.captured_options[0]


def test_social_cookie_paths_are_absolute():
    assert tiktok_instagram_utils.TIKTOK_COOKIES_FILE.is_absolute()
    assert tiktok_instagram_utils.INSTAGRAM_COOKIES_FILE.is_absolute()


def test_cache_stats_requires_admin(monkeypatch):
    message = _DummyMessage("")
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=2),
        message=message,
    )
    context = SimpleNamespace(args=[])

    monkeypatch.setattr(cache_commands, "ADMIN_IDS", [1])

    asyncio.run(cache_commands.stats_command(update, context))

    assert message.replies == ["🔒 Эта команда доступна только администраторам"]


def test_non_admin_document_upload_is_rejected_without_download(monkeypatch):
    document = _DummyDocument("www.youtube.com_cookies.txt")
    message = _DummyMessage("")
    message.document = document
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=2),
        message=message,
    )
    context = SimpleNamespace(user_data={})

    monkeypatch.setattr(cookie_manager, "ADMIN_IDS", [1])

    asyncio.run(cookie_manager.handle_document_upload(update, context))

    assert message.replies == [cookie_manager.NON_ADMIN_DOCUMENT_MESSAGE]
    assert document.get_file_called is False


def test_admin_document_upload_requires_armed_mode(monkeypatch):
    document = _DummyDocument("www.youtube.com_cookies.txt")
    message = _DummyMessage("")
    message.document = document
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        message=message,
    )
    context = SimpleNamespace(user_data={})

    monkeypatch.setattr(cookie_manager, "ADMIN_IDS", [1])

    asyncio.run(cookie_manager.handle_document_upload(update, context))

    assert message.replies == [cookie_manager.ADMIN_UPLOAD_REQUIRED_MESSAGE]
    assert document.get_file_called is False


def test_admin_cookie_upload_renames_txt_to_expected_file(monkeypatch, tmp_path):
    telegram_file = _DummyTelegramFile()
    document = _DownloadableDummyDocument("youtube-cookies-export.txt", telegram_file)
    message = _DummyMessage("")
    message.document = document
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        message=message,
    )
    context = SimpleNamespace(
        user_data={
            cookie_manager.ADMIN_UPLOAD_TARGET_KEY: "www.youtube.com_cookies.txt"
        }
    )

    monkeypatch.setattr(cookie_manager, "ADMIN_IDS", [1])
    monkeypatch.setattr(cookie_manager, "SECRETS_DIR", tmp_path)

    asyncio.run(cookie_manager.handle_document_upload(update, context))

    expected_path = tmp_path / "www.youtube.com_cookies.txt"
    assert document.get_file_called is True
    assert telegram_file.downloaded_to == expected_path
    assert expected_path.exists()
    assert cookie_manager.ADMIN_UPLOAD_TARGET_KEY not in context.user_data
    assert "сохранены как www.youtube.com_cookies.txt" in message.replies[-2]


def test_admin_cookie_upload_rejects_non_txt_name(monkeypatch):
    document = _DummyDocument("youtube-cookies.json")
    message = _DummyMessage("")
    message.document = document
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        message=message,
    )
    context = SimpleNamespace(
        user_data={
            cookie_manager.ADMIN_UPLOAD_TARGET_KEY: "www.youtube.com_cookies.txt"
        }
    )

    monkeypatch.setattr(cookie_manager, "ADMIN_IDS", [1])

    asyncio.run(cookie_manager.handle_document_upload(update, context))

    assert "расширением `.txt`" in message.replies[0]
    assert document.get_file_called is False


def test_admin_callback_arms_specific_cookie_upload(monkeypatch):
    query = _DummyQuery("admin|cookies|upload|youtube", user_id=1)
    update = SimpleNamespace(callback_query=query)
    context = SimpleNamespace(user_data={})

    monkeypatch.setattr(cookie_manager, "ADMIN_IDS", [1])

    asyncio.run(cookie_manager.handle_admin_callback(update, context))

    assert (
        context.user_data[cookie_manager.ADMIN_UPLOAD_TARGET_KEY]
        == "www.youtube.com_cookies.txt"
    )
    assert "Ожидаемый файл: www.youtube.com_cookies.txt" in query.edits[-1][0]


def test_admin_command_shows_cookie_panel(monkeypatch):
    message = _DummyMessage("")
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        message=message,
    )
    context = SimpleNamespace(
        user_data={cookie_manager.ADMIN_UPLOAD_TARGET_KEY: "stale.txt"}
    )

    monkeypatch.setattr(cookie_manager, "ADMIN_IDS", [1])

    asyncio.run(cookie_manager.admin_command(update, context))

    assert cookie_manager.ADMIN_UPLOAD_TARGET_KEY not in context.user_data
    assert "Статус файлов cookies:" in message.reply_calls[-1][0]
    assert "reply_markup" in message.reply_calls[-1][1]


def test_cookie_health_reports_missing_file(monkeypatch, tmp_path):
    missing_path = tmp_path / "missing.txt"
    monkeypatch.setitem(cookie_health.COOKIE_PATHS, "youtube", missing_path)
    cookie_health._COOKIE_HEALTH_CACHE.clear()

    result = cookie_health.check_cookie_health("youtube", force=True)

    assert result.status == "missing"


def test_cookie_health_detects_expired_auth_cookie(monkeypatch, tmp_path):
    cookie_file = tmp_path / "youtube.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1\tSID\texpired\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(cookie_health.COOKIE_PATHS, "youtube", cookie_file)
    cookie_health._COOKIE_HEALTH_CACHE.clear()

    result = cookie_health.check_cookie_health("youtube", force=True)

    assert result.status == "expired"


def test_admin_callback_runs_cookie_health_check(monkeypatch):
    query = _DummyQuery("admin|cookies|check", user_id=1)
    update = SimpleNamespace(callback_query=query)
    context = SimpleNamespace(user_data={})

    monkeypatch.setattr(cookie_manager, "ADMIN_IDS", [1])
    monkeypatch.setattr(
        cookie_manager,
        "check_all_cookie_health",
        lambda: {
            "youtube": cookie_health.CookieHealthResult(
                "youtube", "valid", "probe ok", 0.0, 3, 3
            ),
            "instagram": cookie_health.CookieHealthResult(
                "instagram", "expired", "all auth cookies are expired", 0.0, 2, 0
            ),
            "tiktok": cookie_health.CookieHealthResult(
                "tiktok",
                "rate_limited",
                "platform temporarily rate-limited the validation probe",
                0.0,
                2,
                2,
            ),
        },
    )

    asyncio.run(cookie_manager.handle_admin_callback(update, context))

    assert "Проверка здоровья cookies" in query.edits[-1][0]
    assert "YouTube: активны — probe ok" in query.edits[-1][0]


def test_admin_broadcast_sends_text_to_all_known_users(monkeypatch):
    message = _DummyMessage("Новость для всех")
    status_reply = _EditableReply()

    async def fake_reply_text(text, **kwargs):
        message.replies.append(text)
        message.reply_calls.append((text, kwargs))
        return status_reply

    message.reply_text = fake_reply_text
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        message=message,
    )
    bot = _DummyBot()
    context = SimpleNamespace(
        user_data={cookie_manager.ADMIN_BROADCAST_MODE_KEY: True},
        bot=bot,
    )
    tracked_events = []

    monkeypatch.setattr(cookie_manager, "ADMIN_IDS", [1])
    monkeypatch.setattr(cookie_manager, "get_all_user_ids", lambda: [1, 7, 8])
    monkeypatch.setattr(
        cookie_manager,
        "track_event",
        lambda *args, **kwargs: tracked_events.append((args, kwargs)),
    )

    asyncio.run(cookie_manager.handle_admin_text_input(update, context))

    assert bot.sent_messages == [(7, "Новость для всех"), (8, "Новость для всех")]
    assert cookie_manager.ADMIN_BROADCAST_MODE_KEY not in context.user_data
    assert "Рассылка завершена." in status_reply.edits[-1][0]
    assert len(tracked_events) == 2


def test_search_cache_requires_admin(monkeypatch):
    message = _DummyMessage("")
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=2),
        message=message,
    )
    context = SimpleNamespace(args=["example"])

    monkeypatch.setattr(cache_commands, "ADMIN_IDS", [1])

    asyncio.run(cache_commands.search_cache_command(update, context))

    assert message.replies == ["🔒 Эта команда доступна только администраторам"]


def test_search_cache_escapes_markdown(monkeypatch):
    message = _DummyMessage("")
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=7),
        message=message,
    )
    context = SimpleNamespace(args=["query_[1]"])

    monkeypatch.setattr(cache_commands, "ADMIN_IDS", [7])
    monkeypatch.setattr(
        cache_commands.telegram_cache,
        "search_by_title",
        lambda query, limit=10: [
            SimpleNamespace(
                platform="youtube",
                title="Title_[1]",
                cached_at=datetime(2026, 1, 1),
            )
        ],
    )

    asyncio.run(cache_commands.search_cache_command(update, context))

    text, kwargs = message.reply_calls[-1]
    assert kwargs["parse_mode"] == "Markdown"
    assert r"query\_\[1\]" in text
    assert r"Title\_\[1\]" in text


def test_send_file_keeps_session_but_cleans_media_on_send_failure(monkeypatch):
    query = _DummyQuery()
    context = SimpleNamespace(user_data={})
    cleaned_session_ids = []
    session_token = telegram_utils._store_session(
        context,
        url="https://example.com/1",
        video_info={"title": "One"},
        session_id="session-1",
        platform="youtube",
        formats={"combined": []},
    )
    session_data = telegram_utils._get_session(context, session_token)

    async def fake_send_single_file(*args, **kwargs):
        return False

    monkeypatch.setattr(telegram_utils, "send_single_file", fake_send_single_file)
    monkeypatch.setattr(
        telegram_utils,
        "cleanup_temp_files",
        lambda session_id: cleaned_session_ids.append(session_id),
    )

    asyncio.run(
        telegram_utils.send_file(
            query,
            Path("fake.mp4"),
            session_token,
            session_data,
            context,
        )
    )

    assert telegram_utils._get_session(context, session_token) is session_data
    assert cleaned_session_ids == ["session-1"]


def test_send_file_cleans_session_and_media_on_success(monkeypatch):
    query = _DummyQuery()
    context = SimpleNamespace(user_data={})
    cleaned_session_ids = []
    session_token = telegram_utils._store_session(
        context,
        url="https://example.com/1",
        video_info={"title": "One"},
        session_id="session-1",
        platform="youtube",
        formats={"combined": []},
    )
    session_data = telegram_utils._get_session(context, session_token)

    async def fake_send_single_file(*args, **kwargs):
        return True

    monkeypatch.setattr(telegram_utils, "send_single_file", fake_send_single_file)
    monkeypatch.setattr(
        telegram_utils,
        "cleanup_temp_files",
        lambda session_id: cleaned_session_ids.append(session_id),
    )

    asyncio.run(
        telegram_utils.send_file(
            query,
            Path("fake.mp4"),
            session_token,
            session_data,
            context,
        )
    )

    assert telegram_utils._get_session(context, session_token) is None
    assert cleaned_session_ids == ["session-1"]


def test_process_url_youtube_does_not_short_circuit_by_video_cache(monkeypatch):
    processing_messages: list[_EditableReply] = []
    message = _DummyMessage("https://youtu.be/abc123def45")

    async def fake_reply_text(text, **kwargs):
        if text == telegram_utils.PROCESSING_MESSAGE:
            reply = _EditableReply()
            processing_messages.append(reply)
            return reply
        message.replies.append(text)
        message.reply_calls.append((text, kwargs))
        return None

    message.reply_text = fake_reply_text
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=7),
        message=message,
    )
    context = SimpleNamespace(user_data={}, args=[])

    async def fake_run_blocking(func, *args, **kwargs):
        return {
            "title": "Stub title",
            "uploader": "Tester",
            "duration": 30,
            "formats": [],
        }

    monkeypatch.setattr(telegram_utils, "is_valid_youtube_url", lambda url: True)
    monkeypatch.setattr(
        telegram_utils,
        "get_available_formats",
        lambda video_info: {"combined": [], "video_only": [], "audio_only": []},
    )
    monkeypatch.setattr(telegram_utils, "create_temp_dir", lambda session_id: None)
    monkeypatch.setattr(telegram_utils, "run_blocking", fake_run_blocking)

    asyncio.run(telegram_utils.process_url(update, context, message.text))

    assert processing_messages
    assert "Stub title" in processing_messages[-1].edits[-1][0]


def test_youtube_menu_retries_plain_text_after_telegram_parse_error(monkeypatch):
    import telegram

    message = _DummyMessage("https://youtu.be/abc123def45")
    edits = []

    async def edit_text(text, **kwargs):
        edits.append((text, kwargs))
        if len(edits) == 1:
            raise telegram.error.BadRequest("Can't parse entities: end of entity")

    async def reply_text(text, **kwargs):
        return SimpleNamespace(edit_text=edit_text)

    async def fake_run_blocking(func, *args, **kwargs):
        return {"title": "Тест *звезда*", "uploader": "Автор", "duration": 30, "formats": []}

    message.reply_text = reply_text
    update = SimpleNamespace(effective_user=SimpleNamespace(id=7), message=message)
    context = SimpleNamespace(user_data={}, args=[])
    monkeypatch.setattr(telegram_utils, "is_valid_youtube_url", lambda url: True)
    monkeypatch.setattr(
        telegram_utils,
        "get_available_formats",
        lambda info: {"combined": [], "video_only": [], "audio_only": []},
    )
    monkeypatch.setattr(telegram_utils, "create_temp_dir", lambda session_id: None)
    monkeypatch.setattr(telegram_utils, "run_blocking", fake_run_blocking)

    asyncio.run(telegram_utils.process_url(update, context, message.text))

    assert len(edits) == 2
    assert edits[0][1]["parse_mode"] == "Markdown"
    assert edits[1][1]["parse_mode"] is None
    assert edits[1][1]["reply_markup"] == edits[0][1]["reply_markup"]
    assert "Тест *звезда*" in edits[1][0]


def test_process_url_tiktok_shows_menu(monkeypatch):
    """TikTok URL должен показывать меню, а не отправлять из кэша напрямую."""
    message = _DummyMessage("https://www.tiktok.com/@user/video/1")
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=7),
        message=message,
    )
    context = SimpleNamespace(user_data={}, args=[])
    monkeypatch.setattr(telegram_utils, "is_valid_youtube_url", lambda url: False)
    monkeypatch.setattr(telegram_utils, "is_valid_tiktok_url", lambda url: True)
    monkeypatch.setattr(telegram_utils, "is_instagram_audio_url", lambda url: False)
    monkeypatch.setattr(telegram_utils, "is_valid_instagram_url", lambda url: False)

    # Кэш TikTok проверяется после выбора действия, поэтому здесь показывается меню.
    async def fake_reply_text(text, **kwargs):
        return SimpleNamespace(edit_text=fake_edit_text)

    async def fake_edit_text(text, **kwargs):
        pass

    message.reply_text = fake_reply_text

    async def fake_get_tiktok_info(url):
        return {"title": "Test", "uploader": "User", "duration": 10}

    monkeypatch.setattr(telegram_utils, "get_tiktok_info", fake_get_tiktok_info)
    monkeypatch.setattr(telegram_utils, "run_blocking", lambda func, *a, **kw: func(*a))

    asyncio.run(telegram_utils.process_url(update, context, message.text))

    assert context.user_data["sessions"]


def test_send_single_file_persists_explicit_cache_key(monkeypatch, tmp_path):
    file_path = tmp_path / "clip.mp4"
    file_path.write_bytes(b"video")
    stored = []

    async def fake_reply_video(*args, **kwargs):
        return SimpleNamespace(
            video=SimpleNamespace(
                file_id="file-1",
                file_unique_id="uniq-1",
                file_size=4,
                duration=9,
            )
        )

    query = _DummyQuery()
    query.message.reply_video = fake_reply_video
    monkeypatch.setattr(
        telegram_utils.telegram_cache, "set", lambda cached: stored.append(cached)
    )

    result = asyncio.run(
        telegram_utils.send_single_file(
            query,
            file_path,
            "sess-token",
            {
                "url": "https://youtu.be/abc123def45",
                "video_info": {"title": "Cached clip"},
                "platform": "youtube",
            },
            cache_format_id="tg_video",
        )
    )

    assert result is True
    assert stored
    assert stored[0].format_id == "tg_video"


def test_create_tiktok_ytdl_modifies_formats():
    # Создаем фиктивный класс ytdlp.YoutubeDL для тестирования, чтобы не зависеть от реальной сети
    class DummyYDL:
        def __init__(self, opts):
            self.opts = opts

    # Подменим yt_dlp.YoutubeDL на DummyYDL
    original_ydl = tiktok_instagram_utils.yt_dlp.YoutubeDL
    tiktok_instagram_utils.yt_dlp.YoutubeDL = DummyYDL
    try:
        ydl = tiktok_instagram_utils.create_tiktok_ytdl({})

        info = {
            "extractor": "TikTok",
            "id": "12345",
            "formats": [
                {
                    "format_id": "bytevc1_1080p",
                    "vcodec": "h265",
                    "acodec": "aac",
                    "url": "https://example.com/media-video-hvc1/1.mp4",
                },
                {
                    "format_id": "h264_540p",
                    "vcodec": "h264",
                    "acodec": "aac",
                    "url": "https://example.com/1.mp4",
                }
            ]
        }

        # Вызовем _modify_formats
        ydl._modify_formats(info)

        # Проверяем, что HEVC формат изменен
        hevc_fmt = next(f for f in info["formats"] if f["format_id"] == "bytevc1_1080p")
        assert hevc_fmt["acodec"] == "none"
        assert hevc_fmt["audio_ext"] == "none"

        # Проверяем, что добавлен виртуальный аудио-формат
        virt_audio = next(f for f in info["formats"] if f["format_id"] == "virtual_audio_from_muxed")
        assert virt_audio["vcodec"] == "none"
        assert virt_audio["ext"] == "m4a"

    finally:
        tiktok_instagram_utils.yt_dlp.YoutubeDL = original_ydl


def test_hevc_video_conversion_triggered(monkeypatch, tmp_path):
    """HEVC обязан перекодироваться, а исходник — удаляться (ADR-001).

    Решение о перекодировании переехало в `media_processor` и расширилось до
    VP9, AV1 и H.264 High 10 (ADR-002), поэтому подменяется общий шов. Раньше
    этот тест повторял логику у себя внутри и настоящий код не трогал вовсе.
    """
    from utils import media_processor

    converted_files = []

    def mock_convert_to_format(input_path, output_format, session_id):
        converted_files.append(input_path)
        out_file = input_path.parent / f"converted_{input_path.name}"
        out_file.write_bytes(b"converted_data")
        return out_file

    monkeypatch.setattr(media_processor, "needs_ios_reencode", lambda path: True)
    monkeypatch.setattr(media_processor, "convert_to_format", mock_convert_to_format)

    hevc_file = tmp_path / "hevc_video.mp4"
    hevc_file.write_bytes(b"hevc_data")

    downloaded_file = tiktok_instagram_utils._ensure_ios_compatible_video(
        hevc_file, "test_sess", "TikTok"
    )

    assert downloaded_file.name == "converted_hevc_video.mp4"
    assert not hevc_file.exists()
    assert downloaded_file.read_bytes() == b"converted_data"
    assert converted_files == [hevc_file]
