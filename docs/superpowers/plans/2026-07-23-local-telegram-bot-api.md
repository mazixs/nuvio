# Local Telegram Bot API Implementation Plan

> Archive plan. Work completed on 2026-07-23; teams, versions, and unchecked checkboxes below are preserved as development history. For installation and operation, use `README.md` and `docs/guides/deployment.md`.
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
**Goal:** Connect Nuvio to the local Telegram Bot API within a single Compose project, send files up to 2000 MB via a local path, and fully eliminate dependency on Gokapi.
**Architecture:** The `bot`, `web`, and `telegram-bot-api` components run as separate containers within one internal Compose network. The `bot` and `telegram-bot-api` containers mount a shared volume at `/app/media`, so `python-telegram-bot` in `local_mode` passes the server the absolute path instead of re-uploading the file via an external HTTP API. `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` are used only by the local server; the bot token remains in `TELEGRAM_TOKEN`.
**Tech Stack:** Python 3.14+, python-telegram-bot 22.8, Telegram Bot API 10.2, Docker Compose, pytest, ruff.

## Global Constraints

- The local Telegram Bot API is built exclusively from the official `tdlib/telegram-bot-api`, revision `adfd7f6a8e990272851777eeb3ae0def4216f161`.
- Port `8081` is not exposed to the host and is accessible only within the Compose project.
- Maximum file size in local mode: `2000` MB; in cloud mode: `50` MB.
- Working configuration file: `.secrets/.env`, template: `.env.example`; the root `.env` is not used by Compose.
- `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_TOKEN`, cookies, and passwords are not stored in images and are not logged.
- A single token cannot be used simultaneously through both cloud and local Bot API; migration requires manual `logOut`.
- User messages do not reference cookies, `api_hash`, internal containers, or Gokapi.
- The implementation does not add automatic fallback back to the cloud Bot API.

---

### Task 1: Local Bot API Configuration

**Files:**
- Modify: `config.py`
- Create: `tests/test_local_bot_api_config.py`
**Interfaces:**
- Consumes: environment variables `TELEGRAM_LOCAL_MODE`, `TELEGRAM_BOT_API_BASE_URL`, `TELEGRAM_BOT_API_FILE_URL`, `TELEGRAM_MAX_FILE_SIZE_MB`, `TEMP_DIR`.
- Produces: `TELEGRAM_LOCAL_MODE: bool`, `TELEGRAM_BOT_API_BASE_URL: str`, `TELEGRAM_BOT_API_FILE_URL: str`, `MAX_FILE_SIZE: int`, `TEMP_DIR: Path`.
- [ ] **Step 1: Write the failing configuration tests**

```python
import importlib.util
import os
from pathlib import Path


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.py"


def load_config(monkeypatch, **env):
    for name in (
        "TELEGRAM_LOCAL_MODE",
        "TELEGRAM_BOT_API_BASE_URL",
        "TELEGRAM_BOT_API_FILE_URL",
        "TELEGRAM_MAX_FILE_SIZE_MB",
        "TEMP_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    spec = importlib.util.spec_from_file_location("config_under_test", CONFIG_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cloud_mode_keeps_telegram_limit(monkeypatch, tmp_path):
    config = load_config(
        monkeypatch,
        TEMP_DIR=str(tmp_path),
        TELEGRAM_MAX_FILE_SIZE_MB="2000",
    )
    assert config.TELEGRAM_LOCAL_MODE is False
    assert config.MAX_FILE_SIZE == 50 * 1024 * 1024


def test_local_mode_uses_shared_directory_and_2gb_limit(monkeypatch, tmp_path):
    config = load_config(
        monkeypatch,
        TELEGRAM_LOCAL_MODE="true",
        TELEGRAM_BOT_API_BASE_URL="http://telegram-bot-api:8081/bot",
        TELEGRAM_BOT_API_FILE_URL="http://telegram-bot-api:8081/file/bot",
        TELEGRAM_MAX_FILE_SIZE_MB="2000",
        TEMP_DIR=str(tmp_path),
    )
    assert config.TELEGRAM_LOCAL_MODE is True
    assert config.TEMP_DIR == tmp_path
    assert config.MAX_FILE_SIZE == 2000 * 1024 * 1024


def test_local_limit_cannot_exceed_telegram_limit(monkeypatch, tmp_path):
    config = load_config(
        monkeypatch,
        TELEGRAM_LOCAL_MODE="true",
        TELEGRAM_MAX_FILE_SIZE_MB="2500",
        TEMP_DIR=str(tmp_path),
    )
    assert config.MAX_FILE_SIZE == 2000 * 1024 * 1024
```

- [ ] **Step 2: Run the tests and verify the missing behavior**
Run:

```bash
.venv/bin/pytest tests/test_local_bot_api_config.py -v
```

Expected: FAIL because `TELEGRAM_LOCAL_MODE` and configurable `TEMP_DIR` are not defined.
- [ ] **Step 3: Implement the configuration**
Replace the fixed temporary directory and file limit in `config.py` with:

```python
BASE_DIR = Path(__file__).parent
TEMP_DIR = Path(os.environ.get("TEMP_DIR", str(BASE_DIR / "temp"))).resolve()
TEMP_DIR.mkdir(parents=True, exist_ok=True)
SECRETS_DIR = BASE_DIR / ".secrets"
SECRETS_DIR.mkdir(exist_ok=True)

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
```

Extend `validate_config()`:

```python
if TELEGRAM_LOCAL_MODE:
    if not TELEGRAM_BOT_API_BASE_URL.startswith("http://"):
        raise ValueError(
            "TELEGRAM_BOT_API_BASE_URL локального сервера должен использовать http://"
        )
    if not TELEGRAM_BOT_API_FILE_URL.startswith("http://"):
        raise ValueError(
            "TELEGRAM_BOT_API_FILE_URL локального сервера должен использовать http://"
        )
```

- [ ] **Step 4: Run the focused tests**
Run:

```bash
.venv/bin/pytest tests/test_local_bot_api_config.py -v
```

Expected: 3 passed.
- [ ] **Step 5: Commit the configuration**

```bash
git add config.py tests/test_local_bot_api_config.py
git commit -m "feat: добавить конфигурацию локального Bot API"
```

---

### Task 2: Configure python-telegram-bot

**Files:**
- Modify: `main.py`
- Create: `tests/test_local_bot_api_application.py`
**Interfaces:**
- Consumes: constants from Task 1.
- Produces: `_configure_application_builder(builder: ApplicationBuilder) -> ApplicationBuilder`.
- [ ] **Step 1: Write failing builder tests**

```python
from unittest.mock import MagicMock

import main


def test_cloud_builder_does_not_override_api_urls(monkeypatch):
    builder = MagicMock()
    builder.configure_mock(**{
        "token.return_value": builder,
        "connect_timeout.return_value": builder,
        "read_timeout.return_value": builder,
        "write_timeout.return_value": builder,
        "get_updates_connect_timeout.return_value": builder,
        "get_updates_read_timeout.return_value": builder,
        "get_updates_write_timeout.return_value": builder,
        "get_updates_pool_timeout.return_value": builder,
        "http_version.return_value": builder,
        "get_updates_http_version.return_value": builder,
        "media_write_timeout.return_value": builder,
        "base_url.return_value": builder,
        "base_file_url.return_value": builder,
        "local_mode.return_value": builder,
    })
    monkeypatch.setattr(main, "TELEGRAM_LOCAL_MODE", False)

    main._configure_application_builder(builder)

    builder.local_mode.assert_not_called()
    builder.base_url.assert_not_called()
    builder.base_file_url.assert_not_called()


def test_local_builder_uses_internal_api(monkeypatch):
    builder = MagicMock()
    for method in (
        "token", "connect_timeout", "read_timeout", "write_timeout",
        "get_updates_connect_timeout", "get_updates_read_timeout",
        "get_updates_write_timeout", "get_updates_pool_timeout",
        "http_version", "get_updates_http_version", "media_write_timeout",
        "base_url", "base_file_url", "local_mode",
    ):
        getattr(builder, method).return_value = builder
    monkeypatch.setattr(main, "TELEGRAM_LOCAL_MODE", True)
    monkeypatch.setattr(
        main, "TELEGRAM_BOT_API_BASE_URL", "http://telegram-bot-api:8081/bot"
    )
    monkeypatch.setattr(
        main, "TELEGRAM_BOT_API_FILE_URL", "http://telegram-bot-api:8081/file/bot"
    )

    main._configure_application_builder(builder)

    builder.base_url.assert_called_once_with("http://telegram-bot-api:8081/bot")
    builder.base_file_url.assert_called_once_with(
        "http://telegram-bot-api:8081/file/bot"
    )
    builder.local_mode.assert_called_once_with(True)
    builder.media_write_timeout.assert_called_once_with(1800.0)
```

- [ ] **Step 2: Verify the helper is absent**
Run:

```bash
.venv/bin/pytest tests/test_local_bot_api_application.py -v
```

Expected: FAIL with `AttributeError: module 'main' has no attribute '_configure_application_builder'`.
- [ ] **Step 3: Extract and implement builder configuration**
Import the new constants in `main.py` and add:

```python
def _configure_application_builder(builder):
    builder = (
        builder.token(TELEGRAM_TOKEN)
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
```

Change `_build_application()`:

```python
application = _configure_application_builder(Application.builder()).build()
```

- [ ] **Step 4: Run application and polling tests**
Run:

```bash
.venv/bin/pytest tests/test_local_bot_api_application.py tests/test_main_polling.py -v
```

Expected: all tests pass.
- [ ] **Step 5: Commit client integration**

```bash
git add main.py tests/test_local_bot_api_application.py
git commit -m "feat: направить бота в локальный Telegram API"
```

---

### Task 3: Local file delivery and removal of Gokapi

**Files:**
- Modify: `utils/ytdlp_common.py`
- Modify: `utils/telegram_utils.py`
- Modify: `utils/tiktok_instagram_utils.py`
- Modify: `utils/youtube_utils.py`
- Modify: `utils/rutube_vk_utils.py`
- Delete: `utils/gokapi_utils.py`
- Modify: `messages.py`
- Create: `tests/test_local_file_delivery.py`
- Modify: `tests/test_audit_regressions.py`
**Interfaces:**
- Consumes: `MAX_FILE_SIZE`, `Path`.
- Produces: `FileSizeLimitError`, `finalize_downloaded_file(...) -> Path`, directly sends the `Path` to Telegram methods.
- [ ] **Step 1: Write failing delivery tests**

```python
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from utils import telegram_utils, ytdlp_common


def test_finalize_returns_local_path_below_limit(monkeypatch, tmp_path):
    media = tmp_path / "video.mp4"
    media.write_bytes(b"x" * 10)
    monkeypatch.setattr(ytdlp_common, "MAX_FILE_SIZE", 20)
    assert ytdlp_common.finalize_downloaded_file(media, False) == media


def test_finalize_deletes_and_rejects_file_above_limit(monkeypatch, tmp_path):
    media = tmp_path / "video.mp4"
    media.write_bytes(b"x" * 21)
    monkeypatch.setattr(ytdlp_common, "MAX_FILE_SIZE", 20)
    with pytest.raises(ytdlp_common.FileSizeLimitError):
        ytdlp_common.finalize_downloaded_file(media, False)
    assert not media.exists()


def test_send_single_file_passes_path_to_local_api(monkeypatch, tmp_path):
    media = tmp_path / "video.mp4"
    media.write_bytes(b"video")
    sent_message = SimpleNamespace(
        video=None, audio=None, document=None
    )
    query = SimpleNamespace(
        message=SimpleNamespace(reply_video=AsyncMock(return_value=sent_message)),
        edit_message_text=AsyncMock(),
    )
    monkeypatch.setattr(telegram_utils, "TELEGRAM_LOCAL_MODE", True)

    result = asyncio.run(
        telegram_utils.send_single_file(
            query,
            media,
            "session-token",
            {"platform": "tiktok", "url": "https://example.test/video"},
            max_retries=1,
        )
    )

    assert result is True
    assert query.message.reply_video.await_args.kwargs["video"] == media
```

- [ ] **Step 2: Verify the old Gokapi path fails the tests**
Run:

```bash
.venv/bin/pytest tests/test_local_file_delivery.py -v
```

Expected: the oversized-file test attempts Gokapi and the send test receives an opened file object instead of `Path`.
- [ ] **Step 3: Replace Gokapi finalization**
In `utils/ytdlp_common.py`, remove the Gokapi import and add:

```python
class FileSizeLimitError(Exception):
    """Файл превышает предел выбранного Telegram Bot API."""


def finalize_downloaded_file(downloaded_file: Path, force_local: bool) -> Path:
    file_size = downloaded_file.stat().st_size
    if force_local or file_size <= MAX_FILE_SIZE:
        return downloaded_file

    try:
        raise FileSizeLimitError(
            f"Файл превышает допустимый размер: {file_size} > {MAX_FILE_SIZE}"
        )
    finally:
        downloaded_file.unlink(missing_ok=True)
```

- [ ] **Step 4: Send local paths and remove URL delivery**
Import `TELEGRAM_LOCAL_MODE` in `utils/telegram_utils.py`. In `send_single_file`, use:

```python
telegram_file = file_path if TELEGRAM_LOCAL_MODE else file_path.open("rb")
try:
    if file_ext in [".mp4", ".webm", ".mkv", ".avi", ".mov"]:
        message = await query.message.reply_video(
            video=telegram_file,
            caption=None,
            supports_streaming=True,
            write_timeout=1800,
            read_timeout=1800,
        )
    elif file_ext in [".mp3", ".m4a", ".wav", ".ogg"]:
        message = await query.message.reply_audio(
            audio=telegram_file,
            caption=None,
            write_timeout=1800,
            read_timeout=1800,
        )
    else:
        message = await query.message.reply_document(
            document=telegram_file,
            caption=None,
            write_timeout=1800,
            read_timeout=1800,
        )
finally:
    if hasattr(telegram_file, "close"):
        telegram_file.close()
```

Change `send_file` to accept only `Path` and delete its HTTP-link branch.
- [ ] **Step 5: Remove all Gokapi-dependent branches**
Import `FileSizeLimitError` from `utils.ytdlp_common` in
`utils/tiktok_instagram_utils.py` and replace the preliminary check:

```python
if cached_info and not force_local:
    filesize = cached_info.get("filesize") or cached_info.get("filesize_approx", 0)
    if filesize and filesize > MAX_FILE_SIZE:
        raise FileSizeLimitError(
            f"Файл превышает допустимый размер {MAX_FILE_SIZE // 1024 // 1024} МБ"
        )
```

In `utils/telegram_utils.py`, always use:

```python
best_label = BEST_QUALITY_LABEL
```

Delete `utils/gokapi_utils.py`, Gokapi imports,
`_classify_large_file_delivery_error`, its tests and Gokapi-specific
messages. Update return annotations and docstrings in
`utils/youtube_utils.py`, `utils/tiktok_instagram_utils.py` and
`utils/rutube_vk_utils.py` so download functions return a local `Path`, not
an external URL.
- [ ] **Step 6: Run delivery and regression tests**
Run:

```bash
.venv/bin/pytest tests/test_local_file_delivery.py tests/test_audit_regressions.py -v
```

Expected: all tests pass and `rg -n "gokapi|Gokapi|GOKAPI" --glob '*.py'` returns no matches.
- [ ] **Step 7: Commit local file delivery**

```bash
git add utils/ytdlp_common.py utils/telegram_utils.py utils/tiktok_instagram_utils.py utils/youtube_utils.py utils/rutube_vk_utils.py messages.py tests/test_local_file_delivery.py tests/test_audit_regressions.py
git add -u utils/gokapi_utils.py
git commit -m "feat: отправлять большие файлы через локальный Bot API"
```

---

### Task 4: Create Telegram Bot API and unified Compose project

**Files:**
- Create: `Dockerfile.telegram-bot-api`
- Create: `compose.yaml`
- Create: `compose.dev.yaml`
- Delete: `docker-compose.yml`
- Delete: `docker-compose.prod.yml`
- Modify: `.dockerignore`
- Modify: `.gitignore`
- Create: `tests/test_compose_configuration.py`
**Interfaces:**
- Consumes: `.secrets/.env`, official Telegram Bot API source code.
- Produces: service `telegram-bot-api`, volumes `bot-data`, `telegram-bot-api-data`, `shared-media`.
- [ ] **Step 1: Write failing structural tests**

```python
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_compose_has_local_bot_api_and_shared_media():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "telegram-bot-api:" in compose
    assert "TELEGRAM_API_ID" in compose
    assert "TELEGRAM_API_HASH" in compose
    assert "shared-media:/app/media" in compose
    assert "8081:8081" not in compose
    assert "env_file:" in compose
    assert ".secrets/.env" in compose


def test_local_api_source_revision_is_pinned():
    dockerfile = (ROOT / "Dockerfile.telegram-bot-api").read_text(encoding="utf-8")
    assert "adfd7f6a8e990272851777eeb3ae0def4216f161" in dockerfile
    assert "github.com/tdlib/telegram-bot-api.git" in dockerfile
```

- [ ] **Step 2: Verify infrastructure files are absent**
Run:

```bash
.venv/bin/pytest tests/test_compose_configuration.py -v
```

Expected: FAIL because `compose.yaml` and `Dockerfile.telegram-bot-api` do not exist.
- [ ] **Step 3: Add the pinned multi-stage image**
Create `Dockerfile.telegram-bot-api`:

```dockerfile
FROM debian:bookworm-slim AS builder

ARG TELEGRAM_BOT_API_COMMIT=adfd7f6a8e990272851777eeb3ae0def4216f161

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates cmake g++ git gperf make libssl-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone --recursive https://github.com/tdlib/telegram-bot-api.git . \
    && git checkout "${TELEGRAM_BOT_API_COMMIT}" \
    && git submodule update --init --recursive
RUN cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build build --target telegram-bot-api --parallel 2

FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates libssl3 libstdc++6 netcat-openbsd zlib1g \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /src/build/telegram-bot-api /usr/local/bin/telegram-bot-api
RUN mkdir -p /var/lib/telegram-bot-api/temp /app/media

WORKDIR /var/lib/telegram-bot-api
EXPOSE 8081
ENTRYPOINT ["telegram-bot-api"]
```

- [ ] **Step 4: Add common and development Compose files**
Create `compose.yaml` with:

```yaml
services:
  telegram-bot-api:
    build:
      context: .
      dockerfile: Dockerfile.telegram-bot-api
    image: nuvio-telegram-bot-api:10.2
    restart: unless-stopped
    env_file:
      - ${NUVIO_ENV_FILE:-.secrets/.env}
    environment:
      TELEGRAM_API_ID: ${TELEGRAM_API_ID:?TELEGRAM_API_ID is required}
      TELEGRAM_API_HASH: ${TELEGRAM_API_HASH:?TELEGRAM_API_HASH is required}
    command:
      - --local
      - --http-port=8081
      - --dir=/var/lib/telegram-bot-api
      - --temp-dir=/var/lib/telegram-bot-api/temp
    expose:
      - "8081"
    volumes:
      - telegram-bot-api-data:/var/lib/telegram-bot-api
      - shared-media:/app/media
    healthcheck:
      test: ["CMD", "nc", "-z", "127.0.0.1", "8081"]
      interval: 10s
      timeout: 3s
      retries: 12
      start_period: 20s

  bot:
    image: ghcr.io/mazixs/nuvio:${TAG:-latest}
    restart: unless-stopped
    env_file:
      - ${NUVIO_ENV_FILE:-.secrets/.env}
    environment:
      DATA_DIR: /app/data
      TEMP_DIR: /app/media
      TELEGRAM_LOCAL_MODE: "true"
      TELEGRAM_BOT_API_BASE_URL: http://telegram-bot-api:8081/bot
      TELEGRAM_BOT_API_FILE_URL: http://telegram-bot-api:8081/file/bot
      TELEGRAM_MAX_FILE_SIZE_MB: "2000"
    depends_on:
      telegram-bot-api:
        condition: service_healthy
    volumes:
      - bot-data:/app/data
      - shared-media:/app/media
      - ./logs:/app/logs
      - ./.secrets:/app/.secrets

  web:
    image: ghcr.io/mazixs/nuvio:${TAG:-latest}
    restart: unless-stopped
    command: python -m web.app
    env_file:
      - ${NUVIO_ENV_FILE:-.secrets/.env}
    environment:
      DATA_DIR: /app/data
      WEB_PORT: "8080"
    ports:
      - "${WEB_PORT:-8080}:8080"
    volumes:
      - bot-data:/app/data

volumes:
  bot-data:
  telegram-bot-api-data:
  shared-media:
```

Create `compose.dev.yaml`:

```yaml
services:
  bot:
    build: .
    image: nuvio:dev
  web:
    build: .
    image: nuvio:dev
```

Retain the existing log rotation and WebUI healthcheck blocks when moving the services. Delete the two obsolete `docker-compose*.yml` files.
- [ ] **Step 5: Validate structure and Compose rendering**
Run:

```bash
.venv/bin/pytest tests/test_compose_configuration.py -v
NUVIO_COMPOSE_TEST_ENV=$(mktemp .secrets/.env.compose-test.XXXXXX)
cp .env.example "$NUVIO_COMPOSE_TEST_ENV"
sed -i \
  -e 's/^TELEGRAM_TOKEN=.*/TELEGRAM_TOKEN=123456:test-token/' \
  -e 's/^ADMIN_IDS=.*/ADMIN_IDS=123456/' \
  -e 's/^TELEGRAM_API_ID=.*/TELEGRAM_API_ID=12345/' \
  -e 's/^TELEGRAM_API_HASH=.*/TELEGRAM_API_HASH=test-api-hash/' \
  "$NUVIO_COMPOSE_TEST_ENV"
NUVIO_ENV_FILE="$NUVIO_COMPOSE_TEST_ENV" \
  docker compose --env-file "$NUVIO_COMPOSE_TEST_ENV" config --quiet
rm "$NUVIO_COMPOSE_TEST_ENV"
```

Expected: tests pass and Compose exits with code 0. The uniquely named
temporary environment file is removed after the check.
- [ ] **Step 6: Build both images**
Run:

```bash
docker compose -f compose.yaml -f compose.dev.yaml build bot telegram-bot-api
```

Expected: both images build successfully; `telegram-bot-api --help` is available in its image.
- [ ] **Step 7: Commit infrastructure**

```bash
git add Dockerfile.telegram-bot-api compose.yaml compose.dev.yaml .dockerignore .gitignore tests/test_compose_configuration.py
git add -u docker-compose.yml docker-compose.prod.yml
git commit -m "feat: добавить локальный Telegram Bot API в Compose"
```

---

### Task 5: Environment Template and Migration Instructions

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `docs/guides/configuration.md`
- Modify: `docs/guides/deployment.md`
- Modify: `docs/technical/architecture.md`
- Modify: `docs/technical/fsm-architecture.md`
- Modify: `docs/troubleshooting/common-issues.md`
- Modify: `docs/error-codes.md`
- Modify: `docs/PRD.md`
- Modify: `AGENTS.md`
- Modify: `CLAUDE.md`
- Modify: `.github/workflows/release.yml`
- Modify: `.github/workflows/ci.yml`
- Create: `tests/test_environment_template.py`
**Interfaces:**
- Consumes: Compose and variables from previous tasks.
- Produces: a single reproducible setup path and a manual migration procedure.
- [ ] **Step 1: Write failing environment-template test**

```python
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_environment_template_contains_local_api_secrets_without_gokapi():
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "TELEGRAM_API_ID=" in template
    assert "TELEGRAM_API_HASH=" in template
    assert "TELEGRAM_TOKEN=" in template
    assert "ADMIN_IDS=" in template
    assert "GOKAPI_" not in template
    assert "python-dotenv" not in template
```

- [ ] **Step 2: Verify the old template fails**
Run:

```bash
.venv/bin/pytest tests/test_environment_template.py -v
```

Expected: FAIL because the local API credentials are absent and Gokapi remains.
- [ ] **Step 3: Replace the template**
The required beginning of `.env.example` becomes:

```dotenv
# Токен бота от @BotFather
TELEGRAM_TOKEN=

# ID администраторов через запятую
ADMIN_IDS=

# Учётные данные приложения Telegram с https://my.telegram.org
# Это не токен бота. Они нужны контейнеру локального Telegram Bot API.
TELEGRAM_API_ID=
TELEGRAM_API_HASH=

# Версия образа Nuvio из GHCR
TAG=latest
```

Keep the WebUI, cookies, worker and yt-dlp settings. Remove all Gokapi and obsolete `python-dotenv` instructions.
- [ ] **Step 4: Document first launch and migration**
Use these exact operational commands in `docs/guides/deployment.md`:

```bash
mkdir -p .secrets
cp .env.example .secrets/.env

# Остановить текущий Compose-проект до переключения токена
docker compose down

# Один раз отключить токен от облачного Bot API
curl --fail --silent --show-error \
  "https://api.telegram.org/bot${TELEGRAM_TOKEN}/logOut"

# Запустить локальный Bot API и Nuvio
docker compose --env-file .secrets/.env up -d --build
docker compose ps
docker compose logs --tail=100 telegram-bot-api bot
```

Add an explicit warning that `logOut` causes planned downtime and must not run
automatically. Update all old Compose filenames, `.env` copy commands, Gokapi
descriptions and architecture diagrams in README, project instruction files,
PRD, troubleshooting, CI and release workflow.
- [ ] **Step 5: Run documentation and configuration checks**
Run:

```bash
.venv/bin/pytest tests/test_environment_template.py tests/test_utils.py -v
rg -n "GOKAPI|Gokapi|docker-compose\\.prod\\.yml|cp \\.env\\.example \\.env" \
  README.md docs --glob '!docs/superpowers/**' .github .env.example \
  compose.yaml compose.dev.yaml AGENTS.md CLAUDE.md
```

Expected: tests pass; the search returns no obsolete operational instructions.
- [ ] **Step 6: Commit configuration documentation**

```bash
git add .env.example README.md AGENTS.md CLAUDE.md docs/guides/configuration.md docs/guides/deployment.md docs/technical/architecture.md docs/technical/fsm-architecture.md docs/troubleshooting/common-issues.md docs/error-codes.md docs/PRD.md .github/workflows/release.yml .github/workflows/ci.yml tests/test_environment_template.py
git commit -m "docs: описать миграцию на локальный Bot API"
```

---

### Task 6: Full verification and release preparation

**Files:**
- Modify only files that are specifically required by concrete failures identified in this task.
**Interfaces:**
- Consumes: complete implementation.
- Produces: verified release candidate.
- [ ] **Step 1: Run formatting and static checks**
Run:

```bash
.venv/bin/ruff check .
git diff --check
```

Expected: both commands exit with code 0.
- [ ] **Step 2: Run the full test suite**
Run:

```bash
.venv/bin/pytest -v
```

Expected: all tests pass; network and slow tests remain skipped unless explicitly enabled.
- [ ] **Step 3: Validate Compose using a temporary non-secret environment**
Create a uniquely named temporary file under `.secrets/` from `.env.example`,
set syntactically valid test values for `TELEGRAM_TOKEN`, `ADMIN_IDS`,
`TELEGRAM_API_ID`, `TELEGRAM_API_HASH` and run:

```bash
NUVIO_COMPOSE_TEST_ENV=$(mktemp .secrets/.env.compose-test.XXXXXX)
cp .env.example "$NUVIO_COMPOSE_TEST_ENV"
sed -i \
  -e 's/^TELEGRAM_TOKEN=.*/TELEGRAM_TOKEN=123456:test-token/' \
  -e 's/^ADMIN_IDS=.*/ADMIN_IDS=123456/' \
  -e 's/^TELEGRAM_API_ID=.*/TELEGRAM_API_ID=12345/' \
  -e 's/^TELEGRAM_API_HASH=.*/TELEGRAM_API_HASH=test-api-hash/' \
  "$NUVIO_COMPOSE_TEST_ENV"
NUVIO_ENV_FILE="$NUVIO_COMPOSE_TEST_ENV" \
  docker compose --env-file "$NUVIO_COMPOSE_TEST_ENV" config --quiet
NUVIO_ENV_FILE="$NUVIO_COMPOSE_TEST_ENV" \
  docker compose --env-file "$NUVIO_COMPOSE_TEST_ENV" \
  -f compose.yaml -f compose.dev.yaml build
rm "$NUVIO_COMPOSE_TEST_ENV"
```

Expected: Compose validation and both image builds succeed. Delete only the
uniquely named temporary file after the check.
- [ ] **Step 4: Inspect the final change set**
Run:

```bash
git status --short
git diff --stat v1.2.2..HEAD
rg -n "GOKAPI|Gokapi|GOKAPI" --glob '!docs/superpowers/**' .
```

Expected: no uncommitted implementation files and no live Gokapi references.
- [ ] **Step 5: Perform deployment acceptance outside CI**
After the operator provides real credentials and manually completes `logOut`:

```bash
docker compose --env-file .secrets/.env up -d
docker compose ps
docker compose logs --tail=100 telegram-bot-api bot
```

Acceptance:
- `telegram-bot-api` and `bot` are healthy/running;
- the bot receives a command;
- a file smaller than 50 MB is delivered;
- a file between 60 and 100 MB is delivered without Gokapi;
- restart preserves service state and cached Telegram `file_id`;
- port 8081 is not listed in `docker compose port telegram-bot-api 8081`;
- [ ] **Step 6: Create the patch release only after acceptance**

```bash
git tag -a v1.2.3 -m "Nuvio v1.2.3"
git push origin main
git push origin v1.2.3
```

Expected: release workflow publishes `ghcr.io/mazixs/nuvio:1.2.3` and updates `latest`.
