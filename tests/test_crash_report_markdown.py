"""Краш-репорт уходит админам как markdown-файл и не ломается на своём же тексте.

В репорт попадают тексты исключений и строки исходников, поэтому проверяется не
только разметка, но и устойчивость: вертикальная черта не должна закрывать
столбец таблицы, а обратные кавычки внутри traceback — разрывать блок кода.

Отдельно проверяется диагностика: версия yt-dlp с каналом обновлений и хвост его
вывода. Без них отказ приходилось объяснять по SSH, потому что «каким клиентом
качали и что ответила платформа» живёт только в предупреждениях yt-dlp.
"""

import asyncio
from types import SimpleNamespace

import pytest

from utils import cookie_health, download_report, telegram_utils


SESSION_ID = "42_f1336a68"


@pytest.fixture(autouse=True)
def clean_registry():
    """Регистр диагностики общий на процесс, поэтому чистится вокруг теста."""
    download_report._OUTPUT.clear()
    download_report._DELIVERED.clear()
    yield
    download_report._OUTPUT.clear()
    download_report._DELIVERED.clear()


class _DummyBot:
    def __init__(self):
        self.sent_documents = []

    async def send_document(self, chat_id, document, filename, caption):
        self.sent_documents.append(
            {
                "chat_id": chat_id,
                "text": document.getvalue().decode("utf-8"),
                "filename": filename,
                "caption": caption,
            }
        )


def _send(monkeypatch, exc, *, url="https://www.youtube.com/watch?v=UyXbRBxS2RI"):
    bot = _DummyBot()
    monkeypatch.setattr(telegram_utils, "_bot_instance", bot)
    monkeypatch.setattr(telegram_utils, "ADMIN_IDS", [1])
    monkeypatch.setattr(
        telegram_utils,
        "check_cookie_health",
        lambda platform: cookie_health.CookieHealthResult(
            platform, "valid", "auth cookies are active", 0.0, 2, 2
        ),
    )
    asyncio.run(
        telegram_utils._log_platform_failure(
            platform="youtube",
            stage="format_download",
            url=url,
            error_code="YT-MEDIA_FO-ABC123",
            exc=exc,
            session_id=SESSION_ID,
        )
    )
    return bot.sent_documents[0]


def _table_rows(text: str) -> list[str]:
    """Возвращает строки таблицы фактов — от заголовка до конца таблицы."""
    lines = text.splitlines()
    rows = []
    for line in lines[lines.index("| Поле | Значение |") :]:
        if not line.startswith("|"):
            break
        rows.append(line)
    return rows


def _section(text: str, header: str) -> str:
    """Возвращает тело секции репорта без заголовка и обрамляющих пустых строк."""
    body = text.split(f"{header}\n", 1)[1]
    return body.split("\n## ", 1)[0].strip()


@pytest.mark.unit
def test_report_is_a_markdown_file(monkeypatch):
    document = _send(monkeypatch, RuntimeError("boom"))

    assert document["filename"] == "crash_YT-MEDIA_FO-ABC123.md"
    assert document["text"].startswith("# 🔴 Краш-репорт — `YT-MEDIA_FO-ABC123`")


@pytest.mark.unit
def test_report_carries_facts_as_a_table(monkeypatch):
    text = _send(monkeypatch, RuntimeError("boom"))["text"]

    assert "| Поле | Значение |" in text
    assert "| Платформа | youtube |" in text
    assert "| Этап | format_download |" in text
    assert "| Сессия | `42_f1336a68` |" in text
    assert "| Cookies | valid |" in text
    assert "<https://www.youtube.com/watch?v=UyXbRBxS2RI>" in text
    # Время нужно, чтобы искать событие в `logs/bot.log` по метке, а не по глазам.
    assert "| Время (UTC) | " in text


@pytest.mark.unit
def test_exception_and_traceback_live_in_code_blocks(monkeypatch):
    text = _send(monkeypatch, RuntimeError("boom"))["text"]

    assert "## Исключение" in text
    assert "```text\nRuntimeError: boom\n```" in text
    assert "## Traceback" in text
    assert "```python" in text


@pytest.mark.unit
def test_pipe_in_message_does_not_break_the_table(monkeypatch):
    text = _send(monkeypatch, RuntimeError("a | b"), url="https://x.test/a|b")["text"]

    for line in _table_rows(text):
        assert line.count("|") - line.count("\\|") == 3, line


@pytest.mark.unit
def test_backticks_in_message_do_not_break_the_code_block(monkeypatch):
    text = _send(monkeypatch, RuntimeError("```oops```"))["text"]

    assert "````text\nRuntimeError: ```oops```\n````" in text


@pytest.mark.unit
def test_missing_url_and_session_are_marked_plainly(monkeypatch):
    bot = _DummyBot()
    monkeypatch.setattr(telegram_utils, "_bot_instance", bot)
    monkeypatch.setattr(telegram_utils, "ADMIN_IDS", [1])

    asyncio.run(
        telegram_utils._notify_admins_crash(
            error_code="BOT-UNEXPECT-ABC123",
            platform="bot",
            stage="callback",
            url=None,
            exc=RuntimeError("boom"),
        )
    )

    text = bot.sent_documents[0]["text"]
    assert "| Ссылка | N/A |" in text
    assert "| Сессия | N/A |" in text


@pytest.mark.unit
def test_report_names_the_ytdlp_version_and_update_method(monkeypatch):
    """Отчет показывает версию и способ ее смены."""
    monkeypatch.setattr(
        telegram_utils, "get_installed_yt_dlp_version", lambda: "2026.8.18.122307.dev0"
    )

    text = _send(monkeypatch, RuntimeError("boom"))["text"]

    assert "| Версия yt-dlp | 2026.8.18.122307.dev0 |" in text
    assert "| Обновление | через новый образ |" in text


@pytest.mark.unit
def test_undetected_ytdlp_version_is_marked_plainly(monkeypatch):
    """Пустая ячейка не отличила бы «нет версии» от «не смогли прочитать»."""
    monkeypatch.setattr(telegram_utils, "get_installed_yt_dlp_version", lambda: None)

    text = _send(monkeypatch, RuntimeError("boom"))["text"]

    assert "| Версия yt-dlp | N/A |" in text
    assert "| Версия yt-dlp |  |" not in text


@pytest.mark.unit
def test_report_carries_the_ytdlp_output_tail(monkeypatch):
    download_report.record_output(None, "[youtube] Чужая сессия")
    download_report.record_output(
        SESSION_ID, "WARNING: [youtube] Some formats require a GVS PO Token"
    )
    download_report.record_output(SESSION_ID, "ERROR: unable to download: HTTP 403")

    text = _send(monkeypatch, RuntimeError("boom"))["text"]

    assert "## Последние строки yt-dlp" in text
    assert _section(text, "## Последние строки yt-dlp") == (
        "```text\n"
        "WARNING: [youtube] Some formats require a GVS PO Token\n"
        "ERROR: unable to download: HTTP 403\n"
        "```"
    )


@pytest.mark.unit
def test_empty_tail_is_prose_instead_of_an_empty_code_block(monkeypatch):
    text = _send(monkeypatch, RuntimeError("boom"))["text"]

    section = _section(text, "## Последние строки yt-dlp")
    assert section == "Вывода yt-dlp по этой сессии нет: до его запуска дело не дошло."
    assert "```" not in section


@pytest.mark.unit
def test_tail_of_another_session_stays_out_of_the_report(monkeypatch):
    download_report.record_output("7_other", "WARNING: чужая сессия")

    text = _send(monkeypatch, RuntimeError("boom"))["text"]

    assert "чужая сессия" not in text


@pytest.mark.unit
def test_tail_is_snapshotted_before_the_session_is_forgotten(monkeypatch):
    """Хвост снимается в момент отказа, а не когда репорт дойдёт до отправки.

    Репорт собирает фоновая задача, и она ждёт пробу cookies, а обработчик за
    это время уничтожает сессию вместе с её записями в регистре. Читая регистр
    позже, репорт нашёл бы пусто — то есть ровно на аварии остался бы без
    объяснения аварии.
    """
    bot = _DummyBot()
    monkeypatch.setattr(telegram_utils, "_bot_instance", bot)
    monkeypatch.setattr(telegram_utils, "ADMIN_IDS", [1])
    monkeypatch.setattr(
        telegram_utils,
        "check_cookie_health",
        lambda platform: cookie_health.CookieHealthResult(
            platform, "valid", "auth cookies are active", 0.0, 2, 2
        ),
    )
    download_report.record_output(SESSION_ID, "ERROR: unable to download: HTTP 403")

    async def scenario():
        telegram_utils._schedule_platform_failure_log(
            platform="youtube",
            stage="download_video",
            url=None,
            error_code="YT-MEDIA_FO-ABC123",
            exc=RuntimeError("boom"),
            session_id=SESSION_ID,
        )
        download_report.forget(SESSION_ID)
        pending = [
            task for task in asyncio.all_tasks() if task is not asyncio.current_task()
        ]
        await asyncio.gather(*pending)

    asyncio.run(scenario())

    assert "ERROR: unable to download: HTTP 403" in bot.sent_documents[0]["text"]


@pytest.mark.unit
def test_session_token_is_not_passed_where_session_id_is_expected():
    """Токен помечает клавиатуру, `session_id` — файлы и хвост вывода.

    Перепутать их значит молча получить пустую диагностику: регистр ключуется по
    `session_id`, и по токену в нём никогда ничего не найдётся.
    """
    context = SimpleNamespace(
        user_data={
            telegram_utils._SESSION_STORE_KEY: {
                "tok3n": {"session_id": SESSION_ID, "url": "https://x.test/v"}
            }
        }
    )

    assert telegram_utils._session_id_of(context, "tok3n") == SESSION_ID
    assert telegram_utils._session_id_of(context, "нет такого") is None
    assert telegram_utils._session_id_of(context, None) is None
