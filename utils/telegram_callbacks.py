"""Подтверждение inline-кнопок без потери самого пользовательского действия."""

import logging

from telegram.error import TelegramError

logger = logging.getLogger(__name__)


async def answer_callback(query, *args, **kwargs) -> bool:
    """Сбой короткого подтверждения не отменяет обработку нажатия."""
    try:
        await query.answer(*args, **kwargs)
    except TelegramError:
        logger.debug(
            "Не удалось подтвердить callback, продолжаем обработку", exc_info=True
        )
        return False
    return True
