"""Проверки выбора рабочего медиафайла и аудио на TikTok и Instagram."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from utils import tiktok_instagram_utils as media


TIKTOK_URL = "https://www.tiktok.com/@tester/video/123456789"
INSTAGRAM_URL = "https://www.instagram.com/reel/ABC123/"


class _TikTokYDL:
    calls: list[dict] = []
    formats: list[dict] = []
    silent_formats: set[str] = set()
    failed_formats: set[str] = set()

    def __init__(self, options):
        self.options = options
        self.calls.append(options.copy())

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, url, download=False):
        assert url == TIKTOK_URL
        if not download:
            return {"formats": self.formats}
        format_id = self.options["format"]
        if format_id in self.failed_formats:
            raise RuntimeError("Postprocessing: unable to obtain file audio codec")
        info = {"title": "media", "ext": "mp4", "format_id": format_id}
        path = Path(self.prepare_filename(info))
        path.parent.mkdir(parents=True, exist_ok=True)
        if "postprocessors" in self.options:
            path = path.with_suffix(".m4a")
        path.write_bytes(format_id.encode())
        return info

    def prepare_filename(self, info):
        return self.options["outtmpl"].replace("%(title).150B", info["title"]).replace(
            "%(ext)s", info["ext"]
        )


@pytest.fixture(autouse=True)
def _setup_platforms(monkeypatch, tmp_path):
    _TikTokYDL.calls = []
    _TikTokYDL.formats = []
    _TikTokYDL.silent_formats = set()
    _TikTokYDL.failed_formats = set()
    monkeypatch.setattr(media, "TIKTOK_FAST_PATH", False)
    monkeypatch.setattr(media, "INSTAGRAM_FAST_PATH", False)
    monkeypatch.setattr(media, "TIKTOK_COOKIES_FILE", tmp_path / "missing-tiktok-cookie")
    monkeypatch.setattr(media, "INSTAGRAM_COOKIES_FILE", tmp_path / "missing-instagram-cookie")
    monkeypatch.setattr(media, "_resolve_tiktok_url", lambda url: url)
    monkeypatch.setattr(media, "_get_tiktok_base_configs", lambda: [{}])
    monkeypatch.setattr(media, "_smart_retry", lambda fn, **_kwargs: fn())
    monkeypatch.setattr(media, "create_tiktok_ytdl", _TikTokYDL)
    monkeypatch.setattr(media.yt_dlp, "YoutubeDL", _TikTokYDL)
    monkeypatch.setattr(media, "_ensure_ios_compatible_video", lambda path, *_args: path)


def test_tiktok_video_skips_silent_resolution_and_removes_failed_file(
    monkeypatch, tmp_path
):
    _TikTokYDL.formats = [
        {"format_id": "high", "vcodec": "h264", "height": 720, "tbr": 1000},
        {"format_id": "high-copy", "vcodec": "h264", "height": 720, "tbr": 900},
        {"format_id": "lower", "vcodec": "h264", "height": 480, "tbr": 500},
    ]
    seen = []

    def audio_probe(path):
        seen.append(path.read_bytes())
        return path.read_bytes() != b"high"

    monkeypatch.setattr(media, "has_audio_stream", audio_probe)

    result = media.download_tiktok_video(
        TIKTOK_URL, "tiktok-video", output_dir=tmp_path, force_local=True
    )

    assert result.read_bytes() == b"lower"
    assert seen == [b"high", b"lower"]
    assert [call["format"] for call in _TikTokYDL.calls if "format" in call] == [
        "high",
        "lower",
    ]


def test_tiktok_video_uses_generic_selector_when_formats_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(media, "has_audio_stream", lambda _path: True)

    result = media.download_tiktok_video(
        TIKTOK_URL, "tiktok-generic", output_dir=tmp_path, force_local=True
    )

    assert result.read_bytes() == b"bestvideo+bestaudio/best"
    assert _TikTokYDL.calls[-1]["format"] == "bestvideo+bestaudio/best"


def test_tiktok_video_checks_cached_size_before_yt_dlp(tmp_path):
    with pytest.raises(media.FileSizeLimitError):
        media.download_tiktok_video(
            TIKTOK_URL,
            "tiktok-large",
            output_dir=tmp_path,
            cached_info={"filesize": media.MAX_FILE_SIZE + 1},
        )

    assert _TikTokYDL.calls == []


def test_tiktok_audio_tries_next_bitrate_after_postprocessing_failure(tmp_path):
    _TikTokYDL.formats = [
        {"format_id": "bad", "acodec": "aac", "tbr": 128, "height": 720},
        {"format_id": "bad-copy", "acodec": "aac", "tbr": 128, "height": 720},
        {"format_id": "good", "acodec": "aac", "tbr": 192, "height": 480},
    ]
    _TikTokYDL.failed_formats = {"bad"}

    result = media.download_tiktok_audio(
        TIKTOK_URL, "tiktok-audio", output_dir=tmp_path, force_local=True
    )

    assert result.suffix == ".m4a"
    assert result.read_bytes() == b"good"
    assert [call["format"] for call in _TikTokYDL.calls if "format" in call] == [
        "bad",
        "good",
    ]


def test_tiktok_audio_uses_generic_format_when_metadata_has_no_audio(tmp_path):
    result = media.download_tiktok_audio(
        TIKTOK_URL, "tiktok-audio-generic", output_dir=tmp_path, force_local=True
    )

    assert result.read_bytes() == b"bestaudio/best"
    assert _TikTokYDL.calls[-1]["format"] == "bestaudio/best"


@pytest.mark.parametrize(
    ("outcomes", "suffix", "expected_codec"),
    [([0], ".m4a", "copy"), ([1, 0], ".m4a", "aac"), ([1, 1, 0], ".mp3", "mp3")],
)
def test_instagram_audio_falls_back_only_when_needed(
    monkeypatch, tmp_path, outcomes, suffix, expected_codec
):
    video = tmp_path / "instagram.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(media, "download_instagram_video", lambda *_args, **_kwargs: video)
    commands = []

    def ffmpeg_run(command, **_kwargs):
        commands.append(command)
        result = outcomes[len(commands) - 1]
        if result == 0:
            Path(command[-1]).write_bytes(b"audio")
        return SimpleNamespace(returncode=result, stderr="conversion failed" if result else "")

    monkeypatch.setattr(media, "_run_ffmpeg", ffmpeg_run)

    result = media.download_instagram_audio(
        INSTAGRAM_URL, "instagram-audio", output_dir=tmp_path, force_local=True
    )

    assert result.suffix == suffix
    assert result.read_bytes() == b"audio"
    assert not video.exists()
    assert expected_codec in commands[-1]
    assert len(commands) == len(outcomes)


def test_instagram_audio_failure_removes_input_and_partial_output(monkeypatch, tmp_path):
    video = tmp_path / "instagram.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(media, "download_instagram_video", lambda *_args, **_kwargs: video)

    def failing_ffmpeg(command, **_kwargs):
        Path(command[-1]).write_bytes(b"partial")
        return SimpleNamespace(returncode=1, stderr="conversion failed")

    monkeypatch.setattr(media, "_run_ffmpeg", failing_ffmpeg)

    with pytest.raises(Exception, match="даже в MP3"):
        media.download_instagram_audio(
            INSTAGRAM_URL, "instagram-failed", output_dir=tmp_path, force_local=True
        )

    assert not video.exists()
    assert not video.with_suffix(".m4a").exists()


def _install_instagram_ydl(monkeypatch, *, outcome="single", require_cookie=False):
    calls = []

    class InstagramYDL:
        def __init__(self, options):
            self.options = options
            calls.append(options.copy())

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, url, download=False):
            assert url == INSTAGRAM_URL
            if require_cookie and "cookiefile" not in self.options:
                raise RuntimeError("login required")
            if outcome == "empty":
                return {"_type": "playlist", "entries": []}
            entry = {"title": "ig-entry", "ext": "mp4", "format_id": "ig-h264"}
            info = (
                {"_type": "playlist", "entries": [entry]}
                if outcome == "playlist"
                else entry
            )
            if download and outcome != "missing":
                path = Path(self.prepare_filename(entry))
                path.parent.mkdir(parents=True, exist_ok=True)
                if outcome == "alternate":
                    (path.parent / "alternate.mp4").write_bytes(b"ig-video")
                else:
                    path.write_bytes(b"ig-video")
            return info

        def prepare_filename(self, info):
            return self.options["outtmpl"].replace("%(title).150B", info["title"]).replace(
                "%(ext)s", info["ext"]
            )

    monkeypatch.setattr(media.yt_dlp, "YoutubeDL", InstagramYDL)
    monkeypatch.setattr(media, "_try_get_instagram_photo_info", lambda _url: None)
    return calls


@pytest.mark.parametrize("outcome", ["single", "playlist", "alternate"])
def test_instagram_video_accepts_single_playlist_or_alternate_file(
    monkeypatch, tmp_path, outcome
):
    calls = _install_instagram_ydl(monkeypatch, outcome=outcome)

    result = media.download_instagram_video(
        INSTAGRAM_URL, "ig-video", output_dir=tmp_path, force_local=True
    )

    assert result.read_bytes() == b"ig-video"
    assert "cookiefile" not in calls[0]


def test_instagram_story_reports_missing_file(monkeypatch, tmp_path):
    _install_instagram_ydl(monkeypatch, outcome="missing")
    monkeypatch.setattr(media, "is_instagram_story_url", lambda _url: True)

    with pytest.raises(Exception, match="Story"):
        media.download_instagram_video(
            INSTAGRAM_URL, "ig-story", output_dir=tmp_path, force_local=True
        )


def test_instagram_private_video_retries_with_cookie(monkeypatch, tmp_path):
    cookie = tmp_path / "ig-cookie.txt"
    cookie.write_text("cookies")
    monkeypatch.setattr(media, "INSTAGRAM_COOKIES_FILE", cookie)
    monkeypatch.setattr(
        media, "_instagram_cookiefile", lambda use: str(cookie) if use else None
    )
    calls = _install_instagram_ydl(monkeypatch, require_cookie=True)

    result = media.download_instagram_video(
        INSTAGRAM_URL, "ig-private", output_dir=tmp_path, force_local=True
    )

    assert result.exists()
    assert len(calls) == 2
    assert calls[1]["cookiefile"] == str(cookie)


def test_instagram_empty_playlist_switches_to_photo_info(monkeypatch):
    _install_instagram_ydl(monkeypatch, outcome="empty")
    photo = {"_nuvio_instagram_photo_post": True, "images": ["photo"]}
    monkeypatch.setattr(media, "_try_get_instagram_photo_info", lambda _url: photo)

    assert media.get_instagram_info(INSTAGRAM_URL) == photo


def test_instagram_private_metadata_retries_with_cookie(monkeypatch, tmp_path):
    cookie = tmp_path / "ig-cookie.txt"
    cookie.write_text("cookies")
    monkeypatch.setattr(media, "INSTAGRAM_COOKIES_FILE", cookie)
    monkeypatch.setattr(
        media, "_instagram_cookiefile", lambda use: str(cookie) if use else None
    )
    calls = _install_instagram_ydl(monkeypatch, require_cookie=True)

    result = media.get_instagram_info(INSTAGRAM_URL)

    assert result["title"] == "ig-entry"
    assert len(calls) == 2
    assert calls[1]["cookiefile"] == str(cookie)


def test_instagram_private_metadata_without_cookie_reports_access(monkeypatch):
    _install_instagram_ydl(monkeypatch, require_cookie=True)

    with pytest.raises(Exception, match="ограничил доступ"):
        media.get_instagram_info(INSTAGRAM_URL)


def _install_tiktok_info_ydl(monkeypatch, *, fail_cookie=False, fail_configs=()):
    calls = []

    class InfoYDL:
        def __init__(self, options):
            self.options = options
            calls.append(options.copy())

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, url, download=False):
            assert url == TIKTOK_URL and not download
            if fail_cookie and "cookiefile" in self.options:
                raise RuntimeError("cookie expired")
            if self.options.get("client") in fail_configs:
                raise RuntimeError("temporary extractor error")
            return {"title": "TikTok media", "formats": [{"format_id": "play"}]}

    monkeypatch.setattr(media, "create_tiktok_ytdl", InfoYDL)
    return calls


def test_tiktok_metadata_tries_next_configuration(monkeypatch):
    monkeypatch.setattr(
        media, "_get_tiktok_base_configs", lambda: [{"client": "broken"}, {"client": "ok"}]
    )
    calls = _install_tiktok_info_ydl(monkeypatch, fail_configs={"broken"})

    result = media.get_tiktok_info(TIKTOK_URL)

    assert result["title"] == "TikTok media"
    assert [call["client"] for call in calls] == ["broken", "ok"]


def test_tiktok_metadata_retries_anonymously_after_bad_cookies(monkeypatch, tmp_path):
    cookie = tmp_path / "tiktok-cookie.txt"
    cookie.write_text("cookies")
    monkeypatch.setattr(media, "TIKTOK_COOKIES_FILE", cookie)
    monkeypatch.setattr(
        media, "_tiktok_cookiefile", lambda use: str(cookie) if use else None
    )
    calls = _install_tiktok_info_ydl(monkeypatch, fail_cookie=True)

    result = media.get_tiktok_info(TIKTOK_URL)

    assert result["title"] == "TikTok media"
    assert calls[0]["cookiefile"] == str(cookie)
    assert "cookiefile" not in calls[1]


def test_tiktok_access_failure_without_cookie_is_explained(monkeypatch):
    class DeniedYDL:
        def __init__(self, _options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download=False):
            raise RuntimeError("login required")

    monkeypatch.setattr(media, "create_tiktok_ytdl", DeniedYDL)

    with pytest.raises(Exception, match="TikTok ограничил доступ"):
        media.get_tiktok_info(TIKTOK_URL)


def test_tiktok_critical_extractor_error_stops_configuration_loop(monkeypatch):
    calls = []

    def fail(_function, **_kwargs):
        calls.append("attempt")
        raise media.CriticalExtractorError("blocked")

    monkeypatch.setattr(media, "_smart_retry", fail)
    monkeypatch.setattr(media, "_get_tiktok_base_configs", lambda: [{}, {}])

    with pytest.raises(media.CriticalExtractorError, match="blocked"):
        media.get_tiktok_info(TIKTOK_URL)

    assert calls == ["attempt"]
