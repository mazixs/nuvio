"""Проверки доставки YouTube-аудио и переходов между источниками."""

import functools
import time
from pathlib import Path

import pytest

from utils import work_budget, youtube_utils


URL = "https://www.youtube.com/watch?v=abc123def45"


class _AudioYDL:
    calls: list[dict] = []
    fail_requested = False
    fail_anonymous = False
    fail_all = False

    def __init__(self, options):
        self.options = options
        self.calls.append(options.copy())

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, url, download=False):
        assert url == URL and download
        if self.fail_all or (self.fail_anonymous and "cookiefile" not in self.options):
            raise youtube_utils.yt_dlp.utils.DownloadError("HTTP 403")
        if self.fail_requested and not self.options["format"].startswith("bestaudio"):
            raise youtube_utils.yt_dlp.utils.DownloadError(
                "Requested format is not available"
            )
        output = Path(self.prepare_filename({"title": "track", "ext": "m4a"}))
        output.parent.mkdir(parents=True, exist_ok=True)
        if "postprocessors" in self.options:
            output = output.with_suffix(
                "." + self.options["postprocessors"][0]["preferredcodec"]
            )
        output.write_bytes(b"audio")
        return {"title": "track", "ext": "m4a", "format_id": self.options["format"]}

    def prepare_filename(self, info):
        return self.options["outtmpl"].replace("%(title).150B", info["title"]).replace(
            "%(ext)s", info["ext"]
        )


@pytest.fixture(autouse=True)
def _setup_audio(monkeypatch):
    _AudioYDL.calls = []
    _AudioYDL.fail_requested = False
    _AudioYDL.fail_anonymous = False
    _AudioYDL.fail_all = False
    monkeypatch.setattr(youtube_utils.yt_dlp, "YoutubeDL", _AudioYDL)
    monkeypatch.setattr(youtube_utils, "execute_with_backoff", lambda _label, f: f())
    monkeypatch.setattr(youtube_utils, "_cookiefile_if_available", lambda use: None)
    monkeypatch.setattr(youtube_utils, "YTDLP_CLI_FALLBACK", False)


@pytest.mark.parametrize(
    ("download", "suffix"),
    [("download_audio_native", ".m4a"), ("download_audio", ".mp3")],
)
def test_requested_audio_is_saved_with_selected_format(tmp_path, download, suffix):
    result = getattr(youtube_utils, download)(
        URL, "140-1", "audio-ok", output_dir=tmp_path, force_local=True
    )

    assert result == tmp_path / f"track{suffix}"
    assert result.read_bytes() == b"audio"
    assert _AudioYDL.calls[0]["format"] == "140-1"
    assert "cookiefile" not in _AudioYDL.calls[0]


@pytest.mark.parametrize("download", ["download_audio_native", "download_audio"])
def test_missing_selected_audio_uses_russian_generic_fallback(tmp_path, download):
    _AudioYDL.fail_requested = True

    result = getattr(youtube_utils, download)(
        URL, "140-1", "audio-fallback", output_dir=tmp_path, force_local=True
    )

    assert result.exists()
    assert [call["format"] for call in _AudioYDL.calls] == [
        "140-1",
        "bestaudio[language^=ru]/bestaudio",
    ]


@pytest.mark.parametrize("download", ["download_audio_native", "download_audio"])
def test_anonymous_audio_failure_retries_with_cookie_file(
    monkeypatch, tmp_path, download
):
    _AudioYDL.fail_anonymous = True
    cookie = tmp_path / "cookies.txt"
    cookie.write_text("cookies")
    monkeypatch.setattr(youtube_utils, "YOUTUBE_COOKIES_FILE", str(cookie))
    monkeypatch.setattr(
        youtube_utils, "_cookiefile_if_available", lambda use: str(cookie) if use else None
    )

    result = getattr(youtube_utils, download)(
        URL, "140-1", "audio-cookie", output_dir=tmp_path, force_local=True
    )

    assert result.exists()
    assert len(_AudioYDL.calls) == 2
    assert "cookiefile" not in _AudioYDL.calls[0]
    assert _AudioYDL.calls[1]["cookiefile"] == str(cookie)


@pytest.mark.parametrize("download", ["download_audio_native", "download_audio"])
def test_api_failure_retries_cli_without_cookies(monkeypatch, tmp_path, download):
    _AudioYDL.fail_all = True
    monkeypatch.setattr(youtube_utils, "YTDLP_CLI_FALLBACK", True)
    monkeypatch.setattr(youtube_utils, "YOUTUBE_COOKIES_FILE", "")
    calls = []
    expected = tmp_path / "cli.m4a"
    expected.write_bytes(b"fallback")

    def cli(**kwargs):
        calls.append(kwargs)
        return expected

    monkeypatch.setattr(youtube_utils, "_download_with_cli_fallback", cli)

    result = getattr(youtube_utils, download)(
        URL, "140-1", "audio-cli", output_dir=tmp_path, force_local=True
    )

    assert result == expected
    assert len(calls) == 1
    assert calls[0]["format_selector"] == "140-1"
    assert calls[0]["use_cookies"] is False


@pytest.mark.parametrize("download", ["download_audio_native", "download_audio"])
def test_access_denial_does_not_use_cli(monkeypatch, tmp_path, download):
    _AudioYDL.fail_all = True
    monkeypatch.setattr(youtube_utils, "YTDLP_CLI_FALLBACK", True)
    monkeypatch.setattr(youtube_utils, "YOUTUBE_COOKIES_FILE", "")
    monkeypatch.setattr(
        youtube_utils, "classify_download_error_kind", lambda _message: "ACCESS_RESTRICTED"
    )
    monkeypatch.setattr(
        youtube_utils,
        "_download_with_cli_fallback",
        lambda **_kwargs: pytest.fail("CLI не должен обходить отказ в доступе"),
    )

    with pytest.raises(youtube_utils.yt_dlp.utils.DownloadError, match="403"):
        getattr(youtube_utils, download)(
            URL, "140-1", "audio-denied", output_dir=tmp_path, force_local=True
        )


def _in_job(download, limit):
    return work_budget.execute(download, (), time.monotonic() + 30, None, limit)


def test_nonlocal_audio_selector_applies_the_job_limit(tmp_path):
    limit = youtube_utils.MAX_FILE_SIZE * 2
    _in_job(functools.partial(
        youtube_utils.download_audio_native, URL, "140-1", "audio-job-limit", output_dir=tmp_path,
    ), limit)

    assert f"filesize<=?{limit}" in _AudioYDL.calls[0]["format"]


def test_nonlocal_audio_selector_applies_size_limit(tmp_path):
    youtube_utils.download_audio_native(URL, "140-1", "audio-limit", output_dir=tmp_path)

    assert "filesize<=?" in _AudioYDL.calls[0]["format"]


class _VideoYDL:
    calls: list[dict] = []
    failure: str | None = None

    def __init__(self, options):
        self.options = options
        self.calls.append(options.copy())

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, url, download=False):
        assert url == URL and download
        if self.failure == "all":
            raise youtube_utils.yt_dlp.utils.DownloadError("HTTP 403")
        if self.failure == "cookie" and "cookiefile" not in self.options:
            raise youtube_utils.yt_dlp.utils.DownloadError("private video")
        if self.options["format"] == "137+140" and self.failure in {"403", "format"}:
            reason = (
                "HTTP 403" if self.failure == "403" else "Requested format is not available"
            )
            raise youtube_utils.yt_dlp.utils.DownloadError(reason)
        info = {"title": "video", "ext": "mp4", "format_id": self.options["format"]}
        path = Path(self.prepare_filename(info))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")
        return info

    def prepare_filename(self, info):
        return self.options["outtmpl"].replace("%(title).150B", info["title"]).replace(
            "%(ext)s", info["ext"]
        )


@pytest.fixture
def _setup_video(monkeypatch):
    _VideoYDL.calls = []
    _VideoYDL.failure = None
    monkeypatch.setattr(youtube_utils.yt_dlp, "YoutubeDL", _VideoYDL)
    monkeypatch.setattr(youtube_utils, "_ensure_ios_compatible", lambda path, _sid: path)


@pytest.mark.parametrize(
    ("failure", "expected_part"),
    [("403", "protocol!=m3u8_dash"), ("format", "bestaudio[language^=ru]")],
)
def test_youtube_video_recovers_with_explicit_safe_selector(
    tmp_path, _setup_video, failure, expected_part
):
    _VideoYDL.failure = failure

    result = youtube_utils.download_video(
        URL, "137+140", "video-fallback", output_dir=tmp_path, force_local=True
    )

    assert result.read_bytes() == b"video"
    assert len(_VideoYDL.calls) == 2
    assert expected_part in _VideoYDL.calls[1]["format"]


def test_youtube_video_retries_with_cookies_after_anonymous_failure(
    monkeypatch, tmp_path, _setup_video
):
    cookie = tmp_path / "cookies.txt"
    cookie.write_text("cookies")
    _VideoYDL.failure = "cookie"
    monkeypatch.setattr(youtube_utils, "YOUTUBE_COOKIES_FILE", str(cookie))
    monkeypatch.setattr(
        youtube_utils, "_cookiefile_if_available", lambda use: str(cookie) if use else None
    )

    result = youtube_utils.download_video(
        URL, "137+140", "video-cookie", output_dir=tmp_path, force_local=True
    )

    assert result.exists()
    assert len(_VideoYDL.calls) == 2
    assert "cookiefile" not in _VideoYDL.calls[0]
    assert _VideoYDL.calls[1]["cookiefile"] == str(cookie)


def test_youtube_video_selector_applies_the_job_limit(tmp_path, _setup_video):
    limit = youtube_utils.MAX_FILE_SIZE * 2
    _in_job(functools.partial(
        youtube_utils.download_video, URL, "137", "video-job-limit", output_dir=tmp_path,
    ), limit)

    assert f"filesize<=?{limit}" in _VideoYDL.calls[0]["format"]


def test_youtube_video_passes_canary_size_budget(tmp_path, _setup_video):
    result = youtube_utils.download_video(
        URL,
        "137+140",
        "video-budget",
        output_dir=tmp_path,
        force_local=True,
        max_file_size=50_000_000,
    )

    assert result.exists()
    assert _VideoYDL.calls[0]["max_filesize"] == 50_000_000


def test_youtube_video_uses_cli_after_api_failure(monkeypatch, tmp_path, _setup_video):
    _VideoYDL.failure = "all"
    monkeypatch.setattr(youtube_utils, "YTDLP_CLI_FALLBACK", True)
    monkeypatch.setattr(youtube_utils, "YOUTUBE_COOKIES_FILE", "")
    expected = tmp_path / "cli.mp4"
    expected.write_bytes(b"cli")
    calls = []

    def cli(**kwargs):
        calls.append(kwargs)
        return expected

    monkeypatch.setattr(youtube_utils, "_download_with_cli_fallback", cli)

    assert youtube_utils.download_video(
        URL, "137+140", "video-cli", output_dir=tmp_path, force_local=True
    ) == expected
    assert len(calls) == 1
    assert calls[0]["merge_output_format"] == "mp4"
    assert calls[0]["use_cookies"] is False
