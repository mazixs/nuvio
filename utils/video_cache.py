"""
Кэширование file_id для мгновенной доставки видео.

Telegram сохраняет загруженные файлы и присваивает им file_id.
При повторной отправке file_id скачивание и загрузка файла не нужны.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import contextmanager
from config import BASE_DIR
from utils.logger import setup_logger

logger = setup_logger(__name__)


# Версия контракта доставки видео. Растёт, когда меняется то, что записано в
# самом документе Telegram, а не в нашей строке кэша.
#
# 1 — ADR-002: до этой версии бот отправлял видео без `width`/`height`, и на
#     тяжёлых файлах Telegram записывал в документ `320x320`. Такие записи
#     правкой кода не чинятся, потому что пересылка по `file_id` берёт атрибуты
#     сохранённого документа.
# 2 - выбор языка YouTube по метаданным. Старые file_id могли содержать
#     английскую озвучку даже при доступной русской дорожке.
DELIVERY_CONTRACT_VERSION = 2

# Ключи, под которыми в кэше лежит видео. Совпадают с `utils/platform_actions.py`
# и продублированы здесь намеренно: импорт ради двух строк связал бы кэш с
# модулем пользовательских действий, а видеоключ вида `combined:{format_id}`
# всё равно проверяется отдельно, по префиксу.
DIRECT_VIDEO_CACHE_KEY = "direct_video"
TG_VIDEO_CACHE_KEY = "tg_video"


@dataclass
class CachedVideo:
    """Закэшированное видео в Telegram."""

    url: str
    file_id: str
    file_unique_id: str
    platform: str
    format_id: str
    cached_at: datetime
    file_size: int | None = None
    duration: int | None = None
    title: str | None = None

    def is_valid(self, cache_ttl_days: int | None = None) -> bool:
        """
        Проверяет, не истек ли кэш.

        Args:
            cache_ttl_days: Явный срок; без него запись не истекает.

        Returns:
            True если кэш валиден
        """
        if cache_ttl_days is None:
            return True
        age = datetime.now() - self.cached_at
        return age < timedelta(days=cache_ttl_days)

class TelegramVideoCache:
    """
    Кэш file_id для видео.

    Повторная доставка использует сохраненный Telegram file_id.

    Использует SQLite в WAL-режиме с оптимизациями для конкурентного доступа:
    - timeout=3.0 (ограниченное ожидание при блокировке)
    - synchronous=NORMAL (ускорение записи без потери целостности в WAL)
    - cache_size=-64000 (64 МБ кэш страниц)
    - BEGIN IMMEDIATE для всех транзакций записи (защита от upgrade deadlock)
    """

    def __init__(self, db_path: Path | None = None):
        """
        Инициализирует кэш.

        Args:
            db_path: Путь к файлу базы данных.
                     По умолчанию BASE_DIR / "telegram_cache.db"
        """
        if db_path is None:
            import os

            data_dir = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
            db_path = data_dir / "telegram_cache.db"

        self.db_path = db_path
        self._backup_before_migration()
        self._init_db()
        from utils.artifact_cache import ArtifactCache

        self.artifacts = ArtifactCache(self.db_path)
        logger.info(f"Telegram video cache инициализирован: {self.db_path}")

    def _backup_before_migration(self):
        """Копирует согласованный снимок SQLite до изменения прежней схемы."""
        if not self.db_path.exists():
            return
        from contextlib import closing
        import uuid

        with closing(sqlite3.connect(self.db_path, timeout=3)) as source:
            indexes = source.execute("PRAGMA index_list(video_cache)").fetchall()
            old = any(row[2] and [item[2] for item in source.execute(f'PRAGMA index_info("{row[1]}")')] == ["file_unique_id"] for row in indexes)
            if not old:
                return
            backup = self.db_path.with_name(f"{self.db_path.name}.before-artifacts-{uuid.uuid4().hex[:8]}.bak")
            with closing(sqlite3.connect(backup)) as destination:
                source.backup(destination)
            logger.info("Создан согласованный снимок кеша перед миграцией")

    def _configure_connection(self, conn: sqlite3.Connection) -> None:
        """Применяет оптимальные PRAGMA к свежему соединению."""
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-64000")

    @contextmanager
    def _get_connection(self):
        """Context manager для безопасного чтения из БД."""
        conn = sqlite3.connect(str(self.db_path), timeout=3.0)
        try:
            self._configure_connection(conn)
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write_transaction(self):
        """
        Context manager для транзакций записи.

        Использует isolation_level=None и BEGIN IMMEDIATE для защиты
        от upgrade deadlock при конкурентной записи.
        """
        conn = sqlite3.connect(str(self.db_path), timeout=3.0, isolation_level=None)
        started = False
        try:
            self._configure_connection(conn)
            conn.execute("BEGIN IMMEDIATE")
            started = True
            yield conn
            conn.execute("COMMIT")
        except Exception:
            if started and conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _init_db(self):
        """Инициализирует схему базы данных."""
        with self._write_transaction() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS video_cache (
                    url TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    file_unique_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    format_id TEXT NOT NULL,
                    cached_at TEXT NOT NULL,
                    file_size INTEGER,
                    duration INTEGER,
                    title TEXT,
                    PRIMARY KEY (url, format_id)
                )
            """)

            # Один файл может иметь несколько ссылок. REPLACE по UNIQUE терял их.
            unique_file = any(
                row[2] and [column[2] for column in conn.execute(f'PRAGMA index_info("{row[1]}")')] == ["file_unique_id"]
                for row in conn.execute("PRAGMA index_list(video_cache)")
            )
            if unique_file:
                legacy_name = "video_cache_legacy"
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (legacy_name,)).fetchone():
                    import uuid

                    legacy_name += "_" + uuid.uuid4().hex[:8]
                conn.execute(f"ALTER TABLE video_cache RENAME TO {legacy_name}")
                conn.execute("""CREATE TABLE video_cache (
                    url TEXT NOT NULL, file_id TEXT NOT NULL,
                    file_unique_id TEXT NOT NULL, platform TEXT NOT NULL,
                    format_id TEXT NOT NULL, cached_at TEXT NOT NULL,
                    file_size INTEGER, duration INTEGER, title TEXT,
                    PRIMARY KEY (url, format_id))""")
                conn.execute(f"INSERT INTO video_cache SELECT * FROM {legacy_name}")
                # Копия остается для восстановления и просмотра мигрированных данных.
                conn.execute("DROP INDEX IF EXISTS idx_file_id")
                conn.execute("DROP INDEX IF EXISTS idx_platform")
                conn.execute("DROP INDEX IF EXISTS idx_cached_at")

            # Индексы для быстрого поиска
            # Первичный ключ (url, format_id) уже покрывает поиск по url.
            conn.execute("DROP INDEX IF EXISTS idx_url")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_file_id ON video_cache(file_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_platform ON video_cache(platform)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cached_at ON video_cache(cached_at)"
            )

            self._drop_stale_video_documents(conn)

    def _drop_stale_video_documents(self, conn: sqlite3.Connection) -> None:
        """Один раз очищает записи, нарушающие актуальный контракт доставки.

        Пересылка по `file_id` берёт размеры из сохранённого документа, а не из
        нашей строки, поэтому записи, снятые до ADR-002, правкой кода не
        чинятся: в документе уже записано `320x320`. Без этой чистки сломанное
        видео приезжало бы до конца TTL, то есть 90 дней.

        При переходе на версию 2 удаляются только автоматические YouTube-кнопки
        и форматы видео, которые могли быть закэшированы с чужой озвучкой.
        Отметка хранится в `PRAGMA user_version`.
        """
        stored = conn.execute("PRAGMA user_version").fetchone()[0]
        if stored >= DELIVERY_CONTRACT_VERSION:
            return

        if stored < 1:
            cursor = conn.execute(
                "DELETE FROM video_cache "
                "WHERE format_id IN (?, ?) OR format_id LIKE 'combined:%'",
                (DIRECT_VIDEO_CACHE_KEY, TG_VIDEO_CACHE_KEY),
            )
            if cursor.rowcount:
                logger.info(
                    "Кэш видео: удалено %d записей со старыми размерами Telegram",
                    cursor.rowcount,
                )
        if stored < 2:
            cursor = conn.execute(
                "DELETE FROM video_cache WHERE platform = 'youtube' "
                "AND (format_id IN (?, ?) OR format_id LIKE 'combined:%')",
                (TG_VIDEO_CACHE_KEY, "audio_m4a"),
            )
            if cursor.rowcount:
                logger.info(
                    "Кэш YouTube: удалено %d записей со старым выбором языка",
                    cursor.rowcount,
                )
        # PRAGMA не принимает параметры, а значение здесь — наша же константа.
        conn.execute(f"PRAGMA user_version = {DELIVERY_CONTRACT_VERSION:d}")

    def get(
        self, url: str, format_id: str = "best", check_validity: bool = True
    ) -> CachedVideo | None:
        """
        Получает file_id из кэша.

        Args:
            url: URL видео
            format_id: ID формата (best, 720p, audio и т.д.)
            check_validity: Проверять ли TTL кэша

        Returns:
            CachedVideo если найден в кэше и валиден, иначе None
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM video_cache WHERE url = ? AND format_id = ?",
                (url, format_id),
            )
            row = cursor.fetchone()

        if not row:
            logger.debug(f"Cache MISS для {url} (format: {format_id})")
            return None

        cached = CachedVideo(
            url=row[0],
            file_id=row[1],
            file_unique_id=row[2],
            platform=row[3],
            format_id=row[4],
            cached_at=datetime.fromisoformat(row[5]),
            file_size=row[6],
            duration=row[7],
            title=row[8],
        )

        # Проверяем валидность
        if check_validity and not cached.is_valid():
            logger.warning(f"Кэш устарел для {url}, удаляем")
            self.delete(url, format_id)
            return None

        logger.info(
            f"✅ Cache HIT для {url} (format: {format_id}, age: {(datetime.now() - cached.cached_at).days} дней)"
        )
        return cached

    def set(self, cached: CachedVideo):
        """
        Сохраняет file_id в кэш.

        Args:
            cached: Объект CachedVideo для сохранения
        """
        with self._write_transaction() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO video_cache
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(url, format_id) DO UPDATE SET
                    file_id=excluded.file_id, file_unique_id=excluded.file_unique_id,
                    platform=excluded.platform, cached_at=excluded.cached_at,
                    file_size=excluded.file_size, duration=excluded.duration,
                    title=excluded.title
                """,
                    (
                        cached.url,
                        cached.file_id,
                        cached.file_unique_id,
                        cached.platform,
                        cached.format_id,
                        cached.cached_at.isoformat(),
                        cached.file_size,
                        cached.duration,
                        cached.title,
                    ),
                )
                logger.info(f"💾 Сохранен в кэш: {cached.url} -> {cached.file_id}")
            except sqlite3.IntegrityError as e:
                logger.error(f"Ошибка сохранения в кэш: {e}")

    def delete(self, url: str, format_id: str):
        """
        Удаляет запись из кэша.

        Args:
            url: URL видео
            format_id: ID формата
        """
        with self._write_transaction() as conn:
            conn.execute(
                "DELETE FROM video_cache WHERE url = ? AND format_id = ?",
                (url, format_id),
            )
            logger.debug(f"Удалено из кэша: {url} (format: {format_id})")

    def delete_by_file_id(self, file_id: str):
        """
        Удаляет запись по file_id (если Telegram вернул ошибку).

        Args:
            file_id: file_id для удаления
        """
        with self._write_transaction() as conn:
            conn.execute("DELETE FROM video_cache WHERE file_id = ?", (file_id,))
            logger.debug(f"Удалено из кэша по file_id: {file_id}")

    def cleanup_expired(self, ttl_days: int = 90):
        """
        Удаляет устаревшие записи.

        Args:
            ttl_days: TTL кэша в днях
        """
        cutoff = (datetime.now() - timedelta(days=ttl_days)).isoformat()

        with self._write_transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM video_cache WHERE cached_at < ?", (cutoff,)
            )
            deleted = cursor.rowcount

        if deleted > 0:
            logger.info(f"🗑️ Очищено {deleted} устаревших записей из кэша")

        return deleted

    def vacuum(self) -> None:
        """Выполняет VACUUM и checkpoint для базы кэша."""
        # VACUUM и wal_checkpoint нельзя выполнять внутри явной транзакции,
        # поэтому используем обычное соединение (без BEGIN IMMEDIATE).
        with self._get_connection() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
            conn.commit()
        logger.info("🧹 Выполнена оптимизация базы кэша (VACUUM)")

    def get_stats(self) -> dict:
        """
        Получает статистику по кэшу.

        Returns:
            Словарь со статистикой
        """
        with self._get_connection() as conn:
            total = conn.execute("SELECT COUNT(*) FROM video_cache").fetchone()[0]

            by_platform = dict(
                conn.execute("""
                SELECT platform, COUNT(*) 
                FROM video_cache 
                GROUP BY platform
            """).fetchall()
            )

            oldest = conn.execute("SELECT MIN(cached_at) FROM video_cache").fetchone()[
                0
            ]

            newest = conn.execute("SELECT MAX(cached_at) FROM video_cache").fetchone()[
                0
            ]

        return {
            "total_videos": total,
            "by_platform": by_platform,
            "oldest_entry": oldest,
            "newest_entry": newest,
        }

    def search_by_title(self, query: str, limit: int = 10) -> list[CachedVideo]:
        """
        Ищет видео по названию.

        Args:
            query: Поисковый запрос
            limit: Максимум результатов

        Returns:
            Список CachedVideo
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """SELECT * FROM video_cache 
                   WHERE title LIKE ? 
                   ORDER BY cached_at DESC 
                   LIMIT ?""",
                (f"%{query}%", limit),
            )
            rows = cursor.fetchall()

        return [
            CachedVideo(
                url=row[0],
                file_id=row[1],
                file_unique_id=row[2],
                platform=row[3],
                format_id=row[4],
                cached_at=datetime.fromisoformat(row[5]),
                file_size=row[6],
                duration=row[7],
                title=row[8],
            )
            for row in rows
        ]


# Глобальный экземпляр кэша
class _UnavailableCache:
    """Сбой ускоряющего хранилища разрешает обычную подготовку медиа."""

    def __init__(self):
        self.artifacts = self

    def get(self, *args, **kwargs):
        return None

    def put(self, *args, **kwargs):
        return None

    store = touch = invalidate = invalidate_manifest = delete_by_file_id = put

    def get_stats(self):
        return {"total_videos": 0, "by_platform": {}, "oldest_entry": None, "newest_entry": None}


try:
    telegram_cache = TelegramVideoCache()
except (sqlite3.Error, OSError):
    logger.exception("Кеш недоступен при запуске; загрузки продолжатся без него")
    telegram_cache = _UnavailableCache()
