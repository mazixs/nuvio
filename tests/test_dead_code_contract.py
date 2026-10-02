"""Контракт отсутствия подтверждённого мёртвого кода."""

import dataclasses
from pathlib import Path

import pytest

import messages
from utils import telegram_utils, tiktok_fast_path
from utils.video_cache import CachedVideo


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.unit
def test_url_handoff_leftovers_are_removed():
    """Остатки первой, невзлетевшей попытки доставки по URL не возвращаются.

    URL_HANDOFF_LIMIT_BYTES, fits_url_handoff и FastMedia.cover использовались
    только тестами. Работающая доставка по ссылке живёт в utils/url_delivery.py
    и меряет лимит сама — дублировать её константы в разборе ответа резолвера
    незачем.
    """
    assert not hasattr(tiktok_fast_path, "URL_HANDOFF_LIMIT_BYTES")
    assert not hasattr(tiktok_fast_path, "fits_url_handoff")

    field_names = {
        field.name for field in dataclasses.fields(tiktok_fast_path.FastMedia)
    }
    assert "cover" not in field_names

    guide = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "лимита передачи по URL" not in guide


def test_obsolete_message_constants_are_removed():
    obsolete_names = {
        "TOO_LARGE_FILE_MESSAGE",
        "ERROR_YTDLP",
        "ERROR_GENERIC",
        "ERROR_SENDING_FILE",
        "AUDIO_ONLY_LABEL",
        "VIDEO_ONLY_LABEL",
        "COMBINED_LABEL",
        # Кнопки убраны при переходе на каскадное меню: «Лучшее качество» и
        # «Лучшее аудио» дублировали первый экран, MP3 требовал перекодирования
        # там, где родная дорожка уже пригодна.
        "BEST_QUALITY_LABEL",
        "BEST_AUDIO_LABEL",
        "MP3_MIN_LABEL",
        # Формат субтитров теперь выбирает пользователь, поэтому кнопки с
        # зашитым SRT в подписи больше нет.
        "BTN_SUBTITLES",
    }

    assert obsolete_names.isdisjoint(vars(messages))


def test_flat_format_menu_is_not_back():
    """Плоское меню с лимитами «не более 3» и дедупликацией по тексту удалено."""
    assert not hasattr(telegram_utils, "_build_youtube_more_menu")

    from utils import media_processor

    assert not hasattr(media_processor, "convert_to_mp3_with_compression")

    source = (ROOT / "utils" / "telegram_utils.py").read_text(encoding="utf-8")
    for gone in ('"mp3_min"', '"audio_best"', '"video_only":\n'):
        assert gone not in source, gone


def test_obsolete_cache_helpers_are_removed():
    assert not hasattr(telegram_utils, "_try_send_cached")
    assert not hasattr(CachedVideo, "to_dict")


def test_unused_photo_post_helpers_are_removed():
    """Документный откат фото идет через общий поэлементный путь альбома."""
    assert not hasattr(telegram_utils, "_send_photo_path_as_document")

    source = (ROOT / "utils" / "telegram_utils.py").read_text(encoding="utf-8")
    for gone in ('"_description_sent"', '"_description_caption"'):
        assert gone not in source, gone


def test_unused_pytest_network_scaffolding_is_removed():
    conftest = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    pytest_ini = (ROOT / "pytest.ini").read_text(encoding="utf-8")

    assert "--run-slow" not in conftest
    assert "--run-network" not in conftest
    assert "network:" not in pytest_ini
    assert "slow:" not in pytest_ini
