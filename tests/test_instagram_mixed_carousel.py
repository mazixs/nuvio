import pytest

from utils import tiktok_instagram_utils as instagram


def _sidecar():
    return {
        "shortcode": "AbCdEf123",
        "owner": {"username": "cook"},
        "caption": {"text": "Рецепт\nСмешать и запечь"},
        "carousel_media": [
            {
                "is_video": False,
                "image_versions2": {
                    "candidates": [{"url": "https://cdn.example/photo-1.jpg", "width": 800}]
                },
            },
            {
                "is_video": True,
                "video_duration": 8.5,
                "video_versions": [
                    {
                        "url": "https://cdn.example/clip.mp4",
                        "width": 640,
                        "height": 360,
                        "bandwidth": 500,
                    }
                ],
                "image_versions2": {
                    "candidates": [{"url": "https://cdn.example/cover.jpg", "width": 800}]
                },
            },
            {
                "is_video": False,
                "image_versions2": {
                    "candidates": [{"url": "https://cdn.example/photo-2.jpg", "width": 900}]
                },
            },
        ],
    }


def test_instagram_sidecar_keeps_mixed_order_and_direct_video_url():
    info = instagram._build_instagram_photo_info(
        "https://www.instagram.com/p/AbCdEf123/", _sidecar()
    )

    items = info["_nuvio_instagram_carousel_items"]
    assert info["_nuvio_instagram_mixed_post"] is True
    assert info["_nuvio_instagram_carousel_complete"] is True
    assert [item["kind"] for item in items] == ["photo", "video", "photo"]
    assert [item["url"] for item in items] == [
        "https://cdn.example/photo-1.jpg",
        "https://cdn.example/clip.mp4",
        "https://cdn.example/photo-2.jpg",
    ]
    assert info["description"] == "Рецепт\nСмешать и запечь"


def test_instagram_mixed_sidecar_without_video_url_is_marked_incomplete():
    data = _sidecar()
    data["carousel_media"][1]["video_versions"] = []

    info = instagram._build_instagram_photo_info(
        "https://www.instagram.com/p/AbCdEf123/", data
    )

    assert info["_nuvio_instagram_mixed_post"] is True
    assert info["_nuvio_instagram_carousel_complete"] is False
    assert info["_nuvio_instagram_carousel_items"][1]["url"] is None


def test_instagram_html_meta_caption_is_not_shown_as_full_description():
    info = instagram._build_instagram_photo_info(
        "https://www.instagram.com/p/AbCdEf123/",
        {
            "shortcode": "AbCdEf123",
            "display_url": "https://cdn.example/photo.jpg",
            "caption": "1,200 likes, 20 comments - cook: recipe preview...",
            "_nuvio_description_source": "instagram_html_meta",
        },
    )

    assert info["description"] is None
    assert info["_nuvio_description_status"] == "unavailable"


def test_instagram_mixed_carousel_metadata_count_must_match_ytdlp(monkeypatch):
    monkeypatch.setattr(
        instagram,
        "_try_get_instagram_photo_info",
        lambda _url: instagram._build_instagram_photo_info(
            "https://www.instagram.com/p/AbCdEf123/", _sidecar()
        ),
    )

    enriched = instagram._enrich_instagram_carousel_info(
        "https://www.instagram.com/p/AbCdEf123/",
        {"_type": "playlist", "entries": [{"id": "1"}, {"id": "2"}]},
    )

    assert enriched["_nuvio_instagram_mixed_post"] is True
    assert enriched["_nuvio_instagram_carousel_complete"] is False
    assert enriched["_nuvio_instagram_carousel_incomplete"] is True


def test_instagram_mixed_carousel_types_must_match_ytdlp(monkeypatch):
    monkeypatch.setattr(
        instagram,
        "_try_get_instagram_photo_info",
        lambda _url: instagram._build_instagram_photo_info(
            "https://www.instagram.com/p/AbCdEf123/", _sidecar()
        ),
    )

    enriched = instagram._enrich_instagram_carousel_info(
        "https://www.instagram.com/p/AbCdEf123/",
        {
            "_type": "playlist",
            "entries": [{"formats": []}, {"formats": []}, {"formats": []}],
        },
    )

    assert enriched["_nuvio_instagram_carousel_complete"] is False
    assert enriched["_nuvio_instagram_carousel_incomplete"] is True
    assert enriched["_nuvio_instagram_expected_kinds"] == [
        "photo",
        "photo",
        "photo",
    ]


def test_incomplete_mixed_carousel_stops_before_downloading(monkeypatch):
    info = instagram._build_instagram_photo_info(
        "https://www.instagram.com/p/AbCdEf123/", _sidecar()
    )
    info["_nuvio_instagram_carousel_complete"] = False
    monkeypatch.setattr(
        instagram,
        "_download_remote_file",
        lambda *_args, **_kwargs: pytest.fail("неполный sidecar нельзя скачивать"),
    )

    with pytest.raises(ValueError, match="полный состав"):
        instagram.download_instagram_photo_post_assets(
            "https://www.instagram.com/p/AbCdEf123/", "carousel-test", info
        )
