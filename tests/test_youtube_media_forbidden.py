"""Поведение на 403, который приходит на сам медиафайл, а не на видео.

Ссылка `googlevideo` подписана на исходящий IP (`ip=` внутри `sparams`) и живёт
считанные минуты. Если маршрут до Google меняется между разбором ссылки и
скачиванием, CDN отвечает 403 на первом же байте. Это временная ошибка: помогает
повторный разбор ссылки, а не отказ пользователю.
"""

import logging

import pytest
import yt_dlp

from utils.public_errors import is_media_forbidden_error, youtube_error_code
from utils.ytdlp_common import classify_download_error_kind, execute_with_backoff

MEDIA_403 = "ERROR: unable to download video data: HTTP Error 403: Forbidden"
RESTRICTED = "ERROR: Private video. Sign in if you've been granted access"


def test_media_403_separated_from_access_restriction():
    assert classify_download_error_kind(MEDIA_403) == "MEDIA_FORBIDDEN"
    assert classify_download_error_kind(RESTRICTED) == "ACCESS_RESTRICTED"


def test_fragment_403_is_also_transient():
    assert is_media_forbidden_error(
        "ERROR: fragment 3 not found: HTTP Error 403: Forbidden"
    )


def test_bare_403_stays_access_restriction():
    """Без указания на медиафайл 403 трактуется по-прежнему строго."""
    assert not is_media_forbidden_error("HTTP Error 403: Forbidden")
    assert (
        classify_download_error_kind("HTTP Error 403: Forbidden")
        == "ACCESS_RESTRICTED"
    )


def test_cli_fallback_gate_stays_open_for_media_403():
    """Гейт CLI-fallback в youtube_utils закрыт только для ACCESS_RESTRICTED."""
    assert classify_download_error_kind(MEDIA_403) != "ACCESS_RESTRICTED"


def test_public_classifier_agrees_with_downloader():
    assert youtube_error_code(MEDIA_403) == "MEDIA_FORBIDDEN"
    assert youtube_error_code(RESTRICTED) == "ACCESS_RESTRICTED"


def test_execute_with_backoff_retries_media_403(monkeypatch):
    monkeypatch.setattr("utils.ytdlp_common.time.sleep", lambda _: None)
    attempts: list[int] = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise yt_dlp.utils.DownloadError(MEDIA_403)
        return "ok"

    assert execute_with_backoff("проба", flaky) == "ok"
    assert len(attempts) == 3


def test_execute_with_backoff_keeps_access_restriction_fatal(monkeypatch):
    monkeypatch.setattr("utils.ytdlp_common.time.sleep", lambda _: None)
    attempts: list[int] = []

    def restricted():
        attempts.append(1)
        raise yt_dlp.utils.DownloadError(RESTRICTED)

    with pytest.raises(yt_dlp.utils.DownloadError):
        execute_with_backoff("проба", restricted)
    assert len(attempts) == 1


@pytest.mark.parametrize(
    ("error", "category"),
    [
        ("Sign in to confirm you’re not a bot", "ACCESS_RESTRICTED"),
        ("HTTP Error 429: Too Many Requests", "RATE_LIMIT"),
    ],
)
def test_youtube_access_failures_are_expected_without_retry_or_traceback(
    monkeypatch, caplog, error, category
):
    monkeypatch.setattr("utils.ytdlp_common.time.sleep", lambda _: None)
    attempts = []

    def rejected():
        attempts.append(1)
        raise yt_dlp.utils.DownloadError(error)

    assert classify_download_error_kind(error) == category
    assert youtube_error_code(error) == category
    with caplog.at_level(logging.WARNING, logger="utils.ytdlp_common"):
        with pytest.raises(yt_dlp.utils.DownloadError):
            execute_with_backoff("проба", rejected)

    assert len(attempts) == 1
    assert caplog.records[-1].levelno == logging.WARNING
    assert caplog.records[-1].exc_info is None


def test_media_403_reaches_admins():
    """Серия таких отказов — единственный признак смены правил у платформы."""
    from utils.telegram_utils import _should_notify_admins_platform_failure

    assert _should_notify_admins_platform_failure(
        "youtube", "MEDIA_FORBIDDEN", "format_download"
    )
    assert not _should_notify_admins_platform_failure(
        "youtube", "NETWORK_TIMEOUT", "format_download"
    )
