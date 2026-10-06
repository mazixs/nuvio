"""Отмена длительных задач по идентификатору сессии.

Кнопка отмены должна прерывать работу, а не прятать её результат: иначе сервер
всё равно скачивает файл, который пользователь уже не ждёт, и вся экономия
диска и трафика теряется на первом же передумавшем.

Задачи выполняются в пуле потоков, а ``contextvars`` через
``loop.run_in_executor`` не передаются, поэтому признак отмены живёт в реестре
по ``session_id`` — он и так есть у каждой загрузки. До самого yt-dlp отмена
доходит через ``progress_hooks``: это единственная точка, из которой его можно
остановить, и исключение оттуда он пропускает наружу.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any


__all__ = [
    "CancelledByUser",
    "cancellation_hook",
    "forget_cancellation",
    "is_cancelled",
    "request_cancellation",
]


class CancelledByUser(BaseException):
    """Задача прервана по кнопке отмены.

    Наследуется от ``BaseException`` намеренно, как ``asyncio.CancelledError``:
    это не ошибка, а сигнал управления. Обработчики платформ ловят широкий
    ``except Exception`` и показали бы пользователю код ошибки вместо честного
    «Отменено», а отмену нужно довести до одного места без правки каждого из
    них. Проверено, что yt-dlp пропускает исключение из progress hook как есть,
    не заворачивая его в ``DownloadError``.
    """


# Сколько помнить отмену. Загрузчик видит признак за секунды, поэтому окно
# выбрано с большим запасом: оно нужно лишь чтобы реестр не рос вечно.
CANCELLATION_MEMORY_SECONDS = 600

_CANCELLED: dict[str, float] = {}
_LOCK = threading.Lock()


def _prune(now: float) -> None:
    """Убирает отметки, которые уже никому не нужны. Вызывать под блокировкой."""
    stale = [
        session
        for session, marked_at in _CANCELLED.items()
        if now - marked_at > CANCELLATION_MEMORY_SECONDS
    ]
    for session in stale:
        del _CANCELLED[session]


def request_cancellation(session_id: str) -> None:
    """Помечает сессию отменённой."""
    if not session_id:
        return
    now = time.monotonic()
    with _LOCK:
        _prune(now)
        _CANCELLED[session_id] = now


def is_cancelled(session_id: str) -> bool:
    """Сообщает, просил ли пользователь прервать эту сессию."""
    if not session_id:
        return False
    now = time.monotonic()
    with _LOCK:
        _prune(now)
        return session_id in _CANCELLED


def forget_cancellation(session_id: str) -> None:
    """Убирает сессию из реестра, чтобы он не рос бесконечно."""
    if not session_id:
        return
    with _LOCK:
        _CANCELLED.pop(session_id, None)


def cancellation_hook(session_id: str) -> Callable[[dict[str, Any]], None]:
    """Возвращает progress hook для yt-dlp, прерывающий отменённую загрузку."""

    def _hook(_status: dict[str, Any]) -> None:
        from utils.work_budget import check

        check()
        if is_cancelled(session_id):
            raise CancelledByUser(f"загрузка сессии {session_id} отменена")

    return _hook
