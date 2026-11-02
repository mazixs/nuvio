"""Ключи HTTP API: формат, хранение, срок, ротация с льготным периодом и отзыв."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from utils import analytics_db, api_keys


@pytest.fixture
def db(tmp_path, monkeypatch):
    analytics_db.close_connection()
    monkeypatch.setattr(analytics_db, "_DB_PATH", tmp_path / "analytics.db")
    monkeypatch.setattr(api_keys, "_touch_attempts", {})
    analytics_db.init_db()
    yield
    analytics_db.close_connection()


@pytest.fixture
def clock(monkeypatch):
    """Управляемые часы модуля ключей."""
    moment = {"now": datetime(2026, 10, 31, 12, 0, tzinfo=UTC)}
    monkeypatch.setattr(api_keys, "_now", lambda: moment["now"])
    return moment


def row(key_id):
    with analytics_db._cursor_read() as cur:
        return dict(cur.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone())


def test_generated_keys_are_well_formed_and_unique():
    keys = {api_keys.generate_key() for _ in range(200)}
    assert len(keys) == 200
    for key in keys:
        assert len(key) == api_keys.KEY_LENGTH == 44
        assert key.startswith("nuvio_")
        assert key[len("nuvio_"):].isalnum()
        assert api_keys.is_well_formed(key)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda key: key[:-1] + ("A" if key[-1] != "A" else "B"),
        lambda key: key[:10] + ("x" if key[10] != "x" else "y") + key[11:],
        lambda key: key[:-1],
        lambda key: "other_" + key[len("nuvio_"):],
        lambda key: key[:-2] + "-_",
        lambda key: "",
    ],
)
def test_typos_and_foreign_tokens_fail_the_checksum(mutate):
    assert not api_keys.is_well_formed(mutate(api_keys.generate_key()))


def test_only_the_hash_and_display_prefix_are_stored(db):
    key_id, key = api_keys.create_api_key("  Partner   one ", None)
    stored = row(key_id)
    assert stored["name"] == "Partner one"
    assert stored["key_hash"] == api_keys.hash_key(key)
    assert stored["prefix"] == key[:12]
    assert key not in repr(stored)


@pytest.mark.parametrize(
    ("choice", "custom", "expected"),
    [
        ("never", "", None),
        ("7", "", datetime(2026, 11, 7, 12, 0, tzinfo=UTC)),
        (" 90 ", "", datetime(2027, 1, 29, 12, 0, tzinfo=UTC)),
        ("custom", "2026-11-01", datetime(2026, 11, 1, 23, 59, 59, tzinfo=UTC)),
        ("custom", "2026-10-31", datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC)),
    ],
)
def test_parse_expiry(choice, custom, expected):
    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    assert api_keys.parse_expiry(choice, custom, now=now) == expected


@pytest.mark.parametrize(
    ("choice", "custom", "message"),
    [
        ("custom", "2026-10-30", "Expiry must be in the future"),
        ("custom", "2036-11-01", "Expiry must be within 10 years"),
        ("custom", "31.12.2026", "Enter a valid expiry date"),
        ("custom", "", "Enter a valid expiry date"),
        ("14", "", "Unknown expiry option"),
        ("-7", "", "Unknown expiry option"),
        ("", "", "Unknown expiry option"),
    ],
)
def test_parse_expiry_rejects(choice, custom, message):
    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match=message):
        api_keys.parse_expiry(choice, custom, now=now)


@pytest.mark.parametrize(("name", "message"), [("", "Key name is required"), ("x" * 65, "64 characters")])
def test_create_validates_the_name(db, name, message):
    with pytest.raises(ValueError, match=message):
        api_keys.create_api_key(name, None)


def test_create_rejects_a_past_expiry(db, clock):
    with pytest.raises(ValueError, match="in the future"):
        api_keys.create_api_key("Partner", clock["now"] - timedelta(seconds=1))


def test_authenticate_returns_the_owner_and_touches_last_used(db, clock):
    expires_at = clock["now"] + timedelta(days=30)
    key_id, key = api_keys.create_api_key("Partner", expires_at)

    identity = api_keys.authenticate_api_key(key)

    assert identity == api_keys.ApiKeyIdentity(
        id=key_id, name="Partner", prefix=key[:12], expires_at=expires_at
    )
    assert row(key_id)["last_used_at"] == "2026-10-31T12:00:00+00:00"


def test_last_used_is_written_at_most_once_a_minute(db, clock):
    key_id, key = api_keys.create_api_key("Partner", None)
    api_keys.authenticate_api_key(key)
    clock["now"] += timedelta(seconds=59)
    api_keys.authenticate_api_key(key)
    assert row(key_id)["last_used_at"] == "2026-10-31T12:00:00+00:00"
    clock["now"] += timedelta(seconds=1)
    api_keys.authenticate_api_key(key)
    assert row(key_id)["last_used_at"] == "2026-10-31T12:01:00+00:00"


def test_busy_database_is_not_waited_for_on_every_request(db, clock, monkeypatch):
    _, key = api_keys.create_api_key("Partner", None)
    attempts = []

    def busy():
        attempts.append(clock["now"])
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(analytics_db, "_cursor_write", busy)
    api_keys.authenticate_api_key(key)
    api_keys.authenticate_api_key(key)
    assert len(attempts) == 1
    clock["now"] += api_keys.TOUCH_INTERVAL
    api_keys.authenticate_api_key(key)
    assert len(attempts) == 2


def test_unknown_and_malformed_keys_are_rejected(db):
    api_keys.create_api_key("Partner", None)
    with pytest.raises(api_keys.InvalidApiKeyError):
        api_keys.authenticate_api_key(api_keys.generate_key())
    with pytest.raises(api_keys.InvalidApiKeyError):
        api_keys.authenticate_api_key("nuvio_short")


def test_expired_key_is_reported_separately(db, clock):
    _, key = api_keys.create_api_key("Partner", clock["now"] + timedelta(days=7))
    clock["now"] += timedelta(days=7)
    with pytest.raises(api_keys.ExpiredApiKeyError):
        api_keys.authenticate_api_key(key)


def test_expiry_can_be_extended_and_removed(db, clock):
    key_id, key = api_keys.create_api_key("Partner", clock["now"] + timedelta(days=7))
    api_keys.set_api_key_expiry(key_id, clock["now"] + timedelta(days=30))
    clock["now"] += timedelta(days=8)
    assert api_keys.authenticate_api_key(key).id == key_id
    api_keys.set_api_key_expiry(key_id, None)
    assert row(key_id)["expires_at"] is None


def test_rotation_keeps_name_expiry_and_counters(db, clock):
    expires_at = clock["now"] + timedelta(days=30)
    key_id, old_key = api_keys.create_api_key("Partner", expires_at)
    api_keys.record_api_download(key_id)

    new_key = api_keys.rotate_api_key(key_id)

    stored = row(key_id)
    assert new_key != old_key
    assert stored["name"] == "Partner"
    assert stored["expires_at"] == expires_at.isoformat(timespec="seconds")
    assert stored["downloads_total"] == 1
    assert stored["prefix"] == new_key[:12]
    assert stored["previous_hash"] is None
    assert api_keys.authenticate_api_key(new_key).id == key_id
    with pytest.raises(api_keys.InvalidApiKeyError):
        api_keys.authenticate_api_key(old_key)


def test_old_key_works_only_during_the_grace_period(db, clock):
    key_id, old_key = api_keys.create_api_key("Partner", None)
    new_key = api_keys.rotate_api_key(key_id, grace_hours=24)

    assert api_keys.authenticate_api_key(old_key).id == key_id
    assert api_keys.list_api_keys()[0]["grace_until"] == "2026-11-01T12:00:00+00:00"

    clock["now"] += timedelta(hours=24)
    with pytest.raises(api_keys.InvalidApiKeyError):
        api_keys.authenticate_api_key(old_key)
    assert api_keys.authenticate_api_key(new_key).id == key_id
    assert api_keys.list_api_keys()[0]["grace_until"] is None


def test_second_rotation_retires_the_first_secret(db):
    key_id, first = api_keys.create_api_key("Partner", None)
    second = api_keys.rotate_api_key(key_id, grace_hours=24)
    third = api_keys.rotate_api_key(key_id, grace_hours=24)

    with pytest.raises(api_keys.InvalidApiKeyError):
        api_keys.authenticate_api_key(first)
    assert api_keys.authenticate_api_key(second).id == key_id
    assert api_keys.authenticate_api_key(third).id == key_id


def test_rotation_rejects_unknown_grace_and_missing_keys(db):
    key_id, _ = api_keys.create_api_key("Partner", None)
    with pytest.raises(ValueError, match="Unknown grace period"):
        api_keys.rotate_api_key(key_id, grace_hours=5)
    with pytest.raises(LookupError):
        api_keys.rotate_api_key(key_id + 1)


def test_revocation_disables_current_and_previous_secrets(db):
    key_id, old_key = api_keys.create_api_key("Partner", None)
    new_key = api_keys.rotate_api_key(key_id, grace_hours=24)

    api_keys.revoke_api_key(key_id)
    api_keys.revoke_api_key(key_id)  # повторный отзыв ничего не ломает

    for key in (old_key, new_key):
        with pytest.raises(api_keys.InvalidApiKeyError):
            api_keys.authenticate_api_key(key)
    with pytest.raises(ValueError, match="cannot be rotated"):
        api_keys.rotate_api_key(key_id)
    with pytest.raises(ValueError, match="cannot be changed"):
        api_keys.set_api_key_expiry(key_id, None)
    assert api_keys.list_api_keys()[0]["status"] == "revoked"


def test_delete_requires_revocation_first(db):
    key_id, _ = api_keys.create_api_key("Partner", None)
    with pytest.raises(ValueError, match="Revoke the key"):
        api_keys.delete_api_key(key_id)
    api_keys.revoke_api_key(key_id)
    api_keys.delete_api_key(key_id)
    assert api_keys.list_api_keys() == []
    for action in (api_keys.delete_api_key, api_keys.revoke_api_key):
        with pytest.raises(LookupError):
            action(key_id)


def test_list_reports_statuses_newest_first(db, clock):
    active_id, _ = api_keys.create_api_key("Active", None)
    expiring_id, _ = api_keys.create_api_key("Expiring", clock["now"] + timedelta(days=7))
    revoked_id, _ = api_keys.create_api_key("Revoked", None)
    api_keys.revoke_api_key(revoked_id)
    clock["now"] += timedelta(days=8)

    listed = api_keys.list_api_keys()

    assert [entry["id"] for entry in listed] == [revoked_id, expiring_id, active_id]
    assert [entry["status"] for entry in listed] == ["revoked", "expired", "active"]
    assert all("key_hash" not in entry and "previous_hash" not in entry for entry in listed)


def test_download_counter(db):
    key_id, _ = api_keys.create_api_key("Partner", None)
    for _ in range(3):
        api_keys.record_api_download(key_id)
    assert api_keys.list_api_keys()[0]["downloads_total"] == 3
