"""Тесты решения о доставке медиа прямой ссылкой вместо файла.

Все пороги здесь — не из документации, а из замеров на живом сервере
(docs/research/latency-disk-network-research.md §8).
"""

import pytest

from utils.url_delivery import (
    MAX_HANDOFF_BYTES,
    MAX_PHOTO_HANDOFF_BYTES,
    find_format_url,
    plan_url_handoff,
)


MB = 1024 * 1024

TIKTOK_VIDEO = "https://v16m.tiktokcdn-us.com/abc/video.mp4"
TIKTOK_AUDIO = "https://sf16-ies-music.tiktokcdn.com/obj/track.mp3"
INSTAGRAM_VIDEO = "https://scontent-ams2-1.cdninstagram.com/o1/v/t2/reel.mp4"
INSTAGRAM_PHOTO = "https://scontent-ams2-1.cdninstagram.com/v/t51/photo.jpg"
YOUTUBE_VIDEO = "https://rr5---sn-4g5ednkk.googlevideo.com/videoplayback?itag=18"

pytestmark = pytest.mark.unit


def test_small_tiktok_video_goes_by_link():
    plan = plan_url_handoff(TIKTOK_VIDEO, "video", 3 * MB)

    assert plan is not None
    assert plan.url == TIKTOK_VIDEO
    assert plan.kind == "video"
    assert plan.size == 3 * MB


def test_video_at_the_limit_still_goes_by_link():
    plan = plan_url_handoff(TIKTOK_VIDEO, "video", MAX_HANDOFF_BYTES)

    assert plan is not None


def test_video_over_the_limit_is_refused():
    """37.2 МБ Telegram отверг с `failed to get HTTP URL content` — не пробуем."""
    assert plan_url_handoff(TIKTOK_VIDEO, "video", MAX_HANDOFF_BYTES + 1) is None


def test_audio_uses_the_same_limit_as_video():
    assert plan_url_handoff(TIKTOK_AUDIO, "audio", MAX_HANDOFF_BYTES) is not None
    assert plan_url_handoff(TIKTOK_AUDIO, "audio", MAX_HANDOFF_BYTES + 1) is None


def test_photo_limit_is_lower_than_video_limit():
    assert MAX_PHOTO_HANDOFF_BYTES < MAX_HANDOFF_BYTES
    assert plan_url_handoff(INSTAGRAM_PHOTO, "photo", MAX_PHOTO_HANDOFF_BYTES) is not None
    assert plan_url_handoff(INSTAGRAM_PHOTO, "photo", MAX_PHOTO_HANDOFF_BYTES + 1) is None


@pytest.mark.parametrize("size", [None, 0, -1])
def test_unknown_size_is_refused(size):
    """Без размера обещать соблюдение лимита нельзя — идём обычным путём."""
    assert plan_url_handoff(TIKTOK_VIDEO, "video", size) is None


def test_instagram_and_youtube_hosts_are_allowed():
    assert plan_url_handoff(INSTAGRAM_VIDEO, "video", MB) is not None
    assert plan_url_handoff(YOUTUBE_VIDEO, "video", MB) is not None


def test_internal_address_is_refused():
    """Внутренний адрес Docker наружу не уходит, даже если он мал."""
    assert plan_url_handoff("http://telegram-bot-api:8081/file/x.mp4", "video", MB) is None


def test_plain_http_is_refused():
    assert plan_url_handoff(TIKTOK_VIDEO.replace("https://", "http://"), "video", MB) is None


def test_lookalike_host_is_refused():
    lookalike = "https://v16m.tiktokcdn-us.com.evil.test/video.mp4"

    assert plan_url_handoff(lookalike, "video", MB) is None


def test_empty_url_is_refused():
    assert plan_url_handoff("", "video", MB) is None


def test_missing_url_is_refused():
    """Составной формат ссылки не имеет — решение обязано это переварить."""
    assert plan_url_handoff(None, "video", MB) is None


# --- поиск прямой ссылки на выбранный формат --------------------------------

INFO = {
    "formats": [
        {"format_id": "18", "url": "https://rr5---sn-x.googlevideo.com/vp?itag=18"},
        {"format_id": "299", "url": "https://rr5---sn-x.googlevideo.com/vp?itag=299"},
        {"format_id": "140", "url": "https://rr5---sn-x.googlevideo.com/vp?itag=140"},
        {"format_id": "137"},
    ]
}


def test_direct_url_is_found_by_format_id():
    assert find_format_url(INFO, "18") == INFO["formats"][0]["url"]


def test_composite_format_has_no_single_link():
    """`299+140` собирается FFmpeg из двух файлов — одной ссылки не существует."""
    assert find_format_url(INFO, "299+140") is None


def test_unknown_format_id_has_no_link():
    assert find_format_url(INFO, "271") is None


def test_format_without_url_has_no_link():
    assert find_format_url(INFO, "137") is None


@pytest.mark.parametrize("info", [None, {}, {"formats": None}])
def test_missing_formats_are_handled(info):
    assert find_format_url(info, "18") is None


# --- allowlist не должен расходиться с быстрыми путями ----------------------


def test_handoff_trusts_every_domain_the_fast_paths_download_from():
    """Домен, откуда мы качаем сами, обязан быть пригоден и для передачи ссылки.

    Две независимые копии allowlist рано или поздно разойдутся — ровно об этом
    предупреждает docstring utils/fast_path.py.
    """
    from utils.instagram_fast_path import ALLOWED_INSTAGRAM_MEDIA_DOMAINS
    from utils.tiktok_fast_path import ALLOWED_MEDIA_DOMAINS
    from utils.url_delivery import ALLOWED_HANDOFF_DOMAINS

    assert ALLOWED_MEDIA_DOMAINS <= ALLOWED_HANDOFF_DOMAINS
    assert ALLOWED_INSTAGRAM_MEDIA_DOMAINS <= ALLOWED_HANDOFF_DOMAINS
