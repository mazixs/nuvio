"""Чистые решения быстрого пути Instagram.

GraphQL-ответ Instagram уже содержит прямую ссылку на файл, поэтому скачивание
обходится без yt-dlp. Здесь только разбор ответа и проверки пригодности — без
сети и без файловых операций.

Замеры на реальных рилсах: прямая ссылка отдаёт **H.264 + AAC** (720x1280,
4.61 МБ за 0.90 с анонимно), тогда как путь через yt-dlp занимал 7.54 с.
Обоснование: docs/research/latency-disk-network-research.md §5.7
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from utils.fast_path import FastPathUnavailable, is_allowed_media_url


# Домены, с которых разрешено скачивать медиа Instagram. Meta отдаёт файлы и с
# `cdninstagram.com` (проверено на реальных рилсах), и с `fbcdn.net` — второй
# оставлен, потому что Instagram чередует CDN между запросами.
ALLOWED_INSTAGRAM_MEDIA_DOMAINS = frozenset({"cdninstagram.com", "fbcdn.net"})


def is_allowed_instagram_media_url(url: str) -> bool:
    """Проверяет ссылку Instagram по allowlist доменов Meta."""
    return is_allowed_media_url(url, ALLOWED_INSTAGRAM_MEDIA_DOMAINS)


@dataclass(frozen=True)
class InstagramFastMedia:
    """Прямая ссылка и метаданные, достаточные для отправки без обработки.

    Размеры нужны для отправки: по этому пути файл не скачивается, померить его
    нечем, а `video_versions` сообщает их рядом со ссылкой (ADR-002).
    """

    video_url: str
    title: str
    width: int | None = None
    height: int | None = None
    duration: int | None = None


def _extract_caption_text(caption: Any) -> str:
    """Достаёт текст подписи, которая приходит то словарём, то строкой."""
    if isinstance(caption, dict):
        return str(caption.get("text") or "")
    if isinstance(caption, str):
        return caption
    return ""


def _first_video_url(media: dict[str, Any]) -> str | None:
    """Возвращает первую прямую ссылку на видео из ответа GraphQL.

    Версии одного рилса — один и тот же файл: на реальном ответе три записи
    `video_versions` (type 101/102/103) пришли с одинаковыми разрешением,
    размером и длительностью. Поэтому берётся первая пригодная.
    """
    for version in media.get("video_versions") or []:
        if isinstance(version, dict) and version.get("url"):
            return str(version["url"])

    direct_url = media.get("video_url")
    return str(direct_url) if direct_url else None


def _first_video_dimensions(media: dict[str, Any]) -> tuple[int | None, int | None]:
    """Размеры кадра из той же записи `video_versions`, что дала ссылку."""
    for version in media.get("video_versions") or []:
        if not isinstance(version, dict) or not version.get("url"):
            continue
        width, height = version.get("width"), version.get("height")
        if isinstance(width, int) and isinstance(height, int):
            return width, height
        return None, None
    return None, None


def parse_instagram_fast_media(media: dict[str, Any]) -> InstagramFastMedia:
    """Разбирает элемент GraphQL-ответа в прямую ссылку на видео.

    Raises:
        FastPathUnavailable: карусель, публикация без видео или ссылка вне
            allowlist разрешённых доменов.
    """
    if media.get("carousel_media") or media.get("edge_sidecar_to_children"):
        raise FastPathUnavailable(
            "это карусель Instagram, её собирает отдельный сборщик"
        )

    video_url = _first_video_url(media)
    if not video_url:
        raise FastPathUnavailable("в ответе Instagram нет прямой ссылки на видео")
    if not is_allowed_instagram_media_url(video_url):
        raise FastPathUnavailable(
            f"ссылка на видео вне allowlist разрешённых доменов: {video_url}"
        )

    width, height = _first_video_dimensions(media)
    try:
        duration = int(float(media.get("video_duration") or 0)) or None
    except (TypeError, ValueError):
        duration = None

    return InstagramFastMedia(
        video_url=video_url,
        title=_extract_caption_text(media.get("caption")),
        width=width,
        height=height,
        duration=duration,
    )
