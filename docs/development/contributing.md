# Contributor guide

## Local environment

```bash
git clone https://github.com/mazixs/nuvio.git
cd nuvio
python -m venv .venv
source .venv/bin/activate
python -m pip install --requirement requirements-dev.txt
```

System dependencies: Python 3.14+, FFmpeg, and git.

Edit direct dependencies in `requirements.in` and `requirements-dev.in`. Rebuild the hash-pinned lock files after a dependency change:

```bash
uv pip compile --python-version 3.14 --generate-hashes --output-file requirements.txt requirements.in
uv pip compile --python-version 3.14 --generate-hashes --output-file requirements-dev.txt requirements-dev.in
```

## Code layout

- `main.py` - entry point, handler registration, event loop, scheduled tasks.
- `config.py` - environment configuration.
- `messages.py` - centralized bot-facing text.
- `pyproject.toml` - ruff configuration.
- `utils/` - core application logic.
- `web/` - FastAPI analytics WebUI.
- `tests/` - pytest tests.
- `docs/` - documentation.

## Tests

```bash
pytest
pytest tests/test_youtube_smoke.py -v
pytest -k "test_name"
coverage run --branch -m pytest tests/
coverage report --fail-under=70
```

Pytest markers:

- `syntax` - syntax, imports, and ruff checks.
- `unit` - tests with mocked boundaries.
- `integration` - SQLite cache and CSI integration.

YouTube tests use a mocked `YoutubeDL` and make no network requests. Real cookie files are disabled during tests. Shared fixtures and hooks live in `tests/conftest.py`.

## Code conventions

### User-facing text

Keep bot-facing messages in `messages.py` rather than embedding them in handlers. Bot messages, comments, and docstrings remain Russian. Documentation is written in English, and the WebUI defaults to English with an optional Russian translation.

### Errors

- Error IDs use `PREFIX-CATEGORY-RANDOM`, for example `YT-ACCESS-A1B2C3`.
- Prefixes: `YT`, `TT`, `IG`, `RU`, `VK`, `TG`, `FILE`, and `BOT`.
- Show users a safe platform-specific explanation and an error ID. Keep tracebacks and operational details in administrative logs.
- Use `Exception.add_note()` to attach diagnostic context.
- The [error code reference](../error-codes.md) defines current categories and their eight-character IDs.

### Async work

- Run blocking yt-dlp and FFmpeg operations in `ThreadPoolExecutor`.
- `DOWNLOAD_WORKERS` defaults to 8.
- Use `match`/`case` for platform and format selection where appropriate.

### Databases

- SQLite uses WAL for concurrent access (`PRAGMA journal_mode=WAL`, `synchronous=NORMAL`, `cache_size=-64000`).
- `_cursor_read()` handles reads; `_cursor_write()` starts writes with `BEGIN IMMEDIATE`.
- `telegram_cache.db` stores Telegram file IDs; `analytics.db` stores users, events, CSI feedback, and settings.

### Logging

- Configure logging through `utils/logger.py` and `setup_logger`.
- Logs rotate at 10 MB with five backups.
- Set the level with `LOG_LEVEL`.

## Docker development

```bash
docker compose --env-file .secrets/.env \
  -f compose.yaml -f compose.dev.yaml up --build
```

The stack runs `bot` and `web`; both use the `bot-data` volume for the analytics database.
