"""Синтетическое HLS с потерянным сегментом не может завершиться как полное медиа."""

import functools
import shutil
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yt_dlp

from utils.ytdlp_common import DEFAULT_YTDLP_NETWORK_OPTS, is_progress_line
from utils.ytdlp_runtime import run_yt_dlp_cli
from utils import work_budget


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


@pytest.mark.parametrize("transport", ["api", "cli"])
def test_missing_hls_segment_is_rejected(tmp_path, transport):
    if not shutil.which("ffmpeg") or not getattr(yt_dlp, "YoutubeDL", None):
        pytest.skip("Нужны FFmpeg и настоящий yt-dlp")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25",
                    "-t", "4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "25", "-sc_threshold", "0", "-f", "hls",
                    "-hls_time", "1", "-hls_list_size", "0", "-hls_segment_filename", str(tmp_path / "part%03d.ts"), str(tmp_path / "input.m3u8")], check=True)
    (tmp_path / "part001.ts").unlink()
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/input.m3u8"
    try:
        if transport == "api":
            options = {**DEFAULT_YTDLP_NETWORK_OPTS, "outtmpl": str(tmp_path / "output.%(ext)s"), "quiet": True, "fragment_retries": 0, "hls_prefer_native": True}
            with yt_dlp.YoutubeDL(options) as downloader, pytest.raises((yt_dlp.utils.DownloadError, ValueError)) as failure:
                downloader.download([url])
            # В закрепленной версии параллельный abort может закрыть поток раньше соседнего фрагмента.
            if isinstance(failure.value, ValueError):
                assert "write to closed file" in str(failure.value)
        else:
            result = run_yt_dlp_cli([sys.executable, "-m", "yt_dlp", "--abort-on-unavailable-fragments", "--fragment-retries", "0", "--hls-prefer-native", "-o", str(tmp_path / "output.%(ext)s"), url], timeout=10)
            assert result.returncode != 0
        assert not (tmp_path / "output.mp4").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_missing_fragment_diagnostic_is_not_filtered_as_progress():
    assert not is_progress_line("[download] fragment not found; Skipping fragment 2")
    assert is_progress_line("[download] 10.0% of 4 MiB")


def test_cli_honors_shared_deadline_and_stops_process(tmp_path):
    output = tmp_path / "should-not-exist"
    code = f"import time; from pathlib import Path; time.sleep(2); Path({str(output)!r}).touch()"
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        work_budget.execute(lambda: run_yt_dlp_cli([sys.executable, "-c", code]), (), start + .05, None)
    assert time.monotonic() - start < 1.5
    assert not output.exists()


def test_ffmpeg_worker_process_stops_at_shared_deadline(tmp_path):
    output = tmp_path / "should-not-exist"
    code = f"import time; from pathlib import Path; time.sleep(2); Path({str(output)!r}).touch()"
    process = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        work_budget.execute(lambda: work_budget.communicate(process, 600), (), start + .05, None)
    assert process.poll() is not None
    assert time.monotonic() - start < 1.5
    assert not output.exists()
