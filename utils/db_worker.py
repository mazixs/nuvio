"""Ограниченный пул для целых операций SQLite вне event loop."""

import asyncio
import contextvars
import functools
import sys
import weakref
from concurrent.futures import ThreadPoolExecutor

_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="nuvio-db")
_LIMITS = weakref.WeakKeyDictionary()
_CACHE_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nuvio-cache")
_CACHE_LIMITS = weakref.WeakKeyDictionary()


def _execute(context, function, args, kwargs):
    try:
        return context.run(function, *args, **kwargs)
    finally:
        # Соединение не переживает смену БД в тестах и принадлежит своему потоку.
        module = sys.modules.get("utils.analytics_db")
        if module is not None:
            module.close_connection()


async def _run(pool, limits, capacity, function, args, kwargs):
    """Выполняет транзакцию целиком, ограничивая число ожидающих операций."""
    loop = asyncio.get_running_loop()
    limit = limits.setdefault(loop, asyncio.Semaphore(capacity))
    async with limit:
        task = loop.run_in_executor(
            pool,
            functools.partial(_execute, contextvars.copy_context(), function, args, kwargs),
        )
        # Отмена ожидания не освобождает слот, пока работа реально не закончилась.
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            # Извлекаем исключение рабочего потока даже при отмененном ожидании.
            if not task.cancelled():
                task.exception()
            raise


async def run_db(function, *args, **kwargs):
    """Авторитетные проверки доступа и журнал не ждут блокировки кеша."""
    return await _run(_POOL, _LIMITS, 32, function, args, kwargs)


async def run_cache_db(function, *args, **kwargs):
    """У кеша свой ограниченный пул, независимый от доступа и журнала."""
    return await _run(_CACHE_POOL, _CACHE_LIMITS, 8, function, args, kwargs)


def shutdown_db_worker():
    """Дожидается операций при штатном завершении процесса."""
    _POOL.shutdown(wait=True, cancel_futures=True)
    _CACHE_POOL.shutdown(wait=True, cancel_futures=True)
