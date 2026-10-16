# Dependency, Configuration, and Local Telegram Bot API Update

Date: 2026-07-23
Status: Implemented
> Archive of the adopted solution. Current startup commands and parameters are located in `README.md`, `docs/guides/deployment.md`, and `docs/guides/configuration.md`.

## 1. Objective

Update the operational environment of Nuvio without altering user workflows:
1. Lock in current versions of Python dependencies and GitHub Actions;
2. Make `.env.example` the sole configuration template, and `.secrets/.env` the only active configuration file;
3. Remove Gokapi and send files up to 2 GB via the local Telegram Bot API;
4. Run the bot, WebUI, and Telegram Bot API within a single Compose project, but in separate containers;
5. Maintain clear user-facing error messages without exposing internal issues such as cookie state or local API status.

## 2. Scope of Changes

Changes include:
- Python dependencies, Python version in the image, and GitHub Actions versions;
- Structure of dependency files;
- `.env.example`, configuration loading, and Compose files;
- Integration of `python-telegram-bot` with the local Telegram Bot API;
- Limiting media size, sending local files, and removing Gokapi;
- Tests, deployment documentation, and migration instructions.

Excluded:
- Automatic switching between cloud and local Telegram Bot API;
- Exposing the Telegram Bot API port externally;
- Changes to WebUI functionality;
- Automatic call to `logOut` for the working bot;
- Implementation of previously designed Instagram Highlight support.

## 3. Locked Versions and Dependency Management

As of the review date, the following direct dependencies are in use:

| Package            | Version     |
|--------------------|-------------|
| `python-telegram-bot` | 22.8        |
| `yt-dlp`            | 2026.7.4    |
| `curl_cffi`         | 0.15.0      |
| `httpx`             | 0.28.1      |
| `python-dotenv`     | 1.2.2       |
| `fastapi`           | 0.139.2     |
| `uvicorn`           | 0.51.0      |
| `jinja2`            | 3.1.6       |
| `itsdangerous`      | 2.2.0       |
| `python-multipart`  | 0.0.32      |
| `pytest`            | 9.1.1       |

`pydantic-core` is not updated separately: its version is determined by the strict dependency of the current `pydantic`.

### 3.1. Dependency Files

Proposed separation of declarations and reproducible installation:
- `requirements.in` - direct application dependencies with exact, verified versions;
- `requirements-dev.in` - includes `-r requirements.in`, `pytest`, and `ruff`;
- `requirements.txt` - compiled full set for the production image;
- `requirements-dev.txt` - compiled full set for development and CI.

Both final files are generated via `uv pip compile --generate-hashes`. The production Docker image installs only `requirements.txt`; CI installs `requirements-dev.txt`. This excludes `pytest` and `ruff` from the production image and ensures reproducible builds.

### 3.2. Python and GitHub Actions

- Base image is migrated from `python:3.13-slim` to `python:3.14-slim`;
- CI checks both Python 3.13 and 3.14, while Python 3.13 remains the officially declared minimum version;
- `actions/checkout` is updated to version `v7`;
- `actions/setup-python` is updated to version `v7`;
- All other used actions remain on their current main versions.

Automatic updating of `yt-dlp` on container startup is disabled by default. The production image must start with a verified version from the lock file. Nightly and master channels remain available for diagnostic purposes but do not alter the working container environment without operator intervention.

## 4. Unified Environment Configuration

### 4.1. Configuration Source

Only the following are used:
- `.env.example` - versioned template without secrets;
- `.secrets/.env` - local working file, excluded from Git.
Preparation command:

```bash
mkdir -p .secrets
cp .env.example .secrets/.env
```

Compose receives the same file for variable substitution using `${VARIABLE}` and as an `env_file` for services. The root `.env` file is no longer required. Support for reading old `.env.local` and `.env` files in Python is preserved for one transitional cycle only when running without Docker; it is marked as deprecated and not used in documentation.

### 4.2. `.env.example` Template

The template contains only operator-configurable parameters:
- `TELEGRAM_TOKEN`, `ADMIN_IDS`;
- `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`;
- `WEB_USERNAME`, `WEB_PASSWORD`, `WEB_SECRET_KEY`, `WEB_PORT`;
- `DOWNLOAD_WORKERS`, `BLOCKING_TASK_TIMEOUT`, `LOG_LEVEL`;
- `FAIL2BAN_RETRIES`, `FAIL2BAN_TIME`;
- paths to cookies if needed;
- `yt-dlp` parameters, including disabled default auto-updating;
- `TAG` for selecting the image version.

Internal container paths (`DATA_DIR`, media exchange directory, internal service addresses) are defined in Compose and are not duplicated in the user-provided template.

Gokapi variables are removed. The outdated instruction about manually adding `python-dotenv` is removed.

### 4.3. Configuration Validation

- `TELEGRAM_TOKEN` and `ADMIN_IDS` are required for the bot;
- `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` are required for the local Telegram Bot API;
- empty values and the default template password `changeme` are not allowed for the WebUI;
- secrets are never logged;
- user messages do not contain information about cookies, internal services, or reasons for bot authentication.

## 5. Compose Structure

Instead of two nearly identical files, the following are introduced:
- `compose.yaml` - the main working configuration with images from GHCR;
- `compose.dev.yaml` - a small addition for local builds from source.

Services:
1. `telegram-bot-api` - a local API accessible only within the internal network;
2. `bot` - the main Telegram bot;
3. `web` - the WebUI.

Shared volume mounts:
- Nuvio data;
- Telegram Bot API state;
- media exchange directory between `bot` and `telegram-bot-api`.

The media exchange directory is mounted into both containers at the same absolute path. This is a mandatory requirement for local mode: the path passed to the library must point to the same file inside the API container.

The `telegram-bot-api` service does not expose a port to the host. The `bot` service depends on its readiness check. The WebUI still exposes only the configured port.

## 6. Telegram Bot API Image

Since the official TDLib project does not publish an official Docker image, a separate multi-stage `Dockerfile.telegram-bot-api` is created in the repository:
- source code is taken from the official `tdlib/telegram-bot-api`;
- the revision is fixed using a full commit identifier;
- compilation is performed in the build stage;
- only the binary and required libraries are copied into the final minimal image;
- the server is started with `--local`;
- `api_id` and `api_hash` are provided only via environment variables;
- state is stored in a separate persistent volume.

Updates to the fixed revision are performed deliberately, alongside build verification and migration notes for the Telegram Bot API.

## 7. Application Integration with Local API

### 7.1. Client Configuration

Internal parameters are added to the application configuration:
- base URL for bot methods: `http://telegram-bot-api:8081/bot`;
- base URL for files: `http://telegram-bot-api:8081/file/bot`;
- local mode;
- maximum file size for uploads, defaulting to 2000 MB.

When creating an `Application`, the following are used:
- `base_url(...)`;
- `base_file_url(...)`;
- `local_mode(True)`;
- increased `media_write_timeout(...)`.
Cloud API mode remains available for use without Compose: if the local mode is disabled, standard Telegram addresses and a 50 MB limit are used.

### 7.2. File Handling

In the internal interface, the download result is always presented as a local `Path`. In local mode, `python-telegram-bot` sends the server an absolute path; in cloud mode, the library sends the file content directly.
`MAX_FILE_SIZE` is no longer a fixed 50 MB value - it is calculated based on the mode and configuration. All format filters for YouTube, TikTok, Instagram, Rutube, and VK use the same limit.
If the final file exceeds the set limit, it is not uploaded to the external service. The user receives a general message:
> Failed to retrieve this material from the specified source. The link may require authentication, the content may have been removed, or the link may no longer be valid.
The specific source is inserted into the existing notification system. Internal reasons and error codes are logged.

### 7.3. Removal of Gokapi

The following are removed:
- `utils/gokapi_utils.py`;
- Gokapi settings and checks;
- branches returning external links instead of files;
- signatures and menu items related to Gokapi;
- documentation and tests for external file export.
After these changes, the external file service is no longer required.

## 8. Migration of the Working Bot

Telegram does not support reliable simultaneous operation of a single token through both cloud and local Bot APIs. Therefore, migration is performed with a short scheduled downtime:
1. Stop the current bot;
2. Call `logOut` in the cloud Bot API for its token;
3. Start the Compose project with local API;
4. Wait for the API to become ready and for the bot to start;
5. Verify receipt of updates and sending of a small file;
6. Verify sending of a file between 60–100 MB;
7. Only after successful verification is the migration considered complete.
The `logOut` operation is not performed automatically by the application because it changes the service mode for the working token.
To return to the cloud API, the local bot is stopped, `logOut` is executed on the local server, and then the application is restarted with the local mode disabled.

## 9. Failures and User Messages

| Situation | Behavior |
|---------|---------|
| Local API not ready at startup | Compose does not start the bot until readiness is confirmed |
| API becomes unavailable during operation | Request ends with a general error; details are logged |
| File larger than 2 GB | General error when retrieving material; file is deleted |
| Insufficient disk space | General error; temporary files are cleared; details logged |
| Link requires authentication or content has been removed | General message with possible external reasons |
| Cookies have expired or the service account lacks access | This internal detail is not shown to the user |
Automatic switching to the cloud API is not available: after `logOut`, there is a risk of losing updates and unpredictable behavior.

## 10. Security and Operational Limits

- The Telegram Bot API port is accessible only to Compose project containers;
- Tokens, `api_id`, `api_hash`, cookies, and passwords do not appear in images, Git repositories, or logs;
- The media directory is accessible only to containers that need it;
- Temporary files are deleted after successful delivery and after errors;
- Operators must ensure sufficient free disk space - at least the product of the maximum file size and the number of simultaneous uploads, with a buffer for re-encoding;
- The local Telegram Bot API eliminates dependency on Gokapi but does not eliminate network dependency on Telegram data centers.

## 11. Verification

### 11.1. Automated Tests

- Parsing and validation of new environment variables;
- Selection of cloud and local modes;
- Configuration of `ApplicationBuilder`;
- Unified size limit for all loaders;
- Return of `Path` instead of URL;
- Temporary file deletion upon limit exceedance or error;
- No user-facing messages about cookies or internal services;
- Full set of `pytest`;
- `ruff check`.

### 11.2. Infrastructure Checks

- `docker compose --env-file .secrets/.env config`;
- Building the main image;
- Building the Telegram Bot API image;
- Checking API readiness;
- Starting services with temporary test values without publishing the API port.

### 11.3. Manual Acceptance

- Regular video under 50 MB;
- A 60–100 MB file confirming cloud limit bypass;
- A file close to the configured limit;
- An inaccessible, deleted, or authorization-required link;
- Container restart with state preservation;
- Verification of no secrets in logs and output of `docker compose config`.

## 12. Readiness Criteria

A change is considered complete when:
1. Direct dependencies are pinned to verified, up-to-date versions, and production and test dependencies are separated;
2. CI passes on Python 3.13 and 3.14 with current major versions of Actions;
3. `docker compose` does not require a root `.env` file;
4. Documentation flows only from `.env.example` to `.secrets/.env`;
5. All three services are launched within a single Compose project;
6. The Telegram Bot API is not accessible from outside;
7. The bot sends a local file larger than 50 MB;
8. Gokapi is completely removed from the code, configurations, and documentation;
9. Internal authentication and cookie reasons are not shown to users;
10. `pytest`, `ruff`, and building both images pass successfully.
