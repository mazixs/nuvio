#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Тесты для Telegram-логики CSI (Customer Satisfaction Index).
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from utils import telegram_utils
from utils.telegram_utils import send_csi_request, button_callback, process_url

pytestmark = pytest.mark.anyio


class MockBot:
    def __init__(self):
        self.send_message = AsyncMock()


class MockContext:
    def __init__(self):
        self.bot = MockBot()
        self.user_data = {}


class MockQuery:
    def __init__(self, data):
        self.data = data
        self.answer = AsyncMock()
        self.edit_message_text = AsyncMock()
        self.from_user = MagicMock(id=123)
        self.message = MagicMock(message_id=77)
        self.message.chat.id = 123


class MockUpdate:
    def __init__(self, query_data=None, message_text=None):
        self.effective_user = MagicMock(id=123)
        if query_data is not None:
            self.callback_query = MockQuery(query_data)
        else:
            self.callback_query = None
        if message_text is not None:
            self.message = MagicMock(text=message_text)
            self.message.reply_text = AsyncMock()
        else:
            self.message = None


@pytest.mark.unit
async def test_send_csi_request_builds_keyboard(monkeypatch):
    """Проверяет что send_csi_request создаёт inline keyboard с оценками 0–10."""
    ctx = MockContext()
    monkeypatch.setattr(telegram_utils, "update_last_csi_sent", lambda uid: None)
    monkeypatch.setattr(telegram_utils, "create_csi_poll", lambda uid: "a" * 12)
    await send_csi_request(123, ctx)
    assert ctx.bot.send_message.called
    args = ctx.bot.send_message.call_args.kwargs
    assert "0" in args["text"]
    markup = args["reply_markup"]
    buttons = [btn for row in markup.inline_keyboard for btn in row]
    assert len(buttons) == 11
    assert buttons[0].callback_data == "csi|aaaaaaaaaaaa|0"
    assert buttons[5].callback_data == "csi|aaaaaaaaaaaa|5"
    assert buttons[10].callback_data == "csi|aaaaaaaaaaaa|10"


@pytest.fixture
def mock_csi_deps(monkeypatch):
    """Моки для зависимостей CSI callback."""
    monkeypatch.setattr(
        telegram_utils, "_should_rate_limit_callback", lambda data: False
    )
    monkeypatch.setattr(telegram_utils, "_check_spam", lambda *a, **k: False)


@pytest.mark.unit
async def test_button_callback_csi_high_rating(mock_csi_deps, monkeypatch):
    """При высокой оценке (≥7) сохраняется rating, feedback state не создаётся."""
    saved = {}

    def mock_save(uid, rating, *_args):
        saved["rating"] = rating
        return 42, True

    monkeypatch.setattr(telegram_utils, "save_csi_vote", mock_save)
    update = MockUpdate(query_data="csi|aaaaaaaaaaaa|9")
    ctx = MockContext()
    await button_callback(update, ctx)
    assert saved["rating"] == 9
    assert update.callback_query.edit_message_text.called
    assert "awaiting_csi_feedback_id" not in ctx.user_data
    assert not ctx.bot.send_message.called


@pytest.mark.unit
async def test_button_callback_csi_low_rating(mock_csi_deps, monkeypatch):
    """При низкой оценке (<7) сохраняется rating и запрашивается отзыв."""
    saved = {}

    def mock_save(uid, rating, *_args):
        saved["rating"] = rating
        return 42, True

    monkeypatch.setattr(telegram_utils, "save_csi_vote", mock_save)
    update = MockUpdate(query_data="csi|aaaaaaaaaaaa|5")
    ctx = MockContext()
    await button_callback(update, ctx)
    assert saved["rating"] == 5
    assert update.callback_query.edit_message_text.called
    assert ctx.user_data.get("awaiting_csi_feedback_id") == 42
    assert ctx.bot.send_message.called


@pytest.mark.unit
async def test_button_callback_csi_invalid_rating(mock_csi_deps, monkeypatch):
    """Некорректная оценка вызывает query.answer с предупреждением."""
    update = MockUpdate(query_data="csi|abc")
    ctx = MockContext()
    await button_callback(update, ctx)
    assert update.callback_query.answer.called
    assert not update.callback_query.edit_message_text.called


@pytest.mark.unit
async def test_process_url_csi_feedback(monkeypatch):
    """Текст без URL сохраняет отзыв при awaiting_csi_feedback_id."""
    feedback_calls = []

    def mock_update_feedback(csi_id, text):
        feedback_calls.append((csi_id, text))

    monkeypatch.setattr(telegram_utils, "update_csi_feedback", mock_update_feedback)
    monkeypatch.setattr(telegram_utils, "_check_spam", lambda *a, **k: False)

    update = MockUpdate(message_text="Медленно загружает")
    ctx = MockContext()
    ctx.user_data["awaiting_csi_feedback_id"] = 7
    await process_url(update, ctx, url=None)
    assert feedback_calls == [(7, "Медленно загружает")]
    assert "awaiting_csi_feedback_id" not in ctx.user_data
    assert update.message.reply_text.called


@pytest.mark.unit
async def test_process_url_csi_link_resets_state(monkeypatch):
    """URL сбрасывает awaiting_csi_feedback_id и продолжает обработку."""
    monkeypatch.setattr(telegram_utils, "_check_spam", lambda *a, **k: False)
    monkeypatch.setattr(telegram_utils, "_track_tg_user", lambda *a, **k: None)
    monkeypatch.setattr(telegram_utils, "is_valid_youtube_url", lambda u: True)
    monkeypatch.setattr(telegram_utils, "is_valid_tiktok_url", lambda u: False)
    monkeypatch.setattr(telegram_utils, "is_valid_instagram_url", lambda u: False)
    monkeypatch.setattr(telegram_utils, "is_valid_rutube_url", lambda u: False)
    monkeypatch.setattr(telegram_utils, "is_valid_vk_url", lambda u: False)

    update = MockUpdate(message_text="https://youtube.com/watch?v=test")
    ctx = MockContext()
    ctx.user_data["awaiting_csi_feedback_id"] = 7

    # process_url упадёт на get_video_info, но state уже должен быть сброшен
    try:
        await process_url(update, ctx, url=None)
    except Exception:
        pass

    assert "awaiting_csi_feedback_id" not in ctx.user_data
