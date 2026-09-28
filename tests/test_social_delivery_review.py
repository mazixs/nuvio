"""Регрессии, найденные при ревью альбомов и описаний."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import telegram

from utils import telegram_utils as delivery
from utils import tiktok_instagram_utils as sources
from utils.cancellation import (
    CancelledByUser,
    forget_cancellation,
    request_cancellation,
)
from utils.platform_actions import cache_key_for_main_action


def test_photo_only_instagram_sidecar_matches_playlist(monkeypatch):
    media = {
        "carousel_media": [
            {"display_url": "https://cdn.example/1.jpg"},
            {"display_url": "https://cdn.example/2.jpg"},
        ]
    }
    info = sources._build_instagram_photo_info(
        "https://www.instagram.com/p/test/", media
    )
    monkeypatch.setattr(sources, "_try_get_instagram_photo_info", lambda _url: info)
    enriched = sources._enrich_instagram_carousel_info(
        "https://www.instagram.com/p/test/", {"entries": [{"id": "1"}, {"id": "2"}]}
    )
    assert not enriched.get("_nuvio_instagram_carousel_incomplete")
    assert len(enriched["_nuvio_instagram_carousel_items"]) == 2


def test_missing_photo_in_sidecar_is_not_silently_dropped():
    info = sources._build_instagram_photo_info(
        "https://www.instagram.com/p/test/",
        {"carousel_media": [{"display_url": "https://cdn.example/1.jpg"}, {}]},
    )
    assert info["_nuvio_instagram_carousel_incomplete"] is True


@pytest.mark.parametrize("platform", ["tiktok", "instagram"])
def test_cache_caption_error_does_not_invalidate_file_or_download(
    monkeypatch, platform
):
    session = {
        "platform": platform,
        "session_id": "cache-refusal",
        "url": "https://example.test/post",
    }
    monkeypatch.setattr(delivery, "_get_session", lambda *_args: session)
    cache = SimpleNamespace(
        get=Mock(return_value=SimpleNamespace(file_id="cached")),
        delete_by_file_id=Mock(),
    )
    monkeypatch.setattr(delivery, "telegram_cache", cache)
    query = SimpleNamespace(
        message=SimpleNamespace(
            reply_video=AsyncMock(
                side_effect=telegram.error.BadRequest("caption is too long")
            )
        )
    )
    with pytest.raises(telegram.error.BadRequest, match="caption"):
        asyncio.run(
            delivery._handle_main_callback(
                query, SimpleNamespace(), 1, "token", f"{platform}_download_desc"
            )
        )
    cache.delete_by_file_id.assert_not_called()


@pytest.mark.parametrize(
    "platform,action",
    [
        ("tiktok", "instagram_download_desc"),
        ("instagram", "tiktok_audio"),
        ("vk", "instagram_download"),
    ],
)
def test_cache_action_must_match_platform(platform, action):
    assert cache_key_for_main_action(platform, action) is None


def test_cancellation_before_telegram_call_never_sends():
    session_id = "review-cancel-before-call"
    call = AsyncMock()
    request_cancellation(session_id)
    try:
        with pytest.raises(CancelledByUser):
            asyncio.run(
                delivery._call_telegram_with_retry_after(
                    call, {"session_id": session_id}
                )
            )
    finally:
        forget_cancellation(session_id)
    call.assert_not_awaited()


def test_partial_callback_keeps_receipts_and_choice(monkeypatch):
    session = {
        "platform": "tiktok",
        "session_id": "review-partial",
        "_delivered_items": 2,
        "_delivery_progress": 2,
        "_delivery_action": "tiktok_download_desc",
        "_description_attempted": True,
        "_description_delivery_failed": True,
    }
    context = SimpleNamespace(user_data={"sessions": {"token1234": session}})
    seen = []

    async def handle(*_args):
        seen.append(
            (
                session["_delivery_progress"],
                session["_include_description"],
                session["_description_delivery_failed"],
            )
        )

    @asynccontextmanager
    async def pulse(*_args):
        yield

    monkeypatch.setattr(delivery, "_handle_main_callback", handle)
    monkeypatch.setattr(delivery, "_pulsing_chat_action", pulse)
    query = SimpleNamespace(
        data="s|token1234|main|tiktok_download_desc",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=SimpleNamespace(chat=SimpleNamespace()),
    )
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=1))
    asyncio.run(delivery.button_callback(update, context))
    query.data = "s|token1234|main|tiktok_download"
    asyncio.run(delivery.button_callback(update, context))
    assert seen == [(2, True, True)]


def test_partial_retry_skips_url_and_sends_only_remaining_files(monkeypatch, tmp_path):
    paths = [tmp_path / f"{i}.jpg" for i in range(3)]
    resolver = Mock(side_effect=AssertionError("URL уже частично отправлен"))
    monkeypatch.setattr(sources, "resolve_photo_post_handoff", resolver)
    monkeypatch.setattr(
        sources, "download_tiktok_photo_post_assets", lambda *_args: {"images": paths}
    )

    async def blocking(func, *args, **_kwargs):
        return func(*args)

    monkeypatch.setattr(delivery, "run_blocking", blocking)
    send = AsyncMock(return_value=[SimpleNamespace(message_id=3)])
    monkeypatch.setattr(delivery, "_send_photo_file_group", send)
    monkeypatch.setattr(delivery, "_record_delivery", AsyncMock())
    monkeypatch.setattr(delivery, "_cleanup_user_session", AsyncMock())
    session = {
        "url": "https://example.test/post",
        "session_id": "review-partial-files",
        "platform": "tiktok",
        "_delivered_items": 2,
        "_delivery_progress": 2,
        "_first_media_message": SimpleNamespace(message_id=1),
        "video_info": {"_nuvio_tiktok_images": ["a", "b", "c"]},
    }
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=1), edit_message_text=AsyncMock()
    )
    asyncio.run(
        delivery._send_photo_post_assets(query, "token", session, SimpleNamespace())
    )
    resolver.assert_not_called()
    assert send.await_args.args[1] == [paths[2]]
    assert session["_delivered_items"] == 3
    first_markup = query.edit_message_text.await_args_list[0].kwargs["reply_markup"]
    assert first_markup.inline_keyboard[0][0].callback_data.endswith("|cancel")


def test_individual_fallback_preserves_first_receipt_when_second_fails(
    monkeypatch, tmp_path
):
    paths = [tmp_path / f"{i}.jpg" for i in range(2)]
    for path in paths:
        path.write_bytes(b"image")
    monkeypatch.setattr(delivery, "TELEGRAM_LOCAL_MODE", False)
    first = SimpleNamespace(message_id=5)
    message = SimpleNamespace(
        reply_media_group=AsyncMock(
            side_effect=telegram.error.BadRequest("PHOTO_INVALID_DIMENSIONS")
        ),
        reply_photo=AsyncMock(
            side_effect=[first, telegram.error.BadRequest("chat write forbidden")]
        ),
    )
    session = {"session_id": "review-fallback"}
    with pytest.raises(telegram.error.BadRequest):
        asyncio.run(
            delivery._send_photo_file_group(
                SimpleNamespace(message=message), paths, "caption", session
            )
        )
    assert session["_first_media_message"] is first
    assert session["_delivered_items"] == 1
    assert session["_confirmed_delivery_messages"] == (first,)


def test_album_input_streams_stay_open_until_upload(monkeypatch, tmp_path):
    paths = [tmp_path / f"{i}.jpg" for i in range(2)]
    for path in paths:
        path.write_bytes(b"image")
    handles = []

    async def send(*, media, **kwargs):
        assert kwargs["do_quote"] is False
        for item in media:
            handle = item.media.input_file_content
            assert hasattr(handle, "read")
            assert not handle.closed
            handles.append(handle)
        return [SimpleNamespace(message_id=i) for i in range(2)]

    monkeypatch.setattr(delivery, "TELEGRAM_LOCAL_MODE", False)
    query = SimpleNamespace(message=SimpleNamespace(reply_media_group=send))
    asyncio.run(
        delivery._send_photo_file_group(query, paths, None, {"session_id": "streaming"})
    )
    assert all(handle.closed for handle in handles)


def test_missing_reply_target_preserves_topic_without_quoting_status(monkeypatch):
    bot = telegram.Bot("123:ABC")
    send = AsyncMock(
        side_effect=[
            telegram.error.BadRequest("Message to reply not found"),
            SimpleNamespace(message_id=3),
        ]
    )
    monkeypatch.setattr(telegram.Bot, "send_message", send)
    message = telegram.Message(
        message_id=42,
        date=datetime.now(),
        chat=telegram.Chat(-100123, "supergroup"),
        is_topic_message=True,
        message_thread_id=8,
    )
    message.set_bot(bot)
    assert asyncio.run(
        delivery._send_description_chunks(
            SimpleNamespace(message=message),
            ["recipe"],
            SimpleNamespace(message_id=2),
            {"session_id": "topic"},
        )
    )
    retried = send.await_args.kwargs
    assert retried["chat_id"] == -100123
    assert retried["message_thread_id"] == 8
    assert retried["reply_parameters"] is None


def test_late_discovery_of_mixed_carousel_uses_all_media(monkeypatch):
    info = {
        "_nuvio_instagram_photo_post": True,
        "_nuvio_instagram_mixed_post": True,
        "_nuvio_instagram_carousel_complete": True,
    }
    monkeypatch.setattr(sources, "_fetch_instagram_photo_post_media", lambda _url: {})
    monkeypatch.setattr(sources, "_build_instagram_photo_info", lambda *_args: info)
    items = [{"kind": "photo", "path": "1.jpg"}, {"kind": "video", "path": "2.mp4"}]
    monkeypatch.setattr(
        sources,
        "_collect_instagram_mixed_carousel_assets",
        lambda *_args: (info, items, None),
    )
    photo_only = Mock(
        side_effect=AssertionError("Смешанный пост не должен терять видео")
    )
    monkeypatch.setattr(sources, "_collect_instagram_photo_assets", photo_only)
    result = sources.download_instagram_photo_post_assets(
        "https://example.test/post", "late", {}
    )
    assert result["items"] == items
    photo_only.assert_not_called()


def test_html_cover_cannot_confirm_complete_instagram_post():
    info = sources._build_instagram_photo_info(
        "https://www.instagram.com/p/test/",
        {
            "display_url": "https://cdn.example/cover.jpg",
            "_nuvio_description_source": "instagram_html_meta",
        },
    )
    assert info["_nuvio_instagram_carousel_incomplete"] is True
    assert info["_nuvio_instagram_carousel_complete"] is False


def test_invalid_sidecar_node_cannot_disappear_from_completeness_check():
    info = sources._build_instagram_photo_info(
        "https://www.instagram.com/p/test/",
        {"carousel_media": [{"display_url": "https://cdn.example/1.jpg"}, None]},
    )
    assert info["_nuvio_instagram_carousel_incomplete"] is True


def test_audio_timeout_returns_unknown_after_confirmed_photos():
    from utils.url_delivery import PhotoPostHandoff, UrlHandoff

    query = SimpleNamespace(
        message=SimpleNamespace(
            reply_photo=AsyncMock(return_value=SimpleNamespace(message_id=1)),
            reply_audio=AsyncMock(side_effect=telegram.error.TimedOut()),
        )
    )
    plan = PhotoPostHandoff(
        images=(
            UrlHandoff(url="https://cdn.example/review.jpg", kind="photo", size=100),
        ),
        audio=UrlHandoff(url="https://cdn.example/review.m4a", kind="audio", size=100),
    )
    result = asyncio.run(delivery._deliver_photo_post_by_url(query, plan, {}))
    assert result.state == "unknown"
    assert result.confirmed_items == 1
    assert result.audio_delivered is None


def test_status_timeout_after_cached_video_keeps_confirmed_delivery(monkeypatch):
    session = {
        "platform": "tiktok",
        "session_id": "status-timeout",
        "url": "https://example.test/post",
    }
    monkeypatch.setattr(delivery, "_get_session", lambda *_args: session)
    cache = SimpleNamespace(
        get=Mock(return_value=SimpleNamespace(file_id="cached")),
        delete_by_file_id=Mock(),
    )
    monkeypatch.setattr(delivery, "telegram_cache", cache)
    cleanup = AsyncMock()
    monkeypatch.setattr(delivery, "_cleanup_user_session", cleanup)
    monkeypatch.setattr(delivery, "_record_delivery", AsyncMock())
    query = SimpleNamespace(
        message=SimpleNamespace(
            reply_video=AsyncMock(return_value=SimpleNamespace(message_id=1))
        ),
        from_user=SimpleNamespace(id=1),
        edit_message_text=AsyncMock(side_effect=telegram.error.TimedOut()),
    )
    asyncio.run(
        delivery._handle_main_callback(
            query, SimpleNamespace(), 1, "token", "tiktok_download"
        )
    )
    assert session["_delivered_items"] == 1
    assert not session.get("_delivery_outcome_unknown")
    query.message.reply_video.assert_awaited_once()
    cache.delete_by_file_id.assert_not_called()
    cleanup.assert_awaited_once()


@pytest.mark.parametrize(
    "state,action",
    [
        ({"_delivery_active": True}, "tiktok_audio"),
        ({"_delivery_outcome_unknown": True}, "tiktok_download"),
        ({}, "instagram_download_desc"),
    ],
)
def test_stale_or_concurrent_callback_cannot_start_another_send(
    monkeypatch, state, action
):
    session = {"platform": "tiktok", "session_id": "blocked-callback", **state}
    context = SimpleNamespace(user_data={"sessions": {"token1234": session}})
    handler = AsyncMock()
    monkeypatch.setattr(delivery, "_handle_main_callback", handler)
    query = SimpleNamespace(
        data=f"s|token1234|main|{action}",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=SimpleNamespace(chat=SimpleNamespace()),
    )
    asyncio.run(
        delivery.button_callback(
            SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=1)),
            context,
        )
    )
    handler.assert_not_awaited()
