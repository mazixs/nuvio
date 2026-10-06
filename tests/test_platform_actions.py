"""Тесты чистых платформенных решений и границ покрытия кэша."""

import ast
from pathlib import Path

import pytest

from utils.platform_actions import (
    cache_key_for_format_selection,
    cache_key_for_main_action,
)


ROOT = Path(__file__).resolve().parents[1]
TELEGRAM_UTILS_PATH = ROOT / "utils" / "telegram_utils.py"

# Способы прочитать кэш file_id в обработчиках: прямой вызов и хелпер аудио.
_CACHE_READER_NAMES = (
    "_cache_get",
    "telegram_cache.get",
    "_deliver_cached_audio",
    "_deliver_cached_video",
)


def _called_name(node: ast.Call) -> str:
    """Возвращает читаемое имя вызываемого объекта."""
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return f"{func.value.id}.{func.attr}"
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _cache_readers_in_actions(actions: tuple[str, ...]) -> list[str]:
    """Ищет чтение кэша внутри ветвей `match` указанных действий.

    Проверка идёт по исходнику, потому что решение «читать кэш» видно только
    в обработчике: чистая функция ключей о читателях ничего не знает.
    """
    tree = ast.parse(TELEGRAM_UTILS_PATH.read_text(encoding="utf-8"))
    found: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.match_case):
            continue
        pattern = node.pattern
        if isinstance(pattern, ast.MatchOr):
            constants = [
                value.value.value
                for value in pattern.patterns
                if isinstance(value, ast.MatchValue)
                and isinstance(value.value, ast.Constant)
            ]
        elif (
            isinstance(pattern, ast.MatchValue)
            and isinstance(pattern.value, ast.Constant)
        ):
            constants = [pattern.value.value]
        else:
            constants = []
        matched_actions = [action for action in constants if action in actions]
        if not matched_actions:
            continue

        for statement in node.body:
            for inner in ast.walk(statement):
                if isinstance(inner, ast.Call) and (
                    _called_name(inner) in _CACHE_READER_NAMES
                ):
                    found.extend(
                        f"{action}: {_called_name(inner)}" for action in matched_actions
                    )

    return found


def test_main_action_cache_keys_are_explicit():
    assert cache_key_for_main_action("tiktok", "tiktok_download") == "direct_video"
    assert cache_key_for_main_action("instagram", "instagram_download") == "direct_video"
    assert cache_key_for_main_action("tiktok", "tiktok_download_desc") == "direct_video"
    assert cache_key_for_main_action("instagram", "instagram_download_desc") == "direct_video"
    assert cache_key_for_main_action("youtube", "tg_video") == "tg_video"
    assert cache_key_for_main_action("youtube", "audio_m4a") == "audio_m4a"


def test_format_selection_cache_keys_preserve_scope():
    """Ключ есть только у combined: остальные ветви меню удалены вместе с кнопками."""
    assert cache_key_for_format_selection("combined", "18") == "combined:18"
    assert cache_key_for_format_selection("combined", "299+140") == "combined:299+140"
    assert cache_key_for_format_selection("audio_only", "140") is None
    assert cache_key_for_format_selection("video_only", "137") is None
    assert cache_key_for_format_selection("best", "ignored") is None


def test_main_action_cache_keys_cover_rutube_and_vk():
    assert cache_key_for_main_action("rutube", "rutube_download") == "direct_video"
    assert cache_key_for_main_action("vk", "vk_download") == "direct_video"


def test_main_action_cache_keys_cover_audio_actions():
    assert cache_key_for_main_action("tiktok", "tiktok_audio") == "tiktok_audio"
    assert cache_key_for_main_action("instagram", "instagram_audio") == "instagram_audio"
    assert cache_key_for_main_action("rutube", "rutube_audio") == "rutube_audio"
    assert cache_key_for_main_action("vk", "vk_audio") == "vk_audio"


@pytest.mark.unit
def test_listed_main_actions_have_a_key_for_writing_to_cache():
    """Проверяет ТОЛЬКО наличие ключа, под которым запись попадёт в кэш.

    Граница проверки: наличие ЧИТАТЕЛЯ кэша здесь не проверяется и из
    зелёного результата не следует. Действие может иметь ключ и при этом
    писать в кэш «в одну сторону» — так сейчас у YouTube, см.
    test_youtube_cache_is_write_only.
    """
    actions = [
        ("tiktok", "tiktok_download"),
        ("tiktok", "tiktok_audio"),
        ("instagram", "instagram_download"),
        ("instagram", "instagram_audio"),
        ("rutube", "rutube_download"),
        ("rutube", "rutube_audio"),
        ("vk", "vk_download"),
        ("vk", "vk_audio"),
        ("youtube", "tg_video"),
        ("youtube", "audio_m4a"),
    ]

    without_key = [
        action for platform, action in actions
        if cache_key_for_main_action(platform, action) is None
    ]

    assert without_key == []


@pytest.mark.unit
def test_youtube_audio_m4a_key_comes_from_the_pure_function():
    """Ключ записи `audio_m4a` больше не литерал в обработчике.

    Пока он задавался строкой прямо в вызове send_file, ключи чтения и записи
    могли разойтись незамеченными. Значение оставлено прежним, иначе уже
    записанные в кэш строки стали бы недостижимыми.
    """
    assert cache_key_for_main_action("youtube", "audio_m4a") == "audio_m4a"

    source = TELEGRAM_UTILS_PATH.read_text(encoding="utf-8")
    assert 'cache_format_id="audio_m4a"' not in source


@pytest.mark.unit
def test_youtube_main_actions_read_cache():
    """YouTube обязан читать кэш, а не только писать в него.

    По аналитике проекта YouTube даёт большую часть запросов, поэтому
    отсутствие читателя означало гарантированный промах на каждом повторе
    при занятом 90-дневном TTL.
    """
    readers = _cache_readers_in_actions(("tg_video", "audio_m4a"))

    assert any(reader.startswith("tg_video:") for reader in readers), readers
    assert any(reader.startswith("audio_m4a:") for reader in readers), readers


@pytest.mark.unit
def test_format_selection_path_reads_cache():
    """Выбор конкретного формата YouTube тоже обязан проверять кэш.

    Ключ `combined:<id>` пишется в кэш, поэтому без чтения он так же
    расходовал бы TTL без единого попадания.
    """
    tree = ast.parse(TELEGRAM_UTILS_PATH.read_text(encoding="utf-8"))

    enclosing: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = {
            _called_name(inner)
            for inner in ast.walk(node)
            if isinstance(inner, ast.Call)
        }
        if "_cache_format_id_for_format_selection" in calls:
            enclosing.append(node.name)
            assert _CACHE_READER_NAMES[0] in calls or any(
                name in calls for name in _CACHE_READER_NAMES
            ), f"{node.name} считает ключ формата, но кэш не читает"

    assert enclosing, "не найден обработчик выбора формата"


@pytest.mark.unit
def test_cache_reader_detection_is_not_vacuous():
    """Контроль инструмента: в ветвях с чтением кэша он читателей находит."""
    readers = _cache_readers_in_actions(("tiktok_download", "tiktok_audio"))

    assert "tiktok_download: _cache_get" in readers
    assert "tiktok_audio: _deliver_cached_audio" in readers
