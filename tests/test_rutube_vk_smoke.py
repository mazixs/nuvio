#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke tests for rutube_vk_utils with mocked yt-dlp."""

import functools
import time
from pathlib import Path
from contextlib import contextmanager

import pytest

from utils import rutube_vk_utils, work_budget


class FakeYDL:
    """Minimal yt-dlp stub to avoid real network calls."""

    def __init__(self, options):
        self.options = options
        self._info = {
            "id": "abc123",
            "title": "smoke_video",
            "ext": "mp4",
            "duration": 60,
            "uploader": "tester",
            "formats": [
                {
                    "format_id": "0",
                    "ext": "mp4",
                    "height": 720,
                    "width": 1280,
                    "vcodec": "h264",
                    "acodec": "aac",
                    "filesize": 10_000_000,
                },
                {
                    "format_id": "1",
                    "ext": "m4a",
                    "vcodec": "none",
                    "acodec": "aac",
                    "audio_channels": 2,
                    "filesize": 2_000_000,
                },
            ],
        }

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, url, download=False):
        self.last_url = url
        if download:
            output_path = self._resolve_output_path()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("stub video content")
        return self._info

    def prepare_filename(self, info):
        return str(self._resolve_output_path(info))

    def _resolve_output_path(self, info=None):
        info = info or self._info
        template = self.options.get("outtmpl", "%(title).150B.%(ext)s")
        path_str = template.replace("%(title).150B", info["title"]).replace(
            "%(ext)s", info["ext"]
        )
        return Path(path_str)


def test_is_valid_rutube_url():
    assert rutube_vk_utils.is_valid_rutube_url("https://rutube.ru/video/abc123/")
    assert rutube_vk_utils.is_valid_rutube_url("https://rutube.ru/video/abc123")
    assert rutube_vk_utils.is_valid_rutube_url("https://rutu.be/abc123")
    assert not rutube_vk_utils.is_valid_rutube_url("https://youtube.com/watch?v=abc123")


def test_is_valid_vk_url():
    assert rutube_vk_utils.is_valid_vk_url("https://vk.com/video-12345_67890")
    assert rutube_vk_utils.is_valid_vk_url("https://vkvideo.ru/video-12345_67890")
    assert rutube_vk_utils.is_valid_vk_url("https://vk.com/clip-12345_67890")
    assert rutube_vk_utils.is_valid_vk_url("https://vk.com/wall-12345_67890")
    assert not rutube_vk_utils.is_valid_vk_url("https://youtube.com/watch?v=abc123")


def test_get_rutube_info_smoke_without_network(monkeypatch):
    monkeypatch.setattr(rutube_vk_utils.yt_dlp, "YoutubeDL", FakeYDL)
    info = rutube_vk_utils.get_rutube_info("https://rutube.ru/video/abc123/")
    assert info["title"] == "smoke_video"
    assert info["duration"] == 60


def test_get_vk_info_smoke_without_network(monkeypatch):
    monkeypatch.setattr(rutube_vk_utils.yt_dlp, "YoutubeDL", FakeYDL)
    info = rutube_vk_utils.get_vk_info("https://vk.com/video-12345_67890")
    assert info["title"] == "smoke_video"
    assert info["duration"] == 60


def test_download_rutube_video_smoke_returns_local_file(monkeypatch, tmp_path):
    monkeypatch.setattr(rutube_vk_utils.yt_dlp, "YoutubeDL", FakeYDL)
    result = rutube_vk_utils.download_rutube_video(
        "https://rutube.ru/video/abc123/",
        session_id="smoke",
        output_dir=tmp_path,
        force_local=True,
    )
    assert isinstance(result, Path)
    assert result.exists()
    assert result.read_text() == "stub video content"


def test_download_vk_video_smoke_returns_local_file(monkeypatch, tmp_path):
    monkeypatch.setattr(rutube_vk_utils.yt_dlp, "YoutubeDL", FakeYDL)
    result = rutube_vk_utils.download_vk_video(
        "https://vk.com/video-12345_67890",
        session_id="smoke",
        output_dir=tmp_path,
        force_local=True,
    )
    assert isinstance(result, Path)
    assert result.exists()
    assert result.read_text() == "stub video content"


def test_get_available_formats_rutube_groups_formats(monkeypatch):
    monkeypatch.setattr(rutube_vk_utils.yt_dlp, "YoutubeDL", FakeYDL)
    info = rutube_vk_utils.get_rutube_info("https://rutube.ru/video/abc123/")
    formats = rutube_vk_utils.get_available_formats_rutube(info)
    assert len(formats["combined"]) == 1
    assert len(formats["audio_only"]) == 1
    assert formats["combined"][0]["height"] == 720


def test_get_available_formats_vk_groups_formats(monkeypatch):
    monkeypatch.setattr(rutube_vk_utils.yt_dlp, "YoutubeDL", FakeYDL)
    info = rutube_vk_utils.get_vk_info("https://vk.com/video-12345_67890")
    formats = rutube_vk_utils.get_available_formats_vk(info)
    assert len(formats["combined"]) == 1
    assert len(formats["audio_only"]) == 1


def test_vk_direct_size_uses_range_when_head_is_rejected(monkeypatch):
    class FakeResponse:
        def __init__(self, status_code, headers):
            self.status_code = status_code
            self.headers = headers

    class FakeClient:
        def __init__(self, **kwargs):
            self.range_headers = None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def head(self, url, headers):
            return FakeResponse(405, {})

        @contextmanager
        def stream(self, method, url, headers):
            self.range_headers = headers
            yield FakeResponse(206, {"content-range": "bytes 0-0/30000000"})

    monkeypatch.setattr(rutube_vk_utils.httpx, "Client", FakeClient)
    info = {"formats": [{
        "format_id": "url480", "protocol": "https", "ext": "mp4",
        "url": "https://example.com/video.mp4", "height": 480,
    }]}

    assert rutube_vk_utils._select_vk_direct_format(info) == ("url480", 30_000_000, 480)


def test_vk_long_recording_with_unknown_size_remains_downloadable(monkeypatch):
    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def head(self, url, headers):
            raise rutube_vk_utils.httpx.TimeoutException("timeout")

    monkeypatch.setattr(rutube_vk_utils.httpx, "Client", FakeClient)
    monkeypatch.setattr(rutube_vk_utils, "_get_info", lambda *args, **kwargs: {
        "duration": rutube_vk_utils.MAX_VIDEO_DURATION + 1,
        "formats": [
            {"format_id": "url720", "protocol": "https", "ext": "mp4",
             "url": "https://example.com/720.mp4", "height": 720},
            {"format_id": "url360", "protocol": "https", "ext": "mp4",
             "url": "https://example.com/360.mp4", "height": 360},
        ],
    })

    info = rutube_vk_utils.get_vk_info("https://vk.com/video-1_2")
    assert info["_nuvio_vk_format_id"] == "url360"
    assert info["_nuvio_vk_format_size"] is None


def test_vk_direct_download_stops_at_size_limit(monkeypatch, tmp_path):
    class OversizeYDL(FakeYDL):
        def __init__(self, options):
            super().__init__(options)
            assert options["max_filesize"] == rutube_vk_utils.MAX_FILE_SIZE

        def extract_info(self, url, download=False):
            for hook in self.options["progress_hooks"]:
                hook({"status": "downloading", "downloaded_bytes": rutube_vk_utils.MAX_FILE_SIZE + 1})
            pytest.fail("размер файла не остановил загрузку")

    monkeypatch.setattr(rutube_vk_utils.yt_dlp, "YoutubeDL", OversizeYDL)
    with pytest.raises(rutube_vk_utils.FileSizeLimitError):
        rutube_vk_utils.download_vk_video(
            "https://vk.com/video-1_2", "vk-size-limit", tmp_path,
            force_local=True, format_id="url360",
        )


def test_vk_direct_download_keeps_the_job_limit_outside_the_job(monkeypatch, tmp_path):
    """Поток фрагментов yt-dlp вызывает хуки без контекста задачи."""
    limit = rutube_vk_utils.MAX_FILE_SIZE * 2
    seen = {}

    class CapturingYDL(FakeYDL):
        def __init__(self, options):
            super().__init__(options)
            seen.update(options)

        def extract_info(self, url, download=False):
            raise rutube_vk_utils.FileSizeLimitError("стоп")

    monkeypatch.setattr(rutube_vk_utils.yt_dlp, "YoutubeDL", CapturingYDL)
    download = functools.partial(
        rutube_vk_utils.download_vk_video,
        "https://vk.com/video-1_2", "vk-job-limit", tmp_path,
        force_local=True, format_id="url360",
    )
    with pytest.raises(rutube_vk_utils.FileSizeLimitError):
        work_budget.execute(download, (), time.monotonic() + 30, None, limit)

    assert seen["max_filesize"] == limit
    stop_oversize = seen["progress_hooks"][-1]
    stop_oversize({"status": "downloading", "downloaded_bytes": limit})
    with pytest.raises(rutube_vk_utils.FileSizeLimitError):
        stop_oversize({"status": "downloading", "downloaded_bytes": limit + 1})
