"""Общий срок и лимит размера одной подготовки, включая вложенные повторы и CLI."""

import time
import subprocess
from contextvars import ContextVar

_deadline = ContextVar("work_deadline", default=None)
_session = ContextVar("work_session", default=None)
_max_file_size = ContextVar("work_max_file_size", default=None)


def remaining(default: float) -> float:
    """Возвращает остаток бюджета без его обновления при очередном повторе."""
    check()
    deadline = _deadline.get()
    return default if deadline is None else min(default, max(0.001, deadline - time.monotonic()))


def job_size_limit() -> int | None:
    """Возвращает лимит размера, явно переданный этой работе; у бота его нет."""
    return _max_file_size.get()


def size_limit(default: int) -> int:
    """Возвращает лимит размера этой работы, а без него — лимит по умолчанию."""
    limit = job_size_limit()
    return default if limit is None else limit


def check() -> None:
    from utils.cancellation import CancelledByUser, is_cancelled

    session_id = _session.get()
    if session_id and is_cancelled(session_id):
        raise CancelledByUser("подготовка отменена")
    deadline = _deadline.get()
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("Общий срок подготовки медиа истек")


def execute(
    function,
    args,
    deadline: float,
    session_id: str | None,
    max_file_size: int | None = None,
):
    """Устанавливает бюджет внутри рабочего потока.

    `max_file_size` передаёт тот, кто отдаёт файлы не в Telegram; без него
    загрузчики держатся лимита бота. Новый поток контекст не наследует, поэтому
    лимит читается до его запуска.
    """
    deadline_token = _deadline.set(deadline)
    session_token = _session.set(session_id)
    max_file_size_token = _max_file_size.set(max_file_size)
    try:
        check()
        try:
            result = function(*args)
        except Exception:
            # yt-dlp превращает TimeoutError из хука в свою ошибку: он наследник OSError.
            check()
            raise
        check()
        return result
    finally:
        _deadline.reset(deadline_token)
        _session.reset(session_token)
        _max_file_size.reset(max_file_size_token)


def pause(seconds: float):
    """Ожидание повтора также подчиняется отмене и общему сроку."""
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        check()
        time.sleep(min(.2, until - time.monotonic(), remaining(seconds)))


def communicate(process, timeout: float):
    """Останавливает дочерний процесс при отмене или общем сроке подготовки."""
    if _deadline.get() is None:
        return process.communicate(timeout=timeout)
    local_deadline = time.monotonic() + timeout
    try:
        while True:
            check()
            left = local_deadline - time.monotonic()
            if left <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout)
            try:
                return process.communicate(timeout=min(.25, left, remaining(timeout)))
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        process.kill()
        process.communicate()
        raise
