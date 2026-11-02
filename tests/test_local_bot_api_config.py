"""Тесты конфигурации локального Telegram Bot API."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.py"
LOCAL_API_URL = "http://telegram-bot-api:8081/bot"
LOCAL_FILE_URL = "http://telegram-bot-api:8081/file/bot"


def _load_config(monkeypatch: pytest.MonkeyPatch, **env: str):
    for name in (
        "TELEGRAM_LOCAL_MODE",
        "TELEGRAM_BOT_API_BASE_URL",
        "TELEGRAM_BOT_API_FILE_URL",
        "TELEGRAM_MAX_FILE_SIZE_MB",
        "TEMP_DIR",
        "API_MAX_FILE_SIZE_MB",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    spec = importlib.util.spec_from_file_location("config_under_test", CONFIG_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_cloud_mode_keeps_50_mb_limit(monkeypatch, tmp_path):
    config = _load_config(
        monkeypatch,
        TEMP_DIR=str(tmp_path),
        TELEGRAM_MAX_FILE_SIZE_MB="2000",
    )

    assert config.TELEGRAM_LOCAL_MODE is False
    assert config.MAX_FILE_SIZE == 50 * 1024 * 1024


@pytest.mark.unit
def test_local_mode_uses_shared_directory_and_2_gb_limit(monkeypatch, tmp_path):
    config = _load_config(
        monkeypatch,
        TELEGRAM_LOCAL_MODE="true",
        TELEGRAM_BOT_API_BASE_URL=LOCAL_API_URL,
        TELEGRAM_BOT_API_FILE_URL=LOCAL_FILE_URL,
        TELEGRAM_MAX_FILE_SIZE_MB="2000",
        TEMP_DIR=str(tmp_path),
    )

    assert config.TELEGRAM_LOCAL_MODE is True
    assert config.TEMP_DIR == tmp_path.resolve()
    assert config.MAX_FILE_SIZE == 2000 * 1024 * 1024
    assert config.TELEGRAM_BOT_API_BASE_URL == LOCAL_API_URL
    assert config.TELEGRAM_BOT_API_FILE_URL == LOCAL_FILE_URL


@pytest.mark.unit
def test_local_limit_cannot_exceed_telegram_limit(monkeypatch, tmp_path):
    config = _load_config(
        monkeypatch,
        TELEGRAM_LOCAL_MODE="true",
        TELEGRAM_MAX_FILE_SIZE_MB="2500",
        TEMP_DIR=str(tmp_path),
    )

    assert config.MAX_FILE_SIZE == 2000 * 1024 * 1024


@pytest.mark.unit
def test_local_mode_rejects_cloud_api_urls(monkeypatch, tmp_path):
    config = _load_config(
        monkeypatch,
        TELEGRAM_LOCAL_MODE="true",
        TELEGRAM_BOT_API_BASE_URL="https://api.telegram.org/bot",
        TELEGRAM_BOT_API_FILE_URL="https://api.telegram.org/file/bot",
        TEMP_DIR=str(tmp_path),
    )
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", "test-token")

    with pytest.raises(ValueError, match="TELEGRAM_BOT_API_BASE_URL"):
        config.validate_config()


@pytest.mark.unit
def test_api_limit_does_not_depend_on_the_telegram_mode(monkeypatch, tmp_path):
    """API отдаёт файлы сам: облачные 50 МБ бота к нему не относятся."""
    config = _load_config(monkeypatch, TEMP_DIR=str(tmp_path))

    assert config.TELEGRAM_LOCAL_MODE is False
    assert config.API_MAX_FILE_SIZE_MB == 2000
    assert config.MAX_FILE_SIZE == 50 * 1024 * 1024


@pytest.mark.unit
def test_api_limit_leaves_the_bot_limit_alone(monkeypatch, tmp_path):
    config = _load_config(
        monkeypatch,
        API_MAX_FILE_SIZE_MB="300",
        TELEGRAM_LOCAL_MODE="true",
        TELEGRAM_BOT_API_BASE_URL=LOCAL_API_URL,
        TELEGRAM_BOT_API_FILE_URL=LOCAL_FILE_URL,
        TEMP_DIR=str(tmp_path),
    )

    assert config.API_MAX_FILE_SIZE_MB == 300
    assert config.MAX_FILE_SIZE == 2000 * 1024 * 1024


@pytest.mark.unit
@pytest.mark.parametrize(("process", "log_name"), [(None, "bot.log"), ("api", "api.log")])
def test_api_process_writes_its_own_log(monkeypatch, process, log_name):
    """Ротацию одного файла из двух процессов RotatingFileHandler не переживает."""
    if process is None:
        monkeypatch.delenv("NUVIO_PROCESS", raising=False)
    else:
        monkeypatch.setenv("NUVIO_PROCESS", process)
    path = CONFIG_PATH.parent / "utils" / "logger.py"
    spec = importlib.util.spec_from_file_location("logger_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module._LOG_FILE.name == log_name


@pytest.mark.unit
def test_api_app_import_selects_the_log_without_the_entry_point():
    """`uvicorn api.app:app` минует `__main__`, но не пакет `api`."""
    code = "; ".join([
        "import api.app",
        "from utils import logger",
        "assert logger._LOG_FILE.name == 'api.log'",
    ])
    env = {name: value for name, value in os.environ.items() if name != "NUVIO_PROCESS"}
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=CONFIG_PATH.parent, env=env,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
