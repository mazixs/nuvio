"""Лимит размера файла HTTP API: его передаёт задача, а лимит бота остаётся прежним."""

import ast
import time
from pathlib import Path

import pytest

from utils import (
    media_fetch,
    rutube_vk_utils,
    tiktok_instagram_utils,
    work_budget,
    youtube_utils,
    ytdlp_common,
)

ROOT = Path(__file__).resolve().parents[1]
YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
BOT_LIMIT = 100
JOB_LIMIT = 1000


def in_job(function, *args):
    return work_budget.execute(function, args, time.monotonic() + 30, None, JOB_LIMIT)


@pytest.fixture(autouse=True)
def bot_limit(monkeypatch):
    for module in (media_fetch, rutube_vk_utils, tiktok_instagram_utils, youtube_utils, ytdlp_common):
        monkeypatch.setattr(module, "MAX_FILE_SIZE", BOT_LIMIT)


@pytest.mark.unit
def test_limit_applies_only_inside_the_job():
    assert in_job(work_budget.size_limit, BOT_LIMIT) == JOB_LIMIT
    assert work_budget.size_limit(BOT_LIMIT) == BOT_LIMIT


@pytest.mark.unit
def test_bot_work_keeps_the_bot_limit():
    """`run_blocking` бота лимит не передаёт."""
    limit = work_budget.execute(work_budget.size_limit, (BOT_LIMIT,), time.monotonic() + 30, None)
    assert limit == BOT_LIMIT


@pytest.mark.unit
def test_limit_does_not_outlive_a_failed_job():
    def fail():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        in_job(fail)
    assert work_budget.size_limit(BOT_LIMIT) == BOT_LIMIT


@pytest.mark.unit
def test_finished_file_is_checked_against_the_job_limit(tmp_path):
    media = tmp_path / "video.mp4"
    media.write_bytes(b"x" * (BOT_LIMIT + 1))

    assert in_job(ytdlp_common.finalize_downloaded_file, media, False) == media
    with pytest.raises(ytdlp_common.FileSizeLimitError):
        ytdlp_common.finalize_downloaded_file(media, False)


@pytest.mark.unit
@pytest.mark.parametrize(
    "get_formats",
    [youtube_utils.get_available_formats, rutube_vk_utils._get_available_formats],
)
def test_format_filter_uses_the_job_limit(get_formats):
    info = {"formats": [{
        "format_id": "137", "ext": "mp4", "height": 1080,
        "vcodec": "avc1", "acodec": "none", "filesize": BOT_LIMIT + 1,
    }]}

    assert in_job(get_formats, info)["video_only"]
    assert not get_formats(info)["video_only"]


@pytest.mark.unit
def test_video_choice_uses_the_job_limit(monkeypatch):
    limits = []
    monkeypatch.setattr(youtube_utils, "get_video_info", lambda url, session_id=None: {})
    monkeypatch.setattr(youtube_utils, "get_available_formats", lambda info: {"combined": [{"format_id": "22"}]})
    monkeypatch.setattr(media_fetch, "select_tg_video_format", lambda video, audio, combined, limit: limits.append(limit))

    in_job(media_fetch.prepare_download, YOUTUBE_URL, "youtube", "video", "api-limit")

    assert limits == [JOB_LIMIT]


class _RemoteFile:
    headers = {"content-length": str(BOT_LIMIT + 1)}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def raise_for_status(self):
        pass

    def iter_bytes(self):
        yield b"x" * (BOT_LIMIT + 1)


@pytest.mark.unit
def test_fast_path_download_uses_the_job_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(tiktok_instagram_utils.httpx, "stream", lambda *_, **__: _RemoteFile())
    target = tmp_path / "video.mp4"
    download = tiktok_instagram_utils._download_remote_file

    assert in_job(download, "https://example.com/video", target) == target
    with pytest.raises(ValueError, match="превышает лимит"):
        download("https://example.com/video", target)


@pytest.mark.unit
@pytest.mark.parametrize(
    "module",
    [
        "utils/media_fetch.py",
        "utils/media_processor.py",
        "utils/rutube_vk_utils.py",
        "utils/tiktok_instagram_utils.py",
        "utils/youtube_utils.py",
        "utils/ytdlp_common.py",
    ],
)
def test_shared_modules_read_the_limit_through_the_job(module):
    """Голая константа молча вернула бы задаче API лимит Telegram: 50 МБ при прямом запуске."""
    tree = ast.parse((ROOT / module).read_text(encoding="utf-8"))
    through_the_job = {
        id(node.args[0])
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "size_limit"
    }
    bare_reads = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id == "MAX_FILE_SIZE"
        and id(node) not in through_the_job
    ]
    assert bare_reads == []
