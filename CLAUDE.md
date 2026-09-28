# CLAUDE.md

Guidance for working with the Nuvio codebase. Read `AGENTS.md` for the complete environment table, security rules, and CI/CD overview.

Nuvio is a Python 3.14+ asynchronous Telegram bot for downloading media from YouTube, TikTok, Instagram, Rutube, and VK Video. It includes an analytics WebUI and uses a local Telegram Bot API in Docker. Documentation is in English. Bot messages, comments, and docstrings remain in Russian; code identifiers are English. The WebUI defaults to English and offers Russian.

## Commands

```bash
python -m pip install --requirement requirements-dev.txt
python main.py
python -m web
docker compose --env-file .secrets/.env -f compose.yaml -f compose.dev.yaml up --build
docker compose --env-file .secrets/.env up -d
pytest
pytest tests/test_youtube_smoke.py
pytest -k "test_name"
pytest -m syntax
ruff check --output-format=github .
coverage run --branch -m pytest tests/
coverage report --fail-under=70
```

A direct run uses the cloud Bot API and its 50 MB limit. The Docker stack uses the local Bot API for files up to 2 GB.

## Architecture

**Entry point:** `main.py` sets up the event loop, handlers, graceful SIGINT/SIGTERM shutdown, daily cache cleanup, weekly `VACUUM`, and CSI surveys. It loads `.secrets/.env`; `.env.local` and root `.env` remain legacy fallbacks. Imports intentionally follow `load_dotenv()`; `pyproject.toml` grants this file `E402`. Do not move those imports above environment loading.

**Configuration:** `config.py` parses typed environment settings. `resolve_secret_path()` prefers `.secrets/<file>` and supports legacy root files. `MAX_FILE_SIZE_MB` is clamped by delivery mode to 2,000 MB with `TELEGRAM_LOCAL_MODE=true` and 50 MB otherwise. In local mode, `validate_config()` requires `http://` for the Bot API URL.

**Main modules:**

| Module | Responsibility |
|---|---|
| `utils/telegram_utils.py` | Handlers, user flow, spam protection, errors, and delivery |
| `utils/callback_fsm.py` | `CallbackEvent.parse()`, the single callback parser, and `SessionStore` with up to five sessions per user |
| `utils/platform_actions.py` | Platform action and cache-key decisions |
| `utils/file_delivery.py` | Select the Telegram send method by file type |
| `utils/public_errors.py` | Safe public error classification |
| `utils/ytdlp_common.py` | Shared yt-dlp network options, backoff, and size checks |
| `utils/fast_path.py` | Fast-path primitives and shared domain allowlist checks |
| `utils/url_delivery.py` | Decide whether to send a direct URL; 20 MB media and 5 MB photo limits |
| `utils/instagram_fast_path.py` | Instagram GraphQL video URL with Meta domain allowlist and yt-dlp fallback |
| `utils/tiktok_fast_path.py` | TikTok resolver H.264 URL, audio-source marker, allowlist, and yt-dlp fallback |
| `utils/youtube_utils.py` | YouTube and Shorts through yt-dlp, cookies, and retries |
| `utils/tiktok_instagram_utils.py` | TikTok and Instagram downloaders, photos, and carousels |
| `utils/rutube_vk_utils.py` | Rutube and VK Video; VK uses `best[protocol=https]` to avoid fragmented HLS |
| `utils/media_processor.py` | FFmpeg audio extraction, conversion, and merging |
| `utils/video_cache.py` | SQLite `file_id` cache keyed by URL and format, WAL, 90-day TTL |
| `utils/analytics_db.py` | Users, events, CSI, settings, and SQLite migrations |
| `utils/ytdlp_runtime.py` | Installed yt-dlp version and CLI fallback |
| `utils/cookie_manager.py`, `utils/cookie_health.py`, `utils/cookie_workfile.py` | Administrator cookie uploads, validation, and working copies |
| `utils/download_report.py` | Per-session yt-dlp diagnostic tail and actual delivered format |
| `utils/canary.py` | Scheduled real YouTube download without the `file_id` cache |

`analytics_db.settings` is shared by separate bot and WebUI processes through `DATA_DIR`. `csi_interval_days` is read for every survey run, so changing it in the WebUI needs no restart. Invalid or out-of-range values fall back to 14 days.

yt-dlp rewrites a supplied `cookiefile` from its jar. A measured YouTube request removed `YSC` from a 15-cookie file, whereas tested TikTok and Instagram requests removed none. Downloaders must use a working copy under `DATA_DIR/cookie-work` and preserve the administrator-uploaded original. `cookie_health.py` reads enough response body to see late YouTube authentication markers and also checks redirects to `consent.youtube.com`.

Locally, `DATA_DIR` defaults to the repository root and holds `analytics.db` and `telegram_cache.db`. Docker mounts `bot-data` at `/app/data`. Temporary media lives in `TEMP_DIR`, locally `./temp` and in Docker the shared `/app/media` volume, so the local Bot API can read absolute paths.

**WebUI:** FastAPI, Jinja2, and Uvicorn provide PBKDF2 plus timing-safe sign-in checks, in-memory IP fail2ban with administrator alerts, and `/health` for Compose and CI. Swagger and ReDoc are disabled. `WEB_PORT` sets the port. The `/settings` page writes data; its current POST relies on a session cookie with `SameSite=lax` rather than a separate CSRF token. Revisit that protection if the cookie's `same_site` changes.

**Request flow:** URL validation, `get_video_info` in a worker thread, format menu, callback, format-specific cache check, then a cache hit or URL-delivery attempt. If Telegram rejects direct URL delivery, the bot downloads in the worker pool, sends the file, caches the returned `file_id`, cleans temporary media, and ends the session.

Direct URL delivery applies before download for allowed domains and media up to 20 MB (photos up to 5 MB). It covers TikTok video/audio, Instagram video, atomic photo sets from both platforms, and progressive YouTube formats. The Telegram infrastructure fetches the URL, so local Bot API mode does not raise this limit. YouTube audio-only, VK, and Rutube were measured as unsuitable for this path; see `docs/technical/latency-disk-network-research.md`, section 8. A Telegram rejection falls back to file delivery. Rejections are remembered for 15 minutes by domain and media type.

**FSM:** State is tied to the inline keyboard on a specific message. Callback forms include `s|{token}|main|{action}`, `s|{token}|format|{action}|{value}`, and `csi|{rating}`. Sessions live in `context.user_data["sessions"]` and do not survive a process restart. See `docs/technical/fsm-architecture.md`.

## Behavior that must remain intact

- Blocking yt-dlp and FFmpeg operations run in a `ThreadPoolExecutor` (`DOWNLOAD_WORKERS=8`, `BLOCKING_TASK_TIMEOUT=600`). `asyncio.wait_for` cancels waiting, not the worker thread; one measured download continued for four minutes after timeout. Session-owned work must pass `session_id=`.
- `UPDATE_CONCURRENCY=32` in `main.py` allows cancellation callbacks and new URLs during a download. Sequential update processing previously hid a working cancellation mechanism behind the update queue. Use `run_blocking(..., session_id=...)` for blocking work and `context.application.create_task` for long background work. `tests/test_concurrent_update_delivery.py` protects this behavior.
- Edit existing Telegram messages through `safe_edit_message_text`. It treats unchanged text and user-deleted messages as benign. A direct edit in an error handler can cause cascading failures.
- Every video send must include `width`, `height`, and `duration`. Telegram inferred `320x320` for measured 10.67 MB and 35.8 MB files when dimensions were omitted, distorting iOS playback. Synthetic tiny files missed the defect. See ADR-002.
- Send only finished-file H.264 8-bit `yuv420p` video to Telegram. Inspect with `ffprobe`, not filename extension: yt-dlp can place VP9 in MP4. VP9 and AV1 produced black screens with playing audio on the tested iPhone; H.264 High 10 played smoothly, so forcing `-pix_fmt yuv420p` is a conservative compatibility rule, not a claimed fix for the observed stutter. A separate stutter cause was not confirmed.
- Error codes follow `PREFIX-CATEGORY-RANDOM` with platform prefixes in `AGENTS.md` and categories in `docs/error-codes.md`. Use `UNEXPECT` for unconfirmed causes and keep exception types in logs. Users receive safe text and a code; internal paths, cookies, and tracebacks stay in administrator diagnostics.
- YouTube and Instagram try without cookies, then with cookies, then CLI where applicable. Authenticated YouTube can lose the token-free `android_vr` client and require a PO token. TikTok tries cookies first. Preserve these platform-specific orders.
- A 403 on the media URL is `MEDIA_FORBIDDEN`, distinct from a private or otherwise restricted video. Retry with backoff and fresh extraction, then use CLI fallback where applicable. A final failure is reported to administrators by the current `_should_notify_admins_platform_failure()` rule.
- Pin yt-dlp in `requirements.in` and update through `scripts/update_ytdlp.py` and a new image. The 2026-08-18 incident showed that a 10 MB `Range` request could fail while a small probe worked; see the YouTube runbook.
- Do not suppress yt-dlp warnings. `apply_network_opts` writes them to `bot.log` and the per-session tail in `utils/download_report.py`. Progress lines are excluded from that 60-line tail. The actual delivered format from `download_report.delivered_format(session_id)` determines a format-specific cache key after fallback.
- Use parameterized SQLite queries with WAL. Keep user messages in `messages.py`. Spam protection allows four requests in five seconds before a ten-second cooldown. Logs rotate at 10 MB with five backups.

## Tests and configuration contracts

Pytest uses strict `syntax`, `unit`, and `integration` markers. `tests/conftest.py` supplies fixtures and a lightweight yt-dlp stub; YouTube tests mock `YoutubeDL` and disable real cookies. FFmpeg and Git are system dependencies; Docker is required for the CI build stage.

Several tests inspect **configuration text**, so infrastructure edits must update their contracts:

| Change | Related checks |
|---|---|
| `.github/workflows/*.yml` | `test_workflow_quality_gates.py`: full SHA pins, minimal permissions, concurrency, timeouts, job order, digest checks |
| `Dockerfile`, `Dockerfile.telegram-bot-api` | Pinned base-image digests and Bot API revision |
| `requirements*.in/.txt` | `test_environment_template.py`, `test_workflow_quality_gates.py`: exact direct versions, hashes, runtime/dev split |
| `pyproject.toml` | Exactly `select = ["E4", "E7", "E9", "F"]` |
| `.env.example` | Required environment keys and no in-container update settings |
| `compose.yaml`, `compose.dev.yaml` | Local Bot API, `shared-media`, unexposed port 8081, no legacy compose files |
| `pytest.ini`, `messages.py`, `utils/video_cache.py` | `test_dead_code_contract.py`: removed markers, flags, constants, and helpers stay removed |
| `.github/dependabot.yml` | Minor/patch groups for three ecosystems |

`tests/test_ruff.py` runs Ruff within pytest. `tests/test_syntax.py` forbids production `print()` and star imports.

Docker requires `TELEGRAM_TOKEN`, `ADMIN_IDS`, `TELEGRAM_API_ID`, and `TELEGRAM_API_HASH`; direct runs need the first two. Other settings include `WEB_*`, `FAIL2BAN_*`, `DATA_DIR`, `TEMP_DIR`, `YTDLP_*`, `CANARY_*`, and local Bot API variables. Use `.env.example` and the full table in `AGENTS.md`.

## Change with care

- `messages.py`: Keep messages within Telegram's 4,096-character limit.
- `config.py`: Add new keys to `.env.example` and `README.md`.
- `callback_fsm.py` and `telegram_utils.py`: Callback format changes break buttons already sent to users; cover parsing and transitions.
- Session eviction: `SessionStore` keeps five entries per user but **must not delete temporary media** when evicting. An earlier version removed an active download directory after 12 sessions from one user. Busy sessions are protected by `_session_is_disposable`; `hard_limit` bounds growth if all are busy.
- `cancellation.py`: `CancelledByUser` deliberately inherits `BaseException`, like `asyncio.CancelledError`, to bypass broad platform `except Exception` handlers and reach the one handler in `button_callback`. `apply_network_opts(..., session_id=...)` installs yt-dlp `progress_hooks`; tests showed yt-dlp propagates their exception.
- Format menus have two levels: `main|video_menu`, `main|audio_menu`, `main|subtitles`, then a choice. Live `format` actions are `combined` and `audio_only`; `best`, `audio_best`, `mp3_min`, and `video_only` were removed.
- Subtitles use `main|subtitles` -> `format|subs_lang|ru` -> `format|subs|ru:srt`. The menu offers Russian and English; `utils/subtitles.py` builds TXT from SRT.
- Cancellation is available while parsing a URL, in the main menu, and during downloads. The session starts **before** link extraction so the button has a target.
- Preserve YouTube fallback order, the shared yt-dlp test stub, SQLite WAL, and migration behavior. `analytics_db.py` migration of `last_csi_sent` is an example.
