"""Общий срок одной подготовки, включая вложенные повторы и CLI."""

import time
import subprocess
from contextvars import ContextVar

_deadline = ContextVar("work_deadline", default=None)
_session = ContextVar("work_session", default=None)


def remaining(default: float) -> float:
    """Возвращает остаток бюджета без его обновления при очередном повторе."""
    check()
    deadline = _deadline.get()
    return default if deadline is None else min(default, max(0.001, deadline - time.monotonic()))


def check() -> None:
    from utils.cancellation import CancelledByUser, is_cancelled

    session_id = _session.get()
    if session_id and is_cancelled(session_id):
        raise CancelledByUser("подготовка отменена")
    deadline = _deadline.get()
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("Общий срок подготовки медиа истек")


def execute(function, args, deadline: float, session_id: str | None):
    """Устанавливает бюджет внутри рабочего потока."""
    deadline_token = _deadline.set(deadline)
    session_token = _session.set(session_id)
    try:
        check()
        result = function(*args)
        check()
        return result
    finally:
        _deadline.reset(deadline_token)
        _session.reset(session_token)


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
