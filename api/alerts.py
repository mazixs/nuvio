"""Уведомления админов о сбоях загрузок HTTP API.

Правило то же, что у бота (`should_notify_admins`): ожидаемые ограничения
платформ молчат, а `MEDIA_FORBIDDEN` и непонятные сбои доходят до админов.
Клиент API может повторять одну и ту же ссылку, поэтому одинаковый сбой
(платформа и категория) приходит не чаще раза в REPEAT_INTERVAL_SECONDS.

Отправка идёт в своём потоке: при недоступном Bot API таймауты не должны
занимать воркеры загрузок.
"""

import html
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from config import ADMIN_IDS, TELEGRAM_BOT_API_BASE_URL, TELEGRAM_TOKEN
from utils.logger import setup_logger
from utils.public_errors import should_notify_admins

logger = setup_logger(__name__)

REPEAT_INTERVAL_SECONDS = 15 * 60
# Если сообщение не дошло ни до одного админа, тот же сбой повторяется раньше:
# иначе первый же сбой Bot API заглушил бы сигнал на все 15 минут.
RETRY_AFTER_FAILED_SEND_SECONDS = 60
# Ссылка и текст ошибки обрезаются до экранирования: так сообщение заведомо
# укладывается в 4096 символов Telegram.
_PREVIEW_LENGTH = 300
_lock = threading.Lock()
_last_sent: dict[tuple[str, str], float] = {}
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nuvio-api-alerts")


def _claim(platform: str, category: str, now: float) -> bool:
    """Решает, отправлять ли сбой, и сразу отмечает отправку."""
    with _lock:
        last = _last_sent.get((platform, category))
        if last is not None and now - last < REPEAT_INTERVAL_SECONDS:
            return False
        _last_sent[(platform, category)] = now
        return True


def _release_claim(platform: str, category: str, now: float) -> None:
    """Ни один админ не получил сообщение: повтор разрешён через минуту."""
    with _lock:
        if _last_sent.get((platform, category)) == now:
            _last_sent[(platform, category)] = (
                now - REPEAT_INTERVAL_SECONDS + RETRY_AFTER_FAILED_SEND_SECONDS
            )


def submit_download_failure(
    *, platform: str, category: str, reference: str, url: str, error: BaseException
) -> None:
    """Ставит уведомление в очередь и сразу возвращается."""
    _executor.submit(
        notify_download_failure,
        platform=platform, category=category, reference=reference, url=url, error=error,
    )


def notify_download_failure(
    *, platform: str, category: str, reference: str, url: str, error: BaseException
) -> None:
    """Сообщает админам о сбое загрузки. Блокирующий вызов для потока задачи."""
    if not should_notify_admins(category) or not TELEGRAM_TOKEN or not ADMIN_IDS:
        return
    now = time.monotonic()
    if not _claim(platform, category, now):
        logger.info("Уведомление о сбое API %s/%s уже отправлено недавно", platform, category)
        return
    text = (
        "⚠️ <b>HTTP API: сбой загрузки</b>\n\n"
        f"<b>Код:</b> <code>{html.escape(reference)}</code>\n"
        f"<b>Платформа:</b> {html.escape(platform)}\n"
        f"<b>Категория:</b> {html.escape(category)}\n"
        f"<b>Ссылка:</b> {html.escape(url[:_PREVIEW_LENGTH])}\n"
        f"<b>Исключение:</b> <code>{html.escape(type(error).__name__)}</code>\n"
        f"<pre>{html.escape(str(error)[:_PREVIEW_LENGTH])}</pre>\n\n"
        f"Такие же сбои следующие {REPEAT_INTERVAL_SECONDS // 60} минут не присылаются. "
        f"Подробности в журнале API: <code>API_DL_FAIL ref={html.escape(reference)}</code>."
    )
    if not _send_to_admins(text):
        _release_claim(platform, category, now)


def notify_restart(stuck_workers: int) -> None:
    """Сообщает о перезапуске процесса. Блокирующий вызов: фоновый поток не успел бы."""
    if not TELEGRAM_TOKEN or not ADMIN_IDS:
        return
    _send_to_admins(
        "🚨 <b>HTTP API: зависли все воркеры</b>\n\n"
        f"Потоков загрузки не вернулось: {stuck_workers}. Процесс завершается, и Compose "
        "поднимет его заново; задачи и файлы в нём теряются. Подробности в журнале API."
    )


def _send_to_admins(text: str) -> bool:
    """Отправляет сообщение всем админам; True, если дошло хотя бы до одного."""
    endpoint = f"{TELEGRAM_BOT_API_BASE_URL}{TELEGRAM_TOKEN}/sendMessage"
    delivered = False
    for admin_id in ADMIN_IDS:
        try:
            response = httpx.post(
                endpoint,
                json={
                    "chat_id": admin_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "link_preview_options": {"is_disabled": True},
                },
                timeout=10,
            )
            response.raise_for_status()
            delivered = True
        except Exception as exc:
            # Не только сетевые ошибки: результат потока уведомлений никто не
            # читает, и любое другое исключение пропало бы молча. Текст исключения
            # httpx содержит адрес запроса, а в нём токен бота.
            logger.warning(
                "Не удалось уведомить админа %s о сбое API: %s", admin_id, type(exc).__name__
            )
    return delivered
