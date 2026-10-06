"""Единая проверка административной блокировки на входе в бот и при отправке."""

import asyncio
import logging
from contextvars import ContextVar

from telegram import MessageEntity, Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from config import ADMIN_IDS
from messages import USER_ACCESS_UNAVAILABLE, USER_BLOCKED_MESSAGE
from utils.analytics_db import UserBlockedError, is_user_blocked
from utils.db_worker import run_db

logger = logging.getLogger(__name__)
active_user_id: ContextVar[int | None] = ContextVar("bot_active_user_id", default=None)


async def assert_user_allowed(user_id: int) -> None:
    """Не использует кеш, чтобы изменение из WebUI применялось сразу."""
    if user_id not in ADMIN_IDS and await run_db(is_user_blocked, user_id):
        raise UserBlockedError()


async def guard_user_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Останавливает все группы обработчиков для заблокированного пользователя."""
    user = update.effective_user
    active_user_id.set(user.id if user else None)
    message = update.effective_message
    entities = getattr(message, "entities", ()) or ()
    if (
        user
        and message
        and message.text
        and entities
        and entities[0].type == MessageEntity.BOT_COMMAND
    ):
        command = message.text.split()[0].split("@")[0].lower()
        if command not in {"/feedback", "/cancel"}:
            context.user_data.pop("feedback_form", None)
    if (
        user
        and update.callback_query
        and not (update.callback_query.data or "").startswith("feedback|")
    ):
        context.user_data.pop("feedback_form", None)
    if not user or user.id in ADMIN_IDS:
        return
    text = None
    try:
        await assert_user_allowed(user.id)
    except UserBlockedError:
        text = USER_BLOCKED_MESSAGE
        context.user_data.pop("feedback_form", None)
        context.user_data.pop("awaiting_csi_feedback_id", None)
    except Exception:
        logger.exception("Не удалось проверить доступ пользователя %s", user.id)
        text = USER_ACCESS_UNAVAILABLE
    if text is None:
        return
    # При серии запросов отказ не превращается в поток ответов бота.
    now = asyncio.get_running_loop().time()
    previous = context.user_data.get("access_denied_at", float("-inf"))
    context.user_data["access_denied_at"] = now
    try:
        if update.callback_query:
            await update.callback_query.answer(text=text, show_alert=True)
        elif update.effective_message and now - previous >= 30:
            await update.effective_message.reply_text(text)
    except Exception:
        logger.warning(
            "Не удалось доставить отказ в доступе пользователю %s",
            user.id,
            exc_info=True,
        )
    raise ApplicationHandlerStop
