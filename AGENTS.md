# AGENTS.md - Nuvio

Guidance for AI agents working in the Nuvio repository. Documentation is written in English. Bot messages, code comments, and docstrings remain in Russian; code identifiers remain in English. The WebUI opens in English by default and offers Russian as an option. The root README has English and Russian versions.

## Project overview

Nuvio is an asynchronous Telegram bot that downloads video, photo posts, and audio from YouTube, TikTok, Instagram, Rutube, and VK Video. It caches Telegram `file_id` values for fast repeat delivery, records user analytics in a WebUI, and updates yt-dlp through pinned dependencies and new images.

Features include YouTube videos and Shorts; TikTok and Instagram videos, reels, photos, and carousels; Rutube and VK Video; MP3 192k extraction with FFmpeg; a 90-day SQLite `file_id` cache; files up to 2 GB through the local Telegram Bot API; temporary-media cleanup; spam protection; administrator cache commands; CSI survey scheduling in the WebUI; and pinned yt-dlp releases.

## Technology

- Python 3.14+ and asynchronous `python-telegram-bot` 22.8.
- yt-dlp `2026.9.27.232945.dev0` nightly, pinned in `requirements.in`.
- FastAPI 0.141.1, Uvicorn, and Jinja2 for the WebUI.
- SQLite in WAL mode: `telegram_cache.db` for file IDs and `analytics.db` for analytics.
- System FFmpeg; Docker Compose with a local Telegram Bot API.
- Ruff and pytest 9.1.1.

## Repository map

| Path | Purpose |
|---|---|
| `main.py` | Bot entry point, handlers, event loop, and graceful shutdown |
| `config.py` | Environment parsing, secret paths, and validation |
| `messages.py` | Centralized bot-facing text |
| `requirements.in`, `requirements.txt` | Direct runtime dependencies and hashed lock file |
| `requirements-dev.in`, `requirements-dev.txt` | Direct development dependencies and hashed lock file |
| `Dockerfile`, `Dockerfile.telegram-bot-api` | Nuvio and local Bot API images |
| `compose.yaml`, `compose.dev.yaml` | Published-image and source-build stacks |
| `init_env.sh` | Headless systemd bootstrap and secret migration |
| `utils/telegram_utils.py`, `utils/callback_fsm.py` | Bot flow, callbacks, delivery, and cancellation |
| `utils/youtube_utils.py`, `utils/tiktok_instagram_utils.py`, `utils/rutube_vk_utils.py` | Platform downloaders |
| `utils/media_processor.py` | FFmpeg conversion, extraction, and compression |
| `utils/video_cache.py`, `utils/analytics_db.py` | SQLite cache and analytics |
| `utils/ytdlp_runtime.py`, `utils/cookie_manager.py`, `utils/cookie_health.py` | yt-dlp runtime and cookie management |
| `utils/logger.py`, `utils/cache_commands.py`, `utils/temp_file_manager.py` | Logging, cache commands, and temporary files |
| `web/` | FastAPI application, templates, static assets, and localization |
| `tests/` | Unit, integration, smoke, and regression tests |
| `docs/` | English guides, technical notes, audits, and archived plans |
| `scripts/release_notes.py` | GitHub Release changelog generation |
| `.github/workflows/ci.yml`, `.github/workflows/release.yml` | CI and release pipelines |

The documentation index is `docs/index.md`. The archived plans in `docs/superpowers/` describe past work and may contain historical commands or versions.

## Setup and operation

Create a virtual environment, install development dependencies, and copy the environment template:

```bash
git clone https://github.com/mazixs/nuvio.git
cd nuvio
python -m venv .venv
source .venv/bin/activate
python -m pip install --requirement requirements-dev.txt
mkdir -p .secrets
cp .env.example .secrets/.env
```

Set `TELEGRAM_TOKEN` and `ADMIN_IDS` for a direct run. Start the bot with `python main.py` and the WebUI in a separate process with `python -m web`. Direct runs use the cloud Bot API and a 50 MB file limit.

For the Docker stack, also set `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`:

```bash
# Build Nuvio from the current checkout
docker compose --env-file .secrets/.env -f compose.yaml -f compose.dev.yaml up -d --build

# Use the published Nuvio image from GHCR
docker compose --env-file .secrets/.env up -d
```

Docker delivery supports files up to 2 GB. FFmpeg is required. Deno 2.3+ provides the full YouTube format set and is included in the Docker image. `init_env.sh` and release operations also need Git.

## Tests and style

```bash
pytest
pytest -v
pytest -k "test_name"
pytest tests/test_youtube_smoke.py -v
coverage run --branch -m pytest tests/
coverage report --fail-under=70
ruff check --output-format=github .
```

Pytest markers `syntax`, `unit`, and `integration` require no extra flag. YouTube smoke tests mock `YoutubeDL` and do not make network requests. Tests disable real cookies with monkeypatch. Fixtures and hooks are in `tests/conftest.py`, which also provides a yt-dlp stub where the package is unavailable.

Keep all bot-facing messages in `messages.py`; do not hardcode them in handlers. Write bot messages, comments, and docstrings in Russian. Write documentation in English and keep the English WebUI translation complete. Identifiers for variables, functions, and classes remain English. Production code must not call `print()`; `test_no_print_statements` enforces this.
Write commit messages in English.

Public documentation may identify the repository owner as `mazixs` and use `mazix@bk.ru` as the owner's contact address. Replace other personal contact details, real user-submitted media links and identifiers from investigations, and private deployment addresses, hostnames, and filesystem paths with neutral placeholders. Keep official service URLs, documented local or Compose endpoints, and the openly licensed reference media configured by the application when they are needed to follow an instruction.

### Error handling

- Error IDs follow `PREFIX-CATEGORY-RANDOM`, for example `YT-ACCESS-A1B2C3`.
- Prefixes are `YT`, `TT`, `IG`, `RU`, `VK`, `TG`, `FILE`, and `BOT`.
- Categories and their eight-character abbreviations are listed in `docs/error-codes.md`. If the cause is unconfirmed, use `UNEXPECT` and include the exception type in the log. Do not create new `UNKNOWN` codes.
- Users see a safe, platform-specific explanation and the error ID. Cookie state, internal addresses, container details, and tracebacks belong only in administrator logs and reports.
- Use `Exception.add_note()` to attach diagnostic context.

### Asynchronous work

The bot uses `python-telegram-bot` asynchronously. yt-dlp and FFmpeg run in a `ThreadPoolExecutor`. `DOWNLOAD_WORKERS` defaults to 8 and `BLOCKING_TASK_TIMEOUT` to 600 seconds.

`UPDATE_CONCURRENCY = 32` in `main.py` exceeds the worker count so cancellation callbacks and a second URL can be handled during a download. Do not keep the update fetcher occupied with long blocking work. Use `run_blocking(..., session_id=...)` for long tasks and `context.application.create_task` for background tasks.

### Logging, databases, and configuration

`utils/logger.py` configures one rotating `logs/bot.log` file (10 MB, five backups). `LOG_LEVEL` controls verbosity. SQLite uses `PRAGMA journal_mode=WAL` for concurrent access. Parameterize every SQL query with `?`; do not concatenate SQL input.

Parse settings in `config.py`. Prefer secrets in `.secrets/`; `resolve_secret_path()` keeps the legacy repository-root fallback. `telegram_cache.db` stores file IDs, and `analytics.db` stores users, events, and metrics.

## Security

- Parameterize SQL queries.
- Compare WebUI credentials in constant time, including `hmac.compare_digest`.
- Block repeated WebUI sign-in attempts by IP using `FAIL2BAN_RETRIES` and `FAIL2BAN_TIME`.
- Limit entered usernames and passwords to 128 characters.
- Sign sessions through `SessionMiddleware` with `WEB_SECRET_KEY`.
- Keep Swagger and ReDoc disabled; let Jinja2 escape HTML.
- Do not expose database structure or tracebacks to users.
- Save cookie files with owner-only `0o600` permissions.

## CI and releases

`.github/workflows/ci.yml` runs on pushes and PRs to `main` and `develop`, plus `workflow_dispatch` for yt-dlp update PRs created with `GITHUB_TOKEN`. It runs actionlint and Ruff, the full test suite on Python 3.14 with at least 70% coverage, then a Buildx Docker build with GHA cache, a Trivy check for fixable HIGH/CRITICAL vulnerabilities, and smoke checks of Nuvio and the local Bot API.

`.github/workflows/release.yml` runs on `v*` tags. It verifies the semver tag belongs to `main`, repeats lint/tests/coverage, publishes a canonical GHCR digest with SBOM and provenance, validates that digest with Trivy and smoke checks, and only then assigns `latest`, `major.minor`, and `major` tags. Pre-releases do not receive `latest` because `compose.yaml` defaults to `${TAG:-latest}`. `scripts/release_notes.py` assigns each commit to one changelog section. GitHub Release is created only after image publication succeeds.

## Architectural rules

1. Blocking yt-dlp and FFmpeg work stays outside the event loop.
2. Callback buttons use session tokens of the form `s|{token}|{scope}|{action}`, with at most five active sessions per user.
3. Save the Telegram `file_id` after first delivery; subsequent equivalent requests can use the cached ID.
4. Retry network timeouts with exponential backoff. Use the `python -m yt_dlp` CLI fallback when the embedded API fails.
5. YouTube attempts start **without cookies**, then try cookies. Authentication can remove the token-free `android_vr` client. Instagram follows the same order; TikTok tries cookies first.
6. TikTok `/photo/` links and Instagram carousels produce image sets, with separate audio when available.
7. Pin yt-dlp in `requirements.in` and the image. `scripts/update_ytdlp.py` and a new image release perform updates.
8. With `CANARY_ENABLED=true`, `utils/canary.py` periodically downloads a reference YouTube video through the real path and bypasses the `file_id` cache. It alerts administrators on failure.

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `TELEGRAM_TOKEN` | Yes | - | Bot token from @BotFather |
| `ADMIN_IDS` | Yes | - | Comma-separated administrator IDs |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | For Docker | - | Telegram application credentials |
| `WEB_USERNAME` | No | `admin` | WebUI username |
| `WEB_PASSWORD` | No | `changeme` | WebUI password; change it |
| `WEB_SECRET_KEY` | No | Generated | Session signing key, preferably 64 hex characters |
| `WEB_PORT` | No | `8080` | WebUI port |
| `FAIL2BAN_RETRIES` | No | `5` | Failed sign-ins before IP lockout |
| `FAIL2BAN_TIME` | No | `10m` | Lockout duration, such as `10m`, `1h`, or `300` |
| `LOG_LEVEL` | No | `INFO` | Log level |
| `DOWNLOAD_WORKERS` | No | `8` | Blocking worker threads |
| `BLOCKING_TASK_TIMEOUT` | No | `600` | Blocking task timeout in seconds |
| `TIKTOK_FAST_PATH` | No | `true` | Direct H.264 TikTok URL instead of yt-dlp, generally 576x1024 |
| `INSTAGRAM_FAST_PATH` | No | `true` | Direct Instagram GraphQL URL instead of yt-dlp |
| `YTDLP_CLI_FALLBACK` | No | `true` | CLI fallback after API failure |
| `YTDLP_CLI_TIMEOUT` | No | `900` | CLI timeout in seconds |
| `CANARY_ENABLED` | No | `false` | Scheduled YouTube check and failure alert |
| `CANARY_INTERVAL_HOURS` | No | `12` | Check interval, from 1 to 168 hours |
| `CANARY_VIDEO_ID` | No | `6WFj-CKldv4` | Reference video ID; use a video longer than a few minutes |
| `DATA_DIR` | No | Repository root | SQLite directory; `/app/data` in Docker |
| `TEMP_DIR` | No | `./temp` | Temporary media; shared `/app/media` in Docker |
| `YOUTUBE_COOKIES_FILE` | No | `www.youtube.com_cookies.txt` | Cookie filename in `.secrets/` |
| `TIKTOK_COOKIES_FILE` | No | `www.tiktok.com_cookies.txt` | Cookie filename in `.secrets/` |
| `INSTAGRAM_COOKIES_FILE` | No | `www.instagram.com_cookies.txt` | Cookie filename in `.secrets/` |
| `TELEGRAM_LOCAL_MODE` | No | `false` | Local Bot API instead of cloud; set to `true` in Docker |
| `TELEGRAM_BOT_API_BASE_URL`, `TELEGRAM_BOT_API_FILE_URL` | No | - | Local Bot API endpoints set by `compose.yaml` |
| `TELEGRAM_MAX_FILE_SIZE_MB` | No | `50` | Requested limit, clamped to 2,000 locally or 50 in cloud mode |

The final four delivery settings are set by `compose.yaml`; they are not in `.env.example` because direct runs do not need them and Docker users do not need to edit them.

### TikTok fast path and cached files

The `file_id` cache is checked before downloading and has a 90-day TTL. Changing `TIKTOK_FAST_PATH` does not alter cached links: after setting it to `false`, a cached 576x1024 file will still be delivered. `/cleanup_cache` deletes only expired entries. For an immediate reset, remove `telegram_cache.db` from `DATA_DIR` and restart the bot.

## Change with care

- `messages.py`: Telegram messages must fit its limits, including 4,096 characters for ordinary messages.
- `config.py`: Update `.env.example` and `README.md` when adding environment variables.
- `utils/telegram_utils.py` and `utils/callback_fsm.py`: Accompany `callback_data` changes with event-parsing and session-transition tests.
- `utils/youtube_utils.py`: Preserve the without-cookies, with-cookies, then CLI fallback sequence and its recovery behavior.
- `tests/conftest.py`: Its yt-dlp stub is shared across many tests.
- SQLite schemas: Preserve WAL behavior and provide migrations for existing installations when changing `telegram_cache.db` or `analytics.db`.
