"""Сохраняет попытки отправки, не обещая идемпотентность внешнего API."""

import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from config import BASE_DIR


class DeliveryJournal:
    def __init__(self, path: Path | None = None):
        self.path = path or Path(os.environ.get("DATA_DIR", BASE_DIR)) / "delivery_journal.db"
        with self.connection() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS attempts (
                operation TEXT NOT NULL, part INTEGER NOT NULL, attempt INTEGER NOT NULL,
                recipient INTEGER NOT NULL, state TEXT NOT NULL,
                messages TEXT, updated REAL NOT NULL,
                PRIMARY KEY(operation, part, attempt))""")

    def connection(self):
        """Каждая запись подтверждается с FULL до внешнего запроса."""
        connection = sqlite3.connect(self.path, timeout=3)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return closing(connection)

    def begin(self, operation: str, part: int, attempt: int, recipient: int):
        with self.connection() as connection:
            connection.execute("""INSERT INTO attempts VALUES (?, ?, ?, ?, 'dispatch_started', NULL, ?)""",
                               (operation, part, attempt, recipient, time.time()))
            connection.commit()

    def finish(self, operation: str, part: int, attempt: int, state: str, messages=()):
        with self.connection() as connection:
            connection.execute("""UPDATE attempts SET state=?, messages=?, updated=?
                WHERE operation=? AND part=? AND attempt=?""",
                (state, json.dumps(list(messages)), time.time(), operation, part, attempt))
            connection.commit()

    def recover(self) -> int:
        """Незавершенные запросы становятся неизвестными, а не повторяемыми."""
        with self.connection() as connection:
            cursor = connection.execute("""UPDATE attempts SET state='unknown', updated=?
                WHERE state='dispatch_started'""", (time.time(),))
            connection.commit()
            return cursor.rowcount

    def uncertain(self, operation: str) -> bool:
        with self.connection() as connection:
            return connection.execute("""SELECT 1 FROM attempts WHERE operation=?
                AND state IN ('dispatch_started', 'unknown') LIMIT 1""", (operation,)).fetchone() is not None

    def prune_known(self, days: int = 180) -> int:
        """Хранит известные завершенные попытки 180 дней; неопределенные операции не истекают."""
        with self.connection() as connection:
            cursor = connection.execute("""DELETE FROM attempts WHERE updated < ? AND operation NOT IN
                (SELECT operation FROM attempts WHERE state IN ('unknown', 'dispatch_started'))""",
                (time.time() - days * 86400,))
            connection.commit()
            return cursor.rowcount


journal = DeliveryJournal()
