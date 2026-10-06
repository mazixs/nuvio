"""Ручная проверка потери HLS-сегмента на синтетическом локальном видео.

Использует только временные файлы и HTTP-сервер на loopback; Telegram не вызывается.
Требует настоящие yt-dlp, FFmpeg и ffprobe.
"""

import functools
import json
import subprocess
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yt_dlp


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class CaptureLogger:
    def __init__(self):
        self.warnings = []

    def debug(self, message):
        if "fragment" in message.lower() and "skip" in message.lower():
            self.warnings.append(message)

    def warning(self, message):
        self.warnings.append(message)

    def error(self, message):
        self.warnings.append(message)


with tempfile.TemporaryDirectory(prefix="nuvio-hls-probe-") as directory:
    root = Path(directory)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x120:rate=25",
            "-t",
            "4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "25",
            "-sc_threshold",
            "0",
            "-f",
            "hls",
            "-hls_time",
            "1",
            "-hls_list_size",
            "0",
            "-hls_segment_filename",
            str(root / "part%03d.ts"),
            str(root / "input.m3u8"),
        ],
        check=True,
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(QuietHandler, directory=directory)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        capture = CaptureLogger()
        opts = {
            "logger": capture,
            "quiet": True,
            "noprogress": True,
            "retries": 5,
            "fragment_retries": 5,
            "skip_unavailable_fragments": True,
            "abort_on_unavailable_fragments": False,
            "concurrent_fragment_downloads": 4,
            "continuedl": False,
            "outtmpl": str(root / "output.%(ext)s"),
            "hls_prefer_native": True,
        }
        opts["outtmpl"] = str(root / "baseline.%(ext)s")
        with yt_dlp.YoutubeDL(opts) as downloader:
            downloader.download([f"http://127.0.0.1:{server.server_port}/input.m3u8"])
        (root / "part001.ts").unlink()
        opts["outtmpl"] = str(root / "output.%(ext)s")
        with yt_dlp.YoutubeDL(opts) as downloader:
            result = downloader.download(
                [f"http://127.0.0.1:{server.server_port}/input.m3u8"]
            )
        output = root / "output.mp4"
        duration = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(output),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        frame_counts = {}
        for name in ["baseline", "output"]:
            frame_counts[name] = int(
                subprocess.run(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-select_streams",
                        "v:0",
                        "-count_frames",
                        "-show_entries",
                        "stream=nb_read_frames",
                        "-of",
                        "default=noprint_wrappers=1:nokey=1",
                        str(root / f"{name}.mp4"),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            )
        print(
            json.dumps(
                {
                    "yt_dlp_version": yt_dlp.version.__version__,
                    "download_result": result,
                    "declared_duration_seconds": 4,
                    "output_duration_seconds": float(duration),
                    "output_exists": output.exists(),
                    "decoded_frame_counts": frame_counts,
                    "warnings": capture.warnings,
                }
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
