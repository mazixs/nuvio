"""Версия установленного yt-dlp и локальный CLI fallback."""

from __future__ import annotations

import importlib.metadata
import subprocess
import os
import signal
import time
from pathlib import Path

from config import YTDLP_CLI_TIMEOUT
from utils.logger import setup_logger
from utils import work_budget

logger = setup_logger(__name__)


def get_installed_yt_dlp_version() -> str | None:
    """Возвращает установленную версию yt-dlp без импорта самого пакета."""
    try:
        return importlib.metadata.version("yt-dlp")
    except importlib.metadata.PackageNotFoundError:
        return None


def run_yt_dlp_cli(
    command: list[str], *, timeout: int | None = None
) -> subprocess.CompletedProcess[str]:
    """Запускает локальный `python -m yt_dlp` сценарий без GUI."""
    logger.info("CLI fallback yt-dlp: %s", " ".join(command))
    allowed = work_budget.remaining(timeout or YTDLP_CLI_TIMEOUT)
    deadline = time.monotonic() + allowed
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=os.name != "nt")
    try:
        while True:
            work_budget.check()
            left = deadline - time.monotonic()
            if left <= 0:
                raise subprocess.TimeoutExpired(command, allowed)
            try:
                stdout, stderr = process.communicate(timeout=min(.25, left))
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            process.terminate()
        try:
            process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.communicate()
        raise



def extract_cli_output_path(stdout: str) -> Path | None:
    """Возвращает последний путь, напечатанный `--print after_move:filepath`."""
    for raw_line in reversed(stdout.splitlines()):
        candidate = raw_line.strip().strip('"')
        if not candidate:
            continue
        path = Path(candidate)
        if path.exists():
            return path
    return None
