# CLAUDE.md

Guidance for working with the Nuvio codebase. Read `AGENTS.md` for the complete environment table, security rules, and CI/CD overview.

Nuvio is a Python 3.14+ asynchronous Telegram bot for downloading media from YouTube, TikTok, Instagram, Rutube, and VK Video. It includes an analytics WebUI and uses a local Telegram Bot API in Docker. Documentation is in English. Bot messages, comments, and docstrings remain in Russian; code identifiers are English. The WebUI defaults to English and offers Russian.

## Commands

```bash
python -m pip install --requirement requirements-dev.txt
python main.py
python -m web
python -m api
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

**Entry point:** `main.py` sets up the event loop, handlers, graceful SIGINT/SIGTERM shutdown, daily cache cleanup, a daily cookie health check, weekly `VACUUM`, and CSI surveys. It loads `.secrets/.env`; `.env.local` and root `.env` remain legacy fallbacks. Imports intentionally follow `load_dotenv()`; `pyproject.toml` grants this file `E402`. Do not move those imports above environment loading.

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
| `utils/video_cache.py` | SQLite `file_id` cache keyed by URL and format, WAL, no expiry unless cleanup is enabled in the WebUI |
| `utils/analytics_db.py` | Users, events, CSI, settings, and SQLite migrations |
| `utils/ytdlp_runtime.py` | Installed yt-dlp version and CLI fallback |
| `utils/cookie_manager.py`, `utils/cookie_health.py`, `utils/cookie_workfile.py` | Administrator cookie uploads, working-copy health checks, TikTok session renewal, and working copies |
| `utils/download_report.py` | Per-session yt-dlp diagnostic tail and actual delivered format |
| `utils/canary.py` | Scheduled real YouTube download without the `file_id` cache |
| `utils/api_keys.py` | HTTP API keys: SHA-256 storage, expiry, rotation with a grace period, revocation |
| `utils/media_fetch.py` | Telegram-free download by link for the HTTP API, with the bot's format choices |
| `api/app.py`, `api/jobs.py`, `api/alerts.py` | HTTP API process: key auth, rate limits, per-key job ownership, file TTL, stuck-worker watchdog, administrator alerts |
| `api/schemas.py` | OpenAPI response models and error codes; handlers return `JSONResponse`, so tests check real responses against them |

`analytics_db.settings` is shared by separate bot and WebUI processes through `DATA_DIR`. `csi_interval_days` is read for every survey run, so changing it in the WebUI needs no restart. Invalid or out-of-range values fall back to 14 days.

yt-dlp rewrites a supplied `cookiefile` from its jar. A measured YouTube request removed `YSC` from a 15-cookie file, whereas tested TikTok and Instagram requests removed none. Downloaders must use a working copy under `DATA_DIR/cookie-work` and preserve the administrator-uploaded original. `cookie_workfile.py` also replaces yt-dlp's jar class with one that writes atomically, because a parallel run that read a half-written copy kept 2 of 6 cookies in a check. yt-dlp has no option for this, so `tests/test_cookie_workfile.py` catches a renamed class. `cookie_health.py` checks that working copy, not the original, because only the copy shows what yt-dlp lost: on 2026-11-01 the YouTube original had 22 cookies with every auth cookie, the copy had 15 with one auth cookie of six and no `LOGIN_INFO`, and the old check had reported `valid` five times in a row. yt-dlp warnings cannot replace this: it warns only at the moment of loss, then treats requests as anonymous from the start. TikTok and Instagram report `valid` only on direct evidence of sign-in, and an unfamiliar response from `passport/web/account/info/` or `/accounts/edit/` is `probe_failed`; a YouTube `valid` means only that a public video was extracted and `LOGIN_INFO` is still in the file, which does not prove the session. The passport response also extends the TikTok session, so a daily job (`interval=86400`, first run after 900 s) runs the full check, saves the new date to the copy on `valid`, and alerts administrators; if that endpoint changes, the check returns `probe_failed` and renewal stops, and the first alert comes when fewer than 14 days remain. An upload is inspected in a temporary file before `os.replace`, then `working_cookie_file(path, fresh=True)` rebuilds the copy, because the copy's name depends on the original's hash and identical content would otherwise reuse the damaged copy. Do not restore a copy from the original automatically, because the server may already have revoked that session; see "Cookie health" in `docs/reference/architecture.md`.

Locally, `DATA_DIR` defaults to the repository root and holds `analytics.db` and `telegram_cache.db`. Docker mounts `bot-data` at `/app/data`. Temporary media lives in `TEMP_DIR`, locally `./temp` and in Docker the shared `/app/media` volume, so the local Bot API can read absolute paths.

**WebUI:** FastAPI, Jinja2, and Uvicorn provide PBKDF2 plus timing-safe sign-in checks, in-memory IP fail2ban with administrator alerts, and `/health` for Compose and CI. Swagger, ReDoc, and the OpenAPI schema (`/openapi.json`) are disabled. `WEB_PORT` sets the port. `WEB_PASSWORD` has no default, so sign-in stays closed until it is set, and a `WEB_SECRET_KEY` shorter than 32 characters is replaced by a random key. The `/settings` page writes data; its current POST relies on a session cookie with `SameSite=lax` rather than a separate CSRF token. Revisit that protection if the cookie's `same_site` changes.

**Request flow:** URL validation, `get_video_info` in a worker thread, format menu, callback, format-specific cache check, then a cache hit or URL-delivery attempt. If Telegram rejects direct URL delivery, the bot downloads in the worker pool, sends the file, caches the returned `file_id`, cleans temporary media, and ends the session.

Direct URL delivery applies before download for allowed domains and media up to 20 MB (photos up to 5 MB). It covers TikTok video/audio, Instagram video, atomic photo sets from both platforms, and progressive YouTube formats. The Telegram infrastructure fetches the URL, so local Bot API mode does not raise this limit. YouTube audio-only, VK, and Rutube were measured as unsuitable for this path; see `docs/research/latency-disk-network-research.md`, section 8. A Telegram rejection falls back to file delivery. Rejections are remembered for 15 minutes by domain and media type.

**FSM:** State is tied to the inline keyboard on a specific message. Callback forms include `s|{token}|main|{action}`, `s|{token}|format|{action}|{value}`, and `csi|{rating}`. Sessions live in `context.user_data["sessions"]` and do not survive a process restart. See `docs/reference/fsm-architecture.md`.

## Behavior that must remain intact

- Blocking yt-dlp and FFmpeg operations run in a `ThreadPoolExecutor` (`DOWNLOAD_WORKERS=8`, `BLOCKING_TASK_TIMEOUT=600`). `asyncio.wait_for` cancels waiting, not the worker thread; one measured download continued for four minutes after timeout. Session-owned work must pass `session_id=`.
- `UPDATE_CONCURRENCY=32` in `main.py` allows cancellation callbacks and new URLs during a download. Sequential update processing previously hid a working cancellation mechanism behind the update queue. Use `run_blocking(..., session_id=...)` for blocking work and `context.application.create_task` for long background work. `tests/test_concurrent_update_delivery.py` protects this behavior.
- Shared downloaders read the file limit as `work_budget.size_limit(MAX_FILE_SIZE)`, never the bare constant. An HTTP API job passes `API_MAX_FILE_SIZE_MB` to `work_budget.execute()`; without it the bot's Telegram limit applies. A new thread does not inherit that context, so read the limit before starting one. `tests/test_api_file_limit.py` protects this.
- Pass every yt-dlp options dict through `apply_network_opts`, or `apply_work_limits` where a platform sets its own network options. It adds the cancellation hook, caps `socket_timeout` at the time left, and stops an HTTP API job once its tracks together pass the limit. Bot work gets no size hook, because only its finished file counts. Fallback loops re-raise `FileSizeLimitError` and start `except Exception` with `work_budget.check()`, so a cancelled, expired, or oversized download skips the remaining attempts. yt-dlp wraps a `TimeoutError` raised in a hook as `UnavailableVideoError`, so `work_budget.execute` re-checks the budget on any exception. Long-running child processes go through `work_budget.communicate`, so cancellation and the deadline stop them. `tests/test_download_controls.py` protects this.
- Name downloads with `OUTPUT_FILENAME_TEMPLATE`, never `%(title)s`: a long non-Latin title plus yt-dlp's `.f140.m4a.part` suffix can pass the 255-byte file name limit.
- Edit existing Telegram messages through `safe_edit_message_text`. It treats unchanged text and user-deleted messages as benign. A direct edit in an error handler can cause cascading failures.
- Every video send must include `width`, `height`, and `duration`. Telegram inferred `320x320` for measured 10.67 MB and 35.8 MB files when dimensions were omitted, distorting iOS playback. Synthetic tiny files missed the defect. See ADR-002.
- Send only finished-file H.264 8-bit `yuv420p` video to Telegram. Inspect with `ffprobe`, not filename extension: yt-dlp can place VP9 in MP4. VP9 and AV1 produced black screens with playing audio on the tested iPhone; H.264 High 10 played smoothly, so forcing `-pix_fmt yuv420p` is a conservative compatibility rule, not a claimed fix for the observed stutter. A separate stutter cause was not confirmed.
- Error codes follow `PREFIX-CATEGORY-RANDOM` with platform prefixes in `AGENTS.md` and categories in `docs/reference/error-codes.md`. Use `UNEXPECT` for unconfirmed causes and keep exception types in logs. Users receive safe text and a code; internal paths, cookies, and tracebacks stay in administrator diagnostics.
- YouTube and Instagram try without cookies, then with cookies, then CLI where applicable. Authenticated YouTube loses `visionos`, the only default client that needs neither the JavaScript player nor a PO token, and relies on `web_embedded` (`web_creator` for Premium) and `tv_samsung` with Deno challenge solving or on `web` with a PO token. TikTok tries cookies first. Preserve these platform-specific orders.
- A server connectivity failure (DNS resolution, unreachable network or host, refused connection) is `NETWORK` and stops after yt-dlp's own retries. YouTube skips backoff, the cookie attempt, and CLI; the first failing CLI run raises its error with the original as `__cause__`. Rutube and VK skip backoff through `execute_with_backoff`. TikTok and Instagram are affected only by the classification. `is_connectivity_failure()` in `utils/public_errors.py` checks the exception chain and the `error:` lines of the message; an `OSError` is judged by type only, so a video title in a file path cannot match. On 2026-10-15 a DNS outage was classified as `DOWNLOAD`, so every request walked the whole fallback path against the same resolver. Timeouts and connection resets keep backoff and CLI.
- A 403 on the media URL is `MEDIA_FORBIDDEN`, distinct from a private or otherwise restricted video. Retry with backoff and fresh extraction, then use CLI fallback where applicable. A final failure is reported to administrators by the current `_should_notify_admins_platform_failure()` rule.
- Pin yt-dlp in `requirements.in` and update through `scripts/update_ytdlp.py` and a new image. Dependabot ignores yt-dlp, because its PRs skip the documented pins and fail the documentation tests. GitHub applies `ignore` to security updates too, so for a yt-dlp advisory run the `ytdlp-update` workflow manually. The 2026-08-18 incident showed that a 10 MB `Range` request could fail while a small probe worked; see the YouTube runbook.
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
| `.github/dependabot.yml` | Minor/patch groups for three ecosystems; pip ignores yt-dlp |
| `api/schemas.py`, `docs/guides/api.md` | `test_api_downloads.py`: real responses match the models; documented error codes equal the schema's |

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
