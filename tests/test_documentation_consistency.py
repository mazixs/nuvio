"""Проверки соответствия документации фактическому поведению кода."""

import inspect
import re
from pathlib import Path

import pytest

from utils import cache_commands


ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "technical" / "youtube-download-runbook.md"


@pytest.mark.unit
def test_adr_001_records_fast_path_supersession():
    """ADR-001 отклонил снижение качества, а быстрый путь его реализует.

    Отклонённая альтернатива «540p H.264 вместо HEVC» стала поведением по
    умолчанию (576×1024 H.264), поэтому ADR обязан это фиксировать вместе с
    датой решения и ссылкой на замеры.
    """
    adr = (
        ROOT / "docs" / "technical" / "adr-001-tiktok-audio-hevc-compatibility.md"
    ).read_text(encoding="utf-8")

    assert "2026-07-25" in adr
    assert "TIKTOK_FAST_PATH" in adr
    assert "576×1024" in adr
    assert "latency-disk-network-research.md" in adr
    # Требование ADR-001 остаётся в силе для обоих путей скачивания.
    assert "H.264" in adr


@pytest.mark.unit
def test_fast_path_flag_documents_cache_reset():
    """Смена флага не влияет на уже закэшированные ссылки — это надо сказать.

    Кэш file_id читается до скачивания и живёт 90 дней, поэтому после
    TIKTOK_FAST_PATH=false прежние URL продолжат отдавать 576×1024.
    """
    documents = {
        ".env.example": (ROOT / ".env.example").read_text(encoding="utf-8"),
        "README.md": (ROOT / "README.md").read_text(encoding="utf-8"),
        "AGENTS.md": (ROOT / "AGENTS.md").read_text(encoding="utf-8"),
    }

    for name, text in documents.items():
        assert "TIKTOK_FAST_PATH" in text, name
        assert "cache" in text.lower(), name
        assert "/cleanup_cache" in text, f"{name}: нет упоминания команды кэша"
        # /cleanup_cache снимает только просроченные записи, поэтому честный
        # способ немедленного откатa — удаление файла кэша.
        assert "telegram_cache.db" in text, f"{name}: нет способа откатить кэш"


@pytest.mark.unit
def test_instagram_fast_path_flag_is_documented():
    """Каждый флаг быстрого пути обязан быть описан во всех трёх документах.

    `config.py` — не документация: оператор ищет переменную в `.env.example`,
    README и AGENTS.md. Флаг Instagram, в отличие от TikTok, качество не меняет,
    поэтому предупреждения про кэш ему не нужно — нужен сам факт наличия.
    """
    documents = {
        ".env.example": (ROOT / ".env.example").read_text(encoding="utf-8"),
        "README.md": (ROOT / "README.md").read_text(encoding="utf-8"),
        "AGENTS.md": (ROOT / "AGENTS.md").read_text(encoding="utf-8"),
    }

    for name, text in documents.items():
        assert "INSTAGRAM_FAST_PATH" in text, name


@pytest.mark.unit
def test_cleanup_cache_command_only_removes_expired_records():
    """Команда, на которую ссылается документация, должна делать ровно то.

    /cleanup_cache вызывает cleanup_expired(ttl_days=90), то есть удаляет лишь
    записи старше TTL и НЕ сбрасывает свежие ссылки быстрого пути. Документация
    обязана описывать это без преувеличения.
    """
    assert hasattr(cache_commands, "cleanup_cache_command")

    source = inspect.getsource(cache_commands.cleanup_cache_command)
    assert "cleanup_expired(ttl_days=90)" in source

    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert 'CommandHandler("cleanup_cache", cleanup_cache_command)' in main_source


@pytest.mark.unit
def test_youtube_runbook_names_the_pinned_ytdlp_version():
    """Runbook обязан называть версию yt-dlp, на которую зажат образ.

    Пин на nightly временный, и §4.3 runbook'а требует вернуть его на стабильную.
    Если версию поднимут, не тронув документ, процедура «накатить свежий yt-dlp»
    начнёт ссылаться на версию, которой в проекте уже нет.
    """
    requirements = (ROOT / "requirements.in").read_text(encoding="utf-8")
    match = re.search(r"^yt-dlp\[default\]==(\S+)$", requirements, re.MULTILINE)
    assert match, "requirements.in: пин yt-dlp не найден"
    pinned_version = match.group(1)

    runbook = RUNBOOK.read_text(encoding="utf-8")
    assert pinned_version in runbook, (
        f"docs/technical/youtube-download-runbook.md не знает про yt-dlp {pinned_version}"
    )
    assert "scripts/update_ytdlp.py" in runbook
    for duplicate in ("AGENTS.md", "docs/PRD.md"):
        assert pinned_version in (ROOT / duplicate).read_text(encoding="utf-8")


@pytest.mark.unit
def test_youtube_runbook_records_false_negative_probes():
    """Три ловушки ложноотрицательных проб — главная ценность runbook'а.

    Из-за них августовский разбор занял сутки: `--test` подменяет размер куска на
    10 КБ, кэш `file_id` отдаёт готовый файл вместо запроса к платформе, а ролик
    короче минуты проходил при полностью сломанном скачивании. Вычистить их из
    документа при правках нельзя.
    """
    runbook = RUNBOOK.read_text(encoding="utf-8")

    assert "--test" in runbook and "10 KB" in runbook
    assert "file_id" in runbook and "cache" in runbook.lower()
    assert "minute" in runbook
    # Категория отличает смену правил выдачи от закрытого видео: без неё оператор
    # уходит проверять cookies вместо версии yt-dlp.
    assert "MEDIA_FORBIDDEN" in runbook
    assert "ACCESS_RESTRICTED" in runbook
    # Проба обязана воспроизводить штатный размер куска, иначе она врёт.
    assert "--http-chunk-size 10M" in runbook


@pytest.mark.unit
def test_youtube_403_troubleshooting_leads_to_runbook():
    """Runbook бесполезен, если на него не выводит ни один вход.

    Оператор приходит с жалобой «не качается» в troubleshooting и с вопросом
    «где документация» в оглавление, поэтому ссылки нужны в обоих местах.
    """
    relative_link = "technical/youtube-download-runbook.md"

    issues = (ROOT / "docs" / "troubleshooting" / "common-issues.md").read_text(
        encoding="utf-8"
    )
    assert "HTTP Error 403" in issues
    assert relative_link in issues

    index = (ROOT / "docs" / "index.md").read_text(encoding="utf-8")
    assert relative_link in index
