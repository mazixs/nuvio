<p align="center"><img src="web/static/logo.svg" alt="Nuvio" width="64" height="64"></p>
<h1 align="center">Nuvio</h1>
<p align="center">English · <a href="README.ru.md">Русский</a></p>

Nuvio is an asynchronous Telegram bot for downloading video, photo posts, and audio from YouTube, TikTok, Instagram, Rutube, and VK Video. It includes an analytics WebUI, a Telegram file ID cache, and pinned yt-dlp updates.

## Features

- YouTube videos and Shorts, TikTok videos and photo posts, Instagram posts, reels and carousels, Rutube, and VK Video.
- YouTube audio selection prefers a Russian track when one is available within the Telegram size limit. The audio menu identifies track languages.
- Photo posts are delivered as images, with audio sent separately when available.
- Audio extraction through FFmpeg, including MP3 192k for video sources.
- A persistent Telegram file ID cache supports videos, photos, ordered albums, and audio; optional cleanup is controlled in the WebUI.
- Files up to 2 GB can be sent through the local Telegram Bot API. Direct runs use the cloud API and its 50 MB limit.
- Temporary downloaded media is removed after delivery or failure.
- Spam protection and administrator commands for cache and cookie management.
- CSI surveys with feedback and NPS/CSI metrics. Survey frequency is adjustable in the WebUI without restarting the bot.
- General feedback through `/feedback` or an error-message button, with reports managed in the WebUI.
- User blocking and unblocking from WebUI profiles, applied to new bot requests without a restart.
- A FastAPI analytics WebUI. English is the default language; Russian is available from the language switch.

### Platform notes

- Rutube supports standard videos, Shorts, embeds, and playlists without cookies.
- VK Video supports videos, clips, wall posts, and completed live streams. For direct MP4 files, the bot chooses the best quality within the Telegram size limit. Active live streams are not sent as files.
- TikTok photo posts and Instagram carousels are sent as image sets. A post without audio still delivers its images.

## Quick start

Requirements: Python 3.14+, FFmpeg, and a Telegram bot token from [@BotFather](https://t.me/BotFather). Deno 2.3+ provides the full set of YouTube formats and is included in the Docker image. Docker also needs a Telegram application ID and hash from [my.telegram.org](https://my.telegram.org).

```bash
git clone https://github.com/mazixs/nuvio.git
cd nuvio
python -m venv .venv
source .venv/bin/activate
python -m pip install --requirement requirements.txt
mkdir -p .secrets
cp .env.example .secrets/.env
```

Set `TELEGRAM_TOKEN` and `ADMIN_IDS` in `.secrets/.env`. For Docker, also set `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`, and change `WEB_PASSWORD`.

Run the bot and WebUI in separate terminals:

```bash
python main.py
python -m web
```

The WebUI is available at `http://localhost:8080` by default. A direct bot run uses the cloud Telegram Bot API and has a 50 MB file limit. Use the Docker stack for larger files.

### Docker

```bash
# Build from this checkout
docker compose --env-file .secrets/.env -f compose.yaml -f compose.dev.yaml up -d --build

# Use the published Nuvio image from GHCR
docker compose --env-file .secrets/.env up -d
```

The local Bot API port is not exposed to the host. See the [deployment guide](docs/guides/deployment.md) for ports, volumes, passwords, cookies, and systemd setup.

## Configuration

The sole environment template is [`.env.example`](.env.example). Keep the working copy in `.secrets/.env`.

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_TOKEN` | required | Token issued by @BotFather |
| `ADMIN_IDS` | required | Comma-separated administrator IDs |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | required for Docker | Telegram application credentials |
| `WEB_USERNAME`, `WEB_PASSWORD` | `admin`, `changeme` | WebUI sign-in credentials; change the password |
| `WEB_SECRET_KEY` | generated on startup | Session signing key; set it to keep sessions across restarts |
| `WEB_PORT` | `8080` | WebUI port |
| `DOWNLOAD_WORKERS` | `8` | Worker threads for blocking downloads and FFmpeg tasks |
| `BLOCKING_TASK_TIMEOUT` | `600` | Blocking task timeout in seconds |
| `TIKTOK_FAST_PATH` | `true` | Direct TikTok H.264 path; cached links retain their previous quality |
| `INSTAGRAM_FAST_PATH` | `true` | Direct Instagram GraphQL media path; keeps the same quality |
| `CANARY_ENABLED` | `false` | Scheduled YouTube download check |
| `DATA_DIR` | repository root | SQLite database directory |
| `TEMP_DIR` | `./temp` | Temporary media directory |

The [configuration reference](docs/guides/configuration.md) lists all variables, including platform fast paths, cookies, size limits, fail2ban, and yt-dlp fallback settings. The [error code reference](docs/error-codes.md) explains user-visible error IDs.

**Media cache:** Valid Telegram references and source links have no expiration by default. The WebUI settings let you enable cleanup and choose days since last successful reuse, without environment variables or a restart. `/cleanup_cache` applies that same policy and does nothing while cleanup is disabled. Temporary downloads are cleaned independently. The cache separates bots, processing recipes, and actual Telegram media types. Changing `TIKTOK_FAST_PATH` now uses a different recipe and does not reuse the former quality. Legacy references without verified bot/type/recipe identity remain inspectable but are not promoted automatically. See [cache operation and migration](docs/guides/cache-and-delivery.md).

## Bot commands

| Command | Purpose | Access |
|---|---|---|
| `/start` | Welcome and quick help | Everyone |
| `/help` | Detailed usage help | Everyone |
| `/download <URL>` | Download media; sending a URL directly also works | Everyone |
| `/admin` | Cookie administration | Administrators |
| `/cache_stats` | File ID cache statistics | Administrators |
| `/cleanup_cache` | Remove expired cache entries | Administrators |
| `/search_cache` | Search cached file IDs | Administrators |

## Development and releases

Install the development dependencies, then run the same checks used in CI:

```bash
python -m pip install --requirement requirements-dev.txt
ruff check --output-format=github .
coverage run --branch -m pytest tests/
coverage report --fail-under=70
```

The test suite replaces external yt-dlp, FFmpeg, and Telegram boundaries. Runtime behavior against a live platform requires a separate live check.

Update the pinned yt-dlp version with `python scripts/update_ytdlp.py <version>`. The script updates the pin and both lock files and checks the code. A weekly GitHub workflow proposes new nightly versions in a separate PR. Running bots receive a new version only after a new image is released and pulled.

CI runs linting, tests, Docker smoke checks, and vulnerability scanning on pushes and pull requests. A `v*` tag triggers the release workflow, which verifies the published image before assigning GHCR tags and creating a GitHub Release.

## Security

SQLite queries are parameterized. WebUI credential checks use timing-safe comparison, sign-in attempts are rate limited by IP, and sessions are signed with `WEB_SECRET_KEY`. The WebUI disables Swagger and ReDoc, and Jinja2 escapes HTML. Users receive safe error summaries; operational details stay in administrative logs. Cookie files are stored with owner-only permissions.

## Documentation

- [Documentation index](docs/index.md)
- [Feedback and moderation](docs/guides/feedback-and-moderation.md)
- [Architecture](docs/technical/architecture.md)
- [YouTube incident runbook](docs/technical/youtube-download-runbook.md)
- [Contributor guide](docs/development/contributing.md)
- [Troubleshooting](docs/troubleshooting/common-issues.md)

The code is organized around `main.py` (bot lifecycle), `config.py` (settings), `messages.py` (bot text), `utils/` (media and delivery logic), `web/` (analytics WebUI), and `tests/`. The [architecture guide](docs/technical/architecture.md) describes the modules and their interactions.

## License

See [LICENSE](LICENSE).
