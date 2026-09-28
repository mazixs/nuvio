"""Кэш `file_id` не должен запоминать чужой формат.

Каскад фолбеков при 403 молча подменяет формат: запрос `137+140` уезжает в
`bestvideo+bestaudio`, а файл возвращается как запрошенный. Запись живёт 90
дней, поэтому кнопка «1080p» могла навсегда отдавать 240p. Здесь проверяется,
что ключом становится формат, который загрузчик реально принёс.

Обратная сторона важна не меньше: загрузчик пишет в регистр обычный id формата
(`137+140`), а ключи кэша бывают двух видов — `combined:{format_id}` с обещанием
конкретного формата и корзины вроде `tg_video`. Приняв id за подмену корзины,
кэш этих кнопок перестал бы находиться вовсе.
"""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from utils import download_report, telegram_utils
from utils.url_delivery import UrlHandoff

pytestmark = pytest.mark.unit

SESSION_ID = "42_9c1f4b0e"
URL = "https://www.youtube.com/watch?v=UyXbRBxS2RI"
FORMAT_KEY = "combined:137+140"
BUCKET_KEY = "tg_video"


@pytest.fixture(autouse=True)
def clean_registry():
    download_report._OUTPUT.clear()
    download_report._DELIVERED.clear()
    yield
    download_report._OUTPUT.clear()
    download_report._DELIVERED.clear()


@pytest.fixture
def stored(monkeypatch):
    """Перехватывает запись в кэш: для проверки ключа SQLite не нужен."""
    records = []
    monkeypatch.setattr(
        telegram_utils.telegram_cache, "set", lambda cached: records.append(cached)
    )
    return records


def _message():
    media = SimpleNamespace(
        file_id="AGADvideo", file_unique_id="unique", file_size=2048, duration=11
    )
    return SimpleNamespace(video=media, audio=None, document=None)


def _cache(session_id=SESSION_ID, requested=FORMAT_KEY):
    telegram_utils._cache_sent_media(
        _message(), URL, "youtube", requested, None, session_id
    )


def _keys(records):
    return [record.format_id for record in records]


def _warnings(caplog):
    return [record for record in caplog.records if record.levelno == logging.WARNING]


def test_cache_key_follows_the_delivered_format(stored):
    """Фолбек принёс своё — под ним запись и ложится, а не под запрошенным."""
    download_report.record_delivered_format(SESSION_ID, "bestvideo+bestaudio")

    _cache()

    assert _keys(stored) == ["combined:bestvideo+bestaudio"]
    assert stored[0].file_id == "AGADvideo"


def test_requested_key_survives_when_the_format_matches(stored):
    """Обычная удачная загрузка приносит ровно запрошенный формат."""
    download_report.record_delivered_format(SESSION_ID, "137+140")

    _cache()

    assert _keys(stored) == [FORMAT_KEY]


def test_bucket_key_is_never_replaced_by_a_format_id(stored):
    """`tg_video` значит «то, что бот выбрал», и подменять его нечем."""
    download_report.record_delivered_format(SESSION_ID, "137+140")

    _cache(requested=BUCKET_KEY)

    assert _keys(stored) == [BUCKET_KEY]


def test_empty_registry_keeps_the_requested_key(stored):
    """Остальные платформы в регистр не пишут, и ломать им кэш нельзя."""
    _cache()

    assert _keys(stored) == [FORMAT_KEY]


def test_other_session_does_not_leak_into_the_key(stored):
    download_report.record_delivered_format("11_someone_else", "bestvideo+bestaudio")

    _cache()

    assert _keys(stored) == [FORMAT_KEY]


def test_unknown_session_ignores_the_shared_record(stored):
    """Записи вне сессии лежат под общим ключом, и доверять им нечего."""
    download_report.record_delivered_format(None, "bestvideo+bestaudio")

    _cache(session_id=None)

    assert _keys(stored) == [FORMAT_KEY]


def test_substituted_format_is_logged_as_a_warning(stored, caplog):
    """Расхождение — признак сработавшего фолбека, админ должен его видеть."""
    download_report.record_delivered_format(SESSION_ID, "bestvideo+bestaudio")

    with caplog.at_level(logging.WARNING, logger="utils.telegram_utils"):
        _cache()

    assert len(_warnings(caplog)) == 1
    message = _warnings(caplog)[0].getMessage()
    assert "137+140" in message
    assert "bestvideo+bestaudio" in message


def test_matching_format_is_not_logged_as_a_warning(stored, caplog):
    download_report.record_delivered_format(SESSION_ID, "137+140")

    with caplog.at_level(logging.WARNING, logger="utils.telegram_utils"):
        _cache()

    assert _warnings(caplog) == []


def test_link_handoff_keeps_the_requested_key(stored):
    """Ссылка ведёт на запрошенный формат, а запись в регистре может быть старой.

    Загрузчик в этом пути не участвует вовсе: если предыдущая попытка той же
    сессии свалилась в фолбек, её формат к отданной ссылке отношения не имеет.
    """
    download_report.record_delivered_format(SESSION_ID, "bestvideo+bestaudio")
    reply_video = AsyncMock(return_value=_message())

    delivered = asyncio.run(
        telegram_utils._deliver_by_url(
            SimpleNamespace(message=SimpleNamespace(reply_video=reply_video)),
            UrlHandoff(
                url="https://rr3---sn-x.googlevideo.com/v.mp4",
                kind="video",
                size=2048,
            ),
            URL,
            "youtube",
            FORMAT_KEY,
        )
    )

    assert delivered.state == "delivered"
    assert _keys(stored) == [FORMAT_KEY]
