"""Необязательные операции кеша, вынесенные из event loop."""

import logging
import asyncio
import sqlite3

from utils.db_worker import run_cache_db

logger = logging.getLogger(__name__)


async def cache_call(function, *args, **kwargs):
    """Ошибка кеша становится промахом, но не подавляет ошибки авторизации."""
    try:
        return await run_cache_db(function, *args, **kwargs)
    except (sqlite3.Error, OSError, ValueError, asyncio.TimeoutError):
        logger.warning("Операция кеша недоступна: %s", function.__name__, exc_info=True)
        return None
