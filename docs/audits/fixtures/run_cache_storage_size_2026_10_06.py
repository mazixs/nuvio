"""Оценивает размер нынешней схемы кеша на синтетических записях.

Создает только временные базы. Медиа, cookies и Telegram не используются.
Результат не является прогнозом нагрузки или размера будущей схемы за год.
"""

import json
import sqlite3
import tempfile
from pathlib import Path


def measure(
    directory: Path, scenario: str, url_bytes: int, file_id_bytes: int, title_bytes: int
):
    """Заполняет копию текущей схемы с теми же индексами и измеряет файл."""
    path = directory / f"{scenario}.db"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript("""
            CREATE TABLE video_cache (
                url TEXT NOT NULL,
                file_id TEXT NOT NULL,
                file_unique_id TEXT NOT NULL UNIQUE,
                platform TEXT NOT NULL,
                format_id TEXT NOT NULL,
                cached_at TEXT NOT NULL,
                file_size INTEGER,
                duration INTEGER,
                title TEXT,
                PRIMARY KEY (url, format_id)
            );
            CREATE INDEX idx_file_id ON video_cache(file_id);
            CREATE INDEX idx_platform ON video_cache(platform);
            CREATE INDEX idx_cached_at ON video_cache(cached_at);
        """)

        def rows():
            for index in range(10000):
                yield (
                    f"https://example.test/{index:08d}/".ljust(url_bytes, "u"),
                    f"file-{index:08d}-".ljust(file_id_bytes, "f"),
                    f"unique-{index:08d}".ljust(32, "x"),
                    "youtube",
                    "tg_video",
                    "2026-10-06T00:00:00",
                    123456789,
                    120,
                    "t" * title_bytes,
                )

        connection.executemany(
            "INSERT INTO video_cache VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows()
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()
    size = path.stat().st_size
    return {
        "scenario": scenario,
        "rows": 10000,
        "url_bytes": url_bytes,
        "file_id_bytes": file_id_bytes,
        "title_bytes": title_bytes,
        "db_bytes": size,
        "MiB": round(size / 2**20, 2),
        "bytes_per_row": round(size / 10000, 2),
        "3GiB_rows_extrapolated": int(3 * 2**30 / (size / 10000)),
    }


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="nuvio-cache-size-") as directory:
        root = Path(directory)
        results = [
            measure(root, "compact", 100, 150, 160),
            measure(root, "larger", 512, 512, 1024),
        ]
    print(json.dumps(results))
