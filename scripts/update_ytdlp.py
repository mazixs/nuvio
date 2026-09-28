"""Обновляет точную версию yt-dlp и оба lock-файла одной командой."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PIN = re.compile(r"^yt-dlp\[default\]==(?P<version>[^\s]+)$", re.MULTILINE)
VERSION = re.compile(r"^\d{4}\.\d{1,2}\.\d{1,2}(?:\.\d+\.dev0)?$")
DOCUMENTED_PIN_OCCURRENCES = {
    "AGENTS.md": 1,
    "README.ru.md": 1,
    "docs/PRD.md": 1,
    "docs/technical/youtube-download-runbook.md": 2,
}


def update_documented_pin(
    path: Path, old_version: str, new_version: str, expected_count: int
) -> None:
    """Обновляет точную версию в документе, не завися от языка его текста."""
    content = path.read_text(encoding="utf-8")
    count = content.count(old_version)
    if count != expected_count:
        raise RuntimeError(
            f"ожидалось {expected_count} упоминаний {old_version} в {path.name}, найдено {count}"
        )
    path.write_text(content.replace(old_version, new_version), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="Точная версия, например 2026.9.16.232951.dev0")
    parser.add_argument("--skip-checks", action="store_true", help="Только обновить lock-файлы")
    args = parser.parse_args()
    if not VERSION.fullmatch(args.version):
        parser.error("нужна точная версия yt-dlp, без канала или URL")

    source = ROOT / "requirements.in"
    original = source.read_text(encoding="utf-8")
    matches = list(PIN.finditer(original))
    if len(matches) != 1:
        raise RuntimeError("в requirements.in ожидается ровно один pin yt-dlp[default]")
    old_version = matches[0].group("version")
    replacement, count = PIN.subn(f"yt-dlp[default]=={args.version}", original)
    if count != 1:
        raise RuntimeError("в requirements.in ожидается ровно один pin yt-dlp[default]")

    files = [
        source,
        ROOT / "requirements.txt",
        ROOT / "requirements-dev.txt",
        *(ROOT / relative for relative in DOCUMENTED_PIN_OCCURRENCES),
    ]
    backup = {path: path.read_bytes() for path in files}
    try:
        source.write_text(replacement, encoding="utf-8")
        for inputs, output in (
            ("requirements.in", "requirements.txt"),
            ("requirements-dev.in", "requirements-dev.txt"),
        ):
            subprocess.run(
                ["uv", "pip", "compile", "--python-version", "3.14", "--generate-hashes",
                 "--output-file", output, inputs],
                cwd=ROOT,
                check=True,
            )
        for output in files[1:]:
            if output.suffix != ".txt":
                continue
            lock = output.read_text(encoding="utf-8")
            if f"yt-dlp=={args.version}" not in lock:
                raise RuntimeError(f"{output.name} не закрепил версию {args.version}")
        for relative, expected_count in DOCUMENTED_PIN_OCCURRENCES.items():
            update_documented_pin(
                ROOT / relative, old_version, args.version, expected_count
            )
        if not args.skip_checks:
            subprocess.run(
                ["uv", "pip", "sync", "--python", sys.executable, "requirements-dev.txt"],
                cwd=ROOT,
                check=True,
            )
            subprocess.run([sys.executable, "-m", "ruff", "check", "."], cwd=ROOT, check=True)
            subprocess.run([sys.executable, "-m", "pytest", "-q", "--basetemp=/tmp/nuvio-update-ytdlp"], cwd=ROOT, check=True)
    except BaseException:
        for path, contents in backup.items():
            path.write_bytes(contents)
        raise

    subprocess.run(["git", "diff", "--", *(str(path.relative_to(ROOT)) for path in files)], cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
