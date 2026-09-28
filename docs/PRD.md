# Nuvio - Product Requirements Document

**Version:** 1.1
**Last updated:** 2026-07-23
**License:** Apache 2.0
**Repository:** [github.com/mazixs/nuvio](https://github.com/mazixs/nuvio)

This is a dated product snapshot. For current setup, versions, and operating behavior, use the [README](../README.md) and [architecture guide](technical/architecture.md).

---

## 1. Positioning

### 1.1 What is Nuvio?

Nuvio is an open-source Telegram bot for downloading videos, photo posts, and audio from YouTube, TikTok, Instagram, Rutube, and VK Video. It offers format selection buttons, caches Telegram `file_id` values for repeated delivery, and includes an analytics WebUI.

### 1.2 Audience

| Audience | Need |
|---|---|
| **Telegram users** | Download video or audio in chat without third-party sites or apps |
| **Channel administrators** | Run their own bot instance for their audience |
| **Developers** | Fork, customize, or extend the project |

### 1.3 Key difference

One `docker compose up` deploys the bot and an authenticated analytics dashboard. Nuvio uses a pinned yt-dlp version and fallback download paths instead of depending on an external API aggregator.

### 1.4 License

Apache License 2.0 permits use, modification, and redistribution, including commercial use, subject to its notice requirements. It does not set limits on users, instances, or platforms.

---

## 2. Supported platforms

### 2.1 YouTube

| Feature | Description |
|---|---|
| Standard videos | `youtube.com/watch?v=...` |
| Shorts | `youtube.com/shorts/...` |
| Format selection | Combined video and audio, video only, or audio only |
| Quality selection | From 360p to the highest available resolution |
| Audio | Native M4A or conversion through FFmpeg |
| Subtitles | Manual and automatically generated SRT |
| Size-aware selection | Select a format within the current delivery limit |
| Cookies | Support for restricted content where authentication helps |

### 2.2 TikTok

| Feature | Description |
|---|---|
| Standard videos | `tiktok.com/@user/video/...` |
| Mobile links | `vm.tiktok.com/...`, `vt.tiktok.com/...` |
| Video | Download with audio |
| Audio | Extract an MP3 track |
| Cookies | Optional support |
| Reliability | Three API configurations with automatic rotation |

### 2.3 Instagram

| Feature | Description |
|---|---|
| Posts | `instagram.com/p/...` |
| Reels | `instagram.com/reel/...` |
| IGTV | `instagram.com/tv/...` |
| Video | Download with audio |
| Audio | Extract an MP3 track |
| Private profiles | Use cookies where authorized |
| Stories | Not supported in this dated PRD |

---

## 3. User scenarios

### 3.1 Download a video

```text
User sends a URL
  -> Bot identifies the platform
  -> Bot retrieves metadata (title, author, duration)
  -> Bot shows format choices
  -> User selects a format
  -> Bot checks the cache for that choice:
       HIT  -> deliver through Telegram CDN
       MISS -> download, send, and save the file_id
```

### 3.2 YouTube menu

- **Download video for Telegram:** Select the best quality within the delivery limit, moving to a lower resolution when needed.
- **Download audio only:** Offer native M4A or conversion when audio is present.
- **More options:** Open video, audio, and subtitle sections. The video section has one size-labeled button per resolution, from available 8K down to 360p, and hides options above the delivery limit. The audio section offers native Telegram-compatible M4A/AAC tracks or one conversion option if none is available. The subtitle section selects Russian or English, then SRT, VTT, or text, marking automatic translations.
- **Cancel:** Available while parsing a link and during downloads, and interrupts the download rather than only the final send.

### 3.3 TikTok and Instagram menu

- **Download video:** Deliver the video with audio.
- **Audio only (MP3):** Extract the audio track.

### 3.4 Large files

```text
File up to 2 GB
  -> shared temporary volume
  -> local Telegram Bot API
  -> save file_id
  -> remove temporary file
```

A direct run through the cloud Bot API has a 50 MB limit.

---

## 4. System architecture

### 4.1 Technology stack

| Component | Technology |
|---|---|
| **Language** | Python 3.14+ |
| **Telegram API** | python-telegram-bot 22+ (async) |
| **Video downloads** | Pinned yt-dlp version; updates require a new image |
| **Media processing** | FFmpeg (audio extraction, conversion and merging) |
| **WebUI** | FastAPI + Jinja2 + Uvicorn |
| **Database** | SQLite (WAL mode) - cache and analytics |
| **HTTP client** | httpx |
| **CI/CD** | GitHub Actions (lint, test, Docker → GHCR) |
| **Containerization** | Docker Compose (Nuvio and the local Bot API) |

### 4.2 Architectural patterns

- **Async event loop:** `asyncio` handles Telegram updates.
- **ThreadPoolExecutor:** Blocking yt-dlp work runs in a worker pool (`DOWNLOAD_WORKERS=8`).
- **SQLite WAL:** Supports concurrent reads and writes for cache and analytics.
- **Session tokens:** Each request has an eight-character hex token, with up to five active sessions per user.
- **Graceful shutdown:** SIGINT and SIGTERM trigger cleanup of temporary files.

### 4.3 Caching layers

```text
Telegram CDN: immediate repeat delivery by file_id
  -> SQLite file ID cache: key (URL, format_id), 90-day TTL
  -> On a cache miss: yt-dlp download, FFmpeg if needed, Telegram upload
  -> Save the returned file_id; delete temporary media
```

The cache stores the `file_id`, platform, size, duration, and timestamps. Expired entries are cleaned daily.

### 4.4 Processing flows

**YouTube:** Validate URL, extract metadata (without cookies first, then with cookies when needed), show formats, check the selected format in the cache, download video or audio on a miss, process with FFmpeg if needed, enforce the size limit, send through Telegram, cache the `file_id`, and clean up.

**TikTok:** Resolve metadata using rotating API configurations and backoff, show format choices, check the cache, download on a miss with the platform-specific cookie order, enforce the size limit, send, cache, and clean up.

**Instagram:** Reject unsupported Stories and Audio pages, extract metadata without cookies first and with cookies if required, show formats, check the cache, download selected playlist items, enforce the size limit, send, cache, and clean up.

### 4.5 Failure recovery

| Layer | Strategy |
|---|---|
| **yt-dlp API** | Without cookies, then with cookies, then CLI fallback for YouTube |
| **TikTok API** | 3 API hosts with automatic rotation |
| **Instagram** | Without cookies → with cookies at rate-limit |
| **Retry** | Exponential backoff (1s, 2s, 4s) for network errors |
| **Telegram upload** | 3 tries with exponential backoff |
| **Format selection** | Filter by size, then try a lower resolution when needed |
| **Cache** | Delete an invalid `file_id` and download again |

---

## 5. Analytics WebUI

### 5.1 Pages

| Page | Purpose |
|---|---|
| **Login** | Username and password with fail2ban protection |
| **Dashboard** | KPI cards, charts, and cohort analysis |
| **Users** | User list with search and sorting |
| **User detail** | Profile, statistics, platform usage, and event history |

### 5.2 Dashboard metrics

**Users:**
- Total number of users
- New users today, in seven days, and in 30 days
- Active users today, in seven days, and in 30 days
- Download requests

**Retention and churn:**
- Retention D3, D7, D30 (% of returned users)
- Churn 30d (% of outflow for 30 days)

**Product metrics:**
- Average number of downloads per user
- Repeat users (% with > 1 download)
- DAU/MAU engagement (stickiness)

### 5.3 Charts

- **Downloads per day** - bar chart (30 days)
- **New users per day** - line chart with fill (30 days)
- **DAU/MAU engagement** - daily DAU and stickiness chart
- **Platform usage** - doughnut chart and user-reach bars

### 5.4 Cohort analysis

Retention table by weekly cohort (W0-W8), with color showing intensity.

### 5.5 WebUI security

| Measure | Implementation |
|---|---|
| **Fail2ban** | N attempts → block IP for M minutes (customizable) |
| **Timing-safe comparison** | `hmac.compare_digest` for credentials |
| **Signed sessions** | `SessionMiddleware` uses `WEB_SECRET_KEY` |
| **Jinja2 autoescape** | Escapes HTML by default |
| **Swagger disabled** | `docs_url=None, redoc_url=None` |
| **Input length** | Username and password are limited to 128 characters |

---

## 6. Administration

### 6.1 Bot commands

| Command | Purpose |
|---|---|
| `/admin` | Control panel for cookies (download, health check) |
| `/cache_stats` | Cache statistics: number of records, breakdown by platform |
| `/cleanup_cache` | Manual cleaning of records older than 90 days |
| `/search_cache <query>` | Search the cache by video name |

### 6.2 Cookie management

Through `/admin`, an administrator can:

1. **Upload** Netscape cookie files for YouTube, TikTok, and Instagram (up to 1 MB).
2. **Health check** - validation of each cookie with a detailed status:
   - ✅ Valid - working cookies
   - ❌ Expired / Stale / Invalid Format
   - ⚠️ Rate Limited / Probe Failed

### 6.3 Automated tasks

| Task | Schedule | Purpose |
|---|---|---|
| Cache cleanup | Every 24 hours | Delete entries older than 90 days |
| Database `VACUUM` | Every seven days | Reduce SQLite file size |
| yt-dlp update | At startup if enabled in this dated design | Upgrade from the selected channel; current releases use a pinned image |

### 6.4 Crash reports

On an unhandled exception, the bot sends a crash report to administrators:

- The error code (format: `PREFIX-CATEGORY-RANDOM`, for example `YT-TIMEOUT-A3F2B1`)
- The platform, the stage, the URL, the session ID.
- Exception type and traceback
- The status of cookies (automatically checked)

---

## 7. Safeguards and limits

### 7.1 Anti-spam

- **Limit:** 4 requests in 5 seconds → cooldown 10 seconds
- **Domain:** Per-user, applies to downloads (not to navigation)
- **Parallel sessions:** Maximum of 5 active menus for each user

### 7.2 Content limits

| Limit | Value |
|---|---|
| Maximum video duration | 3 hours |
| Local Bot API file limit | 2 GB |
| Cloud Bot API file limit | 50 MB |
| Instagram Stories | Not supported |
| Instagram Audio posts | Not supported |

### 7.3 Data security

- SQL queries - only parameterized (`?`)
- Errors - do not reveal internal structure or patterns to the user
- Secrets - stored in `.secrets/`, not in git
- Cookies - downloaded by administrators only, validated before use

---

## 8. Deployment

### 8.1 Docker (recommended)

```bash
mkdir -p .secrets
cp .env.example .secrets/.env
# Set TELEGRAM_TOKEN, ADMIN_IDS, TELEGRAM_API_ID, and
# TELEGRAM_API_HASH, then change WEB_PASSWORD
docker compose --env-file .secrets/.env up -d
```

The stack has three services: `bot` for Telegram updates and delivery, `web` for the dashboard on `WEB_PORT` (default 8080), and `telegram-bot-api` on the internal Compose network.

The `bot-data` volume stores SQLite databases. `shared-media` is the temporary directory shared by the bot and local API.

### 8.2 Direct run

```bash
pip install -r requirements.txt
# Configure .secrets/.env
python main.py           # bot
python -m web            # dashboard in a separate process
```

**System dependencies:** Python 3.14+, FFmpeg, and Git. A direct run uses the cloud Bot API with a 50 MB limit.

### 8.3 Published image (GHCR)

When a `v*` tag is pushed, GitHub Actions verifies the semver tag belongs to `main`, runs lint and tests, builds a canonical image digest with SBOM and provenance, checks it with Trivy and smoke tests, assigns release tags, and creates the changelog and GitHub Release.

```bash
TAG=1.0.0 docker compose --env-file .secrets/.env up -d
```

### 8.4 CI/CD pipeline

```
Push/PR → CI:
  ├── Workflow lint (actionlint) + Python lint (ruff)
  ├── Full tests + coverage ≥70% (Python 3.14)
  └── Cached Docker build + smoke checks

Tag v* → Release:
  ├── Validate semver tag and main ancestry
  ├── Full tests + lint + coverage
  ├── Build/push canonical digest with SBOM/provenance
  ├── Smoke digest → publish GHCR tags
  ├── Changelog generation
  └── GitHub Release
```

---

## 9. Configuration

### 9.1 Required variables

| Variable | Purpose |
|---|---|
| `TELEGRAM_TOKEN` | Bot token from @BotFather |
| `ADMIN_IDS` | Comma-separated administrator IDs |
| `TELEGRAM_API_ID` | Application ID from my.telegram.org |
| `TELEGRAM_API_HASH` | Application hash from my.telegram.org |

### 9.2 Optional bot settings

| Variable | Default | Purpose |
|---|---|---|
| `DOWNLOAD_WORKERS` | `8` | ThreadPoolExecutor worker count |
| `BLOCKING_TASK_TIMEOUT` | `600` | Timeout of blocking operations (seconds) |
| `LOG_LEVEL` | `INFO` | Logging level |
| `YTDLP_CLI_FALLBACK` | `true` | CLI fallback when the API fails |

### 9.3 Optional WebUI settings

| Variable | Default | Purpose |
|---|---|---|
| `WEB_USERNAME` | `admin` | Dashboard username |
| `WEB_PASSWORD` | `changeme` | Dashboard password; change it |
| `WEB_SECRET_KEY` | Generated | Session signing key |
| `WEB_PORT` | `8080` | The dashboard port |
| `FAIL2BAN_RETRIES` | `5` | Failed attempts before IP lockout |
| `FAIL2BAN_TIME` | `10m` | Lockout duration (`10m`, `1h`, `300`) |

---

## 10. Project structure

| Path | Role |
|---|---|
| `main.py` | Event loop, handlers, scheduled tasks, and shutdown |
| `config.py` | Typed environment parsing |
| `messages.py` | Bot-facing messages |
| `utils/telegram_utils.py` | Commands, callbacks, URL handling, and delivery |
| `utils/youtube_utils.py` | YouTube yt-dlp, cookies, retry, and CLI fallback |
| `utils/tiktok_instagram_utils.py` | TikTok and Instagram download paths |
| `utils/media_processor.py` | FFmpeg audio, conversion, and merging |
| `utils/video_cache.py` | SQLite file ID cache with WAL and 90-day TTL |
| `utils/analytics_db.py` | SQLite users, events, and metrics |
| `utils/ytdlp_runtime.py` | yt-dlp runtime and version reporting |
| `utils/cookie_manager.py`, `utils/cookie_health.py` | Administrator cookie management and validation |
| `utils/logger.py`, `utils/cache_commands.py`, `utils/temp_file_manager.py` | Logging, cache commands, and temporary media |
| `web/` | FastAPI application, Jinja2 templates, and CSS |
| `tests/` | Syntax, unit, integration, and regression tests |
| `docs/` | Project documentation |
| `.github/workflows/` | CI and GHCR release |
| `Dockerfile`, `Dockerfile.telegram-bot-api` | Nuvio and local Bot API images |
| `compose.yaml`, `compose.dev.yaml` | Published-image and source-build stacks |
| `requirements*.in`, `requirements*.txt` | Direct dependencies and hashed lock files |

---

## 11. Dependencies

| Package | Purpose |
|---|---|
| `python-telegram-bot[job-queue]` 22.8 | Telegram Bot API (async) |
| `yt-dlp[default]` 2026.9.27.232945.dev0 | Downloading videos from platforms |
| `curl_cffi` 0.16.0 | Impersonation of HTTP requests from browsers |
| `httpx` 0.28.1 | HTTP client |
| `python-dotenv` 1.2.2 | Load environment settings |
| `fastapi` 0.141.1 | WebUI |
| `uvicorn[standard]` 0.53.0 | ASGI server for the WebUI |
| `jinja2` 3.1.6 | Template engine |
| `itsdangerous` 2.2.0 | Signed sessions |
| `python-multipart` 0.0.32 | Processing of forms |
| `pytest` 9.1.1 | Testing |

**System requirements:** Python 3.14+, FFmpeg, Git, and Deno for the full YouTube format set.

---

## 12. Roadmap

> Open roadmap - priorities are defined by the community through Issues and Discussions.

### Potential directions

- Support for additional platforms (Twitter/X, VK Video, Twitch clips)
- Localization of the bot interface (i18n)
- Webhook mode for production (instead of polling)
- Prometheus metrics for the monitoring
- Download queue (Redis/RabbitMQ) to be scaled horizontally
- PWA version of the WebUI

---

## 13. Contributing

Nuvio is an open-source project licensed under the Apache 2.0 license.

1. Fork the repository, create a branch, and open a PR to `main`.
2. CI runs Ruff and tests on Python 3.14.
3. Commit prefixes such as `feat:`, `fix:`, `ci:`, and `docs:` feed the changelog.
4. Use Issues for bugs and suggestions.

---

*This dated PRD should be read with the current README and technical guides.*
