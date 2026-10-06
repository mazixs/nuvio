"""
Аналитическая SQLite база данных для трекинга пользователей и событий.

Оптимизации WAL:
- timeout=30.0: терпеливое ожидание при блокировке
- synchronous=NORMAL: ускорение записи без потери целостности
- cache_size=-64000: 64 МБ кэш страниц
- isolation_level=None + BEGIN IMMEDIATE: ручное управление транзакциями
  и защита от upgrade deadlock при конкурентной записи.
"""

import logging
import sqlite3
import threading
from collections import Counter, defaultdict
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import os

_DATA_DIR = Path(
    os.environ.get("DATA_DIR", str(Path(__file__).resolve().parent.parent))
)
_DB_PATH = Path(_DATA_DIR) / "analytics.db"
_local = threading.local()

logger = logging.getLogger(__name__)

FEEDBACK_MAX_LENGTH = 2000
FEEDBACK_COOLDOWN_SECONDS = 60


class FeedbackRateLimitError(ValueError):
    """Обращения отправляются слишком часто."""


class UserBlockedError(Exception):
    """Администратор ограничил доступ пользователя к боту."""


def _get_connection() -> sqlite3.Connection:
    """Возвращает thread-local соединение с настроенными PRAGMA."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(
            str(_DB_PATH),
            check_same_thread=False,
            timeout=3.0,
            isolation_level=None,
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-64000")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return conn


def close_connection() -> None:
    """Закрывает соединение аналитики текущего потока."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        return
    try:
        conn.close()
    finally:
        delattr(_local, "conn")


@contextmanager
def _cursor_read():
    """Контекстный менеджер для операций чтения."""
    conn = _get_connection()
    cur = conn.cursor()
    try:
        yield cur
    finally:
        cur.close()


@contextmanager
def _cursor_write():
    """
    Контекстный менеджер для операций записи.

    Использует BEGIN IMMEDIATE для резервирования права записи
    на старте транзакции, предотвращая upgrade deadlock.
    """
    conn = _get_connection()
    cur = conn.cursor()
    started = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        started = True
        yield cur
        conn.execute("COMMIT")
    except Exception:
        if started and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        cur.close()


def init_db() -> None:
    """Создаёт таблицы если не существуют."""
    with _cursor_write() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id       INTEGER PRIMARY KEY,
                username      TEXT,
                first_name    TEXT,
                last_name     TEXT,
                language_code TEXT,
                first_seen    TEXT NOT NULL,
                last_seen     TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   INTEGER NOT NULL,
                event     TEXT NOT NULL,
                platform  TEXT,
                url       TEXT,
                metadata  TEXT,
                ts        TEXT NOT NULL
            )
        """)

        # Таблица ответов CSI (Customer Satisfaction Index)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS csi_responses (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                rating      INTEGER NOT NULL,
                feedback    TEXT,
                created_at  TEXT NOT NULL
            )
        """)
        columns = {row[1] for row in cur.execute("PRAGMA table_info(csi_responses)")}
        if "poll_id" not in columns:
            cur.execute("ALTER TABLE csi_responses ADD COLUMN poll_id TEXT")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_csi_poll ON csi_responses(user_id, poll_id) WHERE poll_id IS NOT NULL")
        cur.execute("""CREATE TABLE IF NOT EXISTS csi_polls (
            id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
            message_id INTEGER, created_at TEXT NOT NULL)""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_csi_user ON csi_responses(user_id)")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_csi_created ON csi_responses(created_at)"
        )

        # Настройки, которые оператор меняет из WebUI. Таблица нужна именно в
        # этой БД: бот и WebUI — разные процессы, и analytics.db — их единственное
        # общее место записи (том DATA_DIR смонтирован в оба контейнера).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)

        # Миграция: добавляем last_csi_sent в users если колонка отсутствует
        cur.execute("PRAGMA table_info(users)")
        existing_cols = {row[1] for row in cur.fetchall()}
        if "last_csi_sent" not in existing_cols:
            cur.execute("ALTER TABLE users ADD COLUMN last_csi_sent TEXT")
        for column, definition in (
            ("is_blocked", "INTEGER NOT NULL DEFAULT 0"),
            ("block_reason", "TEXT"),
            ("blocked_at", "TEXT"),
        ):
            if column not in existing_cols:
                cur.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                error_code TEXT,
                source_url TEXT,
                message_key TEXT,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'closed')),
                UNIQUE(user_id, message_key)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_feedback_user_time ON feedback(user_id, created_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_feedback_status_time ON feedback(status, created_at DESC)")

        cur.execute("DROP INDEX IF EXISTS idx_events_user")
        cur.execute("DROP INDEX IF EXISTS idx_events_event")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_last_seen ON users(last_seen DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_first_seen ON users(first_seen)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_user_ts ON events(user_id, ts DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_event_ts ON events(event, ts)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_ts     ON events(ts)")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_platform ON events(platform)"
        )


# ── запись событий ──────────────────────────────────────────────


def track_user(
    user_id: int,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    language_code: str | None = None,
) -> None:
    """Создаёт или обновляет пользователя."""
    now = datetime.now(UTC).isoformat()
    with _cursor_write() as cur:
        cur.execute(
            """
            INSERT INTO users (user_id, username, first_name, last_name, language_code, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username      = COALESCE(excluded.username, users.username),
                first_name    = COALESCE(excluded.first_name, users.first_name),
                last_name     = COALESCE(excluded.last_name, users.last_name),
                language_code = COALESCE(excluded.language_code, users.language_code),
                last_seen     = excluded.last_seen
            """,
            (user_id, username, first_name, last_name, language_code, now, now),
        )


def track_event(
    user_id: int,
    event: str,
    platform: str | None = None,
    url: str | None = None,
    metadata: str | None = None,
) -> None:
    """Записывает событие."""
    now = datetime.now(UTC).isoformat()
    with _cursor_write() as cur:
        cur.execute(
            "INSERT INTO events (user_id, event, platform, url, metadata, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, event, platform, url, metadata, now),
        )


def prune_old_event_urls(days: int = 90, batch_size: int = 500) -> tuple[int, Path | None]:
    """Убирает старые URL, сохраняя события и резервную копию до правки."""
    if days < 1 or batch_size < 1:
        raise ValueError("days и batch_size должны быть положительными")
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with _cursor_read() as cur:
        cur.execute("SELECT COUNT(*) FROM events WHERE url IS NOT NULL AND ts < ?", (cutoff,))
        count = cur.fetchone()[0]
    if not count:
        return 0, None

    backup_dir = _DB_PATH.parent / "backups"
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup_dir.chmod(0o700)
    backup_path = backup_dir / f"analytics-{datetime.now(UTC):%Y%m%dT%H%M%S%f}.sqlite3"
    backup_path.touch(mode=0o600, exist_ok=False)
    with closing(sqlite3.connect(str(backup_path))) as backup:
        _get_connection().backup(backup)
        if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise sqlite3.DatabaseError("резервная копия аналитики не прошла проверку")

    removed = 0
    while True:
        with _cursor_write() as cur:
            cur.execute(
                """
                UPDATE events SET url = NULL
                WHERE id IN (
                    SELECT id FROM events
                    WHERE url IS NOT NULL AND ts < ?
                    ORDER BY id LIMIT ?
                )
                """,
                (cutoff, batch_size),
            )
            batch = cur.rowcount
        removed += batch
        if batch < batch_size:
            break

    backups = sorted(backup_dir.glob("analytics-*.sqlite3"), reverse=True)
    for old in backups[7:]:
        try:
            old.unlink()
        except OSError as exc:
            logger.warning("Не удалось удалить старую копию аналитики %s: %s", old, exc)
    return removed, backup_path


# ── Настройки ───────────────────────────────────────────────────

CSI_INTERVAL_DAYS_DEFAULT = 14
CSI_INTERVAL_DAYS_MIN = 1
CSI_INTERVAL_DAYS_MAX = 365

_SETTING_CSI_INTERVAL = "csi_interval_days"


def get_setting(key: str, default: str | None = None) -> str | None:
    """Возвращает значение настройки или default, если её ещё не задавали."""
    with _cursor_read() as cur:
        cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    """Записывает настройку, перетирая прежнее значение."""
    now = datetime.now(UTC).isoformat()
    with _cursor_write() as cur:
        cur.execute(
            """
            INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                           updated_at = excluded.updated_at
            """,
            (key, value, now),
        )


def get_csi_interval_days() -> int:
    """Как часто одному пользователю приходит CSI-опрос, в днях.

    Читается на каждой рассылке, поэтому смена настройки в WebUI применяется
    без перезапуска бота. Испорченное значение не должно ронять рассылку —
    возвращаем значение по умолчанию.
    """
    raw = get_setting(_SETTING_CSI_INTERVAL)
    if raw is None:
        return CSI_INTERVAL_DAYS_DEFAULT
    try:
        days = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Настройка %s содержит не число (%r), используется %s дней",
            _SETTING_CSI_INTERVAL,
            raw,
            CSI_INTERVAL_DAYS_DEFAULT,
        )
        return CSI_INTERVAL_DAYS_DEFAULT
    if not CSI_INTERVAL_DAYS_MIN <= days <= CSI_INTERVAL_DAYS_MAX:
        logger.warning(
            "Настройка %s вне допустимого диапазона (%s), используется %s дней",
            _SETTING_CSI_INTERVAL,
            days,
            CSI_INTERVAL_DAYS_DEFAULT,
        )
        return CSI_INTERVAL_DAYS_DEFAULT
    return days


def set_csi_interval_days(days: int) -> None:
    """Задаёт интервал CSI-опросов.

    Нижняя граница не декоративна: рассылка запускается раз в сутки, и при
    нулевом интервале опрос уходил бы каждому активному пользователю каждый
    день.
    """
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise ValueError("интервал должен быть целым числом дней") from None
    if not CSI_INTERVAL_DAYS_MIN <= days <= CSI_INTERVAL_DAYS_MAX:
        raise ValueError(
            f"интервал должен быть от {CSI_INTERVAL_DAYS_MIN} "
            f"до {CSI_INTERVAL_DAYS_MAX} дней"
        )
    set_setting(_SETTING_CSI_INTERVAL, str(days))
    logger.info("Интервал CSI-опросов изменён на %s дней", days)


# ── CSI (Customer Satisfaction Index) ───────────────────────────


def get_users_for_csi(
    days_since_last: int = CSI_INTERVAL_DAYS_DEFAULT, min_active_days: int = 1
) -> list[int]:
    """
    Возвращает ID пользователей, которым пора отправить CSI-опрос.

    Условия:
    - Пользователь активен за последние min_active_days дней
    - CSI не отправлялся ранее ИЛИ прошло более days_since_last дней
    """
    active_since = (datetime.now(UTC) - timedelta(days=min_active_days)).isoformat()
    last_csi_since = (datetime.now(UTC) - timedelta(days=days_since_last)).isoformat()
    with _cursor_read() as cur:
        cur.execute(
            """
            SELECT user_id FROM users
            WHERE last_seen >= ?
              AND is_blocked = 0
              AND (last_csi_sent IS NULL OR last_csi_sent < ?)
            ORDER BY last_seen DESC
            """,
            (active_since, last_csi_since),
        )
        return [int(row["user_id"]) for row in cur.fetchall()]


def update_last_csi_sent(user_id: int) -> None:
    """Обновляет время последней отправки CSI для пользователя."""
    now = datetime.now(UTC).isoformat()
    with _cursor_write() as cur:
        cur.execute(
            "UPDATE users SET last_csi_sent = ? WHERE user_id = ?",
            (now, user_id),
        )


def save_csi_rating(user_id: int, rating: int) -> int:
    """
    Сохраняет оценку CSI.

    Returns:
        ID созданной записи
    """
    now = datetime.now(UTC).isoformat()
    with _cursor_write() as cur:
        cur.execute(
            "INSERT INTO csi_responses (user_id, rating, created_at) VALUES (?, ?, ?)",
            (user_id, rating, now),
        )
        return cur.lastrowid


def update_csi_feedback(csi_id: int, feedback: str) -> None:
    """Добавляет текстовый отзыв к существующей записи CSI."""
    with _cursor_write() as cur:
        cur.execute(
            "UPDATE csi_responses SET feedback = ? WHERE id = ?",
            (feedback, csi_id),
        )


def get_csi_metrics() -> dict:
    """
    Возвращает метрики CSI.

    Returns:
        dict с avg_rating, total_responses, distribution, recent_low_feedback
    """
    with _cursor_read() as cur:
        cur.execute("SELECT COUNT(*) FROM csi_responses")
        total = cur.fetchone()[0]

        if total == 0:
            return {
                "avg_rating": 0.0,
                "total_responses": 0,
                "distribution": {},
                "recent_low_feedback": [],
            }

        cur.execute("SELECT AVG(rating) FROM csi_responses")
        avg = round(cur.fetchone()[0] or 0, 1)

        cur.execute(
            """
            SELECT rating, COUNT(*) as cnt
            FROM csi_responses
            GROUP BY rating
            ORDER BY rating
            """
        )
        distribution = {int(row["rating"]): row["cnt"] for row in cur.fetchall()}

        cur.execute(
            """
            SELECT id, user_id, rating, feedback, created_at
            FROM csi_responses
            WHERE rating < 7 AND feedback IS NOT NULL AND feedback != ''
            ORDER BY created_at DESC
            LIMIT 10
            """
        )
        recent_low = [dict(row) for row in cur.fetchall()]

        return {
            "avg_rating": avg,
            "total_responses": total,
            "distribution": distribution,
            "recent_low_feedback": recent_low,
        }


# ── метрики ─────────────────────────────────────────────────────


def total_users() -> int:
    with _cursor_read() as cur:
        cur.execute("SELECT COUNT(*) FROM users")
        return cur.fetchone()[0]


def new_users(days: int = 1) -> int:
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with _cursor_read() as cur:
        cur.execute("SELECT COUNT(*) FROM users WHERE first_seen >= ?", (since,))
        return cur.fetchone()[0]


def active_users(days: int = 1) -> int:
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with _cursor_read() as cur:
        cur.execute(
            "SELECT COUNT(DISTINCT user_id) FROM events WHERE ts >= ?", (since,)
        )
        return cur.fetchone()[0]


def retention(day: int) -> float:
    """Retention на N-й день: % пользователей, вернувшихся ровно через day дней после first_seen."""
    with _cursor_read() as cur:
        cur.execute("SELECT COUNT(*) FROM users")
        total = cur.fetchone()[0]
        if total == 0:
            return 0.0
        cur.execute(
            """
            SELECT COUNT(DISTINCT u.user_id)
            FROM users u
            JOIN events e ON e.user_id = u.user_id
            WHERE DATE(e.ts) = DATE(u.first_seen, '+' || ? || ' days')
              AND DATE(u.first_seen) <= DATE('now', '-' || ? || ' days')
            """,
            (day, day),
        )
        retained = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM users WHERE DATE(first_seen) <= DATE('now', '-' || ? || ' days')",
            (day,),
        )
        eligible = cur.fetchone()[0]
        if eligible == 0:
            return 0.0
        return round(retained / eligible * 100, 1)


def churn_rate(days: int = 30) -> float:
    """Churn: % пользователей, которые были активны ранее, но не активны за последние days дней."""
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with _cursor_read() as cur:
        cur.execute("SELECT COUNT(*) FROM users")
        total = cur.fetchone()[0]
        if total == 0:
            return 0.0
        cur.execute(
            "SELECT COUNT(*) FROM users WHERE last_seen < ?",
            (since,),
        )
        churned = cur.fetchone()[0]
        return round(churned / total * 100, 1)


def downloads_by_platform() -> dict[str, int]:
    with _cursor_read() as cur:
        cur.execute(
            """
            SELECT COALESCE(platform, 'unknown') as p, COUNT(*) as c
            FROM events
            WHERE event = 'download'
            GROUP BY p
            ORDER BY c DESC
            """,
        )
        return {row["p"]: row["c"] for row in cur.fetchall()}


def total_downloads() -> int:
    """Исторические запросы ссылки; старые записи не доказывают доставку."""
    with _cursor_read() as cur:
        cur.execute("SELECT COUNT(*) FROM events WHERE event = 'download'")
        return cur.fetchone()[0]


def total_deliveries() -> int:
    """Подтвержденные отправки с момента введения события delivery."""
    with _cursor_read() as cur:
        cur.execute("SELECT COUNT(*) FROM events WHERE event = 'delivery'")
        return cur.fetchone()[0]


def popular_videos(limit: int = 20) -> list[dict]:
    with _cursor_read() as cur:
        cur.execute(
            """
            SELECT url, platform, COUNT(*) as cnt, MAX(ts) as last_download
            FROM events
            WHERE event = 'download' AND url IS NOT NULL
            GROUP BY url
            ORDER BY cnt DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]


def downloads_per_day(days: int = 30) -> list[dict]:
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with _cursor_read() as cur:
        cur.execute(
            """
            SELECT DATE(ts) as day, COUNT(*) as cnt
            FROM events
            WHERE event = 'download' AND ts >= ?
            GROUP BY day
            ORDER BY day
            """,
            (since,),
        )
        return [dict(row) for row in cur.fetchall()]


def new_users_per_day(days: int = 30) -> list[dict]:
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with _cursor_read() as cur:
        cur.execute(
            """
            SELECT DATE(first_seen) as day, COUNT(*) as cnt
            FROM users
            WHERE first_seen >= ?
            GROUP BY day
            ORDER BY day
            """,
            (since,),
        )
        return [dict(row) for row in cur.fetchall()]


def is_user_blocked(user_id: int) -> bool:
    """Читает актуальную блокировку из общей базы бота и WebUI."""
    with _cursor_read() as cur:
        cur.execute("SELECT is_blocked FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        return bool(row and row["is_blocked"])


def set_user_blocked(user_id: int, blocked: bool, reason: str = "") -> None:
    """Меняет доступ с записью события, сохраняя профиль и историю пользователя."""
    from config import ADMIN_IDS

    if user_id in ADMIN_IDS:
        raise ValueError("Administrator accounts cannot be blocked")
    reason = reason.strip()
    if len(reason) > 500:
        raise ValueError("Block reason must not exceed 500 characters")
    now = datetime.now(UTC).isoformat()
    with _cursor_write() as cur:
        cur.execute(
            "UPDATE users SET is_blocked = ?, block_reason = ?, blocked_at = ? WHERE user_id = ?",
            (int(blocked), reason if blocked else None, now if blocked else None, user_id),
        )
        if cur.rowcount != 1:
            raise LookupError("User not found")
        cur.execute(
            "INSERT INTO events(user_id, event, metadata, ts) VALUES (?, ?, ?, ?)",
            (user_id, "user_blocked" if blocked else "user_unblocked", reason if blocked else None, now),
        )


def save_feedback(
    user_id: int, text: str, *, error_code: str | None = None,
    source_url: str | None = None, message_key: str | None = None,
) -> int:
    """Сохраняет обращение атомарно с защитой от повторов и спама."""
    text = text.strip()
    if not 3 <= len(text) <= FEEDBACK_MAX_LENGTH:
        raise ValueError("Feedback must contain 3 to 2000 characters")
    now = datetime.now(UTC)
    cutoff = (now - timedelta(seconds=FEEDBACK_COOLDOWN_SECONDS)).isoformat()
    with _cursor_write() as cur:
        cur.execute("SELECT is_blocked FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if row and row["is_blocked"]:
            raise UserBlockedError()
        if message_key:
            cur.execute("SELECT id FROM feedback WHERE user_id = ? AND message_key = ?", (user_id, message_key))
            existing = cur.fetchone()
            if existing:
                return int(existing["id"])
        cur.execute("SELECT id FROM feedback WHERE user_id = ? AND created_at > ? LIMIT 1", (user_id, cutoff))
        if cur.fetchone():
            raise FeedbackRateLimitError()
        cur.execute(
            "INSERT INTO feedback(user_id, text, error_code, source_url, message_key, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, text, error_code, source_url, message_key, now.isoformat()),
        )
        return int(cur.lastrowid)


def get_feedback(limit: int = 50, offset: int = 0, *, status: str = "open", user_id: int | None = None) -> list[dict]:
    """Возвращает обращения с профилями для административного интерфейса."""
    if status not in {"open", "closed", "all"}:
        raise ValueError("Invalid feedback status")
    with _cursor_read() as cur:
        cur.execute(
            """SELECT f.*, u.username, u.first_name, u.is_blocked
               FROM feedback f LEFT JOIN users u ON u.user_id = f.user_id
               WHERE (? = 'all' OR f.status = ?) AND (? IS NULL OR f.user_id = ?)
               ORDER BY f.id DESC LIMIT ? OFFSET ?""",
            (status, status, user_id, user_id, limit, offset),
        )
        return [dict(row) for row in cur.fetchall()]


def set_feedback_status(feedback_id: int, status: str) -> None:
    """Закрывает или повторно открывает обращение без удаления текста."""
    if status not in {"open", "closed"}:
        raise ValueError("Invalid feedback status")
    with _cursor_write() as cur:
        cur.execute("UPDATE feedback SET status = ? WHERE id = ?", (status, feedback_id))
        if cur.rowcount != 1:
            raise LookupError("Feedback not found")


def get_all_users(limit: int = 100, offset: int = 0) -> list[dict]:
    with _cursor_read() as cur:
        cur.execute(
            """
            WITH page AS (
                SELECT * FROM users ORDER BY last_seen DESC LIMIT ? OFFSET ?
            )
            SELECT u.user_id, u.username, u.first_name, u.last_name,
                   u.language_code, u.first_seen, u.last_seen,
                   u.is_blocked,
                   COUNT(e.id) as total_events,
                   SUM(CASE WHEN e.event = 'download' THEN 1 ELSE 0 END) as total_downloads
            FROM page u
            LEFT JOIN events e ON e.user_id = u.user_id
            GROUP BY u.user_id
            ORDER BY u.last_seen DESC
            """,
            (limit, offset),
        )
        return [dict(row) for row in cur.fetchall()]


def get_all_user_ids() -> list[int]:
    """Возвращает ID всех известных пользователей для админской рассылки."""
    with _cursor_read() as cur:
        cur.execute("SELECT user_id FROM users WHERE is_blocked = 0 ORDER BY last_seen DESC")
        return [int(row["user_id"]) for row in cur.fetchall()]


def get_user_detail(user_id: int) -> dict | None:
    with _cursor_read() as cur:
        cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if not row:
            return None
        user = dict(row)

        cur.execute(
            "SELECT COUNT(*) FROM events WHERE user_id = ? AND event = 'download'",
            (user_id,),
        )
        user["total_downloads"] = cur.fetchone()[0]

        cur.execute(
            """
            SELECT COALESCE(platform, 'unknown') as p, COUNT(*) as c
            FROM events
            WHERE user_id = ? AND event = 'download'
            GROUP BY p
            """,
            (user_id,),
        )
        user["downloads_by_platform"] = {row["p"]: row["c"] for row in cur.fetchall()}

        cur.execute(
            """
            SELECT event, platform, url, ts
            FROM events
            WHERE user_id = ?
            ORDER BY ts DESC
            LIMIT 50
            """,
            (user_id,),
        )
        user["recent_events"] = [dict(row) for row in cur.fetchall()]

        return user


def avg_downloads_per_user() -> float:
    """Среднее количество скачиваний на пользователя."""
    with _cursor_read() as cur:
        cur.execute("SELECT COUNT(*) FROM users")
        total = cur.fetchone()[0]
        if total == 0:
            return 0.0
        cur.execute("SELECT COUNT(*) FROM events WHERE event = 'download'")
        dl = cur.fetchone()[0]
        return round(dl / total, 1)


def repeat_users_rate() -> float:
    """% пользователей с более чем 1 скачиванием."""
    with _cursor_read() as cur:
        cur.execute("SELECT COUNT(*) FROM users")
        total = cur.fetchone()[0]
        if total == 0:
            return 0.0
        cur.execute("""
            SELECT COUNT(*) FROM (
                SELECT user_id FROM events WHERE event = 'download'
                GROUP BY user_id HAVING COUNT(*) > 1
            )
        """)
        repeat = cur.fetchone()[0]
        return round(repeat / total * 100, 1)


def engagement_score() -> float:
    """Индекс вовлечённости: DAU/MAU * 100 (stickiness ratio)."""
    dau = active_users(1)
    mau = active_users(30)
    if mau == 0:
        return 0.0
    return round(dau / mau * 100, 1)


def cohort_retention(weeks: int = 8) -> list[dict]:
    """Когортный анализ удержания по неделям регистрации.

    Возвращает список: [{week: "2026-W10", size: 15, w0: 100, w1: 60, w2: 40, ...}, ...]
    """
    since = (datetime.now(UTC) - timedelta(days=weeks * 7)).date().isoformat()
    with _cursor_read() as cur:
        # Получаем когорты (неделя регистрации)
        cur.execute("""
            SELECT strftime('%Y-W%W', first_seen) as cohort_week,
                   COUNT(*) as cohort_size
            FROM users
            WHERE first_seen >= ?
            GROUP BY cohort_week
            ORDER BY cohort_week
        """, (since,))
        cohorts = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """
            SELECT strftime('%Y-W%W', u.first_seen) AS cohort_week,
                   CAST((julianday(e.ts) - julianday(u.first_seen)) / 7 AS INTEGER) AS week_offset,
                   COUNT(DISTINCT e.user_id) AS returned
            FROM users u
            JOIN events e ON e.user_id = u.user_id
            WHERE u.first_seen >= ?
              AND e.ts >= u.first_seen
              AND julianday(e.ts) < julianday(u.first_seen) + ?
            GROUP BY cohort_week, week_offset
            """,
            (since, (weeks + 1) * 7),
        )
        counts = {
            (row["cohort_week"], row["week_offset"]): row["returned"]
            for row in cur.fetchall()
        }
    for cohort in cohorts:
        cohort["w0"] = 100.0
        for week in range(1, weeks + 1):
            returned = counts.get((cohort["cohort_week"], week), 0)
            cohort[f"w{week}"] = round(returned / cohort["cohort_size"] * 100, 1)
    return cohorts


def engagement_per_day(days: int = 30) -> list[dict]:
    """DAU/MAU (stickiness) по дням за период.

    Возвращает: [{day: "2026-03-22", dau: 5, stickiness: 25.0}, ...]
    """
    today = datetime.now(UTC).date()
    since = (today - timedelta(days=days + 29)).isoformat()
    until = (today + timedelta(days=1)).isoformat()
    with _cursor_read() as cur:
        cur.execute(
            """
            SELECT DISTINCT DATE(ts) as day, user_id
            FROM events
            WHERE ts >= ? AND ts < ?
            """,
            (since, until),
        )
        users_by_day: dict[str, set[int]] = defaultdict(set)
        for row in cur.fetchall():
            users_by_day[row["day"]].add(row["user_id"])

    window: Counter[int] = Counter()
    result = []
    first = today - timedelta(days=days + 29)
    for offset in range(days + 30):
        day = first + timedelta(days=offset)
        label = day.isoformat()
        window.update(users_by_day.get(label, ()))
        expired = (day - timedelta(days=30)).isoformat()
        for user_id in users_by_day.get(expired, ()):
            window[user_id] -= 1
            if window[user_id] == 0:
                del window[user_id]
        if offset >= 30 and users_by_day.get(label):
            dau = len(users_by_day[label])
            result.append(
                {"day": label, "dau": dau, "stickiness": round(dau / len(window) * 100, 1)}
            )
    return result


def platform_conversion() -> dict[str, dict]:
    """Конверсия по платформам: сколько уникальных пользователей скачивали с каждой."""
    with _cursor_read() as cur:
        cur.execute("SELECT COUNT(*) FROM users")
        total = cur.fetchone()[0]
        if total == 0:
            return {}
        cur.execute("""
            SELECT COALESCE(platform, 'unknown') as p,
                   COUNT(*) as downloads,
                   COUNT(DISTINCT user_id) as users
            FROM events WHERE event = 'download'
            GROUP BY p ORDER BY downloads DESC
        """)
        result = {}
        for row in cur.fetchall():
            p = row["p"]
            result[p] = {
                "downloads": row["downloads"],
                "users": row["users"],
                "pct_users": round(row["users"] / total * 100, 1),
            }
        return result


def dashboard_summary() -> dict:
    """Собирает все показатели из одного SQLite-снимка."""
    conn = _get_connection()
    conn.execute("BEGIN")
    try:
        return _dashboard_summary_values()
    finally:
        conn.execute("ROLLBACK")


def _dashboard_summary_values() -> dict:
    """Полная сводка для дашборда внутри открытого снимка."""
    return {
        "total_users": total_users(),
        "new_users_today": new_users(1),
        "new_users_7d": new_users(7),
        "new_users_30d": new_users(30),
        "active_today": active_users(1),
        "active_7d": active_users(7),
        "active_30d": active_users(30),
        "retention_3": retention(3),
        "retention_7": retention(7),
        "retention_30": retention(30),
        "churn_30": churn_rate(30),
        "total_downloads": total_downloads(),
        "total_deliveries": total_deliveries(),
        "downloads_by_platform": downloads_by_platform(),
        "downloads_per_day": downloads_per_day(30),
        "new_users_per_day": new_users_per_day(30),
        "popular_videos": popular_videos(10),
        # Продуктовые метрики
        "avg_downloads": avg_downloads_per_user(),
        "repeat_rate": repeat_users_rate(),
        "engagement": engagement_score(),
        "engagement_per_day": engagement_per_day(30),
        "cohorts": cohort_retention(8),
        "platform_conversion": platform_conversion(),
        # CSI метрики
        "csi": get_csi_metrics(),
    }


def create_csi_poll(user_id: int) -> str:
    """Выдает отдельный долговечный идентификатор приглашения."""
    import uuid

    token = uuid.uuid4().hex[:12]
    with _cursor_write() as cursor:
        cursor.execute("INSERT INTO csi_polls VALUES (?, ?, ?, NULL, ?)",
                       (token, user_id, user_id, datetime.now(UTC).isoformat()))
    return token


def bind_csi_poll(token: str, message_id: int) -> None:
    """Привязывает подтвержденное приглашение к сообщению Telegram."""
    with _cursor_write() as cursor:
        cursor.execute("UPDATE csi_polls SET message_id=? WHERE id=?", (message_id, token))


def save_csi_vote(user_id: int, rating: int, poll_id: str, chat_id: int, message_id: int) -> tuple[int, bool]:
    """Первый ответ на приглашение неизменяем; повтор не создает новую запись."""
    if not 0 <= rating <= 10:
        raise ValueError("Некорректная оценка")
    with _cursor_write() as cursor:
        poll = cursor.execute("SELECT * FROM csi_polls WHERE id=?", (poll_id,)).fetchone()
        if not poll or poll["user_id"] != user_id or poll["chat_id"] != chat_id or poll["message_id"] != message_id:
            raise ValueError("Опрос не принадлежит этому пользователю или сообщению")
        existing = cursor.execute("SELECT id FROM csi_responses WHERE user_id=? AND poll_id=?", (user_id, poll_id)).fetchone()
        if existing:
            return existing[0], False
        cursor.execute("INSERT INTO csi_responses (user_id, rating, poll_id, created_at) VALUES (?, ?, ?, ?)",
                       (user_id, rating, poll_id, datetime.now(UTC).isoformat()))
        return cursor.lastrowid, True
