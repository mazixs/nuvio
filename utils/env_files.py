"""Загрузка файлов окружения для точек входа бота и HTTP API.

Модуль не импортирует config: его вызывают до config, иначе тот прочитал бы
окружение без значений из файлов.
"""

from pathlib import Path

from dotenv import load_dotenv

_BASE_DIR = Path(__file__).resolve().parent.parent
# Основной файл, затем устаревшие запасные пути. Уже заданные переменные
# окружения (например, из Compose) не перезаписываются.
ENV_FILES = (
    _BASE_DIR / ".secrets" / ".env",
    _BASE_DIR / ".env.local",
    _BASE_DIR / ".env",
)


def load_env_files() -> None:
    for path in ENV_FILES:
        if path.exists():
            load_dotenv(dotenv_path=path, override=False)
