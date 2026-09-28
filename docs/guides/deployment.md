# Deploying Nuvio

## Requirements

- Docker Engine with Compose v2.
- A bot token from [@BotFather](https://t.me/BotFather).
- A Telegram application `API_ID` and `API_HASH` from [my.telegram.org](https://my.telegram.org).
- Enough free disk space to process files up to 2 GB.

Create the application credentials under **API development tools**. They do not replace `TELEGRAM_TOKEN` and do not give the bot access to a personal Telegram account.

## Prepare the environment

```bash
git clone https://github.com/mazixs/nuvio.git
cd nuvio
mkdir -p .secrets
cp .env.example .secrets/.env
```

Set these values in `.secrets/.env`:

```env
TELEGRAM_TOKEN=1234567890:ABCdef...
ADMIN_IDS=123456789
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
WEB_PASSWORD=replace-me
WEB_SECRET_KEY=replace-with-a-random-value
```

## First switch from the cloud Bot API

A bot token cannot be used through the cloud and local Bot APIs at the same time. Switch manually:

1. Stop the previous Nuvio instance.
2. Log the token out of the cloud Bot API:

   ```bash
   curl "https://api.telegram.org/bot<BOT_TOKEN>/logOut"
   ```

3. Start the new stack:

   ```bash
   docker compose --env-file .secrets/.env up -d
   ```

`logOut` is deliberately not built into the container. An automatic call could interrupt another running bot instance.

## Services

| Service | Purpose | Host access |
|---|---|---|
| `telegram-bot-api` | Local Telegram Bot API in `--local` mode | Not published |
| `bot` | Download, processing, and delivery | Not published |
| `web` | Analytics dashboard | `${WEB_PORT:-8080}` |

The bot and local Bot API share `/app/media`. The bot passes an absolute file path, so a large file does not need to be uploaded to an external intermediate store.

## Start

Use the published Nuvio image from GHCR:

```bash
docker compose --env-file .secrets/.env pull bot web
docker compose --env-file .secrets/.env up -d
```

Or build Nuvio from the current checkout:

```bash
docker compose \
  --env-file .secrets/.env \
  -f compose.yaml \
  -f compose.dev.yaml \
  up -d --build
```

Check the services and logs:

```bash
docker compose --env-file .secrets/.env ps
docker compose --env-file .secrets/.env logs -f bot telegram-bot-api
```

The WebUI is at `http://<server-address>:<WEB_PORT>`. It opens in English by default and offers a Russian language switch.

## Update and rollback

For the published image:

```bash
docker image inspect ghcr.io/mazixs/nuvio:latest --format '{{index .RepoDigests 0}}'
docker compose --env-file .secrets/.env pull
docker compose --env-file .secrets/.env up -d
docker compose --env-file .secrets/.env exec bot python -m scripts.check_media_runtime
```

Record the previous digest before updating. If the new image fails validation, pull the previous digest, tag it as `ghcr.io/mazixs/nuvio:rollback`, and start the stack with `TAG=rollback`. Publishing a new GitHub Release does not recreate the server containers.

```bash
docker pull ghcr.io/mazixs/nuvio@sha256:<previous-digest>
docker tag ghcr.io/mazixs/nuvio@sha256:<previous-digest> ghcr.io/mazixs/nuvio:rollback
TAG=rollback docker compose --env-file .secrets/.env up -d bot web
```

For a build from source:

```bash
git pull
docker compose \
  --env-file .secrets/.env \
  -f compose.yaml \
  -f compose.dev.yaml \
  up -d --build
```

## Storage and cleanup

| Volume or directory | Contents | Persistent |
|---|---|---|
| `bot-data` | Analytics and file ID cache SQLite databases; validated backups | Yes |
| `telegram-bot-api-data` | Local Bot API state | Yes |
| `shared-media` | Downloaded and processed media | No; cleaned |
| `./logs` | Rotating application logs | Yes |
| `./.secrets` | Environment and cookies | Yes |

Nuvio does not keep an archive of downloads. It removes temporary media after delivery or failure and cleans the temporary directory at startup and graceful shutdown. Abandoned directories older than one day are removed daily.

Analytics URLs are removed after 90 days, after a verified backup is created in `bot-data/backups`. The latest seven backups are retained. Events remain available for historical metrics.

The `shared-media` volume exists only so the bot and local Bot API can refer to the same file path.

## Cookies

Optional Netscape cookie files can be placed in `.secrets/`:

```text
.secrets/www.youtube.com_cookies.txt
.secrets/www.instagram.com_cookies.txt
.secrets/www.tiktok.com_cookies.txt
```

Administrators can also update them through `/admin`.

## Return to the cloud Bot API

1. Stop the `bot` and `web` services, leaving `telegram-bot-api` available.
2. Call `logOut` on the local API:

   ```bash
   curl "http://127.0.0.1:8081/bot<BOT_TOKEN>/logOut"
   ```

   Port 8081 is not published by default. Temporarily bind `127.0.0.1:8081:8081` for this operation, or make the request from inside the Compose network.

3. Start the application without `TELEGRAM_LOCAL_MODE`. It then uses `https://api.telegram.org` and the 50 MB delivery limit.

## Run without Docker

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
set -a
source .secrets/.env
set +a
python main.py
```

This uses the cloud Bot API by default. To use a local API outside Compose, provide a shared filesystem path and set `TELEGRAM_LOCAL_MODE`, `TELEGRAM_BOT_API_BASE_URL`, `TELEGRAM_BOT_API_FILE_URL`, `TELEGRAM_MAX_FILE_SIZE_MB`, and `TEMP_DIR` yourself.
