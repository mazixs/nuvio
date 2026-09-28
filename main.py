#!/usr/bin/env python3
"""
Telegram бот для скачивания видео с YouTube, TikTok и Instagram.
"""

import asyncio
import signal
from contextlib import suppress
from pathlib import Path

import telegram
from dotenv import load_dotenv
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# Load file-based environment variables from canonical to legacy paths.
_BASE_DIR = Path(__file__).parent
for _dotenv_path in (
    _BASE_DIR / ".secrets" / ".env",
    _BASE_DIR / ".env.local",
    _BASE_DIR / ".env",
):
    if _dotenv_path.exists():
        load_dotenv(dotenv_path=_dotenv_path, override=False)

from config import (  # noqa: E402
    CANARY_ENABLED,
    CANARY_INTERVAL_HOURS,
    LOG_LEVEL,
    TELEGRAM_BOT_API_BASE_URL,
    TELEGRAM_BOT_API_FILE_URL,
    TELEGRAM_LOCAL_MODE,
    TELEGRAM_TOKEN,
    validate_config,
)
from utils.canary import youtube_canary_job  # noqa: E402
from utils.logger import setup_logger  # noqa: E402
from utils.temp_file_manager import cleanup_stale_temp_files  # noqa: E402
from utils.cache_commands import (
    stats_command,
    cleanup_cache_command,
    search_cache_command,
)  # noqa: E402
from utils.video_cache import telegram_cache  # noqa: E402
from utils.cookie_manager import (
    admin_command,
    handle_admin_callback,
    handle_document_upload,
)  # noqa: E402
from utils.ytdlp_runtime import get_installed_yt_dlp_version  # noqa: E402
from utils.runtime_status import runtime_components  # noqa: E402
from utils.cookie_workfile import cleanup_old_workfiles  # noqa: E402
from utils.analytics_db import (  # noqa: E402
    close_connection,
    get_csi_interval_days,
    get_users_for_csi,
    prune_old_event_urls,
)

# Настройка логирования
logger = setup_logger(__name__, level=LOG_LEVEL)

# Сколько апдейтов бот обрабатывает одновременно.
#
# По умолчанию PTB обрабатывает апдейты строго по одному: фетчер ждёт завершения
# текущего обработчика. Из-за этого нажатие «Отменить» лежало в очереди всё
# скачивание и обрабатывалось, когда файл уже отправлен, а вторая ссылка не
# читалась вовсе.
#
# Значение заведомо выше DOWNLOAD_WORKERS: скачивания занимают слоты надолго, и
# на навигацию по меню с отменой должно оставаться место, иначе ограничение
# вернёт ту же поломку под нагрузкой.
UPDATE_CONCURRENCY = 32

# Задержка первой канареечной проверки после старта.
CANARY_FIRST_RUN_DELAY = 300


def _classify_polling_error(exc: telegram.error.TelegramError) -> tuple[str, str]:
    """Классифицирует типичные сбои polling по шаблону ошибки."""
    message = str(exc)
    msg_lower = message.lower()

    if isinstance(exc, telegram.error.Conflict):
        return (
            "POLLING_CONFLICT",
            "Параллельный polling другим экземпляром или сервером с тем же токеном.",
        )

    if isinstance(exc, telegram.error.RetryAfter):
        return ("RATE_LIMIT", "Bot API ограничил частоту запросов polling.")

    if isinstance(exc, telegram.error.Forbidden):
        return ("ACCESS", "Bot API отказал в доступе при long polling.")

    if isinstance(exc, telegram.error.BadRequest):
        return ("API", "Bot API отклонил запрос long polling.")

    if isinstance(exc, telegram.error.TimedOut):
        return ("TIMEOUT", "Превышен таймаут ожидания ответа Bot API.")

    if (
        "server disconnected without sending a response" in msg_lower
        or "remoteprotocolerror" in msg_lower
    ):
        return (
            "REMOTE_DISCONNECT",
            "Bot API закрыл long polling без ответа. Частый сценарий: другой сервер перехватил polling или произошёл обрыв на переключении маршрута.",
        )

    if "connection refused" in msg_lower or "connecterror" in msg_lower:
        return (
            "CONNECT_REFUSED",
            "Удалённая сторона отказала в подключении или маршрут до Bot API недоступен.",
        )

    if "timed out" in msg_lower:
        return (
            "TIMEOUT",
            "Превышен таймаут ожидания ответа Bot API.",
        )

    if isinstance(exc, telegram.error.NetworkError):
        return (
            "NETWORK",
            "Сетевой сбой при long polling Bot API.",
        )

    return (
        "UNEXPECT",
        "Неожиданная ошибка при long polling Bot API.",
    )


def _polling_error_callback(exc: telegram.error.TelegramError) -> None:
    """Пишет в лог интерпретацию типовых ошибок polling-цикла."""
    category, summary = _classify_polling_error(exc)
    logger.warning(
        "Ошибка polling [%s]: %s | исходное сообщение: %s",
        category,
        summary,
        exc,
    )


async def scheduled_cache_cleanup(context: ContextTypes.DEFAULT_TYPE):
    """Периодическая очистка кеша (запускается раз в сутки)."""
    try:
        deleted = await asyncio.to_thread(telegram_cache.cleanup_expired, ttl_days=90)
        if deleted > 0:
            logger.info(f"🧹 Автоматическая очистка кэша: удалено {deleted} записей")
        removed, failed = await asyncio.to_thread(cleanup_old_workfiles)
        logger.info("Очистка старых копий cookies: удалено %s, ошибок %s", removed, failed)
        from utils.telegram_utils import active_download_sessions

        removed, failed = await asyncio.to_thread(
            cleanup_stale_temp_files, active_sessions=active_download_sessions()
        )
        logger.info("Очистка брошенных медиа: удалено %s, ошибок %s", removed, failed)
        removed_urls, backup = await asyncio.to_thread(prune_old_event_urls)
        if removed_urls:
            logger.info(
                "Старые URL аналитики удалены: %s; проверенная копия: %s",
                removed_urls,
                backup,
            )
    except Exception as e:
        logger.error(f"Ошибка при автоматической очистке кэша: {e}")


async def scheduled_cache_vacuum(context: ContextTypes.DEFAULT_TYPE):
    """Еженедельная оптимизация SQLite кэша."""
    try:
        db_path = telegram_cache.db_path
        before = db_path.stat().st_size if db_path.exists() else 0
        await asyncio.to_thread(telegram_cache.vacuum)
        after = db_path.stat().st_size if db_path.exists() else 0
        logger.info(
            "🧽 VACUUM кэша завершён: размер %.2f МБ → %.2f МБ",
            before / (1024 * 1024),
            after / (1024 * 1024),
        )
    except Exception as e:
        logger.error(f"Ошибка при оптимизации кэша: {e}")


async def scheduled_csi_dispatch(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневная рассылка CSI-опросов активным пользователям.

    Сам job'а ходит раз в сутки, а частоту опроса для одного пользователя
    задаёт оператор в WebUI. Настройка читается на каждом запуске, поэтому
    перезапуск бота после её смены не нужен.
    """
    try:
        from utils.telegram_utils import send_csi_request

        interval_days = await asyncio.to_thread(get_csi_interval_days)
        user_ids = await asyncio.to_thread(
            get_users_for_csi, days_since_last=interval_days, min_active_days=1
        )
        for user_id in user_ids:
            try:
                await send_csi_request(user_id, context)
            except Exception as e:
                logger.error(f"Ошибка отправки CSI пользователю {user_id}: {e}")
        if user_ids:
            logger.info(
                f"📊 Разослано {len(user_ids)} CSI-опросов "
                f"(интервал {interval_days} дн.)"
            )
    except Exception as e:
        logger.error(f"Ошибка при рассылке CSI: {e}")


def _configure_application_builder(
    builder: ApplicationBuilder,
) -> ApplicationBuilder:
    """Настраивает клиент облачного или локального Telegram Bot API."""
    builder = (
        builder.token(TELEGRAM_TOKEN)
        .concurrent_updates(UPDATE_CONCURRENCY)
        .connect_timeout(10.0)
        .read_timeout(120.0)
        .write_timeout(120.0)
        .media_write_timeout(1800.0)
        .get_updates_connect_timeout(10.0)
        .get_updates_read_timeout(120.0)
        .get_updates_write_timeout(30.0)
        .get_updates_pool_timeout(5.0)
        .http_version("1.1")
        .get_updates_http_version("1.1")
    )
    if TELEGRAM_LOCAL_MODE:
        builder = (
            builder.base_url(TELEGRAM_BOT_API_BASE_URL)
            .base_file_url(TELEGRAM_BOT_API_FILE_URL)
            .local_mode(True)
        )
    return builder


def _build_application() -> Application:
    """Создаёт и конфигурирует экземпляр Application."""
    from utils.telegram_utils import (
        button_callback,
        download_command,
        help_command,
        process_url,
        start_command,
        set_bot_instance,
        _notify_admins_crash,
    )

    application = _configure_application_builder(Application.builder()).build()

    set_bot_instance(application.bot)

    async def _global_error_handler(
        update: object, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Глобальный обработчик необработанных исключений — шлёт краш-репорт админам."""
        logger.error("Необработанное исключение:", exc_info=context.error)
        if context.error:
            from utils.telegram_utils import _make_error_code_for_exception

            error_code = _make_error_code_for_exception("bot", context.error)
            await _notify_admins_crash(
                error_code=error_code,
                platform="bot",
                stage="global_error_handler",
                url=None,
                exc=context.error,
            )

    application.add_error_handler(_global_error_handler)

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("download", download_command))
    application.add_handler(CommandHandler("admin", admin_command))

    application.add_handler(CommandHandler("cache_stats", stats_command))
    application.add_handler(CommandHandler("cleanup_cache", cleanup_cache_command))
    application.add_handler(CommandHandler("search_cache", search_cache_command))

    application.add_handler(
        MessageHandler(filters.Document.ALL, handle_document_upload)
    )
    application.add_handler(
        CallbackQueryHandler(handle_admin_callback, pattern=r"^admin\|")
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, process_url)
    )
    application.add_handler(CallbackQueryHandler(button_callback))

    if application.job_queue:
        application.job_queue.run_repeating(
            scheduled_cache_cleanup, interval=86400, first=60
        )
        application.job_queue.run_repeating(
            scheduled_cache_vacuum, interval=604800, first=600
        )
        application.job_queue.run_repeating(
            scheduled_csi_dispatch, interval=86400, first=3600
        )
        logger.info(
            "🕒 Планировщик задач инициализирован (автоочистка кэша + CSI активны)"
        )
        if CANARY_ENABLED:
            # Первая проверка через 5 минут после старта: сигнал нужен вскоре
            # после перезапуска, но не в момент, когда бот ещё поднимается.
            # Дальше — раз в CANARY_INTERVAL_HOURS часов и только по YouTube:
            # успешных скачиваний за всю историю лога 58, и ежечасная канарейка
            # стала бы и главным потребителем, и главным риском антибот-лимитов
            # домашнего адреса.
            application.job_queue.run_repeating(
                youtube_canary_job,
                interval=CANARY_INTERVAL_HOURS * 3600,
                first=CANARY_FIRST_RUN_DELAY,
                name="youtube_canary",
            )
            logger.info(
                "🐤 Канарейка YouTube включена: проверка раз в %s ч",
                CANARY_INTERVAL_HOURS,
            )

    return application


async def _shutdown_application(application: Application) -> None:
    """Аккуратно останавливает Application и связанные ресурсы."""
    logger.info("⏹️ Остановка бота...")
    if application.updater:
        with suppress(RuntimeError):
            await application.updater.stop()
    with suppress(RuntimeError):
        await application.stop()
    with suppress(RuntimeError):
        await application.shutdown()
    close_connection()


def _prepare_runtime_storage() -> None:
    """Удаляет только давно брошенные медиа, сохраняя файлы живого процесса."""
    removed, failed = cleanup_stale_temp_files()
    logger.info("Остаточные медиа: удалено %s, ошибок %s", removed, failed)


async def run_bot() -> None:
    """Основной цикл с graceful shutdown (SIGINT/SIGTERM)."""
    _prepare_runtime_storage()
    try:
        validate_config()
        logger.info("Конфигурация валидна")
    except ValueError as exc:
        logger.critical(f"Ошибка конфигурации: {exc}")
        return

    logger.info("Запуск бота...")
    logger.info(
        "Текущая версия yt-dlp: %s", get_installed_yt_dlp_version() or "unknown"
    )
    logger.info("Локальная диагностика загрузчика: %s", runtime_components())
    application = _build_application()
    try:
        await application.initialize()
        await application.start()
        if not application.updater:
            raise RuntimeError("Updater не инициализирован")
        # Явно запрашиваем все типы апдейтов, чтобы колбэки кнопок гарантированно приходили
        await application.updater.start_polling(
            allowed_updates=telegram.Update.ALL_TYPES,
            bootstrap_retries=3,
            error_callback=_polling_error_callback,
        )

        cache_stats = telegram_cache.get_stats()
        logger.info(f"💾 В кэше {cache_stats['total_videos']} видео")
        logger.info("✅ Бот запущен и готов к работе!")
        logger.info("⚡ Система быстрой доставки активна")

        stop_event = asyncio.Event()

        def _request_stop(sig: signal.Signals) -> None:
            logger.info("Получен сигнал %s, начинаем остановку...", sig.name)
            stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, lambda s=sig: _request_stop(s))

        await stop_event.wait()
    finally:
        await _shutdown_application(application)
        logger.info("Фоновые задачи завершат очистку своих временных файлов")


def main() -> None:
    """Точка входа с asyncio.run и обработкой ошибок верхнего уровня."""
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем (KeyboardInterrupt)")
    except (ConnectionError, OSError) as exc:
        logger.error(f"Ошибка сети или системы: {exc}")
    except ImportError as exc:
        logger.error(f"Ошибка импорта модулей: {exc}")
    except Exception as exc:  # noqa: BLE001
        exc.add_note("main.py: глобальный обработчик ошибок")
        logger.error(f"Неожиданная ошибка: {exc}", exc_info=True)
    finally:
        logger.info("Завершение процесса бота")


if __name__ == "__main__":
    main()
