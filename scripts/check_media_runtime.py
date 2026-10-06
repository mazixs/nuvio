"""Проверяет медиа-компоненты образа и при необходимости YouTube extractor."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from utils.runtime_status import runtime_components
from utils.canary import canary_video_url, run_youtube_canary_check
from utils.youtube_utils import get_available_formats, get_video_info


def _locked_ytdlp_version(lock_text: str) -> str:
    """Возвращает единственную закрепленную версию yt-dlp из lock-файла."""
    versions = re.findall(
        r"^yt-dlp(?:\[[A-Za-z0-9_,.-]+\])?==([^\s\\]+)",
        lock_text,
        re.MULTILINE,
    )
    if len(versions) != 1:
        raise RuntimeError("ожидается ровно один pin yt-dlp в lock-файле")
    return versions[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--online", action="store_true", help="Проверить форматы контрольного ролика")
    parser.add_argument("--download", action="store_true", help="Скачать контрольный ролик через канарейку")
    args = parser.parse_args()
    pin = _locked_ytdlp_version(Path("requirements.txt").read_text(encoding="utf-8"))
    parts = runtime_components()
    sys.stdout.write(f"{parts}\n")
    if parts["yt_dlp_installed"] != pin:
        raise RuntimeError("версия yt-dlp в образе не совпадает с pin")
    if parts["ejs"] == "отсутствует" or parts["deno"] == "отсутствует":
        raise RuntimeError("для YouTube нужны EJS и Deno")
    if parts["sqlite"] != "3.53.4":
        raise RuntimeError("образ должен использовать закрепленный SQLite 3.53.4 с исправленной гонкой WAL")
    if args.download:
        outcome = run_youtube_canary_check("image-smoke")
        if not outcome.ok:
            raise RuntimeError(f"YouTube download: {outcome.stage}: {outcome.detail}")
        sys.stdout.write(f"YouTube download: {outcome.detail}\n")
        return
    if not args.online:
        return
    info = get_video_info(canary_video_url())
    formats = get_available_formats(info)
    if not formats["combined"] and not (
        formats["video_only"] and formats["audio_only"]
    ):
        raise RuntimeError("YouTube не вернул пригодную пару видео и аудио")
    sys.stdout.write("YouTube extractor: пригодные форматы найдены\n")


if __name__ == "__main__":
    main()
