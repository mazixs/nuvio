"""Admin-only cookie management for Telegram uploads."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from messages import BROADCAST_RESTRICTED
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import ContextTypes

from config import ADMIN_IDS, SECRETS_DIR
from utils.analytics_db import get_all_user_ids, is_user_blocked, track_event
from utils.db_worker import run_db
from utils.cookie_health import (
    CookieHealthResult,
    check_all_cookie_health,
    check_cookie_health,
    inspect_cookie_file,
)
from utils.cookie_workfile import working_cookie_file
from utils.logger import setup_logger
from utils.runtime_status import format_runtime_status
from utils.telegram_callbacks import answer_callback

logger = setup_logger(__name__)


async def _safe_edit(query, text, **kwargs):
    """edit_message_text с подавлением ошибки 'Message is not modified'."""
    try:
        await query.edit_message_text(text, **kwargs)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


ADMIN_UPLOAD_TARGET_KEY = "admin_expected_cookie_file"
ADMIN_BROADCAST_MODE_KEY = "admin_broadcast_mode"
MAX_COOKIE_FILE_SIZE = 1 * 1024 * 1024  # 1 MiB
BROADCAST_MAX_LENGTH = 4096
BROADCAST_SEND_DELAY_SECONDS = 0.04
ALLOWED_MIME_TYPES = {
    "text/plain",
    "application/octet-stream",
    "application/x-netscape-cookie",
}
COOKIE_TARGETS = {
    "youtube": "www.youtube.com_cookies.txt",
    "instagram": "www.instagram.com_cookies.txt",
    "tiktok": "www.tiktok.com_cookies.txt",
}
COOKIE_PLATFORMS = {file_name: platform for platform, file_name in COOKIE_TARGETS.items()}
COOKIE_LABELS = {
    "youtube": "YouTube",
    "instagram": "Instagram",
    "tiktok": "TikTok",
}
COOKIE_SUMMARY_TRANSLATIONS = {
    "file not found": "файл cookies не найден",
    "no cookie records found": "записи cookies не найдены в файле",
    "cookie file cannot be read": "файл cookies не удалось прочитать",
    "Cookie file is not valid UTF-8": "файл cookies не в кодировке UTF-8",
    "Cookie file has no Netscape header": (
        "нет заголовка Netscape (первая строка должна быть «# Netscape HTTP Cookie File»)"
    ),
    "required auth cookies are missing": "отсутствуют cookies авторизации",
    "all auth cookies are expired": "срок действия всех cookies авторизации истёк",
    "session cookies are expired": "срок действия сессионных cookies истёк",
    "auth cookies are active": "cookies авторизации активны",
    "auth cookies are active and probe succeeded": "cookies активны, тест авторизации пройден",
    "session cookies are present and the test video was extracted": (
        "сессионные cookies на месте, тестовое видео извлечено"
    ),
    "auth cookie set is incomplete, re-upload is needed": (
        "часть cookies авторизации потеряна, загрузите файл заново"
    ),
    "auth cookies exist but authenticated probe failed": "cookies есть, но тест авторизации не прошёл",
    "platform temporarily rate-limited the validation probe": "платформа временно ограничила запросы проверки",
    "auth cookies are active but live probe could not complete": "cookies активны, но сетевой тест не завершился",
}
# Ядро дописывает к этим текстам « at line N», поэтому в словарь выше они не попадают.
COOKIE_LINE_SUMMARY_TRANSLATIONS = {
    "Invalid Netscape cookie row": "строка {line} не похожа на запись cookies в формате Netscape",
    "Invalid cookie expiration value": "неверный срок действия cookie в строке {line}",
}
COOKIE_ALERT_STATUSES = frozenset({"expired", "stale", "degraded", "invalid_format"})
COOKIE_EXPIRY_ALERT_SECONDS = 14 * 24 * 60 * 60
NON_ADMIN_DOCUMENT_MESSAGE = (
    "🔒 Бот не принимает файлы от пользователей. "
    "Поддерживаются только ссылки на видео и админская загрузка cookies."
)
ADMIN_ONLY_MESSAGE = "🔒 Эта функция доступна только администраторам."
ADMIN_UPLOAD_REQUIRED_MESSAGE = (
    "Сначала откройте /admin и выберите, cookies какой платформы хотите обновить."
)
BROADCAST_CANCELLED_MESSAGE = "Рассылка отменена."


def is_admin(user_id: int | None) -> bool:
    """Checks whether the user is an administrator."""
    return user_id is not None and user_id in ADMIN_IDS


def build_admin_entry_markup() -> InlineKeyboardMarkup:
    """Small entry point shown to admins on /start."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Админ-панель", callback_data="admin|cookies|panel")]]
    )


def _build_admin_panel_markup(
    expected_file_name: str | None = None,
    *,
    broadcast_mode: bool = False,
) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton(
                "YouTube", callback_data="admin|cookies|upload|youtube"
            ),
            InlineKeyboardButton(
                "Instagram", callback_data="admin|cookies|upload|instagram"
            ),
        ],
        [
            InlineKeyboardButton("TikTok", callback_data="admin|cookies|upload|tiktok"),
            InlineKeyboardButton("Проверить cookies", callback_data="admin|cookies|check"),
        ],
        [InlineKeyboardButton("Сообщить всем", callback_data="admin|broadcast|start")],
        [InlineKeyboardButton("Обновить", callback_data="admin|cookies|panel")],
    ]
    if expected_file_name:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "Отменить загрузку", callback_data="admin|cookies|cancel"
                )
            ]
        )
    if broadcast_mode:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "Отменить рассылку", callback_data="admin|broadcast|cancel"
                )
            ]
        )
    return InlineKeyboardMarkup(keyboard)


def _format_cookie_status(file_name: str) -> str:
    file_path = SECRETS_DIR / file_name
    if not file_path.exists():
        return f"отсутствует — {file_name}"

    size_kib = max(1, round(file_path.stat().st_size / 1024))
    return f"настроен — {file_name} ({size_kib} КБ)"


def _build_admin_panel_text(expected_file_name: str | None = None) -> str:
    lines = [
        "🔐 Админ-панель",
        "",
        "Статус файлов cookies:",
        f"- YouTube: {_format_cookie_status(COOKIE_TARGETS['youtube'])}",
        f"- Instagram: {_format_cookie_status(COOKIE_TARGETS['instagram'])}",
        f"- TikTok: {_format_cookie_status(COOKIE_TARGETS['tiktok'])}",
        "",
        format_runtime_status(),
    ]
    if expected_file_name:
        lines.extend(
            [
                "",
                f"Режим загрузки включен для: {expected_file_name}",
                "Отправьте любой .txt файл Netscape cookies как Telegram-документ.",
                "Бот автоматически сохранит его под нужным именем.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "Выберите платформу ниже, чтобы включить режим загрузки cookies.",
            ]
        )
    return "\n".join(lines)


def _build_upload_instruction(file_name: str) -> str:
    return "\n".join(
        [
            "📥 Режим загрузки включен.",
            "",
            f"Ожидаемый файл: {file_name}",
            "Отправьте любой .txt файл Netscape cookies как Telegram-документ.",
            f"Он будет сохранен как {file_name}.",
            "Принимаются только .txt файлы Netscape cookies.",
        ]
    )


def _build_broadcast_instruction() -> str:
    return "\n".join(
        [
            "Режим рассылки включён.",
            "",
            "Отправьте следующим сообщением текст объявления.",
            "Бот перешлет его пользователям из базы, которым разрешен доступ.",
            "",
            "Чтобы отменить, нажмите «Отменить рассылку» или отправьте /admin.",
        ]
    )


def _format_health_icon(status: str) -> str:
    if status == "valid":
        return "✅"
    if status in {"expired", "stale", "invalid_format", "degraded"}:
        return "❌"
    if status in {"rate_limited", "probe_failed"}:
        return "⚠️"
    return "ℹ️"


def _translate_cookie_summary(summary: str) -> str:
    prefix, separator, line = summary.rpartition(" at line ")
    if separator and prefix in COOKIE_LINE_SUMMARY_TRANSLATIONS:
        return COOKIE_LINE_SUMMARY_TRANSLATIONS[prefix].format(line=line)
    return COOKIE_SUMMARY_TRANSLATIONS.get(summary, summary)


def _format_session_date(expires_at: int) -> str:
    try:
        return datetime.fromtimestamp(expires_at, UTC).strftime("%d.%m.%Y")
    except (ValueError, OverflowError, OSError):
        # Срок берётся из файла как есть: например, в миллисекундах он не влезет в
        # datetime, и панель из-за этого падать не должна.
        return "неизвестной даты"


def _build_cookie_health_text(
    results: dict[str, CookieHealthResult],
    expected_file_name: str | None = None,
) -> str:
    lines = [
        "🔍 Проверка здоровья cookies",
        "",
    ]

    STATUS_TRANSLATIONS = {
        "valid": "активны",
        "degraded": "набор неполон, нужна перезагрузка",
        "stale": "устарели (неавторизован)",
        "rate_limited": "ограничение запросов",
        "expired": "истёк срок",
        "missing": "отсутствуют",
        "invalid_format": "неверный формат",
        "probe_failed": "ошибка проверки",
    }

    for platform in ("youtube", "instagram", "tiktok"):
        result = results[platform]
        label = COOKIE_LABELS[platform]
        status_ru = STATUS_TRANSLATIONS.get(result.status, result.status)
        summary_ru = _translate_cookie_summary(result.summary)
        lines.append(
            f"{_format_health_icon(result.status)} {label}: {status_ru} — {summary_ru}"
        )
        if result.session_expires_at is not None:
            lines.append(f"Сессия до {_format_session_date(result.session_expires_at)}")

    if expected_file_name:
        lines.extend(
            [
                "",
                f"Режим загрузки всё ещё включен для: {expected_file_name}",
            ]
        )

    lines.extend(
        [
            "",
            "Используйте кнопку «Обновить», чтобы вернуться в панель управления.",
        ]
    )
    return "\n".join(lines)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows the admin cookie panel."""
    user_id = update.effective_user.id if update.effective_user else None
    if not is_admin(user_id):
        await update.message.reply_text(ADMIN_ONLY_MESSAGE)
        return

    context.user_data.pop(ADMIN_UPLOAD_TARGET_KEY, None)
    context.user_data.pop(ADMIN_BROADCAST_MODE_KEY, None)
    await update.message.reply_text(
        _build_admin_panel_text(),
        reply_markup=_build_admin_panel_markup(),
    )


async def handle_admin_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handles admin-only inline actions for cookie management."""
    query = update.callback_query
    user_id = query.from_user.id if query and query.from_user else None

    if not query:
        return

    if not is_admin(user_id):
        await answer_callback(query, ADMIN_ONLY_MESSAGE, show_alert=True)
        return

    data = query.data or ""
    await answer_callback(query)

    if data in {"admin|cookies|panel", "admin|cookies|refresh"}:
        expected_file_name = context.user_data.get(ADMIN_UPLOAD_TARGET_KEY)
        await _safe_edit(
            query,
            _build_admin_panel_text(expected_file_name),
            reply_markup=_build_admin_panel_markup(expected_file_name),
        )
        return

    if data == "admin|cookies|check":
        expected_file_name = context.user_data.get(ADMIN_UPLOAD_TARGET_KEY)
        await _safe_edit(
            query,
            "Проверяю состояние файлов cookies...",
            reply_markup=_build_admin_panel_markup(expected_file_name),
        )
        results = await asyncio.to_thread(check_all_cookie_health, force=True)
        await _safe_edit(
            query,
            _build_cookie_health_text(results, expected_file_name),
            reply_markup=_build_admin_panel_markup(expected_file_name),
        )
        return

    if data == "admin|cookies|cancel":
        context.user_data.pop(ADMIN_UPLOAD_TARGET_KEY, None)
        await _safe_edit(
            query,
            _build_admin_panel_text(),
            reply_markup=_build_admin_panel_markup(),
        )
        return

    if data == "admin|broadcast|start":
        context.user_data.pop(ADMIN_UPLOAD_TARGET_KEY, None)
        context.user_data[ADMIN_BROADCAST_MODE_KEY] = True
        await _safe_edit(
            query,
            _build_broadcast_instruction(),
            reply_markup=_build_admin_panel_markup(broadcast_mode=True),
        )
        return

    if data == "admin|broadcast|cancel":
        context.user_data.pop(ADMIN_BROADCAST_MODE_KEY, None)
        await _safe_edit(
            query,
            BROADCAST_CANCELLED_MESSAGE,
            reply_markup=_build_admin_panel_markup(),
        )
        return

    parts = data.split("|")
    if len(parts) == 4 and parts[:3] == ["admin", "cookies", "upload"]:
        platform = parts[3]
        file_name = COOKIE_TARGETS.get(platform)
        if not file_name:
            await _safe_edit(
                query,
                _build_admin_panel_text(),
                reply_markup=_build_admin_panel_markup(),
            )
            return

        context.user_data[ADMIN_UPLOAD_TARGET_KEY] = file_name
        await _safe_edit(
            query,
            _build_upload_instruction(file_name),
            reply_markup=_build_admin_panel_markup(file_name),
        )
        return

    await _safe_edit(
        query,
        _build_admin_panel_text(),
        reply_markup=_build_admin_panel_markup(),
    )


async def _check_uploaded_cookies(platform: str) -> str:
    """Живая проверка уже сохранённого файла: её итог только сообщается.

    Файл прошёл локальную проверку, а `stale` или сетевой сбой могут быть временными,
    поэтому откатывать загрузку незачем: прежний файл, скорее всего, ещё хуже.
    """
    label = COOKIE_LABELS[platform]
    try:
        result = await asyncio.to_thread(check_cookie_health, platform, force=True)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось проверить загруженные cookies %s", platform)
        return (
            f"⚠️ {label}: файл сохранён, но проверка не завершилась. "
            "Повторите её кнопкой «Проверить cookies»."
        )

    summary_ru = _translate_cookie_summary(result.summary)
    if result.status == "valid":
        lines = [f"✅ {label}: {summary_ru}"]
    else:
        lines = [
            f"⚠️ {label}: {summary_ru}",
            "Файл уже сохранён. Если скачивание не заработает, выгрузите cookies заново.",
        ]
    if result.session_expires_at is not None:
        lines.append(f"Сессия до {_format_session_date(result.session_expires_at)}")
    return "\n".join(lines)


async def handle_document_upload(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handles document uploads with strict admin-only gating."""
    user = update.effective_user
    message = update.message
    document = message.document if message else None
    user_id = user.id if user else None

    if not message or not document:
        return

    if not is_admin(user_id):
        logger.warning("Ignoring document from non-admin user %s", user_id)
        await message.reply_text(NON_ADMIN_DOCUMENT_MESSAGE)
        return

    expected_file_name = context.user_data.get(ADMIN_UPLOAD_TARGET_KEY)
    if not expected_file_name:
        await message.reply_text(ADMIN_UPLOAD_REQUIRED_MESSAGE)
        return

    file_name = document.file_name or ""
    if not file_name.lower().endswith(".txt"):
        await message.reply_text(
            "❌ Неверный тип файла. Отправьте cookies как документ с расширением `.txt`.",
            parse_mode="Markdown",
        )
        return

    if document.file_size and document.file_size > MAX_COOKIE_FILE_SIZE:
        await message.reply_text(
            f"❌ Файл слишком большой ({document.file_size / (1024 * 1024):.1f} MiB). "
            f"Максимум {MAX_COOKIE_FILE_SIZE // (1024 * 1024)} MiB."
        )
        logger.warning(
            "Admin %s attempted to upload an oversized cookie file (%s bytes)",
            user_id,
            document.file_size,
        )
        return

    if document.mime_type and document.mime_type not in ALLOWED_MIME_TYPES:
        await message.reply_text(
            "❌ Неверный тип файла. Принимаются только текстовые cookie-файлы (.txt)."
        )
        logger.warning(
            "Admin %s sent a cookie file with unsupported MIME type %s",
            user_id,
            document.mime_type,
        )
        return

    temporary_path: Path | None = None
    try:
        platform = COOKIE_PLATFORMS[expected_file_name]
        file_path = SECRETS_DIR / expected_file_name
        telegram_file = await document.get_file()
        # Рядом с целевым файлом: `os.replace` атомарен только в пределах одной
        # файловой системы. `mkstemp` сразу даёт режим 600, а PTB пишет в готовый
        # файл и режим не меняет, так что живая сессия не лежит открытой ни на миг.
        descriptor, temporary_name = tempfile.mkstemp(
            dir=SECRETS_DIR, prefix=f".{expected_file_name}.", suffix=".tmp"
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        await telegram_file.download_to_drive(temporary_path)

        inspection = await asyncio.to_thread(
            inspect_cookie_file, platform, temporary_path
        )
        if inspection.status != "valid":
            logger.warning(
                "Admin %s uploaded rejected cookie file for %s: %s (%s)",
                user_id,
                platform,
                inspection.status,
                inspection.summary,
            )
            await message.reply_text(
                "\n".join(
                    [
                        f"❌ Файл не принят: {_translate_cookie_summary(inspection.summary)}.",
                        "Сохранённые ранее cookies не изменены.",
                        "Режим загрузки всё ещё включён: отправьте другой файл "
                        "или нажмите «Отменить загрузку».",
                    ]
                ),
                reply_markup=_build_admin_panel_markup(expected_file_name),
            )
            return

        os.replace(temporary_path, file_path)
        context.user_data.pop(ADMIN_UPLOAD_TARGET_KEY, None)
        working_cookie_file(file_path, fresh=True)
        logger.info(
            "Admin %s updated cookie file %s from uploaded file %s",
            user_id,
            expected_file_name,
            file_name,
        )
        await message.reply_text(
            f"✅ Cookies обновлены и сохранены как {expected_file_name}. Проверяю их...",
        )
        await message.reply_text(await _check_uploaded_cookies(platform))
        await message.reply_text(
            _build_admin_panel_text(),
            reply_markup=_build_admin_panel_markup(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Failed to update cookie file %s: %s",
            expected_file_name,
            exc,
            exc_info=True,
        )
        await message.reply_text(f"❌ Произошла ошибка при сохранении файла: {exc}")
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _build_cookie_alert_text(
    results: dict[str, CookieHealthResult], now: float
) -> str | None:
    """Текст тревоги для админов или None, если им нечего делать.

    Срок сессии проверяется при любом статусе: его даёт файл, а не сеть. Если TikTok
    изменит `passport`, проверка будет молча давать `probe_failed`, продление
    остановится, и приближение срока останется единственным сигналом.
    """
    problems = []
    for platform, result in results.items():
        reasons = []
        if result.status in COOKIE_ALERT_STATUSES:
            reasons.append(_translate_cookie_summary(result.summary))
        expires_at = result.session_expires_at
        if expires_at is not None and expires_at - now < COOKIE_EXPIRY_ALERT_SECONDS:
            reasons.append(f"сессия истекает {_format_session_date(expires_at)}")
        if reasons:
            problems.append(f"- {COOKIE_LABELS[platform]}: {'; '.join(reasons)}")
    if not problems:
        return None
    return "\n".join(
        [
            "⚠️ Cookies пора обновить",
            "",
            *problems,
            "",
            "Загрузите свежие файлы через /admin.",
        ]
    )


async def scheduled_cookie_health_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Раз в сутки проверяет cookies и зовёт админов, если их пора обновить.

    Заодно сохраняет новый срок сессии TikTok: его приносит только ответ живой
    проверки, и без регулярного запуска cookie в указанный день перестанет
    отправляться. TikTok сдвигает срок не в каждом ответе, поэтому неизменная дата
    при `valid` — норма. Состояние не хранится: тревога повторяется, пока проблема
    не решена.
    """
    try:
        results = await asyncio.to_thread(check_all_cookie_health, force=True)
        logger.info(
            "Плановая проверка cookies: %s",
            {platform: result.status for platform, result in results.items()},
        )
        text = _build_cookie_alert_text(results, time.time())
        if text is None:
            return
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(chat_id=admin_id, text=text)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Не удалось отправить тревогу о cookies админу %s: %s", admin_id, exc
                )
    except Exception:  # noqa: BLE001
        logger.exception("Плановая проверка cookies не удалась")


async def handle_admin_text_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Обрабатывает текстовые админские режимы. Возвращает True, если сообщение уже обработано."""
    user = update.effective_user
    message = update.message
    user_id = user.id if user else None

    if not message or not message.text:
        return False
    if not is_admin(user_id):
        return False
    if not context.user_data.get(ADMIN_BROADCAST_MODE_KEY):
        return False

    text = message.text.strip()
    if not text:
        await message.reply_text(
            "❌ Текст рассылки пустой. "
            "Отправьте сообщение ещё раз или нажмите «Отменить рассылку»."
        )
        return True
    if len(text) > BROADCAST_MAX_LENGTH:
        await message.reply_text(
            f"❌ Текст слишком длинный: {len(text)} символов. "
            f"Максимум для Telegram-сообщения — {BROADCAST_MAX_LENGTH}."
        )
        return True

    context.user_data.pop(ADMIN_BROADCAST_MODE_KEY, None)
    user_ids = [target_id for target_id in await run_db(get_all_user_ids) if target_id != user_id]
    if not user_ids:
        await message.reply_text(
            "Некому отправлять рассылку: в базе пока нет пользователей."
        )
        return True

    status_message = await message.reply_text(
        f"Рассылаю объявление {len(user_ids)} пользователям..."
    )
    sent = 0
    failed = 0
    blocked = 0
    restricted = 0

    for target_id in user_ids:
        if await run_db(is_user_blocked, target_id):
            restricted += 1
            continue
        try:
            await context.bot.send_message(chat_id=target_id, text=text)
            sent += 1
            await run_db(track_event, target_id, "admin_broadcast", metadata=f"sent_by={user_id}")
            await asyncio.sleep(BROADCAST_SEND_DELAY_SECONDS)
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after))
            if await run_db(is_user_blocked, target_id):
                restricted += 1
                continue
            try:
                await context.bot.send_message(chat_id=target_id, text=text)
                sent += 1
                await run_db(track_event, target_id, "admin_broadcast", metadata=f"sent_by={user_id}")
            except Forbidden:
                blocked += 1
                failed += 1
                logger.info("Broadcast retry skipped blocked user %s", target_id)
            except TelegramError as retry_exc:
                failed += 1
                logger.warning(
                    "Broadcast retry failed for user %s: %s", target_id, retry_exc
                )
        except Forbidden:
            blocked += 1
            failed += 1
            logger.info("Broadcast skipped blocked user %s", target_id)
        except TelegramError as exc:
            failed += 1
            logger.warning("Broadcast failed for user %s: %s", target_id, exc)

    await status_message.edit_text(
        "\n".join(
            [
                "Рассылка завершена.",
                f"✅ Отправлено: {sent}",
                f"🚫 Бот заблокирован: {blocked}",
                BROADCAST_RESTRICTED.format(count=restricted),
                f"❌ Ошибок: {failed - blocked}",
            ]
        )
    )
    return True
