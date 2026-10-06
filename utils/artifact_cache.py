"""Идентификаторы Telegram и полные публикации с независимыми ссылками."""

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def canonical_source(url: str) -> str:
    """Нормализует только известные варианты YouTube и параметры отслеживания."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    known_hosts = {"youtu.be", "www.youtu.be", "youtube.com", "www.youtube.com", "m.youtube.com", "instagram.com", "www.instagram.com", "tiktok.com", "www.tiktok.com"}
    if host not in known_hosts:
        # Порядок и кодирование параметров могут участвовать в подписи ссылки.
        return url
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if not (key in {"si", "feature", "igsh"} or key.startswith("utm_"))]
    if host in {"youtu.be", "www.youtu.be"}:
        query.append(("v", parsed.path.strip("/")))
        host, path = "www.youtube.com", "/watch"
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        host, path = "www.youtube.com", parsed.path
        if path.startswith(("/shorts/", "/embed/")):
            query.append(("v", path.split("/")[2]))
            path = "/watch"
    else:
        host, path = parsed.netloc.lower(), parsed.path
    return urlunsplit((parsed.scheme.lower(), host, path, urlencode(sorted(query)), parsed.fragment))


def recipe_for(platform: str, selection: str) -> str:
    """Версия обработки входит в ключ независимо от имени кнопки."""
    from config import TIKTOK_FAST_PATH, INSTAGRAM_FAST_PATH

    return json.dumps({"version": 3, "platform": platform, "selection": selection,
                       "tiktok_fast": TIKTOK_FAST_PATH if platform == "tiktok" else None,
                       "instagram_fast": INSTAGRAM_FAST_PATH if platform == "instagram" else None},
                      sort_keys=True)


@dataclass(frozen=True)
class Artifact:
    file_id: str
    file_unique_id: str
    kind: str
    width: int | None = None
    height: int | None = None
    size: int | None = None
    duration: int | None = None


def artifact_from_message(message) -> Artifact | None:
    """Берет фактически возвращенный Telegram тип, включая размеры фото."""
    for kind in ("video", "audio", "document", "photo"):
        media = getattr(message, kind, None)
        if kind == "photo":
            if not isinstance(media, (tuple, list)) or not media:
                continue
            media = max(media, key=lambda photo: photo.width * photo.height)
        file_id = getattr(media, "file_id", None)
        if not isinstance(file_id, str) or not file_id:
            continue
        def number(name):
            value = getattr(media, name, None)
            if name == "duration" and isinstance(value, timedelta):
                return int(value.total_seconds())
            return value if isinstance(value, int) else None
        unique = getattr(media, "file_unique_id", "")
        return Artifact(file_id, unique if isinstance(unique, str) else "", kind,
                        number("width"), number("height"), number("file_size"), number("duration"))
    return None


class ArtifactCache:
    """Схема рядом с прежним кешем, без удаления данных прежнего читателя."""

    def __init__(self, path: Path):
        self.path = path
        with self.connection() as connection:
            connection.executescript("""BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY, bot_id INTEGER NOT NULL,
                    file_id TEXT NOT NULL, file_unique_id TEXT NOT NULL, kind TEXT NOT NULL,
                    width INTEGER, height INTEGER, size INTEGER, duration INTEGER,
                    created REAL NOT NULL, last_used REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS manifests (
                    id TEXT PRIMARY KEY, bot_id INTEGER NOT NULL, source TEXT NOT NULL,
                    recipe TEXT NOT NULL, metadata TEXT NOT NULL, valid INTEGER NOT NULL DEFAULT 1,
                    created REAL NOT NULL, last_used REAL NOT NULL,
                    UNIQUE(bot_id, source, recipe));
                CREATE TABLE IF NOT EXISTS manifest_items (
                    manifest_id TEXT NOT NULL REFERENCES manifests(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    artifact_id TEXT NOT NULL REFERENCES artifacts(id),
                    role TEXT NOT NULL DEFAULT 'media', PRIMARY KEY(manifest_id, position));
                CREATE TABLE IF NOT EXISTS artifact_aliases (
                    bot_id INTEGER NOT NULL, url TEXT NOT NULL, recipe TEXT NOT NULL,
                    manifest_id TEXT NOT NULL REFERENCES manifests(id) ON DELETE CASCADE,
                    PRIMARY KEY(bot_id, url, recipe));
                CREATE TABLE IF NOT EXISTS artifact_schema (version INTEGER PRIMARY KEY);
                INSERT OR IGNORE INTO artifact_schema VALUES (3);
                CREATE INDEX IF NOT EXISTS idx_artifacts_file ON artifacts(bot_id, file_id);
                CREATE INDEX IF NOT EXISTS idx_manifests_used ON manifests(last_used);
            """)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(manifests)")}
            if "valid" not in columns:
                connection.execute("ALTER TABLE manifests ADD COLUMN valid INTEGER NOT NULL DEFAULT 1")

    def connection(self):
        """Каждая цельная операция владеет своим соединением."""
        from contextlib import contextmanager

        @contextmanager
        def connect():
            with closing(sqlite3.connect(self.path, timeout=3, isolation_level=None)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    yield connection
                    connection.execute("COMMIT")
                except BaseException:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    raise
        return connect()

    @staticmethod
    def _store(connection, bot_id: int, item: Artifact, now: float) -> str:
        key = hashlib.sha256(f"{bot_id}:{item.kind}:{item.file_id}".encode()).hexdigest()
        connection.execute("""INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET last_used=excluded.last_used""",
            (key, bot_id, item.file_id, item.file_unique_id, item.kind, item.width,
             item.height, item.size, item.duration, now, now))
        return key

    def store(self, bot_id: int, items: list[Artifact]):
        """Сохраняет подтвержденные части без объявления публикации полной."""
        with self.connection() as connection:
            for item in items:
                self._store(connection, bot_id, item, time.time())

    def put(self, bot_id: int, url: str, recipe: str, items: list[tuple[Artifact, str]], metadata: dict):
        """Публикует только полный упорядоченный состав одной транзакцией."""
        if not items:
            return
        source = canonical_source(url)
        manifest_id = hashlib.sha256(f"{bot_id}:{source}:{recipe}".encode()).hexdigest()
        now = time.time()
        with self.connection() as connection:
            connection.execute("""INSERT INTO manifests (id, bot_id, source, recipe, metadata, created, last_used) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata, last_used=excluded.last_used, valid=1""",
                (manifest_id, bot_id, source, recipe, json.dumps(metadata, ensure_ascii=False), now, now))
            connection.execute("DELETE FROM manifest_items WHERE manifest_id=?", (manifest_id,))
            for position, (item, role) in enumerate(items):
                key = self._store(connection, bot_id, item, now)
                connection.execute("INSERT INTO manifest_items VALUES (?, ?, ?, ?)",
                                   (manifest_id, position, key, role))
            connection.execute("""INSERT INTO artifact_aliases VALUES (?, ?, ?, ?)
                ON CONFLICT(bot_id, url, recipe) DO UPDATE SET manifest_id=excluded.manifest_id""",
                (bot_id, url, recipe, manifest_id))

    def get(self, bot_id: int, url: str, recipe: str):
        """Повторное использование продлевает время последнего успешного доступа отдельно."""
        with self.connection() as connection:
            manifest = connection.execute(
                "SELECT * FROM manifests WHERE bot_id=? AND source=? AND recipe=? AND valid=1",
                (bot_id, canonical_source(url), recipe)).fetchone()
            if manifest is None:
                return None
            rows = connection.execute("""SELECT a.*, i.role FROM manifest_items i
                JOIN artifacts a ON a.id=i.artifact_id WHERE i.manifest_id=? ORDER BY i.position""",
                (manifest["id"],)).fetchall()
            if not rows:
                return None
            connection.execute("""INSERT INTO artifact_aliases VALUES (?, ?, ?, ?)
                ON CONFLICT(bot_id, url, recipe) DO UPDATE SET manifest_id=excluded.manifest_id""",
                (bot_id, url, recipe, manifest["id"]))
            return {"id": manifest["id"], "metadata": json.loads(manifest["metadata"]),
                    "items": [(Artifact(row["file_id"], row["file_unique_id"], row["kind"],
                               row["width"], row["height"], row["size"], row["duration"]), row["role"])
                              for row in rows]}

    def touch(self, manifest_id: str):
        """Фиксирует успешное использование, а не один лишь просмотр меню."""
        with self.connection() as connection:
            connection.execute("UPDATE manifests SET last_used=? WHERE id=?", (time.time(), manifest_id))
            connection.execute("""UPDATE artifacts SET last_used=? WHERE id IN
                (SELECT artifact_id FROM manifest_items WHERE manifest_id=?)""", (time.time(), manifest_id))

    def invalidate(self, bot_id: int, file_id: str):
        """Удаляет составы с отвергнутым ID, сохраняя остальные файлы."""
        with self.connection() as connection:
            connection.execute("""UPDATE manifests SET valid=0 WHERE id IN
                (SELECT i.manifest_id FROM manifest_items i JOIN artifacts a ON a.id=i.artifact_id
                 WHERE a.bot_id=? AND a.file_id=?)""", (bot_id, file_id))


    def stats(self):
        """Считает указатели и реальные размеры SQLite вместе с WAL."""
        with self.connection() as connection:
            result = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in ("artifacts", "manifests", "artifact_aliases")}
            result["manifests"] = connection.execute("SELECT COUNT(*) FROM manifests WHERE valid=1").fetchone()[0]
        result["bytes"] = sum(path.stat().st_size for path in
                              (self.path, self.path.with_name(self.path.name + "-wal")) if path.exists())
        return result

    def invalidate_manifest(self, manifest_id: str):
        """Сохраняет ссылки и состав, но исключает непригодную публикацию из выдачи."""
        with self.connection() as connection:
            connection.execute("UPDATE manifests SET valid=0 WHERE id=?", (manifest_id,))
