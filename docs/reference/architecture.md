# Nuvio architecture

## Overview

Nuvio is an asynchronous Telegram bot for downloading video, photo posts, and audio from YouTube, TikTok, Instagram, Rutube, and VK Video. It uses python-telegram-bot for the bot and a separate FastAPI process for the analytics WebUI.

## Entry point: `main.py`

`main.py` starts the asyncio event loop and owns the bot lifecycle. It:

- Loads `.secrets/.env` as the working configuration. Root `.env` and `.env.local` remain compatibility paths; Compose does not use them.
- Validates required settings and records the yt-dlp version included in the image.
- Registers `/start`, `/help`, `/download`, `/admin`, `/cache_stats`, `/cleanup_cache`, and `/search_cache`, along with URL, callback, and cookie-upload handlers.
- Schedules cache cleanup, database VACUUM, CSI survey dispatch, and the daily cookie health check. The per-user CSI interval comes from the shared `csi_interval_days` setting.
- Handles SIGINT and SIGTERM for graceful shutdown and sends crash reports to administrators through the global error handler.

## Request flow

```text
URL received
  -> validate URL and identify platform
  -> extract metadata and show format choices
  -> user selects a format
  -> check the file_id cache for the selected format
      -> hit: send the saved Telegram file_id
      -> miss: download and process media in ThreadPoolExecutor
          -> send through the local Telegram Bot API using a shared file path
          -> save the returned file_id
  -> remove temporary media
```

Exact branches vary by platform and by whether the result is a video, audio file, photo set, or link. Callback data is tied to a session, and long-running work supports cancellation.

## Modules

### `config.py`

Parses and validates environment variables, resolves cookie paths, and prefers `.secrets/` while retaining a legacy root-path fallback. `TELEGRAM_TOKEN` is required for the bot.

### Telegram handlers and callback state

`utils/telegram_utils.py` handles commands, URLs, inline buttons, and delivery. Related responsibilities are separated into:

- `utils/callback_fsm.py` - callback parsing and session storage.
- `utils/platform_actions.py` - platform action and cache-key decisions.
- `utils/file_delivery.py` - choosing the Telegram delivery method for a file.
- `utils/public_errors.py` - safe, platform-specific error classification.
- `utils/cancellation.py` - cancellation of long-running work by session ID.

The bot limits repeated requests: four requests in five seconds trigger a ten-second cooldown. Public error IDs use `<PREFIX>-<CATEGORY>-<RANDOM>`; see the [error code reference](error-codes.md).

### Platform downloaders

- `utils/youtube_utils.py` uses yt-dlp for YouTube and Shorts, with format selection, cookies when needed, and bounded retry and fallback behavior.
- `utils/tiktok_instagram_utils.py` handles TikTok and Instagram media, including photo posts and carousels. Platform-specific fast paths and yt-dlp fallbacks live in adjacent modules.
- `utils/rutube_vk_utils.py` handles Rutube and VK Video, including archive URL normalization and size-aware format selection.

### Media processing

`utils/media_processor.py` uses FFmpeg and ffprobe for audio extraction, MP3 conversion at 192k, WebM-to-MP4 conversion, stream merging, codec checks, and compatibility processing.

### SQLite state

- `utils/video_cache.py` stores Telegram file IDs in `telegram_cache.db`. The cache uses WAL and retains valid references by default. Optional idle-age cleanup is configured in the WebUI; actual media kinds and ordered publication manifests are bot/recipe scoped.
- `utils/analytics_db.py` stores users, events, CSI responses, and settings in `analytics.db`. It uses WAL, manual read/write transactions, and `BEGIN IMMEDIATE` for writes.
- The `settings` table is shared by the bot and WebUI: the WebUI changes the CSI interval, and the bot reads it for survey dispatch.

### yt-dlp runtime

`utils/ytdlp_runtime.py` reports the pinned yt-dlp version and supports a CLI fallback with `python -m yt_dlp`. Version updates are prepared with `scripts/update_ytdlp.py` and reach a running bot through a newly released image.

### Local Telegram Bot API

Compose runs a separate `telegram-bot-api` service with `--local`. It reads files by absolute path from the `shared-media` volume and supports delivery up to 2 GB. Its port is available only inside the Compose network.

### Support modules

- `utils/cookie_manager.py` and `utils/cookie_health.py` provide administrator cookie upload and health checks; see [Cookie health](#cookie-health).
- `utils/logger.py` writes `logs/bot.log` with 10 MB rotation and five backups.
- `utils/cache_commands.py` implements cache administration commands.
- `utils/temp_file_manager.py` manages temporary media and cleanup.

### Cookie health

`utils/cookie_health.py` checks the working copy in `DATA_DIR/cookie-work`, not the uploaded original: yt-dlp changes only the copy, so a lost cookie does not show in the original. On 2026-11-01 the YouTube original had 22 cookies including every auth cookie, while its copy had 15, with one auth cookie of six and no `LOGIN_INFO`; the previous check had reported `valid` five times in a row. An Instagram `ds_user_id` that had expired in the original had been extended by 78 days in the copy.

The local check runs first and stops at the first match:

1. No auth records: `invalid_format`.
2. No live auth cookies: `expired`.
3. A session cookie is present but expired: `expired`.
4. No session cookie, or fewer than half of the platform's auth cookies alive: `degraded`.
5. Otherwise the live probe decides.

Session cookies are `LOGIN_INFO` on YouTube, `sessionid` on Instagram, and `sessionid` or `sid_tt` on TikTok. yt-dlp warnings are not used as a signal: it warns about a loss only when the loss happens and then stays silent, because without `LOGIN_INFO` it treats the request as anonymous from the start.

The parser accepts only what yt-dlp's jar reads whole and silently, because the jar quotes a row it dislikes together with the cookie value. It refuses a whole file without the Netscape header or with a subdomain flag that disagrees with the domain's leading dot, quoting the row in the error text, and it fails on invalid UTF-8. It skips a row whose expiry is not a plain number, such as `-1`, and prints that row to stderr. All of these are `invalid_format`, so an upload cannot bring such a row to yt-dlp. Our error messages contain only the line number, never the value.

| Platform | Live probe | `valid` | `stale` | Otherwise |
|---|---|---|---|---|
| YouTube | yt-dlp extracts a public video, then the file is read again | `LOGIN_INFO` is still live | `LOGIN_INFO` is gone, or yt-dlp reports an authentication failure | `rate_limited` on a rate-limit message, else `probe_failed` |
| TikTok | `GET https://www.tiktok.com/passport/web/account/info/` | `message=success` and `data.user_id_str` | HTTP 200 with `data.name=session_expired` or `data.error_code=13` | `rate_limited` on HTTP 429, else `probe_failed` |
| Instagram | `GET /accounts/edit/`, first 256 KB of the body | `NON_FACEBOOK_USER_ID` is not zero | Redirect to `/accounts/login`, or HTTP 401 or 403 | `rate_limited` on HTTP 429 or a rate-limit message, else `probe_failed` |

- **YouTube:** `valid` does not prove the session, because the test video extracts without sign-in. The summary says so: `session cookies are present and the test video was extracted`.
- **TikTok:** the endpoint is undocumented, so an unfamiliar response is `probe_failed`, not a verdict on the session. The response contains the email, phone number, and `session_key`; only the verdict fields are read, and the body is not written anywhere. In a 2026-11-01 measurement, `sessionid` or `sid_tt` alone confirmed sign-in and `uid_tt` alone did not.
- **Instagram:** any unfamiliar page used to look like a live session; now only the non-zero ID counts. The signal sits about 160 KB into the page, hence the 256 KB read. Markers such as `Log in` are useless because neither the sign-in page nor the signed-in page contains them, and `USER_ID` is `0` on both.

**TikTok renewal.** The passport response carries `Set-Cookie` with the same token and a later expiry (measured on 2026-11-01: from 2027-02-12 to 2027-04-30). On `valid` the new date is saved to the working copy. No other verdict writes it, and the administrator's original does not change.

**Daily check.** A scheduled job (`interval=86400`, first run after 900 s) runs the full check, which is what keeps the TikTok session alive. It adds no environment variables and stores no state. It messages administrators for `expired`, `stale`, `degraded`, or `invalid_format`, or when fewer than 14 days remain until the session ends. `rate_limited`, `probe_failed`, and `missing` raise no alert by themselves; the 14-day rule applies whatever the status. Without stored state, the alert repeats once a day until the problem is resolved.

Only TikTok renews automatically. If `passport` changes, the check returns `probe_failed` and renewal stops. The `/admin` panel shows this, and the first alert comes when fewer than 14 days remain.

**Upload.** An upload from `/admin` is first saved to a temporary file next to the target and checked with `inspect_cookie_file`, so the bot needs write access to the `.secrets` directory, not only to the file. If the local verdict is not `valid`, the old file stays and upload mode is not reset. Otherwise `os.replace` installs the file, `working_cookie_file(path, fresh=True)` rebuilds the copy, and the live check runs. `stale` at this step is only a warning, and the file stays. `fresh` is needed because the copy's name depends on the original's hash: uploading the same file again would otherwise get the old copy without `LOGIN_INFO`. The check button in `/admin` runs with `force=True` and shows the session end date as `Сессия до DD.MM.YYYY`.

The copy is deliberately not restored from the original automatically: the file would look healthy again, but the server may already have revoked that session. After `degraded` on YouTube, upload a fresh browser export; see [Cookie problems](../runbooks/common-issues.md#cookie-problems).

## WebUI

The separate `web/` process uses FastAPI, Jinja2, and Uvicorn. It has sign-in, dashboard, user list and detail, and settings pages. Credentials are checked against environment settings with PBKDF2 password hashing and timing-safe comparison; sessions are signed with `WEB_SECRET_KEY`. The WebUI opens in English and offers a Russian language switch.

The dashboard shows users, retention, CSI/NPS, and Chart.js charts. `/api/summary` provides JSON metrics. The CSI interval form is the state-changing page; its session cookie uses `SameSite=lax`. `WEB_PORT` defaults to 8080.

## HTTP API

The separate `api/` process serves downloads by link to external clients. Keys live in the `api_keys` table of `analytics.db`: the WebUI creates, rotates, changes the expiry of, and revokes them through `utils/api_keys.py`, and the API only authenticates them. Only a SHA-256 hash is stored; a rotated secret keeps working until its grace period ends.

`POST /api/v1/downloads` creates an in-memory job owned by the key and returns `202`. `api/jobs.py` runs it in its own thread pool (`API_DOWNLOAD_WORKERS`): `utils/media_fetch.py` resolves the link and chooses the format with the same helpers as the bot's video and audio buttons, then the platform downloader writes into `TEMP_DIR/api-<job id>`. `utils/work_budget.py` bounds both steps and passes cancellation from `DELETE` to yt-dlp progress hooks. Finished files expire after `API_RESULT_TTL_MINUTES` or earlier above the per-key and `API_STORAGE_LIMIT_MB` limits, and the startup purge removes `api-*` folders left by a previous run. A sweep every minute also drops jobs of revoked or deleted keys. A job passes `API_MAX_FILE_SIZE_MB` to `work_budget.execute()`, and the downloaders read it with `work_budget.size_limit()` instead of the bot's Telegram limit. `apply_work_limits()` in `utils/ytdlp_common.py` also stops such a download once its tracks together pass the limit, and caps `socket_timeout` at the time left. The same sweep runs `JobManager.fail_stuck_jobs()`: it fails a job whose thread outlived the deadline by `STUCK_GRACE_SECONDS` and exits the process when no worker is left. `api/schemas.py` describes the responses for the OpenAPI schema. Handlers return `JSONResponse` directly, so `tests/test_api_downloads.py` checks real responses against those models. Importing the `api` package sets `NUVIO_PROCESS=api`, which sends the process log to `logs/api.log`. `api/alerts.py` notifies administrators about failures by the bot's `should_notify_admins()` rule, at most once per platform and category in 15 minutes. See the [HTTP API guide](../guides/api.md).

## Design patterns

- **Async and worker threads:** yt-dlp and FFmpeg run in `ThreadPoolExecutor`, leaving the event loop responsive. `DOWNLOAD_WORKERS` defaults to 8.
- **Typed callback sessions:** Inline actions carry session tokens so choices and cancellation apply to the correct request.
- **Structured errors:** Public messages use centralized text in `messages.py`; detailed exceptions remain in administrative logs and can carry context through `Exception.add_note()`.
- **SQLite WAL and explicit transactions:** Read and write helpers support concurrent bot and WebUI access.
