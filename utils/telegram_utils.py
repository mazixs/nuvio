"""
Модуль для работы с Telegram API.
"""

import asyncio
import contextlib
import functools
import io
import re
import sqlite3
import threading
import time
import traceback
import uuid
from pathlib import Path
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import ContextVar

import telegram
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
)
from telegram.helpers import escape_markdown as _telegram_escape_markdown
from telegram.ext import ContextTypes

from config import (
    ADMIN_IDS,
    BLOCKING_TASK_TIMEOUT,
    DOWNLOAD_WORKERS,
    MAX_FILE_SIZE,
    TELEGRAM_LOCAL_MODE,
    TEMP_DIR,
)
from utils import download_report
from messages import CSI_INVALID_RATING, CSI_RATING_OUT_OF_RANGE, CSI_SURVEY_EXPIRED
from utils.logger import setup_logger
from utils.cancellation import CancelledByUser, is_cancelled, request_cancellation
from utils.subtitles import (
    SUBTITLE_FORMATS,
    available_subtitle_languages,
    parse_subtitle_choice,
)
from utils.tg_video_choice import (
    audio_language_rank,
    list_audio_options,
    list_video_options,
    select_tg_video_format,
)
from utils.analytics_db import (
    init_db as _init_analytics,
    track_user,
    track_event,
    update_last_csi_sent,
    create_csi_poll,
    bind_csi_poll,
    save_csi_vote,
    update_csi_feedback,
)
from utils.youtube_utils import (
    is_valid_youtube_url,
    get_video_info,
    get_available_formats,
    download_video,
    download_audio,
    download_audio_native,
    download_subtitles,
)
from utils.temp_file_manager import create_temp_dir, cleanup_temp_files
from utils.callback_fsm import CallbackEvent, SessionStore
from utils.telegram_callbacks import answer_callback
from utils.file_delivery import media_kind_for_suffix
from utils.social_delivery import (
    DeliveryOutcome,
    chunk_media,
    description_delivery_plan,
    description_status,
    media_album_sizes,
    normalize_description,
)
from utils.media_processor import get_video_geometry
from utils.feedback import feedback_markup, FORM_KEY as FEEDBACK_FORM_KEY
from utils.user_access import active_user_id, assert_user_allowed
from utils.platform_actions import (
    DIRECT_VIDEO_CACHE_KEY,
    cache_key_for_format_selection,
    cache_key_for_main_action,
)
from utils.public_errors import (
    build_public_error_message,
    classify_internal_error_category,
    youtube_error_code,
)
from utils.runtime_status import runtime_components, read_canary_status
from utils.url_delivery import (
    HandoffRefusals,
    PhotoPostHandoff,
    UrlHandoff,
    find_format_geometry,
    find_format_url,
    plan_url_handoff,
)
import yt_dlp
import httpx
from utils.db_worker import run_db
from utils.cache_access import cache_call
from utils.artifact_cache import artifact_from_message, recipe_for
from utils.delivery_journal import journal
from messages import (
    WELCOME_MESSAGE,
    HELP_MESSAGE,
    PROCESSING_MESSAGE,
    DOWNLOADING_MESSAGE,
    DOWNLOADING_AUDIO_MESSAGE,
    DOWNLOADING_SUBTITLES_MESSAGE,
    PHOTO_POST_AUDIO_UNAVAILABLE,
    INVALID_URL_MESSAGE,
    VK_NO_SUITABLE_FORMAT_MESSAGE,
    VK_LIVE_ACTIVE_MESSAGE,
    ERROR_MESSAGE,
    NO_URL_AFTER_COMMAND,
    SESSION_EXPIRED,
    FILE_PREPARING,
    FILE_SENT,
    DOWNLOAD_FORMAT_PROMPT,
    NO_SUBTITLES_AVAILABLE,
    NO_TG_VIDEO,
    NO_FILESIZE,
    BTN_AUDIO_M4A,
    BTN_TG_VIDEO,
    BTN_MORE,
    BTN_BACK,
    BTN_DOWNLOAD_VIDEO,
    BTN_DOWNLOAD_POST,
    BTN_DOWNLOAD_WITHOUT_DESCRIPTION,
    BTN_DOWNLOAD_WITH_DESCRIPTION,
    DESCRIPTION_EMPTY,
    DESCRIPTION_UNAVAILABLE,
    DOWNLOADING_PHOTOS_MESSAGE,
    ERROR_INSTAGRAM_NO_PHOTOS,
    ERROR_TIKTOK_NO_PHOTOS,
    MIXED_INSTAGRAM_POST,
    INSTAGRAM_CAROUSEL_INCOMPLETE,
    PHOTO_POST_PARTIAL,
    DELIVERY_OUTCOME_UNKNOWN,
    DESCRIPTION_SEND_FAILED,
    PARTIAL_CANCELLED,
    CANCEL_IN_FLIGHT_MESSAGE,
    BTN_AUDIO_ONLY,
    BTN_SECTION_VIDEO,
    BTN_SECTION_AUDIO,
    BTN_SECTION_SUBTITLES,
    BTN_AUDIO_TRANSCODE,
    CHOOSE_SECTION_MESSAGE,
    CHOOSE_RESOLUTION_MESSAGE,
    CHOOSE_AUDIO_MESSAGE,
    NO_VIDEO_OPTIONS_MESSAGE,
    NO_AUDIO_OPTIONS_MESSAGE,
    BTN_CANCEL,
    CANCELLED_MESSAGE,
    CHOOSE_SUBTITLE_LANGUAGE_MESSAGE,
    CHOOSE_SUBTITLE_FORMAT_MESSAGE,
    NO_SUBTITLE_LANGUAGES_MESSAGE,
    ERROR_FALLBACK,
    SUBTITLE_CAPTION,
    SPAM_WARNING,
    USER_FILE_ERROR_WITH_CODE,
    CSI_REQUEST_MESSAGE,
    CSI_THANKS_MESSAGE,
    CSI_FEEDBACK_REQUEST,
    CSI_FEEDBACK_THANKS,
)
from utils.tiktok_instagram_utils import (
    is_valid_tiktok_url,
    is_valid_instagram_url,
    is_instagram_story_url,
    get_tiktok_info,
    get_instagram_info,
    is_instagram_audio_url,
    handle_instagram_audio_url,
    PhotoPostAudioMissingError,
)
from utils.rutube_vk_utils import (
    VkLiveActiveError,
    is_valid_rutube_url,
    is_valid_vk_url,
    get_rutube_info,
    get_vk_info,
    get_available_formats_rutube,
    get_available_formats_vk,
    download_rutube_video,
    download_vk_video,
    download_rutube_audio,
    download_vk_audio,
)
from utils.ytdlp_common import FileSizeLimitError
from utils.video_cache import telegram_cache, CachedVideo
from utils.cookie_health import check_cookie_health
from utils.ytdlp_runtime import get_installed_yt_dlp_version
from datetime import datetime, timedelta, timezone

logger = setup_logger(__name__)

# Инициализация аналитической БД
_init_analytics()

# Ссылка на экземпляр бота для отправки краш-репортов админам
_bot_instance: telegram.Bot | None = None


def set_bot_instance(bot: telegram.Bot) -> None:
    """Устанавливает ссылку на бота для отправки краш-репортов."""
    global _bot_instance
    _bot_instance = bot


async def send_csi_request(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет пользователю inline-клавиатуру с оценками 0–10 для CSI."""
    await assert_user_allowed(user_id)
    poll_token = await run_db(create_csi_poll, user_id)
    keyboard = []
    row = []
    for i in range(11):
        row.append(InlineKeyboardButton(str(i), callback_data=f"csi|{poll_token}|{i}"))
        if len(row) == 6:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    reply_markup = InlineKeyboardMarkup(keyboard)
    prompt = await context.bot.send_message(
        chat_id=user_id, text=CSI_REQUEST_MESSAGE, reply_markup=reply_markup
    )
    if isinstance(prompt.message_id, int):
        await run_db(bind_csi_poll, poll_token, prompt.message_id)
    await run_db(update_last_csi_sent, user_id)


def _format_exception_traceback(exc: BaseException) -> str:
    """Формирует traceback из объекта исключения даже вне активного except-блока."""
    if exc.__traceback__:
        return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return (
        "Traceback unavailable: exception was logged outside the original except block."
    )


def _md_cell(value: object) -> str:
    """Готовит значение к вставке в ячейку markdown-таблицы.

    Вертикальная черта закрывает столбец, а перевод строки — всю строку таблицы,
    поэтому и то и другое обезвреживается: в краш-репорт попадают тексты
    исключений, где встречается любой символ.
    """
    text = str(value).replace("|", "\\|")
    return " ".join(text.split()) or "N/A"


def _md_code_block(text: str, language: str = "text") -> str:
    """Оборачивает текст в ограждённый блок кода, который он не сможет разорвать.

    Забор берётся длиннее самой длинной череды обратных кавычек внутри: в
    traceback попадают строки исходников, а в них кавычки бывают.
    """
    longest_run = 0
    current_run = 0
    for char in text:
        current_run = current_run + 1 if char == "`" else 0
        longest_run = max(longest_run, current_run)
    fence = "`" * max(3, longest_run + 1)
    return f"{fence}{language}\n{text.rstrip()}\n{fence}"


def _md_ytdlp_tail_section(tail: list[str]) -> list[str]:
    """Собирает секцию репорта с последними строками вывода yt-dlp.

    Объяснение отказа живёт в предупреждениях yt-dlp («cookies are no longer
    valid», «formats require a GVS PO Token», «forcing SABR streaming»), и
    только они говорят, каким клиентом качали и что ответила платформа: в
    info-dict этого нет, а без секции ответ приходилось добывать по SSH. Пустой
    хвост даёт строку прозой, потому что пустой блок кода читается как потеря
    данных, а не как их отсутствие.
    """
    body = (
        _md_code_block("\n".join(tail))
        if tail
        else "Вывода yt-dlp по этой сессии нет: до его запуска дело не дошло."
    )
    return ["## Последние строки yt-dlp", "", body, ""]


async def _notify_admins_crash(
    *,
    error_code: str,
    platform: str,
    stage: str,
    url: str | None,
    exc: Exception,
    session_id: str | None = None,
    cookie_status: str = "not_checked",
    cookie_summary: str = "not_checked",
    output_tail: list[str] | None = None,
) -> None:
    """Отправляет админам из ADMIN_IDS краш-репорт файлом в markdown.

    `output_tail` — снимок вывода yt-dlp, снятый в момент отказа. Без снимка
    хвост читается из регистра здесь, но тогда он может уже опустеть: сессию
    уничтожают сразу после отказа, а репорт собирается фоновой задачей.
    """
    if not _bot_instance or not ADMIN_IDS:
        return

    tail = (
        output_tail
        if output_tail is not None
        else download_report.output_tail(session_id)
    )

    # Ссылка идёт в угловых скобках: так markdown делает её ссылкой, не пытаясь
    # разобрать подчёркивания и звёздочки внутри как разметку. Время — в UTC,
    # как и метки в `logs/bot.log`, чтобы репорт искался в журнале по времени.
    #
    # Версия yt-dlp и канал обновлений отвечают на первый же вопрос при поломке
    # платформы: обновление уехало вперёд или, наоборот, отстало. Версия
    # определяется по метаданным пакета и может не определиться вовсе — тогда в
    # ячейке честное «N/A», а не пустое место, за которым не видно, что версии
    # нет и что её не смогли прочитать.
    installed_version = get_installed_yt_dlp_version()
    runtime = runtime_components()
    canary_status = read_canary_status()
    facts = (
        (
            "Время (UTC)",
            _md_cell(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")),
        ),
        ("Платформа", _md_cell(platform)),
        ("Этап", _md_cell(stage)),
        ("Ссылка", f"<{_md_cell(url)}>" if url else "N/A"),
        ("Сессия", f"`{_md_cell(session_id)}`" if session_id else "N/A"),
        ("Cookies", _md_cell(cookie_status)),
        ("Что с cookies", _md_cell(cookie_summary)),
        ("Версия yt-dlp", _md_cell(installed_version) if installed_version else "N/A"),
        ("Загруженный yt-dlp", _md_cell(runtime["yt_dlp_loaded"])),
        ("EJS", _md_cell(runtime["ejs"])),
        ("Deno", _md_cell(runtime["deno"])),
        ("Образ", _md_cell(runtime["image"])),
        ("Канарейка", _md_cell(canary_status.get("state", "неизвестно"))),
        ("Обновление", "через новый образ"),
    )
    report_text = "\n".join(
        [
            f"# 🔴 Краш-репорт — `{error_code}`",
            "",
            "| Поле | Значение |",
            "| --- | --- |",
            *(f"| {name} | {value} |" for name, value in facts),
            "",
            *_md_ytdlp_tail_section(tail),
            "## Исключение",
            "",
            _md_code_block(f"{type(exc).__name__}: {exc}"),
            "",
            "## Traceback",
            "",
            _md_code_block(_format_exception_traceback(exc), "python"),
            "",
        ]
    )

    for admin_id in ADMIN_IDS:
        try:
            await _bot_instance.send_document(
                chat_id=admin_id,
                document=io.BytesIO(report_text.encode("utf-8")),
                filename=f"crash_{error_code}.md",
                caption=f"🔴 {error_code} | {platform} | {stage}",
            )
        except Exception:  # noqa: BLE001
            logger.debug("Не удалось отправить краш-репорт админу %s", admin_id)


# Глобальный executor для тяжёлых задач
executor = ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS)
_worker_lock = threading.Lock()
_active_workers: dict[str, set[Future]] = {}
_active_deliveries: dict[str, int] = {}
_pending_cleanup: dict[str, bool] = {}


def active_download_sessions() -> set[str]:
    """Снимок сессий с еще работающими задачами."""
    with _worker_lock:
        return {
            session
            for session in set(_active_workers) | set(_active_deliveries)
            if _active_workers.get(session) or _active_deliveries.get(session)
        }


def _cleanup_session_when_idle(session_id: str, *, forget_report: bool = False) -> None:
    """Удаляет медиа только после фактического выхода фоновой задачи."""
    with _worker_lock:
        if _active_workers.get(session_id) or _active_deliveries.get(session_id):
            _pending_cleanup[session_id] = (
                _pending_cleanup.get(session_id, False) or forget_report
            )
            return
    cleanup_temp_files(session_id)
    if forget_report:
        download_report.forget(session_id)


def _worker_finished(session_id: str, future: Future) -> None:
    with _worker_lock:
        workers = _active_workers.get(session_id)
        if workers:
            workers.discard(future)
            if workers:
                return
            _active_workers.pop(session_id, None)
        if _active_deliveries.get(session_id):
            return
        pending = _pending_cleanup.pop(session_id, None)
    if pending is not None:
        cleanup_temp_files(session_id)
        if pending:
            download_report.forget(session_id)


def _delivery_started(session_id: str) -> None:
    with _worker_lock:
        _active_deliveries[session_id] = _active_deliveries.get(session_id, 0) + 1


def _delivery_finished(session_id: str) -> None:
    with _worker_lock:
        count = _active_deliveries.get(session_id, 0) - 1
        if count > 0:
            _active_deliveries[session_id] = count
            return
        _active_deliveries.pop(session_id, None)
        if _active_workers.get(session_id):
            return
        pending = _pending_cleanup.pop(session_id, None)
    if pending is not None:
        cleanup_temp_files(session_id)
        if pending:
            download_report.forget(session_id)
_SPAM_WINDOW_SECONDS = 5
_SPAM_REQUEST_LIMIT = 4
_SPAM_TIMEOUT_SECONDS = 10
_MAX_ACTIVE_SESSIONS = 5
_ANTISPAM_STATE_KEYS = ("recent_requests", "spam_blocked_until")
_SESSION_STORE_KEY = "sessions"
_DIRECT_VIDEO_CACHE_KEY = DIRECT_VIDEO_CACHE_KEY
_active_delivery_session: ContextVar[dict | None] = ContextVar(
    "telegram_delivery_session", default=None
)


async def _track_tg_user(update: Update) -> None:
    """Регистрирует / обновляет пользователя в аналитике."""
    user = update.effective_user
    if not user:
        return
    try:
        await run_db(
            track_user,
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            language_code=user.language_code,
        )
    except Exception:  # noqa: BLE001
        logger.debug("analytics: не удалось записать пользователя %s", user.id)


async def _record_delivery(user_id: int, session_data: dict) -> None:
    """Отмечает подтвержденную отправку после ответа Telegram."""
    if session_data.get("_cached_manifest_id"):
        await cache_call(telegram_cache.artifacts.touch, session_data["_cached_manifest_id"])
    try:
        await run_db(
            track_event,
            user_id,
            "delivery",
            platform=session_data.get("platform"),
            url=session_data.get("url"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось записать успешную доставку: %s", exc)


async def run_blocking(
    func,
    *args,
    description: str = "blocking task",
    session_id: str | None = None,
    timeout: float | None = None,
):
    """Запускает sync-функцию в executor с таймаутом.

    `wait_for` отменяет только ожидание — поток в пуле продолжает работать. Для
    скачивания это означает занятый воркер и трафик ради файла, который уже
    никому не отдадут: замерено, что загрузка завершилась через 4 минуты после
    своего таймаута. Поэтому по таймауту сессия помечается отменённой, и
    progress hook обрывает yt-dlp изнутри.

    Args:
        session_id: Сессия задачи. Без неё остановить работу нечем.
        timeout: Лимит отдельной операции; по умолчанию общий лимит.
    """
    loop = asyncio.get_running_loop()
    effective_timeout = BLOCKING_TASK_TIMEOUT if timeout is None else timeout
    try:
        from utils import work_budget

        future = executor.submit(work_budget.execute, func, args, time.monotonic() + effective_timeout, session_id)
        if session_id:
            with _worker_lock:
                _active_workers.setdefault(session_id, set()).add(future)
            future.add_done_callback(
                lambda done: _worker_finished(session_id, done)
            )
        wrapped = asyncio.wrap_future(future, loop=loop)
        wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return await asyncio.wait_for(
            asyncio.shield(wrapped),
            effective_timeout,
        )
    except asyncio.CancelledError:
        if session_id:
            request_cancellation(session_id)
        raise
    except asyncio.TimeoutError as exc:
        logger.error(
            "%s превысил таймаут %sс", description, effective_timeout, exc_info=True
        )
        if session_id:
            request_cancellation(session_id)
            logger.info(
                "Запрошена отмена сессии %s: задача осталась в пуле после таймаута",
                session_id,
            )
        raise exc


# Простая защита от спама: 4 запросa подряд без паузы -> предупреждение и таймаут 10с
def _check_spam(user_id: int, context: ContextTypes.DEFAULT_TYPE, now: float) -> bool:
    blocked_until = context.user_data.get("spam_blocked_until", 0.0)
    if blocked_until and now < blocked_until:
        return True

    if blocked_until and now >= blocked_until:
        context.user_data.pop("spam_blocked_until", None)

    timestamps: list[float] = context.user_data.get("recent_requests", [])
    timestamps = [t for t in timestamps if now - t < _SPAM_WINDOW_SECONDS]
    timestamps.append(now)
    context.user_data["recent_requests"] = timestamps

    if len(timestamps) >= _SPAM_REQUEST_LIMIT:
        context.user_data["spam_blocked_until"] = now + _SPAM_TIMEOUT_SECONDS
        return True

    return False


def _session_is_disposable(session_id: str | None) -> bool:
    """Сообщает, можно ли вытеснить сессию, не потеряв ничьей работы.

    Признак занятости — файлы в её временном каталоге. Пустой каталог означает
    брошенное меню: формат не выбирали, качать нечего. Непустой означает либо
    идущую загрузку, либо готовый файл, который ещё не отправлен, — и такую
    запись терять нельзя: в ней лежит `session_id`, по которому владелец потом
    удалит файлы.
    """
    if not session_id:
        return True
    with _worker_lock:
        if _active_workers.get(session_id) or _active_deliveries.get(session_id):
            return False
    directory = TEMP_DIR / session_id
    try:
        return not any(directory.iterdir())
    except FileNotFoundError:
        return True
    except OSError as error:
        # Не смогли посмотреть — считаем занятой: вытеснение обратимо ожиданием,
        # а потеря скачанного файла нет.
        logger.warning(
            "Не удалось проверить каталог сессии %s (%s), сессия сохранена",
            session_id,
            error,
        )
        return False


def _get_session_store(context: ContextTypes.DEFAULT_TYPE) -> dict[str, dict]:
    """Возвращает хранилище активных сессий пользователя."""
    return SessionStore(
        context.user_data,
        key=_SESSION_STORE_KEY,
        max_active=_MAX_ACTIVE_SESSIONS,
        is_disposable=_session_is_disposable,
    ).data


def _session_id_of(
    context: ContextTypes.DEFAULT_TYPE, session_token: str | None
) -> str | None:
    """Достаёт `session_id` по токену, не разрушая запись.

    Токен и `session_id` — разные вещи: первым помечена клавиатура, вторым названы
    временные файлы и хвост вывода yt-dlp. Передать токен там, где ждут
    `session_id`, значит молча получить пустую диагностику в краш-репорте.
    """
    if not session_token:
        return None
    session = _get_session_store(context).get(session_token)
    return session.get("session_id") if session else None


def _store_session(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    url: str,
    video_info: dict,
    session_id: str,
    platform: str,
    formats: dict,
) -> str:
    """Сохраняет новую сессию и возвращает короткий токен для callback_data."""
    return SessionStore(
        context.user_data,
        key=_SESSION_STORE_KEY,
        max_active=_MAX_ACTIVE_SESSIONS,
        is_disposable=_session_is_disposable,
    ).create(
        url=url,
        video_info=video_info,
        session_id=session_id,
        platform=platform,
        formats=formats,
    )


def _get_session(context: ContextTypes.DEFAULT_TYPE, session_token: str) -> dict | None:
    """Возвращает данные сессии по токену."""
    return _get_session_store(context).get(session_token)


def _make_callback_data(
    session_token: str,
    scope: str,
    action: str,
    extra: str | None = None,
) -> str:
    """Формирует callback_data с привязкой к конкретной сессии."""
    parts = ["s", session_token, scope, action]
    if extra is not None:
        parts.append(extra)
    return "|".join(parts)


def _build_back_markup(session_token: str) -> InlineKeyboardMarkup:
    """Клавиатура с возвратом в меню текущей сессии."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    BTN_BACK,
                    callback_data=_make_callback_data(session_token, "main", "back"),
                )
            ]
        ]
    )


def _build_youtube_prompt(video_info: dict) -> str:
    """Текст карточки YouTube-видео с безопасным Markdown."""
    title = escape_markdown(str(video_info.get("title") or "Video"))
    duration = format_duration(int(video_info.get("duration") or 0))
    return DOWNLOAD_FORMAT_PROMPT.format(title=title, duration=duration)


def _build_main_menu(
    platform: str,
    video_info: dict,
    session_token: str,
    formats: dict | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Возвращает текст и клавиатуру главного меню для платформы."""
    formats = formats or {}
    raw_title = re.sub(r"\s+", " ", str(video_info.get("title") or "Video"))
    title = escape_markdown(raw_title[:160])
    uploader = escape_markdown(str(video_info.get("uploader") or "N/A"))
    duration = format_duration(int(video_info.get("duration") or 0))

    def download_rows(platform_name: str, is_photo_post: bool) -> list[list[InlineKeyboardButton]]:
        ordinary_label = BTN_DOWNLOAD_POST if is_photo_post else BTN_DOWNLOAD_VIDEO
        ordinary_action = f"{platform_name}_download"
        if description_status(video_info) == "available":
            return [
                [
                    InlineKeyboardButton(
                        BTN_DOWNLOAD_WITHOUT_DESCRIPTION,
                        callback_data=_make_callback_data(
                            session_token, "main", ordinary_action
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        BTN_DOWNLOAD_WITH_DESCRIPTION,
                        callback_data=_make_callback_data(
                            session_token,
                            "main",
                            f"{platform_name}_download_desc",
                        ),
                    )
                ],
            ]
        return [
            [
                InlineKeyboardButton(
                    ordinary_label,
                    callback_data=_make_callback_data(
                        session_token, "main", ordinary_action
                    ),
                )
            ]
        ]

    def description_status_line() -> str:
        status = description_status(video_info)
        if status == "empty":
            return f"\n{DESCRIPTION_EMPTY}"
        if status == "unavailable":
            return f"\n{DESCRIPTION_UNAVAILABLE}"
        return ""

    if platform == "tiktok":
        is_photo_post = bool(video_info.get("_nuvio_tiktok_photo_post"))
        keyboard = download_rows("tiktok", is_photo_post)
        if not (is_photo_post and not video_info.get("_nuvio_tiktok_audio_url")):
            keyboard.append(
                [
                    InlineKeyboardButton(
                        BTN_AUDIO_ONLY,
                        callback_data=_make_callback_data(
                            session_token, "main", "tiktok_audio"
                        ),
                    )
                ]
            )
        keyboard.append(
            [
                InlineKeyboardButton(
                    BTN_CANCEL,
                    callback_data=_make_callback_data(session_token, "main", "cancel"),
                )
            ]
        )
        if is_photo_post:
            images_count = len(video_info.get("_nuvio_tiktok_images") or [])
            text = f"*{title}*\nАвтор: {uploader}\nКадров: {images_count}\nЗвук: {'есть' if video_info.get('_nuvio_tiktok_audio_url') else 'нет'}\nДлительность: {duration}{description_status_line()}"
        else:
            text = f"*{title}*\nАвтор: {uploader}\nДлительность: {duration}{description_status_line()}"
        return text, InlineKeyboardMarkup(keyboard)

    if platform == "instagram":
        is_photo_post = bool(video_info.get("_nuvio_instagram_photo_post"))
        keyboard = download_rows("instagram", is_photo_post)
        if not (is_photo_post and not video_info.get("_nuvio_instagram_audio_url")):
            keyboard.append(
                [
                    InlineKeyboardButton(
                        BTN_AUDIO_ONLY,
                        callback_data=_make_callback_data(
                            session_token, "main", "instagram_audio"
                        ),
                    )
                ]
            )
        keyboard.append(
            [
                InlineKeyboardButton(
                    BTN_CANCEL,
                    callback_data=_make_callback_data(session_token, "main", "cancel"),
                )
            ]
        )
        if is_photo_post:
            images_count = len(video_info.get("_nuvio_instagram_images") or [])
            if video_info.get("_nuvio_instagram_mixed_post"):
                images_count = len(video_info.get("_nuvio_instagram_carousel_items") or [])
                text = f"*{title}*\nАвтор: {uploader}\n{MIXED_INSTAGRAM_POST}: {images_count} элементов\nДлительность: {duration}{description_status_line()}"
            else:
                text = f"*{title}*\nАвтор: {uploader}\nКадров: {images_count}\nЗвук: {'есть' if video_info.get('_nuvio_instagram_audio_url') else 'нет'}\nДлительность: {duration}{description_status_line()}"
        else:
            text = f"*{title}*\nАвтор: {uploader}\nДлительность: {duration}{description_status_line()}"
        return text, InlineKeyboardMarkup(keyboard)

    if platform == "rutube":
        keyboard = [
            [
                InlineKeyboardButton(
                    BTN_DOWNLOAD_VIDEO,
                    callback_data=_make_callback_data(
                        session_token, "main", "rutube_download"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    BTN_AUDIO_ONLY,
                    callback_data=_make_callback_data(
                        session_token, "main", "rutube_audio"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    BTN_CANCEL,
                    callback_data=_make_callback_data(session_token, "main", "cancel"),
                )
            ],
        ]
        text = f"*{title}*\nАвтор: {uploader}\nДлительность: {duration}"
        return text, InlineKeyboardMarkup(keyboard)

    if platform == "vk":
        selected_height = video_info.get("_nuvio_vk_format_height")
        video_label = (
            f"{BTN_DOWNLOAD_VIDEO} ({selected_height}p)"
            if selected_height else BTN_DOWNLOAD_VIDEO
        )
        keyboard = [
            [
                InlineKeyboardButton(
                    video_label,
                    callback_data=_make_callback_data(
                        session_token, "main", "vk_download"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    BTN_AUDIO_ONLY,
                    callback_data=_make_callback_data(
                        session_token, "main", "vk_audio"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    BTN_CANCEL,
                    callback_data=_make_callback_data(session_token, "main", "cancel"),
                )
            ],
        ]
        text = f"*{title}*\nАвтор: {uploader}\nДлительность: {duration}"
        return text, InlineKeyboardMarkup(keyboard)

    keyboard = [
        [
            InlineKeyboardButton(
                BTN_TG_VIDEO,
                callback_data=_make_callback_data(session_token, "main", "tg_video"),
            )
        ]
    ]
    # Кнопка звука рисуется только когда звук есть: у беззвучного видео она
    # раньше приводила к отказу уже после нажатия.
    if formats.get("audio_only"):
        keyboard.append(
            [
                InlineKeyboardButton(
                    BTN_AUDIO_M4A,
                    callback_data=_make_callback_data(
                        session_token, "main", "audio_m4a"
                    ),
                )
            ]
        )
    keyboard.append(
        [
            InlineKeyboardButton(
                BTN_MORE,
                callback_data=_make_callback_data(session_token, "main", "more"),
            )
        ]
    )
    keyboard.append(
        [
            InlineKeyboardButton(
                BTN_CANCEL,
                callback_data=_make_callback_data(session_token, "main", "cancel"),
            )
        ]
    )
    text = _build_youtube_prompt(video_info)
    return text, InlineKeyboardMarkup(keyboard)


def _format_size(size: int) -> str:
    """Размер для подписи кнопки; для неизвестного размера — пустая строка."""
    if size <= 0:
        return ""
    if size >= 1024 * 1024 * 1024:
        return f" · {size / 1024 / 1024 / 1024:.1f} ГБ"
    if size < 1024 * 1024:
        # «0 МБ» на дорожке в несколько сотен килобайт выглядит как ошибка.
        return f" · {size / 1024:.0f} КБ"
    return f" · {size / 1024 / 1024:.0f} МБ"


def _button_row(label: str, session_token: str, scope: str, action: str, value=None):
    """Строка клавиатуры из одной кнопки."""
    return [
        InlineKeyboardButton(
            label,
            callback_data=_make_callback_data(session_token, scope, action, value),
        )
    ]


SUBTITLE_FORMAT_LABELS = {"srt": "SRT", "vtt": "VTT", "txt": "Текст"}


def _build_cancel_markup(session_token: str) -> InlineKeyboardMarkup:
    """Клавиатура из одной кнопки отмены — для экранов ожидания."""
    return InlineKeyboardMarkup([_button_row(BTN_CANCEL, session_token, "main", "cancel")])


def _build_subtitle_language_menu(
    video_info: dict, session_token: str
) -> InlineKeyboardMarkup | None:
    """Меню языков субтитров.

    Returns:
        None, если ни русских, ни английских субтитров у видео нет.
    """
    languages = available_subtitle_languages(video_info)
    if not languages:
        return None

    keyboard = [
        _button_row(
            language.label, session_token, "format", "subs_lang", language.code
        )
        for language in languages
    ]
    keyboard.append(_button_row(BTN_BACK, session_token, "main", "more"))
    return InlineKeyboardMarkup(keyboard)


def _build_subtitle_format_menu(
    language: str, session_token: str
) -> InlineKeyboardMarkup:
    """Меню форматов субтитров для выбранного языка."""
    keyboard = [
        _button_row(
            SUBTITLE_FORMAT_LABELS[subtitle_format],
            session_token,
            "format",
            "subs",
            f"{language}:{subtitle_format}",
        )
        for subtitle_format in SUBTITLE_FORMATS
    ]
    # Назад ведёт к выбору языка, а не в главное меню: иначе каскад теряет смысл.
    keyboard.append(_button_row(BTN_BACK, session_token, "main", "subtitles"))
    return InlineKeyboardMarkup(keyboard)


def _build_more_menu(formats: dict, session_token: str) -> InlineKeyboardMarkup:
    """Разделы расширенного меню.

    Раньше здесь лежал плоский список: до трёх combined, до трёх «без звука», до
    двух аудио, плюс «Лучшее качество», «Лучшее аудио» и MP3. Ограничения
    прятали форматы (из 22 доступных было видно шесть), а дедупликация по тексту
    кнопки скрывала одно разрешение за другим. Теперь выбор идёт по разделам, и
    ни одно доступное разрешение не теряется.
    """
    keyboard = [_button_row(BTN_SECTION_VIDEO, session_token, "main", "video_menu")]
    if formats.get("audio_only"):
        keyboard.append(
            _button_row(BTN_SECTION_AUDIO, session_token, "main", "audio_menu")
        )
    keyboard.append(
        _button_row(BTN_SECTION_SUBTITLES, session_token, "main", "subtitles")
    )
    keyboard.append(_button_row(BTN_BACK, session_token, "main", "back"))
    return InlineKeyboardMarkup(keyboard)


def _build_video_menu(formats: dict, session_token: str) -> InlineKeyboardMarkup | None:
    """Меню разрешений: по одной кнопке на разрешение, от высокого к низкому.

    Returns:
        None, если ни одно разрешение не проходит по лимиту доставки.
    """
    options = list_video_options(
        formats.get("video_only", []),
        formats.get("audio_only", []),
        formats.get("combined", []),
        MAX_FILE_SIZE,
    )
    if not options:
        return None

    keyboard = [
        _button_row(
            f"{option.resolution}p{_format_size(option.size)}",
            session_token,
            "format",
            "combined",
            option.format_id,
        )
        for option in options
    ]
    keyboard.append(_button_row(BTN_BACK, session_token, "main", "more"))
    return InlineKeyboardMarkup(keyboard)


def _build_audio_menu(formats: dict, session_token: str) -> InlineKeyboardMarkup | None:
    """Меню звуковых дорожек: только родные и только пригодные для Telegram.

    Если родной пригодной дорожки нет, предлагается единственный вариант с
    перекодированием — иначе звук у таких видео был бы недоступен вовсе.

    Returns:
        None, если у видео нет звука вообще.
    """
    if not formats.get("audio_only"):
        return None

    options = list_audio_options(formats.get("audio_only", []), MAX_FILE_SIZE)
    if options:
        keyboard = [
            _button_row(
                f"🎵 {option.ext.upper()}{_format_size(option.size)}"
                f"{' · ' + option.language.upper() if option.language else ''}",
                session_token,
                "format",
                "audio_only",
                option.format_id,
            )
            for option in options
        ]
    else:
        keyboard = [
            _button_row(BTN_AUDIO_TRANSCODE, session_token, "main", "audio_m4a")
        ]
    keyboard.append(_button_row(BTN_BACK, session_token, "main", "more"))
    return InlineKeyboardMarkup(keyboard)


# Telegram гасит отметку активности через пять секунд, поэтому её обновляем чаще.
_CHAT_ACTION_REFRESH_SECONDS = 4

# Сколько подряд неудачных отметок терпим молча. Одна — рядовой сбой на длинной
# выгрузке; серия означает, что шапка чата пуста всё время работы.
_CHAT_ACTION_FAILURES_BEFORE_WARNING = 3


def _chat_action_for(action: str) -> str:
    """Подбирает отметку активности под характер работы."""
    if "audio" in action or action == "subtitles":
        return telegram.constants.ChatAction.UPLOAD_DOCUMENT
    return telegram.constants.ChatAction.UPLOAD_VIDEO


@contextlib.asynccontextmanager
async def _pulsing_chat_action(chat, action: str, enabled: bool = True):
    """Держит отметку «отправляет видео…» в шапке чата на всё время работы.

    Это единственная анимация, доступная боту: рисовать «крутилку» правкой
    текста значило бы запрос на каждый кадр и затирание статусов. Отметка —
    украшение, поэтому её отказ работу не роняет и не прекращает попыток:
    прежний цикл выходил навсегда после первой ошибки, и на длинной отправке
    шапка чата пустела до самого конца.
    """
    if not enabled:
        yield
        return

    async def _pulse() -> None:
        failures = 0
        while True:
            try:
                await chat.send_action(action)
                failures = 0
            except telegram.error.TelegramError as error:
                failures += 1
                # Первый сбой — рядовое дело на длинной выгрузке, поэтому шумим
                # только когда отметка не проходит подряд: это уже означает, что
                # пользователь всё время видит пустую шапку.
                if failures == _CHAT_ACTION_FAILURES_BEFORE_WARNING:
                    logger.warning(
                        "Отметка активности не проходит %s раз подряд: %s",
                        failures,
                        error,
                    )
                else:
                    logger.debug("Отметка активности не отправлена: %s", error)
            await asyncio.sleep(_CHAT_ACTION_REFRESH_SECONDS)

    task = asyncio.create_task(_pulse())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


_MAIN_DOWNLOAD_ACTIONS = {
    "youtube": {"tg_video", "audio_m4a"},
    "tiktok": {"tiktok_download", "tiktok_download_desc", "tiktok_audio"},
    "instagram": {"instagram_download", "instagram_download_desc", "instagram_audio"},
    "rutube": {"rutube_download", "rutube_audio"},
    "vk": {"vk_download", "vk_audio"},
}
_YOUTUBE_MENU_ACTIONS = {"more", "video_menu", "audio_menu", "subtitles"}
_FORMAT_DOWNLOAD_ACTIONS = {"combined", "audio_only", "subs"}


def _should_rate_limit_callback(callback_data: str | None) -> bool:
    """Ограничивает только дорогие callback-действия, а не навигацию по меню."""
    if not callback_data:
        return False

    event = CallbackEvent.parse(callback_data)
    if not event:
        return False
    if event.scope == "format":
        return event.action in _FORMAT_DOWNLOAD_ACTIONS
    return event.scope == "main" and any(
        event.action in actions for actions in _MAIN_DOWNLOAD_ACTIONS.values()
    )


def _is_main_action_allowed(session_data: dict, action: str) -> bool:
    """Проверяет действие по платформе сессии, а не по присланному названию."""
    if session_data.get("_metadata_pending"):
        return action == "cancel"
    platform = session_data.get("platform", "youtube")
    return (
        action in {"back", "cancel"}
        or action in _MAIN_DOWNLOAD_ACTIONS.get(platform, set())
        or (platform == "youtube" and action in _YOUTUBE_MENU_ACTIONS)
    )


def _is_format_selection_allowed(session_data: dict, action: str, value: str) -> bool:
    """Разрешает только варианты, предложенные меню этой сессии."""
    if session_data.get("_metadata_pending") or session_data.get("platform", "youtube") != "youtube":
        return False
    formats = session_data.get("formats") or {}
    if action == "combined":
        options = list_video_options(
            formats.get("video_only", []), formats.get("audio_only", []),
            formats.get("combined", []), MAX_FILE_SIZE,
        )
        return any(str(option.format_id) == value for option in options)
    if action == "audio_only":
        options = list_audio_options(formats.get("audio_only", []), MAX_FILE_SIZE)
        return any(str(option.format_id) == value for option in options)
    languages = {
        language.code for language in available_subtitle_languages(
            session_data.get("video_info") or {}
        )
    }
    if action == "subs_lang":
        return value in languages
    if action == "subs" and (choice := parse_subtitle_choice(value)):
        return choice[0] in languages
    return False


async def safe_edit_message_text(
    query: telegram.CallbackQuery, text: str, **kwargs
) -> bool:
    """Безопасно правит сообщение, отличая «нечего править» от настоящей поломки.

    Два исхода не считаются ошибкой:

    * текст и разметка не изменились — править нечего;
    * сообщения больше нет, пользователь его удалил. Это его право, и работу
      это не отменяет: файл уйдёт отдельным сообщением. Раньше такая правка
      поднимала исключение, обработчик ошибки пытался сообщить о ней той же
      правкой, падал так же, и всё уезжало в глобальный обработчик.

    Returns:
        `True`, если правка прошла, иначе `False`.
    """
    try:
        await query.edit_message_text(text, **kwargs)
        return True
    except telegram.error.BadRequest as e:
        reason = str(e).lower()
        if "message is not modified" in reason:
            logger.debug("edit_message_text пропущен: текст и разметка без изменений")
            return False
        if "message to edit not found" in reason:
            logger.info("Сообщение удалено пользователем — правка статуса пропущена")
            return False
        raise


async def _edit_delivery_status(query: telegram.CallbackQuery, text: str, **kwargs) -> bool:
    """Не превращает отказ служебного статуса в повторную отправку медиа."""
    try:
        return await safe_edit_message_text(query, text, **kwargs)
    except telegram.error.TelegramError as error:
        logger.warning("Не удалось обновить статус доставки: %s", type(error).__name__)
        return False


def _youtube_error_code(error_msg: str | BaseException) -> str:
    """Возвращает короткий код YouTube/yt-dlp ошибки для структурированного логирования."""
    return youtube_error_code(error_msg)


def _make_error_code(platform: str, category: str) -> str:
    platform_prefix = {
        "youtube": "YT",
        "tiktok": "TT",
        "instagram": "IG",
        "rutube": "RU",
        "vk": "VK",
        "telegram": "TG",
        "file": "FILE",
        "bot": "BOT",
    }.get(platform, "BOT")
    # Старые вызовы не должны возвращать пользователю категорию UNKNOWN.
    normalized_category = (
        "UNEXPECT" if category.upper() == "UNKNOWN" else category.upper()
    )[:8]
    return f"{platform_prefix}-{normalized_category}-{uuid.uuid4().hex[:6].upper()}"


def _classify_internal_error_category(platform: str, error_msg: str | BaseException) -> str:
    return classify_internal_error_category(platform, error_msg)


def _make_error_code_for_exception(
    platform: str, exc: BaseException, *, prefix_platform: str | None = None
) -> str:
    """Сохраняет одну категорию в коде и журнале при разных префиксах."""
    category = _classify_internal_error_category(platform, exc)
    return _make_error_code(prefix_platform or platform, category)


def _build_public_error_message(
    platform: str, error_code: str, error_msg: str | BaseException
) -> str:
    return build_public_error_message(platform, error_code, error_msg)


def _should_notify_admins_platform_failure(
    platform: str, category: str, stage: str
) -> bool:
    """Отделяет ожидаемые пользовательские ограничения платформ от настоящих аварий.

    `MEDIA_FORBIDDEN` сюда не входит намеренно. 403 на медиафайле — единственный
    признак, по которому видно, что платформа сменила правила выдачи: поломка
    YouTube 18.08.2026 пришла именно так, и узнали о ней ровно потому, что
    краш-репорт дошёл до админа. Замолчать эту категорию значит согласиться, что
    бот будет стоять сломанным, пока кто-нибудь не пожалуется.
    """
    if category in {
        "DURATION", "LARGE", "STORY_UNSUPPORTED", "NETWORK", "NETWORK_TIMEOUT", "TIMEOUT", "BLOCKED",
    }:
        return False
    return True


async def _log_platform_failure(
    *,
    platform: str,
    stage: str,
    url: str | None,
    error_code: str,
    exc: Exception,
    session_id: str | None = None,
    output_tail: list[str] | None = None,
) -> None:
    category = _classify_internal_error_category(platform, exc)
    should_notify_admins = _should_notify_admins_platform_failure(
        platform, category, stage
    )
    cookie_status = "not_checked"
    cookie_summary = "not_checked"
    if should_notify_admins and platform in {"youtube", "instagram", "tiktok"}:
        try:
            health = await asyncio.to_thread(check_cookie_health, platform)
            cookie_status = health.status
            cookie_summary = health.summary
        except Exception as health_exc:  # noqa: BLE001
            cookie_status = "health_failed"
            cookie_summary = str(health_exc)

    log_method = logger.error if should_notify_admins else logger.warning
    log_method(
        "USER_FLOW_FAIL code=%s platform=%s stage=%s category=%s exception=%s session_id=%s url=%s cookie_status=%s cookie_summary=%s error=%s",
        error_code,
        platform,
        stage,
        category,
        type(exc).__name__,
        session_id,
        url,
        cookie_status,
        cookie_summary,
        exc,
        exc_info=should_notify_admins,
    )

    if should_notify_admins:
        await _notify_admins_crash(
            error_code=error_code,
            platform=platform,
            stage=stage,
            url=url,
            exc=exc,
            session_id=session_id,
            cookie_status=cookie_status,
            cookie_summary=cookie_summary,
            output_tail=output_tail,
        )


def _schedule_platform_failure_log(
    *,
    platform: str,
    stage: str,
    url: str | None,
    error_code: str,
    exc: Exception,
    session_id: str | None = None,
) -> None:
    # Хвост вывода yt-dlp снимается здесь, синхронно, пока обработчик не отдал
    # управление. Репорт собирает фоновая задача, и она ждёт пробу cookies, а
    # обработчик за это время успевает уничтожить сессию вместе с её записями в
    # регистре: между отказом и уничтожением стоит всего один await.
    output_tail = download_report.output_tail(session_id)

    async def _runner() -> None:
        try:
            await _log_platform_failure(
                platform=platform,
                stage=stage,
                url=url,
                error_code=error_code,
                exc=exc,
                session_id=session_id,
                output_tail=output_tail,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to emit structured platform error log for %s", error_code
            )

    asyncio.create_task(_runner())


def _cache_format_id_for_main_action(platform: str, action: str) -> str | None:
    """Возвращает cache-key для прямых пользовательских действий."""
    return cache_key_for_main_action(platform, action)


async def _deliver_cached_audio(
    query: telegram.CallbackQuery, url: str, cache_key: str
) -> bool:
    """Отправляет аудио из кэша по file_id.

    Returns:
        bool: True, если файл доставлен; False, если записи нет или file_id устарел.
    """
    cached = await _cache_get(url, format_id=cache_key)
    if not cached:
        return False

    try:
        await _call_telegram_with_retry_after(
            lambda: _reply_cached(query, cached, default_kind="audio")
        )
    except telegram.error.BadRequest as e:
        if not _is_stale_file_id(e):
            raise
        logger.warning("file_id аудио устарел (key=%s): %s", cache_key, e)
        await _cache_invalidate(cached.file_id)
        return False

    logger.info("Аудио доставлено из кэша (key=%s)", cache_key)
    return True


async def _cache_get(url: str, format_id: str):
    """Старые строки без принадлежности боту не подмешиваются в новый кеш."""
    session = _active_delivery_session.get() or {}
    if session.get("_bot_id"):
        manifest = await cache_call(
            telegram_cache.artifacts.get, session["_bot_id"], url,
            recipe_for(session.get("platform", "youtube"), format_id),
        )
        if manifest and len(manifest["items"]) == 1:
            session["_cached_manifest_id"] = manifest["id"]
            return manifest["items"][0][0]
        return None
    return await cache_call(telegram_cache.get, url, format_id=format_id)


async def _cache_invalidate(file_id: str):
    session = _active_delivery_session.get() or {}
    if session.get("_bot_id"):
        await cache_call(telegram_cache.artifacts.invalidate, session["_bot_id"], file_id)
        session.pop("_cached_manifest_id", None)
    else:
        await cache_call(telegram_cache.delete_by_file_id, file_id)


def _record_confirmed_send(session: dict, result):
    """Подтверждение сохраняется до аналитики, кеша и следующего await."""
    messages = list(result) if isinstance(result, (tuple, list)) else [result]
    recorded = session.setdefault("_media_messages", [])
    recorded.extend(message for message in messages if message is not None)
    session["_delivery_progress"] = len(recorded)
    if recorded:
        session.setdefault("_first_media_message", recorded[0])


def _is_proven_not_sent(error: BaseException) -> bool:
    """Проверяет причину backend, не угадывая по тексту таймаута."""
    seen = set()
    while error is not None and id(error) not in seen:
        if isinstance(error, httpx.PoolTimeout):
            return True
        seen.add(id(error))
        if isinstance(error, httpx.RequestError):
            return False
        error = error.__cause__
    return False


def _description_for_delivery(session_data: dict) -> str | None:
    """Возвращает описание только для выбранного действия Instagram/TikTok."""
    if not session_data.get("_include_description"):
        return None
    if session_data.get("platform") not in {"tiktok", "instagram"}:
        return None
    return normalize_description(
        (session_data.get("video_info") or {}).get("description")
    )


def _description_chunks_for_delivery(session_data: dict) -> list[str]:
    return description_delivery_plan(_description_for_delivery(session_data))[1]


async def _send_description_chunks(
    query: telegram.CallbackQuery,
    chunks: list[str],
    first_media: telegram.Message | None,
    session_data: dict,
) -> bool:
    """Отправляет длинное описание рядом с первым подтвержденным медиа."""
    if not chunks:
        return True
    if session_data.get("_description_attempted"):
        return not session_data.get("_description_delivery_failed", False)
    session_data["_description_attempted"] = True
    first_id = getattr(first_media, "message_id", None)
    for chunk in chunks:
        if is_cancelled(str(session_data.get("session_id") or "")):
            raise CancelledByUser("отправка описания отменена")
        try:
            await _call_telegram_with_retry_after(
                lambda: query.message.reply_text(
                    chunk,
                    parse_mode=None,
                    reply_to_message_id=first_id,
                    allow_sending_without_reply=True,
                    do_quote=False,
                ),
                session_data,
                track_delivery_outcome=False,
            )
        except telegram.error.BadRequest as error:
            if first_id is None or not _is_missing_reply_target(error):
                session_data["_description_delivery_failed"] = True
                logger.warning("Не удалось отправить часть описания: %s", error)
                return False
            logger.info(
                "Медиа удалено до отправки описания; отправляю текст без reply"
            )
            first_id = None
            try:
                await _call_telegram_with_retry_after(
                    lambda: query.message.reply_text(chunk, parse_mode=None, do_quote=False),
                    session_data,
                    track_delivery_outcome=False,
                )
            except telegram.error.TelegramError as retry_error:
                session_data["_description_delivery_failed"] = True
                logger.warning(
                    "Не удалось отправить описание без привязки к медиа: %s",
                    retry_error,
                )
                return False
        except telegram.error.TelegramError as error:
            session_data["_description_delivery_failed"] = True
            logger.warning("Не удалось отправить часть описания: %s", error)
            return False
    return True


def _is_missing_reply_target(error: telegram.error.BadRequest) -> bool:
    """Проверяет только отказ, который подтверждает отсутствие reply-цели."""
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "message to reply not found",
            "message to be replied not found",
            "replied message not found",
            "reply message not found",
        )
    )


async def _call_telegram_with_retry_after(
    call,
    session_data: dict | None = None,
    *,
    reset_files=None,
    max_attempts: int = 2,
    track_delivery_outcome: bool = True,
) -> object:
    """Повторяет только явный RetryAfter с коротким бюджетом и отменой."""
    if session_data is None and track_delivery_outcome:
        session_data = _active_delivery_session.get()
    attempt = 1
    part = 0
    operation = None
    if session_data is not None:
        part = int(session_data.get("_journal_part", 0)) + 1
        session_data["_journal_part"] = part
        operation = session_data.get("_operation_id")
    while True:
        if (user_id := active_user_id.get()) is not None:
            await assert_user_allowed(user_id)
        if session_data and is_cancelled(str(session_data.get("session_id") or "")):
            raise CancelledByUser("отправка отменена до запроса Telegram")
        try:
            if operation:
                await run_db(journal.begin, operation, part, attempt,
                             session_data["_recipient_id"])
                if is_cancelled(str(session_data.get("session_id") or "")):
                    await run_db(journal.finish, operation, part, attempt, "not_sent")
                    raise CancelledByUser("отмена до передачи запроса Telegram")
                if (user_id := active_user_id.get()) is not None:
                    try:
                        await assert_user_allowed(user_id)
                    except BaseException:
                        await run_db(journal.finish, operation, part, attempt, "not_sent")
                        raise
            if operation and is_cancelled(str(session_data.get("session_id") or "")):
                await run_db(journal.finish, operation, part, attempt, "not_sent")
                raise CancelledByUser("отмена после проверки доступа до запроса Telegram")
            if session_data is not None:
                session_data["_delivery_request_in_flight"] = True
            try:
                try:
                    result = await call()
                except BaseException as error:
                    # Отмена ожидания и локальная ошибка не доказывают отказ Telegram.
                    if session_data is not None and track_delivery_outcome and not isinstance(error, telegram.error.TelegramError):
                        session_data["_delivery_outcome_unknown"] = True
                    raise
                if session_data is not None:
                    session_data["_delivery_request_in_flight"] = False
                if session_data is not None and track_delivery_outcome:
                    _record_confirmed_send(session_data, result)
                if operation:
                    ids = [getattr(message, "message_id", None) for message in
                           (result if isinstance(result, (list, tuple)) else [result])]
                    try:
                        await run_db(journal.finish, operation, part, attempt, "sent", ids)
                    except Exception:
                        logger.exception("Ответ Telegram подтвержден, запись журнала не обновлена")
                if session_data is not None and track_delivery_outcome and session_data.get("_bot_id"):
                    messages = result if isinstance(result, (list, tuple)) else [result]
                    items = [item for message in messages if (item := artifact_from_message(message)) is not None]
                    if items:
                        await cache_call(telegram_cache.artifacts.store, session_data["_bot_id"], items)
                return result
            finally:
                if session_data is not None:
                    session_data["_delivery_request_in_flight"] = False
        except telegram.error.RetryAfter as error:
            if operation:
                await run_db(journal.finish, operation, part, attempt, "refused")
            if attempt >= max_attempts:
                raise
            delay = error.retry_after
            delay_seconds = (
                delay.total_seconds() if isinstance(delay, timedelta) else float(delay)
            )
            if delay_seconds < 0 or delay_seconds > 60:
                raise
            logger.warning("Telegram попросил подождать %.2fс перед повтором", delay_seconds)
            remaining = delay_seconds
            while remaining > 0:
                if session_data and is_cancelled(
                    str(session_data.get("session_id") or "")
                ):
                    raise CancelledByUser("отправка отменена во время RetryAfter")
                interval = min(remaining, 1.0)
                await asyncio.sleep(interval)
                remaining -= interval
            if reset_files:
                reset_files()
            attempt += 1
        except telegram.error.BadRequest:
            # BadRequest наследует NetworkError, но подтверждает отказ сервера.
            if operation:
                await run_db(journal.finish, operation, part, attempt, "refused")
            raise
        except telegram.error.NetworkError as error:
            not_sent = _is_proven_not_sent(error)
            if session_data is not None and track_delivery_outcome:
                session_data["_delivery_outcome_unknown"] = not not_sent
            if operation:
                await run_db(journal.finish, operation, part, attempt, "not_sent" if not_sent else "unknown")
            if not_sent and attempt < max_attempts:
                if reset_files:
                    reset_files()
                attempt += 1
                await asyncio.sleep(0.25)
                continue
            raise
        except telegram.error.TelegramError:
            if operation:
                await run_db(journal.finish, operation, part, attempt, "refused")
            raise


async def _deliver_cached_video(
    query: telegram.CallbackQuery, url: str, cache_key: str
) -> bool:
    """Отправляет видео из кэша по file_id.

    Размеры здесь не передаются намеренно: при отправке по `file_id` Telegram
    берёт атрибуты сохранённого документа, и переданные значения игнорирует.
    Записи, снятые до ADR-002, поэтому и не чинятся правкой кода — их
    выбрасывает разовая чистка в `utils/video_cache.py`.

    Returns:
        bool: True, если файл доставлен; False, если записи нет или file_id устарел.
    """
    cached = await _cache_get(url, format_id=cache_key)
    if not cached:
        return False

    try:
        await _call_telegram_with_retry_after(
            lambda: _reply_cached(query, cached)
        )
    except telegram.error.BadRequest as e:
        if not _is_stale_file_id(e):
            raise
        logger.warning("file_id видео устарел (key=%s): %s", cache_key, e)
        await _cache_invalidate(cached.file_id)
        return False

    logger.info("Видео доставлено из кэша (key=%s)", cache_key)
    return True


def _is_stale_file_id(error: telegram.error.BadRequest) -> bool:
    """Разрешает повтор загрузки только при явном отказе ссылки на файл."""
    text = str(error).lower()
    return any(marker in text for marker in (
        "wrong file_id", "wrong file identifier", "file_id not found",
        "wrong remote file identifier",
        "file reference expired", "file_reference_expired",
    ))


async def _deliver_social_cached_video(
    query: telegram.CallbackQuery,
    file_id,
    session_data: dict,
) -> telegram.Message:
    """Отправляет социальное видео из кэша с выбранным описанием."""
    message = await _call_telegram_with_retry_after(
        lambda: _reply_cached(query, file_id, social=True,
            caption=description_delivery_plan(_description_for_delivery(session_data))[0]),
        session_data,
    )
    session_data["_first_media_message"] = message
    session_data["_delivered_items"] = 1
    session_data["_delivery_progress"] = 1
    session_data["_confirmed_delivery_messages"] = (message,)
    await _send_description_chunks(
        query,
        _description_chunks_for_delivery(session_data),
        message,
        session_data,
    )
    return message


async def _reply_cached(query, cached, *, default_kind="video", social=False, caption=None):
    """Метод отправки определяется фактическим типом сохраненного файла."""
    kind = getattr(cached, "kind", default_kind)
    if kind not in {"video", "audio", "photo", "document"}:
        kind = default_kind
    file_id = cached if isinstance(cached, str) else cached.file_id
    arguments = {kind: file_id}
    if kind == "video":
        arguments.update(caption=caption, supports_streaming=True)
    if social:
        arguments.update(do_quote=False, caption=caption, parse_mode=None)
    return await getattr(query.message, f"reply_{kind}")(**arguments)


def _cache_key_with_delivered_format(
    cache_format_id: str, delivered: str | None, url: str
) -> str:
    """Правит ключ кэша, если загрузчик принёс не тот формат, который просили.

    Каскад фолбеков при 403 молча подменяет формат: запрос `18` уезжает в
    `bestvideo+bestaudio`, а файл возвращается как запрошенный. Запись живёт в
    кэше 90 дней, поэтому под запрошенным ключом кнопка «1080p» отдавала бы
    240p до конца TTL — ключом становится формат, который реально принесли.

    Правится только ключ вида `combined:{format_id}`: обещание конкретного
    формата есть лишь в нём. Ключи-корзины (`tg_video`, `audio_m4a`,
    `direct_video`, `*_audio`) значат «то, что бот выбрал для этой кнопки», а
    загрузчик пишет в регистр обычный id формата (`137+140`) — принимая его за
    подмену, кэш этих кнопок перестал бы находиться вовсе.
    """
    prefix, separator, requested = cache_format_id.rpartition(":")
    if not separator or not delivered or delivered == requested:
        return cache_format_id

    # Расхождение — признак сработавшего фолбека: пользователь своё получил, но
    # админ должен знать, что запрошенный формат платформа не отдаёт.
    logger.warning(
        "Загрузчик принёс другой формат: запрошен %s, получен %s — "
        "кэшируем под полученным (url=%s)",
        requested,
        delivered,
        url,
    )
    return f"{prefix}{separator}{delivered}"


def _cache_sent_media(
    message: telegram.Message,
    url: str,
    platform: str,
    cache_format_id: str,
    video_info: dict | None = None,
    session_id: str | None = None,
) -> None:
    """Сохраняет file_id отправленного медиа в кэш.

    Ключ берётся с поправкой на формат, который загрузчик реально принёс, —
    см. `_cache_key_with_delivered_format`. Когда в регистр ничего не записали
    (так ведут себя все платформы, кроме YouTube), ключ остаётся запрошенным:
    прежнее поведение.

    Доставка уже состоялась, поэтому сбой кэша только логируется: ронять из-за
    него ответ пользователю нельзя.
    """
    session = _active_delivery_session.get() or {}
    artifact = artifact_from_message(message)
    if session.get("_bot_id"):
        if artifact:
            delivered = download_report.delivered_format(session_id) if session_id else None
            key = _cache_key_with_delivered_format(cache_format_id, delivered, url)
            try:
                telegram_cache.artifacts.put(session["_bot_id"], url,
                    recipe_for(platform, key), [(artifact, "media")], {})
            except (OSError, ValueError, sqlite3.Error):
                logger.warning("Не удалось сохранить подтвержденный файл в кеш", exc_info=True)
        return
    media = message.video or message.audio or message.document
    file_id = getattr(media, "file_id", None) if media else None
    if not file_id:
        return

    # Без session_id регистр спрашивать нечем: записи вне сессии лежат под общим
    # ключом, и чужой формат попал бы в кэш ровно тем способом, от которого
    # здесь защищаемся.
    delivered = download_report.delivered_format(session_id) if session_id else None
    key_format_id = _cache_key_with_delivered_format(cache_format_id, delivered, url)

    try:
        telegram_cache.set(
            CachedVideo(
                url=url,
                file_id=file_id,
                file_unique_id=getattr(media, "file_unique_id", None),
                platform=platform,
                format_id=key_format_id,
                cached_at=datetime.now(),
                file_size=getattr(media, "file_size", None),
                duration=getattr(media, "duration", None),
                title=video_info.get("title") if video_info else None,
            )
        )
        logger.info(
            "💾 Файл сохранён в кэш: %s -> %s (key=%s)", url, file_id, key_format_id
        )
    except Exception as e:
        logger.error("Ошибка сохранения в кэш: %s", e)


async def _deliver_by_url(
    query: telegram.CallbackQuery,
    plan: UrlHandoff,
    url: str,
    platform: str,
    cache_format_id: str | None = None,
    video_info: dict | None = None,
    session_data: dict | None = None,
) -> DeliveryOutcome:
    """Отдаёт медиа Telegram прямой ссылкой, минуя диск.

    Returns:
        DeliveryOutcome: состояние отправки, подтвержденное сообщение и число
        доставленных элементов. Неизвестный исход запрещает слепой fallback.
    """
    size_mb = plan.size / 1024 / 1024
    now = asyncio.get_running_loop().time()
    if _HANDOFF_REFUSALS.is_cooling_down(plan.url, plan.kind, now):
        logger.info(
            "Пропускаю доставку ссылкой (%s): CDN недавно отказал Telegram", plan.kind
        )
        return DeliveryOutcome("refused")

    if session_data is not None:
        session_data["_delivery_request_in_flight"] = True
    try:
        match plan.kind:
            case "video":
                # Размеры сюда приходят из метаданных источника: файл на этом
                # пути не скачивается, померить его нечем. Источник их знает не
                # всегда — тогда отправка идёт как раньше (ADR-002).
                geometry = {
                    key: value
                    for key, value in (
                        ("width", plan.width),
                        ("height", plan.height),
                        ("duration", plan.duration),
                    )
                    if value
                }
                message = await _call_telegram_with_retry_after(
                    lambda: query.message.reply_video(
                        do_quote=False if platform in {"tiktok", "instagram"} else None,
                        video=plan.url,
                        caption=(
                            description_delivery_plan(
                                _description_for_delivery(session_data or {})
                            )[0]
                        ),
                        parse_mode=None,
                        supports_streaming=True,
                        **geometry,
                    ),
                    session_data,
                )
            case "audio":
                message = await _call_telegram_with_retry_after(
                    lambda: query.message.reply_audio(audio=plan.url, caption=None, do_quote=False),
                    session_data,
                )
            case _:
                message = await _call_telegram_with_retry_after(
                    lambda: query.message.reply_photo(photo=plan.url, caption=None, do_quote=False),
                    session_data,
                )
    except telegram.error.BadRequest as e:
        if platform in {"tiktok", "instagram"} and not _is_url_handoff_refusal(e):
            raise
        _HANDOFF_REFUSALS.remember(plan.url, plan.kind, now)
        logger.warning(
            "Telegram не принял ссылку (%s, %.2f МБ): %s — уходим на скачивание",
            plan.kind,
            size_mb,
            e,
        )
        return DeliveryOutcome("refused", error=e)
    except (telegram.error.NetworkError, telegram.error.TimedOut) as e:
        if session_data is not None:
            session_data["_delivery_outcome_unknown"] = not _is_proven_not_sent(e)
        return DeliveryOutcome("refused" if _is_proven_not_sent(e) else "unknown", error=e)
    except telegram.error.TelegramError as e:
        if platform in {"tiktok", "instagram"}:
            raise
        _HANDOFF_REFUSALS.remember(plan.url, plan.kind, now)
        logger.warning("Telegram не принял ссылку (%s): %s", plan.kind, e)
        return DeliveryOutcome("refused", error=e)
    finally:
        if session_data is not None:
            session_data["_delivery_request_in_flight"] = False

    logger.info("Медиа доставлено ссылкой (%s, %.2f МБ)", plan.kind, size_mb)
    if session_data is not None:
        session_data["_first_media_message"] = message
        session_data["_delivered_items"] = 1
        session_data["_delivery_progress"] = 1
        session_data["_confirmed_delivery_messages"] = (message,)
    if plan.kind == "video" and session_data is not None:
        chunks = _description_chunks_for_delivery(session_data)
        await _send_description_chunks(query, chunks, message, session_data)
    # Кэш file_id рассчитан на видео, аудио и документы; фото-посты в нём не
    # хранятся, поэтому для них запись пропускается.
    # Регистр диагностики здесь не спрашивается намеренно: ссылка ведёт ровно на
    # запрошенный формат, загрузчик в этом пути не участвует, а запись в регистре
    # могла остаться от предыдущей неудачной попытки той же сессии.
    if cache_format_id and plan.kind != "photo":
        await cache_call(_cache_sent_media, message, url, platform, cache_format_id, video_info)
    return DeliveryOutcome("delivered", (message,), confirmed_items=1)


# CDN может отказать инфраструктуре Telegram, оставаясь доступным для нас;
# тогда попытка отдать ссылку — чистая потеря 0.3 с на каждом запросе. Память
# отказов гасит попытки на время и сама возвращает их, когда политика CDN
# меняется. Живёт в процессе: переживать перезапуск ей незачем.
_HANDOFF_REFUSALS = HandoffRefusals()


async def _deliver_photo_post_by_url(
    query: telegram.CallbackQuery,
    plan: PhotoPostHandoff,
    session_data: dict | None = None,
) -> DeliveryOutcome:
    """Отправляет фото-пост прямыми ссылками.

    Returns:
        DeliveryOutcome: число подтвержденных фото и сообщения; unknown запрещает
        повторять отправку на файловом пути.
    """
    now = asyncio.get_running_loop().time()
    delivery_state = session_data if session_data is not None else {}
    first = plan.images[0]
    if _HANDOFF_REFUSALS.is_cooling_down(first.url, first.kind, now):
        logger.info("Пропускаю фото-пост ссылками: CDN недавно отказал Telegram")
        return DeliveryOutcome("refused")

    offset = 0
    sent_messages = []
    caption, description_chunks = description_delivery_plan(
        _description_for_delivery(delivery_state)
    )
    for block_index, group_size in enumerate(
        media_album_sizes(len(plan.images)), start=1
    ):
        group = plan.images[offset : offset + group_size]
        if is_cancelled(str(delivery_state.get("session_id") or "")):
            raise CancelledByUser("отправка альбома отменена")
        try:
            if len(group) == 1:
                messages = [
                    await _call_telegram_with_retry_after(
                        lambda: query.message.reply_photo(
                            do_quote=False,
                            photo=group[0].url,
                            caption=caption if offset == 0 else None,
                            parse_mode=None,
                        ),
                        delivery_state,
                    )
                ]
            else:
                media = [
                    InputMediaPhoto(
                        media=image.url,
                        caption=caption if offset == 0 and index == 0 else None,
                        parse_mode=None,
                    )
                    for index, image in enumerate(group)
                ]
                messages = await _call_telegram_with_retry_after(
                    lambda: query.message.reply_media_group(media=media, do_quote=False),
                    delivery_state,
                )
        except telegram.error.BadRequest as error:
            if not (
                _is_url_handoff_refusal(error)
                or _is_photo_format_refusal(error)
            ):
                raise
            _HANDOFF_REFUSALS.remember(group[0].url, group[0].kind, now)
            logger.warning(
                "Telegram не принял URL блока фото-поста после %s кадров: %s",
                offset,
                error,
            )
            return DeliveryOutcome(
                "refused",
                tuple(sent_messages),
                confirmed_items=offset,
                error=error,
            )
        except telegram.error.NetworkError as error:
            delivery_state["_delivery_outcome_unknown"] = not _is_proven_not_sent(error)
            return DeliveryOutcome(
                "refused" if _is_proven_not_sent(error) else "unknown",
                tuple(sent_messages),
                confirmed_items=offset,
                error=error,
            )
        if offset == 0 and messages:
            delivery_state["_first_media_message"] = messages[0]
        sent_messages.extend(messages)
        offset += group_size
        delivery_state["_delivered_items"] = offset
        delivery_state["_delivery_progress"] = offset
        delivery_state["_confirmed_delivery_messages"] = tuple(sent_messages)
        logger.info(
            "Медиа-блок доставлен: platform=%s session=%s block=%s transport=url items=%s",
            delivery_state.get("platform", "social"),
            delivery_state.get("session_id", "unknown"),
            block_index,
            group_size,
        )

    await _send_description_chunks(
        query,
        description_chunks,
        delivery_state.get("_first_media_message"),
        delivery_state,
    )

    if plan.audio:
        if is_cancelled(str(delivery_state.get("session_id") or "")):
            raise CancelledByUser("отправка аудио отменена")
        try:
            audio_message = await _call_telegram_with_retry_after(
                lambda: query.message.reply_audio(
                    do_quote=False,
                    audio=plan.audio.url, caption=None
                ),
                delivery_state,
            )
        except telegram.error.BadRequest as error:
            if not _is_url_handoff_refusal(error):
                raise
            logger.warning("Telegram не принял URL аудио фото-поста: %s", error)
            delivery_state["_audio_delivered"] = False
            return DeliveryOutcome(
                "refused",
                tuple(sent_messages),
                confirmed_items=offset,
                audio_delivered=False,
                error=error,
            )
        except telegram.error.NetworkError as error:
            delivery_state["_delivery_outcome_unknown"] = not _is_proven_not_sent(error)
            return DeliveryOutcome(
                "refused" if _is_proven_not_sent(error) else "unknown", tuple(sent_messages), confirmed_items=offset,
                audio_delivered=None, error=error,
            )
        delivery_state["_audio_delivered"] = True
        sent_messages.append(audio_message)

    logger.info("Фото-пост доставлен ссылками: %s кадров", len(plan.images))
    return DeliveryOutcome(
        "delivered",
        tuple(sent_messages),
        confirmed_items=offset,
        audio_delivered=True if plan.audio else None,
    )


def _is_url_handoff_refusal(error: telegram.error.BadRequest) -> bool:
    """Отличает отказ загрузить URL от ошибки подписи или параметров запроса."""
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "failed to get http url content",
            "wrong file identifier/http url specified",
            "wrong type of the web page content",
            "failed to open the file",
            "failed to fetch url",
        )
    )


def _is_photo_format_refusal(error: telegram.error.BadRequest) -> bool:
    """Проверяет конкретные ответы Telegram о формате или геометрии фото."""
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "photo must be non-empty",
            "photo dimensions",
            "photo_invalid_dimensions",
            "unsupported image format",
            "invalid image",
            "image_process_failed",
        )
    )


@contextlib.contextmanager
def _upload_source(path: Path):
    """Дает путь для локального Bot API или открытый поток для облачного.

    Второе значение перематывает поток перед повтором после RetryAfter.
    """
    if TELEGRAM_LOCAL_MODE:
        yield path.resolve(), None
        return
    with path.open("rb") as handle:
        yield handle, lambda: handle.seek(0)


def _album_input(source):
    """Оборачивает поток так, чтобы PTB читал его только при выгрузке."""
    if TELEGRAM_LOCAL_MODE:
        return source
    return telegram.InputFile(source, attach=True, read_file_handle=False)


def _reset_all(sources: list[tuple[object, object]]):
    resets = [reset for _source, reset in sources if reset]
    if not resets:
        return None

    def reset_files() -> None:
        for reset in resets:
            reset()

    return reset_files


async def _send_items_one_by_one(
    query: telegram.CallbackQuery,
    items: list[tuple[str, Path, dict]],
    caption: str | None,
    session_data: dict,
    *,
    photo_refused: bool,
    cancel_reason: str,
) -> list[telegram.Message]:
    """Досылает отклоненный альбом по одному элементу в исходном порядке.

    sendMediaGroup отклоняется целиком, поэтому повтор не создает дублей.
    Фото с отказом по формату уходит тем же файлом как документ. Если
    отклонено было одиночное фото, повторять sendPhoto незачем.
    """
    results = []
    progress_base = int(session_data.get("_delivered_items") or 0)
    for index, (kind, path, geometry) in enumerate(items):
        if is_cancelled(str(session_data.get("session_id") or "")):
            raise CancelledByUser(cancel_reason)
        item_caption = caption if index == 0 else None
        with _upload_source(path) as (source, reset):
            if kind == "video":
                sent = await _call_telegram_with_retry_after(
                    lambda: query.message.reply_video(
                        do_quote=False,
                        video=source,
                        caption=item_caption,
                        parse_mode=None,
                        supports_streaming=True,
                        **geometry,
                    ),
                    session_data,
                    reset_files=reset,
                )
            else:
                def send_document():
                    return _call_telegram_with_retry_after(
                        lambda: query.message.reply_document(
                            do_quote=False,
                            document=source,
                            caption=item_caption,
                            parse_mode=None,
                        ),
                        session_data,
                        reset_files=reset,
                    )

                if photo_refused:
                    sent = await send_document()
                else:
                    try:
                        sent = await _call_telegram_with_retry_after(
                            lambda: query.message.reply_photo(
                                do_quote=False,
                                photo=source,
                                caption=item_caption,
                                parse_mode=None,
                            ),
                            session_data,
                            reset_files=reset,
                        )
                    except telegram.error.BadRequest as photo_error:
                        if not _is_photo_format_refusal(photo_error):
                            raise
                        if reset:
                            reset()
                        sent = await send_document()
        results.append(sent)
        session_data.setdefault("_first_media_message", sent)
        session_data["_confirmed_delivery_messages"] = (
            *session_data.get("_confirmed_delivery_messages", ()), sent
        )
        session_data["_delivered_items"] = progress_base + len(results)
        session_data["_delivery_progress"] = progress_base + len(results)
    return results


async def _send_photo_file_group(
    query: telegram.CallbackQuery,
    image_paths: list[Path],
    caption: str | None,
    session_data: dict,
) -> list[telegram.Message]:
    """Отправляет последовательную группу фото и сохраняет документный откат."""
    if is_cancelled(str(session_data.get("session_id") or "")):
        raise CancelledByUser("отправка фото отменена")
    with contextlib.ExitStack() as stack:
        sources = [stack.enter_context(_upload_source(path)) for path in image_paths]
        reset_files = _reset_all(sources)
        media = [
            InputMediaPhoto(
                media=_album_input(source),
                caption=caption if index == 0 else None,
                parse_mode=None,
            )
            for index, (source, _reset) in enumerate(sources)
        ]
        try:
            if len(media) == 1:
                message = await _call_telegram_with_retry_after(
                    lambda: query.message.reply_photo(
                        do_quote=False,
                        photo=media[0].media,
                        caption=caption,
                        parse_mode=None,
                        write_timeout=1800,
                        read_timeout=1800,
                    ),
                    session_data,
                    reset_files=reset_files,
                )
                return [message]
            return await _call_telegram_with_retry_after(
                lambda: query.message.reply_media_group(media=media, do_quote=False),
                session_data,
                reset_files=reset_files,
            )
        except telegram.error.BadRequest as error:
            if not _is_photo_format_refusal(error):
                raise
    return await _send_items_one_by_one(
        query,
        [("photo", path, {}) for path in image_paths],
        caption,
        session_data,
        photo_refused=len(image_paths) == 1,
        cancel_reason="отправка фото отменена",
    )


async def _send_mixed_media_group(
    query: telegram.CallbackQuery,
    items: list[dict],
    caption: str | None,
    session_data: dict,
) -> list[telegram.Message]:
    """Отправляет упорядоченный смешанный блок фото и видео Instagram."""
    if is_cancelled(str(session_data.get("session_id") or "")):
        raise CancelledByUser("отправка карусели отменена")

    async def geometry_for(path: Path) -> dict:
        try:
            return await run_blocking(
                get_video_geometry,
                path,
                description="get_instagram_carousel_video_geometry",
                session_id=session_data.get("session_id"),
            ) or {}
        except Exception as error:  # noqa: BLE001
            logger.warning("Не удалось измерить видео карусели %s: %s", path, error)
            return {}

    prepared = []
    for item in items:
        kind = item.get("kind")
        path = Path(item["path"])
        if kind not in {"photo", "video"} or not _file_ready_to_send(path):
            raise FileNotFoundError(str(path))
        prepared.append((kind, path, await geometry_for(path) if kind == "video" else {}))

    with contextlib.ExitStack() as stack:
        sources = [stack.enter_context(_upload_source(path)) for _kind, path, _g in prepared]
        reset_files = _reset_all(sources)
        media = []
        for index, ((kind, _path, geometry), (source, _reset)) in enumerate(
            zip(prepared, sources)
        ):
            item_caption = caption if index == 0 else None
            if kind == "video":
                media_geometry = dict(geometry)
                if media_geometry.get("duration") is not None:
                    media_geometry["duration"] = timedelta(
                        seconds=int(media_geometry["duration"])
                    )
                media.append(
                    InputMediaVideo(
                        media=_album_input(source),
                        caption=item_caption,
                        parse_mode=None,
                        supports_streaming=True,
                        **media_geometry,
                    )
                )
            else:
                media.append(
                    InputMediaPhoto(
                        media=_album_input(source),
                        caption=item_caption,
                        parse_mode=None,
                    )
                )

        try:
            if len(media) > 1:
                return await _call_telegram_with_retry_after(
                    lambda: query.message.reply_media_group(media=media, do_quote=False),
                    session_data,
                    reset_files=reset_files,
                )
            kind, _path, geometry = prepared[0]
            file_input = media[0].media
            if kind == "video":
                return [
                    await _call_telegram_with_retry_after(
                        lambda: query.message.reply_video(
                            do_quote=False,
                            video=file_input,
                            caption=caption,
                            parse_mode=None,
                            supports_streaming=True,
                            **{key: value for key, value in geometry.items() if value},
                        ),
                        session_data,
                        reset_files=reset_files,
                    )
                ]
            return [
                await _call_telegram_with_retry_after(
                    lambda: query.message.reply_photo(
                        do_quote=False,
                        photo=file_input,
                        caption=caption,
                        parse_mode=None,
                    ),
                    session_data,
                    reset_files=reset_files,
                )
            ]
        except telegram.error.BadRequest as error:
            if not any(kind == "photo" for kind, _path, _g in prepared) or not _is_photo_format_refusal(error):
                raise
    return await _send_items_one_by_one(
        query,
        prepared,
        caption,
        session_data,
        photo_refused=len(prepared) == 1,
        cancel_reason="отправка карусели отменена",
    )


async def _deliver_plan(
    query: telegram.CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    session_token: str,
    session_data: dict,
    plan: UrlHandoff | None,
    cache_format_id: str | None = None,
) -> bool:
    """Доводит доставку по ссылке до конца: отправка, статус, очистка сессии.

    Returns:
        bool: True, если пользователь уже получил медиа. False означает, что
        доставка ссылкой не состоялась и нужно продолжать обычным путём — диск
        при этом ещё не тронут.
    """
    if not plan:
        return False

    outcome = await _deliver_by_url(
        query,
        plan,
        session_data["url"],
        session_data.get("platform", "bot"),
        cache_format_id,
        session_data.get("video_info"),
        session_data,
    )
    if outcome.state == "unknown":
        logger.warning(
            "Неизвестный исход URL-видео: platform=%s session=%s error=%s",
            session_data.get("platform", "social"),
            session_data.get("session_id", "unknown"),
            type(outcome.error).__name__ if outcome.error else "unknown",
        )
        await _edit_delivery_status(query, DELIVERY_OUTCOME_UNKNOWN)
        await _cleanup_user_session(query.from_user.id, context, session_token)
        return True
    if outcome.state != "delivered":
        return False

    await _record_delivery(query.from_user.id, session_data)
    await _edit_delivery_status(
        query,
        DESCRIPTION_SEND_FAILED
        if session_data.get("_description_delivery_failed")
        else FILE_SENT
    )
    await _cleanup_user_session(query.from_user.id, context, session_token)
    return True


def _cache_format_id_for_format_selection(
    content_type: str, format_id: str
) -> str | None:
    """Возвращает cache-key для выбранного формата."""
    return cache_key_for_format_selection(content_type, format_id)


async def _begin_processing(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    platform: str,
) -> tuple[telegram.Message, str, str]:
    """Показывает экран ожидания с кнопкой отмены и заводит сессию заранее.

    Сессия создаётся до разбора ссылки намеренно: без неё кнопке отмены не за
    что зацепиться, и передумавшему оставалось бы только ждать.
    """
    session_id = f"{update.effective_user.id}_{uuid.uuid4()}"
    create_temp_dir(session_id)
    session_token = _store_session(
        context,
        url=url,
        video_info={},
        session_id=session_id,
        platform=platform,
        formats={},
    )
    _get_session(context, session_token)["_metadata_pending"] = True
    message = await update.message.reply_text(
        PROCESSING_MESSAGE, reply_markup=_build_cancel_markup(session_token)
    )
    return message, session_token, session_id


def _finish_processing(
    context: ContextTypes.DEFAULT_TYPE,
    session_token: str,
    video_info: dict,
    formats: dict,
) -> bool:
    """Дописывает сессию разобранными данными.

    Returns:
        False, если сессии уже нет: пользователь нажал «Отмена», пока шёл
        разбор ссылки, и показывать меню поверх этого нельзя.
    """
    session = _get_session(context, session_token)
    if session is None:
        return False
    session["video_info"] = video_info
    session["formats"] = formats
    session.pop("_metadata_pending", None)
    return True


async def _cleanup_user_session(
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    session_token: str | None = None,
) -> None:
    """Очищает конкретную сессию пользователя, не затрагивая остальные меню.

    Вместе с файлами уходят и записи регистра диагностики: сессии больше нет,
    а хвост вывода yt-dlp и фактический формат относятся именно к ней. В
    аварийных ветках, где файлы чистятся, а сессия остаётся, регистр намеренно
    сохраняется: краш-репорт собирается фоновой задачей уже после выхода из
    обработчика и читает хвост оттуда.
    """
    if session_token:
        session = _get_session_store(context).pop(session_token, None)
        session_id = session.get("session_id") if session else None
        if session_id:
            _cleanup_session_when_idle(session_id, forget_report=True)
            logger.info(
                "Временные файлы для сессии %s пользователя %s очищены.",
                session_id,
                user_id,
            )
        logger.info("Сессия %s пользователя %s очищена.", session_token, user_id)
        return

    session_id = context.user_data.get("session_id")
    if session_id:
        _cleanup_session_when_idle(session_id, forget_report=True)
        logger.info(
            f"Временные файлы для legacy-сессии {session_id} пользователя {user_id} очищены."
        )
    preserved_state = {
        key: context.user_data[key]
        for key in (*_ANTISPAM_STATE_KEYS, _SESSION_STORE_KEY)
        if key in context.user_data
    }
    context.user_data.clear()
    context.user_data.update(preserved_state)
    logger.info(f"Legacy-сессия (user_data) для пользователя {user_id} очищена.")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает команду /start.

    Args:
        update (Update): Объект обновления Telegram.
        context (ContextTypes.DEFAULT_TYPE): Контекст.
    """
    context.user_data.pop(FEEDBACK_FORM_KEY, None)
    logger.info(f"Получена команда /start от пользователя {update.effective_user.id}")
    await _track_tg_user(update)
    await run_db(track_event, update.effective_user.id, "start")
    from utils.cookie_manager import build_admin_entry_markup, is_admin

    user_id = update.effective_user.id if update.effective_user else None
    if is_admin(user_id):
        await update.message.reply_text(
            f"{WELCOME_MESSAGE}\n\n🔐 Доступна админ-панель: /admin",
            reply_markup=feedback_markup(context, existing=build_admin_entry_markup()),
        )
        return

    await update.message.reply_text(WELCOME_MESSAGE, reply_markup=feedback_markup(context))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает команду /help.

    Args:
        update (Update): Объект обновления Telegram.
        context (ContextTypes.DEFAULT_TYPE): Контекст.
    """
    logger.info(f"Получена команда /help от пользователя {update.effective_user.id}")
    from utils.cookie_manager import is_admin

    user_id = update.effective_user.id if update.effective_user else None
    help_message = HELP_MESSAGE
    if is_admin(user_id):
        help_message += (
            "\n\n🔐 *Для администратора:* используйте /admin для управления cookies."
        )
    await update.message.reply_text(help_message, parse_mode="Markdown")


async def _get_url_from_context(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> str | None:
    """Извлекает URL из команды /download или текста сообщения."""
    if update.message and update.message.text.startswith("/download"):
        if not context.args:
            await update.message.reply_text(NO_URL_AFTER_COMMAND)
            return None
        return context.args[0]
    elif update.message:
        return update.message.text
    return None


async def download_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает команду /download.

    Args:
        update (Update): Объект обновления Telegram.
        context (ContextTypes.DEFAULT_TYPE): Контекст.
    """
    context.user_data.pop(FEEDBACK_FORM_KEY, None)
    user_id = update.effective_user.id
    logger.info(f"Получена команда /download от пользователя {user_id}")
    url = await _get_url_from_context(update, context)
    if not url:
        return

    await process_url(update, context, url)


async def process_url(
    update: Update, context: ContextTypes.DEFAULT_TYPE, url: str | None = None
) -> None:
    """
    Обрабатывает полученный URL от пользователя.

    Args:
        update (Update): Объект обновления Telegram.
        context (ContextTypes.DEFAULT_TYPE): Контекст.
    """
    user_id = update.effective_user.id

    # Перехват текстового отзыва CSI (если пользователь не прислал ссылку)
    if url is None and update.message and update.message.text:
        awaiting_id = context.user_data.get("awaiting_csi_feedback_id")
        if awaiting_id:
            text = update.message.text
            # Если прислана ссылка — сбрасываем ожидание отзыва и обрабатываем как URL
            if "http://" in text or "https://" in text:
                context.user_data.pop("awaiting_csi_feedback_id", None)
            else:
                try:
                    await run_db(update_csi_feedback, awaiting_id, text)
                    await update.message.reply_text(CSI_FEEDBACK_THANKS)
                except Exception as e:
                    logger.error(f"Ошибка сохранения CSI отзыва: {e}")
                finally:
                    context.user_data.pop("awaiting_csi_feedback_id", None)
                return

    if url is None:
        from utils.cookie_manager import handle_admin_text_input

        if await handle_admin_text_input(update, context):
            return

    if update.message:
        now = asyncio.get_running_loop().time()
        if _check_spam(user_id, context, now):
            await update.message.reply_text(SPAM_WARNING)
            return
    if not url:
        url_from_message = await _get_url_from_context(update, context)
        if not url_from_message:
            return
        url = url_from_message
    logger.info(f"Обработка URL '{url}' от пользователя {user_id}")
    await _track_tg_user(update)

    # Определяем платформу для аналитики
    _analytics_platform = None
    if is_valid_youtube_url(url):
        _analytics_platform = "youtube"
    elif is_valid_tiktok_url(url):
        _analytics_platform = "tiktok"
    elif is_valid_instagram_url(url):
        _analytics_platform = "instagram"
    elif is_valid_rutube_url(url):
        _analytics_platform = "rutube"
    elif is_valid_vk_url(url):
        _analytics_platform = "vk"
    if _analytics_platform:
        try:
            await run_db(
                track_event, user_id, "download", platform=_analytics_platform, url=url
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Аналитика запроса недоступна: %s", exc)

    # Проверка YouTube
    if is_valid_youtube_url(url):
        processing_message, session_token, session_id = await _begin_processing(
            update, context, url, "youtube"
        )
        try:
            video_info = await run_blocking(
                functools.partial(get_video_info, url, session_id=session_id),
                description="get_video_info",
                session_id=session_id,
            )
            formats = get_available_formats(video_info)
            if not _finish_processing(context, session_token, video_info, formats):
                return
            text, reply_markup = _build_main_menu(
                "youtube", video_info, session_token, formats
            )
            try:
                await processing_message.edit_text(
                    text, reply_markup=reply_markup, parse_mode="Markdown"
                )
            except telegram.error.BadRequest as exc:
                if not any(
                    marker in str(exc).lower()
                    for marker in ("parse entities", "end of entity")
                ):
                    raise
                logger.warning("Telegram отклонил Markdown меню YouTube: %s", exc)
                await processing_message.edit_text(
                    _markdown_to_plain_text(text),
                    reply_markup=reply_markup,
                    parse_mode=None,
                )
        except (yt_dlp.utils.DownloadError, yt_dlp.cookies.CookieLoadError) as e_cookie:
            error_code = _make_error_code(
                "youtube", _classify_internal_error_category("youtube", e_cookie)
            )
            _schedule_platform_failure_log(
                platform="youtube",
                stage="process_url",
                url=url,
                error_code=error_code,
                exc=e_cookie,
                session_id=session_id,
            )
            await processing_message.edit_text(
                _build_public_error_message("youtube", error_code, e_cookie),
                reply_markup=feedback_markup(context, error_code, url),
            )
            if session_id:
                _cleanup_session_when_idle(session_id)
        except asyncio.CancelledError:
            if session_id:
                _cleanup_session_when_idle(session_id)
            raise
        except asyncio.TimeoutError as e:
            error_code = _make_error_code_for_exception("youtube", e)
            _schedule_platform_failure_log(
                platform="youtube",
                stage="process_url_timeout",
                url=url,
                error_code=error_code,
                exc=e,
                session_id=session_id,
            )
            await processing_message.edit_text(
                _build_public_error_message("youtube", error_code, e),
                reply_markup=feedback_markup(context, error_code, url),
            )
            if session_id:
                _cleanup_session_when_idle(session_id)
        except Exception as e:
            error_code = _make_error_code("youtube", _classify_internal_error_category("youtube", e))
            _schedule_platform_failure_log(
                platform="youtube",
                stage="process_url",
                url=url,
                error_code=error_code,
                exc=e,
                session_id=session_id,
            )
            await processing_message.edit_text(
                _build_public_error_message("youtube", error_code, e),
                reply_markup=feedback_markup(context, error_code, url),
            )
            if session_id:
                _cleanup_session_when_idle(session_id)
        return
    # Проверка TikTok
    if is_valid_tiktok_url(url):
        processing_message, session_token, session_id = await _begin_processing(
            update, context, url, "tiktok"
        )
        try:
            video_info = await run_blocking(
                get_tiktok_info, url, description="get_tiktok_info"
            )
            from utils.tiktok_instagram_utils import get_available_formats_tiktok

            formats = get_available_formats_tiktok(video_info)
            if not _finish_processing(context, session_token, video_info, formats):
                return
            text, reply_markup = _build_main_menu("tiktok", video_info, session_token)
            await processing_message.edit_text(
                text, reply_markup=reply_markup, parse_mode="Markdown"
            )
        except Exception as e:
            error_code = _make_error_code(
                "tiktok", _classify_internal_error_category("tiktok", e)
            )
            _schedule_platform_failure_log(
                platform="tiktok",
                stage="process_url",
                url=url,
                error_code=error_code,
                exc=e,
                session_id=session_id,
            )
            await processing_message.edit_text(
                _build_public_error_message("tiktok", error_code, e),
                reply_markup=feedback_markup(context, error_code, url),
            )
            if session_id:
                _cleanup_session_when_idle(session_id)
        return
    # Проверка Instagram Stories (не поддерживается)
    if is_instagram_story_url(url):
        await update.message.reply_text(
            "📛 Скачивание Instagram Stories не поддерживается.\n\n"
            "Stories — это временный контент (24 часа), и Instagram "
            "ограничивает их загрузку через API.\n\n"
            "Попробуйте скачать обычный пост, Reel или видео из IGTV."
        )
        return
    # Проверка Instagram аудио ссылок
    if is_instagram_audio_url(url):
        message_text = handle_instagram_audio_url(url)
        await update.message.reply_text(
            message_text, parse_mode="Markdown", disable_web_page_preview=True
        )
        return
    # Проверка Instagram
    if is_valid_instagram_url(url):
        processing_message, session_token, session_id = await _begin_processing(
            update, context, url, "instagram"
        )
        try:
            video_info = await run_blocking(
                get_instagram_info, url, description="get_instagram_info"
            )
            if not _finish_processing(context, session_token, video_info, {}):
                return
            text, reply_markup = _build_main_menu(
                "instagram", video_info, session_token
            )
            await processing_message.edit_text(
                text, reply_markup=reply_markup, parse_mode="Markdown"
            )
        except Exception as e:
            error_code = _make_error_code(
                "instagram", _classify_internal_error_category("instagram", e)
            )
            _schedule_platform_failure_log(
                platform="instagram",
                stage="process_url",
                url=url,
                error_code=error_code,
                exc=e,
                session_id=session_id,
            )
            await processing_message.edit_text(
                _build_public_error_message("instagram", error_code, e),
                reply_markup=feedback_markup(context, error_code, url),
            )
            if session_id:
                _cleanup_session_when_idle(session_id)
        return
    # Проверка Rutube
    if is_valid_rutube_url(url):
        processing_message, session_token, session_id = await _begin_processing(
            update, context, url, "rutube"
        )
        try:
            video_info = await run_blocking(
                functools.partial(get_rutube_info, url, session_id=session_id),
                description="get_rutube_info", session_id=session_id,
            )
            formats = get_available_formats_rutube(video_info)
            if not _finish_processing(context, session_token, video_info, formats):
                return
            text, reply_markup = _build_main_menu("rutube", video_info, session_token)
            await processing_message.edit_text(
                text, reply_markup=reply_markup, parse_mode="Markdown"
            )
        except Exception as e:
            error_code = _make_error_code(
                "rutube", _classify_internal_error_category("rutube", e)
            )
            _schedule_platform_failure_log(
                platform="rutube",
                stage="process_url",
                url=url,
                error_code=error_code,
                exc=e,
                session_id=session_id,
            )
            await processing_message.edit_text(
                _build_public_error_message("rutube", error_code, e),
                reply_markup=feedback_markup(context, error_code, url),
            )
            if session_id:
                _cleanup_session_when_idle(session_id)
        return
    # Проверка VK
    if is_valid_vk_url(url):
        processing_message, session_token, session_id = await _begin_processing(
            update, context, url, "vk"
        )
        try:
            video_info = await run_blocking(
                functools.partial(get_vk_info, url, session_id=session_id),
                description="get_vk_info", session_id=session_id,
            )
            formats = get_available_formats_vk(video_info)
            if not _finish_processing(context, session_token, video_info, formats):
                return
            text, reply_markup = _build_main_menu("vk", video_info, session_token)
            await processing_message.edit_text(
                text, reply_markup=reply_markup, parse_mode="Markdown"
            )
        except FileSizeLimitError:
            await processing_message.edit_text(
                VK_NO_SUITABLE_FORMAT_MESSAGE.format(
                    max_mb=MAX_FILE_SIZE // (1024 * 1024)
                )
            )
            await _cleanup_user_session(user_id, context, session_token)
        except VkLiveActiveError:
            await processing_message.edit_text(VK_LIVE_ACTIVE_MESSAGE)
            await _cleanup_user_session(user_id, context, session_token)
        except Exception as e:
            error_code = _make_error_code(
                "vk", _classify_internal_error_category("vk", e)
            )
            _schedule_platform_failure_log(
                platform="vk",
                stage="process_url",
                url=url,
                error_code=error_code,
                exc=e,
                session_id=session_id,
            )
            await processing_message.edit_text(
                _build_public_error_message("vk", error_code, e),
                reply_markup=feedback_markup(context, error_code, url),
            )
            if session_id:
                _cleanup_session_when_idle(session_id)
        return
    # Если не подходит ни один из вариантов
    await update.message.reply_text(INVALID_URL_MESSAGE)


async def _handle_main_callback(
    query: telegram.CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    session_token: str,
    action: str,
) -> None:
    """Новая версия обработчика main-callback с привязкой к токену сессии."""
    session_data = _get_session(context, session_token)
    if not session_data:
        await query.edit_message_text(SESSION_EXPIRED)
        return

    formats = session_data.get("formats", {})
    url = session_data["url"]
    session_id = session_data["session_id"]
    platform = session_data.get("platform", "youtube")
    if not _is_main_action_allowed(session_data, action):
        await safe_edit_message_text(query, SESSION_EXPIRED)
        return
    back_markup = _build_back_markup(session_token)
    # Пока идёт скачивание, отмена — единственное осмысленное действие.
    cancel_markup = _build_cancel_markup(session_token)

    match action:
        case "tiktok_download" | "tiktok_download_desc":
            if session_data.get("video_info", {}).get("_nuvio_tiktok_photo_post"):
                await _send_photo_post_assets(
                    query, session_token, session_data, context
                )
                return

            # Проверяем кэш перед скачиванием
            cache_key = _cache_format_id_for_main_action("tiktok", action)
            if cache_key:
                cached = await _cache_get(url, format_id=cache_key)
                if cached:
                    try:
                        await _deliver_social_cached_video(
                            query, cached, session_data
                        )
                        logger.info(
                            "TikTok видео доставлено из кэша (key=%s)", cache_key
                        )
                        await _record_delivery(query.from_user.id, session_data)
                        await _edit_delivery_status(
                            query,
                            DESCRIPTION_SEND_FAILED
                            if session_data.get("_description_delivery_failed")
                            else FILE_SENT
                        )
                        await _cleanup_user_session(user_id, context, session_token)
                        return
                    except telegram.error.BadRequest as e:
                        if not _is_stale_file_id(e):
                            raise
                        logger.warning("file_id устарел (key=%s): %s", cache_key, e)
                        await _cache_invalidate(cached.file_id)
                    except (telegram.error.NetworkError, telegram.error.TimedOut) as e:
                        session_data["_delivery_outcome_unknown"] = not _is_proven_not_sent(e)
                        raise

            await _edit_delivery_status(
                query, DOWNLOADING_MESSAGE, reply_markup=cancel_markup
            )
            from utils.tiktok_instagram_utils import (
                download_tiktok_video,
                resolve_tiktok_video_handoff,
            )

            # Ролик до 20 МБ Telegram забирает по ссылке сам — это дешевле, чем
            # скачать его к себе и выгрузить обратно. При отказе идём ниже
            # обычным путём: резолвер будет вызван повторно, но только в этом
            # редком случае.
            plan = await run_blocking(
                resolve_tiktok_video_handoff,
                url,
                description="resolve_tiktok_video_handoff",
                session_id=session_id,
            )
            if await _deliver_plan(
                query, context, session_token, session_data, plan, cache_key
            ):
                return

            try:
                file_path = await run_blocking(
                    download_tiktok_video,
                    url,
                    session_id,
                    None,
                    False,
                    session_data.get("video_info"),
                    description="download_tiktok_video",
                    session_id=session_id,
                )
                if not file_path:
                    await query.edit_message_text(ERROR_MESSAGE)
                    await _cleanup_user_session(user_id, context, session_token)
                    return
                await send_file(
                    query,
                    file_path,
                    session_token,
                    session_data,
                    context,
                    cache_format_id=_cache_format_id_for_main_action(
                        "tiktok", action
                    ),
                )
            except Exception as e:
                error_code = _make_error_code(
                    "tiktok", _classify_internal_error_category("tiktok", e)
                )
                _schedule_platform_failure_log(
                    platform="tiktok",
                    stage="download_video",
                    url=url,
                    error_code=error_code,
                    exc=e,
                    session_id=session_id,
                )
                await query.edit_message_text(
                    _build_public_error_message("tiktok", error_code, e),
                    reply_markup=feedback_markup(context, error_code, url),
                )
                await _cleanup_user_session(user_id, context, session_token)
            return

        case "tiktok_audio":
            cache_key = _cache_format_id_for_main_action("tiktok", "tiktok_audio")
            if cache_key and await _deliver_cached_audio(query, url, cache_key):
                await _record_delivery(query.from_user.id, session_data)
                await query.edit_message_text(FILE_SENT)
                await _cleanup_user_session(user_id, context, session_token)
                return

            await _edit_delivery_status(
                query, DOWNLOADING_AUDIO_MESSAGE, reply_markup=cancel_markup
            )
            from utils.tiktok_instagram_utils import (
                download_tiktok_audio,
                resolve_tiktok_audio_handoff,
            )

            plan = await run_blocking(
                resolve_tiktok_audio_handoff,
                url,
                description="resolve_tiktok_audio_handoff",
                session_id=session_id,
            )
            if await _deliver_plan(
                query, context, session_token, session_data, plan, cache_key
            ):
                return

            try:
                file_path = await run_blocking(
                    download_tiktok_audio,
                    url,
                    session_id,
                    None,
                    False,
                    session_data.get("video_info"),
                    description="download_tiktok_audio",
                    session_id=session_id,
                )
                if not file_path:
                    await query.edit_message_text(ERROR_MESSAGE)
                    await _cleanup_user_session(user_id, context, session_token)
                    return
                await send_file(
                    query,
                    file_path,
                    session_token,
                    session_data,
                    context,
                    cache_format_id=cache_key,
                )
            except PhotoPostAudioMissingError:
                await query.edit_message_text(
                    PHOTO_POST_AUDIO_UNAVAILABLE, reply_markup=back_markup
                )
                await _cleanup_user_session(user_id, context, session_token)
            except Exception as e:
                error_code = _make_error_code(
                    "tiktok", _classify_internal_error_category("tiktok", e)
                )
                _schedule_platform_failure_log(
                    platform="tiktok",
                    stage="download_audio",
                    url=url,
                    error_code=error_code,
                    exc=e,
                    session_id=session_id,
                )
                await query.edit_message_text(
                    _build_public_error_message("tiktok", error_code, e),
                    reply_markup=feedback_markup(context, error_code, url),
                )
                await _cleanup_user_session(user_id, context, session_token)
            return

        case "instagram_download" | "instagram_download_desc":
            if session_data.get("video_info", {}).get(
                "_nuvio_instagram_carousel_incomplete"
            ) or (
                session_data.get("video_info", {}).get("_nuvio_instagram_mixed_post")
                and not session_data.get("video_info", {}).get(
                    "_nuvio_instagram_carousel_complete"
                )
            ):
                await query.edit_message_text(INSTAGRAM_CAROUSEL_INCOMPLETE)
                await _cleanup_user_session(user_id, context, session_token)
                return
            if session_data.get("video_info", {}).get("_nuvio_instagram_photo_post"):
                await _send_photo_post_assets(
                    query, session_token, session_data, context
                )
                return

            # Проверяем кэш перед скачиванием
            cache_key = _cache_format_id_for_main_action("instagram", action)
            if cache_key:
                cached = await _cache_get(url, format_id=cache_key)
                if cached:
                    try:
                        await _deliver_social_cached_video(
                            query, cached, session_data
                        )
                        logger.info(
                            "Instagram видео доставлено из кэша (key=%s)", cache_key
                        )
                        await _record_delivery(query.from_user.id, session_data)
                        await _edit_delivery_status(
                            query,
                            DESCRIPTION_SEND_FAILED
                            if session_data.get("_description_delivery_failed")
                            else FILE_SENT
                        )
                        await _cleanup_user_session(user_id, context, session_token)
                        return
                    except telegram.error.BadRequest as e:
                        if not _is_stale_file_id(e):
                            raise
                        logger.warning("file_id устарел (key=%s): %s", cache_key, e)
                        await _cache_invalidate(cached.file_id)
                    except (telegram.error.NetworkError, telegram.error.TimedOut) as e:
                        session_data["_delivery_outcome_unknown"] = not _is_proven_not_sent(e)
                        raise

            await _edit_delivery_status(
                query, DOWNLOADING_MESSAGE, reply_markup=cancel_markup
            )
            from utils.tiktok_instagram_utils import (
                download_instagram_video,
                resolve_instagram_video_handoff,
            )

            plan = await run_blocking(
                resolve_instagram_video_handoff,
                url,
                description="resolve_instagram_video_handoff",
                session_id=session_id,
            )
            if await _deliver_plan(
                query, context, session_token, session_data, plan, cache_key
            ):
                return

            try:
                file_path = await run_blocking(
                    download_instagram_video,
                    url,
                    session_id,
                    None,
                    False,
                    session_data.get("video_info"),
                    description="download_instagram_video",
                    session_id=session_id,
                )
                if not file_path:
                    await query.edit_message_text(ERROR_MESSAGE)
                    await _cleanup_user_session(user_id, context, session_token)
                    return
                await send_file(
                    query,
                    file_path,
                    session_token,
                    session_data,
                    context,
                    cache_format_id=_cache_format_id_for_main_action(
                        "instagram", action
                    ),
                )
            except Exception as e:
                if "фото-пост нужно отправлять" in str(e).lower():
                    await _send_photo_post_assets(
                        query, session_token, session_data, context
                    )
                    return
                error_code = _make_error_code(
                    "instagram", _classify_internal_error_category("instagram", e)
                )
                _schedule_platform_failure_log(
                    platform="instagram",
                    stage="download_video",
                    url=url,
                    error_code=error_code,
                    exc=e,
                    session_id=session_id,
                )
                await query.edit_message_text(
                    _build_public_error_message("instagram", error_code, e),
                    reply_markup=feedback_markup(context, error_code, url),
                )
                await _cleanup_user_session(user_id, context, session_token)
            return

        case "instagram_audio":
            cache_key = _cache_format_id_for_main_action(
                "instagram", "instagram_audio"
            )
            if cache_key and await _deliver_cached_audio(query, url, cache_key):
                await _record_delivery(query.from_user.id, session_data)
                await query.edit_message_text(FILE_SENT)
                await _cleanup_user_session(user_id, context, session_token)
                return

            await _edit_delivery_status(
                query, DOWNLOADING_AUDIO_MESSAGE, reply_markup=cancel_markup
            )
            from utils.tiktok_instagram_utils import download_instagram_audio

            try:
                file_path = await run_blocking(
                    download_instagram_audio,
                    url,
                    session_id,
                    None,
                    False,
                    session_data.get("video_info"),
                    description="download_instagram_audio",
                    session_id=session_id,
                )
                if not file_path:
                    await query.edit_message_text(ERROR_MESSAGE)
                    await _cleanup_user_session(user_id, context, session_token)
                    return
                await send_file(
                    query,
                    file_path,
                    session_token,
                    session_data,
                    context,
                    cache_format_id=cache_key,
                )
            except PhotoPostAudioMissingError:
                await query.edit_message_text(
                    PHOTO_POST_AUDIO_UNAVAILABLE, reply_markup=back_markup
                )
                await _cleanup_user_session(user_id, context, session_token)
            except Exception as e:
                error_code = _make_error_code(
                    "instagram", _classify_internal_error_category("instagram", e)
                )
                _schedule_platform_failure_log(
                    platform="instagram",
                    stage="download_audio",
                    url=url,
                    error_code=error_code,
                    exc=e,
                    session_id=session_id,
                )
                await query.edit_message_text(
                    _build_public_error_message("instagram", error_code, e),
                    reply_markup=feedback_markup(context, error_code, url),
                )
                await _cleanup_user_session(user_id, context, session_token)
            return

        case "rutube_download":
            cache_key = _cache_format_id_for_main_action("rutube", "rutube_download")
            if cache_key:
                cached = await _cache_get(url, format_id=cache_key)
                if cached:
                    try:
                        await _call_telegram_with_retry_after(
                            lambda: _reply_cached(query, cached),
                            session_data,
                        )
                        logger.info(
                            "Rutube видео доставлено из кэша (key=%s)", cache_key
                        )
                        await _record_delivery(query.from_user.id, session_data)
                        await query.edit_message_text(FILE_SENT)
                        await _cleanup_user_session(user_id, context, session_token)
                        return
                    except telegram.error.BadRequest as e:
                        if not _is_stale_file_id(e):
                            raise
                        logger.warning("file_id устарел (key=%s): %s", cache_key, e)
                        await _cache_invalidate(cached.file_id)

            await safe_edit_message_text(
                query, DOWNLOADING_MESSAGE, reply_markup=cancel_markup
            )
            try:
                file_path = await run_blocking(
                    download_rutube_video,
                    url,
                    session_id,
                    description="download_rutube_video",
                    session_id=session_id,
                )
                if not file_path:
                    await query.edit_message_text(ERROR_MESSAGE)
                    await _cleanup_user_session(user_id, context, session_token)
                    return
                await send_file(
                    query,
                    file_path,
                    session_token,
                    session_data,
                    context,
                    cache_format_id=_cache_format_id_for_main_action(
                        "rutube", "rutube_download"
                    ),
                )
            except Exception as e:
                error_code = _make_error_code(
                    "rutube", _classify_internal_error_category("rutube", e)
                )
                _schedule_platform_failure_log(
                    platform="rutube",
                    stage="download_video",
                    url=url,
                    error_code=error_code,
                    exc=e,
                    session_id=session_id,
                )
                await query.edit_message_text(
                    _build_public_error_message("rutube", error_code, e),
                    reply_markup=feedback_markup(context, error_code, url),
                )
                await _cleanup_user_session(user_id, context, session_token)
            return

        case "rutube_audio":
            cache_key = _cache_format_id_for_main_action("rutube", "rutube_audio")
            if cache_key and await _deliver_cached_audio(query, url, cache_key):
                await _record_delivery(query.from_user.id, session_data)
                await query.edit_message_text(FILE_SENT)
                await _cleanup_user_session(user_id, context, session_token)
                return

            await safe_edit_message_text(
                query, DOWNLOADING_AUDIO_MESSAGE, reply_markup=cancel_markup
            )
            try:
                file_path = await run_blocking(
                    download_rutube_audio,
                    url,
                    session_id,
                    description="download_rutube_audio",
                    session_id=session_id,
                )
                if not file_path:
                    await query.edit_message_text(ERROR_MESSAGE)
                    await _cleanup_user_session(user_id, context, session_token)
                    return
                await send_file(
                    query,
                    file_path,
                    session_token,
                    session_data,
                    context,
                    cache_format_id=cache_key,
                )
            except Exception as e:
                error_code = _make_error_code(
                    "rutube", _classify_internal_error_category("rutube", e)
                )
                _schedule_platform_failure_log(
                    platform="rutube",
                    stage="download_audio",
                    url=url,
                    error_code=error_code,
                    exc=e,
                    session_id=session_id,
                )
                await query.edit_message_text(
                    _build_public_error_message("rutube", error_code, e),
                    reply_markup=feedback_markup(context, error_code, url),
                )
                await _cleanup_user_session(user_id, context, session_token)
            return

        case "vk_download":
            vk_info = session_data.get("video_info") or {}
            selected_format = vk_info.get("_nuvio_vk_format_id")
            selected_size = vk_info.get("_nuvio_vk_format_size")
            cache_key = _cache_format_id_for_main_action("vk", "vk_download")
            if cache_key and selected_format:
                cache_key = f"{cache_key}:{selected_format}"
            if cache_key:
                cached = await _cache_get(url, format_id=cache_key)
                if cached:
                    try:
                        await _call_telegram_with_retry_after(
                            lambda: _reply_cached(query, cached),
                            session_data,
                        )
                        logger.info("VK видео доставлено из кэша (key=%s)", cache_key)
                        await _record_delivery(query.from_user.id, session_data)
                        await query.edit_message_text(FILE_SENT)
                        await _cleanup_user_session(user_id, context, session_token)
                        return
                    except telegram.error.BadRequest as e:
                        if not _is_stale_file_id(e):
                            raise
                        logger.warning("file_id устарел (key=%s): %s", cache_key, e)
                        await _cache_invalidate(cached.file_id)

            await safe_edit_message_text(
                query, DOWNLOADING_MESSAGE, reply_markup=cancel_markup
            )
            try:
                file_path = await run_blocking(
                    functools.partial(download_vk_video, format_id=selected_format),
                    url,
                    session_id,
                    description="download_vk_video",
                    session_id=session_id,
                    timeout=(
                        max(
                            BLOCKING_TASK_TIMEOUT,
                            min(3600, selected_size // (1024 * 1024) + 120),
                        )
                        if selected_size else None
                    ),
                )
                if not file_path:
                    await query.edit_message_text(ERROR_MESSAGE)
                    await _cleanup_user_session(user_id, context, session_token)
                    return
                await send_file(
                    query,
                    file_path,
                    session_token,
                    session_data,
                    context,
                    cache_format_id=cache_key,
                )
            except Exception as e:
                error_code = _make_error_code(
                    "vk", _classify_internal_error_category("vk", e)
                )
                _schedule_platform_failure_log(
                    platform="vk",
                    stage="download_video",
                    url=url,
                    error_code=error_code,
                    exc=e,
                    session_id=session_id,
                )
                await query.edit_message_text(
                    _build_public_error_message("vk", error_code, e),
                    reply_markup=feedback_markup(context, error_code, url),
                )
                await _cleanup_user_session(user_id, context, session_token)
            return

        case "vk_audio":
            cache_key = _cache_format_id_for_main_action("vk", "vk_audio")
            if cache_key and await _deliver_cached_audio(query, url, cache_key):
                await _record_delivery(query.from_user.id, session_data)
                await query.edit_message_text(FILE_SENT)
                await _cleanup_user_session(user_id, context, session_token)
                return

            await safe_edit_message_text(
                query, DOWNLOADING_AUDIO_MESSAGE, reply_markup=cancel_markup
            )
            try:
                file_path = await run_blocking(
                    download_vk_audio,
                    url,
                    session_id,
                    description="download_vk_audio",
                    session_id=session_id,
                )
                if not file_path:
                    await query.edit_message_text(ERROR_MESSAGE)
                    await _cleanup_user_session(user_id, context, session_token)
                    return
                await send_file(
                    query,
                    file_path,
                    session_token,
                    session_data,
                    context,
                    cache_format_id=cache_key,
                )
            except Exception as e:
                error_code = _make_error_code(
                    "vk", _classify_internal_error_category("vk", e)
                )
                _schedule_platform_failure_log(
                    platform="vk",
                    stage="download_audio",
                    url=url,
                    error_code=error_code,
                    exc=e,
                    session_id=session_id,
                )
                await query.edit_message_text(
                    _build_public_error_message("vk", error_code, e),
                    reply_markup=feedback_markup(context, error_code, url),
                )
                await _cleanup_user_session(user_id, context, session_token)
            return

        case "audio_m4a":
            cache_key = _cache_format_id_for_main_action("youtube", "audio_m4a")
            if cache_key and await _deliver_cached_audio(query, url, cache_key):
                await _record_delivery(query.from_user.id, session_data)
                await query.edit_message_text(FILE_SENT)
                await _cleanup_user_session(user_id, context, session_token)
                return

            audio_only = formats.get("audio_only", [])
            ranked_audio = sorted(
                audio_only,
                key=lambda fmt: (
                    audio_language_rank(fmt),
                    -(fmt.get("filesize") or 0),
                ),
            )
            native_audio = None
            best_language_rank = (
                audio_language_rank(ranked_audio[0]) if ranked_audio else None
            )
            for ext in ("m4a", "mp3", "ogg"):
                native_audio = next(
                    (
                        fmt for fmt in ranked_audio
                        if fmt.get("ext") == ext
                        and audio_language_rank(fmt) == best_language_rank
                    ),
                    None,
                )
                if native_audio:
                    logger.info(f"Найден нативный аудио формат: {ext}")
                    break

            if not native_audio and ranked_audio:
                logger.warning(
                    "Нативные форматы не найдены. Доступные: %s. Конвертируем в m4a.",
                    [f.get("ext") for f in audio_only],
                )
                await safe_edit_message_text(
                query, DOWNLOADING_AUDIO_MESSAGE, reply_markup=cancel_markup
            )
                file_path = await run_blocking(
                    functools.partial(download_audio, preferred_codec="m4a"),
                    url,
                    ranked_audio[0]["format_id"],
                    session_id,
                    description="download_audio_bestaudio",
                    session_id=session_id,
                )
            elif native_audio:
                await safe_edit_message_text(
                query, DOWNLOADING_AUDIO_MESSAGE, reply_markup=cancel_markup
            )
                file_path = await run_blocking(
                    download_audio_native,
                    url,
                    native_audio["format_id"],
                    session_id,
                    description="download_audio_native",
                    session_id=session_id,
                )
            else:
                await query.edit_message_text(ERROR_MESSAGE)
                await _cleanup_user_session(user_id, context, session_token)
                return

            if not file_path:
                await query.edit_message_text(ERROR_MESSAGE)
                await _cleanup_user_session(user_id, context, session_token)
                return

            await send_file(
                query,
                file_path,
                session_token,
                session_data,
                context,
                cache_format_id=cache_key,
            )
            return

        case "tg_video":
            cache_key = _cache_format_id_for_main_action("youtube", "tg_video")
            if cache_key and await _deliver_cached_video(query, url, cache_key):
                await _record_delivery(query.from_user.id, session_data)
                await query.edit_message_text(FILE_SENT)
                await _cleanup_user_session(user_id, context, session_token)
                return

            combined = formats.get("combined", [])
            tg_video = None

            logger.info("Доступные combined форматы для tg_video:")
            for i, fmt in enumerate(combined):
                logger.info(
                    "  %s: %s - %sp - %s - размер: %s байт",
                    i,
                    fmt.get("format_id"),
                    fmt.get("height"),
                    fmt.get("ext"),
                    fmt.get("filesize"),
                )

            video_only = formats.get("video_only", [])
            audio_only = formats.get("audio_only", [])
            # Бюджет берётся из режима доставки: 2000 МБ через локальный Bot API,
            # 50 МБ через облачный. Прежние 35 + 15 МБ были захардкожены под
            # облачный лимит и обесценивали локальный сервер.
            choice = select_tg_video_format(
                video_only, audio_only, combined, MAX_FILE_SIZE
            )

            if choice:
                tg_video = {
                    "format_id": choice.format_id,
                    "height": choice.height,
                    "ext": choice.ext,
                    "type": choice.kind,
                }
                logger.info(
                    "Выбран формат для Telegram: %s - %sp - %.1f МБ из бюджета %.0f МБ",
                    choice.format_id,
                    choice.height,
                    choice.total_size / 1024 / 1024,
                    MAX_FILE_SIZE / 1024 / 1024,
                )

                await safe_edit_message_text(
                query, DOWNLOADING_MESSAGE, reply_markup=cancel_markup
            )

                # Готовый файл до 20 МБ Telegram забирает по ссылке сам.
                # Пары «видео + аудио» так отдать нельзя: их ещё нужно склеить
                # FFmpeg, поэтому ссылка ищется только для combined-формата.
                if choice.kind == "combined":
                    plan = plan_url_handoff(
                        find_format_url(
                            session_data.get("video_info"), choice.format_id
                        ),
                        "video",
                        choice.total_size,
                        geometry=find_format_geometry(
                            session_data.get("video_info"), choice.format_id
                        ),
                    )
                    if await _deliver_plan(
                        query,
                        context,
                        session_token,
                        session_data,
                        plan,
                        _cache_format_id_for_main_action("youtube", "tg_video"),
                    ):
                        return

                try:
                    file_path = await download_content(
                        url, tg_video["format_id"], session_id, "combined"
                    )
                except Exception as e:
                    error_code = _youtube_error_code(e)
                    logger.warning(
                        "YT_DL_STAGE_FAIL code=%s stage=tg_video_manual_combined format_id=%s url=%s error=%s",
                        error_code,
                        tg_video["format_id"],
                        url,
                        e,
                        exc_info=True,
                    )
                    file_path = None

                if file_path:
                    await send_file(
                        query,
                        file_path,
                        session_token,
                        session_data,
                        context,
                        cache_format_id=_cache_format_id_for_main_action(
                            "youtube", "tg_video"
                        ),
                    )
                    return
                tg_video = None

            if not tg_video:
                # Форматы без известного размера сверить с бюджетом нельзя,
                # поэтому select_tg_video_format их не рассматривает. Берём
                # осторожно — из нижней трети по высоте, чтобы скачанное с
                # большей вероятностью прошло по лимиту доставки.
                formats_without_size = [
                    fmt for fmt in combined if fmt.get("filesize") is None
                ]
                if formats_without_size:
                    best_rank = min(
                        audio_language_rank(fmt) for fmt in formats_without_size
                    )
                    formats_without_size = [
                        fmt for fmt in formats_without_size
                        if audio_language_rank(fmt) == best_rank
                    ]
                    formats_without_size.sort(key=lambda x: x.get("height", 0))
                    tg_video = formats_without_size[len(formats_without_size) // 3]
                    logger.info(
                        "Выбран резервный формат (размер неизвестен): %s - %sp",
                        tg_video.get("format_id"),
                        tg_video.get("height"),
                    )

            if tg_video:
                await safe_edit_message_text(
                query, DOWNLOADING_MESSAGE, reply_markup=cancel_markup
            )
                file_path = await download_content(
                    url, tg_video["format_id"], session_id, "combined"
                )
                if not file_path:
                    await query.edit_message_text(ERROR_MESSAGE)
                    await _cleanup_user_session(user_id, context, session_token)
                    return
                await send_file(
                    query,
                    file_path,
                    session_token,
                    session_data,
                    context,
                    cache_format_id=_cache_format_id_for_main_action(
                        "youtube", "tg_video"
                    ),
                )
                return

            if any(fmt.get("filesize") is None for fmt in combined):
                await safe_edit_message_text(
                    query, NO_FILESIZE, reply_markup=back_markup
                )
            else:
                await safe_edit_message_text(
                    query, NO_TG_VIDEO, reply_markup=back_markup
                )
            return

        case "more":
            await safe_edit_message_text(
                query,
                CHOOSE_SECTION_MESSAGE,
                reply_markup=_build_more_menu(formats, session_token),
            )
            return

        case "video_menu":
            markup = _build_video_menu(formats, session_token)
            if markup is None:
                await safe_edit_message_text(
                    query, NO_VIDEO_OPTIONS_MESSAGE, reply_markup=back_markup
                )
                return
            await safe_edit_message_text(
                query, CHOOSE_RESOLUTION_MESSAGE, reply_markup=markup
            )
            return

        case "audio_menu":
            markup = _build_audio_menu(formats, session_token)
            if markup is None:
                await safe_edit_message_text(
                    query, NO_AUDIO_OPTIONS_MESSAGE, reply_markup=back_markup
                )
                return
            await safe_edit_message_text(
                query, CHOOSE_AUDIO_MESSAGE, reply_markup=markup
            )
            return

        case "subtitles":
            markup = _build_subtitle_language_menu(
                session_data.get("video_info") or {}, session_token
            )
            if markup is None:
                await safe_edit_message_text(
                    query, NO_SUBTITLE_LANGUAGES_MESSAGE, reply_markup=back_markup
                )
                return
            await safe_edit_message_text(
                query, CHOOSE_SUBTITLE_LANGUAGE_MESSAGE, reply_markup=markup
            )
            return

        case "cancel":
            # Отмена обязана останавливать саму работу, а не прятать результат:
            # признак читает progress hook yt-dlp и прерывает загрузку.
            request_cancellation(session_id)
            if session_data.get("_delivery_request_in_flight"):
                cancel_text = CANCEL_IN_FLIGHT_MESSAGE
            elif session_data.get("_delivery_progress", 0):
                cancel_text = PARTIAL_CANCELLED
            else:
                cancel_text = CANCELLED_MESSAGE
            await safe_edit_message_text(query, cancel_text)
            await _cleanup_user_session(user_id, context, session_token)
            return

        case "back":
            text, reply_markup = _build_main_menu(
                platform, session_data["video_info"], session_token, formats
            )
            await safe_edit_message_text(
                query,
                text,
                reply_markup=reply_markup,
                parse_mode="Markdown",
            )
            return

        case _:
            await query.edit_message_text(ERROR_MESSAGE)
            await _cleanup_user_session(user_id, context, session_token)
            return


async def _download_and_send_subtitles(
    query: telegram.CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    session_token: str,
    session_data: dict,
    choice: str,
) -> None:
    """Скачивает субтитры выбранного языка и формата и отправляет их."""
    back_markup = _build_back_markup(session_token)
    parsed = parse_subtitle_choice(choice)
    if not parsed:
        # Значение пришло из callback_data, то есть от пользователя: молча
        # доверять ему нельзя.
        logger.warning("Некорректный выбор субтитров: %s", choice)
        await safe_edit_message_text(query, ERROR_MESSAGE, reply_markup=back_markup)
        return

    language, subtitle_format = parsed
    await safe_edit_message_text(
        query,
        DOWNLOADING_SUBTITLES_MESSAGE,
        reply_markup=_build_cancel_markup(session_token),
    )
    try:
        subtitle_file = await run_blocking(
            download_subtitles,
            session_data["url"],
            session_data["session_id"],
            language,
            subtitle_format,
            description="download_subtitles",
            session_id=session_data["session_id"],
        )
    except Exception as e:
        e.add_note(f"url={session_data['url']}, session_id={session_data['session_id']}")
        error_code = _make_error_code_for_exception("youtube", e)
        _schedule_platform_failure_log(
            platform="youtube", stage="download_subtitles", url=session_data["url"],
            error_code=error_code, exc=e, session_id=session_data["session_id"],
        )
        await safe_edit_message_text(
            query, _build_public_error_message("youtube", error_code, e),
            reply_markup=feedback_markup(context, error_code, session_data["url"], back_markup),
        )
        return

    if not (subtitle_file and subtitle_file.exists()):
        if subtitle_file:
            raise FileNotFoundError("Скачанный файл субтитров отсутствует")
        await safe_edit_message_text(
            query, NO_SUBTITLES_AVAILABLE, reply_markup=back_markup
        )
        return

    await safe_edit_message_text(query, FILE_PREPARING)
    with open(subtitle_file, "rb") as handle:
        await _call_telegram_with_retry_after(
            lambda: query.message.reply_document(
                document=handle,
                caption=f"{SUBTITLE_CAPTION} · {language.upper()} · {subtitle_format.upper()}",
            ),
            session_data,
            reset_files=lambda: handle.seek(0),
        )
    await safe_edit_message_text(query, FILE_SENT)
    subtitle_file.unlink(missing_ok=True)
    await _cleanup_user_session(user_id, context, session_token)


async def _handle_format_callback(
    query: telegram.CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    session_token: str,
    content_type: str,
    format_id: str,
) -> None:
    """Новая версия обработчика format-callback с привязкой к токену сессии."""
    session_data = _get_session(context, session_token)
    if not session_data:
        await query.edit_message_text(SESSION_EXPIRED)
        return

    url = session_data["url"]
    session_id = session_data["session_id"]
    formats = session_data.get("formats", {})
    cancel_markup = _build_cancel_markup(session_token)

    if not _is_format_selection_allowed(session_data, content_type, format_id):
        await safe_edit_message_text(query, SESSION_EXPIRED)
        return

    if content_type == "subs_lang":
        await safe_edit_message_text(
            query,
            CHOOSE_SUBTITLE_FORMAT_MESSAGE,
            reply_markup=_build_subtitle_format_menu(format_id, session_token),
        )
        return

    if content_type == "subs":
        await _download_and_send_subtitles(
            query, context, user_id, session_token, session_data, format_id
        )
        return

    if content_type == "audio_only":
        await safe_edit_message_text(
            query, DOWNLOADING_AUDIO_MESSAGE, reply_markup=cancel_markup
        )
    else:
        await safe_edit_message_text(
            query, DOWNLOADING_MESSAGE, reply_markup=cancel_markup
        )

    try:
        file_path = None
        cache_format_id = _cache_format_id_for_format_selection(content_type, format_id)
        if cache_format_id and await _deliver_cached_video(
            query, url, cache_format_id
        ):
            await _record_delivery(query.from_user.id, session_data)
            await query.edit_message_text(FILE_SENT)
            await _cleanup_user_session(user_id, context, session_token)
            return

        match content_type:
            case "combined":
                selected = next(
                    (
                        fmt
                        for fmt in formats.get("combined", [])
                        if str(fmt.get("format_id")) == format_id
                    ),
                    None,
                )
                plan = plan_url_handoff(
                    find_format_url(session_data.get("video_info"), format_id),
                    "video",
                    (selected or {}).get("filesize"),
                    geometry=find_format_geometry(
                        session_data.get("video_info"), format_id
                    ),
                )
                if await _deliver_plan(
                    query, context, session_token, session_data, plan, cache_format_id
                ):
                    return

                file_path = await download_content(
                    url, format_id, session_id, "combined"
                )
            case "audio_only":
                file_path = await download_content(
                    url, format_id, session_id, "audio_only"
                )

        if not file_path:
            await query.edit_message_text(ERROR_MESSAGE)
            await _cleanup_user_session(user_id, context, session_token)
            return

        await send_file(
            query,
            file_path,
            session_token,
            session_data,
            context,
            cache_format_id=cache_format_id,
        )
    except Exception as e:
        e.add_note(f"user_id={user_id}, url={url}, session_id={session_id}")
        error_code = _make_error_code(
            "youtube", _classify_internal_error_category("youtube", e)
        )
        _schedule_platform_failure_log(
            platform="youtube",
            stage="format_download",
            url=url,
            error_code=error_code,
            exc=e,
            session_id=session_id,
        )
        await query.edit_message_text(
            DELIVERY_OUTCOME_UNKNOWN
            if session_data.get("_delivery_outcome_unknown")
            else _build_public_error_message("youtube", error_code, e),
            reply_markup=feedback_markup(context, error_code, url),
        )
        await _cleanup_user_session(user_id, context, session_token)


async def _dispatch_session_callback(
    query, context, user_id: int, event: CallbackEvent,
    session_data: dict | None, expensive: bool,
) -> None:
    """Захватывает сессию для всех загрузок, сохраняя доступность отмены."""
    if session_data is None:
        await safe_edit_message_text(query, SESSION_EXPIRED)
        return
    allowed = (
        _is_main_action_allowed(session_data, event.action)
        if event.scope == "main"
        else bool(event.value) and _is_format_selection_allowed(
            session_data, event.action, event.value
        )
    )
    if not allowed:
        await safe_edit_message_text(query, SESSION_EXPIRED)
        return
    if session_data.get("_delivery_active") and event.action != "cancel":
        return
    action_key = (
        event.action if event.scope == "main"
        else f"format|{event.action}|{event.value}"
    )
    if expensive and session_data.get("_delivery_outcome_unknown"):
        await safe_edit_message_text(query, DELIVERY_OUTCOME_UNKNOWN)
        return
    if session_data.get("_delivered_items") and action_key not in {
        "cancel", "back", session_data.get("_delivery_action")
    }:
        await safe_edit_message_text(query, PHOTO_POST_PARTIAL)
        return
    delivery_session_id = None
    if expensive:
        session_data["_delivery_active"] = True
        session_data["_delivery_action"] = action_key
        session_data["_include_description"] = event.action.endswith("_desc")
        if not session_data.get("_delivered_items"):
            session_data.pop("_description_delivery_failed", None)
            session_data.pop("_description_attempted", None)
        session_data["_delivery_progress"] = int(session_data.get("_delivered_items") or 0)
        session_data["_delivery_request_in_flight"] = False
        delivery_session_id = session_data.get("session_id")
        bot = getattr(context, "bot", None)
        try:
            bot_id = getattr(bot, "id", None)
        except RuntimeError:
            bot_id = None
        if isinstance(bot_id, int):
            session_data["_bot_id"] = bot_id
            session_data["_recipient_id"] = user_id
            session_data["_operation_id"] = f"{delivery_session_id}:{action_key}"
            if await run_db(journal.uncertain, session_data["_operation_id"]):
                session_data["_delivery_outcome_unknown"] = True
                session_data.pop("_delivery_active", None)
                await safe_edit_message_text(query, DELIVERY_OUTCOME_UNKNOWN)
                return
        if delivery_session_id:
            _delivery_started(delivery_session_id)
    delivery_context_token = _active_delivery_session.set(
        session_data if expensive else None
    )
    try:
        async with _pulsing_chat_action(
            query.message.chat, _chat_action_for(event.action), expensive
        ):
            if event.scope == "main":
                await _handle_main_callback(
                    query, context, user_id, event.session_token, event.action
                )
            else:
                await _handle_format_callback(
                    query, context, user_id, event.session_token, event.action, event.value
                )
    finally:
        _active_delivery_session.reset(delivery_context_token)
        if expensive:
            session_data.pop("_delivery_active", None)
            session_data.pop("_include_description", None)
            if delivery_session_id:
                _delivery_finished(delivery_session_id)


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Новая версия callback-обработчика с независимыми пользовательскими сессиями."""
    query = update.callback_query
    if not query or not query.data:
        return

    user_id = update.effective_user.id
    session_token: str | None = None
    now = asyncio.get_running_loop().time()

    expensive = _should_rate_limit_callback(query.data)
    if expensive and _check_spam(user_id, context, now):
        await answer_callback(query, text=SPAM_WARNING, show_alert=False)
        return

    await answer_callback(query)

    logger.info(f"Получен колбэк от пользователя {user_id}: {query.data}")

    active_session: dict | None = None
    try:
        event = CallbackEvent.parse(query.data)
        if event and event.scope in {"main", "format"} and event.session_token:
            session_token = event.session_token
            active_session = _get_session(context, session_token)
            await _dispatch_session_callback(
                query, context, user_id, event, active_session, expensive
            )
        elif event and event.scope == "csi" and event.value:
            rating = int(event.value)
            chat_id = query.message.chat.id
            message_id = query.message.message_id
            if not event.session_token:
                await safe_edit_message_text(query, CSI_SURVEY_EXPIRED)
                return
            poll_id = event.session_token
            try:
                csi_id, created = await run_db(save_csi_vote, user_id, rating, poll_id, chat_id, message_id)
            except ValueError:
                await answer_callback(query, CSI_SURVEY_EXPIRED)
                return
            if not created:
                await safe_edit_message_text(query, CSI_THANKS_MESSAGE)
                return
            await safe_edit_message_text(query, CSI_THANKS_MESSAGE)
            if rating < 7:
                context.user_data["awaiting_csi_feedback_id"] = csi_id
                await context.bot.send_message(
                    chat_id=user_id,
                    text=CSI_FEEDBACK_REQUEST,
                )
        elif query.data.startswith("csi|"):
            try:
                rating = int(query.data.split("|", maxsplit=1)[1])
                await answer_callback(
                    query,
                    CSI_RATING_OUT_OF_RANGE
                    if not 0 <= rating <= 10
                    else CSI_INVALID_RATING
                )
            except ValueError:
                await answer_callback(query, CSI_INVALID_RATING)
        else:
            await safe_edit_message_text(query, SESSION_EXPIRED)
    except CancelledByUser:
        # Не ошибка, а сигнал управления: сообщение об отмене пользователь уже
        # видит, работа прервана в самом загрузчике.
        logger.info("Задача прервана пользователем")
        if session_token:
            await _cleanup_user_session(user_id, context, session_token)
        return
    except Exception as e:
        failure_session = active_session or _get_session(context, session_token) or {}
        platform = failure_session.get("platform", "bot")
        error_code = _make_error_code_for_exception(platform, e)
        _schedule_platform_failure_log(
            platform=platform,
            stage="button_callback",
            url=failure_session.get("url"),
            error_code=error_code,
            exc=e,
            session_id=_session_id_of(context, session_token),
        )
        text = (
            DELIVERY_OUTCOME_UNKNOWN
            if failure_session.get("_delivery_outcome_unknown")
            else _build_public_error_message(platform, error_code, e)
        )
        try:
            await safe_edit_message_text(query, text, parse_mode=None, reply_markup=feedback_markup(context, error_code, failure_session.get("url")))
        except Exception:
            await safe_edit_message_text(query, ERROR_FALLBACK)

        if session_token:
            await _cleanup_user_session(user_id, context, session_token)


async def download_content(
    url: str, format_id: str, session_id: str, content_type: str
) -> Path | str | None:
    """
    Скачивает контент в зависимости от типа.
    Все блокирующие вызовы выполняются через run_blocking.
    """
    try:
        if "+" in format_id and content_type == "combined":
            logger.info(f"Обнаружен комбинированный формат: {format_id}")
            return await run_blocking(
                download_video,
                url,
                format_id,
                session_id,
                description="download_video_combined",
                session_id=session_id,
            )
        if content_type == "combined":
            return await run_blocking(
                download_video,
                url,
                format_id,
                session_id,
                description="download_video_combined_simple",
                session_id=session_id,
            )
        if content_type == "audio_only":
            return await run_blocking(
                download_audio_native,
                url,
                format_id,
                session_id,
                description="download_audio_native_format",
                session_id=session_id,
            )
        raise ValueError(f"Неподдерживаемый content_type: {content_type}")
    except Exception as e:
        e.add_note(
            f"url={url}, format_id={format_id}, session_id={session_id}, content_type={content_type}"
        )
        error_code = _youtube_error_code(e)
        logger.error(
            "YT_DL_FAIL code=%s stage=download_content content_type=%s format_id=%s url=%s error=%s",
            error_code,
            content_type,
            format_id,
            url,
            e,
            exc_info=True,
        )
        raise


async def send_file(
    query: telegram.CallbackQuery,
    file_path: Path,
    session_token: str,
    session_data: dict,
    context: ContextTypes.DEFAULT_TYPE,
    cache_format_id: str | None = None,
) -> None:
    """Новая версия отправки файла, привязанная к конкретной сессии."""
    user_id = query.from_user.id
    back_markup = _build_back_markup(session_token)
    platform = session_data.get("platform", "bot")
    url = session_data.get("url")
    success = False
    try:
        await _edit_delivery_status(query, FILE_PREPARING)
        success = await send_single_file(
            query,
            file_path,
            session_token,
            session_data,
            cache_format_id=cache_format_id,
            feedback_context=context,
        )
        if success:
            await _record_delivery(user_id, session_data)
            await _edit_delivery_status(
                query,
                DESCRIPTION_SEND_FAILED
                if session_data.get("_description_delivery_failed")
                else FILE_SENT,
            )
    except (FileNotFoundError, PermissionError) as e:
        error_code = _make_error_code_for_exception(platform, e, prefix_platform="file")
        _schedule_platform_failure_log(
            platform=platform,
            stage="send_file_access",
            url=url,
            error_code=error_code,
            exc=e,
            session_id=session_data.get("session_id"),
        )
        await _edit_delivery_status(
            query,
            USER_FILE_ERROR_WITH_CODE.format(error_code=error_code),
            reply_markup=feedback_markup(context, error_code, url, existing=back_markup),
        )
    except telegram.error.TelegramError as e:
        error_code = _make_error_code_for_exception(platform, e, prefix_platform="telegram")
        _schedule_platform_failure_log(
            platform=platform,
            stage="send_file_telegram",
            url=url,
            error_code=error_code,
            exc=e,
            session_id=session_data.get("session_id"),
        )
        await _edit_delivery_status(
            query,
            _build_public_error_message(platform, error_code, e),
            reply_markup=feedback_markup(context, error_code, url, existing=back_markup),
        )
    except Exception as e:
        error_code = _make_error_code_for_exception(platform, e, prefix_platform="bot")
        _schedule_platform_failure_log(
            platform=platform,
            stage="send_file_unexpected",
            url=url,
            error_code=error_code,
            exc=e,
            session_id=session_data.get("session_id"),
        )
        await _edit_delivery_status(
            query,
            _build_public_error_message(platform, error_code, e),
            reply_markup=feedback_markup(context, error_code, url, existing=back_markup),
        )
    finally:
        if success or session_data.get("_delivery_outcome_unknown"):
            await _cleanup_user_session(user_id, context, session_token)
        elif session_id := session_data.get("session_id"):
            _cleanup_session_when_idle(session_id)


async def _cache_complete_post(session_data: dict) -> None:
    """Полная публикация появляется в кеше только после всех подтверждений."""
    bot_id = session_data.get("_bot_id")
    if not bot_id or session_data.get("_cached_manifest_id"):
        return
    info = session_data.get("video_info") or {}
    platform = session_data["platform"]
    key = "_nuvio_instagram_carousel_items" if info.get("_nuvio_instagram_mixed_post") else f"_nuvio_{platform}_images"
    expected = int(session_data.get("_post_expected_count") or len(info.get(key) or []))
    audio_expected = bool(info.get(f"_nuvio_{platform}_audio_url"))
    messages = session_data.get("_media_messages", [])
    artifacts = [artifact_from_message(message) for message in messages]
    if not expected or any(item is None for item in artifacts):
        return
    if len(artifacts) != expected + int(audio_expected) or (audio_expected and not session_data.get("_audio_delivered")):
        return
    items = [(item, "audio" if audio_expected and index == expected else "media")
             for index, item in enumerate(artifacts)]
    await cache_call(telegram_cache.artifacts.put, bot_id, session_data["url"],
                     recipe_for(platform, "photo_post"), items, {"count": expected})


async def _deliver_cached_post(query, session_data: dict) -> bool:
    """Отправляет полный состав с сохраненными типами и порядком блоками до десяти."""
    if not session_data.get("_bot_id") or session_data.get("_delivered_items"):
        return False
    manifest = await cache_call(telegram_cache.artifacts.get, session_data["_bot_id"],
                                session_data["url"], recipe_for(session_data["platform"], "photo_post"))
    if not manifest:
        return False
    caption, chunks = description_delivery_plan(_description_for_delivery(session_data))
    items = manifest["items"]
    offset = 0
    while offset < len(items):
        artifact, role = items[offset]
        family = "visual" if artifact.kind in {"photo", "video"} else artifact.kind
        group = []
        while offset + len(group) < len(items) and len(group) < 10:
            candidate, candidate_role = items[offset + len(group)]
            candidate_family = "visual" if candidate.kind in {"photo", "video"} else candidate.kind
            if candidate_role != role or candidate_family != family:
                break
            group.append(candidate)
        try:
            if len(group) == 1:
                await _call_telegram_with_retry_after(
                    lambda: _reply_cached(query, group[0], social=True, caption=caption if offset == 0 else None), session_data)
            else:
                classes = {"photo": InputMediaPhoto, "video": InputMediaVideo,
                           "audio": telegram.InputMediaAudio, "document": telegram.InputMediaDocument}
                media = [classes[item.kind](media=item.file_id, caption=caption if offset == 0 and index == 0 else None)
                         for index, item in enumerate(group)]
                await _call_telegram_with_retry_after(lambda: query.message.reply_media_group(media=media, do_quote=False), session_data)
        except telegram.error.BadRequest as error:
            if not _is_stale_file_id(error):
                raise
            await cache_call(telegram_cache.artifacts.invalidate_manifest, manifest["id"])
            if offset:
                raise
            return False
        offset += len(group)
        if role == "audio":
            session_data["_audio_delivered"] = True
        else:
            session_data["_delivered_items"] = offset
    session_data["_cached_manifest_id"] = manifest["id"]
    await cache_call(telegram_cache.artifacts.touch, manifest["id"])
    await _send_description_chunks(query, chunks, session_data.get("_first_media_message"), session_data)
    return True


async def _send_photo_post_assets(
    query: telegram.CallbackQuery,
    session_token: str | None,
    session_data: dict,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Отправляет фото-пост последовательными альбомами и отдельным аудио."""
    from utils import tiktok_instagram_utils as sources

    user_id = query.from_user.id
    url = session_data["url"]
    session_id = session_data["session_id"]
    back_markup = (
        _build_back_markup(session_token)
        if session_token
        else InlineKeyboardMarkup(
            [[InlineKeyboardButton(BTN_BACK, callback_data="main|back")]]
        )
    )
    cancel_markup = _build_cancel_markup(session_token) if session_token else None

    if session_data.get("platform") == "instagram":
        platform = "instagram"
        download_photo_post_assets = sources.download_instagram_photo_post_assets
        empty_images_message = ERROR_INSTAGRAM_NO_PHOTOS
        images_key = "_nuvio_instagram_images"
        audio_key = "_nuvio_instagram_audio_url"
        referer = "https://www.instagram.com/"
    else:
        platform = "tiktok"
        download_photo_post_assets = sources.download_tiktok_photo_post_assets
        empty_images_message = ERROR_TIKTOK_NO_PHOTOS
        images_key = "_nuvio_tiktok_images"
        audio_key = "_nuvio_tiktok_audio_url"
        referer = "https://www.tiktok.com/"

    async def finish_delivery() -> None:
        await _cache_complete_post(session_data)
        await _record_delivery(user_id, session_data)
        await _edit_delivery_status(
            query,
            DESCRIPTION_SEND_FAILED
            if session_data.get("_description_delivery_failed")
            else FILE_SENT
        )
        await _cleanup_user_session(user_id, context, session_token)

    def log_failure(stage: str, error_code: str, error: BaseException) -> None:
        _schedule_platform_failure_log(
            platform=platform,
            stage=stage,
            url=url,
            error_code=error_code,
            exc=error,
            session_id=session_id,
        )

    async def report_failure(text: str, reply_markup=None) -> None:
        await _edit_delivery_status(
            query,
            PHOTO_POST_PARTIAL if session_data.get("_delivery_progress") else text,
            reply_markup=reply_markup or back_markup,
        )
        _cleanup_session_when_idle(session_id)

    async def report_telegram_failure(error: telegram.error.TelegramError) -> None:
        error_code = _make_error_code_for_exception(
            platform, error, prefix_platform="telegram"
        )
        log_failure("send_photo_post_telegram", error_code, error)
        await report_failure(_build_public_error_message(platform, error_code, error), reply_markup=feedback_markup(context, error_code, url))

    try:
        await _edit_delivery_status(
            query, DOWNLOADING_PHOTOS_MESSAGE, reply_markup=cancel_markup
        )

        # Прямая отправка сохраняет старый быстрый путь. Отказ URL в первой
        # группе разрешает скачать весь пост; после частичной отправки скачивается
        # весь набор, но пользователю отправляется только неподтвержденный остаток.
        video_info = session_data.get("video_info") or {}
        is_mixed = bool(video_info.get("_nuvio_instagram_mixed_post"))
        if await _deliver_cached_post(query, session_data):
            await finish_delivery()
            return
        if is_mixed and not video_info.get("_nuvio_instagram_carousel_complete"):
            await _edit_delivery_status(query, INSTAGRAM_CAROUSEL_INCOMPLETE)
            await _cleanup_user_session(user_id, context, session_token)
            return
        if not is_mixed and not session_data.get("_delivered_items"):
            photo_plan = await run_blocking(
                sources.resolve_photo_post_handoff,
                list(video_info.get(images_key) or []),
                video_info.get(audio_key),
                referer,
                description=f"resolve_{platform}_photo_post_handoff",
                session_id=session_id,
            )
            if photo_plan:
                outcome = await _deliver_photo_post_by_url(
                    query, photo_plan, session_data
                )
                session_data["_delivered_items"] = outcome.confirmed_items
                session_data["_delivery_progress"] = max(int(session_data.get("_delivery_progress") or 0), outcome.confirmed_items)
                if outcome.messages:
                    session_data["_confirmed_delivery_messages"] = outcome.messages
                    session_data.setdefault(
                        "_first_media_message", outcome.messages[0]
                    )
                if outcome.audio_delivered is not None:
                    session_data["_audio_delivered"] = outcome.audio_delivered
                if outcome.state == "unknown":
                    logger.warning(
                        "Неизвестный исход URL-отправки: platform=%s session=%s confirmed=%s error=%s",
                        platform,
                        session_id,
                        outcome.confirmed_items,
                        type(outcome.error).__name__ if outcome.error else "unknown",
                    )
                    await _edit_delivery_status(query, DELIVERY_OUTCOME_UNKNOWN)
                    await _cleanup_user_session(user_id, context, session_token)
                    return
                if outcome.state == "delivered":
                    await finish_delivery()
                    return

        assets = await run_blocking(
            download_photo_post_assets,
            url,
            session_id,
            video_info,
            description=f"download_{platform}_photo_post_assets",
            session_id=session_id,
        )
        asset_info = assets.get("info")
        if isinstance(asset_info, dict):
            video_info = asset_info
            session_data["video_info"] = asset_info
            is_mixed = bool(asset_info.get("_nuvio_instagram_mixed_post"))
        media_items = list(assets.get("items") or [])
        audio_path = assets.get("audio")
        if not media_items:
            raise Exception(empty_images_message)

        delivered_items = int(session_data.get("_delivered_items") or 0)
        expected_count = len(
            video_info.get(
                "_nuvio_instagram_carousel_items" if is_mixed else images_key
            )
            or []
        )
        session_data["_post_expected_count"] = expected_count or len(media_items)
        if delivered_items > len(media_items) or (expected_count and expected_count != len(media_items)):
            raise RuntimeError("Состав публикации изменился после частичной отправки")
        if delivered_items and not session_data.get("_first_media_message"):
            raise RuntimeError("Нет подтверждения первого сообщения альбома")

        caption, description_chunks = description_delivery_plan(
            _description_for_delivery(session_data)
        )
        for block_index, group in enumerate(
            chunk_media(media_items[delivered_items:]), start=1
        ):
            if is_cancelled(session_id):
                raise CancelledByUser("отправка публикации отменена")
            group_caption = caption if delivered_items == 0 else None
            if is_mixed:
                sent_messages = await _send_mixed_media_group(
                    query, group, group_caption, session_data
                )
            else:
                sent_messages = await _send_photo_file_group(
                    query,
                    [Path(item["path"]) for item in group],
                    group_caption,
                    session_data,
                )
            if not session_data.get("_first_media_message") and sent_messages:
                session_data["_first_media_message"] = sent_messages[0]
            delivered_items += len(group)
            session_data["_delivered_items"] = delivered_items
            session_data["_delivery_progress"] = delivered_items
            logger.info(
                "Медиа-блок доставлен: platform=%s session=%s block=%s transport=file items=%s",
                platform,
                session_id,
                block_index,
                len(group),
            )

        await _send_description_chunks(
            query,
            description_chunks,
            session_data.get("_first_media_message"),
            session_data,
        )

        if audio_path and not session_data.get("_audio_delivered"):
            if is_cancelled(session_id):
                raise CancelledByUser("отправка аудио отменена")
            await _edit_delivery_status(
                query, DOWNLOADING_AUDIO_MESSAGE, reply_markup=cancel_markup
            )
            with _upload_source(audio_path) as (audio_file, reset_audio):
                await _call_telegram_with_retry_after(
                    lambda: query.message.reply_audio(
                        do_quote=False,
                        audio=audio_file,
                        caption=None,
                        write_timeout=1800,
                        read_timeout=1800,
                    ),
                    session_data,
                    reset_files=reset_audio,
                )
            session_data["_audio_delivered"] = True

        await finish_delivery()
    except (FileNotFoundError, PermissionError) as e:
        error_code = _make_error_code_for_exception(
            platform, e, prefix_platform="file"
        )
        log_failure("send_photo_post_access", error_code, e)
        await _edit_delivery_status(
            query,
            USER_FILE_ERROR_WITH_CODE.format(error_code=error_code),
            reply_markup=feedback_markup(context, error_code, url, existing=back_markup),
        )
        _cleanup_session_when_idle(session_id)
    except telegram.error.BadRequest as e:
        await report_telegram_failure(e)
    except telegram.error.NetworkError as e:
        session_data["_delivery_outcome_unknown"] = not _is_proven_not_sent(e)
        error_code = _make_error_code_for_exception(
            platform, e, prefix_platform="telegram"
        )
        log_failure("send_photo_post_network", error_code, e)
        await _edit_delivery_status(query, DELIVERY_OUTCOME_UNKNOWN if not _is_proven_not_sent(e) else PHOTO_POST_PARTIAL if session_data.get("_delivery_progress") else _build_public_error_message(platform, error_code, e))
        await _cleanup_user_session(user_id, context, session_token)
    except telegram.error.TelegramError as e:
        await report_telegram_failure(e)
    except Exception as e:
        error_code = _make_error_code(
            platform, _classify_internal_error_category(platform, e)
        )
        log_failure("send_photo_post_unexpected", error_code, e)
        await report_failure(_build_public_error_message(platform, error_code, e), reply_markup=feedback_markup(context, error_code, url))


def _file_ready_to_send(file_path: Path) -> bool:
    """Проверяет, что отправлять действительно есть что.

    В локальном режиме путь уходит в PTB как объект, а `resolve()` файловую
    систему не трогает. Пропавший файл в этом случае превращался в
    `TypeError: Object of type PosixPath is not JSON serializable`: PTB
    конвертирует путь в ссылку только когда `is_file()` истинно, иначе кладёт
    объект в тело запроса. Пользователь получал «ошибку сети» вместо «нет
    файла».

    Нулевой размер считается отсутствием: Telegram пустой файл не примет.
    """
    try:
        return file_path.is_file() and file_path.stat().st_size > 0
    except OSError:
        return False


async def send_single_file(
    query: telegram.CallbackQuery,
    file_path: Path,
    session_token: str,
    session_data: dict,
    max_retries: int = 3,
    cache_format_id: str | None = None,
    feedback_context=None,
) -> bool:
    """Новая версия отправки одного файла с обратной кнопкой для текущей сессии."""
    last_error: Exception | None = None
    back_markup = _build_back_markup(session_token)
    platform = session_data.get("platform", "bot")
    url = session_data.get("url")

    def failure_markup(error_code: str):
        return (
            feedback_markup(feedback_context, error_code, url, existing=back_markup)
            if feedback_context is not None else back_markup
        )

    # Повторять нечего: файла нет, и от третьей попытки он не появится.
    if not _file_ready_to_send(file_path):
        missing_file = FileNotFoundError(str(file_path))
        error_code = _make_error_code_for_exception(
            platform, missing_file, prefix_platform="file"
        )
        _schedule_platform_failure_log(
            platform=platform,
            stage="send_single_file_missing",
            url=url,
            error_code=error_code,
            exc=missing_file,
            session_id=session_data.get("session_id"),
        )
        await _edit_delivery_status(
            query,
            USER_FILE_ERROR_WITH_CODE.format(error_code=error_code),
            reply_markup=failure_markup(error_code),
        )
        return False

    media_kind = media_kind_for_suffix(file_path.suffix.lower())
    # Размеры измеряются один раз до попыток: файл между ними не меняется.
    # Без них Telegram на тяжёлых файлах записывает `320x320`, и плеер на iOS
    # рисует видео по этому квадрату — ADR-002. Сбой пробы не повод отменять
    # отправку, поэтому пустой результат просто означает «как раньше».
    video_kwargs: dict = {}
    if media_kind == "video":
        try:
            geometry = await run_blocking(
                get_video_geometry,
                file_path,
                description="get_video_geometry",
                session_id=session_data.get("session_id"),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Не удалось измерить видео %s: %s", file_path, e)
            geometry = None
        if geometry:
            video_kwargs = geometry
        else:
            logger.warning(
                "Размеры видео %s неизвестны, отправляем без них: Telegram может "
                "записать 320x320",
                file_path,
            )

    retry_limit = 1 if platform in {"tiktok", "instagram"} else max_retries
    for attempt in range(1, retry_limit + 1):
        try:
            message = None
            telegram_file = (
                file_path.resolve()
                if TELEGRAM_LOCAL_MODE
                else file_path.open("rb")
            )

            try:
                if media_kind == "video":
                    message = await _call_telegram_with_retry_after(
                        lambda: query.message.reply_video(
                            do_quote=False if platform in {"tiktok", "instagram"} else None,
                            video=telegram_file,
                            caption=description_delivery_plan(
                                _description_for_delivery(session_data)
                            )[0],
                            parse_mode=None,
                            supports_streaming=True,
                            write_timeout=1800,
                            read_timeout=1800,
                            **video_kwargs,
                        ),
                        session_data,
                        reset_files=lambda: telegram_file.seek(0)
                        if not TELEGRAM_LOCAL_MODE
                        else None,
                    )
                elif media_kind == "audio":
                    message = await _call_telegram_with_retry_after(
                        lambda: query.message.reply_audio(
                            do_quote=False if platform in {"tiktok", "instagram"} else None,
                            audio=telegram_file,
                            caption=None,
                            write_timeout=1800,
                            read_timeout=1800,
                        ),
                        session_data,
                        reset_files=lambda: telegram_file.seek(0)
                        if not TELEGRAM_LOCAL_MODE
                        else None,
                    )
                else:
                    message = await _call_telegram_with_retry_after(
                        lambda: query.message.reply_document(
                            do_quote=False if platform in {"tiktok", "instagram"} else None,
                            document=telegram_file,
                            caption=None,
                            write_timeout=1800,
                            read_timeout=1800,
                        ),
                        session_data,
                        reset_files=lambda: telegram_file.seek(0)
                        if not TELEGRAM_LOCAL_MODE
                        else None,
                    )
            finally:
                if not TELEGRAM_LOCAL_MODE:
                    telegram_file.close()

            if message and platform in {"tiktok", "instagram"}:
                session_data["_first_media_message"] = message
                session_data["_delivered_items"] = 1
                session_data["_delivery_progress"] = 1
                session_data["_confirmed_delivery_messages"] = (message,)

            # Кэширование file_id для видео, аудио и документов
            if message and url and cache_format_id:
                await cache_call(_cache_sent_media,
                    message,
                    url,
                    platform,
                    cache_format_id,
                    session_data.get("video_info"),
                    session_data.get("session_id"),
                )

            if message and media_kind == "video":
                await _send_description_chunks(
                    query,
                    _description_chunks_for_delivery(session_data),
                    message,
                    session_data,
                )

            return True
        except (FileNotFoundError, PermissionError) as e:
            error_code = _make_error_code_for_exception(
                platform, e, prefix_platform="file"
            )
            _schedule_platform_failure_log(
                platform=platform,
                stage="send_single_file_access",
                url=url,
                error_code=error_code,
                exc=e,
                session_id=session_data.get("session_id"),
            )
            await _edit_delivery_status(
                query,
                USER_FILE_ERROR_WITH_CODE.format(error_code=error_code),
                reply_markup=failure_markup(error_code),
            )
            return False
        except telegram.error.BadRequest as e:
            error_code = _make_error_code_for_exception(
                platform, e, prefix_platform="telegram"
            )
            _schedule_platform_failure_log(
                platform=platform,
                stage="send_single_file_bad_request",
                url=url,
                error_code=error_code,
                exc=e,
                session_id=session_data.get("session_id"),
            )
            message = _build_public_error_message(platform, error_code, e)
            await _edit_delivery_status(query, message, reply_markup=failure_markup(error_code))
            return False
        except (telegram.error.NetworkError, telegram.error.TimedOut) as e:
            last_error = e
            logger.warning(f"Попытка {attempt}/{retry_limit} неудачна: {e}")
            session_data["_delivery_outcome_unknown"] = not _is_proven_not_sent(e)
            break
        except telegram.error.TelegramError as e:
            error_code = _make_error_code_for_exception(
                platform, e, prefix_platform="telegram"
            )
            _schedule_platform_failure_log(
                platform=platform,
                stage="send_single_file_telegram",
                url=url,
                error_code=error_code,
                exc=e,
                session_id=session_data.get("session_id"),
            )
            await _edit_delivery_status(
                query,
                _build_public_error_message(platform, error_code, e),
                reply_markup=failure_markup(error_code),
            )
            return False
        except Exception as e:
            error_code = _make_error_code_for_exception(
                platform, e, prefix_platform="bot"
            )
            _schedule_platform_failure_log(
                platform=platform,
                stage="send_single_file_unexpected",
                url=url,
                error_code=error_code,
                exc=e,
                session_id=session_data.get("session_id"),
            )
            await _edit_delivery_status(
                query,
                _build_public_error_message(platform, error_code, e),
                reply_markup=failure_markup(error_code),
            )
            return False

    if last_error:
        error_code = _make_error_code_for_exception(
            platform, last_error, prefix_platform="telegram"
        )
        _schedule_platform_failure_log(
            platform=platform,
            stage="send_single_file_retry_exhausted",
            url=url,
            error_code=error_code,
            exc=last_error,
            session_id=session_data.get("session_id"),
        )
        await _edit_delivery_status(
            query,
            DELIVERY_OUTCOME_UNKNOWN
            if session_data.get("_delivery_outcome_unknown")
            else _build_public_error_message(platform, error_code, last_error),
            reply_markup=(
                None
                if session_data.get("_delivery_outcome_unknown")
                else failure_markup(error_code)
            ),
        )
    return False


def format_duration(seconds: int) -> str:
    """
    Форматирует продолжительность из секунд в формат ЧЧ:ММ:СС.
    Args:
        seconds (int): Продолжительность в секундах.
    Returns:
        str: Отформатированная продолжительность.
    """
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    else:
        return f"{minutes}:{seconds:02d}"


def escape_markdown(text: str) -> str:
    """
    Экранирует специальные символы для Markdown.

    Args:
        text (str): Текст для экранирования.

    Returns:
        str: Экранированный текст.
    """
    if not text:
        return "N/A"

    # Используется legacy Markdown (parse_mode="Markdown"). В нем допустимо
    # экранировать только ограниченный набор символов; обратные слеши перед
    # точками, дефисами и прочей пунктуацией ломают разбор Telegram.
    return _telegram_escape_markdown(text, version=1)


def _markdown_to_plain_text(text: str) -> str:
    """Убирает разметку меню, сохраняя экранированные символы заголовка."""
    without_markup = re.sub(r"(?<!\\)[*`_]", "", text)
    return re.sub(r"\\([_*`\[\\])", r"\1", without_markup)
