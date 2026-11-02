"""Ключи HTTP API: выпуск, ротация, срок действия, отзыв и проверка.

Ключ показывается администратору один раз, в базе лежит только SHA-256.
Случайная часть несёт около 190 бит энтропии, поэтому соль и медленный хеш
не нужны: утечка хеша не ускоряет перебор такого пространства, а быстрый хеш
позволяет искать ключ по индексу на каждом запросе.

Формат: `nuvio_` + 32 символа base62 + 6 символов контрольной суммы CRC32.
Префикс узнаётся сканерами секретов, только буквы и цифры выделяются двойным
щелчком целиком, а контрольная сумма отсекает опечатки и выдуманные ключи до
обращения к базе.

Время хранится в UTC ISO 8601 и сравнивается в Python, а не строками в SQL.
"""

import hashlib
import hmac
import logging
import secrets
import sqlite3
import zlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from utils import analytics_db

logger = logging.getLogger(__name__)

KEY_PREFIX = "nuvio_"
_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_ALPHABET_SET = frozenset(_ALPHABET)
_SECRET_LENGTH = 32
_CHECKSUM_LENGTH = 6
KEY_LENGTH = len(KEY_PREFIX) + _SECRET_LENGTH + _CHECKSUM_LENGTH
# Сколько символов ключа видно в списке, чтобы отличать ключи друг от друга.
DISPLAY_PREFIX_LENGTH = len(KEY_PREFIX) + 6

NAME_MAX_LENGTH = 64
MAX_LIFETIME_DAYS = 3650
EXPIRY_PRESET_DAYS = (7, 30, 90, 365)
DEFAULT_EXPIRY_DAYS = 90
# Сколько часов прежний ключ продолжает работать после ротации.
ROTATION_GRACE_HOURS = (0, 1, 24, 168)
# last_used_at обновляется не чаще раза в минуту: запись на каждый опрос
# статуса конкурировала бы с ботом за блокировку SQLite.
TOUCH_INTERVAL = timedelta(minutes=1)
# Время последней попытки отметки. В памяти, а не в базе: неудачная запись следа
# в базе не оставляет, и при занятой базе каждый запрос снова ждал бы блокировку.
_touch_attempts: dict[int, datetime] = {}


class InvalidApiKeyError(Exception):
    """Ключ не распознан, отозван или его льготный срок после ротации истёк."""


class ExpiredApiKeyError(Exception):
    """Срок действия ключа истёк."""


@dataclass(frozen=True)
class ApiKeyIdentity:
    """Владелец запроса без секретов."""

    id: int
    name: str
    prefix: str
    expires_at: datetime | None


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat(timespec="seconds") if moment else None


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _encode(number: int, length: int) -> str:
    chars = []
    for _ in range(length):
        number, digit = divmod(number, len(_ALPHABET))
        chars.append(_ALPHABET[digit])
    return "".join(reversed(chars))


def _checksum(secret: str) -> str:
    return _encode(zlib.crc32(secret.encode("ascii")), _CHECKSUM_LENGTH)


def generate_key() -> str:
    """Новый ключ из криптографически стойкого генератора."""
    secret = "".join(secrets.choice(_ALPHABET) for _ in range(_SECRET_LENGTH))
    return f"{KEY_PREFIX}{secret}{_checksum(secret)}"


def is_well_formed(key: str) -> bool:
    """Проверяет формат и контрольную сумму без обращения к базе."""
    if len(key) != KEY_LENGTH or not key.startswith(KEY_PREFIX):
        return False
    body = key[len(KEY_PREFIX):]
    if not set(body) <= _ALPHABET_SET:
        return False
    secret, checksum = body[:_SECRET_LENGTH], body[_SECRET_LENGTH:]
    return hmac.compare_digest(_checksum(secret), checksum)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _clean_name(name: str) -> str:
    cleaned = " ".join(name.split())
    if not cleaned:
        raise ValueError("Key name is required")
    if len(cleaned) > NAME_MAX_LENGTH:
        raise ValueError("Key name must not exceed 64 characters")
    return cleaned


def _check_expiry(expires_at: datetime | None, now: datetime) -> None:
    if expires_at is None:
        return
    if expires_at <= now:
        raise ValueError("Expiry must be in the future")
    if expires_at > now + timedelta(days=MAX_LIFETIME_DAYS):
        raise ValueError("Expiry must be within 10 years")


def parse_expiry(
    choice: str, custom_date: str = "", *, now: datetime | None = None
) -> datetime | None:
    """Переводит выбор из формы в момент истечения; None — бессрочно.

    Args:
        choice: Число дней из EXPIRY_PRESET_DAYS, `never` или `custom`.
        custom_date: Дата `YYYY-MM-DD` для `custom`; ключ действует до её конца по UTC.
    """
    now = now or _now()
    choice = choice.strip()
    if choice == "never":
        return None
    if choice == "custom":
        try:
            day = date.fromisoformat(custom_date.strip())
        except ValueError as error:
            raise ValueError("Enter a valid expiry date") from error
        expires_at = datetime.combine(day, time(23, 59, 59), tzinfo=UTC)
    # Сравнение со строками, а не isdigit(): тот пропускает «²», и int() падает
    # с текстом Python, который форма показала бы админу.
    elif choice in {str(days) for days in EXPIRY_PRESET_DAYS}:
        expires_at = now + timedelta(days=int(choice))
    else:
        raise ValueError("Unknown expiry option")
    _check_expiry(expires_at, now)
    return expires_at


def create_api_key(name: str, expires_at: datetime | None) -> tuple[int, str]:
    """Создаёт ключ и возвращает его id и единственную открытую копию."""
    name = _clean_name(name)
    now = _now()
    _check_expiry(expires_at, now)
    key = generate_key()
    with analytics_db._cursor_write() as cur:
        cur.execute(
            """INSERT INTO api_keys(name, prefix, key_hash, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?)""",
            (name, key[:DISPLAY_PREFIX_LENGTH], hash_key(key), _iso(now), _iso(expires_at)),
        )
        return cur.lastrowid, key


def rotate_api_key(key_id: int, grace_hours: int = 0) -> str:
    """Выпускает новый секрет для того же ключа.

    Имя, срок и счётчики сохраняются. Прежний секрет работает ещё
    ``grace_hours`` часов, чтобы клиенты успели перейти; повторная ротация
    в этот период заменяет прежний секрет предыдущим.
    """
    if grace_hours not in ROTATION_GRACE_HOURS:
        raise ValueError("Unknown grace period")
    key = generate_key()
    now = _now()
    with analytics_db._cursor_write() as cur:
        row = cur.execute(
            "SELECT key_hash, revoked_at FROM api_keys WHERE id = ?", (key_id,)
        ).fetchone()
        if row is None:
            raise LookupError("API key not found")
        if row["revoked_at"]:
            raise ValueError("Revoked keys cannot be rotated")
        previous_hash = row["key_hash"] if grace_hours else None
        previous_expires_at = _iso(now + timedelta(hours=grace_hours)) if grace_hours else None
        cur.execute(
            """UPDATE api_keys
               SET prefix = ?, key_hash = ?, previous_hash = ?,
                   previous_expires_at = ?, rotated_at = ?
               WHERE id = ?""",
            (
                key[:DISPLAY_PREFIX_LENGTH], hash_key(key), previous_hash,
                previous_expires_at, _iso(now), key_id,
            ),
        )
    return key


def _key_exists(cur, key_id: int) -> bool:
    return cur.execute("SELECT 1 FROM api_keys WHERE id = ?", (key_id,)).fetchone() is not None


def set_api_key_expiry(key_id: int, expires_at: datetime | None) -> None:
    """Продлевает или сокращает срок; действует со следующего запроса."""
    _check_expiry(expires_at, _now())
    with analytics_db._cursor_write() as cur:
        cur.execute(
            "UPDATE api_keys SET expires_at = ? WHERE id = ? AND revoked_at IS NULL",
            (_iso(expires_at), key_id),
        )
        if cur.rowcount == 1:
            return
        exists = _key_exists(cur, key_id)
    if not exists:
        raise LookupError("API key not found")
    raise ValueError("Revoked keys cannot be changed")


def revoke_api_key(key_id: int) -> None:
    """Отзывает ключ вместе с прежним секретом; запись остаётся для истории."""
    with analytics_db._cursor_write() as cur:
        cur.execute(
            """UPDATE api_keys
               SET revoked_at = ?, previous_hash = NULL, previous_expires_at = NULL
               WHERE id = ? AND revoked_at IS NULL""",
            (_iso(_now()), key_id),
        )
        if cur.rowcount == 1:
            return
        exists = _key_exists(cur, key_id)
    if not exists:
        raise LookupError("API key not found")


def delete_api_key(key_id: int) -> None:
    """Удаляет только отозванный ключ: два шага защищают от случайного удаления."""
    with analytics_db._cursor_write() as cur:
        cur.execute(
            "DELETE FROM api_keys WHERE id = ? AND revoked_at IS NOT NULL", (key_id,)
        )
        if cur.rowcount == 1:
            return
        exists = _key_exists(cur, key_id)
    if not exists:
        raise LookupError("API key not found")
    raise ValueError("Revoke the key before deleting it")


def list_api_keys() -> list[dict]:
    """Ключи для WebUI с вычисленным статусом, новые сверху."""
    now = _now()
    with analytics_db._cursor_read() as cur:
        rows = cur.execute(
            """SELECT id, name, prefix, previous_expires_at, created_at, rotated_at,
                      expires_at, revoked_at, last_used_at, downloads_total
               FROM api_keys ORDER BY id DESC"""
        ).fetchall()
    keys = []
    for row in rows:
        entry = dict(row)
        expires_at = _parse(row["expires_at"])
        grace_until = _parse(row["previous_expires_at"])
        if row["revoked_at"]:
            entry["status"] = "revoked"
        elif expires_at is not None and expires_at <= now:
            entry["status"] = "expired"
        else:
            entry["status"] = "active"
        entry["grace_until"] = (
            row["previous_expires_at"] if grace_until and grace_until > now else None
        )
        keys.append(entry)
    return keys


def authenticate_api_key(key: str) -> ApiKeyIdentity:
    """Находит владельца ключа и отмечает использование.

    Raises:
        InvalidApiKeyError: Ключ не распознан, отозван или его льготный срок истёк.
        ExpiredApiKeyError: Срок действия ключа истёк.
    """
    if not is_well_formed(key):
        raise InvalidApiKeyError()
    digest = hash_key(key)
    now = _now()
    with analytics_db._cursor_read() as cur:
        row = cur.execute(
            """SELECT id, name, prefix, key_hash, previous_expires_at,
                      expires_at, revoked_at
               FROM api_keys WHERE key_hash = ? OR previous_hash = ?""",
            (digest, digest),
        ).fetchone()
    if row is None or row["revoked_at"]:
        raise InvalidApiKeyError()
    if row["key_hash"] != digest:
        grace_until = _parse(row["previous_expires_at"])
        if grace_until is None or grace_until <= now:
            raise InvalidApiKeyError()
    expires_at = _parse(row["expires_at"])
    if expires_at is not None and expires_at <= now:
        raise ExpiredApiKeyError()
    attempted = _touch_attempts.get(row["id"])
    if attempted is None or now - attempted >= TOUCH_INTERVAL:
        _touch_attempts[row["id"]] = now
        try:
            with analytics_db._cursor_write() as cur:
                cur.execute(
                    "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
                    (_iso(now), row["id"]),
                )
        except sqlite3.OperationalError:
            # Отметка использования не стоит отказа в запросе.
            logger.warning("Не удалось отметить использование API-ключа %s", row["id"])
    return ApiKeyIdentity(
        id=row["id"], name=row["name"], prefix=row["prefix"], expires_at=expires_at
    )


def inactive_api_key_ids(key_ids: set[int]) -> set[int]:
    """Ключи из набора, которые отозваны или удалены: их задачи пора снять.

    Ключ с истёкшим сроком сюда не входит: админ может его продлить.
    """
    if not key_ids:
        return set()
    ids = sorted(key_ids)
    placeholders = ", ".join("?" * len(ids))
    with analytics_db._cursor_read() as cur:
        rows = cur.execute(
            f"SELECT id FROM api_keys WHERE revoked_at IS NULL AND id IN ({placeholders})",
            ids,
        ).fetchall()
    return key_ids - {row["id"] for row in rows}


def record_api_download(key_id: int) -> None:
    """Учитывает принятую задачу загрузки в счётчике ключа."""
    with analytics_db._cursor_write() as cur:
        cur.execute(
            "UPDATE api_keys SET downloads_total = downloads_total + 1 WHERE id = ?",
            (key_id,),
        )
