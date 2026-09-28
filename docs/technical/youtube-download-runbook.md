# Runbook: YouTube downloads stop working

> Date: 2026-08-19
> Incident: 2026-08-18, when YouTube changed direct media delivery
> Scope: YouTube, yt-dlp, error categories, and the pinned yt-dlp version
> Evidence: measurements in the production container and reproduction from two outgoing IP addresses

This runbook answers what to do when users report that YouTube videos no longer download. The August investigation took a day, mostly because early tests gave false negatives. **Start with section 2**, which explains those traps.

---

## 1. What failed on 2026-08-18

The pinned stable yt-dlp 2026.7.4 requested formats with clients for which YouTube had stopped serving `videoplayback` responses. Format extraction still worked, but downloading failed selectively:

| CDN request | Response |
|---|---|
| Without a `Range` header | **403**, no bytes |
| `Range` for 10 MB, the bot's normal `http_chunk_size` | **403** |
| `Range` for 64 KB | 206 |
| Download in 1 MB chunks | First megabyte received, then 403 |

The apparent threshold was roughly one minute of video: short videos downloaded, longer ones failed. The same behavior appeared from two outgoing IP addresses, ruling out the particular route or IP reputation in that incident.

This matched a broader YouTube/yt-dlp issue: [yt-dlp#17456](https://github.com/yt-dlp/yt-dlp/issues/17456). Nightly `2026.8.18.122307.dev0` switched to the `visionos` client; the same formats downloaded fully with the bot's options.

**Lesson for the next incident:** Partial success is expected. Extraction can work while media download fails; request chunk size matters; a short video may work while a long one fails. A probe must reproduce all three production conditions.

---

## 2. Three false-negative traps

1. **`--test` changes the request chunk to 10 KB.** That small request always worked during the incident, even though full downloads were broken. Manual probes therefore looked green and led to a diagnosis that had to be withdrawn. Make a full download with `--http-chunk-size 10M`; do not use `--test`.
2. **The `file_id` cache bypasses YouTube.** Reusing a URL the bot downloaded before tests SQLite and Telegram delivery, not the platform. The log line `Видео доставлено из кэша (key=...)` identifies such a hit. `/cleanup_cache` only removes entries older than 90 days; use an uncached URL.
3. **A video shorter than a minute could still download.** Shorts and other short samples gave a false green result. Use a video about ten minutes long.

---

## 3. Diagnostic procedure

### Step 1: identify the error category

```bash
docker compose exec bot grep USER_FLOW_FAIL /app/logs/bot.log | tail -n 20
```

A line resembles `USER_FLOW_FAIL code=YT-… platform=youtube stage=format_download … error=…`. The code contains an eight-character category abbreviation:

| Log code | Category | Interpretation |
|---|---|---|
| `YT-MEDIA_FO-…` | `MEDIA_FORBIDDEN` | 403 for the media file. Retried with backoff and eligible for CLI fallback; a final failure is logged as `ERROR` and reported to administrators |
| `YT-ACCESS_R-…` | `ACCESS_RESTRICTED` | Private, paid, age-restricted, or unavailable video |
| `YT-FORMAT_U-…` | `FORMAT_UNAVAILABLE` | Requested format unavailable; investigate extraction rather than media delivery |
| `YT-EXTRACTO-…` | `EXTRACTOR_RUNTIME` | Missing JS runtime, `nsig extraction failed`, or `remote components` issue |
| `YT-NETWORK_-…` | `NETWORK_TIMEOUT` | Timeout or connection reset |

A surge in `MEDIA_FORBIDDEN` points to section 4. Isolated cases can occur when a short-lived `googlevideo` URL expires; extracting again refreshes it. `is_media_forbidden_error()` in `utils/public_errors.py` separates this category. The final failure is reported to administrators, and the log shows the individual attempts.

### Step 2: check yt-dlp in the container

```bash
docker compose exec bot python -m yt_dlp --version
docker compose exec bot grep "Текущая версия yt-dlp" /app/logs/bot.log | tail -n 1
```

The bot logs its version on startup. If it is more than a week behind the latest nightly, investigate the update path in section 4 early; client changes are a common cause.

### Step 3: reproduce production options

```bash
docker compose exec bot python -m yt_dlp \
  -f 137+140 --merge-output-format mp4 \
  --http-chunk-size 10M --concurrent-fragments 4 \
  --socket-timeout 40 --retries 5 --fragment-retries 5 \
  --skip-unavailable-fragments --no-continue --no-playlist \
  -o '/app/media/probe.%(ext)s' '<VIDEO_URL>'
```

These are the bot's `DEFAULT_YTDLP_NETWORK_OPTS` from `utils/ytdlp_common.py`, plus `--http-chunk-size 10M`. The bot's CLI fallback omits that option, whereas its embedded API includes it and was the failing path. Use an uncached video around ten minutes long. Afterwards remove the probe with `docker compose exec bot rm -f /app/media/probe.mp4`.

Separate extraction from download:

```bash
docker compose exec bot python -m yt_dlp --no-playlist -F '<VIDEO_URL>'
```

If formats appear but the full download returns 403, media delivery is broken; proceed to section 4. If the list is empty or says `Requested format is not available`, first check cookies in step 4.

### Step 4: compare without and with cookies

The YouTube downloader intentionally tries **without cookies first**. An authenticated session can make yt-dlp select `tv_downgraded` or `web_safari`, which need a PO token, while the token-free `android_vr` client is removed when auth cookies are present. In one measurement the cookie attempt returned an empty format list and the anonymous attempt returned all formats.

```bash
# Anonymous attempt, matching the bot's first attempt
docker compose exec bot python -m yt_dlp --no-playlist -F '<VIDEO_URL>'

# Authenticated attempt using a copy, never the original
docker compose exec bot sh -c 'cp /app/.secrets/www.youtube.com_cookies.txt /tmp/probe-cookies.txt'
docker compose exec bot python -m yt_dlp --no-playlist -F --cookies /tmp/probe-cookies.txt '<VIDEO_URL>'
```

yt-dlp rewrites a supplied `cookiefile` from its cookie jar and can remove valid entries. Nuvio therefore uses a working copy in `/app/data/cookie-work` (`utils/cookie_workfile.py`); manual probes must also use a copy. A nonempty anonymous list and empty authenticated list can be expected. Cookies remain needed for age-restricted and private videos.

### Step 5: inspect yt-dlp's own output

| Source | Contents |
|---|---|
| `docker compose logs bot` | yt-dlp output during download, including `[download]` and `ERROR:` because `quiet` is disabled on this path |
| `/app/logs/bot.log` | Bot logger lines such as `USER_FLOW_FAIL`, CLI fallback, exceptions, and yt-dlp warnings through `utils.ytdlp_common` |
| Administrator crash report | Exception, traceback, stage, and cookie state; final `MEDIA_FORBIDDEN` failures are included |

`apply_network_opts` installs a logger adapter that writes yt-dlp warnings to `bot.log` under `utils.ytdlp_common` and to the session tail in `utils/download_report.py`. These warnings reveal the client used and platform response; the info dictionary does not. Progress lines are excluded from the 60-line diagnostic tail so they cannot displace warnings. The CLI fallback includes the first 1,000 stderr characters in the `RuntimeError` text (`utils/youtube_utils.py`).

For more detail, set `LOG_LEVEL=DEBUG` in `.secrets/.env` and restart the bot. The log will then include download progress and reasons for skipped formats.

### Step 6: rule out the outgoing IP

```bash
docker compose exec bot python -c "import httpx; print(httpx.get('https://api.ipify.org').text)"
```

An IP-specific cause is supported only if the same video and client fail from one exit but download fully from another. On 2026-08-18, the failure reproduced from two exits, so neither route nor IP reputation explained it. Equal behavior from both points back to the yt-dlp client and section 4.

---

## 4. Update yt-dlp

The example pinned version in this document is `2026.9.16.232951.dev0`. The bot no longer upgrades a running container with pip: files changed after import, so the reported version could differ from the executing code.

1. Choose an exact version published on PyPI and run the updater, substituting that version:
   ```bash
   .venv/bin/python scripts/update_ytdlp.py 2026.9.16.232951.dev0
   ```
   It changes the pin and both hashed lock files, runs lint and tests, and shows the diff. On error, it restores all three files.
2. Review the PR and CI, including the built-image smoke check. For a PR created with `GITHUB_TOKEN`, branch checks are triggered through `workflow_dispatch`; `pull_request` checks can await GitHub approval. Merge into `main`, create a `v*` tag, and wait for the canonical digest to be published in GHCR.
3. Record the current server digest for rollback. Pull the new image and recreate the service: `docker compose --env-file .secrets/.env pull bot`, then `docker compose --env-file .secrets/.env up -d bot`.
4. Verify yt-dlp and Deno versions, bot polling, and the canary. On regression, pull the previous digest, tag it locally as `rollback`, run `TAG=rollback docker compose --env-file .secrets/.env up -d bot`, and repeat the probe. Changing the repository pin alone does not update the server.

Use a nightly build when stable yt-dlp cannot handle YouTube. When a fixed stable release appears, test it through the same path and pin its exact version. An unpinned `master` channel is unsupported.

---

## 5. Code map

| File | Relevant logic |
|---|---|
| `utils/ytdlp_common.py` | `DEFAULT_YTDLP_NETWORK_OPTS`, including `http_chunk_size`; `classify_download_error_kind()`; `execute_with_backoff()` for `NETWORK_TIMEOUT` and `MEDIA_FORBIDDEN` |
| `utils/youtube_utils.py` | Without-cookies, with-cookies, then CLI attempts; non-HLS fallback after 403; `PO_TOKEN_ONLY_FORMAT_IDS`; CLI stderr tail |
| `utils/public_errors.py` | `youtube_error_code()` and `is_media_forbidden_error()`, separating `MEDIA_FORBIDDEN` from `ACCESS_RESTRICTED` |
| `utils/download_report.py` | yt-dlp output tail and actual downloaded format for crash reports and cache keys |
| `utils/canary.py` | Scheduled YouTube probe using production options without the cache, with failure notification |
| `utils/ytdlp_runtime.py` | Installed version, `run_yt_dlp_cli()`, `extract_cli_output_path()` |
| `utils/telegram_utils.py` | `_log_platform_failure()` for `USER_FLOW_FAIL`; `_should_notify_admins_platform_failure()` and `_notify_admins_crash()` for administrator reports |

For user-facing troubleshooting, see [common issues](../troubleshooting/common-issues.md). For code meanings, see [error codes](../error-codes.md).
