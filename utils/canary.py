"""Канареечная проверка YouTube и уведомление о поломке.

18 августа 2026 YouTube начал отдавать 403 на прямые ссылки `videoplayback`, и
бот стоял сломанным больше суток, пока владелец не наткнулся сам. Канарейка
обнаруживает отказ и сообщает администратору; версия меняется только новым образом.

Проверка идёт продакшн-путём и «облегчённых» вариантов не признаёт, потому что
на разборе инцидента обе ловушки уже сработали:

* **Маленький кусок врёт ровно тогда, когда проверка нужна.** Запрос `Range` на
  64 КБ отдавал 206, пока продакшн-запрос на 10 МБ (штатный `http_chunk_size`
  из `DEFAULT_YTDLP_NETWORK_OPTS`) получал 403 на том же файле. Ключ `--test` у
  yt-dlp подменяет размер куска на 10 КБ и прошёл бы мимо поломки так же.
  Поэтому канарейка качает ролик целиком и теми же сетевыми опциями, что бот.
* **Кэш `file_id` мерил бы сам себя.** В пользовательском потоке кэш читается
  до скачивания, так что на уже виденной ссылке канарейка получила бы готовый
  `file_id` при полностью сломанном YouTube. Поэтому в модуле нет ни одного
  обращения к `utils.video_cache` — ни на чтение, ни на запись, — а скачивание
  зовётся напрямую через `utils.youtube_utils.download_video`.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path

from telegram.ext import ContextTypes

from config import (
    ADMIN_IDS,
    BLOCKING_TASK_TIMEOUT,
    CANARY_ENABLED,
    CANARY_VIDEO_ID,
    MAX_FILE_SIZE,
)
from utils.cancellation import (
    CancelledByUser,
    forget_cancellation,
    request_cancellation,
)
from utils.logger import setup_logger
from utils.public_errors import classify_internal_error_category
from utils.runtime_status import save_canary_status
from utils.temp_file_manager import cleanup_temp_files
from utils.tg_video_choice import select_tg_video_format
from utils.youtube_utils import download_video, get_available_formats, get_video_info

logger = setup_logger(__name__)

# Бюджет проверки. Формат выбирает тот же селектор, что и кнопка «отправить
# видео», но бюджет зажат: смысл канарейки — доказать, что медиа отдаётся, а не
# скачать максимум возможного. При локальном Bot API продакшн-бюджет равен 2 ГБ,
# и на десятиминутном ролике это 200+ МБ дважды в сутки на домашнем канале.
# Важно другое: выбранный формат должен быть заметно больше одного куска в 10 МБ,
# иначе проверка не дойдёт до второго запроса к `videoplayback` — того самого,
# на котором инцидент и проявлялся.
CANARY_BUDGET_BYTES = min(MAX_FILE_SIZE, 50 * 1024 * 1024)
CANARY_MAX_HEIGHT = 720

# Тот же предел, что у пользовательских загрузок: канарейка не имеет права висеть
# дольше, чем реальное скачивание.
CANARY_TIMEOUT_SECONDS = BLOCKING_TASK_TIMEOUT

@dataclass(frozen=True)
class CanaryOutcome:
    """Итог одной проверки: что делали и чем закончилось."""

    ok: bool
    stage: str
    detail: str
    category: str | None = None
    error_code: str | None = None
    format_id: str | None = None
    size_bytes: int | None = None


def canary_video_url() -> str:
    """Ссылка на эталонный ролик проверки."""
    return f"https://www.youtube.com/watch?v={CANARY_VIDEO_ID}"


def _error_code(category: str) -> str:
    """Код в том же формате, что видит пользователь: `YT-<КАТЕГОРИЯ>-<6 знаков>`.

    Формат повторён здесь намеренно: `_make_error_code` живёт в
    `utils/telegram_utils.py` вместе с хэндлерами и антиспамом, а канарейке из
    этого модуля не нужно ничего, кроме одной строки формата.
    """
    normalized = "UNEXPECT" if category.upper() == "UNKNOWN" else category.upper()
    return f"YT-{normalized[:8]}-{uuid.uuid4().hex[:6].upper()}"


def _failure(
    stage: str, exc: BaseException, format_id: str | None = None
) -> CanaryOutcome:
    """Превращает исключение в итог с категорией и кодом ошибки."""
    category = classify_internal_error_category("youtube", exc)
    return CanaryOutcome(
        ok=False,
        stage=stage,
        detail=f"{type(exc).__name__}: {exc}",
        category=category,
        error_code=_error_code(category),
        format_id=format_id,
    )


def run_youtube_canary_check(session_id: str) -> CanaryOutcome:
    """Качает эталонный ролик тем же кодом, которым бот качает пользовательский.

    Блокирующая: вызывать только из пула потоков.
    """
    url = canary_video_url()
    logger.info("🐤 Канарейка YouTube: проверяю %s (сессия %s)", url, session_id)

    stage = "video_info"
    format_id = None
    try:
        video_info = get_video_info(url, session_id=session_id)
        stage = "format_choice"
        formats = get_available_formats(video_info)
        choice = select_tg_video_format(
            formats.get("video_only", []),
            formats.get("audio_only", []),
            formats.get("combined", []),
            CANARY_BUDGET_BYTES,
            max_height=CANARY_MAX_HEIGHT,
        )
        if choice is None:
            return CanaryOutcome(
                ok=False,
                stage=stage,
                detail="ни один формат не влез в бюджет проверки",
                category="FORMAT_UNAVAILABLE",
                error_code=_error_code("FORMAT_UNAVAILABLE"),
            )
        format_id = choice.format_id
        stage = "download"
        downloaded = Path(
            download_video(
                url, format_id, session_id, max_file_size=CANARY_BUDGET_BYTES
            )
        )
        size_bytes = downloaded.stat().st_size
        if size_bytes <= 0 or size_bytes > CANARY_BUDGET_BYTES:
            return CanaryOutcome(
                ok=False,
                stage="download",
                detail="размер файла вне бюджета проверки",
                category="FILE" if size_bytes <= 0 else "LARGE",
                error_code=_error_code("FILE" if size_bytes <= 0 else "LARGE"),
                format_id=format_id,
            )
        return CanaryOutcome(
            ok=True,
            stage="download",
            detail=f"скачано {size_bytes / (1024 * 1024):.1f} МБ",
            format_id=format_id,
            size_bytes=size_bytes,
        )
    except CancelledByUser:
        return CanaryOutcome(
            ok=False,
            stage="cancelled",
            detail="проверка прервана по таймауту",
            category="TIMEOUT",
            error_code=_error_code("TIMEOUT"),
            format_id=format_id,
        )
    except Exception as exc:  # noqa: BLE001
        return _failure(stage, exc, format_id=format_id)
    finally:
        # Файл был нужен только как доказательство, что YouTube отдаёт медиа:
        # никому он не отправляется и на диске не остаётся.
        cleanup_temp_files(session_id)
        forget_cancellation(session_id)


async def _check_in_thread() -> CanaryOutcome:
    """Гоняет блокирующую проверку в пуле потоков и не даёт ей висеть вечно."""
    session_id = f"canary-{uuid.uuid4().hex[:8]}"
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(run_youtube_canary_check, session_id),
            timeout=CANARY_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        # `wait_for` снимает только ожидание, а поток продолжает качать — то же
        # поведение, что у `run_blocking`. До yt-dlp отмена доходит хуком,
        # который `download_video` ставит по этому же `session_id`.
        request_cancellation(session_id)
        logger.error(
            "🐤 Канарейка YouTube: проверка не уложилась в %s с",
            CANARY_TIMEOUT_SECONDS,
        )
        return CanaryOutcome(
            ok=False,
            stage="timeout",
            detail=f"проверка не завершилась за {CANARY_TIMEOUT_SECONDS} с",
            category="NETWORK_TIMEOUT",
            error_code=_error_code("NETWORK_TIMEOUT"),
        )


def _build_report(outcome: CanaryOutcome, reaction: list[str]) -> str:
    """Собирает короткий отчёт для админов."""
    lines = [
        "🐤 Канарейка YouTube упала",
        "",
        f"Проверял: скачивание {canary_video_url()}",
        f"Формат: {outcome.format_id or '—'}",
        f"Этап: {outcome.stage}",
        f"Код: {outcome.error_code or '—'} ({outcome.category or 'UNEXPECT'})",
        f"Причина: {outcome.detail[:400]}",
        "",
        *reaction,
    ]
    return "\n".join(lines)


async def _notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Рассылает отчёт админам.

    Бот берётся из контекста job'ы — штатный путь PTB, который не тянет за собой
    хэндлеры из `utils/telegram_utils.py`.
    """
    if not ADMIN_IDS:
        logger.warning("🐤 ADMIN_IDS пуст: отчёт канарейки остался только в логе")
        save_canary_status(notification="не отправлено: нет администраторов")
        return

    failed = []
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text)
        except Exception as exc:  # noqa: BLE001
            failed.append(admin_id)
            logger.warning("Не удалось отправить отчет канарейки админу %s: %s", admin_id, exc)
    save_canary_status(
        notification="доставлено" if not failed else f"ошибка для {len(failed)} администраторов"
    )


async def youtube_canary_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Периодическая проверка YouTube: качаем эталон, при провале зовём админов."""
    # Флаг проверяется и здесь, хотя job регистрируется в `main.py` только при
    # включённой канарейке: выключаться она должна из одного места.
    if not CANARY_ENABLED:
        return

    outcome = await _check_in_thread()
    save_canary_status(
        state="работает" if outcome.ok else f"ошибка: {outcome.stage}",
        category=outcome.category,
        error_code=outcome.error_code,
        format_id=outcome.format_id,
        size_bytes=outcome.size_bytes,
    )
    if outcome.ok:
        logger.info(
            "🐤 Канарейка YouTube в порядке: формат %s, %s",
            outcome.format_id,
            outcome.detail,
        )
        return

    logger.error(
        "🐤 Канарейка YouTube упала: этап=%s код=%s категория=%s причина=%s",
        outcome.stage,
        outcome.error_code,
        outcome.category,
        outcome.detail,
    )
    await _notify_admins(context, _build_report(outcome, []))
