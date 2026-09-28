"""Частота CSI-опросов настраивается из WebUI, а не правкой кода.

Причина появления: доля пользователей, удаливших и заблокировавших бота,
выросла, а опрос приходил каждые 7 дней и был вкопан в вызов
`get_users_for_csi(days_since_last=7)`. Чтобы менять частоту без релиза,
значение живёт в БД аналитики — её видят оба процесса, бот и WebUI.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient as FastAPITestClient

from utils import analytics_db


pytestmark = pytest.mark.integration


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(analytics_db, "_DB_PATH", tmp_path / "analytics_test.db")
    if hasattr(analytics_db._local, "conn"):
        analytics_db._local.conn.close()
        delattr(analytics_db._local, "conn")
    analytics_db.init_db()
    yield analytics_db
    if hasattr(analytics_db._local, "conn"):
        analytics_db._local.conn.close()
        delattr(analytics_db._local, "conn")


# ── Хранение значения ─────────────────────────────────────────────


def test_default_interval_is_two_weeks(fresh_db):
    """По умолчанию — раз в две недели: 7 дней оказались слишком назойливы."""
    assert fresh_db.get_csi_interval_days() == 14


def test_saved_interval_survives_a_reconnect(fresh_db):
    """Настройку меняет WebUI, а читает другой процесс — только через БД."""
    fresh_db.set_csi_interval_days(30)

    fresh_db._local.conn.close()
    delattr(fresh_db._local, "conn")

    assert fresh_db.get_csi_interval_days() == 30


@pytest.mark.parametrize("bad", [0, -5, 366, 10_000])
def test_absurd_interval_is_rejected(fresh_db, bad):
    """Ноль означал бы опрос при каждом запуске job'а — это спам."""
    with pytest.raises(ValueError):
        fresh_db.set_csi_interval_days(bad)

    assert fresh_db.get_csi_interval_days() == 14


def test_broken_stored_value_does_not_break_the_dispatch(fresh_db):
    """Мусор в БД не должен ронять рассылку — отдаём значение по умолчанию."""
    fresh_db.set_setting("csi_interval_days", "не число")

    assert fresh_db.get_csi_interval_days() == 14


# ── Рассылка использует настройку ────────────────────────────────


def test_dispatch_asks_for_the_configured_interval(monkeypatch):
    """Иначе настройка есть, а рассылка живёт по своему вкопанному числу."""
    import asyncio

    import main

    captured = {}

    def _fake_select(days_since_last, min_active_days):
        captured["days"] = days_since_last
        return []

    monkeypatch.setattr(main, "get_users_for_csi", _fake_select)
    monkeypatch.setattr(main, "get_csi_interval_days", lambda: 21)

    asyncio.run(main.scheduled_csi_dispatch(object()))

    assert captured["days"] == 21


# ── WebUI ─────────────────────────────────────────────────────────


@pytest.fixture
def authenticated_client(monkeypatch):
    import hashlib

    from web import app as web_app

    password = "safe-test-password"
    monkeypatch.setattr(web_app, "init_db", lambda: None)
    monkeypatch.setattr(web_app, "WEB_USERNAME", "operator")
    monkeypatch.setattr(
        web_app,
        "WEB_PASSWORD_HASH",
        hashlib.pbkdf2_hmac("sha256", password.encode(), web_app.SALT, 100000),
    )
    web_app._login_attempts.clear()
    with FastAPITestClient(web_app.app) as client:
        client.post("/login", data={"username": "operator", "password": password})
        yield client


def test_settings_page_shows_the_current_interval(authenticated_client, monkeypatch):
    from web import app as web_app

    monkeypatch.setattr(web_app, "get_csi_interval_days", lambda: 14)

    response = authenticated_client.get("/settings")

    assert response.status_code == 200
    assert 'value="14"' in response.text


def test_operator_can_change_the_interval(authenticated_client, monkeypatch):
    from web import app as web_app

    saved = []
    monkeypatch.setattr(web_app, "set_csi_interval_days", saved.append)
    monkeypatch.setattr(web_app, "get_csi_interval_days", lambda: 14)

    response = authenticated_client.post(
        "/settings", data={"csi_interval_days": "30"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert saved == [30]


def test_rejected_value_is_explained_instead_of_crashing(
    authenticated_client, monkeypatch
):
    from web import app as web_app

    def _reject(value):
        raise ValueError("интервал должен быть от 1 до 365 дней")

    monkeypatch.setattr(web_app, "set_csi_interval_days", _reject)
    monkeypatch.setattr(web_app, "get_csi_interval_days", lambda: 14)

    response = authenticated_client.post("/settings", data={"csi_interval_days": "0"})

    assert response.status_code == 200
    assert "365" in response.text


# ── Прибор: зоны, дорожка, предпросмотр ──────────────────────────


@pytest.mark.parametrize(
    "days, zone",
    [(1, "dense"), (7, "dense"), (8, "balanced"), (45, "balanced"), (46, "sparse")],
)
def test_zone_boundaries(days, zone):
    """Недельный опрос — уже «часто»: именно от него растут блокировки."""
    from web import app as web_app

    assert web_app._csi_zone(days)[0] == zone


def test_track_shows_every_dispatch_inside_the_horizon():
    """Тики — не декор: их столько, сколько опросов реально уйдёт за 90 дней."""
    from web import app as web_app

    assert web_app._csi_dispatch_offsets(14) == [0, 14, 28, 42, 56, 70, 84]
    assert len(web_app._csi_dispatch_offsets(7)) == 13
    assert web_app._csi_dispatch_offsets(90) == [0, 90]


def test_queue_size_comes_from_the_dispatch_query(monkeypatch):
    """Число в «очереди» обязано быть тем же, что увидит рассылка."""
    from web import app as web_app

    seen = {}

    def _select(days_since_last, min_active_days):
        seen["args"] = (days_since_last, min_active_days)
        return [1, 2, 3]

    monkeypatch.setattr(web_app, "get_users_for_csi", _select)

    assert web_app._csi_queue_size(21) == 3
    assert seen["args"] == (21, 1)


def test_unreadable_database_shows_a_dash_not_a_number(monkeypatch):
    """Выдуманное число хуже прочерка: по нему примут решение."""
    from web import app as web_app

    def _boom(**kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(web_app, "get_users_for_csi", _boom)

    assert web_app._csi_queue_size(14) is None


def test_preview_endpoint_reports_the_examined_interval(
    authenticated_client, monkeypatch
):
    from web import app as web_app

    monkeypatch.setattr(web_app, "get_users_for_csi", lambda **kwargs: [1, 2])

    payload = authenticated_client.get("/api/csi/preview?days=7").json()

    assert payload["days"] == 7
    assert payload["zone"] == "dense"
    assert payload["cadence"] == "once a week"
    assert payload["unit"] == "days"
    assert payload["queue_size"] == 2
    assert payload["queue_hint"] == "people will receive a survey at the next check"
    assert payload["per_year"] == 52
    assert len(payload["offsets"]) == 13


def test_settings_preview_uses_selected_language(authenticated_client, monkeypatch):
    from web import app as web_app

    monkeypatch.setattr(web_app, "get_csi_interval_days", lambda: 14)
    monkeypatch.setattr(
        web_app, "get_csi_metrics", lambda: {"total_responses": 0, "avg_rating": 0}
    )
    monkeypatch.setattr(web_app, "get_users_for_csi", lambda **kwargs: [])
    authenticated_client.get("/language/ru?next=/settings")

    page = authenticated_client.get("/settings")
    preview = authenticated_client.get("/api/csi/preview?days=7").json()

    assert '<html lang="ru">' in page.text
    assert "Сохранить интервал" in page.text
    assert preview["cadence"] == "раз в неделю"


@pytest.mark.parametrize(
    "days, english, russian",
    [(1, "day", "день"), (2, "days", "дня"), (5, "days", "дней"), (21, "days", "день")],
)
def test_interval_units_follow_the_selected_language(days, english, russian):
    from web.i18n import day_unit

    assert day_unit("en", days) == english
    assert day_unit("ru", days) == russian


def test_preview_rejects_an_interval_the_form_would_reject(authenticated_client):
    """Иначе предпросмотр обещает то, чего сохранение не примет."""
    assert authenticated_client.get("/api/csi/preview?days=0").status_code == 422
    assert authenticated_client.get("/api/csi/preview?days=400").status_code == 422


def test_preview_is_closed_to_anonymous_requests(monkeypatch):
    """Очередь — это данные о пользователях, а не публичная справка."""
    from web import app as web_app

    monkeypatch.setattr(web_app, "init_db", lambda: None)
    web_app._login_attempts.clear()

    with FastAPITestClient(web_app.app) as client:
        response = client.get("/api/csi/preview?days=14", follow_redirects=False)

    assert response.status_code == 303


def test_cadence_wording_lives_only_on_the_server():
    """Формулировка интервала не должна дублироваться в JavaScript.

    Две реализации одного правила разойдутся: страница начнёт писать «раз в
    2 недели» там, где сервер говорит «раз в две недели».
    """
    template = (
        Path(__file__).resolve().parents[1] / "web" / "templates" / "settings.html"
    ).read_text(encoding="utf-8")
    script = template.split("<script>")[1]

    for phrase in ("раз в неделю", "раз в две недели", "раз в квартал"):
        assert phrase not in script


def test_state_changing_post_is_protected_from_cross_site_requests():
    """У `/settings` нет CSRF-токена — вся защита в `SameSite` cookie сессии.

    Если у SessionMiddleware когда-нибудь выставят `same_site="none"`, сторонний
    сайт сможет менять частоту опросов POST-запросом от имени залогиненного
    оператора. Тогда токен придётся добавлять.
    """
    from starlette.middleware.sessions import SessionMiddleware

    from web import app as web_app

    session_layers = [
        middleware
        for middleware in web_app.app.user_middleware
        if middleware.cls is SessionMiddleware
    ]

    assert len(session_layers) == 1
    assert session_layers[0].kwargs.get("same_site", "lax") in {"lax", "strict"}


def test_settings_are_not_writable_without_login(monkeypatch):
    """Страница меняет поведение бота — она обязана быть за логином."""
    from web import app as web_app

    monkeypatch.setattr(web_app, "init_db", lambda: None)
    calls = []
    monkeypatch.setattr(web_app, "set_csi_interval_days", calls.append)
    web_app._login_attempts.clear()

    with FastAPITestClient(web_app.app) as client:
        page = client.get("/settings", follow_redirects=False)
        write = client.post(
            "/settings", data={"csi_interval_days": "1"}, follow_redirects=False
        )

    assert page.status_code == 303
    assert write.status_code == 303
    assert calls == []
