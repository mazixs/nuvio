"""Тесты доставки фото-постов альбомами и по прямым ссылкам."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import telegram

from utils import telegram_utils, tiktok_instagram_utils
from utils.url_delivery import HandoffRefusals, PhotoPostHandoff, UrlHandoff


MB = 1024 * 1024
IMAGES = [
    "https://p16-sign.tiktokcdn-us.com/obj/image-1.jpeg",
    "https://p16-sign.tiktokcdn-us.com/obj/image-2.jpeg",
]
AUDIO = "https://v16-ies-music.tiktokcdn-us.com/obj/track.mp3"
REFERER = "https://www.tiktok.com/"

pytestmark = pytest.mark.unit


@pytest.fixture
def sizes(monkeypatch):
    """Подменяет замер размера на управляемую таблицу."""
    table: dict[str, int | None] = {}
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "probe_remote_size",
        lambda url, referer=None: table.get(url),
    )
    return table


def test_whole_post_goes_by_link(sizes):
    sizes.update({IMAGES[0]: 300 * 1024, IMAGES[1]: 400 * 1024, AUDIO: 250 * 1024})

    plan = tiktok_instagram_utils.resolve_photo_post_handoff(IMAGES, AUDIO, REFERER)

    assert plan is not None
    assert [item.url for item in plan.images] == IMAGES
    assert all(item.kind == "photo" for item in plan.images)
    assert plan.audio is not None
    assert plan.audio.kind == "audio"


def test_post_without_sound_still_goes_by_link(sizes):
    sizes.update({IMAGES[0]: 300 * 1024, IMAGES[1]: 400 * 1024})

    plan = tiktok_instagram_utils.resolve_photo_post_handoff(IMAGES, None, REFERER)

    assert plan is not None
    assert plan.audio is None


def test_one_unmeasurable_image_cancels_the_whole_post(sizes):
    sizes.update({IMAGES[0]: 300 * 1024, AUDIO: 250 * 1024})

    assert tiktok_instagram_utils.resolve_photo_post_handoff(IMAGES, AUDIO, REFERER) is None


def test_oversized_image_cancels_the_whole_post(sizes):
    """Лимит на фото по ссылке — 5 МБ, и он ниже лимита на видео."""
    sizes.update({IMAGES[0]: 300 * 1024, IMAGES[1]: 6 * MB, AUDIO: 250 * 1024})

    assert tiktok_instagram_utils.resolve_photo_post_handoff(IMAGES, AUDIO, REFERER) is None


def test_unusable_sound_cancels_the_whole_post(sizes):
    """Картинки без звука — это уже другой пост, поэтому лучше обычный путь."""
    sizes.update({IMAGES[0]: 300 * 1024, IMAGES[1]: 400 * 1024})

    assert tiktok_instagram_utils.resolve_photo_post_handoff(IMAGES, AUDIO, REFERER) is None


def test_image_outside_the_allowlist_cancels_the_whole_post(sizes):
    internal = "http://telegram-bot-api:8081/file/image.jpg"
    sizes.update({IMAGES[0]: 300 * 1024, internal: 100 * 1024})

    plan = tiktok_instagram_utils.resolve_photo_post_handoff(
        [IMAGES[0], internal], None, REFERER
    )

    assert plan is None


def test_empty_post_has_nothing_to_hand_over(sizes):
    assert tiktok_instagram_utils.resolve_photo_post_handoff([], None, REFERER) is None


# --- отправка поста ссылками ------------------------------------------------


@pytest.fixture(autouse=True)
def clean_refusal_memory(monkeypatch):
    """Память отказов живёт в модуле, поэтому тесты обнуляют её каждый раз."""
    monkeypatch.setattr(telegram_utils, "_HANDOFF_REFUSALS", HandoffRefusals())



def _query(reply_photo=None, reply_audio=None, reply_media_group=None):
    return SimpleNamespace(
        message=SimpleNamespace(
            reply_photo=reply_photo or AsyncMock(),
            reply_audio=reply_audio or AsyncMock(),
            reply_media_group=reply_media_group or AsyncMock(return_value=[]),
        )
    )


def _plan(audio: bool = True):
    images = tuple(
        UrlHandoff(url=url, kind="photo", size=300 * 1024) for url in IMAGES
    )
    return PhotoPostHandoff(
        images=images,
        audio=UrlHandoff(url=AUDIO, kind="audio", size=250 * 1024) if audio else None,
    )


def test_post_is_sent_as_links_in_order():
    reply_media_group = AsyncMock(
        return_value=[SimpleNamespace(message_id=1), SimpleNamespace(message_id=2)]
    )
    reply_audio = AsyncMock()

    sent = asyncio.run(
        telegram_utils._deliver_photo_post_by_url(
            _query(reply_audio=reply_audio, reply_media_group=reply_media_group), _plan()
        )
    )

    assert sent.state == "delivered"
    assert sent.confirmed_items == 2
    assert sent.audio_delivered is True
    assert len(sent.messages) == 3
    assert len(reply_media_group.await_args.kwargs["media"]) == 2
    assert [item.media for item in reply_media_group.await_args.kwargs["media"]] == IMAGES
    assert reply_audio.await_args.kwargs["audio"] == AUDIO
    assert reply_audio.await_count == 1


def test_refusal_on_the_first_image_falls_back_quietly():
    """Ничего ещё не отправлено — можно спокойно уйти на обычный путь."""
    reply_media_group = AsyncMock(
        side_effect=telegram.error.BadRequest("failed to get HTTP URL content")
    )

    sent = asyncio.run(
        telegram_utils._deliver_photo_post_by_url(
            _query(reply_media_group=reply_media_group), _plan(audio=False)
        )
    )

    assert sent.state == "refused"
    assert sent.confirmed_items == 0


def test_refusal_after_first_album_falls_back_for_unconfirmed_remainder():
    """Подтвержденный первый альбом сохраняется, файлы нужны только для остатка."""
    images = tuple(
        UrlHandoff(
            url=f"https://p16-sign.tiktokcdn-us.com/obj/image-{index}.jpeg",
            kind="photo",
            size=300 * 1024,
        )
        for index in range(11)
    )
    plan = PhotoPostHandoff(images=images, audio=None)
    reply_media_group = AsyncMock(
        side_effect=[
            [SimpleNamespace(message_id=1)],
            telegram.error.BadRequest("failed to get HTTP URL content"),
        ]
    )
    session_data = {"session_id": "photo-handoff-test"}

    sent = asyncio.run(
        telegram_utils._deliver_photo_post_by_url(
            _query(reply_media_group=reply_media_group), plan, session_data
        )
    )

    assert sent.state == "refused"
    assert sent.confirmed_items == 9
    assert len(sent.messages) == 1
    assert session_data["_delivered_items"] == 9
    assert session_data["_delivery_progress"] == 9


def test_unknown_outcome_after_first_album_returns_confirmed_progress():
    images = tuple(
        UrlHandoff(
            url=f"https://p16-sign.tiktokcdn-us.com/obj/image-{index}.jpeg",
            kind="photo",
            size=300 * 1024,
        )
        for index in range(11)
    )
    plan = PhotoPostHandoff(images=images, audio=None)
    first_group = [SimpleNamespace(message_id=index) for index in range(9)]
    reply_media_group = AsyncMock(
        side_effect=[first_group, telegram.error.NetworkError("connection lost")]
    )
    session_data = {"session_id": "unknown-after-album"}

    outcome = asyncio.run(
        telegram_utils._deliver_photo_post_by_url(
            _query(reply_media_group=reply_media_group), plan, session_data
        )
    )

    assert outcome.state == "unknown"
    assert outcome.confirmed_items == 9
    assert outcome.messages == tuple(first_group)
    assert session_data["_delivery_outcome_unknown"] is True
    assert reply_media_group.await_count == 2


# --- скачивание фото-поста файлами ------------------------------------------


@pytest.fixture
def remote_files(monkeypatch, tmp_path):
    """Записывает скачивания вместо сетевых запросов."""
    calls = []

    def fake_download(url, destination, referer=None, expected_content_type=None):
        calls.append((url, destination.name, referer))
        destination.write_bytes(b"media")
        return destination

    monkeypatch.setattr(tiktok_instagram_utils, "_download_remote_file", fake_download)
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "get_temp_file_path",
        lambda _session_id, name: tmp_path / name,
    )
    return calls


def _tiktok_photo_info():
    return {
        "_nuvio_tiktok_photo_post": True,
        "title": "Рецепт",
        "_nuvio_tiktok_images": ["https://cdn.example/a.jpeg", "https://cdn.example/b"],
        "_nuvio_tiktok_audio_url": "https://cdn.example/track",
    }


def _instagram_photo_info():
    return {
        "_nuvio_instagram_photo_post": True,
        "title": "Пост",
        "_nuvio_instagram_images": ["https://cdn.example/a.jpg", "https://cdn.example/b.jpg"],
        "_nuvio_instagram_audio_url": "https://cdn.example/sound",
    }


def test_tiktok_photo_post_assets_keep_order_and_names(remote_files, tmp_path):
    assets = tiktok_instagram_utils.download_tiktok_photo_post_assets(
        "https://www.tiktok.com/@cook/photo/1", "sid", _tiktok_photo_info()
    )

    assert remote_files == [
        ("https://cdn.example/a.jpeg", "Рецепт_01.jpeg", None),
        ("https://cdn.example/b", "Рецепт_02.jpg", None),
        ("https://cdn.example/track", "Рецепт_audio.mp3", None),
    ]
    assert [item["path"].name for item in assets["items"]] == [
        "Рецепт_01.jpeg",
        "Рецепт_02.jpg",
    ]
    assert all(item["kind"] == "photo" for item in assets["items"])
    assert assets["audio"] == tmp_path / "Рецепт_audio.mp3"


def test_instagram_photo_post_assets_use_instagram_referer(remote_files, tmp_path):
    assets = tiktok_instagram_utils.download_instagram_photo_post_assets(
        "https://www.instagram.com/p/abc/", "sid", _instagram_photo_info()
    )

    referer = "https://www.instagram.com/"
    assert remote_files == [
        ("https://cdn.example/a.jpg", "Пост_01.jpg", referer),
        ("https://cdn.example/b.jpg", "Пост_02.jpg", referer),
        ("https://cdn.example/sound", "Пост_audio.m4a", referer),
    ]
    assert [item["path"].name for item in assets["items"]] == ["Пост_01.jpg", "Пост_02.jpg"]
    assert assets["audio"] == tmp_path / "Пост_audio.m4a"


@pytest.mark.parametrize(
    "download,info,expected",
    [
        (
            tiktok_instagram_utils.download_tiktok_photo_audio,
            _tiktok_photo_info,
            ("https://cdn.example/track", "Рецепт_audio.mp3", None),
        ),
        (
            tiktok_instagram_utils.download_instagram_photo_audio,
            _instagram_photo_info,
            ("https://cdn.example/sound", "Пост_audio.m4a", "https://www.instagram.com/"),
        ),
    ],
)
def test_photo_post_audio_skips_image_downloads(remote_files, tmp_path, download, info, expected):
    result = download("https://example.test/post", "sid", None, False, info())

    assert remote_files == [expected]
    assert result == tmp_path / expected[1]


def test_photo_post_audio_missing_track_is_reported(remote_files):
    info = _tiktok_photo_info()
    info["_nuvio_tiktok_audio_url"] = None

    with pytest.raises(tiktok_instagram_utils.PhotoPostAudioMissingError):
        tiktok_instagram_utils.download_tiktok_photo_audio(
            "https://example.test/post", "sid", None, False, info
        )
    assert remote_files == []


def test_disallowed_url_cancels_handoff_before_any_probe(monkeypatch):
    probed = []
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "probe_remote_size",
        lambda url, referer=None: probed.append(url) or 100,
    )

    plan = tiktok_instagram_utils.resolve_photo_post_handoff(
        [IMAGES[0], "http://telegram-bot-api:8081/file/image.jpg"], AUDIO, REFERER
    )

    assert plan is None
    assert probed == []
