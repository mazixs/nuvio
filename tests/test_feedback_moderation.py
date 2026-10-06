"""Проверки обратной связи, миграции и административного ограничения доступа."""

import asyncio
import hashlib
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from telegram.ext import ApplicationHandlerStop

import config
from utils import analytics_db as db, feedback, user_access, telegram_utils
from utils.callback_fsm import CallbackEvent
from web import app as web


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "analytics.db")
    monkeypatch.setattr(db, "_local", threading.local())
    monkeypatch.setattr(config, "ADMIN_IDS", [999])
    monkeypatch.setattr(user_access, "ADMIN_IDS", [999])
    db.init_db()
    yield db
    db.close_connection()


def test_migration_preserves_legacy_users_and_wal(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE users(user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT, language_code TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO users VALUES(12, 'example', NULL, NULL, NULL, '2026-01-01', '2026-01-01')"
        )
    monkeypatch.setattr(db, "_DB_PATH", path)
    monkeypatch.setattr(db, "_local", threading.local())
    try:
        db.init_db()
        db.init_db()
        assert not db.is_user_blocked(12)
        assert db.get_user_detail(12)["username"] == "example"
        assert (
            db._get_connection().execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        )
    finally:
        db.close_connection()


def test_block_persists_across_connections_excludes_surveys_and_preserves_history(
    store,
):
    store.track_user(12, username="example")
    store.track_event(12, "download", platform="youtube")
    report_id = store.save_feedback(12, "Пример обращения")
    store.set_user_blocked(12, True, "Причина для оператора")
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(store.is_user_blocked, 12).result()
    assert 12 not in store.get_all_user_ids()
    assert 12 not in store.get_users_for_csi()
    store.track_user(12, username="renamed")
    assert store.is_user_blocked(12)
    with pytest.raises(store.UserBlockedError):
        store.save_feedback(12, "Повторный текст")
    store.set_user_blocked(12, False)
    assert 12 in store.get_all_user_ids()
    assert 12 in store.get_users_for_csi()
    assert store.get_feedback(status="all")[0]["id"] == report_id
    assert store.get_user_detail(12)["total_downloads"] == 1
    assert store.get_user_detail(12)["block_reason"] is None


def test_admin_protection_unknown_profile_and_validation(store):
    store.track_user(999)
    with pytest.raises(ValueError, match="Administrator"):
        store.set_user_blocked(999, True)
    with pytest.raises(LookupError):
        store.set_user_blocked(123, True)
    with pytest.raises(ValueError, match="500"):
        store.set_user_blocked(12, True, "x" * 501)
    assert not store.is_user_blocked(999)
    assert store.get_user_detail(999)["recent_events"] == []


def test_feedback_atomic_deduplication_cooldown_status_and_escaping(store):
    store.track_user(12)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: store.save_feedback(
                    12, "<script>alert(1)</script>", message_key="12:42"
                ),
                range(2),
            )
        )
    assert results[0] == results[1]
    with pytest.raises(store.FeedbackRateLimitError):
        store.save_feedback(12, "Еще обращение", message_key="12:43")
    with pytest.raises(ValueError):
        store.save_feedback(13, "a" * 2001)
    with pytest.raises(ValueError):
        store.save_feedback(13, "  a  ")
    report_id = results[0]
    store.set_feedback_status(report_id, "closed")
    assert store.get_feedback() == []
    assert store.get_feedback(status="closed", user_id=12)[0]["text"].startswith(
        "<script>"
    )
    store.set_feedback_status(report_id, "open")
    assert len(store.get_feedback()) == 1
    with pytest.raises(ValueError):
        store.get_feedback(status="invalid")
    with pytest.raises(LookupError):
        store.set_feedback_status(1000, "open")
    with store._cursor_write() as cursor:
        cursor.execute(
            "UPDATE feedback SET created_at = '2020-01-01' WHERE id = ?", (report_id,)
        )
    store.save_feedback(12, "Следующее обращение")
    assert len(store.get_feedback(limit=1, offset=1)) == 1


def fake_update(
    text="Пример обращения", *, callback=None, chat_type="private", user_id=12
):
    message = SimpleNamespace(text=text, message_id=42, reply_text=AsyncMock())
    query = SimpleNamespace(data=callback, answer=AsyncMock()) if callback else None
    return SimpleNamespace(
        effective_user=SimpleNamespace(
            id=user_id, username="example", first_name="Demo"
        ),
        effective_chat=SimpleNamespace(id=user_id, type=chat_type),
        effective_message=message,
        callback_query=query,
    )


def test_error_form_binds_owned_issue_saves_url_as_text_and_cancels_old_token(store):
    context = SimpleNamespace(user_data={"admin_broadcast_mode": True})
    code = "YT-UNAVAILA-ABC123"
    markup = feedback.feedback_markup(context, code, "https://example.test/video")
    assert markup.inline_keyboard[0][0].callback_data == f"feedback|open|{code}"
    update = fake_update(callback=f"feedback|open|{code}")
    asyncio.run(feedback.feedback_callback(update, context))
    form = context.user_data[feedback.FORM_KEY]
    assert "admin_broadcast_mode" not in context.user_data
    assert form["error_code"] == code
    old = form["token"]
    asyncio.run(feedback.feedback_command(update, context))
    asyncio.run(
        feedback.feedback_callback(
            fake_update(callback=f"feedback|cancel|{old}"), context
        )
    )
    assert feedback.FORM_KEY in context.user_data
    asyncio.run(feedback.feedback_callback(update, context))
    text_update = fake_update(text="Проблема https://example.test/media")
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(feedback.feedback_text(text_update, context))
    row = store.get_feedback()[0]
    assert row["source_url"] == "https://example.test/video"
    assert row["error_code"] == code
    assert row["text"] == text_update.effective_message.text
    assert feedback.FORM_KEY not in context.user_data
    assert f"#{row['id']}" in text_update.effective_message.reply_text.call_args.args[0]


def test_foreign_or_expired_issue_and_group_feedback_are_rejected(store):
    context = SimpleNamespace(user_data={})
    asyncio.run(
        feedback.feedback_callback(
            fake_update(callback="feedback|open|YT-ACCESS-ABC123"), context
        )
    )
    assert feedback.FORM_KEY not in context.user_data
    feedback.feedback_markup(context, "YT-ACCESS-ABC123")
    context.user_data[feedback.ISSUES_KEY]["YT-ACCESS-ABC123"]["created_at"] = 0
    asyncio.run(
        feedback.feedback_callback(
            fake_update(callback="feedback|open|YT-ACCESS-ABC123"), context
        )
    )
    assert feedback.FORM_KEY not in context.user_data
    asyncio.run(feedback.feedback_command(fake_update(chat_type="group"), context))
    assert feedback.FORM_KEY not in context.user_data


@pytest.mark.parametrize(
    "failure",
    [ValueError(), db.FeedbackRateLimitError(), RuntimeError("private diagnostics")],
)
def test_save_failure_keeps_form_never_claims_saved(store, monkeypatch, failure):
    context = SimpleNamespace(user_data={})
    update = fake_update()
    asyncio.run(feedback.feedback_command(update, context))

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(feedback, "save_feedback", fail)
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(feedback.feedback_text(update, context))
    assert feedback.FORM_KEY in context.user_data
    text = update.effective_message.reply_text.call_args.args[0]
    assert "private diagnostics" not in text
    assert "Обращение сохранено" not in text
    assert store.get_feedback() == []


def test_expired_text_is_not_processed_as_download(store):
    context = SimpleNamespace(user_data={})
    update = fake_update(text="https://example.test/media")
    asyncio.run(feedback.feedback_command(update, context))
    context.user_data[feedback.FORM_KEY]["created_at"] = 0
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(feedback.feedback_text(update, context))
    assert feedback.FORM_KEY not in context.user_data
    assert store.get_feedback() == []


@pytest.mark.parametrize(
    "callback", [None, "s|token|main|video", "feedback|open|general"]
)
def test_guard_stops_all_update_types_and_discards_pending_forms(store, callback):
    store.track_user(12)
    store.set_user_blocked(12, True, "Причина только для оператора")
    context = SimpleNamespace(
        user_data={feedback.FORM_KEY: {}, "awaiting_csi_feedback_id": 1}
    )
    update = fake_update(callback=callback)
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(user_access.guard_user_access(update, context))
    assert feedback.FORM_KEY not in context.user_data
    assert "awaiting_csi_feedback_id" not in context.user_data
    response = (
        update.callback_query.answer
        if callback
        else update.effective_message.reply_text
    )
    assert "оператора" not in str(response.call_args)
    assert "позже" not in str(response.call_args)


def test_guard_fails_closed_and_admin_is_protected(store, monkeypatch):
    def fail(*args):
        raise sqlite3.OperationalError("private database path")

    monkeypatch.setattr(user_access, "is_user_blocked", fail)
    update = fake_update()
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(
            user_access.guard_user_access(update, SimpleNamespace(user_data={}))
        )
    assert "private database" not in str(update.effective_message.reply_text.call_args)
    asyncio.run(
        user_access.guard_user_access(
            fake_update(user_id=999), SimpleNamespace(user_data={})
        )
    )


def test_pending_delivery_rechecks_block(store, monkeypatch):
    store.track_user(12)
    call = AsyncMock()

    async def scenario():
        token = user_access.active_user_id.set(12)
        try:
            store.set_user_blocked(12, True)
            with pytest.raises(store.UserBlockedError):
                await telegram_utils._call_telegram_with_retry_after(call)
        finally:
            user_access.active_user_id.reset(token)

    asyncio.run(scenario())
    call.assert_not_awaited()


@pytest.mark.parametrize(
    "data",
    [
        "feedback|open|general",
        "feedback|open|YT-DURATION-ABC123",
        "feedback|cancel|123456abcdef",
    ],
)
def test_feedback_callback_parsing(data):
    event = CallbackEvent.parse(data)
    assert event.scope == "feedback"


@pytest.mark.parametrize(
    "data",
    [
        "feedback|open|http://evil.test",
        "feedback|cancel|bad",
        "feedback|open|YT-ACCESS-ABC123|extra",
    ],
)
def test_feedback_callback_rejects_invalid_data(data):
    assert CallbackEvent.parse(data) is None


@pytest.fixture
def operator(store, monkeypatch):
    monkeypatch.setattr(web, "ADMIN_IDS", ["999"])
    monkeypatch.setattr(web, "WEB_USERNAME", "operator")
    monkeypatch.setattr(
        web,
        "WEB_PASSWORD_HASH",
        hashlib.pbkdf2_hmac("sha256", b"test-password", web.SALT, 100000),
    )
    web._login_attempts.clear()
    with TestClient(web.app) as client:
        client.post(
            "/login", data={"username": "operator", "password": "test-password"}
        )
        yield client


def csrf(client, path):
    response = client.get(path)
    assert response.status_code == 200
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


def test_web_feedback_requires_auth_escapes_input_and_changes_status(store, operator):
    store.track_user(12)
    report_id = store.save_feedback(
        12, '<script>alert("test")</script>', source_url="javascript:alert(1)"
    )
    with TestClient(web.app) as anonymous:
        assert anonymous.get("/feedback", follow_redirects=False).status_code == 303
        assert (
            anonymous.post(
                f"/feedback/{report_id}/status",
                data={"status": "closed"},
                follow_redirects=False,
            ).status_code
            == 303
        )
    token = csrf(operator, "/feedback")
    page = operator.get("/feedback").text
    assert "<script>alert" not in page
    assert "&lt;script&gt;" in page
    assert 'href="javascript:' not in page
    assert (
        operator.post(
            f"/feedback/{report_id}/status", data={"status": "closed"}
        ).status_code
        == 403
    )
    assert (
        operator.post(
            f"/feedback/{report_id}/status",
            data={"status": "closed", "csrf_token": token},
        ).status_code
        == 200
    )
    assert store.get_feedback() == []
    assert "&lt;script&gt;" in operator.get("/feedback?status=closed").text
    assert (
        operator.post(
            f"/feedback/{report_id}/status",
            data={"status": "open", "csrf_token": token},
        ).status_code
        == 200
    )
    assert len(store.get_feedback()) == 1
    assert operator.get("/feedback?status=wrong").status_code == 422


def test_web_moderation_csrf_admin_protection_and_localization(store, operator):
    store.track_user(12)
    store.track_user(999)
    token = csrf(operator, "/users/12")
    assert (
        operator.post(
            "/users/12/access", data={"action": "block", "csrf_token": "неверно"}
        ).status_code
        == 403
    )
    assert (
        operator.post("/users/12/access", data={"action": "block"}).status_code == 403
    )
    assert not store.is_user_blocked(12)
    assert (
        operator.post(
            "/users/999/access", data={"action": "block", "csrf_token": token}
        ).status_code
        == 422
    )
    assert (
        operator.post(
            "/users/12/access",
            data={"action": "block", "reason": "test", "csrf_token": token},
        ).status_code
        == 200
    )
    assert store.is_user_blocked(12)
    assert "Unblock user" in operator.get("/users/12").text
    assert (
        operator.post(
            "/users/12/access", data={"action": "unblock", "csrf_token": token}
        ).status_code
        == 200
    )
    assert not store.is_user_blocked(12)
    assert (
        operator.post(
            "/users/123/access", data={"action": "block", "csrf_token": token}
        ).status_code
        == 404
    )
    changed = operator.get("/language/ru?next=/feedback", follow_redirects=False)
    assert changed.headers["location"] == "/feedback"
    assert "Обратная связь" in operator.get("/feedback").text
    assert "Заблокировать пользователя" in operator.get("/users/12").text


def test_guard_and_feedback_stop_even_when_telegram_cannot_deliver_notice(store):
    store.track_user(12)
    store.set_user_blocked(12, True)
    update = fake_update()
    update.effective_message.reply_text.side_effect = RuntimeError(
        "Telegram unavailable"
    )
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(
            user_access.guard_user_access(update, SimpleNamespace(user_data={}))
        )
    store.set_user_blocked(12, False)
    context = SimpleNamespace(
        user_data={
            feedback.FORM_KEY: {
                "created_at": 0,
                "error_code": None,
                "source_url": None,
            }
        }
    )
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(feedback.feedback_text(update, context))


def test_block_while_telegram_rate_limit_waits_stops_retry(store, monkeypatch):
    from telegram.error import RetryAfter

    store.track_user(12)
    call = AsyncMock(side_effect=RetryAfter(1))

    async def pause(seconds):
        await asyncio.to_thread(store.set_user_blocked, 12, True)

    monkeypatch.setattr(telegram_utils.asyncio, "sleep", pause)

    async def scenario():
        token = user_access.active_user_id.set(12)
        try:
            with pytest.raises(store.UserBlockedError):
                await telegram_utils._call_telegram_with_retry_after(call)
        finally:
            user_access.active_user_id.reset(token)

    asyncio.run(scenario())
    assert call.await_count == 1


def test_registered_guard_precedes_all_existing_handlers(store, monkeypatch):
    import main
    from telegram.ext import ApplicationBuilder

    monkeypatch.setattr(
        main,
        "_configure_application_builder",
        lambda builder: ApplicationBuilder().token("123456:demo-test-token"),
    )
    application = main._build_application()
    assert application.handlers[-100][0].callback is user_access.guard_user_access
    assert application.handlers[-100][0].block is True
    assert application.handlers[-1][0].callback is feedback.feedback_text
    assert application.handlers[-1][0].block is True
    assert application.handlers[0][0].callback is feedback.feedback_command


def test_csi_selected_before_block_does_not_send(store):
    store.track_user(12)
    store.set_user_blocked(12, True)
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    with pytest.raises(store.UserBlockedError):
        asyncio.run(telegram_utils.send_csi_request(12, context))
    context.bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("block_during_retry", [False, True])
def test_broadcast_rechecks_webui_block_before_send_and_retry(
    store, monkeypatch, block_during_retry
):
    from utils import cookie_manager
    from telegram.error import RetryAfter

    store.track_user(12)
    store.track_user(13)
    monkeypatch.setattr(cookie_manager, "ADMIN_IDS", [999])
    monkeypatch.setattr(cookie_manager, "get_all_user_ids", lambda: [12, 13])
    monkeypatch.setattr(cookie_manager, "BROADCAST_SEND_DELAY_SECONDS", 0)
    update = fake_update(text="Объявление", user_id=999)
    update.message = update.effective_message
    update.message.reply_text.return_value = SimpleNamespace(edit_text=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock())
    context = SimpleNamespace(
        bot=bot, user_data={cookie_manager.ADMIN_BROADCAST_MODE_KEY: True}
    )
    if block_during_retry:
        bot.send_message.side_effect = [RetryAfter(1), None]

        async def sleep(seconds):
            await asyncio.to_thread(store.set_user_blocked, 12, True)

        monkeypatch.setattr(cookie_manager.asyncio, "sleep", sleep)
    else:
        store.set_user_blocked(12, True)
    asyncio.run(cookie_manager.handle_admin_text_input(update, context))
    ids = [call.kwargs["chat_id"] for call in bot.send_message.call_args_list]
    assert ids == ([12, 13] if block_during_retry else [13])
    text = update.message.reply_text.return_value.edit_text.call_args.args[0]
    assert "Доступ ограничен администратором: 1" in text


def test_delivery_error_feedback_button_preserves_back_action_and_context(
    store, monkeypatch, tmp_path
):
    context = SimpleNamespace(user_data={})
    query = SimpleNamespace(edit_message_text=AsyncMock())
    monkeypatch.setattr(
        telegram_utils, "_schedule_platform_failure_log", lambda **kwargs: None
    )
    result = asyncio.run(
        telegram_utils.send_single_file(
            query,
            tmp_path / "missing.mp4",
            "token123",
            {"platform": "youtube", "url": "https://example.test/video"},
            feedback_context=context,
        )
    )
    assert result is False
    buttons = query.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard
    assert any(
        button.callback_data == "s|token123|main|back"
        for row in buttons
        for button in row
    )
    code = next(iter(context.user_data[feedback.ISSUES_KEY]))
    assert code.startswith("FILE-FILE-")
    assert buttons[-1][0].callback_data == f"feedback|open|{code}"
    assert (
        context.user_data[feedback.ISSUES_KEY][code]["url"]
        == "https://example.test/video"
    )


def test_feedback_mode_leaves_on_other_controls_but_preserves_plain_slash_text(store):
    from telegram import MessageEntity

    async def scenario():
        context = SimpleNamespace(user_data={feedback.FORM_KEY: {"token": "current"}})
        update = fake_update(text="/Это текст обращения")
        await user_access.guard_user_access(update, context)
        assert feedback.FORM_KEY in context.user_data
        update = fake_update(text="/CANCEL")
        update.effective_message.entities = [
            MessageEntity(MessageEntity.BOT_COMMAND, 0, 7)
        ]
        await user_access.guard_user_access(update, context)
        assert feedback.FORM_KEY in context.user_data
        update = fake_update(text="/download")
        update.effective_message.entities = [
            MessageEntity(MessageEntity.BOT_COMMAND, 0, 9)
        ]
        await user_access.guard_user_access(update, context)
        assert feedback.FORM_KEY not in context.user_data
        context.user_data[feedback.FORM_KEY] = {"token": "current"}
        await user_access.guard_user_access(
            fake_update(callback="s|token|main|video"), context
        )
        assert feedback.FORM_KEY not in context.user_data

    asyncio.run(scenario())
