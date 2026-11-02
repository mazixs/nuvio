"""Выбор формата YouTube для кнопки «отправить видео в Telegram».

Кнопка обещает файл, который просто откроется в Telegram, поэтому здесь два
критерия, и оба важнее максимального разрешения:

1. **Бюджет.** Он приходит извне и равен реальному лимиту доставки: 2000 МБ при
   локальном Bot API, 50 МБ при облачном. Раньше границы были захардкожены под
   облачный режим (35 МБ на видео + 15 МБ на звук), и локальный сервер это
   никак не учитывал.
2. **Язык.** Русская дорожка выбирается раньше остальных, если пара входит в
   лимит доставки. Когда ее нет, берем оригинальную дорожку.
3. **Потолок разрешения.** Кнопка обещает "просто отправь", а не "максимум
   возможного»: 4K на 32-минутном ролике — это 467–926 МБ и минуты отправки,
   причём H.264 в таком разрешении YouTube не отдаёт вовсе. Кто хочет больше,
   берёт формат в меню осознанно.
4. **Пригодность кодека — как тай-брейк, а не как приоритет.** H.264 и AAC
   проигрываются везде (ADR-001 фиксировал, чем заканчивается нестандартный
   кодек на iOS), поэтому при равном разрешении выбирается именно они. Но
   ставить кодек выше разрешения нельзя: в облачном режиме под 50 МБ H.264
   влезает только в 240p, тогда как AV1 даёт 480p — это была бы потеря
   качества там, где её никто не просил.

Модуль чистый: без сети, без файлов и без обращения к config.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


# Префиксы кодеков, которые Telegram проигрывает без оговорок на всех клиентах.
TELEGRAM_READY_VIDEO_CODECS = ("avc1", "h264")
TELEGRAM_READY_AUDIO_CODECS = ("mp4a", "aac")

# Потолок разрешения для кнопки. Выше — только через меню форматов.
TG_VIDEO_MAX_HEIGHT = 1080


@dataclass(frozen=True)
class TgVideoChoice:
    """Выбранный формат и то, чем за него платим."""

    format_id: str
    height: int | None
    ext: str
    kind: str
    total_size: int


def _codec_rank(codec: Any, ready_prefixes: tuple[str, ...]) -> int:
    """0 — кодек проигрывается везде, 1 — остальные."""
    normalized = str(codec or "").lower()
    return 0 if normalized.startswith(ready_prefixes) else 1


def _sized(formats: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Оставляет форматы с известным размером.

    Формат без размера сверить с бюджетом нельзя, а угадывать здесь нечего:
    решение о таких форматах принимает вызывающий код.
    """
    return [fmt for fmt in formats if isinstance(fmt.get("filesize"), int)]


def _video_sort_key(fmt: dict[str, Any]) -> tuple[int, int, int]:
    """Разрешение важнее кодека, кодек важнее размера."""
    return (
        -(fmt.get("height") or 0),
        _codec_rank(fmt.get("vcodec"), TELEGRAM_READY_VIDEO_CODECS),
        fmt["filesize"],
    )


def audio_language_rank(fmt: dict[str, Any]) -> int:
    """Русский звук, затем оригинал, затем остальные дорожки.

    Суффиксы format_id вроде `-1` не имеют постоянного значения языка.
    Описание для слабовидящих не выбирается автоматически.
    """
    language = str(fmt.get("language") or "").lower()
    note = str(fmt.get("format_note") or "").lower()
    preference = fmt.get("language_preference")
    descriptive = language.endswith("-desc") or "descriptive" in note
    if descriptive:
        return 5
    if language == "ru" or language.startswith("ru-"):
        return 0
    if isinstance(preference, (int, float)) and preference >= 10:
        return 1
    if "original" in note:
        return 1
    if isinstance(preference, (int, float)) and preference >= 5:
        return 2
    if not language:
        return 3
    return 4


def _audio_sort_key(fmt: dict[str, Any]) -> tuple[int, int, int]:
    return (
        audio_language_rank(fmt),
        _codec_rank(fmt.get("acodec"), TELEGRAM_READY_AUDIO_CODECS),
        -(fmt.get("filesize") or 0),
    )


@dataclass(frozen=True)
class VideoOption:
    """Один пункт меню разрешений."""

    resolution: int
    format_id: str
    ext: str
    size: int  # 0 — размер неизвестен


def _resolution_of(fmt: dict[str, Any]) -> int:
    """Разрешение так, как его называет пользователь: по короткой стороне.

    У вертикального видео yt-dlp сообщает высоту длинной стороны, поэтому Shorts
    в меню выглядели как «2560p» и «1920p». Пользователь же знает их как 1440p и
    1080p — ровно так их подписывает и сам YouTube.
    """
    height = fmt.get("height") or 0
    width = fmt.get("width") or 0
    return min(height, width) if width else height


@dataclass(frozen=True)
class AudioOption:
    """Один пункт меню звуковых дорожек."""

    format_id: str
    ext: str
    size: int  # 0 — размер неизвестен
    language: str | None = None


def list_audio_options(
    audio_only: Sequence[dict[str, Any]], budget_bytes: int
) -> list[AudioOption]:
    """Возвращает родные дорожки, пригодные для отправки как аудио.

    Opus в WebM Telegram аудиофайлом не считает, поэтому такие дорожки в меню
    не попадают: предлагать их — значит обещать то, что не воспроизведётся.
    Перекодирование здесь не рассматривается, речь только о родном звуке.
    """
    playable = [
        fmt
        for fmt in audio_only
        if _codec_rank(fmt.get("acodec"), TELEGRAM_READY_AUDIO_CODECS) == 0
        and (
            not isinstance(fmt.get("filesize"), int)
            or fmt["filesize"] <= budget_bytes
        )
    ]
    return [
        AudioOption(
            format_id=str(fmt["format_id"]),
            ext=fmt.get("ext") or "m4a",
            size=fmt["filesize"] if isinstance(fmt.get("filesize"), int) else 0,
            language=fmt.get("language"),
        )
        for fmt in sorted(
            playable,
            key=_audio_sort_key,
        )
    ]


def _best_pair(
    video: dict[str, Any], audios: Sequence[dict[str, Any]], budget_bytes: int
) -> tuple[str, int, int] | None:
    """Подбирает к видеодорожке звук, с которым пара влезает в бюджет."""
    if not isinstance(video.get("filesize"), int):
        # Размер неизвестен — сверять с бюджетом нечего, звук берём лучший.
        if not audios:
            return None
        return (
            f"{video['format_id']}+{audios[0]['format_id']}",
            0,
            audio_language_rank(audios[0]),
        )

    for audio in audios:
        total = video["filesize"] + audio["filesize"]
        if total <= budget_bytes:
            return (
                f"{video['format_id']}+{audio['format_id']}",
                total,
                audio_language_rank(audio),
            )
    return None


def list_video_options(
    video_only: Sequence[dict[str, Any]],
    audio_only: Sequence[dict[str, Any]],
    combined: Sequence[dict[str, Any]],
    budget_bytes: int,
) -> list[VideoOption]:
    """Возвращает по одному пункту на разрешение, от высокого к низкому.

    Меню обещает выбор, поэтому потолок разрешения здесь не действует — в
    отличие от кнопки «отправить в Telegram». Отсекается только то, что не
    влезает в лимит доставки: предлагать заведомо неотправляемый файл значит
    обещать невыполнимое.

    Внутри одного разрешения работают те же правила, что у кнопки: сначала
    H.264, затем меньший размер; готовый ``combined`` предпочтительнее пары,
    потому что его не нужно склеивать FFmpeg. Исключение - пара с русским
    звуком вместо готового файла на другом языке.
    """
    audios = sorted(_sized(audio_only), key=_audio_sort_key)
    options: dict[int, VideoOption] = {}
    option_language_ranks: dict[int, int] = {}

    ready = sorted(
        (fmt for fmt in _sized(combined) if fmt["filesize"] <= budget_bytes),
        key=_video_sort_key,
    )
    for fmt in ready:
        resolution = _resolution_of(fmt)
        rank = audio_language_rank(fmt)
        if resolution not in options or rank < option_language_ranks[resolution]:
            options[resolution] = VideoOption(
                resolution=resolution,
                format_id=str(fmt["format_id"]),
                ext=fmt.get("ext") or "mp4",
                size=fmt["filesize"],
            )
            option_language_ranks[resolution] = rank

    # Форматы без размера сортируются после форматов с размером: у них тот же
    # ключ, но проверить бюджет нельзя, поэтому в приоритете известное.
    tracks = sorted(_sized(video_only), key=_video_sort_key) + [
        fmt for fmt in video_only if not isinstance(fmt.get("filesize"), int)
    ]
    for fmt in tracks:
        resolution = _resolution_of(fmt)
        pair = _best_pair(fmt, audios, budget_bytes)
        if pair:
            format_id, size, rank = pair
            if resolution not in options or (
                size > 0 and rank < option_language_ranks[resolution]
            ):
                options[resolution] = VideoOption(
                    resolution=resolution,
                    format_id=format_id,
                    ext=fmt.get("ext") or "mp4",
                    size=size,
                )
                option_language_ranks[resolution] = rank

    return sorted(options.values(), key=lambda option: -option.resolution)


def select_tg_video_format(
    video_only: Sequence[dict[str, Any]],
    audio_only: Sequence[dict[str, Any]],
    combined: Sequence[dict[str, Any]],
    budget_bytes: int,
    max_height: int = TG_VIDEO_MAX_HEIGHT,
) -> TgVideoChoice | None:
    """Возвращает формат, укладывающийся в ``budget_bytes``.

    Русская дорожка имеет приоритет. При равном языке пара «видео + звук»
    проверяется раньше готового ``combined``: склейка стоит секунды FFmpeg,
    а разница в качестве обычно кратная - у YouTube единственный
    combined-формат чаще всего 360p.

    Returns:
        None, если ни пара, ни combined с известным размером в бюджет не влезли.
    """
    videos = sorted(
        (fmt for fmt in _sized(video_only) if (fmt.get("height") or 0) <= max_height),
        key=_video_sort_key,
    )
    audios = sorted(_sized(audio_only), key=_audio_sort_key)

    pair_choice: TgVideoChoice | None = None
    pair_rank: int | None = None
    audio_groups: dict[int, list[dict[str, Any]]] = {}
    for audio in audios:
        audio_groups.setdefault(audio_language_rank(audio), []).append(audio)
    for rank, group in sorted(audio_groups.items()):
        for video in videos:
            for audio in group:
                total = video["filesize"] + audio["filesize"]
                if total <= budget_bytes:
                    pair_choice = TgVideoChoice(
                        format_id=f"{video['format_id']}+{audio['format_id']}",
                        height=video.get("height"),
                        ext=video.get("ext") or "mp4",
                        kind="combined_manual",
                        total_size=total,
                    )
                    pair_rank = rank
                    break
            if pair_choice:
                break
        if pair_choice:
            break

    for fmt in sorted(
        (f for f in _sized(combined) if (f.get("height") or 0) <= max_height),
        key=lambda fmt: (audio_language_rank(fmt), _video_sort_key(fmt)),
    ):
        if fmt["filesize"] <= budget_bytes:
            combined_choice = TgVideoChoice(
                format_id=str(fmt["format_id"]),
                height=fmt.get("height"),
                ext=fmt.get("ext") or "mp4",
                kind="combined",
                total_size=fmt["filesize"],
            )
            if (
                pair_choice
                and pair_rank is not None
                and pair_rank <= audio_language_rank(fmt)
            ):
                return pair_choice
            return combined_choice

    return pair_choice


def select_unsized_video_format(
    combined: Sequence[dict[str, Any]],
) -> dict[str, Any] | None:
    """Запасной combined-формат, когда размер неизвестен и бюджет не проверить.

    Берётся нижняя треть по высоте среди дорожек лучшего языка, чтобы скачанное
    с большей вероятностью прошло по лимиту доставки.
    """
    unsized = [fmt for fmt in combined if fmt.get("filesize") is None]
    if not unsized:
        return None
    best_rank = min(audio_language_rank(fmt) for fmt in unsized)
    candidates = sorted(
        (fmt for fmt in unsized if audio_language_rank(fmt) == best_rank),
        # Ключ height есть всегда, но бывает None: get(..., 0) его не заменит.
        key=lambda fmt: fmt.get("height") or 0,
    )
    return candidates[len(candidates) // 3]


def select_audio_format(
    audio_only: Sequence[dict[str, Any]],
) -> tuple[dict[str, Any] | None, bool]:
    """Аудиодорожка для кнопки «аудио» и признак, что её можно не перекодировать.

    Среди дорожек лучшего языка предпочитается контейнер, который играет без
    конвертации: m4a, затем mp3, затем ogg. Если такого нет, возвращается лучшая
    дорожка с признаком ``False`` — её нужно перекодировать.
    """
    ranked = sorted(
        audio_only,
        key=lambda fmt: (audio_language_rank(fmt), -(fmt.get("filesize") or 0)),
    )
    if not ranked:
        return None, False
    best_rank = audio_language_rank(ranked[0])
    for ext in ("m4a", "mp3", "ogg"):
        native = next(
            (
                fmt for fmt in ranked
                if fmt.get("ext") == ext and audio_language_rank(fmt) == best_rank
            ),
            None,
        )
        if native:
            return native, True
    return ranked[0], False
