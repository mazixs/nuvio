import pytest

from utils.social_delivery import (
    chunk_media,
    description_delivery_plan,
    description_status,
    media_album_sizes,
    normalize_description,
    split_telegram_text,
    telegram_text_length,
)
from utils import tiktok_instagram_utils


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, []),
        (1, [1]),
        (2, [2]),
        (10, [10]),
        (11, [9, 2]),
        (12, [10, 2]),
        (20, [10, 10]),
        (21, [10, 9, 2]),
        (31, [10, 10, 9, 2]),
    ],
)
def test_media_album_sizes_avoid_singleton_tail(count, expected):
    assert media_album_sizes(count) == expected


def test_chunk_media_preserves_order_and_single_item():
    values = list(range(12))
    groups = chunk_media(values)
    assert [len(group) for group in groups] == [10, 2]
    assert [item for group in groups for item in group] == values
    assert chunk_media(["only"]) == [["only"]]


def test_media_album_chunks_keep_unrelated_posts_separate():
    first_post = chunk_media(["a1", "a2"])
    second_post = chunk_media(["b1", "b2"])

    assert first_post == [["a1", "a2"]]
    assert second_post == [["b1", "b2"]]


@pytest.mark.parametrize(
    ("text", "expected_chunks"),
    [
        ("a" * 1023, 1),
        ("a" * 1024, 1),
        ("a" * 1025, 1),
        ("a" * 4096, 1),
        ("a" * 4097, 2),
        ("🍰" * 2049, 2),
        (("recipe 👩‍🍳\n\n" * 400), 2),
    ],
)
def test_description_chunks_respect_telegram_utf16_limit(text, expected_chunks):
    chunks = split_telegram_text(text)
    assert len(chunks) == expected_chunks
    assert "".join(chunks) == text
    assert all(telegram_text_length(chunk) <= 4096 for chunk in chunks)


@pytest.mark.parametrize(
    ("length", "caption", "text_parts"),
    [
        (1023, True, 0),
        (1024, True, 0),
        (1025, False, 1),
        (4095, False, 1),
        (4096, False, 1),
        (4097, False, 2),
    ],
)
def test_description_delivery_uses_caption_only_within_caption_limit(
    length, caption, text_parts
):
    value = "x" * length
    media_caption, chunks = description_delivery_plan(value)
    assert (media_caption is not None) is caption
    assert len(chunks) == text_parts
    assert "".join(([media_caption] if media_caption else []) + chunks) == value


def test_description_normalization_removes_only_invalid_controls():
    assert normalize_description("first\r\nsecond\x00\tthird\r") == (
        "first\nsecond\tthird\n"
    )
    assert normalize_description("before\ud800after") == "beforeafter"
    assert normalize_description(" \n\t ") is None
    assert normalize_description("  recipe  ") == "  recipe  "
    assert normalize_description(None) is None


def test_long_marked_text_and_links_reconstruct_exactly():
    value = "Ингредиенты 🥣\n\n[ссылка](https://example.test) *текст* #recipe " * 400
    chunks = split_telegram_text(value)

    assert len(chunks) > 2
    assert "".join(chunks) == value
    assert all(telegram_text_length(chunk) <= 4096 for chunk in chunks)


@pytest.mark.parametrize("cluster", ["a\u0301", "👩‍🍳", "🇹🇷", "🏳️‍⚧️"])
def test_text_split_does_not_cut_between_grapheme_parts(cluster):
    prefix = "x" * 4095
    chunks = split_telegram_text(prefix + cluster + " tail")

    assert chunks[0] == prefix
    assert chunks[1].startswith(cluster)
    assert "".join(chunks) == prefix + cluster + " tail"


@pytest.mark.parametrize(
    ("info", "status"),
    [
        ({"description": "recipe"}, "available"),
        ({"description": "  "}, "empty"),
        ({"description": "", "_nuvio_description_status": "unavailable"}, "unavailable"),
        ({"title": "Video title"}, "unavailable"),
        ({"_nuvio_description_status": "empty"}, "empty"),
    ],
)
def test_description_status_never_falls_back_to_title(info, status):
    assert description_status(info) == status


def test_tikwm_title_candidate_is_not_promised_as_complete_description():
    info = tiktok_instagram_utils._build_tiktok_photo_info(
        "https://www.tiktok.com/@cook/photo/123",
        {
            "title": "Возможно неполный текст",
            "description": "Возможно неполный текст",
            "_nuvio_description_source": "tikwm_title",
        },
    )

    assert info["description"] is None
    assert info["_nuvio_description_candidate"] == "Возможно неполный текст"
    assert description_status(info) == "unavailable"


def test_invalid_album_input_is_rejected():
    with pytest.raises(ValueError):
        media_album_sizes(-1)
