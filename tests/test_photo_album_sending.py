import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import telegram

from utils import telegram_utils, tiktok_instagram_utils
from utils.cancellation import forget_cancellation, request_cancellation
from utils.url_delivery import HandoffRefusals, PhotoPostHandoff, UrlHandoff
from telegram import InputMediaPhoto, InputMediaVideo


pytestmark = pytest.mark.unit


def _query(message, session_token="album-token"):
    return SimpleNamespace(
        message=message,
        from_user=SimpleNamespace(id=77),
        edit_message_text=AsyncMock(),
        answer=AsyncMock(),
        data=f"s|{session_token}|main|tiktok_download_desc",
    )


def test_twelve_photos_are_sent_as_two_ordered_albums_with_one_caption(
    monkeypatch, tmp_path
):
    paths = []
    for index in range(12):
        path = tmp_path / f"photo-{index:02}.jpg"
        path.write_bytes(b"photo")
        paths.append(path)

    monkeypatch.setattr(
        tiktok_instagram_utils, "resolve_photo_post_handoff", lambda *_args: None
    )
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "download_tiktok_photo_post_assets",
        lambda *_args: {
            "images": paths,
            "items": [{"kind": "photo", "path": path} for path in paths],
            "audio": None,
        },
    )
    recorded = AsyncMock()
    cleaned = AsyncMock()
    monkeypatch.setattr(telegram_utils, "_record_delivery", recorded)
    monkeypatch.setattr(telegram_utils, "_cleanup_user_session", cleaned)

    async def reply_media_group(media, do_quote=None):
        assert do_quote is False
        return [SimpleNamespace(message_id=100 + index) for index, _ in enumerate(media)]

    reply_media_group_mock = AsyncMock(side_effect=reply_media_group)
    message = SimpleNamespace(
        reply_media_group=reply_media_group_mock,
        reply_photo=AsyncMock(),
        reply_audio=AsyncMock(),
        reply_text=AsyncMock(),
    )
    query = _query(message)
    session = {
        "url": "https://www.tiktok.com/@cook/photo/123",
        "session_id": "album-session",
        "platform": "tiktok",
        "_include_description": True,
        "video_info": {
            "_nuvio_tiktok_photo_post": True,
            "_nuvio_tiktok_images": [f"https://cdn.example/{i}.jpg" for i in range(12)],
            "description": "Рецепт без потери содержания",
        },
    }

    asyncio.run(
        telegram_utils._send_photo_post_assets(
            query, "album-token", session, SimpleNamespace(user_data={})
        )
    )

    calls = reply_media_group_mock.await_args_list
    assert [len(call.kwargs["media"]) for call in calls] == [10, 2]
    sent_paths = [
        item.media.filename
        for call in calls
        for item in call.kwargs["media"]
    ]
    assert sent_paths == [path.name for path in paths]
    assert calls[0].kwargs["media"][0].caption == "Рецепт без потери содержания"
    assert all(item.caption is None for item in calls[0].kwargs["media"][1:])
    assert all(item.caption is None for item in calls[1].kwargs["media"])
    message.reply_photo.assert_not_awaited()
    message.reply_text.assert_not_awaited()
    recorded.assert_awaited_once()
    cleaned.assert_awaited_once()


def test_long_description_is_sent_as_plain_reply_after_album(monkeypatch, tmp_path):
    paths = []
    for index in range(2):
        path = tmp_path / f"photo-{index}.jpg"
        path.write_bytes(b"photo")
        paths.append(path)
    description = "ингредиенты и шаги " * 90

    monkeypatch.setattr(
        tiktok_instagram_utils, "resolve_photo_post_handoff", lambda *_args: None
    )
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "download_tiktok_photo_post_assets",
        lambda *_args: {
            "images": paths,
            "items": [{"kind": "photo", "path": path} for path in paths],
            "audio": None,
        },
    )
    monkeypatch.setattr(telegram_utils, "_record_delivery", AsyncMock())
    monkeypatch.setattr(telegram_utils, "_cleanup_user_session", AsyncMock())

    async def reply_media_group(media, do_quote=None):
        assert do_quote is False
        return [SimpleNamespace(message_id=51), SimpleNamespace(message_id=52)]

    message = SimpleNamespace(
        reply_media_group=AsyncMock(side_effect=reply_media_group),
        reply_photo=AsyncMock(),
        reply_audio=AsyncMock(),
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=53)),
    )
    session = {
        "url": "https://www.tiktok.com/@cook/photo/123",
        "session_id": "long-caption-session",
        "platform": "tiktok",
        "_include_description": True,
        "video_info": {
            "_nuvio_tiktok_photo_post": True,
            "_nuvio_tiktok_images": ["one", "two"],
            "description": description,
        },
    }

    asyncio.run(
        telegram_utils._send_photo_post_assets(
            _query(message), "album-token", session, SimpleNamespace(user_data={})
        )
    )

    media = message.reply_media_group.await_args.kwargs["media"]
    assert all(item.caption is None for item in media)
    text_call = message.reply_text.await_args
    assert text_call.kwargs["parse_mode"] is None
    assert text_call.kwargs["reply_to_message_id"] == 51
    assert "".join(call.args[0] for call in message.reply_text.await_args_list) == description


def test_missing_reply_target_retries_only_text_without_media_reference():
    description = "x" * 4100
    chunks = telegram_utils._description_chunks_for_delivery(
        {"_include_description": True, "platform": "instagram", "video_info": {"description": description}}
    )
    reply_text = AsyncMock(
        side_effect=[
            telegram.error.BadRequest("Message to reply not found"),
            SimpleNamespace(message_id=80),
            SimpleNamespace(message_id=81),
        ]
    )
    query = SimpleNamespace(message=SimpleNamespace(reply_text=reply_text))
    session = {"session_id": "deleted-first-media"}

    delivered = asyncio.run(
        telegram_utils._send_description_chunks(
            query, chunks, SimpleNamespace(message_id=70), session
        )
    )

    assert delivered is True
    assert reply_text.await_count == 3
    assert reply_text.await_args_list[0].kwargs["reply_to_message_id"] == 70
    assert "reply_to_message_id" not in reply_text.await_args_list[1].kwargs
    assert reply_text.await_args_list[2].kwargs["reply_to_message_id"] is None
    assert "".join(
        call.args[0] for call in reply_text.await_args_list[1:]
    ) == description
    assert "_description_delivery_failed" not in session


def test_photo_format_refusal_keeps_order_and_falls_back_to_document(
    monkeypatch, tmp_path
):
    paths = []
    for index in range(2):
        path = tmp_path / f"unsupported-{index}.jpg"
        path.write_bytes(b"image")
        paths.append(path)
    monkeypatch.setattr(telegram_utils, "TELEGRAM_LOCAL_MODE", False)
    actual_open = Path.open
    opened_handles = []

    def tracked_open(path, *args, **kwargs):
        handle = actual_open(path, *args, **kwargs)
        opened_handles.append(handle)
        return handle

    monkeypatch.setattr(Path, "open", tracked_open)
    events = []

    async def reject_album(media, do_quote=None):
        assert do_quote is False
        events.append(("album", media))
        raise telegram.error.BadRequest("PHOTO_INVALID_DIMENSIONS")

    async def send_photo(photo, caption=None, **_kwargs):
        events.append(("photo", photo, caption))
        if len([event for event in events if event[0] == "photo"]) == 2:
            raise telegram.error.BadRequest("PHOTO_INVALID_DIMENSIONS")
        return SimpleNamespace(message_id=10)

    async def send_document(document, caption=None, **_kwargs):
        events.append(("document", document, caption))
        return SimpleNamespace(message_id=11)

    query = SimpleNamespace(
        message=SimpleNamespace(
            reply_media_group=AsyncMock(side_effect=reject_album),
            reply_photo=AsyncMock(side_effect=send_photo),
            reply_document=AsyncMock(side_effect=send_document),
        )
    )
    session = {"session_id": "photo-document-fallback"}

    sent = asyncio.run(
        telegram_utils._send_photo_file_group(
            query, paths, "Caption", session
        )
    )

    assert [event[0] for event in events] == ["album", "photo", "photo", "document"]
    assert [Path(event[1].name).name for event in events[1:]] == [
        paths[0].name,
        paths[1].name,
        paths[1].name,
    ]
    assert events[1][2] == "Caption"
    assert events[2][2] is None
    assert events[3][2] is None
    assert [message.message_id for message in sent] == [10, 11]
    assert session["_delivery_progress"] == 2
    assert opened_handles
    assert all(handle.closed for handle in opened_handles)


def test_active_url_delivery_defers_cleanup_until_delivery_finishes(monkeypatch):
    cleanup = Mock()
    # _cleanup_session_when_idle удаляет каталог синхронно.
    monkeypatch.setattr(telegram_utils, "cleanup_temp_files", cleanup)
    session_id = "url-only-active-session"

    telegram_utils._delivery_started(session_id)
    assert session_id in telegram_utils.active_download_sessions()
    telegram_utils._cleanup_session_when_idle(session_id)
    cleanup.assert_not_called()

    telegram_utils._delivery_finished(session_id)
    cleanup.assert_called_once_with(session_id)


def test_cancellation_stops_before_the_next_photo_album():
    urls = tuple(
        UrlHandoff(
            url=f"https://p16-sign.tiktokcdn-us.com/obj/image-{index}.jpeg",
            kind="photo",
            size=300 * 1024,
        )
        for index in range(11)
    )
    plan = PhotoPostHandoff(images=urls, audio=None)
    session_id = "cancel-between-albums"

    async def send_first_group(media, do_quote=None):
        assert do_quote is False
        request_cancellation(session_id)
        return [SimpleNamespace(message_id=100 + index) for index, _ in enumerate(media)]

    reply_media_group = AsyncMock(side_effect=send_first_group)
    query = _query(
        SimpleNamespace(reply_media_group=reply_media_group), session_token="cancel"
    )
    session = {"session_id": session_id}

    try:
        with pytest.raises(telegram_utils.CancelledByUser):
            asyncio.run(
                telegram_utils._deliver_photo_post_by_url(query, plan, session)
            )
        assert reply_media_group.await_count == 1
        assert session["_delivery_progress"] == 9
    finally:
        forget_cancellation(session_id)


def test_cancellation_before_first_photo_album_sends_nothing():
    plan = PhotoPostHandoff(
        images=(UrlHandoff("https://cdn.example/one.jpg", "photo", 100),),
        audio=None,
    )
    session_id = "cancel-before-first-album"
    reply_photo = AsyncMock()
    query = _query(SimpleNamespace(reply_photo=reply_photo), session_token="cancel")
    request_cancellation(session_id)

    try:
        with pytest.raises(telegram_utils.CancelledByUser):
            asyncio.run(
                telegram_utils._deliver_photo_post_by_url(
                    query, plan, {"session_id": session_id}
                )
            )
        reply_photo.assert_not_awaited()
    finally:
        forget_cancellation(session_id)


def test_file_fallback_sends_only_items_after_confirmed_url_album(
    monkeypatch, tmp_path
):
    paths = []
    for index in range(11):
        path = tmp_path / f"frame-{index:02}.jpg"
        path.write_bytes(b"image")
        paths.append(path)
    plan = PhotoPostHandoff(
        images=tuple(
            UrlHandoff(
                url=f"https://p16-sign.tiktokcdn-us.com/obj/{index}.jpg",
                kind="photo",
                size=300 * 1024,
            )
            for index in range(11)
        ),
        audio=None,
    )
    confirmed_messages = tuple(
        SimpleNamespace(message_id=index, media_group_id=100) for index in range(9)
    )
    outcome = telegram_utils.DeliveryOutcome(
        "refused", confirmed_messages, confirmed_items=9
    )

    async def fake_run_blocking(function, *args, **_kwargs):
        return function(*args)

    async def fake_url_delivery(_query, _plan, _session):
        return outcome

    sent_groups = []

    async def fake_file_group(_query, image_paths, _caption, _session):
        sent_groups.append(image_paths)
        return [SimpleNamespace(message_id=200)]

    monkeypatch.setattr(telegram_utils, "run_blocking", fake_run_blocking)
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "resolve_photo_post_handoff",
        lambda *_args: plan,
    )
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "download_tiktok_photo_post_assets",
        lambda *_args: {
            "images": paths,
            "items": [{"kind": "photo", "path": path} for path in paths],
            "audio": None,
        },
    )
    monkeypatch.setattr(
        telegram_utils, "_deliver_photo_post_by_url", fake_url_delivery
    )
    monkeypatch.setattr(
        telegram_utils, "_send_photo_file_group", fake_file_group
    )
    monkeypatch.setattr(telegram_utils, "_record_delivery", AsyncMock())
    monkeypatch.setattr(telegram_utils, "_cleanup_user_session", AsyncMock())
    query = _query(SimpleNamespace(), session_token="partial-url")
    session = {
        "url": "https://www.tiktok.com/@cook/photo/123",
        "session_id": "partial-url-session",
        "platform": "tiktok",
        "video_info": {
            "_nuvio_tiktok_photo_post": True,
            "_nuvio_tiktok_images": [item.url for item in plan.images],
        },
    }

    asyncio.run(
        telegram_utils._send_photo_post_assets(
            query, "partial-url", session, SimpleNamespace(user_data={})
        )
    )

    assert sent_groups == [[paths[9], paths[10]]]
    assert session["_delivered_items"] == 11


def test_audio_failure_after_album_does_not_resend_photos(monkeypatch, tmp_path):
    photos = []
    for index in range(2):
        path = tmp_path / f"audio-failure-{index}.jpg"
        path.write_bytes(b"image")
        photos.append(path)
    audio_path = tmp_path / "audio.m4a"
    audio_path.write_bytes(b"audio")
    monkeypatch.setattr(telegram_utils, "TELEGRAM_LOCAL_MODE", False)

    async def fake_run_blocking(function, *args, **_kwargs):
        return function(*args)

    monkeypatch.setattr(telegram_utils, "run_blocking", fake_run_blocking)
    monkeypatch.setattr(
        tiktok_instagram_utils, "resolve_photo_post_handoff", lambda *_args: None
    )
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "download_tiktok_photo_post_assets",
        lambda *_args: {
            "images": photos,
            "items": [{"kind": "photo", "path": path} for path in photos],
            "audio": audio_path,
        },
    )
    sent_groups = []

    async def fake_file_group(_query, image_paths, _caption, _session):
        sent_groups.append(image_paths)
        return [SimpleNamespace(message_id=90), SimpleNamespace(message_id=91)]

    monkeypatch.setattr(
        telegram_utils, "_send_photo_file_group", fake_file_group
    )
    recorded = AsyncMock()
    monkeypatch.setattr(telegram_utils, "_record_delivery", recorded)
    cleanup = Mock()
    monkeypatch.setattr(telegram_utils, "_cleanup_session_when_idle", cleanup)

    async def fail_description(_query, _chunks, _message, delivery_state):
        delivery_state["_description_attempted"] = True
        delivery_state["_description_delivery_failed"] = True
        return False

    monkeypatch.setattr(
        telegram_utils, "_send_description_chunks", fail_description
    )
    message = SimpleNamespace(
        reply_audio=AsyncMock(
            side_effect=telegram.error.BadRequest("failed to send audio")
        )
    )
    query = _query(message, session_token="audio-failure")
    session = {
        "url": "https://www.tiktok.com/@cook/photo/123",
        "session_id": "audio-failure-session",
        "platform": "tiktok",
        "_include_description": True,
        "video_info": {
            "_nuvio_tiktok_photo_post": True,
            "_nuvio_tiktok_images": ["one", "two"],
            "_nuvio_tiktok_audio_url": "https://cdn.example/audio.m4a",
            "description": "step " * 900,
        },
    }

    asyncio.run(
        telegram_utils._send_photo_post_assets(
            query, "audio-failure", session, SimpleNamespace(user_data={})
        )
    )

    assert sent_groups == [photos]
    message.reply_audio.assert_awaited_once()
    assert message.reply_audio.await_args.kwargs["audio"].closed is True
    assert telegram_utils.PHOTO_POST_PARTIAL in query.edit_message_text.await_args.args[0]
    cleanup.assert_called_once_with("audio-failure-session")
    recorded.assert_not_awaited()


def test_two_description_buttons_start_only_one_delivery(monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def fake_handle(*_args):
        calls.append(1)
        entered.set()
        await release.wait()

    @asynccontextmanager
    async def no_chat_action(*_args):
        yield

    monkeypatch.setattr(telegram_utils, "_handle_main_callback", fake_handle)
    monkeypatch.setattr(telegram_utils, "_pulsing_chat_action", no_chat_action)
    session = {"session_id": "double-click-session", "platform": "tiktok"}
    context = SimpleNamespace(user_data={"sessions": {"token1234": session}})
    user = SimpleNamespace(id=78)

    async def run():
        first_message = SimpleNamespace(chat=SimpleNamespace())
        first = SimpleNamespace(
            callback_query=SimpleNamespace(
                data="s|token1234|main|tiktok_download_desc",
                message=first_message,
                answer=AsyncMock(),
            ),
            effective_user=user,
        )
        first_task = asyncio.create_task(telegram_utils.button_callback(first, context))
        await entered.wait()

        second = SimpleNamespace(
            callback_query=SimpleNamespace(
                data="s|token1234|main|tiktok_download",
                message=SimpleNamespace(chat=SimpleNamespace()),
                answer=AsyncMock(),
            ),
            effective_user=user,
        )
        await telegram_utils.button_callback(second, context)
        release.set()
        await first_task

    asyncio.run(run())
    assert calls == [1]
    assert "_delivery_active" not in session
    assert "_include_description" not in session


def test_mixed_instagram_group_keeps_photo_video_order_and_caption(monkeypatch, tmp_path):
    paths = []
    for filename in ("first.jpg", "clip.mp4", "last.jpg"):
        path = tmp_path / filename
        path.write_bytes(b"media")
        paths.append(path)
    monkeypatch.setattr(
        telegram_utils,
        "run_blocking",
        AsyncMock(return_value={"width": 640, "height": 360, "duration": 8}),
    )

    async def reply_media_group(media, do_quote=None):
        assert do_quote is False
        return [SimpleNamespace(message_id=index + 10) for index in range(len(media))]

    reply_media_group_mock = AsyncMock(side_effect=reply_media_group)
    message = SimpleNamespace(reply_media_group=reply_media_group_mock)
    session = {"session_id": "mixed-media-session"}
    items = [
        {"kind": "photo", "path": paths[0]},
        {"kind": "video", "path": paths[1]},
        {"kind": "photo", "path": paths[2]},
    ]

    sent = asyncio.run(
        telegram_utils._send_mixed_media_group(
            SimpleNamespace(message=message), items, "Recipe", session
        )
    )

    media = reply_media_group_mock.await_args.kwargs["media"]
    assert [type(item) for item in media] == [
        InputMediaPhoto,
        InputMediaVideo,
        InputMediaPhoto,
    ]
    assert [item.media.filename for item in media] == [path.name for path in paths]
    assert media[0].caption == "Recipe"
    assert all(item.caption is None for item in media[1:])
    assert media[1].width == 640
    assert media[1].height == 360
    assert media[1].to_dict()["duration"] == 8
    assert len(sent) == 3


def test_mixed_instagram_carousel_keeps_order_across_album_boundary(
    monkeypatch, tmp_path
):
    items = []
    for index in range(12):
        kind = "video" if index % 3 == 1 else "photo"
        suffix = ".mp4" if kind == "video" else ".jpg"
        path = tmp_path / f"mixed-{index:02}{suffix}"
        path.write_bytes(b"media")
        items.append({"kind": kind, "path": path})

    async def fake_run_blocking(_function, *_args, **_kwargs):
        return {"items": items, "images": [], "audio": None}

    monkeypatch.setattr(telegram_utils, "run_blocking", fake_run_blocking)
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "download_instagram_photo_post_assets",
        lambda *_args: {"items": items, "images": [], "audio": None},
    )
    groups = []

    async def fake_mixed_group(_query, group, caption, _session):
        groups.append((group, caption))
        return [SimpleNamespace(message_id=index) for index in range(len(group))]

    monkeypatch.setattr(
        telegram_utils, "_send_mixed_media_group", fake_mixed_group
    )
    monkeypatch.setattr(telegram_utils, "_record_delivery", AsyncMock())
    monkeypatch.setattr(telegram_utils, "_cleanup_user_session", AsyncMock())
    query = _query(SimpleNamespace(), session_token="mixed-boundary")
    session = {
        "url": "https://www.instagram.com/p/AbCdEf123/",
        "session_id": "mixed-boundary-session",
        "platform": "instagram",
        "_include_description": True,
        "video_info": {
            "_nuvio_instagram_photo_post": True,
            "_nuvio_instagram_mixed_post": True,
            "_nuvio_instagram_carousel_complete": True,
            "_nuvio_instagram_images": [],
            "_nuvio_instagram_carousel_items": items,
            "description": "Recipe",
        },
    }

    asyncio.run(
        telegram_utils._send_photo_post_assets(
            query, "mixed-boundary", session, SimpleNamespace(user_data={})
        )
    )

    assert [len(group) for group, _caption in groups] == [10, 2]
    assert [item for group, _caption in groups for item in group] == items
    assert [caption for _group, caption in groups] == ["Recipe", None]


def test_video_url_uses_caption_or_plain_long_description(monkeypatch):
    monkeypatch.setattr(telegram_utils, "_HANDOFF_REFUSALS", HandoffRefusals())
    media_message = SimpleNamespace(message_id=71)
    reply_video = AsyncMock(return_value=media_message)
    reply_text = AsyncMock()
    message = SimpleNamespace(reply_video=reply_video, reply_text=reply_text)
    query = SimpleNamespace(message=message)
    plan = UrlHandoff(
        url="https://v16m.tiktokcdn-us.com/video.mp4",
        kind="video",
        size=100,
    )
    short_state = {
        "platform": "tiktok",
        "_include_description": True,
        "video_info": {"description": "Recipe"},
    }

    short_outcome = asyncio.run(
        telegram_utils._deliver_by_url(
            query, plan, "https://www.tiktok.com/@cook/video/1", "tiktok",
            session_data=short_state,
        )
    )
    assert short_outcome.state == "delivered"
    assert short_outcome.confirmed_items == 1
    assert len(short_outcome.messages) == 1
    assert reply_video.await_args.kwargs["caption"] == "Recipe"
    assert reply_video.await_args.kwargs["parse_mode"] is None
    reply_video.reset_mock()

    long_description = "step " * 900
    long_state = {
        "platform": "instagram",
        "_include_description": True,
        "video_info": {"description": long_description},
    }
    long_outcome = asyncio.run(
        telegram_utils._deliver_by_url(
            query, plan, "https://www.instagram.com/reel/abc/", "instagram",
            session_data=long_state,
        )
    )
    assert long_outcome.state == "delivered"
    assert reply_video.await_args.kwargs["caption"] is None
    assert "".join(call.args[0] for call in reply_text.await_args_list) == long_description
    assert all(call.kwargs["parse_mode"] is None for call in reply_text.await_args_list)
    assert all(call.kwargs["reply_to_message_id"] == 71 for call in reply_text.await_args_list)


def test_two_concurrent_links_keep_their_own_descriptions(monkeypatch):
    monkeypatch.setattr(telegram_utils, "_HANDOFF_REFUSALS", HandoffRefusals())
    messages = [SimpleNamespace(message_id=101), SimpleNamespace(message_id=202)]
    queries = [
        SimpleNamespace(message=SimpleNamespace(reply_video=AsyncMock(return_value=msg)))
        for msg in messages
    ]
    sessions = [
        {
            "platform": "tiktok",
            "_include_description": True,
            "video_info": {"description": "Рецепт A"},
            "session_id": "parallel-a",
        },
        {
            "platform": "instagram",
            "_include_description": True,
            "video_info": {"description": "Рецепт B"},
            "session_id": "parallel-b",
        },
    ]
    plans = [
        UrlHandoff("https://cdn.example/a.mp4", "video", 100),
        UrlHandoff("https://cdn.example/b.mp4", "video", 100),
    ]

    async def deliver_both():
        return await asyncio.gather(
            *(
                telegram_utils._deliver_by_url(
                    query,
                    plan,
                    f"https://{session['platform']}.example/post",
                    session["platform"],
                    session_data=session,
                )
                for query, plan, session in zip(queries, plans, sessions)
            )
        )

    outcomes = asyncio.run(deliver_both())

    assert [outcome.state for outcome in outcomes] == ["delivered", "delivered"]
    assert [query.message.reply_video.await_args.kwargs["caption"] for query in queries] == [
        "Рецепт A",
        "Рецепт B",
    ]


def test_unknown_video_url_outcome_is_not_retried_or_downloaded(monkeypatch):
    monkeypatch.setattr(telegram_utils, "_HANDOFF_REFUSALS", HandoffRefusals())
    reply_video = AsyncMock(side_effect=telegram.error.NetworkError("timeout"))
    query = SimpleNamespace(message=SimpleNamespace(reply_video=reply_video))
    plan = UrlHandoff(
        url="https://v16m.tiktokcdn-us.com/video.mp4",
        kind="video",
        size=100,
    )
    session = {"platform": "tiktok", "session_id": "uncertain-url"}

    outcome = asyncio.run(
        telegram_utils._deliver_by_url(
            query, plan, "https://www.tiktok.com/@cook/video/1", "tiktok",
            session_data=session,
        )
    )

    assert outcome.state == "unknown"
    assert outcome.confirmed_items == 0
    assert outcome.messages == ()
    assert reply_video.await_count == 1
    assert session["_delivery_outcome_unknown"] is True


def test_bad_request_for_uploaded_video_is_not_marked_as_unknown(
    monkeypatch, tmp_path
):
    file_path = tmp_path / "video.mp4"
    file_path.write_bytes(b"video")
    monkeypatch.setattr(telegram_utils, "TELEGRAM_LOCAL_MODE", False)
    monkeypatch.setattr(telegram_utils, "run_blocking", AsyncMock(return_value=None))
    reply_video = AsyncMock(
        side_effect=telegram.error.BadRequest("failed to send video")
    )
    query = SimpleNamespace(
        message=SimpleNamespace(reply_video=reply_video),
        edit_message_text=AsyncMock(),
    )
    session = {
        "platform": "tiktok",
        "url": "https://www.tiktok.com/@cook/video/1",
        "session_id": "bad-request-video",
    }

    sent = asyncio.run(
        telegram_utils.send_single_file(
            query, file_path, "token1234", session
        )
    )

    assert sent is False
    assert reply_video.await_count == 1
    assert "_delivery_outcome_unknown" not in session


def test_url_delivery_reports_when_only_description_failed(monkeypatch):
    monkeypatch.setattr(
        telegram_utils,
        "_deliver_by_url",
        AsyncMock(
            return_value=telegram_utils.DeliveryOutcome(
                "delivered", confirmed_items=1
            )
        ),
    )
    monkeypatch.setattr(telegram_utils, "_record_delivery", AsyncMock())
    monkeypatch.setattr(telegram_utils, "_cleanup_user_session", AsyncMock())
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=7), edit_message_text=AsyncMock()
    )
    session = {
        "url": "https://www.tiktok.com/@cook/video/1",
        "platform": "tiktok",
        "_description_delivery_failed": True,
    }
    plan = UrlHandoff("https://cdn.example/video.mp4", "video", 100)

    delivered = asyncio.run(
        telegram_utils._deliver_plan(
            query, SimpleNamespace(), "token1234", session, plan
        )
    )

    assert delivered is True
    assert query.edit_message_text.await_args.args[0] == (
        telegram_utils.DESCRIPTION_SEND_FAILED
    )


def test_deleted_status_message_does_not_turn_delivered_media_into_failure(
    monkeypatch,
):
    monkeypatch.setattr(
        telegram_utils,
        "_deliver_by_url",
        AsyncMock(
            return_value=telegram_utils.DeliveryOutcome(
                "delivered", confirmed_items=1
            )
        ),
    )
    recorded = AsyncMock()
    cleaned = AsyncMock()
    monkeypatch.setattr(telegram_utils, "_record_delivery", recorded)
    monkeypatch.setattr(telegram_utils, "_cleanup_user_session", cleaned)
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=7),
        edit_message_text=AsyncMock(
            side_effect=telegram.error.BadRequest("Message to edit not found")
        ),
    )
    session = {
        "url": "https://www.instagram.com/reel/abc/",
        "platform": "instagram",
        "session_id": "deleted-status-message",
    }

    handled = asyncio.run(
        telegram_utils._deliver_plan(
            query,
            SimpleNamespace(),
            "token1234",
            session,
            UrlHandoff("https://cdn.example/video.mp4", "video", 100),
        )
    )

    assert handled is True
    recorded.assert_awaited_once()
    cleaned.assert_awaited_once()
    assert query.edit_message_text.await_count == 1


def test_retry_after_retries_only_explicitly_rejected_request(monkeypatch):
    monkeypatch.setenv("PTB_TIMEDELTA", "true")
    call = AsyncMock(
        side_effect=[telegram.error.RetryAfter(timedelta()), "accepted"]
    )

    result = asyncio.run(telegram_utils._call_telegram_with_retry_after(call))

    assert result == "accepted"
    assert call.await_count == 2


def test_retry_after_delay_can_be_cancelled_before_retry(monkeypatch):
    monkeypatch.setenv("PTB_TIMEDELTA", "true")
    session_id = "cancel-retry-after"
    session = {"session_id": session_id}

    async def request_then_rate_limit():
        request_cancellation(session_id)
        raise telegram.error.RetryAfter(timedelta(seconds=5))

    try:
        with pytest.raises(telegram_utils.CancelledByUser):
            asyncio.run(
                telegram_utils._call_telegram_with_retry_after(
                    request_then_rate_limit, session
                )
            )
    finally:
        forget_cancellation(session_id)


def _mixed_paths(tmp_path, names):
    paths = []
    for name in names:
        path = tmp_path / name
        path.write_bytes(b"media")
        paths.append(path)
    return paths


def test_mixed_group_photo_refusal_sends_items_one_by_one(monkeypatch, tmp_path):
    paths = _mixed_paths(tmp_path, ("a.jpg", "clip.mp4", "b.jpg"))
    monkeypatch.setattr(telegram_utils, "TELEGRAM_LOCAL_MODE", False)
    geometry = AsyncMock(return_value={"width": 640, "height": 360, "duration": 8})
    monkeypatch.setattr(telegram_utils, "run_blocking", geometry)
    events = []

    async def reply_photo(photo, caption=None, **_kwargs):
        events.append(("photo", Path(photo.name).name, caption))
        if Path(photo.name).name == "b.jpg":
            raise telegram.error.BadRequest("IMAGE_PROCESS_FAILED")
        return SimpleNamespace(message_id=len(events))

    async def reply_video(video, caption=None, **kwargs):
        events.append(("video", Path(video.name).name, caption, kwargs["width"], kwargs["duration"]))
        return SimpleNamespace(message_id=len(events))

    async def reply_document(document, caption=None, **_kwargs):
        events.append(("document", Path(document.name).name, caption))
        return SimpleNamespace(message_id=len(events))

    message = SimpleNamespace(
        reply_media_group=AsyncMock(
            side_effect=telegram.error.BadRequest("PHOTO_INVALID_DIMENSIONS")
        ),
        reply_photo=AsyncMock(side_effect=reply_photo),
        reply_video=AsyncMock(side_effect=reply_video),
        reply_document=AsyncMock(side_effect=reply_document),
    )
    session = {"session_id": "mixed-fallback", "_delivered_items": 10}
    items = [
        {"kind": "photo", "path": paths[0]},
        {"kind": "video", "path": paths[1]},
        {"kind": "photo", "path": paths[2]},
    ]

    sent = asyncio.run(
        telegram_utils._send_mixed_media_group(
            SimpleNamespace(message=message), items, "Recipe", session
        )
    )

    assert events == [
        ("photo", "a.jpg", "Recipe"),
        ("video", "clip.mp4", None, 640, 8),
        ("photo", "b.jpg", None),
        ("document", "b.jpg", None),
    ]
    assert len(sent) == 3
    assert session["_delivered_items"] == 13
    assert session["_delivery_progress"] == 13
    assert geometry.await_count == 1


def test_single_mixed_video_keeps_measured_geometry(monkeypatch, tmp_path):
    (path,) = _mixed_paths(tmp_path, ("clip.mp4",))
    monkeypatch.setattr(
        telegram_utils,
        "run_blocking",
        AsyncMock(return_value={"width": 720, "height": 1280, "duration": 0}),
    )
    reply_video = AsyncMock(return_value=SimpleNamespace(message_id=1))

    asyncio.run(
        telegram_utils._send_mixed_media_group(
            SimpleNamespace(message=SimpleNamespace(reply_video=reply_video)),
            [{"kind": "video", "path": path}],
            None,
            {"session_id": "single-mixed-video"},
        )
    )

    kwargs = reply_video.await_args.kwargs
    assert (kwargs["width"], kwargs["height"]) == (720, 1280)
    assert "duration" not in kwargs
    assert kwargs["supports_streaming"] is True


def test_single_photo_format_refusal_goes_straight_to_document(monkeypatch, tmp_path):
    (path,) = _mixed_paths(tmp_path, ("only.jpg",))
    monkeypatch.setattr(telegram_utils, "TELEGRAM_LOCAL_MODE", False)
    reply_photo = AsyncMock(side_effect=telegram.error.BadRequest("PHOTO_INVALID_DIMENSIONS"))
    reply_document = AsyncMock(return_value=SimpleNamespace(message_id=9))
    session = {"session_id": "single-photo-document"}

    sent = asyncio.run(
        telegram_utils._send_photo_file_group(
            SimpleNamespace(
                message=SimpleNamespace(reply_photo=reply_photo, reply_document=reply_document)
            ),
            [path],
            "Caption",
            session,
        )
    )

    assert [message.message_id for message in sent] == [9]
    reply_photo.assert_awaited_once()
    assert reply_document.await_args.kwargs["caption"] == "Caption"
    assert session["_first_media_message"].message_id == 9
    assert session["_delivered_items"] == 1


@pytest.mark.parametrize(
    "error,progress,expected_text,unknown",
    [
        (telegram.error.BadRequest("chat not found"), 2, telegram_utils.PHOTO_POST_PARTIAL, False),
        (telegram.error.Forbidden("bot was blocked"), 0, "TG-", False),
        (telegram.error.TimedOut(), 2, telegram_utils.DELIVERY_OUTCOME_UNKNOWN, True),
        (telegram.error.NetworkError("reset"), 0, telegram_utils.DELIVERY_OUTCOME_UNKNOWN, True),
    ],
)
def test_photo_post_telegram_errors_keep_their_public_outcome(
    monkeypatch, tmp_path, error, progress, expected_text, unknown
):
    (path,) = _mixed_paths(tmp_path, ("frame.jpg",))
    monkeypatch.setattr(
        tiktok_instagram_utils, "resolve_photo_post_handoff", lambda *_args: None
    )
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "download_tiktok_photo_post_assets",
        lambda *_args: {"items": [{"kind": "photo", "path": path}], "audio": None},
    )

    async def fail_group(*_args):
        raise error

    monkeypatch.setattr(telegram_utils, "_send_photo_file_group", fail_group)
    monkeypatch.setattr(telegram_utils, "_schedule_platform_failure_log", lambda **_kw: None)
    cleanup_session = AsyncMock()
    cleanup_idle = Mock()
    monkeypatch.setattr(telegram_utils, "_cleanup_user_session", cleanup_session)
    monkeypatch.setattr(telegram_utils, "_cleanup_session_when_idle", cleanup_idle)
    query = _query(SimpleNamespace(), session_token="errors")
    session = {
        "url": "https://www.tiktok.com/@cook/photo/123",
        "session_id": "photo-errors",
        "platform": "tiktok",
        "_delivery_progress": progress,
        "video_info": {"_nuvio_tiktok_photo_post": True},
    }

    asyncio.run(
        telegram_utils._send_photo_post_assets(
            query, "errors", session, SimpleNamespace(user_data={})
        )
    )

    assert expected_text in query.edit_message_text.await_args.args[0]
    assert bool(session.get("_delivery_outcome_unknown")) is unknown
    if unknown:
        cleanup_session.assert_awaited_once()
        cleanup_idle.assert_not_called()
    else:
        cleanup_session.assert_not_awaited()
        cleanup_idle.assert_called_once_with("photo-errors")
