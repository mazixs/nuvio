"""Отмена, срок и лимит размера внутри загрузчиков; длинные названия файлов."""

import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yt_dlp

from api.jobs import describe_failure
from utils import rutube_vk_utils, tiktok_instagram_utils as media, work_budget, youtube_utils, ytdlp_common
from utils.cancellation import CancelledByUser, forget_cancellation, request_cancellation
from utils.public_errors import should_notify_admins
from utils.ytdlp_common import FileSizeLimitError, apply_network_opts, apply_work_limits, size_limit_hook

pytestmark = pytest.mark.unit

TIKTOK_URL = "https://www.tiktok.com/@tester/video/123456789"
INSTAGRAM_URL = "https://www.instagram.com/reel/ABC123/"
SESSION = "controls-session"
DOWNLOADERS = [
    (media.download_tiktok_video, TIKTOK_URL),
    (media.download_tiktok_audio, TIKTOK_URL),
    (media.download_instagram_video, INSTAGRAM_URL),
]


class FakeYDL:
    """Вместо загрузки вызывает поведение теста с настройками yt-dlp."""

    attempts: list[dict] = []
    behaviour = staticmethod(lambda options: None)

    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, url, download=False):
        if not download:
            return {"formats": [{"format_id": "1", "height": 720, "vcodec": "h264", "acodec": "aac"}]}
        FakeYDL.attempts.append(self.options)
        FakeYDL.behaviour(self.options)
        raise AssertionError("поведение теста должно прервать загрузку")


@pytest.fixture
def platforms(monkeypatch, tmp_path):
    """Все обходы включены: cookies есть, конфигураций три, повторы настоящие."""
    FakeYDL.attempts = []
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.setattr(media, "TIKTOK_FAST_PATH", False)
    monkeypatch.setattr(media, "INSTAGRAM_FAST_PATH", False)
    monkeypatch.setattr(media, "TIKTOK_COOKIES_FILE", cookies)
    monkeypatch.setattr(media, "INSTAGRAM_COOKIES_FILE", cookies)
    monkeypatch.setattr(media, "_tiktok_cookiefile", lambda use_cookies: None)
    monkeypatch.setattr(media, "_instagram_cookiefile", lambda use_cookies: None)
    monkeypatch.setattr(media, "_resolve_tiktok_url", lambda url: url)
    monkeypatch.setattr(media, "create_tiktok_ytdl", FakeYDL)
    monkeypatch.setattr(media.yt_dlp, "YoutubeDL", FakeYDL)
    yield tmp_path
    forget_cancellation(SESSION)


def in_job(function, *args, seconds=30.0, session_id=None, limit=None):
    return work_budget.execute(function, args, time.monotonic() + seconds, session_id, limit)


def run_hooks(options, **status):
    for hook in options["progress_hooks"]:
        hook({"status": "downloading", **status})


# ── Хук лимита ────────────────────────────────────────────────────


def test_tracks_of_one_download_count_together():
    hook = size_limit_hook(100)
    hook({"filename": "video.f137.mp4", "downloaded_bytes": 60})
    with pytest.raises(FileSizeLimitError):
        hook({"filename": "audio.f140.m4a", "downloaded_bytes": 41})


def test_repeated_progress_of_one_file_is_not_summed():
    hook = size_limit_hook(100)
    for downloaded in (60, 80, 100):
        hook({"filename": "video.mp4", "downloaded_bytes": downloaded})


def test_hook_keeps_counting_from_fragment_threads():
    hook = size_limit_hook(1000)
    errors = []

    def report(name):
        try:
            for downloaded in range(0, 400, 10):
                hook({"filename": name, "downloaded_bytes": downloaded})
        except FileSizeLimitError as error:
            errors.append(error)

    threads = [threading.Thread(target=report, args=(f"part-{index}",)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors


# ── Настройки ─────────────────────────────────────────────────────


def test_bot_work_gets_no_size_hook():
    """Бот проверяет готовый файл: он может ужаться при перекодировании."""
    options = {}
    in_job(apply_work_limits, options, SESSION)
    assert len(options["progress_hooks"]) == 1


def test_api_job_limit_stays_in_the_hook_outside_the_job():
    options = {}
    in_job(apply_network_opts, options, SESSION, limit=100)
    assert len(options["progress_hooks"]) == 2
    # Поток фрагментов вызывает хук без контекста задачи.
    with pytest.raises(FileSizeLimitError):
        options["progress_hooks"][-1]({"filename": "video.mp4", "downloaded_bytes": 101})


def test_options_stay_clean_without_a_session_or_a_limit():
    options = {}
    apply_work_limits(options)
    assert "progress_hooks" not in options
    assert options["socket_timeout"] == 20


def test_socket_timeout_never_outlives_the_deadline():
    options = {"socket_timeout": 40}
    in_job(apply_work_limits, options, seconds=5.0)
    assert 0 < options["socket_timeout"] <= 5


def test_expired_deadline_stops_before_the_options_are_built():
    def expired():
        time.sleep(0.02)
        apply_work_limits({})

    with pytest.raises(TimeoutError):
        in_job(expired, seconds=0.01)


# ── TikTok и Instagram ────────────────────────────────────────────


@pytest.mark.parametrize(("download", "url"), DOWNLOADERS)
def test_cancellation_stops_the_download_at_once(platforms, download, url):
    def cancelled(options):
        request_cancellation(SESSION)
        run_hooks(options)

    FakeYDL.behaviour = staticmethod(cancelled)
    with pytest.raises(CancelledByUser):
        in_job(download, url, SESSION, platforms)
    assert len(FakeYDL.attempts) == 1


@pytest.mark.parametrize(("download", "url"), DOWNLOADERS)
def test_expired_deadline_does_not_walk_the_fallbacks(platforms, download, url):
    def too_slow(options):
        time.sleep(0.6)
        raise yt_dlp.utils.DownloadError("ERROR: timed out")

    FakeYDL.behaviour = staticmethod(too_slow)
    with pytest.raises(TimeoutError):
        in_job(download, url, SESSION, platforms, seconds=0.5)
    assert len(FakeYDL.attempts) == 1


@pytest.mark.parametrize(("download", "url"), DOWNLOADERS)
def test_oversized_download_does_not_walk_the_fallbacks(platforms, download, url):
    FakeYDL.behaviour = staticmethod(
        lambda options: run_hooks(options, filename="video.mp4", downloaded_bytes=1001)
    )
    with pytest.raises(FileSizeLimitError):
        in_job(download, url, SESSION, platforms, limit=1000)
    assert len(FakeYDL.attempts) == 1


@pytest.mark.parametrize(("download", "url"), DOWNLOADERS)
def test_ordinary_failure_still_walks_the_fallbacks(platforms, download, url):
    def fail(options):
        raise yt_dlp.utils.DownloadError("ERROR: boom")

    FakeYDL.behaviour = staticmethod(fail)
    with pytest.raises(Exception, match="boom"):
        in_job(download, url, SESSION, platforms)
    assert len(FakeYDL.attempts) > 1


# ── FFmpeg аудио Instagram ────────────────────────────────────────


HANGING_PROCESS = [sys.executable, "-c", "import time; time.sleep(60)"]


def test_ffmpeg_result_keeps_the_exit_code_and_stderr():
    result = media._run_ffmpeg(
        [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"]
    )
    assert (result.returncode, result.stderr) == (3, "boom")


def test_expired_deadline_stops_ffmpeg():
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        in_job(media._run_ffmpeg, HANGING_PROCESS, seconds=0.3)
    assert time.monotonic() - started < 30


def test_cancellation_stops_ffmpeg(platforms):
    def cancelled():
        request_cancellation(SESSION)
        media._run_ffmpeg(HANGING_PROCESS)

    started = time.monotonic()
    with pytest.raises(CancelledByUser):
        in_job(cancelled, session_id=SESSION)
    assert time.monotonic() - started < 30


def test_ffmpeg_stops_at_its_own_limit_outside_a_job(monkeypatch):
    monkeypatch.setattr(media, "BLOCKING_TASK_TIMEOUT", 0.3)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        media._run_ffmpeg(HANGING_PROCESS)
    assert time.monotonic() - started < 30


# ── Запасной CLI YouTube ──────────────────────────────────────────


YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
YOUTUBE_DOWNLOADERS = [
    youtube_utils.download_video,
    youtube_utils.download_audio_native,
    youtube_utils.download_audio,
]


@pytest.fixture
def youtube_cli(monkeypatch, tmp_path):
    """Встроенный yt-dlp падает, а исход каждого запуска CLI задаёт тест."""

    def unavailable(options):
        raise yt_dlp.utils.DownloadError("ERROR: Requested format is not available")

    FakeYDL.attempts = []
    FakeYDL.behaviour = staticmethod(unavailable)
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.setattr(youtube_utils.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(youtube_utils, "YOUTUBE_COOKIES_FILE", cookies)
    monkeypatch.setattr(youtube_utils, "YTDLP_CLI_FALLBACK", True)
    monkeypatch.setattr(youtube_utils, "_cookiefile_if_available", lambda use_cookies: None)
    monkeypatch.setattr(youtube_utils, "_ensure_ios_compatible", lambda path, session_id: Path(path))
    cli = SimpleNamespace(runs=0, behaviour=None)

    def run(command, *, timeout=None):
        cli.runs += 1
        return cli.behaviour()

    monkeypatch.setattr(youtube_utils, "run_yt_dlp_cli", run)
    return cli


def in_youtube_job(download, output_dir, **limits):
    return in_job(lambda: download(YOUTUBE_URL, "140", SESSION, output_dir=output_dir), **limits)


@pytest.mark.parametrize("download", YOUTUBE_DOWNLOADERS)
def test_youtube_limit_in_the_hook_skips_cookies_and_cli(youtube_cli, tmp_path, download):
    FakeYDL.behaviour = staticmethod(
        lambda options: run_hooks(options, filename="video.mp4", downloaded_bytes=1001)
    )
    with pytest.raises(FileSizeLimitError):
        in_youtube_job(download, tmp_path, limit=1000)
    assert len(FakeYDL.attempts) == 1
    assert youtube_cli.runs == 0


@pytest.mark.parametrize("download", YOUTUBE_DOWNLOADERS)
def test_oversized_cli_file_is_not_downloaded_again(youtube_cli, tmp_path, download):
    def oversized():
        path = tmp_path / "big.mp4"
        path.write_bytes(b"x" * 11)
        return subprocess.CompletedProcess([], 0, f"{path}\n", "")

    youtube_cli.behaviour = oversized
    with pytest.raises(FileSizeLimitError):
        in_youtube_job(download, tmp_path, limit=10)
    assert youtube_cli.runs == 1


@pytest.mark.parametrize("download", YOUTUBE_DOWNLOADERS)
def test_expired_deadline_in_cli_keeps_its_own_error(youtube_cli, tmp_path, download):
    def too_slow():
        time.sleep(0.6)
        work_budget.check()

    youtube_cli.behaviour = too_slow
    with pytest.raises(TimeoutError):
        in_youtube_job(download, tmp_path, seconds=0.5)
    assert youtube_cli.runs == 1


def _cli_exits_with_an_error():
    return subprocess.CompletedProcess([], 1, "", "ERROR: boom")


def _cli_run_times_out():
    raise subprocess.TimeoutExpired("yt-dlp", 1)


@pytest.mark.parametrize("failure", [_cli_exits_with_an_error, _cli_run_times_out])
@pytest.mark.parametrize("download", YOUTUBE_DOWNLOADERS)
def test_failed_cli_run_still_tries_the_next_variant(youtube_cli, tmp_path, download, failure):
    youtube_cli.behaviour = failure
    with pytest.raises(yt_dlp.utils.DownloadError, match="Requested format"):
        in_youtube_job(download, tmp_path)
    assert youtube_cli.runs > 1


# ── Обёртка yt-dlp над истёкшим сроком ────────────────────────────


class UnavailableVideoError(Exception):
    """Как одноимённая ошибка yt-dlp: не наследует DownloadError, и обходы её не ловят."""


def ytdlp_wraps_os_errors(options):
    """Как `YoutubeDL.process_info`: срок истекает в хуке, а OSError оборачивается."""
    time.sleep(0.6)
    try:
        run_hooks(options)
    except OSError as error:
        raise UnavailableVideoError(f"Unable to download video: {error}")


def test_expired_deadline_replaces_the_error_of_the_function():
    def too_slow():
        time.sleep(0.1)
        raise UnavailableVideoError("обёртка")

    with pytest.raises(TimeoutError) as caught:
        in_job(too_slow, seconds=0.05)
    # Обёртка остаётся в цепочке для журнала.
    assert isinstance(caught.value.__context__, UnavailableVideoError)


def test_cancellation_replaces_the_error_of_the_function(platforms):
    def cancelled():
        request_cancellation(SESSION)
        raise UnavailableVideoError("обёртка")

    with pytest.raises(CancelledByUser):
        in_job(cancelled, session_id=SESSION)


def test_error_passes_unchanged_without_an_expired_budget():
    original = UnavailableVideoError("обёртка")

    def fail():
        raise original

    with pytest.raises(UnavailableVideoError) as caught:
        in_job(fail, session_id=SESSION)
    assert caught.value is original


@pytest.mark.parametrize(
    ("platform", "category", "download", "args"),
    [
        ("youtube", "NETWORK_TIMEOUT", youtube_utils.download_video, (YOUTUBE_URL, "140", SESSION)),
        ("rutube", "TIMEOUT", rutube_vk_utils.download_rutube_video, ("https://rutube.ru/video/abc/", SESSION)),
        ("vk", "TIMEOUT", rutube_vk_utils.download_vk_video, ("https://vk.com/video-1_2", SESSION)),
    ],
)
def test_expired_deadline_behind_the_wrapper_is_a_timeout_for_the_client(
    youtube_cli, tmp_path, platform, category, download, args
):
    """`youtube_cli` подменяет общий `yt_dlp.YoutubeDL`, поэтому покрывает и Rutube с VK."""
    FakeYDL.behaviour = staticmethod(ytdlp_wraps_os_errors)
    # Ловится любая ошибка: без правки клиент получал бы `download_failed`.
    with pytest.raises(Exception) as caught:
        in_job(lambda: download(*args, output_dir=tmp_path), seconds=0.5)

    error, reported = describe_failure(platform, caught.value, SESSION)

    assert error["code"] == "timeout"
    assert reported == category
    assert not should_notify_admins(reported)
    # Срок истёк именно в хуке первой попытки, а не до неё.
    assert isinstance(caught.value.__context__, UnavailableVideoError)
    assert len(FakeYDL.attempts) == 1
    assert youtube_cli.runs == 0


# ── Длинные названия ──────────────────────────────────────────────


@pytest.mark.parametrize("title", ["Очень длинное название ролика " * 20, "漢字かな交じり文" * 40])
def test_long_titles_fit_the_file_name_limit(title):
    with yt_dlp.YoutubeDL({"outtmpl": ytdlp_common.OUTPUT_FILENAME_TEMPLATE, "quiet": True}) as ydl:
        name = ydl.prepare_filename({"id": "x", "title": title, "ext": "mp4"})
    assert len(f"{name}.f140.m4a.part".encode()) <= 255
    # Обрезка не должна ломать многобайтовый символ.
    assert "�" not in name


@pytest.mark.parametrize("module", [media, rutube_vk_utils, youtube_utils])
def test_downloaders_use_the_shared_template(module):
    assert "%(title)s" not in Path(module.__file__).read_text(encoding="utf-8")
