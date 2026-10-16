"""Чистые решения быстрого пути TikTok.

Резолвер отдаёт прямую ссылку на H.264-видео со звуком, поэтому быстрый путь
обходится без yt-dlp и без перекодирования. Здесь только разбор ответа и
проверки пригодности — без сети и без файловых операций.

Обоснование и замеры: docs/research/latency-disk-network-research.md
"""

from __future__ import annotations

from dataclasses import dataclass

from utils.fast_path import FastPathUnavailable, is_allowed_media_url as _is_allowed


__all__ = [
    "ALLOWED_MEDIA_DOMAINS",
    "AUDIO_DURATION_TOLERANCE_SECONDS",
    "FastMedia",
    "FastPathUnavailable",
    "audio_matches_video",
    "is_allowed_media_url",
    "parse_fast_media",
]

# Допустимое расхождение длительности звука и видео, секунды.
AUDIO_DURATION_TOLERANCE_SECONDS = 2

# Домены, с которых разрешено скачивать медиа быстрого пути: собственный CDN
# TikTok и хост самого резолвера (проверено на реальных ссылках, см.
# docs/research/latency-disk-network-research.md §4). Ответ резолвера — данные
# третьей стороны: без allowlist подменённый `play` вида
# `http://telegram-bot-api:8081/...` заставил бы бота сходить во внутреннюю
# сеть Docker и отдать тело запросившему пользователю.
ALLOWED_MEDIA_DOMAINS = frozenset(
    {
        "tiktokcdn.com",
        "tiktokcdn-us.com",
        "tiktokcdn-eu.com",
        "tiktokcdn-in.com",
        "tikwm.com",
    }
)


def is_allowed_media_url(url: str) -> bool:
    """Проверяет ссылку TikTok по allowlist доменов резолвера и CDN."""
    return _is_allowed(url, ALLOWED_MEDIA_DOMAINS)


@dataclass(frozen=True)
class FastMedia:
    """Прямые ссылки и метаданные, достаточные для отправки без обработки."""

    video_url: str
    size: int
    duration: int
    title: str
    audio_url: str | None
    audio_is_video_sound: bool


def audio_matches_video(music_info: dict, video_duration: int) -> bool:
    """Проверяет, что ``music`` — это звук самого видео, а не библиотечный трек.

    Резолвер возвращает в ``music`` звуковую дорожку публикации. Для видео с
    лицензированным треком это полная песня: проверено на реальном 35-секундном
    видео, где ``music`` пришёл на 168 секунд. Поэтому требуем и флаг
    ``original``, и совпадение длительности.
    """
    if not music_info:
        return False
    if music_info.get("original") is not True:
        return False

    music_duration = music_info.get("duration")
    if music_duration is None:
        return False

    try:
        drift = abs(int(music_duration) - int(video_duration))
    except (TypeError, ValueError):
        return False

    return drift <= AUDIO_DURATION_TOLERANCE_SECONDS


def parse_fast_media(payload: dict) -> FastMedia:
    """Разбирает ответ резолвера в набор прямых ссылок.

    Raises:
        FastPathUnavailable: ответ с ошибкой, без прямой ссылки, фото-пост или
            ссылка на видео вне allowlist разрешённых доменов.
    """
    if payload.get("code") != 0:
        raise FastPathUnavailable(
            f"резолвер отказал: {payload.get('msg') or 'неизвестная ошибка'}"
        )

    data = payload.get("data") or {}

    if data.get("images"):
        raise FastPathUnavailable(
            "это TikTok фото-пост, быстрый путь видео неприменим"
        )

    video_url = data.get("play")
    if not video_url:
        raise FastPathUnavailable("резолвер не вернул прямую ссылку на видео")
    if not is_allowed_media_url(str(video_url)):
        raise FastPathUnavailable(
            f"ссылка на видео вне allowlist разрешённых доменов: {video_url}"
        )

    duration = int(data.get("duration") or 0)
    music_info = data.get("music_info") or {}
    audio_is_video_sound = audio_matches_video(music_info, duration)
    audio_url = None
    if audio_is_video_sound:
        audio_candidate = data.get("music") or music_info.get("play")
        # Негодная ссылка звука не отменяет быстрый путь: звук будет извлечён
        # из видео копированием потока.
        if audio_candidate and is_allowed_media_url(str(audio_candidate)):
            audio_url = str(audio_candidate)

    return FastMedia(
        video_url=str(video_url),
        size=int(data.get("size") or 0),
        duration=duration,
        title=str(data.get("title") or ""),
        audio_url=audio_url,
        audio_is_video_sound=audio_is_video_sound,
    )
