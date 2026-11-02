# HTTP API

The HTTP API lets external clients download video or audio by link from the same platforms as the bot: YouTube, TikTok, Instagram, Rutube, and VK Video. An administrator issues API keys in the WebUI; clients use a key to start a download, poll its status, and fetch the finished file.

The API runs as its own process (`python -m api`), separate from the bot and the WebUI. You can keep the WebUI private and publish only the API.

## Keys

Open **API keys** in the WebUI navigation.

- **Create** a key with a name (up to 64 characters) and an expiry: 7, 30, 90, or 365 days, a date (the key works until 23:59:59 UTC that day), or no expiry. An expiry cannot be more than 10 years away.
- **Copy the key immediately.** It is shown once. Nuvio stores only its SHA-256 hash, so a lost key cannot be recovered, only rotated. The list shows the first 12 characters to tell keys apart.
- **Change expiry** at any time. It applies to the next request.
- **Rotate** to issue a new secret for the same key. The name, expiry, and counters stay. Choose how long the old secret keeps working: not at all, 1 hour, 1 day, or 7 days. Rotating again during that period retires the oldest secret.
- **Revoke** to reject the key, including any secret still in its grace period, on the next request. Within a minute the API also cancels the key's queued and running jobs and removes its stored files. A revoked key stays in the list for history; **delete** removes it and is available only after revocation.

The list shows each key's status, creation time, last use (updated at most once a minute), and the number of accepted downloads.

Keys look like `nuvio_` followed by 38 letters and digits. The last six characters are a checksum, so a mistyped key is rejected without a database lookup, and secret scanners can recognize the prefix.

## Authentication

Send the key in a header:

```http
Authorization: Bearer nuvio_...
```

`X-API-Key: nuvio_...` is also accepted. A key in the query string is ignored, because URLs end up in proxy and server logs.

## Endpoints

| Method and path | Purpose |
|---|---|
| `GET /api/v1/me` | Key name, prefix, expiry, and limits |
| `POST /api/v1/downloads` | Start a download; returns `202` with the job |
| `GET /api/v1/downloads` | Jobs of this key, newest first |
| `GET /api/v1/downloads/{id}` | Job status |
| `GET /api/v1/downloads/{id}/file` | The finished file |
| `DELETE /api/v1/downloads/{id}` | Cancel a running job or delete a finished one with its file |
| `GET /health` | Liveness check without a key |

The interactive reference is at `/api/docs`, and the OpenAPI schema at `/api/v1/openapi.json`. Both are public and describe only these endpoints; the WebUI keeps its schema disabled. The reference page loads a pinned Swagger UI version from jsDelivr, so the browser needs access to that CDN. The schema itself has no such dependency.

A job belongs to the key that created it. Another key gets `404` for it, exactly as for a job that does not exist. A rotated key keeps its jobs, because jobs are tied to the key, not the secret.

`DELETE` on a running job returns `204` at once and the job disappears from the list, but the download stops at the next progress step of the worker. Until then the job still counts toward `API_MAX_ACTIVE_JOBS`, so a new download right after a cancellation can get `429` for a few seconds.

## Downloading

```bash
export NUVIO_API=https://api.example.com
export NUVIO_API_KEY=nuvio_...

curl -X POST "$NUVIO_API/api/v1/downloads" \
  -H "Authorization: Bearer $NUVIO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=6WFj-CKldv4", "type": "audio"}'
```

`type` is `video` (the default) or `audio`. The response is `202 Accepted` with a `Location` header pointing to the job and `Retry-After: 2`:

```json
{
  "id": "3f0c9a6c1d2b4e5f8a7b6c5d4e3f2a1b",
  "status": "queued",
  "type": "audio",
  "platform": "youtube",
  "url": "https://www.youtube.com/watch?v=6WFj-CKldv4",
  "created_at": "2026-10-31T12:00:00+00:00",
  "started_at": null,
  "finished_at": null,
  "file": null,
  "error": null,
  "links": {"self": "/api/v1/downloads/3f0c9a6c1d2b4e5f8a7b6c5d4e3f2a1b", "file": null}
}
```

Poll the job until its status is final. While it is `queued` or `running`, the response carries `Retry-After`:

```bash
curl "$NUVIO_API/api/v1/downloads/$JOB_ID" -H "Authorization: Bearer $NUVIO_API_KEY"
```

| Status | Meaning |
|---|---|
| `queued` | Waiting for a free worker |
| `running` | Downloading |
| `succeeded` | `file` holds the name, size, content type, and expiry; `links.file` is set |
| `failed` | `error` explains why |
| `cancelled` | The download was cancelled |
| `expired` | The file was removed; start a new download |

Fetch the file before `file.expires_at`:

```bash
curl -OJ "$NUVIO_API/api/v1/downloads/$JOB_ID/file" -H "Authorization: Bearer $NUVIO_API_KEY"
```

`Content-Disposition` carries the full name in `filename*` and an ASCII fallback in `filename`, which is what `curl -J` reads. Downloading the file does not delete it; call `DELETE` when you no longer need it to free disk space early.

A file can reach `API_MAX_FILE_SIZE_MB`, so write the response to disk as it arrives instead of holding it in memory. The endpoint supports `Range`, so an interrupted transfer can resume while the file is available. `curl` cannot combine resuming with `-J`, so name the file yourself:

```bash
curl -C - -o clip.mp4 "$NUVIO_API/api/v1/downloads/$JOB_ID/file" -H "Authorization: Bearer $NUVIO_API_KEY"
```

### What is downloaded

Format choices match the bot's video and audio buttons. YouTube video is the best quality that fits the size limit, with a Russian audio track preferred when available. YouTube audio is the original M4A track when one exists, otherwise a conversion to M4A. TikTok and Instagram use the same fast paths as the bot. VK uses the format the bot would send. Check `file.name` and `file.content_type` for the actual container.

Only a single video or audio track can be downloaded. The link must point to one publication: a YouTube video or Short, a TikTok video, an Instagram post or reel, a Rutube video, or a VK video, clip, or wall post. Channels, profiles, playlists, and search pages are rejected with `unsupported_url` before any download starts; a short TikTok link is expanded first and rejected the same way if it leads to a profile. Photo posts, carousels, Instagram Stories, and active VK live streams fail with `unsupported_media`.

## Errors

Every error uses the same body:

```json
{"error": {"code": "invalid_api_key", "message": "The API key is invalid or revoked."}}
```

| HTTP | Code | When |
|---|---|---|
| 400 | `http_error` | Any other refused request, such as a body that could not be read |
| 401 | `missing_api_key` | No key in the request |
| 401 | `invalid_api_key` | Unknown, revoked, or a rotated secret past its grace period |
| 401 | `api_key_expired` | The key's expiry has passed; the administrator can extend it |
| 404 | `not_found` | Unknown job, a job of another key, or an unknown path |
| 405 | `method_not_allowed` | The path does not support this method |
| 409 | `not_ready` | The file is requested while the job is still active |
| 409 | `no_file` | The job failed or was cancelled |
| 410 | `file_expired` | The file was removed after its expiry |
| 413 | `request_too_large` | The body is larger than 8 KB; refused before the key is checked |
| 422 | `invalid_request` | The body does not match the schema; `details` lists the fields |
| 422 | `unsupported_url` | Not an `http(s)` link to a single publication on a supported platform, or the link has credentials or a port |
| 429 | `rate_limited` | Too many requests for this key |
| 429 | `too_many_active_downloads` | The key already has the maximum number of queued and running jobs |
| 429 | `too_many_failed_attempts` | Too many invalid keys from this address |
| 500 | `internal_error` | Unexpected server failure; the details stay in the log |
| 503 | `service_unavailable` | The key database is busy; retry shortly |

`429`, `503`, and `not_ready` responses include `Retry-After` in seconds.

A failed job reports why in `error.code`: `unsupported_media`, `format_unavailable`, `duration_limit`, `file_too_large`, `media_unavailable`, `media_forbidden`, `platform_rate_limited`, `network_error`, `timeout`, or `download_failed`. Unexpected failures also carry `error.reference` in the bot's error code format, such as `YT-UNEXPECT-52D16C`. Administrators find the details in `logs/api.log` (or `docker compose logs api`) by searching for `API_DL_FAIL ref=<reference>`. Clients never see internal paths, cookies, or tracebacks.

A worker thread cannot be stopped from outside. If a download outlives its deadline by five minutes, the job fails with `timeout` and a reference such as `YT-STUCK-52D16C`, administrators get a message, and the thread keeps its worker slot until it returns. When every worker is stuck, the process tells administrators and exits so Compose restarts it. As after any restart, jobs and stored files are lost.

## Limits

| Setting | Default | Purpose |
|---|---|---|
| `API_RATE_LIMIT_PER_MINUTE` | `120` | Requests per key per minute, polling included. Responses carry `X-RateLimit-Limit` and `X-RateLimit-Remaining` |
| `API_MAX_ACTIVE_JOBS` | `2` | Queued and running jobs per key |
| `API_DOWNLOAD_WORKERS` | `4` | Concurrent downloads across all keys, separate from the bot's pool |
| `API_RESULT_TTL_MINUTES` | `60` | How long a finished file stays available (5 to 1,440) |
| `API_MAX_FILE_SIZE_MB` | `2000` | Largest file to download (1 to 2,000) |
| `API_STORAGE_LIMIT_MB` | `10000` | Disk space for finished files of all keys together |
| `API_PORT` | `8090` | Listening port |

The file size limit does not depend on the bot's Telegram mode: the API serves files itself, so a direct run allows the same 2,000 MB as Compose. Each key keeps at most 20 finished files, and all keys together stay within `API_STORAGE_LIMIT_MB`; above either limit the oldest files expire early, but a file that has just finished is never removed this way. Running downloads are not counted. A download stops as soon as its source tracks together pass `API_MAX_FILE_SIZE_MB`, and merging them briefly takes about as much again, so reserve 2 × `API_DOWNLOAD_WORKERS` × `API_MAX_FILE_SIZE_MB` on top of the storage limit. The YouTube command-line fallback runs in a separate process, and its `--max-filesize` covers only direct downloads: for HLS and DASH only the job deadline bounds it, and the limit applies to its finished file. Records of finished jobs are forgotten after 24 hours, and each key keeps at most the 100 most recent ones, so `GET /api/v1/downloads` stays short. Twenty invalid keys from one address within 10 minutes block further invalid attempts from it, but a valid key from that address is never blocked.

Jobs live in the API process memory. A restart loses queued and running jobs and removes stored files; clients then get `404` and should start a new download.

## Deployment

Compose starts the API only with the `api` profile, because it publishes a new port:

```bash
docker compose --env-file .secrets/.env --profile api up -d
```

Set `COMPOSE_PROFILES=api` in the environment to make the profile permanent. The service shares the `bot-data` volume with the bot and WebUI, where keys are stored, reads uploaded cookies from `.secrets` without write access, and keeps files in its own `api-media` volume.

For a direct run, start `python -m api` next to the bot and WebUI. Like the bot, it reads `.secrets/.env`. The `api` package sets up its environment on import, so `uvicorn api.app:app` gets the same settings and log as `python -m api`.

**Use HTTPS.** The API listens on plain HTTP. A key sent over the open internet without TLS can be intercepted, so publish the port only through a reverse proxy that terminates HTTPS. If the proxy forwards the client address, set `FORWARDED_ALLOW_IPS` to the proxy address so Uvicorn trusts `X-Forwarded-For`; otherwise every client shares the proxy's address for the invalid-key limit.

Administrators from `ADMIN_IDS` get a Telegram message about a failed API download under the same rule as for the bot: expected outcomes such as a duration or size limit, a blocked network, or a timeout are not reported. The message names the platform, category, and reference. The same platform and category are reported at most once per 15 minutes, so a platform outage does not flood the chat. If the message reaches no administrator, the same failure is retried after a minute. Messages are sent from a separate thread, so an unreachable Bot API does not hold download workers. The API sends through `TELEGRAM_BOT_API_BASE_URL`, which Compose points at the local Bot API; without `TELEGRAM_TOKEN` it only logs. Use [the YouTube runbook](../runbooks/youtube-download-runbook.md) when YouTube downloads fail for both the bot and the API.

The API process writes its own `logs/api.log`, separate from the bot's `bot.log`, with the same rotation.
