"""Сохраняемая политика очистки с повторной проверкой внутри транзакции."""

import time
from datetime import UTC, datetime, timedelta

from utils import analytics_db

ENABLED = "cache_auto_cleanup_enabled"
DAYS = "cache_retention_days"
LAST = "cache_last_cleanup"


def get_cache_policy() -> dict:
    """Отсутствие настройки означает хранение без срока."""
    with analytics_db._cursor_read() as cursor:
        rows = cursor.execute("SELECT key, value FROM settings WHERE key IN (?, ?, ?)",
                              (ENABLED, DAYS, LAST)).fetchall()
    values = dict(rows)
    return {"enabled": values.get(ENABLED) == "true",
            "days": int(values.get(DAYS, "90")), "last_cleanup": values.get(LAST)}


def set_cache_policy(enabled: bool, days: int) -> None:
    """Обе настройки изменяются атомарно; включение всегда явное."""
    if isinstance(days, bool) or not 1 <= days <= 36500:
        raise ValueError("Срок должен быть от 1 до 36500 дней")
    with analytics_db._cursor_write() as cursor:
        for key, value in ((ENABLED, "true" if enabled else "false"), (DAYS, str(days))):
            cursor.execute("""INSERT INTO settings VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, datetime.now(UTC).isoformat()))


def cleanup_cache(cache) -> int:
    """Блокирует изменение политики до завершения выбранной очистки."""
    connection = analytics_db._get_connection()
    connection.execute("ATTACH DATABASE ? AS media_cache", (str(cache.db_path),))
    try:
        with analytics_db._cursor_write() as cursor:
            values = dict(cursor.execute("SELECT key, value FROM settings WHERE key IN (?, ?)",
                                         (ENABLED, DAYS)).fetchall())
            if values.get(ENABLED) != "true":
                return 0
            days = int(values.get(DAYS, "90"))
            threshold = time.time() - days * 86400
            # Явное удаление зависимостей работает и для соединения без внешних ключей.
            predicate = "SELECT id FROM media_cache.manifests WHERE last_used < ?"
            cursor.execute(f"DELETE FROM media_cache.artifact_aliases WHERE manifest_id IN ({predicate})", (threshold,))
            cursor.execute(f"DELETE FROM media_cache.manifest_items WHERE manifest_id IN ({predicate})", (threshold,))
            removed = cursor.execute("DELETE FROM media_cache.manifests WHERE last_used < ?", (threshold,)).rowcount
            cursor.execute("""DELETE FROM media_cache.artifacts WHERE last_used < ?
                AND id NOT IN (SELECT artifact_id FROM media_cache.manifest_items)""", (threshold,))
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            removed += cursor.execute("DELETE FROM media_cache.video_cache WHERE cached_at < ?", (cutoff,)).rowcount
            cursor.execute("""INSERT INTO settings VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (LAST, datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()))
            return removed
    finally:
        connection.execute("DETACH DATABASE media_cache")
