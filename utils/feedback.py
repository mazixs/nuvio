"""Обратная связь пользователя с привязкой к ошибке и отменой ввода."""

import logging
import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from messages import (
    BTN_FEEDBACK,
    BTN_FEEDBACK_CANCEL,
    FEEDBACK_PROMPT,
    FEEDBACK_ISSUE_PROMPT,
    FEEDBACK_SAVED,
    FEEDBACK_INVALID,
    FEEDBACK_RATE_LIMIT,
    FEEDBACK_SAVE_FAILED,
    FEEDBACK_CANCELLED,
    FEEDBACK_NO_FORM,
    FEEDBACK_EXPIRED,
    FEEDBACK_PRIVATE_ONLY,
    FEEDBACK_REPLY_HINT,
    FEEDBACK_PROMPT_PENDING,
    USER_BLOCKED_MESSAGE,
)
from utils.analytics_db import (
    FeedbackRateLimitError,
    UserBlockedError,
    save_feedback,
    track_user,
)
from utils.db_worker import run_db
from utils.callback_fsm import CallbackEvent
from utils.telegram_callbacks import answer_callback

logger = logging.getLogger(__name__)
FORM_KEY = "feedback_form"
ISSUES_KEY = "feedback_issues"
FORM_TTL_SECONDS = 1800
ISSUE_TTL_SECONDS = 86400


def feedback_markup(
    context, error_code: str | None = None, url: str | None = None, existing=None
):
    """Хранит только код и ссылку в пределах пользователя, без текста исключения."""
    if error_code and "-BLOCKED-" in error_code:
        return existing
    if error_code:
        issues = context.user_data.setdefault(ISSUES_KEY, {})
        issues[error_code] = {"url": url, "created_at": time.time()}
        while len(issues) > 10:
            issues.pop(next(iter(issues)))
    rows = [list(row) for row in existing.inline_keyboard] if existing else []
    rows.append(
        [
            InlineKeyboardButton(
                BTN_FEEDBACK, callback_data=f"feedback|open|{error_code or 'general'}"
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def _private(update: Update) -> bool:
    return update.effective_chat is not None and update.effective_chat.type == "private"


async def _start_form(update: Update, context, error_code: str | None = None) -> None:
    message = update.effective_message
    if not _private(update):
        await message.reply_text(FEEDBACK_PRIVATE_ONLY)
        return
    issue = (
        context.user_data.get(ISSUES_KEY, {}).get(error_code) if error_code else None
    )
    if error_code and (
        not issue or time.time() - issue["created_at"] > ISSUE_TTL_SECONDS
    ):
        await message.reply_text(FEEDBACK_EXPIRED)
        return
    token = uuid.uuid4().hex[:12]
    form = {
        "token": token,
        "created_at": time.time(),
        "error_code": error_code,
        "source_url": issue.get("url") if issue else None,
        "ready": False,
        "require_reply": bool(context.user_data.get(FORM_KEY)),
    }
    context.user_data[FORM_KEY] = form
    from utils.cookie_manager import ADMIN_BROADCAST_MODE_KEY, ADMIN_UPLOAD_TARGET_KEY

    context.user_data.pop(ADMIN_BROADCAST_MODE_KEY, None)
    context.user_data.pop(ADMIN_UPLOAD_TARGET_KEY, None)
    context.user_data.pop("awaiting_csi_feedback_id", None)
    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    BTN_FEEDBACK_CANCEL, callback_data=f"feedback|cancel|{token}"
                )
            ]
        ]
    )
    text = FEEDBACK_PROMPT + (
        FEEDBACK_ISSUE_PROMPT.format(error_code=error_code) if error_code else ""
    )
    try:
        prompt = await message.reply_text(text + (FEEDBACK_REPLY_HINT if form["require_reply"] else ""), reply_markup=markup)
    except BaseException:
        # Сбой старого приглашения не закрывает форму, открытую параллельно.
        if context.user_data.get(FORM_KEY) is form:
            context.user_data.pop(FORM_KEY, None)
        raise
    if context.user_data.get(FORM_KEY) is form:
        prompt_id = getattr(prompt, "message_id", None)
        form["prompt_id"] = prompt_id if isinstance(prompt_id, int) else None
        form["ready"] = True
    else:
        current = context.user_data.get(FORM_KEY)
        if current:
            current["require_reply"] = True
        try:
            await prompt.edit_text(FEEDBACK_EXPIRED, reply_markup=None)
        except Exception:
            logger.warning("Устаревшее приглашение обратной связи не удалось закрыть")


async def feedback_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Открывает форму независимо от CSI-опроса."""
    await _start_form(update, context)


async def cancel_feedback_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    existed = context.user_data.pop(FORM_KEY, None)
    await update.effective_message.reply_text(
        FEEDBACK_CANCELLED if existed else FEEDBACK_NO_FORM
    )


async def feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    event = CallbackEvent.parse(update.callback_query.data or "")
    await answer_callback(update.callback_query)
    if not event or event.scope != "feedback":
        await update.effective_message.reply_text(FEEDBACK_EXPIRED)
        return
    if event.action == "open":
        await _start_form(
            update, context, None if event.value == "general" else event.value
        )
    elif event.action == "cancel":
        form = context.user_data.get(FORM_KEY)
        if not form or form["token"] != event.value:
            await update.effective_message.reply_text(FEEDBACK_EXPIRED)
            return
        await cancel_feedback_command(update, context)


async def _consume_feedback_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Перехватывает только открытый ввод; ссылки внутри обращения допустимы."""
    form = context.user_data.get(FORM_KEY)
    if not form:
        return
    message = update.effective_message
    if time.time() - form["created_at"] > FORM_TTL_SECONDS:
        context.user_data.pop(FORM_KEY, None)
        await message.reply_text(FEEDBACK_EXPIRED)
        raise ApplicationHandlerStop
    if not _private(update):
        await message.reply_text(FEEDBACK_PRIVATE_ONLY)
        raise ApplicationHandlerStop
    replied_id = getattr(getattr(message, "reply_to_message", None), "message_id", None)
    if (isinstance(replied_id, int) and replied_id != form.get("prompt_id")) or (
        form.get("require_reply") and replied_id != form.get("prompt_id")
    ):
        await message.reply_text(FEEDBACK_REPLY_HINT)
        raise ApplicationHandlerStop
    try:
        user = update.effective_user
        await run_db(
            track_user, user.id, username=user.username, first_name=user.first_name
        )
        feedback_id = await run_db(
            save_feedback,
            user.id,
            message.text or "",
            error_code=form["error_code"],
            source_url=form["source_url"],
            message_key=f"{update.effective_chat.id}:{message.message_id}",
        )
    except FeedbackRateLimitError:
        await message.reply_text(FEEDBACK_RATE_LIMIT)
    except UserBlockedError:
        context.user_data.pop(FORM_KEY, None)
        await message.reply_text(USER_BLOCKED_MESSAGE)
    except ValueError:
        await message.reply_text(FEEDBACK_INVALID)
    except Exception:
        logger.exception("Не удалось сохранить обратную связь")
        await message.reply_text(FEEDBACK_SAVE_FAILED)
    else:
        # Новый ввод, открытый параллельной командой, не закрывается старым ответом.
        if context.user_data.get(FORM_KEY) is form:
            context.user_data.pop(FORM_KEY, None)
        await message.reply_text(FEEDBACK_SAVED.format(feedback_id=feedback_id))
    raise ApplicationHandlerStop


async def feedback_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Не дает тексту обращения перейти к загрузчику даже при отказе Telegram."""
    form = context.user_data.get(FORM_KEY)
    if not form:
        return
    if not form.get("ready", True):
        try:
            await update.effective_message.reply_text(FEEDBACK_PROMPT_PENDING)
        except Exception:
            logger.warning("Не удалось сообщить об ожидающем приглашении обратной связи")
        raise ApplicationHandlerStop
    try:
        await _consume_feedback_text(update, context)
    except ApplicationHandlerStop:
        pass
    except Exception:
        logger.exception("Не удалось доставить ответ на обращение")
    raise ApplicationHandlerStop
