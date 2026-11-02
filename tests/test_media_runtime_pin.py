"""Проверка чтения версии yt-dlp из lock-файла образа."""

import pytest

from scripts.check_media_runtime import _canonical_dev_version, _locked_ytdlp_version


@pytest.mark.unit
@pytest.mark.parametrize(
    "line",
    [
        "yt-dlp==2026.9.16.232951.dev0\n",
        "yt-dlp[default]==2026.9.16.232951.dev0\n",
    ],
)
def test_locked_ytdlp_version_accepts_lock_formats(line):
    assert _locked_ytdlp_version(line) == "2026.9.16.232951.dev0"


@pytest.mark.unit
@pytest.mark.parametrize(
    "lock_text",
    [
        "httpx==0.28.1\n",
        "yt-dlp==1.0\nyt-dlp[default]==2.0\n",
    ],
)
def test_locked_ytdlp_version_rejects_missing_or_duplicate_pin(lock_text):
    with pytest.raises(RuntimeError, match="ровно один pin"):
        _locked_ytdlp_version(lock_text)


@pytest.mark.unit
@pytest.mark.parametrize(
    "installed",
    [
        "2026.10.29.232701dev",
        "2026.10.29.232701.dev",
        "2026.10.29.232701dev0",
        "2026.10.29.232701.dev0",
    ],
)
def test_canonical_dev_version_treats_pep440_spellings_as_equal(installed):
    assert _canonical_dev_version(installed) == "2026.10.29.232701.dev0"


@pytest.mark.unit
@pytest.mark.parametrize(
    "installed",
    [
        "2026.10.29.232700dev",
        "2026.10.29.232701.dev1",
        "2026.10.29.232701",
        "отсутствует",
    ],
)
def test_canonical_dev_version_keeps_real_mismatch(installed):
    assert _canonical_dev_version(installed) != "2026.10.29.232701.dev0"
