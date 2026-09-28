"""Тесты доставки аудио из кэша file_id."""

import asyncio
import typing
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import telegram

from utils import telegram_utils
from utils.video_cache import CachedVideo, TelegramVideoCache


@pytest.mark.unit
def test_deliver_cached_audio_annotates_query():
    """Аннотация query должна совпадать с образцом send_single_file."""
    hints = typing.get_type_hints(telegram_utils._deliver_cached_audio)

    assert hints["query"] is telegram.CallbackQuery


def _query(reply_audio):
    return SimpleNamespace(message=SimpleNamespace(reply_audio=reply_audio))


def _video_query(reply_video):
    return SimpleNamespace(message=SimpleNamespace(reply_video=reply_video))


@pytest.mark.unit
def test_cached_video_is_delivered_by_file_id(monkeypatch):
    reply_video = AsyncMock()
    monkeypatch.setattr(
        telegram_utils.telegram_cache,
        "get",
        lambda url, format_id: SimpleNamespace(file_id="AGADvideo"),
    )

    delivered = asyncio.run(
        telegram_utils._deliver_cached_video(
            _video_query(reply_video), "https://example.test/v", "tg_video"
        )
    )

    assert delivered is True
    assert reply_video.await_args.kwargs["video"] == "AGADvideo"
    assert reply_video.await_args.kwargs["supports_streaming"] is True


@pytest.mark.unit
def test_cached_social_video_uses_the_selected_description():
    short_message = SimpleNamespace(message_id=10)
    long_message = SimpleNamespace(message_id=11)
    no_description_message = SimpleNamespace(message_id=12)
    reply_video = AsyncMock(
        side_effect=[short_message, long_message, no_description_message]
    )
    reply_text = AsyncMock()
    query = SimpleNamespace(
        message=SimpleNamespace(reply_video=reply_video, reply_text=reply_text)
    )
    long_description = "состав и шаги рецепта " * 300

    short = asyncio.run(
        telegram_utils._deliver_social_cached_video(
            query,
            "AGADcached",
            {
                "platform": "tiktok",
                "_include_description": True,
                "video_info": {"description": "Recipe"},
            },
        )
    )
    long = asyncio.run(
        telegram_utils._deliver_social_cached_video(
            query,
            "AGADcached",
            {
                "platform": "instagram",
                "_include_description": True,
                "video_info": {"description": long_description},
            },
        )
    )
    without_description = asyncio.run(
        telegram_utils._deliver_social_cached_video(
            query,
            "AGADcached",
            {
                "platform": "tiktok",
                "_include_description": False,
                "video_info": {"description": "Recipe"},
            },
        )
    )

    assert short is short_message
    assert long is long_message
    assert reply_video.await_args_list[0].kwargs["caption"] == "Recipe"
    assert reply_video.await_args_list[1].kwargs["caption"] is None
    assert reply_video.await_args_list[2].kwargs["caption"] is None
    assert "".join(call.args[0] for call in reply_text.await_args_list) == (
        long_description
    )
    assert len(reply_text.await_args_list) > 1
    assert all(
        call.kwargs["reply_to_message_id"] == long_message.message_id
        for call in reply_text.await_args_list
    )
    assert without_description is no_description_message


@pytest.mark.integration
def test_social_file_id_sqlite_roundtrip_keeps_caption_choice(monkeypatch, tmp_path):
    """Повтор из SQLite использует один file_id с обоими вариантами подписи."""
    cache = TelegramVideoCache(db_path=tmp_path / "social_cache.db")
    monkeypatch.setattr(telegram_utils, "telegram_cache", cache)
    url = "https://www.instagram.com/reel/example"
    cache.set(
        CachedVideo(
            url=url,
            file_id="AGADsocialcached",
            file_unique_id="social-unique",
            platform="instagram",
            format_id="direct_video",
            cached_at=datetime.now(),
            title="Recipe",
        )
    )

    loaded = cache.get(url, "direct_video")
    assert loaded is not None
    assert loaded.file_id == "AGADsocialcached"

    reply_video = AsyncMock(
        side_effect=[SimpleNamespace(message_id=1), SimpleNamespace(message_id=2)]
    )
    query = _video_query(reply_video)
    session = {
        "video_info": {"description": "Ingredients and steps"},
        "platform": "instagram",
    }

    asyncio.run(
        telegram_utils._deliver_social_cached_video(
            query,
            loaded.file_id,
            {**session, "_include_description": True},
        )
    )
    asyncio.run(
        telegram_utils._deliver_social_cached_video(
            query,
            loaded.file_id,
            {**session, "_include_description": False},
        )
    )

    assert [call.kwargs["video"] for call in reply_video.await_args_list] == [
        "AGADsocialcached",
        "AGADsocialcached",
    ]
    assert [call.kwargs["caption"] for call in reply_video.await_args_list] == [
        "Ingredients and steps",
        None,
    ]


@pytest.mark.unit
def test_missing_video_cache_entry_reports_not_delivered(monkeypatch):
    reply_video = AsyncMock()
    monkeypatch.setattr(
        telegram_utils.telegram_cache, "get", lambda url, format_id: None
    )

    delivered = asyncio.run(
        telegram_utils._deliver_cached_video(
            _video_query(reply_video), "https://example.test/v", "tg_video"
        )
    )

    assert delivered is False
    reply_video.assert_not_awaited()


@pytest.mark.unit
def test_stale_video_file_id_is_dropped_from_cache(monkeypatch):
    """Устаревший file_id видео должен удаляться, иначе он вернётся снова."""
    reply_video = AsyncMock(side_effect=telegram.error.BadRequest("wrong file_id"))
    deleted: list[str] = []
    monkeypatch.setattr(
        telegram_utils.telegram_cache,
        "get",
        lambda url, format_id: SimpleNamespace(file_id="AGADstalevideo"),
    )
    monkeypatch.setattr(
        telegram_utils.telegram_cache,
        "delete_by_file_id",
        lambda file_id: deleted.append(file_id),
    )

    delivered = asyncio.run(
        telegram_utils._deliver_cached_video(
            _video_query(reply_video), "https://example.test/v", "tg_video"
        )
    )

    assert delivered is False
    assert deleted == ["AGADstalevideo"]


@pytest.mark.unit
def test_cached_audio_is_delivered_by_file_id(monkeypatch):
    reply_audio = AsyncMock()
    monkeypatch.setattr(
        telegram_utils.telegram_cache,
        "get",
        lambda url, format_id: SimpleNamespace(file_id="AGADcached"),
    )

    delivered = asyncio.run(
        telegram_utils._deliver_cached_audio(
            _query(reply_audio), "https://example.test/v", "tiktok_audio"
        )
    )

    assert delivered is True
    assert reply_audio.await_args.kwargs["audio"] == "AGADcached"


@pytest.mark.unit
def test_missing_cache_entry_reports_not_delivered(monkeypatch):
    reply_audio = AsyncMock()
    monkeypatch.setattr(
        telegram_utils.telegram_cache, "get", lambda url, format_id: None
    )

    delivered = asyncio.run(
        telegram_utils._deliver_cached_audio(
            _query(reply_audio), "https://example.test/v", "tiktok_audio"
        )
    )

    assert delivered is False
    reply_audio.assert_not_awaited()


@pytest.mark.unit
def test_stale_file_id_is_dropped_from_cache(monkeypatch):
    """Устаревший file_id должен удаляться, чтобы не отдаваться повторно."""
    reply_audio = AsyncMock(side_effect=telegram.error.BadRequest("wrong file_id"))
    deleted: list[str] = []
    monkeypatch.setattr(
        telegram_utils.telegram_cache,
        "get",
        lambda url, format_id: SimpleNamespace(file_id="AGADstale"),
    )
    monkeypatch.setattr(
        telegram_utils.telegram_cache,
        "delete_by_file_id",
        lambda file_id: deleted.append(file_id),
    )

    delivered = asyncio.run(
        telegram_utils._deliver_cached_audio(
            _query(reply_audio), "https://example.test/v", "tiktok_audio"
        )
    )

    assert delivered is False
    assert deleted == ["AGADstale"]
