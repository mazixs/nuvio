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
| `TELEGRAM_MAX_FILE_SIZE_MB` | `2000` | Maximum file size for delivery |
| `TEMP_DIR` | `/app/media` | Shared temporary media volume for the bot and Bot API |

A direct Python run uses `api.telegram.org` with local mode disabled. Its delivery limit is capped at 50 MB.

## WebUI

| Variable | Default | Description |
|---|---|---|
| `WEB_USERNAME` | `admin` | Sign-in username |
| `WEB_PASSWORD` | `changeme` | Sign-in password; change it before deployment |
| `WEB_SECRET_KEY` | random at startup | Session signing key; set it to preserve sessions across restarts |
| `WEB_PORT` | `8080` | Published port |
| `FAIL2BAN_RETRIES` | `5` | Failed sign-in attempts before an IP is blocked |
| `FAIL2BAN_TIME` | `10m` | IP lockout duration |

The WebUI opens in English by default. Use its EN/RU switch to select Russian; the choice is kept in the browser session.

Set a stable `WEB_SECRET_KEY` to keep signed sessions valid across restarts. Generate a 64-character hex value with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

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

| Variable | Default path |
|---|---|
| `YOUTUBE_COOKIES_FILE` | `.secrets/www.youtube.com_cookies.txt` |
| `INSTAGRAM_COOKIES_FILE` | `.secrets/www.instagram.com_cookies.txt` |
| `TIKTOK_COOKIES_FILE` | `.secrets/www.tiktok.com_cookies.txt` |

Cookies are optional for public media. Administrators can also update them with the bot's `/admin` command.

## Storage

- `bot-data` stores SQLite databases across container restarts.
- `telegram-bot-api-data` stores the local Bot API's state.
- `shared-media` is temporary storage used for processing and delivery. Files are removed after success or failure; the bot also cleans it at startup and shutdown.
- `logs/` and `.secrets/` are mounted from the host.
