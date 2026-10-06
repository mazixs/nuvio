"""Smoke-тесты WebUI без запуска внешнего сервера."""

import asyncio
import hashlib

import pytest
from fastapi.testclient import TestClient as FastAPITestClient

from web import app as web_app
from utils import analytics_db


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(web_app, "init_db", lambda: None)
    web_app._login_attempts.clear()
    web_app._notified_ips.clear()
    with FastAPITestClient(web_app.app) as test_client:
        yield test_client


def test_web_lifespan_closes_analytics_connection(tmp_path, monkeypatch):
    """Завершение WebUI должно освобождать его SQLite-соединение."""
    monkeypatch.setattr(analytics_db, "_DB_PATH", tmp_path / "analytics.db")
    if hasattr(analytics_db._local, "conn"):
        analytics_db._local.conn.close()
        delattr(analytics_db._local, "conn")

    async def scenario():
        async with web_app.lifespan(web_app.app):
            assert (tmp_path / "analytics.db").exists()
            assert not hasattr(analytics_db._local, "conn")

        assert not hasattr(analytics_db._local, "conn")

    asyncio.run(scenario())


def test_health_endpoint_is_public(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_dashboard_redirects_unauthenticated_user(client):
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_page_is_available(client):
    response = client.get("/login")

    assert response.status_code == 200
    assert "Nuvio" in response.text
    assert '<html lang="en">' in response.text
    assert "Analytics dashboard" in response.text


def test_valid_login_opens_authenticated_summary(client, monkeypatch):
    password = "safe-test-password"
    monkeypatch.setattr(web_app, "WEB_USERNAME", "operator")
    monkeypatch.setattr(
        web_app,
        "WEB_PASSWORD_HASH",
        hashlib.pbkdf2_hmac(
            "sha256",
            password.encode(),
            web_app.SALT,
            100000,
        ),
    )
    monkeypatch.setattr(
        web_app,
        "dashboard_summary",
        lambda: {"total_users": 3, "active_today": 2},
    )

    login_response = client.post(
        "/login",
        data={"username": "operator", "password": password},
        follow_redirects=False,
    )
    summary_response = client.get("/api/summary")

    assert login_response.status_code == 303
    assert login_response.headers["location"] == "/"
    assert summary_response.status_code == 200
    assert summary_response.json() == {"total_users": 3, "active_today": 2}


def test_invalid_login_does_not_authenticate(client):
    response = client.post(
        "/login",
        data={"username": "wrong", "password": "wrong"},
    )
    dashboard = client.get("/", follow_redirects=False)

    assert response.status_code == 200
    assert "Invalid username or password" in response.text
    assert dashboard.status_code == 303


def test_language_switch_persists_in_session(client):
    changed = client.get("/language/ru?next=/login", follow_redirects=False)
    response = client.get("/login")

    assert changed.status_code == 303
    assert changed.headers["location"] == "/login"
    assert '<html lang="ru">' in response.text
    assert "Панель аналитики" in response.text

    client.get("/logout")
    assert '<html lang="ru">' in client.get("/login").text

    client.get("/language/en?next=/login")
    assert '<html lang="en">' in client.get("/login").text


def test_language_switch_rejects_external_redirect(client):
    response = client.get("/language/ru?next=//example.com", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
