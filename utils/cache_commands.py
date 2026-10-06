"""
Команды для управления и мониторинга кэша Telegram file_id.
"""

from telegram import Update
from telegram.ext import ContextTypes

from config import ADMIN_IDS
from utils.video_cache import telegram_cache
from utils.db_worker import run_db
from utils.cache_policy import cleanup_cache, get_cache_policy
from messages import CACHE_CLEANUP_DISABLED, CACHE_CLEANUP_RESULT
from utils.logger import setup_logger

logger = setup_logger(__name__)
_ADMIN_ONLY_MESSAGE = "🔒 Эта команда доступна только администраторам"


def _escape_markdown(text: str) -> str:
    """Экранирует спецсимволы для Markdown."""
    escaped = text
    for char in [
        "*",
        "_",
        "[",
        "]",
        "(",
        ")",
        "~",
        "`",
        ">",
        "#",
        "+",
        "-",
        "=",
        "|",
        "{",
        "}",
        ".",
        "!",
    ]:
        escaped = escaped.replace(char, f"\\{char}")
    return escaped


async def _ensure_admin(update: Update) -> bool:
    """Проверяет административный доступ к cache-командам."""
    user_id = update.effective_user.id
    if user_id in ADMIN_IDS:
        return True
    await update.message.reply_text(_ADMIN_ONLY_MESSAGE)
    return False


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Команда /cache_stats - показывает статистику кэша.

    Args:
        update (Update): Объект обновления Telegram.
        context (ContextTypes.DEFAULT_TYPE): Контекст.
    """
    user_id = update.effective_user.id
    logger.info(f"Команда /cache_stats от пользователя {user_id}")

    if not await _ensure_admin(update):
        return

    try:
        stats = await run_db(telegram_cache.get_stats)

        # Форматируем статистику по платформам
        by_platform_str = "\n".join(
            [
                f"  • {platform.capitalize()}: {count}"
                for platform, count in stats["by_platform"].items()
            ]
        )

        if not by_platform_str:
            by_platform_str = "  • Пусто"

        stats_text = f"""
📊 *Статистика кэша видео*

💾 *Всего видео в кэше:* {stats["total_videos"]}

📱 *По платформам:*
{by_platform_str}

🕒 *Временные рамки:*
  • Самая старая запись: {stats["oldest_entry"] or "N/A"}
  • Новейшая запись: {stats["newest_entry"] or "N/A"}

💡 *Что это значит:*
Кеш повторно использует сохраненные идентификаторы файлов без скачивания и загрузки.
"""

        await update.message.reply_text(stats_text, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Ошибка получения статистики кэша: {e}", exc_info=True)
        await update.message.reply_text("❌ Ошибка получения статистики кэша")


async def cleanup_cache_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Команда /cleanup_cache - очищает устаревший кэш (только для админа).

    Args:
        update (Update): Объект обновления Telegram.
        context (ContextTypes.DEFAULT_TYPE): Контекст.
    """
    user_id = update.effective_user.id
    logger.info(f"Команда /cleanup_cache от пользователя {user_id}")

    if not await _ensure_admin(update):
        return

    try:
        policy = await run_db(get_cache_policy)
        if not policy["enabled"]:
            await update.message.reply_text(CACHE_CLEANUP_DISABLED)
            return
        deleted = await run_db(cleanup_cache, telegram_cache)
        await update.message.reply_text(CACHE_CLEANUP_RESULT.format(count=deleted))

        logger.info(f"Очищено {deleted} устаревших записей из кэша")

    except Exception as e:
        logger.error(f"Ошибка очистки кэша: {e}", exc_info=True)
        await update.message.reply_text("❌ Ошибка при очистке кэша")


async def search_cache_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Команда /search_cache [запрос] - ищет видео в кэше по названию.

    Args:
        update (Update): Объект обновления Telegram.
        context (ContextTypes.DEFAULT_TYPE): Контекст.
    """
    user_id = update.effective_user.id

    if not await _ensure_admin(update):
        return

    if not context.args:
        await update.message.reply_text(
            "🔍 Использование: /search_cache <название видео>\n\n"
            "Например: /search_cache Never Gonna Give"
        )
        return

    query = " ".join(context.args)
    logger.info(f"Поиск в кэше от user {user_id}: '{query}'")

    try:
        results = await run_db(telegram_cache.search_by_title, query, limit=10)
        safe_query = _escape_markdown(query)

        if not results:
            await update.message.reply_text(
                f"❌ Ничего не найдено по запросу: *{safe_query}*",
                parse_mode="Markdown",
            )
            return

        # Форматируем результаты
        results_text = f"🔍 *Результаты поиска*\n\nЗапрос: *{safe_query}*\n\n"

        for i, video in enumerate(results, 1):
            platform_emoji = {"youtube": "📺", "tiktok": "🎵", "instagram": "📸"}.get(
                video.platform, "📹"
            )
            safe_title = _escape_markdown(video.title or "Без названия")

            results_text += f"{i}. {platform_emoji} {safe_title}\n"
            results_text += f"   Платформа: {video.platform.capitalize()}\n"
            results_text += f"   Добавлено: {video.cached_at.strftime('%d.%m.%Y')}\n\n"

        results_text += f"💡 Найдено: {len(results)} видео"

        await update.message.reply_text(results_text, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Ошибка поиска в кэше: {e}", exc_info=True)
        await update.message.reply_text("❌ Ошибка при поиске в кэше")
