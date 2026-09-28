"""Чистые правила упаковки описаний и медиа-постов TikTok/Instagram."""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, TypeVar


T = TypeVar("T")
DeliveryState = Literal["delivered", "refused", "unknown"]
TELEGRAM_CAPTION_LIMIT = 1024
TELEGRAM_TEXT_LIMIT = 4096


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    """Подтвержденный результат отправки и известный прогресс публикации."""

    state: DeliveryState
    messages: tuple[object, ...] = ()
    confirmed_items: int = 0
    audio_delivered: bool | None = None
    error: Exception | None = None


def telegram_text_length(value: str) -> int:
    """Считает длину в единицах UTF-16, используемых Telegram."""
    return len(value.encode("utf-16-le")) // 2


def normalize_description(value: object) -> str | None:
    """Нормализует окончания строк и удаляет недопустимые управляющие знаки."""
    if not isinstance(value, str):
        return None
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = "".join(
        char
        for char in value
        if char in "\n\t" or unicodedata.category(char) not in {"Cc", "Cs"}
    )
    return value if value.strip() else None


def _is_grapheme_extension(char: str) -> bool:
    codepoint = ord(char)
    return (
        unicodedata.combining(char) != 0
        or 0xFE00 <= codepoint <= 0xFE0F
        or 0xE0100 <= codepoint <= 0xE01EF
        or 0x1F3FB <= codepoint <= 0x1F3FF
        or 0xE0020 <= codepoint <= 0xE007F
        or codepoint == 0x20E3
    )


def _next_grapheme_end(value: str, start: int) -> int:
    """Находит границу простой графемы, включая флаги и ZWJ-последовательности."""
    index = start + 1
    first_codepoint = ord(value[start])
    if 0x1F1E6 <= first_codepoint <= 0x1F1FF and index < len(value):
        next_codepoint = ord(value[index])
        if 0x1F1E6 <= next_codepoint <= 0x1F1FF:
            index += 1

    while index < len(value) and _is_grapheme_extension(value[index]):
        index += 1

    while index < len(value) and value[index] == "\u200d":
        index += 1
        if index >= len(value):
            break
        index += 1
        while index < len(value) and _is_grapheme_extension(value[index]):
            index += 1
    return index


def _safe_split_index(value: str, max_units: int) -> int:
    """Находит границу текста, не разрезая простую графему."""
    units = 0
    index = 0
    while index < len(value):
        cluster_end = _next_grapheme_end(value, index)
        cluster_units = telegram_text_length(value[index:cluster_end])
        if units + cluster_units > max_units:
            break
        units += cluster_units
        index = cluster_end
    return index


def split_telegram_text(value: str, max_units: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Делит текст по абзацам и словам, сохраняя исходные символы и порядок."""
    if max_units <= 0:
        raise ValueError("max_units должен быть положительным")
    if not value:
        return []

    chunks: list[str] = []
    remaining = value
    while telegram_text_length(remaining) > max_units:
        safe_end = _safe_split_index(remaining, max_units)
        if safe_end <= 0:
            raise ValueError("Лимит слишком мал для следующего символа")

        boundary = max(
            remaining.rfind("\n\n", 0, safe_end),
            remaining.rfind("\n", 0, safe_end),
            remaining.rfind(" ", 0, safe_end),
            remaining.rfind("\t", 0, safe_end),
        )
        # Разделитель остается в предыдущей части, поэтому склейка точно
        # восстанавливает исходное описание.
        split_at = boundary + 1 if boundary >= 0 else safe_end
        if split_at <= 0:
            split_at = safe_end
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    if remaining:
        chunks.append(remaining)
    return chunks


def description_delivery_plan(value: object) -> tuple[str | None, list[str]]:
    """Возвращает подпись медиа либо отдельные текстовые части описания."""
    description = normalize_description(value)
    if not description:
        return None, []
    if telegram_text_length(description) <= TELEGRAM_CAPTION_LIMIT:
        return description, []
    return None, split_telegram_text(description)


def media_album_sizes(count: int, max_group_size: int = 10) -> list[int]:
    """Делит публикацию на блоки до 10 медиа без одиночного хвостового альбома."""
    if count < 0:
        raise ValueError("count не может быть отрицательным")
    if max_group_size < 2:
        raise ValueError("max_group_size должен быть не меньше 2")
    if count == 0:
        return []
    if count == 1:
        return [1]

    sizes: list[int] = []
    remaining = count
    while remaining > max_group_size:
        group = max_group_size
        if remaining - group == 1:
            group -= 1
        sizes.append(group)
        remaining -= group
    sizes.append(remaining)
    return sizes


def chunk_media(items: Sequence[T], max_group_size: int = 10) -> list[list[T]]:
    """Возвращает упорядоченные группы медиа допустимого размера."""
    groups: list[list[T]] = []
    offset = 0
    for size in media_album_sizes(len(items), max_group_size):
        groups.append(list(items[offset : offset + size]))
        offset += size
    return groups


def description_status(info: dict) -> str:
    """Возвращает available, empty или unavailable без подмены заголовком."""
    value = normalize_description(info.get("description"))
    if value:
        return "available"
    explicit = info.get("_nuvio_description_status")
    if explicit in {"empty", "unavailable"}:
        return explicit
    if "description" in info:
        return "empty"
    return "unavailable"
