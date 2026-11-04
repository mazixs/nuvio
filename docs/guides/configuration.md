# Configuration reference

The only settings template is `.env.example`. Keep the working copy in `.secrets/.env`, which is excluded from version control.

## Required variables

| Variable | Used by | Description |
|---|---|---|
| `TELEGRAM_TOKEN` | Bot | Token from @BotFather |
| `ADMIN_IDS` | Bot | Comma-separated administrator IDs |
| `TELEGRAM_API_ID` | Docker | Telegram application ID from my.telegram.org |
| `TELEGRAM_API_HASH` | Docker | Telegram application hash from my.telegram.org |

Create `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` in **API development tools** at [my.telegram.org](https://my.telegram.org). These are Telegram application credentials, not a bot token or a user's password.

## Local Telegram Bot API

`compose.yaml` already sets these values. They usually need no manual changes.

| Variable | Compose value | Description |
|---|---|---|
| `TELEGRAM_LOCAL_MODE` | `true` | Enables local-path delivery |
| `TELEGRAM_BOT_API_BASE_URL` | `http://telegram-bot-api:8081/bot` | Internal API address |
| `TELEGRAM_BOT_API_FILE_URL` | `http://telegram-bot-api:8081/file/bot` | Internal file endpoint |
| `TEMP_DIR` | `/app/media` | Shared temporary media volume for the bot and Bot API |

The delivery limit follows the mode: 2,000 MB in local mode, 50 MB for a direct Python run, which uses `api.telegram.org`. `TELEGRAM_MAX_FILE_SIZE_MB` can only lower it.

## WebUI

| Variable | Default | Description |
|---|---|---|
| `WEB_USERNAME` | `admin` | Sign-in username |
| `WEB_PASSWORD` | - | Sign-in password; sign-in stays closed until it is set |
| `WEB_SECRET_KEY` | random at startup | Session signing key; set it to preserve sessions across restarts. One shorter than 32 characters is replaced by a random key |
| `WEB_PORT` | `8080` | Published port |
| `FAIL2BAN_RETRIES` | `5` | Failed sign-in attempts before an IP is blocked |
| `FAIL2BAN_TIME` | `10m` | IP lockout duration |

The WebUI opens in English by default. Use its EN/RU switch to select Russian; the choice is kept in the browser session.

Set a stable `WEB_SECRET_KEY` to keep signed sessions valid across restarts. Generate a 64-character hex value with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

## HTTP API

| Variable | Default | Description |
|---|---|---|
| `API_PORT` | `8090` | Listening port |
| `API_DOWNLOAD_WORKERS` | `4` | Concurrent downloads across all keys, separate from `DOWNLOAD_WORKERS` |
| `API_MAX_ACTIVE_JOBS` | `2` | Queued and running jobs per key |
| `API_RATE_LIMIT_PER_MINUTE` | `120` | Requests per key per minute |
| `API_RESULT_TTL_MINUTES` | `60` | Minutes a finished file stays available, from 5 to 1,440 |
| `API_MAX_FILE_SIZE_MB` | `2000` | Largest file the API downloads, from 1 to 2,000; `TELEGRAM_*` limits do not apply to it |
| `API_STORAGE_LIMIT_MB` | `10000` | Disk space for finished files of all keys together; the oldest results expire early above it |

Invalid or out-of-range values fall back to the default with a warning in the log. Keys are managed in the WebUI, not through environment variables. See the [HTTP API guide](api.md).

## Downloads and processing

| Variable | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Log level |
| `DOWNLOAD_WORKERS` | `8` | Worker threads for blocking operations |
| `BLOCKING_TASK_TIMEOUT` | `600` | Blocking task timeout in seconds |
| `YTDLP_CLI_FALLBACK` | `true` | Use yt-dlp CLI as a fallback |
| `YTDLP_CLI_TIMEOUT` | `900` | CLI timeout in seconds |

See `.env.example` for platform fast-path switches and canary settings.

## Cookies

The bot reads `www.youtube.com_cookies.txt`, `www.instagram.com_cookies.txt`, and `www.tiktok.com_cookies.txt` from `.secrets/`.

Cookies are optional for public media. Administrators can also update them with the bot's `/admin` command.

## Compose

| Variable | Default | Description |
|---|---|---|
| `TAG` | `latest` | Published Nuvio image tag |
| `NUVIO_ENV_FILE` | `.secrets/.env` | Settings file passed to the containers |
| `COMPOSE_PROFILES` | - | Set to `api` to start the HTTP API without `--profile api` |

An empty number falls back to its default, but other settings stay empty, so leave a setting commented out to keep its default.

## Storage

- `bot-data` stores SQLite databases across container restarts.
- `telegram-bot-api-data` stores the local Bot API's state.
- `shared-media` is temporary storage used for processing and delivery. Files are removed after success or failure; the bot also cleans it at startup and shutdown.
- `logs/` and `.secrets/` are mounted from the host.
