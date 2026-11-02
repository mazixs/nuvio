"""Скачивание для HTTP API: проверка ссылки и те же решения о формате, что у бота."""

import pytest

from config import BLOCKING_TASK_TIMEOUT
from utils import media_fetch, rutube_vk_utils, tiktok_instagram_utils, youtube_utils

MB = 1024 * 1024
YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


@pytest.mark.parametrize(
    ("url", "platform"),
    [
        (YOUTUBE_URL, "youtube"),
        ("https://youtu.be/dQw4w9WgXcQ", "youtube"),
        ("https://www.tiktok.com/@user/video/1234567890123456789", "tiktok"),
        ("https://www.instagram.com/reel/ABC123/", "instagram"),
        ("https://rutube.ru/video/0123456789abcdef0123456789abcdef/", "rutube"),
        ("https://vkvideo.ru/video-1_2", "vk"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ/", "youtube"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42", "youtube"),
        ("https://vm.tiktok.com/ZMabc123/", "tiktok"),
        ("https://www.tiktok.com/t/ZTabc123/", "tiktok"),
        ("https://www.instagram.com/p/ABC_123-x/", "instagram"),
        ("https://www.instagram.com/someuser/reel/ABC123/", "instagram"),
        ("https://rutube.ru/shorts/0123456789abcdef/", "rutube"),
        ("https://vk.com/clip-1_2", "vk"),
        ("https://vk.com/wall-1_2", "vk"),
        ("https://vk.com/videos-1?z=video-1_2%2Fpl_-1_-2", "vk"),
        ("https://vk.com/video_ext.php?oid=-1&id=2&hash=abc", "vk"),
        # Каналы, профили, плейлисты и поиск — не одна публикация.
        ("https://www.youtube.com/@LinusTechTips", None),
        ("https://www.youtube.com/LinusTechTips", None),
        ("https://www.youtube.com/playlist?list=PL0123456789", None),
        ("https://www.youtube.com/watch?list=PL0123456789", None),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&v=aaaaaaaaaaa", None),
        ("https://www.youtube.com/results?search_query=cats", None),
        ("https://www.tiktok.com/@user", None),
        ("https://www.tiktok.com/@user/video/１２３", None),
        ("https://www.instagram.com/someuser/", None),
        ("https://rutube.ru/channel/1/", None),
        ("https://rutube.ru/plst/123/", None),
        ("https://vk.com/videos-1", None),
        ("https://vk.com/video-١_٢", None),
        ("https://vk.com/video_ext.php?oid=-1", None),
        ("https://example.com/watch?v=dQw4w9WgXcQ", None),
        ("ftp://www.youtube.com/watch?v=dQw4w9WgXcQ", None),
        ("//www.youtube.com/watch?v=dQw4w9WgXcQ", None),
        ("https://user:pass@www.youtube.com/watch?v=dQw4w9WgXcQ", None),
        ("https://www.youtube.com:443/watch?v=dQw4w9WgXcQ", None),
        ("https://www.youtube.com:bad/watch?v=dQw4w9WgXcQ", None),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ\n", None),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&x=\x00", None),
        ("https://youtube.com.evil.example/watch?v=dQw4w9WgXcQ", None),
        ("https://www.youtube.com/watch?v=" + "a" * 2048, None),
    ],
)
def test_detect_platform(url, platform):
    assert media_fetch.detect_platform(url) == platform


def test_unknown_media_type_is_rejected():
    with pytest.raises(ValueError):
        media_fetch.prepare_download(YOUTUBE_URL, "youtube", "gif", "api-test")


# ── YouTube ───────────────────────────────────────────────────────


@pytest.fixture
def youtube(monkeypatch):
    state = {"formats": {}, "calls": [], "failures": {}}

    def download_video(url, format_id, session_id):
        state["calls"].append(("video", format_id))
        if format_id in state["failures"]:
            raise state["failures"][format_id]
        return f"/media/{session_id}/{format_id}.mp4"

    monkeypatch.setattr(youtube_utils, "get_video_info", lambda url, session_id=None: {"id": "x"})
    monkeypatch.setattr(youtube_utils, "get_available_formats", lambda info: state["formats"])
    monkeypatch.setattr(youtube_utils, "download_video", download_video)
    monkeypatch.setattr(
        youtube_utils, "download_audio_native",
        lambda url, format_id, session_id: state["calls"].append(("native", format_id)) or "a.m4a",
    )
    monkeypatch.setattr(
        youtube_utils, "download_audio",
        lambda url, format_id, session_id, preferred_codec=None:
            state["calls"].append(("convert", format_id, preferred_codec)) or "a.m4a",
    )
    return state


def test_youtube_audio_prefers_a_native_track(youtube):
    youtube["formats"] = {"audio_only": [
        {"format_id": "251", "ext": "webm", "acodec": "opus", "filesize": 5 * MB},
        {"format_id": "140", "ext": "m4a", "acodec": "mp4a.40.2", "filesize": 4 * MB},
    ]}
    media_fetch.prepare_download(YOUTUBE_URL, "youtube", "audio", "api-1").run()
    assert youtube["calls"] == [("native", "140")]


def test_youtube_audio_converts_when_no_native_track_exists(youtube):
    youtube["formats"] = {"audio_only": [{"format_id": "251", "ext": "webm", "acodec": "opus", "filesize": 5 * MB}]}
    media_fetch.prepare_download(YOUTUBE_URL, "youtube", "audio", "api-1").run()
    assert youtube["calls"] == [("convert", "251", "m4a")]


def test_youtube_without_audio_has_no_suitable_format(youtube):
    youtube["formats"] = {"audio_only": []}
    with pytest.raises(media_fetch.NoSuitableFormatError):
        media_fetch.prepare_download(YOUTUBE_URL, "youtube", "audio", "api-1")


SIZED_PAIR = {
    "video_only": [{"format_id": "299", "height": 1080, "ext": "mp4", "vcodec": "avc1.64002a", "filesize": 30 * MB}],
    "audio_only": [{"format_id": "140", "ext": "m4a", "acodec": "mp4a.40.2", "filesize": 3 * MB}],
    "combined": [{"format_id": "18", "height": 360, "ext": "mp4", "vcodec": "avc1.42001E", "filesize": None}],
}


def test_youtube_video_uses_the_bot_choice(youtube):
    youtube["formats"] = SIZED_PAIR
    prepared = media_fetch.prepare_download(YOUTUBE_URL, "youtube", "video", "api-1")
    assert prepared.run() == "/media/api-1/299+140.mp4"
    assert prepared.timeout == BLOCKING_TASK_TIMEOUT


def test_youtube_video_falls_back_to_an_unsized_format(youtube):
    youtube["formats"] = SIZED_PAIR
    youtube["failures"]["299+140"] = RuntimeError("ERROR: Requested format is not available")
    assert media_fetch.prepare_download(YOUTUBE_URL, "youtube", "video", "api-1").run() == "/media/api-1/18.mp4"
    assert youtube["calls"] == [("video", "299+140"), ("video", "18")]


def test_youtube_network_failure_skips_the_fallback(youtube):
    youtube["formats"] = SIZED_PAIR
    youtube["failures"]["299+140"] = ConnectionRefusedError("refused")
    with pytest.raises(ConnectionRefusedError):
        media_fetch.prepare_download(YOUTUBE_URL, "youtube", "video", "api-1").run()
    assert youtube["calls"] == [("video", "299+140")]


def test_youtube_video_without_any_format_is_rejected(youtube):
    youtube["formats"] = {"video_only": [], "audio_only": [], "combined": []}
    with pytest.raises(media_fetch.NoSuitableFormatError):
        media_fetch.prepare_download(YOUTUBE_URL, "youtube", "video", "api-1")


# ── TikTok и Instagram ────────────────────────────────────────────


def test_tiktok_photo_post_is_not_a_video(monkeypatch):
    monkeypatch.setattr(tiktok_instagram_utils, "get_tiktok_info", lambda url: {"_nuvio_tiktok_photo_post": True})
    with pytest.raises(media_fetch.UnsupportedMediaError):
        media_fetch.prepare_download("https://www.tiktok.com/@u/video/1", "tiktok", "video", "api-1")


def test_tiktok_short_link_to_a_profile_is_unsupported(monkeypatch):
    monkeypatch.setattr(tiktok_instagram_utils, "resolve_tiktok_url", lambda url: "https://www.tiktok.com/@user?lang=en")
    monkeypatch.setattr(tiktok_instagram_utils, "get_tiktok_info", lambda url: pytest.fail("профиль не скачивается"))
    with pytest.raises(media_fetch.UnsupportedMediaError):
        media_fetch.prepare_download("https://vm.tiktok.com/ZMabc123/", "tiktok", "video", "api-1")


def test_tiktok_short_link_is_downloaded_by_its_final_address(monkeypatch):
    final = "https://www.tiktok.com/@user/video/1234567890123456789?is_from_webapp=1"
    seen = []
    monkeypatch.setattr(tiktok_instagram_utils, "resolve_tiktok_url", lambda url: final)
    monkeypatch.setattr(tiktok_instagram_utils, "get_tiktok_info", lambda url: seen.append(url) or {})
    media_fetch.prepare_download("https://vm.tiktok.com/ZMabc123/", "tiktok", "video", "api-1")
    assert seen == [final]


def test_tiktok_reuses_the_extracted_info(monkeypatch):
    info = {"id": "1"}
    seen = []
    monkeypatch.setattr(tiktok_instagram_utils, "get_tiktok_info", lambda url: info)
    monkeypatch.setattr(
        tiktok_instagram_utils, "download_tiktok_video",
        lambda url, session_id, *args: seen.append((session_id, args)) or "v.mp4",
    )
    media_fetch.prepare_download("https://www.tiktok.com/@u/video/1", "tiktok", "video", "api-1").run()
    assert seen == [("api-1", (None, False, info))]


@pytest.mark.parametrize(
    "marker",
    ["_nuvio_instagram_photo_post", "_nuvio_instagram_mixed_post", "_nuvio_instagram_carousel_incomplete"],
)
def test_instagram_posts_that_are_not_one_video(monkeypatch, marker):
    monkeypatch.setattr(tiktok_instagram_utils, "get_instagram_info", lambda url: {marker: True})
    with pytest.raises(media_fetch.UnsupportedMediaError):
        media_fetch.prepare_download("https://www.instagram.com/p/X/", "instagram", "video", "api-1")


def test_instagram_photo_handoff_error_becomes_unsupported(monkeypatch):
    def refuse(*args):
        raise tiktok_instagram_utils.PhotoPostError("фото-пост")

    monkeypatch.setattr(tiktok_instagram_utils, "get_instagram_info", lambda url: {})
    monkeypatch.setattr(tiktok_instagram_utils, "download_instagram_video", refuse)
    prepared = media_fetch.prepare_download("https://www.instagram.com/p/X/", "instagram", "video", "api-1")
    with pytest.raises(media_fetch.UnsupportedMediaError):
        prepared.run()


def test_instagram_audio_of_a_photo_post_becomes_unsupported(monkeypatch):
    def refuse(*args):
        raise tiktok_instagram_utils.PhotoPostError("фото-пост")

    monkeypatch.setattr(tiktok_instagram_utils, "get_instagram_info", lambda url: {})
    monkeypatch.setattr(tiktok_instagram_utils, "download_instagram_audio", refuse)
    prepared = media_fetch.prepare_download("https://www.instagram.com/p/X/", "instagram", "audio", "api-1")
    with pytest.raises(media_fetch.UnsupportedMediaError):
        prepared.run()


# ── Rutube и VK ───────────────────────────────────────────────────


def test_vk_live_stream_is_unsupported(monkeypatch):
    def live(url, session_id=None):
        raise rutube_vk_utils.VkLiveActiveError("live")

    monkeypatch.setattr(rutube_vk_utils, "get_vk_info", live)
    with pytest.raises(media_fetch.UnsupportedMediaError):
        media_fetch.prepare_download("https://vkvideo.ru/video-1_2", "vk", "video", "api-1")


@pytest.mark.parametrize(
    ("size", "timeout"),
    [
        (None, BLOCKING_TASK_TIMEOUT),
        (100 * MB, BLOCKING_TASK_TIMEOUT),
        (1500 * MB, max(BLOCKING_TASK_TIMEOUT, 1620)),
        (9000 * MB, 3600),
    ],
)
def test_vk_video_gets_time_by_size_and_the_chosen_format(monkeypatch, size, timeout):
    seen = []
    monkeypatch.setattr(
        rutube_vk_utils, "get_vk_info",
        lambda url, session_id=None: {"_nuvio_vk_format_id": "url720", "_nuvio_vk_format_size": size},
    )
    monkeypatch.setattr(
        rutube_vk_utils, "download_vk_video",
        lambda url, session_id, format_id=None: seen.append(format_id) or "v.mp4",
    )
    prepared = media_fetch.prepare_download("https://vkvideo.ru/video-1_2", "vk", "video", "api-1")
    prepared.run()
    assert prepared.timeout == timeout
    assert seen == ["url720"]


@pytest.mark.parametrize(("media_type", "function"), [("video", "download_rutube_video"), ("audio", "download_rutube_audio")])
def test_rutube_checks_the_link_before_downloading(monkeypatch, media_type, function):
    calls = []
    monkeypatch.setattr(rutube_vk_utils, "get_rutube_info", lambda url, session_id=None: calls.append("info"))
    monkeypatch.setattr(rutube_vk_utils, function, lambda url, session_id: calls.append(function) or "f")
    prepared = media_fetch.prepare_download("https://rutube.ru/video/abc/", "rutube", media_type, "api-1")
    assert calls == ["info"]
    prepared.run()
    assert calls == ["info", function]
