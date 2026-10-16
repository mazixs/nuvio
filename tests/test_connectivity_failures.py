"""Недоступная сеть сервера: верная категория и повторы только внутри yt-dlp.

15.10.2026 у провайдера несколько часов не работал DNS. Ошибка yt-dlp
`Failed to resolve … ([Errno -5] No address associated with hostname)` считалась
`DOWNLOAD`, и каждая загрузка проходила режим без cookies, режим с cookies и
CLI-комбинации, упираясь в тот же резолвер. Сети здесь нет: yt-dlp и CLI
подменяются.
"""

import errno
import socket

import pytest
import yt_dlp

from utils import youtube_utils
from utils.public_errors import (
    classify_internal_error_category,
    is_connectivity_failure,
    youtube_error_code,
)
from utils.ytdlp_common import classify_download_error_kind, execute_with_backoff

pytestmark = pytest.mark.unit

URL = "https://www.youtube.com/watch?v=abc123def45"

# Текст из лога прода; имя узла CDN заменено.
DNS_FAILURE = (
    "ERROR: [download] Got error: HTTPSConnection(host='rr1---sn-test.googlevideo.com', "
    "port=443): Failed to resolve 'rr1---sn-test.googlevideo.com' ([Errno -5] No address "
    "associated with hostname). Giving up after 5 retries"
)
DNS_FRAGMENT_FAILURE = (
    "ERROR: fragment 3 not found, unable to continue: Failed to resolve "
    "'rr1---sn-test.googlevideo.com' ([Errno -3] Temporary failure in name resolution)"
)
READ_TIMEOUT = (
    "ERROR: [youtube] abc123def45: Unable to download API page: "
    "HTTPSConnectionPool(host='www.youtube.com', port=443): Read timed out. "
    "(read timeout=40.0)"
)
CONNECTION_RESET = "ERROR: [download] Got error: Connection reset by peer"


class _Transport(Exception):
    """Как `yt_dlp.networking.exceptions.TransportError`: причина лежит в `.cause`."""

    def __init__(self, cause: BaseException):
        super().__init__("HTTPSConnection(host='media.invalid', port=443)")
        self.cause = cause


def _chained_dns_error() -> Exception:
    """DownloadError, у которого DNS-причина есть только в цепочке, но не в тексте.

    Форма замерена на закреплённой версии yt-dlp: `DownloadError.exc_info[1]` →
    `TransportError.cause` → `NameResolutionError.__cause__` → `socket.gaierror`.
    """
    resolution = Exception("resolver wrapper")
    resolution.__cause__ = socket.gaierror(socket.EAI_NONAME, "lookup failed")
    transport = _Transport(resolution)
    error = yt_dlp.utils.DownloadError("ERROR: [generic] video: Unable to download webpage")
    error.exc_info = (type(transport), transport, None)
    return error


def test_production_dns_failure_is_network_not_download():
    assert is_connectivity_failure(DNS_FAILURE)
    assert youtube_error_code(DNS_FAILURE) == "NETWORK"
    assert classify_download_error_kind(DNS_FAILURE) == "NETWORK"
    assert classify_internal_error_category("rutube", DNS_FAILURE) == "NETWORK"


def test_dns_failure_is_found_in_exception_chain_not_only_in_text():
    error = _chained_dns_error()

    assert not is_connectivity_failure(str(error))
    assert is_connectivity_failure(error)
    assert youtube_error_code(error) == "NETWORK"
    assert classify_internal_error_category("vk", error) == "NETWORK"


@pytest.mark.parametrize(
    "error",
    [
        socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution"),
        ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"),
        OSError(errno.ENETUNREACH, "Network is unreachable"),
        OSError(errno.EHOSTUNREACH, "No route to host"),
    ],
)
def test_bare_connectivity_errors_are_network(error):
    """Голый `gaierror` раньше получал `IO`: его `errno` отрицательный."""
    assert is_connectivity_failure(error)
    assert youtube_error_code(error) == "NETWORK"
    assert classify_internal_error_category("tiktok", error) == "NETWORK"


@pytest.mark.parametrize(
    ("message", "category"),
    [
        (READ_TIMEOUT, "NETWORK_TIMEOUT"),
        ("ERROR: unable to download video data: HTTP Error 403: Forbidden", "MEDIA_FORBIDDEN"),
        ("ERROR: Private video. Sign in if you've been granted access", "ACCESS_RESTRICTED"),
        (CONNECTION_RESET, "NETWORK"),
    ],
)
def test_platform_and_transient_failures_are_not_connectivity(message, category):
    """Соединение было: такие отказы идут по прежнему пути повторов."""
    assert not is_connectivity_failure(message)
    assert youtube_error_code(message) == category


def test_video_title_in_file_path_is_not_a_network_failure():
    """Путь к файлу несёт название ролика; у `OSError` ответ даёт тип, а не текст."""
    missing = FileNotFoundError(
        "Скачивание завершилось, но итоговый файл не найден: "
        "/app/media/session/Connection Refused - No Route to Host.mp4"
    )

    assert not is_connectivity_failure(missing)
    assert youtube_error_code(missing) == "FILE"


def test_cli_output_lines_do_not_count_only_error_lines_do():
    title_in_stdout = RuntimeError(
        "CLI fallback yt-dlp завершился ошибкой: "
        "[download] Destination: /app/media/session/Connection refused.mp4\n"
        "ERROR: Postprocessing: Conversion failed!"
    )
    dns_in_stderr = RuntimeError(
        "CLI fallback yt-dlp завершился ошибкой: WARNING: [youtube] abc123def45: retrying\n"
        f"{DNS_FAILURE}"
    )

    assert not is_connectivity_failure(title_in_stdout)
    assert is_connectivity_failure(dns_in_stderr)


def test_suppressed_context_does_not_turn_access_error_into_network():
    try:
        try:
            raise ConnectionRefusedError(errno.ECONNREFUSED, "proxy refused")
        except ConnectionRefusedError:
            raise yt_dlp.utils.DownloadError(
                "ERROR: Private video. Sign in if you've been granted access"
            ) from None
    except yt_dlp.utils.DownloadError as error:
        assert not is_connectivity_failure(error)
        assert youtube_error_code(error) == "ACCESS_RESTRICTED"


def test_backoff_does_not_retry_unreachable_network(monkeypatch):
    """Страховка вместе с тестом категории выше: на `main` DNS-сбой был `DOWNLOAD`
    и тоже не повторялся, но после переименования в `NETWORK` попал бы под
    повторы `execute_with_backoff`, если бы не отдельная проверка."""
    pauses: list[float] = []
    monkeypatch.setattr("utils.ytdlp_common.work_budget.pause", pauses.append)
    attempts: list[int] = []

    def offline():
        attempts.append(1)
        raise yt_dlp.utils.DownloadError(DNS_FAILURE)

    with pytest.raises(yt_dlp.utils.DownloadError):
        execute_with_backoff("проба", offline)

    assert attempts == [1]
    assert pauses == []


def test_backoff_retries_transient_transport_errors_with_full_jitter(monkeypatch):
    pauses: list[float] = []
    bounds: list[tuple[float, float]] = []
    monkeypatch.setattr("utils.ytdlp_common.work_budget.pause", pauses.append)

    def uniform(low, high):
        bounds.append((low, high))
        return high / 2

    monkeypatch.setattr("utils.ytdlp_common.random.uniform", uniform)
    attempts: list[int] = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise yt_dlp.utils.DownloadError(CONNECTION_RESET)
        return "ok"

    assert execute_with_backoff("проба", flaky) == "ok"
    assert bounds == [(0, 2), (0, 4)]
    assert pauses == [1.0, 2.0]


class _FailingYDL:
    calls: list[dict] = []
    error = DNS_FAILURE

    def __init__(self, options):
        self.options = options
        type(self).calls.append(options.copy())

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, url, download=False):
        raise yt_dlp.utils.DownloadError(type(self).error)


@pytest.fixture
def offline_youtube(monkeypatch, tmp_path):
    """YouTube не отвечает; cookies на месте, CLI включён и тоже без сети."""
    _FailingYDL.calls = []
    _FailingYDL.error = DNS_FAILURE
    cookie = tmp_path / "cookies.txt"
    cookie.write_text("cookies")
    cli = {"calls": [], "outcome": RuntimeError(f"CLI fallback yt-dlp завершился ошибкой: {DNS_FAILURE}")}

    def fake_cli(**kwargs):
        cli["calls"].append(kwargs)
        if isinstance(cli["outcome"], BaseException):
            raise cli["outcome"]
        return cli["outcome"]

    monkeypatch.setattr(youtube_utils.yt_dlp, "YoutubeDL", _FailingYDL)
    monkeypatch.setattr(youtube_utils, "YOUTUBE_COOKIES_FILE", str(cookie))
    monkeypatch.setattr(
        youtube_utils, "_cookiefile_if_available", lambda use: str(cookie) if use else None
    )
    monkeypatch.setattr(youtube_utils, "YTDLP_CLI_FALLBACK", True)
    monkeypatch.setattr(youtube_utils, "_download_with_cli_fallback", fake_cli)
    monkeypatch.setattr("utils.ytdlp_common.work_budget.pause", lambda _seconds: None)
    return cli


@pytest.mark.parametrize("download", ["download_video", "download_audio_native", "download_audio"])
def test_unreachable_network_skips_cookies_and_cli(offline_youtube, tmp_path, download):
    with pytest.raises(yt_dlp.utils.DownloadError, match="Failed to resolve"):
        getattr(youtube_utils, download)(
            URL, "137+140", "offline", output_dir=tmp_path, force_local=True
        )

    assert len(_FailingYDL.calls) == 1
    assert "cookiefile" not in _FailingYDL.calls[0]
    assert offline_youtube["calls"] == []


def test_dns_failure_between_fragments_skips_non_hls_selector(offline_youtube, tmp_path):
    """Слово «fragment» не должно запускать повтор с другим селектором."""
    _FailingYDL.error = DNS_FRAGMENT_FAILURE

    with pytest.raises(yt_dlp.utils.DownloadError):
        youtube_utils.download_video(
            URL, "137+140", "offline-fragment", output_dir=tmp_path, force_local=True
        )

    assert len(_FailingYDL.calls) == 1


def test_video_info_does_not_retry_with_cookies_when_offline(offline_youtube):
    with pytest.raises(yt_dlp.utils.DownloadError):
        youtube_utils.get_video_info(URL, session_id="offline-info")

    assert len(_FailingYDL.calls) == 1
    assert "cookiefile" not in _FailingYDL.calls[0]


def test_subtitles_do_not_retry_with_cookies_when_offline(offline_youtube, tmp_path):
    with pytest.raises(yt_dlp.utils.DownloadError):
        youtube_utils.download_subtitles(URL, "offline-subs", output_dir=tmp_path)

    assert len(_FailingYDL.calls) == 1


@pytest.mark.parametrize("download", ["download_video", "download_audio_native", "download_audio"])
def test_cli_stops_after_first_network_failure(offline_youtube, tmp_path, download):
    """Если сеть пропала посреди CLI-комбинаций, остальные не запускаются.

    Пользователь видит сетевую причину, а прежняя ошибка встроенного API
    остаётся в `__cause__` для диагностики.
    """
    _FailingYDL.error = "ERROR: something unexpected happened"

    with pytest.raises(RuntimeError, match="Failed to resolve") as failure:
        getattr(youtube_utils, download)(
            URL, "137+140", "offline-cli", output_dir=tmp_path, force_local=True
        )

    assert isinstance(failure.value.__cause__, yt_dlp.utils.DownloadError)
    assert "unexpected" in str(failure.value.__cause__)
    assert youtube_error_code(failure.value) == "NETWORK"
    assert len(_FailingYDL.calls) == 2
    assert len(offline_youtube["calls"]) == 1


def test_timeout_still_uses_cookies_and_cli(offline_youtube, tmp_path):
    """Таймаут — не отказ сети: соединение было, путь восстановления прежний."""
    _FailingYDL.error = READ_TIMEOUT
    expected = tmp_path / "cli.mp4"
    expected.write_bytes(b"cli")
    offline_youtube["outcome"] = expected

    assert youtube_utils.download_video(
        URL, "137+140", "timeout-cli", output_dir=tmp_path, force_local=True
    ) == expected
    assert any("cookiefile" in call for call in _FailingYDL.calls)
    assert len(offline_youtube["calls"]) == 1
