"""Управление API-ключами в WebUI: CSRF, однократный показ, ротация, отзыв."""

import base64
import hashlib
import re

import pytest
from fastapi.testclient import TestClient

from utils import analytics_db, api_keys
from web import app as web
from web.i18n import RUSSIAN

KEY_PATTERN = re.compile(r"nuvio_[0-9A-Za-z]{38}")


@pytest.fixture
def client(tmp_path, monkeypatch):
    analytics_db.close_connection()
    monkeypatch.setattr(analytics_db, "_DB_PATH", tmp_path / "analytics.db")
    monkeypatch.setattr(web, "WEB_USERNAME", "test")
    monkeypatch.setattr(web, "WEB_PASSWORD_HASH", hashlib.pbkdf2_hmac("sha256", b"test-password", web.SALT, 100000))
    web._login_attempts.clear()
    web._revealed_keys.clear()
    with TestClient(web.app) as client:
        response = client.post("/login", data={"username": "test", "password": "test-password"}, follow_redirects=False)
        assert response.status_code == 303
        yield client
    analytics_db.close_connection()


def token(client):
    response = client.get("/api-keys")
    assert response.status_code == 200
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text)[1]


def create_key(client, name="Partner", expiry="30", expiry_date=""):
    response = client.post(
        "/api-keys",
        data={"csrf_token": token(client), "name": name, "expiry": expiry, "expiry_date": expiry_date},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/api-keys?saved=created"
    page = client.get("/api-keys?saved=created")
    return KEY_PATTERN.search(page.text)[0], page


def stored_rows():
    analytics_db.close_connection()
    with analytics_db._cursor_read() as cur:
        rows = [dict(row) for row in cur.execute("SELECT * FROM api_keys").fetchall()]
    analytics_db.close_connection()
    return rows


def session_payload(client) -> str:
    cookie = client.cookies.get("session")
    return base64.b64decode(cookie.split(".")[0] + "==").decode()


def test_page_requires_login(tmp_path, monkeypatch):
    monkeypatch.setattr(analytics_db, "_DB_PATH", tmp_path / "analytics.db")
    with TestClient(web.app) as anonymous:
        response = anonymous.get("/api-keys", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_empty_page_is_not_cached_and_linked_from_navigation(client):
    response = client.get("/api-keys")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "No API keys yet" in response.text
    assert 'href="/api-keys" class="nav-link active"' in response.text


def test_created_key_is_shown_once_and_only_its_hash_is_stored(client):
    key, page = create_key(client)

    assert "API key created" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert key not in client.get("/api-keys").text
    # Ключ не попадает ни в подписанную, но читаемую cookie, ни в URL.
    assert key not in session_payload(client)
    rows = stored_rows()
    assert len(rows) == 1
    assert rows[0]["key_hash"] == api_keys.hash_key(key)
    assert key not in repr(rows)
    assert rows[0]["prefix"] == key[:api_keys.DISPLAY_PREFIX_LENGTH]
    assert api_keys.authenticate_api_key(key).name == "Partner"


def test_create_rejects_a_forged_csrf_token(client):
    response = client.post("/api-keys", data={"csrf_token": "forged", "name": "Partner", "expiry": "30"})
    assert response.status_code == 403
    assert stored_rows() == []


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"name": "   ", "expiry": "30"}, "Key name is required"),
        ({"name": "x" * 65, "expiry": "30"}, "Key name must not exceed 64 characters"),
        ({"name": "Partner", "expiry": "custom", "expiry_date": "not-a-date"}, "Enter a valid expiry date"),
        ({"name": "Partner", "expiry": "custom", "expiry_date": "2000-01-01"}, "Expiry must be in the future"),
        ({"name": "Partner", "expiry": "custom", "expiry_date": "2099-12-31"}, "Expiry must be within 10 years"),
        ({"name": "Partner", "expiry": "13"}, "Unknown expiry option"),
        # `isdigit()` принимает надстрочные цифры, а `int()` на них падает.
        ({"name": "Partner", "expiry": "²"}, "Unknown expiry option"),
    ],
)
def test_invalid_create_shows_the_error_without_creating(client, data, message):
    response = client.post("/api-keys", data={"csrf_token": token(client), **data})
    assert response.status_code == 422
    assert message in response.text
    assert stored_rows() == []


def test_errors_are_translated(client):
    client.get("/language/ru?next=/api-keys")
    response = client.post("/api-keys", data={"csrf_token": token(client), "name": "", "expiry": "30"})
    assert response.status_code == 422
    assert "Укажите название ключа" in response.text


def test_never_expiring_key_and_custom_date(client):
    create_key(client, name="Forever", expiry="never")
    create_key(client, name="Dated", expiry="custom", expiry_date="2030-12-31")
    rows = {row["name"]: row for row in stored_rows()}
    assert rows["Forever"]["expires_at"] is None
    assert rows["Dated"]["expires_at"] == "2030-12-31T23:59:59+00:00"


def test_rotation_with_grace_keeps_the_old_key_working(client):
    old_key, _ = create_key(client)
    key_id = stored_rows()[0]["id"]

    response = client.post(
        f"/api-keys/{key_id}/rotate",
        data={"csrf_token": token(client), "grace_hours": "24"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/api-keys?saved=rotated"
    page = client.get("/api-keys?saved=rotated")
    new_key = KEY_PATTERN.search(page.text)[0]

    assert new_key != old_key
    assert "The previous key also works until" in page.text
    assert api_keys.authenticate_api_key(old_key).id == key_id
    assert api_keys.authenticate_api_key(new_key).id == key_id


def test_rotation_without_grace_disables_the_old_key(client):
    old_key, _ = create_key(client)
    key_id = stored_rows()[0]["id"]

    client.post(f"/api-keys/{key_id}/rotate", data={"csrf_token": token(client), "grace_hours": "0"})

    with pytest.raises(api_keys.InvalidApiKeyError):
        api_keys.authenticate_api_key(old_key)


@pytest.mark.parametrize("grace_hours", ["5", "-1", "soon", "²"])
def test_rotation_rejects_unknown_grace_periods(client, grace_hours):
    create_key(client)
    key_id = stored_rows()[0]["id"]
    response = client.post(f"/api-keys/{key_id}/rotate", data={"csrf_token": token(client), "grace_hours": grace_hours})
    assert response.status_code == 422
    assert "Unknown grace period" in response.text


def test_expiry_change_applies_to_the_key(client):
    create_key(client)
    key_id = stored_rows()[0]["id"]
    response = client.post(
        f"/api-keys/{key_id}/expiry",
        data={"csrf_token": token(client), "expiry": "never"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/api-keys?saved=expiry"
    assert stored_rows()[0]["expires_at"] is None


def test_revoke_then_delete(client):
    key, _ = create_key(client)
    key_id = stored_rows()[0]["id"]

    response = client.post(f"/api-keys/{key_id}/delete", data={"csrf_token": token(client)})
    assert response.status_code == 422
    assert "Revoke the key before deleting it" in response.text

    client.post(f"/api-keys/{key_id}/revoke", data={"csrf_token": token(client)})
    with pytest.raises(api_keys.InvalidApiKeyError):
        api_keys.authenticate_api_key(key)
    page = client.get("/api-keys")
    assert "Revoked" in page.text
    assert f"/api-keys/{key_id}/rotate" not in page.text

    response = client.post(f"/api-keys/{key_id}/rotate", data={"csrf_token": token(client), "grace_hours": "0"})
    assert response.status_code == 422
    assert "Revoked keys cannot be rotated" in response.text

    client.post(f"/api-keys/{key_id}/delete", data={"csrf_token": token(client)})
    assert stored_rows() == []


@pytest.mark.parametrize("action", ["revoke", "delete", "expiry", "rotate"])
def test_unknown_key_is_reported(client, action):
    data = {"csrf_token": token(client), "expiry": "30", "grace_hours": "0"}
    response = client.post(f"/api-keys/999/{action}", data=data)
    assert response.status_code == 404
    assert "API key not found" in response.text


@pytest.mark.parametrize("action", ["revoke", "delete", "expiry", "rotate"])
def test_key_actions_reject_a_forged_csrf_token(client, action):
    create_key(client)
    key_id = stored_rows()[0]["id"]
    before = stored_rows()
    response = client.post(f"/api-keys/{key_id}/{action}", data={"csrf_token": "forged", "expiry": "never", "grace_hours": "0"})
    assert response.status_code == 403
    assert stored_rows() == before


def test_page_copy_has_russian_translations():
    template = (web.WEB_DIR / "templates" / "api_keys.html").read_text(encoding="utf-8")
    messages = set(re.findall(r"(?<![\w.])t\('([^']+)'", template))
    messages |= set(web._API_KEY_NOTICES.values())
    source = (web.WEB_DIR.parent / "utils" / "api_keys.py").read_text(encoding="utf-8")
    messages |= set(re.findall(r'(?:ValueError|LookupError)\("([^"]+)"\)', source))
    missing = sorted(message for message in messages if message not in RUSSIAN)
    assert missing == []


@pytest.mark.parametrize(
    ("raw", "port"),
    [("9000", 9000), (" 9000 ", 9000), ("", 8090), ("0", 8090), ("70000", 8090), ("²", 8090), ("http", 8090)],
)
def test_api_port_in_examples_falls_back_like_the_api(monkeypatch, raw, port):
    monkeypatch.setenv("API_PORT", raw)
    assert web._api_port() == port
