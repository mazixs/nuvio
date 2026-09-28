"""Тесты доставки медиа прямой ссылкой вместо файла.

Telegram скачивает ссылку сам, поэтому от бота требуется только передать её и
корректно отступить, если Telegram ссылку не принял.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import telegram

from utils import telegram_utils
from utils.url_delivery import UrlHandoff


MB = 1024 * 1024
PAGE_URL = "https://www.tiktok.com/@user/video/7643812958950362389"
MEDIA_URL = "https://v16m.tiktokcdn-us.com/abc/video.mp4"

pytestmark = pytest.mark.unit


def _query(**senders):
    return SimpleNamespace(message=SimpleNamespace(**senders))


def _sent(kind: str, file_id: str = "AGADnew"):
    """Ответ Telegram на успешную отправку."""
    media = SimpleNamespace(
        file_id=file_id, file_unique_id="unique", file_size=2 * MB, duration=11
    )
    fields = {"video": None, "audio": None, "document": None, "photo": None}
    fields[kind] = media
    return SimpleNamespace(**fields)


def _deliver(query, plan, **kwargs):
    return asyncio.run(
        telegram_utils._deliver_by_url(
            query, plan, PAGE_URL, "tiktok", **kwargs
        )
    )


def test_video_is_handed_over_as_a_link():
    reply_video = AsyncMock(return_value=_sent("video"))
    plan = UrlHandoff(url=MEDIA_URL, kind="video", size=2 * MB)

    delivered = _deliver(_query(reply_video=reply_video), plan)

    assert delivered.state == "delivered"
    assert reply_video.await_args.kwargs["video"] == MEDIA_URL
    assert reply_video.await_args.kwargs["supports_streaming"] is True


def test_audio_is_handed_over_as_a_link():
    reply_audio = AsyncMock(return_value=_sent("audio"))
    plan = UrlHandoff(url=MEDIA_URL, kind="audio", size=MB)

    delivered = _deliver(_query(reply_audio=reply_audio), plan)

    assert delivered.state == "delivered"
    assert reply_audio.await_args.kwargs["audio"] == MEDIA_URL


def test_photo_is_handed_over_as_a_link():
    reply_photo = AsyncMock(return_value=_sent("photo"))
    plan = UrlHandoff(url=MEDIA_URL, kind="photo", size=MB)

    delivered = _deliver(_query(reply_photo=reply_photo), plan)

    assert delivered.state == "delivered"
    assert reply_photo.await_args.kwargs["photo"] == MEDIA_URL


def test_refusal_reports_not_delivered_so_caller_falls_back():
    """Отказ Telegram — не ошибка, а сигнал качать файл самим."""
    reply_video = AsyncMock(
        side_effect=telegram.error.BadRequest("failed to get HTTP URL content")
    )
    plan = UrlHandoff(url=MEDIA_URL, kind="video", size=2 * MB)

    delivered = _deliver(_query(reply_video=reply_video), plan)

    assert delivered.state == "refused"


def test_timeout_has_unknown_delivery_outcome_and_does_not_fall_back():
    """Таймаут после начала запроса не запускает рискованную повторную отправку."""
    reply_video = AsyncMock(side_effect=telegram.error.TimedOut())
    plan = UrlHandoff(url=MEDIA_URL, kind="video", size=2 * MB)
    session_data = {}

    outcome = _deliver(
        _query(reply_video=reply_video), plan, session_data=session_data
    )

    assert outcome.state == "unknown"
    assert outcome.confirmed_items == 0
    assert outcome.error.__class__ is telegram.error.TimedOut
    assert session_data["_delivery_outcome_unknown"] is True
    assert reply_video.await_count == 1


def test_file_id_from_a_link_is_cached(monkeypatch):
    """Ссылка не отменяет кэш: Telegram возвращает file_id и его надо сохранить."""
    saved = []
    monkeypatch.setattr(telegram_utils.telegram_cache, "set", saved.append)
    reply_video = AsyncMock(return_value=_sent("video", file_id="AGADfromlink"))
    plan = UrlHandoff(url=MEDIA_URL, kind="video", size=2 * MB)

    _deliver(
        _query(reply_video=reply_video),
        plan,
        cache_format_id="tiktok_video",
        video_info={"title": "ролик"},
    )

    assert len(saved) == 1
    assert saved[0].file_id == "AGADfromlink"
    assert saved[0].url == PAGE_URL
    assert saved[0].format_id == "tiktok_video"
    assert saved[0].title == "ролик"


def test_nothing_is_cached_without_a_cache_key(monkeypatch):
    saved = []
    monkeypatch.setattr(telegram_utils.telegram_cache, "set", saved.append)
    reply_video = AsyncMock(return_value=_sent("video"))
    plan = UrlHandoff(url=MEDIA_URL, kind="video", size=2 * MB)

    _deliver(_query(reply_video=reply_video), plan)

    assert saved == []


def test_photo_is_not_cached(monkeypatch):
    """Кэш file_id рассчитан на видео, аудио и документы — фото туда не пишем."""
    saved = []
    monkeypatch.setattr(telegram_utils.telegram_cache, "set", saved.append)
    reply_photo = AsyncMock(return_value=_sent("photo"))
    plan = UrlHandoff(url=MEDIA_URL, kind="photo", size=MB)

    _deliver(
        _query(reply_photo=reply_photo), plan, cache_format_id="instagram_photo"
    )

    assert saved == []


# --- память отказов CDN -----------------------------------------------------


@pytest.fixture(autouse=True)
def clean_refusal_memory(monkeypatch):
    """Каждый тест начинает с чистой памятью отказов."""
    from utils.url_delivery import HandoffRefusals

    monkeypatch.setattr(telegram_utils, "_HANDOFF_REFUSALS", HandoffRefusals())


def test_refused_cdn_is_not_asked_again():
    """0.3 с на заведомо провальную попытку — потеря на каждом запросе."""
    reply_video = AsyncMock(
        side_effect=telegram.error.BadRequest("failed to get HTTP URL content")
    )
    plan = UrlHandoff(url=MEDIA_URL, kind="video", size=2 * MB)
    query = _query(reply_video=reply_video)

    assert _deliver(query, plan).state == "refused"
    assert _deliver(query, plan).state == "refused"

    assert reply_video.await_count == 1, "вторая попытка не должна была уйти"


def test_refusal_for_video_does_not_block_pictures():
    """Измерено: картинки TikTok уходили, когда видео того же CDN отказывало."""
    reply_video = AsyncMock(
        side_effect=telegram.error.BadRequest("failed to get HTTP URL content")
    )
    reply_photo = AsyncMock(return_value=_sent("photo"))

    assert _deliver(
        _query(reply_video=reply_video), UrlHandoff(MEDIA_URL, "video", 2 * MB)
    ).state == "refused"
    delivered = _deliver(
        _query(reply_photo=reply_photo),
        UrlHandoff("https://p16-sign.tiktokcdn-us.com/obj/image.jpeg", "photo", MB),
    )

    assert delivered.state == "delivered"


def test_unclassified_bad_request_is_not_treated_as_url_refusal():
    """Неизвестный отказ может относиться к подписи или параметрам запроса."""
    reply_video = AsyncMock(side_effect=telegram.error.BadRequest("отказ"))
    plan = UrlHandoff(url=MEDIA_URL, kind="video", size=2 * MB)

    with pytest.raises(telegram.error.BadRequest, match="отказ"):
        _deliver(_query(reply_video=reply_video), plan)

    assert reply_video.await_count == 1
