"""
Модуль для настройки логирования.
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from config import LOG_FORMAT, BASE_DIR, LOG_LEVEL

_LOG_DIR = BASE_DIR / "logs"
# Процесс HTTP API пишет в свой файл: ротацию одного файла из двух процессов
# RotatingFileHandler не переживает, а при прямом запуске API идёт рядом с ботом.
_LOG_FILE = _LOG_DIR / ("api.log" if os.environ.get("NUVIO_PROCESS") == "api" else "bot.log")
_shared_file_handler: RotatingFileHandler | None = None


def _get_shared_file_handler() -> RotatingFileHandler:
    """Возвращает единый RotatingFileHandler для всех логгеров."""
    global _shared_file_handler
    if _shared_file_handler is None:
        _LOG_DIR.mkdir(exist_ok=True)
        _shared_file_handler = RotatingFileHandler(
            _LOG_FILE,
            maxBytes=10 * 1024 * 1024,  # 10 МБ
            backupCount=5,
            encoding="utf-8",
        )
        _shared_file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    return _shared_file_handler


def setup_logger(
    name: str, level: int | None = None, log_to_file: bool = True
) -> logging.Logger:
    """
    Настраивает и возвращает логгер с указанным именем и уровнем логирования.
    Все логгеры процесса пишут в единый файл bot.log (у HTTP API — api.log) с ротацией.

    Args:
        name: Имя логгера
        level: Уровень логирования. По умолчанию INFO.
        log_to_file: Сохранять логи в файл. По умолчанию True.

    Returns:
        Настроенный логгер
    """
    resolved_level = LOG_LEVEL if level is None else level
    logger = logging.getLogger(name)
    logger.setLevel(resolved_level)

    if logger.hasHandlers():
        return logger

    formatter = logging.Formatter(LOG_FORMAT)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    if log_to_file:
        logger.addHandler(_get_shared_file_handler())

    return logger
