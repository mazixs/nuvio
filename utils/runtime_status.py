"""Локальная диагностика образа и сохраненный исход проверки YouTube."""

from __future__ import annotations

import importlib.metadata
import json
import os
import subprocess
import sqlite3
import tempfile
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import yt_dlp

from utils.logger import setup_logger

logger = setup_logger(__name__)


def _status_path() -> Path:
    root = Path(os.environ.get("DATA_DIR") or Path(__file__).resolve().parents[1])
    return root / "youtube_canary_status.json"


@lru_cache(maxsize=1)
def runtime_components() -> dict[str, str]:
    """Проверяет локальные компоненты один раз за жизнь процесса."""
    versions = {"yt_dlp_loaded": yt_dlp.version.__version__, "sqlite": sqlite3.sqlite_version}
    for package, key in (("yt-dlp", "yt_dlp_installed"), ("yt-dlp-ejs", "ejs")):
        try:
            versions[key] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[key] = "отсутствует"
    try:
        result = subprocess.run(
            ["deno", "--version"], capture_output=True, text=True, timeout=3, check=True
        )
        versions["deno"] = result.stdout.splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        versions["deno"] = "отсутствует"
    tag = os.environ.get("NUVIO_IMAGE_TAG") or "не передан"
    digest = os.environ.get("NUVIO_IMAGE_DIGEST") or "digest не передан"
    versions["image"] = f"{tag}; {digest}"
    return versions


def read_canary_status() -> dict:
    """Возвращает последний сохраненный исход, если он доступен."""
    try:
        return json.loads(_status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "еще не проверялось"}


def save_canary_status(**fields: object) -> None:
    """Атомарно сохраняет исход проверки между перезапусками."""
    path = _status_path()
    state = read_canary_status()
    state.update(fields)
    state["updated_at"] = datetime.now(UTC).isoformat()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".canary-", delete=False
        ) as handle:
            json.dump(state, handle, ensure_ascii=False)
            temp_path = Path(handle.name)
        temp_path.replace(path)
    except OSError as exc:
        logger.error("Не удалось сохранить статус канарейки: %s", exc)


def format_runtime_status() -> str:
    """Короткий текст для администратора."""
    parts = runtime_components()
    canary = read_canary_status()
    return "\n".join(
        (
            "Состояние загрузчика:",
            f"- Образ: {parts['image']}",
            f"- yt-dlp: {parts['yt_dlp_loaded']} (пакет: {parts['yt_dlp_installed']})",
            f"- EJS: {parts['ejs']}; Deno: {parts['deno']}",
            f"- SQLite: {parts['sqlite']}",
            f"- YouTube: {canary.get('state', 'неизвестно')}",
            f"- Проверено: {canary.get('updated_at', 'еще не проверялось')}",
            f"- Уведомление: {canary.get('notification', 'не требовалось')}",
        )
    )
