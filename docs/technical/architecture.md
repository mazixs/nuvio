# Nuvio architecture

## Overview

Nuvio is an asynchronous Telegram bot for downloading video, photo posts, and audio from YouTube, TikTok, Instagram, Rutube, and VK Video. It uses python-telegram-bot for the bot and a separate FastAPI process for the analytics WebUI.

## Entry point: `main.py`

`main.py` starts the asyncio event loop and owns the bot lifecycle. It:

- Loads `.secrets/.env` as the working configuration. Root `.env` and `.env.local` remain compatibility paths; Compose does not use them.
- Validates required settings and records the yt-dlp version included in the image.
- Registers `/start`, `/help`, `/download`, `/admin`, `/cache_stats`, `/cleanup_cache`, and `/search_cache`, along with URL, callback, and cookie-upload handlers.
- Schedules cache cleanup, database VACUUM, and CSI survey dispatch. The per-user CSI interval comes from the shared `csi_interval_days` setting.
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

The bot limits repeated requests: four requests in five seconds trigger a ten-second cooldown. Public error IDs use `<PREFIX>-<CATEGORY>-<RANDOM>`; see the [error code reference](../error-codes.md).

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

- `utils/cookie_manager.py` and `utils/cookie_health.py` provide administrator cookie upload and health checks.
- `utils/logger.py` writes `logs/bot.log` with 10 MB rotation and five backups.
- `utils/cache_commands.py` implements cache administration commands.
- `utils/temp_file_manager.py` manages temporary media and cleanup.

## WebUI

The separate `web/` process uses FastAPI, Jinja2, and Uvicorn. It has sign-in, dashboard, user list and detail, and settings pages. Credentials are checked against environment settings with PBKDF2 password hashing and timing-safe comparison; sessions are signed with `WEB_SECRET_KEY`. The WebUI opens in English and offers a Russian language switch.

The dashboard shows users, retention, CSI/NPS, and Chart.js charts. `/api/summary` provides JSON metrics. The CSI interval form is the state-changing page; its session cookie uses `SameSite=lax`. `WEB_PORT` defaults to 8080.

## Design patterns

- **Async and worker threads:** yt-dlp and FFmpeg run in `ThreadPoolExecutor`, leaving the event loop responsive. `DOWNLOAD_WORKERS` defaults to 8.
- **Typed callback sessions:** Inline actions carry session tokens so choices and cancellation apply to the correct request.
- **Structured errors:** Public messages use centralized text in `messages.py`; detailed exceptions remain in administrative logs and can carry context through `Exception.add_note()`.
- **SQLite WAL and explicit transactions:** Read and write helpers support concurrent bot and WebUI access.
