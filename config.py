"""
Файл конфигурации для Telegram бота.
"""

import os
import logging
from pathlib import Path


def _parse_bool(value: str | None, default: bool = False) -> bool:
    """Парсит булевы env-значения."""
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_log_level(value: str | None) -> int:
    """Возвращает числовой уровень логирования из env-строки."""
    level_name = (value or "INFO").strip().upper()
    return getattr(logging, level_name, logging.INFO)


def _parse_admin_ids(value: str | None) -> list[int]:
    """Парсит ADMIN_IDS, игнорируя пустые и некорректные значения."""
    admin_ids: list[int] = []
    for raw_value in (value or "").split(","):
        candidate = raw_value.strip()
        if not candidate:
            continue
        try:
            admin_ids.append(int(candidate))
        except ValueError:
            logging.warning(
                "ADMIN_IDS содержит некорректное значение %r и будет проигнорирован",
                candidate,
            )
    return admin_ids


# Допустимые символы id ролика YouTube: ровно 11 знаков латиницы, цифр, дефиса и
# подчёркивания. Опечатка в id обошлась бы дороже строгости — канарейка стала бы
# звать админов на каждой проверке.
_YOUTUBE_ID_ALPHABET = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)

# Эталон - образовательный ролик NASA JPL длительностью 149 секунд. Проверен
# полной загрузкой 27.6 МБ 24.09.2026. Прежний Blender-ролик стал требовать
# подтверждения входа даже при исправном yt-dlp. Длина и размер важны: отказ
# YouTube 18.08.2026 проявлялся лишь после первого куска медиа в 10 МБ.
DEFAULT_CANARY_VIDEO_ID = "6WFj-CKldv4"


def _parse_canary_interval_hours(value: str | None) -> int:
    """Часы между канареечными проверками: мусор и выход за 1–168 → 12."""
    try:
        hours = int((value or "12").strip())
    except ValueError:
        logging.warning(
            "CANARY_INTERVAL_HOURS=%r не число, используем 12 часов", value
        )
        return 12
    if not 1 <= hours <= 168:
        logging.warning(
            "CANARY_INTERVAL_HOURS=%s вне диапазона 1–168, используем 12 часов",
            hours,
        )
        return 12
    return hours


def _parse_int_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    """Целое из env в границах; мусор и выход за пределы → умолчание."""
    raw = os.environ.get(name)
    try:
        value = int((raw or str(default)).strip())
    except ValueError:
        logging.warning("%s=%r не число, используем %s", name, raw, default)
        return default
    if not minimum <= value <= maximum:
        logging.warning(
            "%s=%s вне диапазона %s–%s, используем %s",
            name, value, minimum, maximum, default,
        )
        return default
    return value


def _parse_canary_video_id(value: str | None) -> str:
    """Проверяет id ролика канарейки, при непохожем значении берёт эталонный."""
    candidate = (value or "").strip()
    if not candidate:
        return DEFAULT_CANARY_VIDEO_ID
    if len(candidate) == 11 and set(candidate) <= _YOUTUBE_ID_ALPHABET:
        return candidate
    logging.warning(
        "CANARY_VIDEO_ID=%r не похож на id ролика YouTube, используем %s",
        value,
        DEFAULT_CANARY_VIDEO_ID,
    )
    return DEFAULT_CANARY_VIDEO_ID


# Токен Telegram бота (получить у @BotFather)
# ОБЯЗАТЕЛЬНО: установите переменную окружения TELEGRAM_TOKEN
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")

# Настройки логирования
LOG_LEVEL = _parse_log_level(os.environ.get("LOG_LEVEL"))
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# Пути для сохранения файлов
BASE_DIR = Path(__file__).parent
TEMP_DIR = Path(os.environ.get("TEMP_DIR", str(BASE_DIR / "temp"))).resolve()
TEMP_DIR.mkdir(parents=True, exist_ok=True)
SECRETS_DIR = BASE_DIR / ".secrets"
SECRETS_DIR.mkdir(exist_ok=True)

# Ограничения
MAX_VIDEO_DURATION = 3 * 60 * 60  # 3 часа в секундах
TELEGRAM_LOCAL_MODE = _parse_bool(
    os.environ.get("TELEGRAM_LOCAL_MODE"), default=False
)
TELEGRAM_BOT_API_BASE_URL = os.environ.get(
    "TELEGRAM_BOT_API_BASE_URL", "https://api.telegram.org/bot"
).rstrip("/")
TELEGRAM_BOT_API_FILE_URL = os.environ.get(
    "TELEGRAM_BOT_API_FILE_URL", "https://api.telegram.org/file/bot"
).rstrip("/")
_configured_max_file_size_mb = int(
    os.environ.get(
        "TELEGRAM_MAX_FILE_SIZE_MB",
        "2000" if TELEGRAM_LOCAL_MODE else "50",
    )
)
_telegram_mode_limit_mb = 2000 if TELEGRAM_LOCAL_MODE else 50
MAX_FILE_SIZE_MB = min(
    max(_configured_max_file_size_mb, 1),
    _telegram_mode_limit_mb,
)
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024

# Настройки пула исполнителей для блокирующих задач
DOWNLOAD_WORKERS = int(os.environ.get("DOWNLOAD_WORKERS", "8"))
BLOCKING_TASK_TIMEOUT = int(os.environ.get("BLOCKING_TASK_TIMEOUT", "600"))  # сек

# Быстрый путь TikTok: прямая ссылка резолвера вместо yt-dlp.
# Отдаёт H.264 со звуком (576x1024), что снимает перекодирование HEVC.
# При отказе резолвера код автоматически возвращается к пути через yt-dlp.
TIKTOK_FAST_PATH = _parse_bool(os.environ.get("TIKTOK_FAST_PATH"), default=True)

# Быстрый путь Instagram: прямая ссылка из GraphQL вместо yt-dlp.
# Instagram отдаёт H.264 + AAC, поэтому перекодирование не требуется.
# При отказе GraphQL код автоматически возвращается к пути через yt-dlp.
INSTAGRAM_FAST_PATH = _parse_bool(
    os.environ.get("INSTAGRAM_FAST_PATH"), default=True
)

# yt-dlp закреплен в requirements.in и обновляется только новым образом.
YTDLP_CLI_FALLBACK = _parse_bool(os.environ.get("YTDLP_CLI_FALLBACK"), default=True)
YTDLP_CLI_TIMEOUT = int(os.environ.get("YTDLP_CLI_TIMEOUT", "900"))

# Канареечная проверка YouTube: раз в CANARY_INTERVAL_HOURS бот сам качает
# эталонный ролик и зовёт админов, если YouTube сломался. По умолчанию выключена:
# это исходящий трафик и лишние обращения к YouTube с домашнего адреса, поэтому
# включает её владелец осознанно.
CANARY_ENABLED = _parse_bool(os.environ.get("CANARY_ENABLED"), default=False)
CANARY_INTERVAL_HOURS = _parse_canary_interval_hours(
    os.environ.get("CANARY_INTERVAL_HOURS")
)
CANARY_VIDEO_ID = _parse_canary_video_id(os.environ.get("CANARY_VIDEO_ID"))

# HTTP API для внешних клиентов (`python -m api`). Ключи выдаёт админ в WebUI.
# Лимиты защищают диск и пул загрузок от одного ключа: файлы результата
# живут API_RESULT_TTL_MINUTES, а не до перезапуска.
API_PORT = _parse_int_setting("API_PORT", 8090, 1, 65535)
API_DOWNLOAD_WORKERS = _parse_int_setting("API_DOWNLOAD_WORKERS", 4, 1, 32)
API_MAX_ACTIVE_JOBS = _parse_int_setting("API_MAX_ACTIVE_JOBS", 2, 1, 50)
# API отдаёт файлы сам, без Telegram, поэтому лимиты Bot API к нему не относятся.
# Загрузчикам этот лимит передаёт задача: см. work_budget.execute.
API_MAX_FILE_SIZE_MB = _parse_int_setting("API_MAX_FILE_SIZE_MB", 2000, 1, 2000)
API_RATE_LIMIT_PER_MINUTE = _parse_int_setting("API_RATE_LIMIT_PER_MINUTE", 120, 1, 10_000)
API_RESULT_TTL_MINUTES = _parse_int_setting("API_RESULT_TTL_MINUTES", 60, 5, 1440)
# Сколько места занимают готовые файлы всех ключей вместе; сверх него старые
# результаты истекают досрочно. Идущие загрузки сверху: до
# API_DOWNLOAD_WORKERS × API_MAX_FILE_SIZE_MB.
API_STORAGE_LIMIT_MB = _parse_int_setting("API_STORAGE_LIMIT_MB", 10000, 100, 1_000_000)

# Список ID администраторов (через запятую)
# Пример: ADMIN_IDS = 123456789,987654321
ADMIN_IDS = _parse_admin_ids(os.environ.get("ADMIN_IDS"))


# Валидация критически важных переменных окружения
def validate_config():
    """Проверяет наличие всех необходимых переменных окружения."""
    missing_vars = []

    if not TELEGRAM_TOKEN:
        missing_vars.append("TELEGRAM_TOKEN")

    if missing_vars:
        error_msg = (
            f"Отсутствуют обязательные переменные окружения: {', '.join(missing_vars)}\n"
            "Пожалуйста, установите их перед запуском бота.\n"
            "Пример для Windows: set TELEGRAM_TOKEN=your_token_here\n"
            "Пример для Linux/Mac: export TELEGRAM_TOKEN=your_token_here"
        )
        raise ValueError(error_msg)

    if TELEGRAM_LOCAL_MODE:
        if not TELEGRAM_BOT_API_BASE_URL.startswith("http://"):
            raise ValueError(
                "TELEGRAM_BOT_API_BASE_URL локального сервера должен использовать http://"
            )
        if not TELEGRAM_BOT_API_FILE_URL.startswith("http://"):
            raise ValueError(
                "TELEGRAM_BOT_API_FILE_URL локального сервера должен использовать http://"
            )

    return True


def _default_secret_path(filename: str) -> Path:
    """Возвращает основной путь для приватных файлов внутри .secrets."""
    return SECRETS_DIR / filename


def resolve_secret_path(filename: str) -> Path:
    """Возвращает совместимый путь к приватному файлу.

    Предпочитает `.secrets/<filename>`, но поддерживает legacy-файл в корне проекта,
    если пользователь ещё не мигрировал локальные секреты.
    """
    preferred = _default_secret_path(filename)
    legacy = BASE_DIR / filename
    if preferred.exists():
        if legacy.exists():
            logging.warning(
                "Обнаружены canonical и legacy версии секрета %s. Используем %s и игнорируем %s",
                filename,
                preferred,
                legacy,
            )
        return preferred
    if not legacy.exists():
        return preferred
    return legacy


# Пути к файлам cookies (опционально). По умолчанию используются файлы в `.secrets/`,
# но legacy-файлы в корне проекта продолжают поддерживаться как fallback.
YOUTUBE_COOKIES_PATH = Path(
    os.environ.get(
        "YOUTUBE_COOKIES_FILE", str(resolve_secret_path("www.youtube.com_cookies.txt"))
    )
)
INSTAGRAM_COOKIES_PATH = Path(
    os.environ.get(
        "INSTAGRAM_COOKIES_FILE",
        str(resolve_secret_path("www.instagram.com_cookies.txt")),
    )
)
TIKTOK_COOKIES_PATH = Path(
    os.environ.get(
        "TIKTOK_COOKIES_FILE", str(resolve_secret_path("www.tiktok.com_cookies.txt"))
    )
)

# Строковая совместимость для youtube_utils/tests
YOUTUBE_COOKIES_FILE = str(YOUTUBE_COOKIES_PATH)
