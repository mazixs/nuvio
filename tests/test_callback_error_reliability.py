"""Регрессии отмены, повторных нажатий и честной обратной связи при сбоях."""

import asyncio
import errno
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram.ext import ApplicationHandlerStop
from telegram.error import BadRequest, NetworkError, TimedOut

from utils import feedback, telegram_utils as tg
from utils.callback_fsm import CallbackEvent
from utils.cancellation import forget_cancellation, is_cancelled
from utils.public_errors import (
    build_public_error_message,
    classify_internal_error_category,
)
from utils.url_delivery import UrlHandoff
from utils.ytdlp_common import classify_download_error_kind


@asynccontextmanager
async def _pulse(*args):
    yield


def _context(platform="youtube"):
    return SimpleNamespace(
        user_data={
            "sessions": {
                "token123": {
                    "session_id": "callback-test",
                    "platform": platform,
                    "url": "https://example.test/video",
                    "video_info": {"subtitles": {"en": [{}]}},
                    "formats": {
                        "combined": [
                            {
                                "format_id": "22",
                                "height": 720,
                                "filesize": 1000,
                                "ext": "mp4",
                            }
                        ]
                    },
                }
            }
        }
    )


def _query(data):
    return SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        from_user=SimpleNamespace(id=12),
        message=SimpleNamespace(chat=SimpleNamespace(send_action=AsyncMock())),
    )


def _update(query):
    return SimpleNamespace(callback_query=query, effective_user=query.from_user)


@pytest.mark.parametrize(
    "action", ["cancel", "more", "back", "video_menu", "audio_menu", "subtitles"]
)
def test_navigation_and_cancel_bypass_spam_limit(action):
    assert not tg._should_rate_limit_callback(f"s|token123|main|{action}")
    assert not tg._should_rate_limit_callback("s|token123|format|subs_lang|en")


def test_cancel_still_stops_a_spam_limited_session(monkeypatch):
    context = _context()
    query = _query("s|token123|main|cancel")
    monkeypatch.setattr(tg, "_pulsing_chat_action", _pulse)

    async def run():
        context.user_data["spam_blocked_until"] = (
            asyncio.get_running_loop().time() + 100
        )
        await tg.button_callback(_update(query), context)

    try:
        asyncio.run(run())
        assert is_cancelled("callback-test")
        assert "token123" not in context.user_data["sessions"]
        assert query.edit_message_text.call_args.args[0] == tg.CANCELLED_MESSAGE
    finally:
        forget_cancellation("callback-test")


@pytest.mark.parametrize(
    ("platform", "callback"),
    [
        ("youtube", "format|combined|22"),
        ("youtube", "main|tg_video"),
        ("youtube", "main|audio_m4a"),
        ("youtube", "format|subs|en:srt"),
        ("tiktok", "main|tiktok_audio"),
        ("instagram", "main|instagram_audio"),
        ("rutube", "main|rutube_download"),
        ("vk", "main|vk_audio"),
    ],
)
def test_concurrent_callback_starts_only_one_task(monkeypatch, platform, callback):
    context = _context(platform)
    session = context.user_data["sessions"]["token123"]
    monkeypatch.setattr(tg, "_pulsing_chat_action", _pulse)
    entered = 0

    async def run():
        nonlocal entered
        started, release = asyncio.Event(), asyncio.Event()

        async def handler(*args):
            nonlocal entered
            entered += 1
            started.set()
            await release.wait()

        monkeypatch.setattr(tg, "_handle_main_callback", handler)
        monkeypatch.setattr(tg, "_handle_format_callback", handler)
        first = asyncio.create_task(
            tg.button_callback(_update(_query(f"s|token123|{callback}")), context)
        )
        try:
            await asyncio.wait_for(started.wait(), 1)
            await tg.button_callback(_update(_query(f"s|token123|{callback}")), context)
            assert entered == 1
        finally:
            release.set()
            await first

    asyncio.run(run())
    assert not session.get("_delivery_active")
    assert "callback-test" not in tg.active_download_sessions()


def test_cancel_remains_available_during_format_download(monkeypatch):
    context = _context()
    session = context.user_data["sessions"]["token123"]
    session["_delivery_active"] = True
    monkeypatch.setattr(tg, "_pulsing_chat_action", _pulse)
    try:
        asyncio.run(
            tg.button_callback(_update(_query("s|token123|main|cancel")), context)
        )
        assert is_cancelled("callback-test")
    finally:
        forget_cancellation("callback-test")


@pytest.mark.parametrize("error", [TimeoutError(), OSError(errno.ENOSPC, "disk full")])
def test_subtitle_technical_failures_keep_code_and_diagnostics(monkeypatch, error):
    context = _context()
    session = context.user_data["sessions"]["token123"]
    query = _query("unused")
    reports = []
    monkeypatch.setattr(tg, "run_blocking", AsyncMock(side_effect=error))
    monkeypatch.setattr(
        tg, "_schedule_platform_failure_log", lambda **kwargs: reports.append(kwargs)
    )
    asyncio.run(
        tg._download_and_send_subtitles(
            query, context, 12, "token123", session, "en:srt"
        )
    )
    text = query.edit_message_text.call_args.args[0]
    assert text != tg.NO_SUBTITLES_AVAILABLE
    assert reports[0]["error_code"] in text
    assert reports[0]["exc"] is error
    assert (
        query.edit_message_text.call_args.kwargs["reply_markup"]
        .inline_keyboard[-1][0]
        .callback_data.startswith("feedback|open|")
    )


def test_missing_subtitle_track_remains_an_expected_empty_result(monkeypatch):
    context = _context()
    query = _query("unused")
    monkeypatch.setattr(tg, "run_blocking", AsyncMock(return_value=None))
    report = Mock()
    monkeypatch.setattr(tg, "_schedule_platform_failure_log", report)
    asyncio.run(
        tg._download_and_send_subtitles(
            query,
            context,
            12,
            "token123",
            context.user_data["sessions"]["token123"],
            "en:srt",
        )
    )
    assert query.edit_message_text.call_args.args[0] == tg.NO_SUBTITLES_AVAILABLE
    report.assert_not_called()


def test_failed_feedback_prompt_does_not_consume_next_url(monkeypatch):
    message = SimpleNamespace(
        reply_text=AsyncMock(side_effect=NetworkError("prompt failed"))
    )
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(type="private"), effective_message=message
    )
    context = SimpleNamespace(user_data={})
    with pytest.raises(NetworkError):
        asyncio.run(feedback.feedback_command(update, context))
    assert feedback.FORM_KEY not in context.user_data
    saved = Mock()
    monkeypatch.setattr(feedback, "save_feedback", saved)
    next_message = SimpleNamespace(
        text="https://example.test/next-video", message_id=2, reply_text=AsyncMock()
    )
    update.effective_message = next_message
    asyncio.run(feedback.feedback_text(update, context))
    saved.assert_not_called()
    next_message.reply_text.assert_not_awaited()


def test_old_failed_feedback_prompt_preserves_newer_form():
    context = SimpleNamespace(user_data={})
    calls = 0

    async def run():
        nonlocal calls
        entered, release = asyncio.Event(), asyncio.Event()

        async def reply(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
                raise NetworkError("old prompt failed")

        update = SimpleNamespace(
            effective_chat=SimpleNamespace(type="private"),
            effective_message=SimpleNamespace(reply_text=reply),
        )
        first = asyncio.create_task(feedback.feedback_command(update, context))
        await entered.wait()
        await feedback.feedback_command(update, context)
        newer = context.user_data[feedback.FORM_KEY]
        release.set()
        with pytest.raises(NetworkError):
            await first
        assert context.user_data[feedback.FORM_KEY] is newer

    asyncio.run(run())


@pytest.mark.parametrize("error", [BadRequest("Query is too old"), TimedOut()])
def test_ack_failure_does_not_prevent_feedback_opening(error):
    query = _query("feedback|open|general")
    query.answer.side_effect = error
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(type="private"),
        effective_message=message,
    )
    context = SimpleNamespace(user_data={})
    asyncio.run(feedback.feedback_callback(update, context))
    message.reply_text.assert_awaited_once()
    assert feedback.FORM_KEY in context.user_data


def test_ack_failure_does_not_prevent_admin_action(monkeypatch):
    from utils import cookie_manager

    query = _query("admin|cookies|cancel")
    query.answer.side_effect = BadRequest("Query is too old")
    monkeypatch.setattr(cookie_manager, "is_admin", lambda uid: True)
    monkeypatch.setattr(cookie_manager, "_build_admin_panel_text", lambda *a: "panel")
    context = SimpleNamespace(
        user_data={cookie_manager.ADMIN_UPLOAD_TARGET_KEY: "demo.txt"}
    )
    asyncio.run(cookie_manager.handle_admin_callback(_update(query), context))
    assert cookie_manager.ADMIN_UPLOAD_TARGET_KEY not in context.user_data
    query.edit_message_text.assert_awaited_once()


@pytest.mark.parametrize("platform", ["instagram", "tiktok", "rutube", "vk", "youtube"])
@pytest.mark.parametrize(
    "detail",
    [
        "Private delivery cache failed",
        "Subprocess output is unavailable",
        "Internal worker blocked",
        "Internal operation forbidden",
        "Внутренний буфер недоступен",
    ],
)
def test_internal_words_do_not_imply_platform_access_refusal(platform, detail):
    error = RuntimeError(detail)
    assert classify_internal_error_category(platform, error) == "UNEXPECT"
    text = build_public_error_message(platform, "BOT-UNEXPECT-ABC123", error)
    assert "авторизации" not in text
    assert "удален" not in text
    assert "позже" not in text
    assert detail not in text


@pytest.mark.parametrize("selector", ["all", "best", "22/999", "22[filesize>1]", "999"])
def test_forged_format_does_not_start_downloader(monkeypatch, selector):
    context = _context()
    query = _query("unused")
    download = AsyncMock()
    monkeypatch.setattr(tg, "download_content", download)
    asyncio.run(
        tg._handle_format_callback(query, context, 12, "token123", "combined", selector)
    )
    download.assert_not_awaited()
    assert query.edit_message_text.call_args.args[0] == tg.SESSION_EXPIRED


def test_valid_menu_format_still_reaches_downloader(monkeypatch):
    context = _context()
    query = _query("unused")
    monkeypatch.setattr(tg, "_deliver_cached_video", AsyncMock(return_value=False))
    monkeypatch.setattr(tg, "_deliver_plan", AsyncMock(return_value=False))
    download = AsyncMock(return_value=None)
    monkeypatch.setattr(tg, "download_content", download)
    monkeypatch.setattr(tg, "_cleanup_user_session", AsyncMock())
    asyncio.run(
        tg._handle_format_callback(query, context, 12, "token123", "combined", "22")
    )
    assert download.call_args.args[1] == "22"


@pytest.mark.parametrize("platform", ["youtube", "rutube", "vk", "tiktok", "instagram"])
@pytest.mark.parametrize(
    "error", [NetworkError("connection lost after transmission"), TimedOut()]
)
def test_uncertain_file_delivery_sends_once_and_does_not_invite_retry(
    monkeypatch, tmp_path, platform, error
):
    media = tmp_path / "media.mp3"
    media.write_bytes(b"demo")
    query = _query("unused")
    query.message.reply_audio = AsyncMock(side_effect=error)
    monkeypatch.setattr(tg, "_schedule_platform_failure_log", lambda **kwargs: None)
    session = {
        "platform": platform,
        "url": "https://example.test/video",
        "session_id": "file-send-test",
    }
    assert asyncio.run(tg.send_single_file(query, media, "token123", session)) is False
    assert query.message.reply_audio.await_count == 1
    assert session["_delivery_outcome_unknown"] is True
    assert query.edit_message_text.call_args.args[0] == tg.DELIVERY_OUTCOME_UNKNOWN


@pytest.mark.parametrize("platform", ["youtube", "rutube", "vk", "tiktok", "instagram"])
def test_uncertain_url_handoff_prevents_fallback(platform):
    query = _query("unused")
    query.message.reply_video = AsyncMock(
        side_effect=NetworkError("connection lost after transmission")
    )
    plan = UrlHandoff(kind="video", url="https://example.test/video.mp4", size=1000)
    session = {"platform": platform}
    result = asyncio.run(
        tg._deliver_by_url(
            query, plan, "https://example.test/watch", platform, session_data=session
        )
    )
    assert result.state == "unknown"
    assert session["_delivery_outcome_unknown"] is True
    assert query.message.reply_video.await_count == 1


@pytest.mark.parametrize(
    "detail",
    [
        "ERROR: unable to download video data: fragment not found",
        "Connection reset by peer",
        "Network is unreachable",
        "Read timed out",
        "HTTP Error 429",
        "Private video",
        "requires a JavaScript runtime",
    ],
)
def test_download_and_public_classifiers_use_same_known_categories(detail):
    assert classify_download_error_kind(detail) == classify_internal_error_category(
        "youtube", detail
    )


@pytest.mark.parametrize(
    "data", ["csi|²", "csi|１２", "csi|111111111111111111111", "csi|-1", "csi|11"]
)
def test_malformed_csi_data_is_rejected_without_parser_exception(data):
    assert CallbackEvent.parse(data) is None


@pytest.mark.parametrize("rating", [0, 6, 10])
def test_valid_csi_rating_is_preserved(rating):
    assert CallbackEvent.parse(f"csi|{rating}").value == str(rating)


def test_expired_malformed_csi_ack_does_not_report_a_crash(monkeypatch):
    query = _query("csi|²")
    query.answer.side_effect = BadRequest("Query is too old")
    report = Mock()
    monkeypatch.setattr(tg, "_schedule_platform_failure_log", report)
    asyncio.run(tg.button_callback(_update(query), SimpleNamespace(user_data={})))
    report.assert_not_called()
    assert query.answer.call_args.args[0] == tg.CSI_INVALID_RATING


def test_feedback_text_before_prompt_confirmation_does_not_reach_downloader():
    context = SimpleNamespace(user_data={})

    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def reply(*args, **kwargs):
            started.set()
            await release.wait()
            raise NetworkError("prompt failed")

        update = SimpleNamespace(
            effective_chat=SimpleNamespace(type="private"),
            effective_message=SimpleNamespace(reply_text=reply),
        )
        pending = asyncio.create_task(feedback.feedback_command(update, context))
        await started.wait()
        incoming = SimpleNamespace(
            effective_message=SimpleNamespace(text="https://example.test/video", reply_text=AsyncMock())
        )
        with pytest.raises(ApplicationHandlerStop):
            await feedback.feedback_text(incoming, context)
        release.set()
        with pytest.raises(NetworkError):
            await pending
        assert feedback.FORM_KEY not in context.user_data

    asyncio.run(run())


@pytest.mark.parametrize(
    "callback", ["main|tg_video", "format|combined|22", "main|audio_m4a"]
)
def test_cached_media_timeout_is_unknown_and_never_downloads_again(
    monkeypatch, callback
):
    context = _context()
    session = context.user_data["sessions"]["token123"]
    query = _query(f"s|token123|{callback}")
    query.message.reply_video = AsyncMock(side_effect=TimedOut())
    query.message.reply_audio = AsyncMock(side_effect=TimedOut())
    monkeypatch.setattr(tg, "_pulsing_chat_action", _pulse)
    monkeypatch.setattr(
        tg.telegram_cache,
        "get",
        lambda *a, **k: SimpleNamespace(file_id="demo-file-id"),
    )
    download = AsyncMock()
    monkeypatch.setattr(tg, "download_content", download)
    monkeypatch.setattr(tg, "_schedule_platform_failure_log", lambda **kwargs: None)
    asyncio.run(tg.button_callback(_update(query), context))
    download.assert_not_awaited()
    assert (
        query.message.reply_video.await_count + query.message.reply_audio.await_count
        == 1
    )
    assert session["_delivery_outcome_unknown"] is True
    assert query.edit_message_text.call_args.args[0] == tg.DELIVERY_OUTCOME_UNKNOWN
    assert tg._active_delivery_session.get() is None


@pytest.mark.parametrize("kind", ["audio", "video"])
def test_cached_api_refusal_does_not_invalidate_a_file_id(monkeypatch, kind):
    query = _query("unused")
    sender = AsyncMock(side_effect=BadRequest("Chat not found"))
    setattr(query.message, f"reply_{kind}", sender)
    monkeypatch.setattr(
        tg.telegram_cache,
        "get",
        lambda *a, **k: SimpleNamespace(file_id="demo-file-id"),
    )
    delete = Mock()
    monkeypatch.setattr(tg.telegram_cache, "delete_by_file_id", delete)
    with pytest.raises(BadRequest):
        asyncio.run(
            getattr(tg, f"_deliver_cached_{kind}")(
                query, "https://example.test/video", "demo-key"
            )
        )
    delete.assert_not_called()
    sender.assert_awaited_once()


def test_description_timeout_preserves_confirmed_media_delivery():
    query = _query("unused")
    query.message.reply_text = AsyncMock(side_effect=TimedOut())
    session = {"session_id": "description-timeout-test", "_delivered_items": 1}
    result = asyncio.run(
        tg._send_description_chunks(
            query, ["Описание"], SimpleNamespace(message_id=1), session
        )
    )
    assert result is False
    assert session["_description_delivery_failed"] is True
    assert not session.get("_delivery_outcome_unknown")
    assert session["_delivered_items"] == 1
    query.message.reply_text.assert_awaited_once()


def test_cancellation_defers_cleanup_until_callback_finishes(monkeypatch):
    context = _context()
    cleanup = Mock()
    monkeypatch.setattr(tg, "cleanup_temp_files", cleanup)
    monkeypatch.setattr(tg, "_pulsing_chat_action", _pulse)

    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def download(*args):
            started.set()
            await release.wait()

        monkeypatch.setattr(tg, "_handle_format_callback", download)
        pending = asyncio.create_task(
            tg.button_callback(
                _update(_query("s|token123|format|combined|22")), context
            )
        )
        try:
            await started.wait()
            await tg.button_callback(_update(_query("s|token123|main|cancel")), context)
            assert is_cancelled("callback-test")
            cleanup.assert_not_called()
        finally:
            release.set()
            await pending

    try:
        asyncio.run(run())
        cleanup.assert_called_once_with("callback-test")
    finally:
        forget_cancellation("callback-test")


@pytest.mark.parametrize(
    "scope", ["main|vk_download", "format|audio_only|999", "format|subs|ru:srt"]
)
def test_unoffered_callback_cannot_enter_a_handler(monkeypatch, scope):
    context = _context()
    query = _query(f"s|token123|{scope}")
    handler = AsyncMock()
    monkeypatch.setattr(tg, "_handle_main_callback", handler)
    monkeypatch.setattr(tg, "_handle_format_callback", handler)
    asyncio.run(tg.button_callback(_update(query), context))
    handler.assert_not_awaited()
    assert query.edit_message_text.call_args.args[0] == tg.SESSION_EXPIRED


def test_empty_platform_response_does_not_guess_why_access_failed():
    import yt_dlp

    error = yt_dlp.utils.DownloadError(
        "ERROR: [Instagram] demo: Instagram sent an empty media response"
    )
    assert classify_internal_error_category("instagram", error) == "DATA"
    text = build_public_error_message("instagram", "IG-DATA-ABC123", error)
    assert "авторизации" not in text
    assert "удален" not in text
    assert "позже" not in text


@pytest.mark.parametrize(
    ("platform", "action"),
    [
        ("youtube", "tg_video"),
        ("tiktok", "tiktok_download"),
        ("instagram", "instagram_download"),
        ("rutube", "rutube_download"),
        ("vk", "vk_download"),
    ],
)
def test_metadata_processing_only_allows_cancel_then_unlocks_menu(
    monkeypatch, platform, action
):
    context = SimpleNamespace(user_data={})
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=12),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    monkeypatch.setattr(tg, "create_temp_dir", lambda sid: None)
    _, token, _ = asyncio.run(
        tg._begin_processing(update, context, "https://example.test/video", platform)
    )
    session = context.user_data["sessions"][token]
    assert tg._is_main_action_allowed(session, "cancel")
    assert not tg._is_main_action_allowed(session, action)
    assert not tg._is_main_action_allowed(session, "back")
    assert not tg._is_format_selection_allowed(session, "combined", "22")
    assert tg._finish_processing(context, token, {"title": "Demo"}, {})
    assert tg._is_main_action_allowed(session, action)
