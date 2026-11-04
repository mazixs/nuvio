"""Структурные тесты Compose для локального Telegram Bot API."""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.unit
def test_compose_has_local_bot_api_and_shared_media():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "telegram-bot-api:" in compose
    assert "TELEGRAM_API_ID" in compose
    assert "TELEGRAM_API_HASH" in compose
    assert "TELEGRAM_LOCAL_MODE: \"true\"" in compose
    assert "shared-media:/app/media" in compose
    assert "8081:8081" not in compose
    assert "${NUVIO_ENV_FILE:-.secrets/.env}" in compose


@pytest.mark.unit
def test_local_api_is_not_pulled_from_a_registry():
    """`docker compose pull` не должен ходить в реестр за собственным образом.

    Сервис собирается из исходников, и в реестре его нет. Без `pull_policy`
    Compose при связке `build` + `image` сначала пробует pull — он падает с
    «pull access denied», а ненулевой код возврата обрывает обновление всего
    стека, включая уже скачанный образ бота.
    """
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    bot_api_block = compose.split("  bot:")[0]

    assert "pull_policy: build" in bot_api_block


@pytest.mark.unit
def test_bot_can_read_files_uploaded_through_the_local_api():
    """Бот обязан видеть том Bot API, иначе загрузка cookies падает с «Not Found».

    В локальном режиме `getFile` возвращает не URL, а абсолютный путь на файловой
    системе Bot API. PTB решает, локальный ли это файл, простым `path.is_file()`
    у себя: не увидев файла, он считает путь ссылкой и получает от локального
    Bot API честный 404. Том нужен только на чтение — PTB копирует из него.
    """
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    bot_block = compose.split("  bot:")[1].split("  web:")[0]

    assert "telegram-bot-api-data:/var/lib/telegram-bot-api:ro" in bot_block


@pytest.mark.unit
def test_local_api_stores_files_where_the_bot_looks_for_them():
    """Путь монтирования обязан совпадать с `--dir`, который отдаёт Bot API."""
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "--dir=/var/lib/telegram-bot-api" in compose


@pytest.mark.unit
def test_local_api_source_revision_is_pinned():
    dockerfile = (ROOT / "Dockerfile.telegram-bot-api").read_text(
        encoding="utf-8"
    )

    assert "adfd7f6a8e990272851777eeb3ae0def4216f161" in dockerfile
    assert "github.com/tdlib/telegram-bot-api.git" in dockerfile


@pytest.mark.unit
def test_image_version_comes_from_the_build():
    """Версию вшивает сборка: digest из .env устаревал с первым обновлением `latest`."""
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "NUVIO_IMAGE" not in compose
    assert "ENV NUVIO_VERSION=$NUVIO_VERSION" in dockerfile
    assert "NUVIO_VERSION=${{ steps.docker_meta.outputs.version }}" in release


@pytest.mark.unit
def test_development_override_builds_nuvio_from_source():
    compose = (ROOT / "compose.dev.yaml").read_text(encoding="utf-8")

    assert "build: ." in compose
    assert "nuvio:dev" in compose


@pytest.mark.unit
def test_legacy_compose_files_are_removed():
    assert not (ROOT / "docker-compose.yml").exists()
    assert not (ROOT / "docker-compose.prod.yml").exists()
