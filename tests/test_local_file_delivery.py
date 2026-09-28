"""Тесты отправки файлов через локальный Telegram Bot API."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import telegram

from utils import telegram_utils, ytdlp_common


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.unit
def test_finalize_returns_local_path_below_limit(monkeypatch, tmp_path):
    media = tmp_path / "video.mp4"
    media.write_bytes(b"x" * 10)
    monkeypatch.setattr(ytdlp_common, "MAX_FILE_SIZE", 20)

    assert ytdlp_common.finalize_downloaded_file(media, False) == media


@pytest.mark.unit
def test_finalize_deletes_and_rejects_file_above_limit(monkeypatch, tmp_path):
    media = tmp_path / "video.mp4"
    media.write_bytes(b"x" * 21)
    monkeypatch.setattr(ytdlp_common, "MAX_FILE_SIZE", 20)

    with pytest.raises(ytdlp_common.FileSizeLimitError):
        ytdlp_common.finalize_downloaded_file(media, False)

    assert not media.exists()


@pytest.mark.unit
def test_send_single_file_passes_absolute_path_to_local_api(monkeypatch, tmp_path):
    media = tmp_path / "video.mp4"
    media.write_bytes(b"video")
    sent_message = SimpleNamespace(video=None, audio=None, document=None)
    reply_video = AsyncMock(return_value=sent_message)
    query = SimpleNamespace(
        message=SimpleNamespace(reply_video=reply_video),
        edit_message_text=AsyncMock(),
    )
    monkeypatch.setattr(
        telegram_utils, "TELEGRAM_LOCAL_MODE", True, raising=False
    )

    result = asyncio.run(
        telegram_utils.send_single_file(
            query,
            media,
            "session-token",
            {"platform": "tiktok", "url": "https://example.test/video"},
            max_retries=1,
        )
    )

    assert result is True
    assert reply_video.await_args.kwargs["video"] == media.resolve()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("reason", "category"),
    [("file is too big", "LARGE"), ("chat not found", "API")],
)
def test_bad_request_has_the_same_category_in_user_code_and_failure_log(
    monkeypatch, tmp_path, reason, category
):
    media = tmp_path / "photo.jpg"
    media.write_bytes(b"photo")
    query = SimpleNamespace(
        message=SimpleNamespace(
            reply_document=AsyncMock(side_effect=telegram.error.BadRequest(reason))
        ),
        edit_message_text=AsyncMock(),
    )
    failures = []
    monkeypatch.setattr(telegram_utils, "TELEGRAM_LOCAL_MODE", True)
    monkeypatch.setattr(
        telegram_utils,
        "_schedule_platform_failure_log",
        lambda **kwargs: failures.append(kwargs),
    )

    result = asyncio.run(
        telegram_utils.send_single_file(
            query,
            media,
            "session-token",
            {"platform": "tiktok", "url": "https://example.test/post"},
            max_retries=1,
        )
    )

    assert result is False
    assert len(failures) == 1
    assert failures[0]["stage"] == "send_single_file_bad_request"
    assert failures[0]["error_code"].startswith(f"TG-{category}-")
    assert failures[0]["error_code"] in query.edit_message_text.await_args.args[0]


@pytest.mark.unit
def test_runtime_has_no_gokapi_dependency():
    runtime_files = [
        PROJECT_ROOT / "config.py",
        PROJECT_ROOT / "messages.py",
        *(PROJECT_ROOT / "utils").glob("*.py"),
    ]

    assert not (PROJECT_ROOT / "utils" / "gokapi_utils.py").exists()
    for runtime_file in runtime_files:
        assert "gokapi" not in runtime_file.read_text(encoding="utf-8").lower(), (
            f"Осталась зависимость Gokapi: {runtime_file}"
        )
