"""Пропавший файл обязан называться пропавшим файлом.

На проде отправка исчезнувшего файла давала пользователю код
`TG-NETWORK-372275`, а в лог — `TypeError: Object of type PosixPath is not JSON
serializable`. Механика: PTB превращает путь в ссылку только если
`path.is_file()` истинно, иначе объект уходит в тело запроса как есть и падает
на сериализации. Диагноз получался ложным дважды — «сеть» вместо «нет файла», —
и вдобавок бессмысленно повторялся три раза: результат не мог измениться.
"""

from pathlib import Path

import pytest

from utils import telegram_utils


pytestmark = pytest.mark.unit


class _Message:
    def __init__(self):
        self.video_calls = 0

    async def reply_video(self, **_kwargs):
        self.video_calls += 1
        raise AssertionError("отправка не должна начинаться без файла")

    async def reply_audio(self, **_kwargs):
        raise AssertionError("отправка не должна начинаться без файла")

    async def reply_document(self, **_kwargs):
        raise AssertionError("отправка не должна начинаться без файла")


class _Query:
    def __init__(self):
        self.message = _Message()
        self.texts: list[str] = []

    async def edit_message_text(self, text, **_kwargs):
        self.texts.append(text)


@pytest.fixture(autouse=True)
def local_bot_api(monkeypatch):
    """Баг живёт только в локальном режиме — это и есть конфигурация прода.

    В облачном режиме код открывает файл через `open("rb")` и честно получает
    `FileNotFoundError`. В локальном он отдаёт PTB `Path`, потому что
    `resolve()` файловую систему не трогает, — и падение уезжает в JSON.
    Без этой фикстуры тесты проходят, ничего не проверяя.
    """
    monkeypatch.setattr(telegram_utils, "TELEGRAM_LOCAL_MODE", True)


def _send(missing_path: Path):
    import asyncio

    query = _Query()
    result = asyncio.run(
        telegram_utils.send_single_file(
            query,
            missing_path,
            "token123",
            {"platform": "youtube", "url": "https://example.com/v", "session_id": "s1"},
        )
    )
    return result, query


def test_missing_file_fails_without_calling_telegram(tmp_path):
    """Ни одной попытки отправки: отправлять нечего."""
    result, query = _send(tmp_path / "нет-такого.mp4")

    assert result is False
    assert query.message.video_calls == 0


def test_user_sees_a_file_error_not_a_network_error(tmp_path):
    """Код ошибки должен указывать на файл, иначе диагноз ведёт не туда."""
    _result, query = _send(tmp_path / "нет-такого.mp4")

    assert query.texts, "пользователю ничего не сказали"
    assert "FILE-FILE" in query.texts[-1]


def test_existing_file_is_not_rejected(tmp_path):
    """Проверка не должна мешать нормальной отправке."""
    present = tmp_path / "есть.mp4"
    present.write_bytes(b"data")

    assert telegram_utils._file_ready_to_send(present) is True
    assert telegram_utils._file_ready_to_send(tmp_path / "нет.mp4") is False


def test_empty_file_counts_as_missing(tmp_path):
    """Файл нулевого размера Telegram всё равно не примет."""
    empty = tmp_path / "пусто.mp4"
    empty.touch()

    assert telegram_utils._file_ready_to_send(empty) is False
