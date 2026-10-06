"""Настройки хранения общие для WebUI и бота, защищены от CSRF и сохраняются атомарно."""

import hashlib
import re

import pytest
from fastapi.testclient import TestClient

from utils import analytics_db
from utils.cache_policy import get_cache_policy
from web import app as web


@pytest.fixture
def client(tmp_path, monkeypatch):
    analytics_db.close_connection()
    monkeypatch.setattr(analytics_db, "_DB_PATH", tmp_path / "analytics.db")
    monkeypatch.setattr(web, "WEB_USERNAME", "test")
    monkeypatch.setattr(web, "WEB_PASSWORD_HASH", hashlib.pbkdf2_hmac("sha256", b"test-password", web.SALT, 100000))
    web._login_attempts.clear()
    with TestClient(web.app) as client:
        response = client.post("/login", data={"username": "test", "password": "test-password"}, follow_redirects=False)
        assert response.status_code == 303
        yield client
    analytics_db.close_connection()


def token(client):
    response = client.get("/settings")
    assert response.status_code == 200
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text)[1]


def test_cleanup_default_disabled_and_enable_persists(client):
    page = client.get("/settings")
    assert "Media cache" in page.text
    assert 'disabled aria-describedby="cache-retention-hint' in page.text
    assert not get_cache_policy()["enabled"]
    response = client.post("/settings/cache", data={"csrf_token": token(client), "cache_retention_days": "365", "cache_auto_cleanup_enabled": "true"})
    assert response.status_code == 200
    assert "Cache settings saved" in response.text
    analytics_db.close_connection()
    assert get_cache_policy()["enabled"] and get_cache_policy()["days"] == 365
    # Выключенное поле не отправляется браузером, но сохраненный срок не теряется.
    response = client.post("/settings/cache", data={"csrf_token": token(client)})
    assert response.status_code == 200
    assert get_cache_policy()["days"] == 365
    assert not get_cache_policy()["enabled"]


@pytest.mark.parametrize("days", ["0", "-1", "36501", "invalid"])
def test_invalid_retention_does_not_partially_enable_cleanup(client, days):
    response = client.post("/settings/cache", data={"csrf_token": token(client), "cache_retention_days": days, "cache_auto_cleanup_enabled": "true"})
    assert response.status_code == 422
    assert "Retention must be" in response.text
    assert not get_cache_policy()["enabled"]
    assert get_cache_policy()["days"] == 90


def test_cache_settings_reject_forged_csrf(client):
    response = client.post("/settings/cache", data={"csrf_token": "forged", "cache_retention_days": "1", "cache_auto_cleanup_enabled": "true"})
    assert response.status_code == 403
    assert not get_cache_policy()["enabled"]


def test_cache_settings_russian_copy_is_complete(client):
    client.get("/language/ru?next=/settings")
    response = client.get("/settings")
    assert "Кеш медиа" in response.text
    assert "Автоматически удалять" in response.text
    assert "Media cache" not in response.text
