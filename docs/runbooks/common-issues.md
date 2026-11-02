# Troubleshooting

## The bot does not start

### TELEGRAM_TOKEN is missing

- **Error:** The startup log says that the required `TELEGRAM_TOKEN` variable is missing.
- **Fix:** Copy `.env.example` to `.secrets/.env` and set the token there, or pass it as a process environment variable. Root `.env` and `.env.local` are read only for backward compatibility; do not use them for new installations.

### Import errors

Install all dependencies:

```bash
pip install -r requirements.txt
```

Check that Python 3.14 or newer is in use:

```bash
python --version
```

### FFmpeg is missing

FFmpeg is required for audio and video processing, including audio extraction, WebM-to-MP4 conversion, and stream merging.

- **Linux:** `sudo apt install ffmpeg`
- **macOS:** `brew install ffmpeg`
- **Windows:** Download it from [ffmpeg.org](https://ffmpeg.org), unpack it, and add the directory containing `ffmpeg.exe` to `PATH`.

## Platform problems

### YouTube asks for authentication

- YouTube may block downloading without cookies. Upload a cookie file to `.secrets/www.youtube.com_cookies.txt` or through `/admin`.
- Cookies expire, and yt-dlp can lose them from the working copy while the uploaded file stays intact. Run the check in `/admin` or look for `cookie_status` in the administrative logs, then follow [Cookie problems](#cookie-problems).

### YouTube returns `HTTP Error 403` during download

The yt-dlp version may be outdated. On 18 August 2026, YouTube changed direct `videoplayback` delivery for clients used by stable yt-dlp 2026.7.4: format listing still worked, but the first CDN request returned 403. This was reproduced from two outgoing IP addresses.

- Update yt-dlp by building or pulling a new image with the exact version pinned in `requirements.in` and `requirements.txt`. Follow the [YouTube runbook](youtube-download-runbook.md).
- A 403 from the media CDN is recorded as `MEDIA_FORBIDDEN` and may be retried; `ACCESS_RESTRICTED` indicates private, paid, or authorization-gated media.
- For a manual probe, run `docker compose exec bot python -m yt_dlp -f 140 <URL>`. Do not use `--test`: its 10 KB range request can succeed while the full download fails.
- The [runbook](youtube-download-runbook.md) records the incident, false-negative probe traps, commands, and pin-update procedure.

### TikTok video does not download

- Check for regional blocking and, if applicable, server-side VPN connectivity.
- The bot tries multiple API hosts with exponential backoff. If all fail, check the server network.

### Instagram rate limit

- Instagram limits request volume. Authorized cookies can raise the available limit.
- The bot inserts pauses between requests. Private accounts require cookies from an authorized session.

## Cookie problems

### A cookie alert or a failed check

The status appears in a daily message to administrators, after the check button in `/admin`, and as `cookie_status` in the administrative logs. It describes the working copy in `DATA_DIR/cookie-work`, which yt-dlp changes, not the file you uploaded. An intact original proves nothing: on 2026-11-01 the YouTube original had all its auth cookies while the copy had one of six and no `LOGIN_INFO`.

| Status | Meaning | Action |
|---|---|---|
| `expired` | Every auth cookie or the session cookie is past its date. | Upload a fresh export from a signed-in browser through `/admin`. |
| `degraded` | There is no session cookie, or fewer than half of the auth cookies are alive. | Upload a fresh export from a signed-in browser. On YouTube, see the warning below. |
| `stale` | The platform rejected the session, or `LOGIN_INFO` disappeared from the YouTube copy during the probe. | Upload a fresh export from a signed-in browser. |
| `invalid_format` | The file has no Netscape header, has a malformed row or expiry, is not UTF-8, cannot be read, or holds no auth cookies. The summary gives a line number, never the value. | Export again in Netscape format and upload. If the file cannot be read, check its permissions on the server. |
| `rate_limited` | The platform limited the probe. This says nothing about the session. The status alone raises no alert. | Nothing now. Check again later. |
| `probe_failed` | The probe did not finish or got an unfamiliar response. This says nothing about the session. The status alone raises no alert. | Check the server network. If it persists on TikTok, see [below](#tiktok-stays-on-probe_failed). |
| `missing` | There is no cookie file for this platform. No alert. | Cookies are optional for public media. Upload a file if this platform needs them. |
| `valid` | See [what it proves](#what-valid-proves). | Nothing. |

The session cookie is `LOGIN_INFO` on YouTube, `sessionid` on Instagram, and `sessionid` or `sid_tt` on TikTok.

**Fewer than 14 days until the session ends** also triggers the alert, whatever the status. On YouTube and Instagram, upload a fresh export before that date. Only TikTok renews itself.

**`degraded` on YouTube: upload a fresh browser export, not the old file.** The old file restores the working copy, but the server may already have revoked that session, and a YouTube `valid` would not show it. The bot deliberately does not restore the copy from the original on its own.

The alert keeps no state, so it repeats once a day until the problem is resolved.

### What `valid` proves

- **TikTok and Instagram:** the platform recognized the account.
- **YouTube:** only that a public test video was extracted and `LOGIN_INFO` is still in the copy. That video usually extracts without sign-in, so this does not prove the session. A server under YouTube's bot check gets `Sign in to confirm you're not a bot` without a live session: on 2026-11-02 this failed the test video and a user request until fresh cookies were uploaded.

### TikTok stays on `probe_failed`

Only TikTok renews its session automatically: the daily check calls an undocumented `passport` endpoint, and its response moves the expiry date in the working copy. If TikTok changes that endpoint, the check returns `probe_failed` and renewal stops. The first alert comes only when fewer than 14 days remain.

- Check `/admin` for `probe_failed` together with a `Сессия до DD.MM.YYYY` line whose date no longer moves.
- Look for `Cookie probe failed for tiktok` in `logs/bot.log`. The text after it separates a network or HTTP error from `unfamiliar response`, which points to a changed endpoint.
- If the server network is fine, the probe in `utils/cookie_health.py` needs fixing. Until then, upload fresh exports by hand before the session end date.

### A cookie upload is refused or warns

An upload is checked locally before it replaces the current file.

- **Refused:** the file did not pass the local check. The previous file stays in use and upload mode stays on, so send a corrected file straight away.
- **Saved with a `stale` warning:** the file passed the local check and was saved, but the live probe did not accept the session. Upload a fresh export from a signed-in browser.

## File problems

### File too large for Telegram

- In Docker, the local Bot API accepts files up to 2 GB.
- Check that `telegram-bot-api` is healthy and that the bot has `TELEGRAM_LOCAL_MODE=true`.
- A direct run without the local API is limited to 50 MB. Select a smaller format or use the Docker stack.

### Files are not delivered

- Check `logs/bot.log`.
- Find the error ID (`PREFIX-CATEGORY-RANDOM`) in the logs for full diagnostics.
- Check free disk space. Temporary files are stored under `temp/` or `downloads/`, depending on the path used.

## Error codes

Users see a safe explanation and an ID in `PREFIX-CATEGORY-RANDOM` format. Internal diagnostics stay in the logs. See the [error code reference](../reference/error-codes.md).

| Prefix | Area |
|---|---|
| `YT` | YouTube |
| `TT` | TikTok |
| `IG` | Instagram |
| `RU` | Rutube |
| `VK` | VK Video |
| `TG` | Telegram |
| `FILE` | Local filesystem |
| `BOT` | Internal bot workflow |

Search for the actual ID received from the bot:

```bash
# systemd
journalctl -u nuvio.service -n 500 --no-pager | grep "ERROR_CODE"

# log file
grep "ERROR_CODE" logs/bot.log
```

## WebUI problems

### The page does not open

- Check `WEB_PORT` (default: 8080).
- With Docker, verify the published port in `compose.yaml`.
- Check the firewall rules for that port.

### Password is rejected

- The default credentials are `admin` / `changeme`; change the password before deployment.
- Set `WEB_USERNAME` and `WEB_PASSWORD` in `.secrets/.env`.

## Cache problems

### Cache uses too much space

- `/cleanup_cache` follows the persisted WebUI policy. With cleanup disabled, it deletes nothing. Enable cleanup and select an idle period in Settings if needed.
- `VACUUM` runs automatically once a week.
- `/cache_stats` shows current cache statistics.

### A previously sent video downloads again

- The cache stores Telegram `file_id` values in SQLite and survives bot restarts.
- Check that `telegram_cache.db` has not been removed.
- A cache outage becomes a cache miss; initialization failure disables caching for that process. Preserve the database for diagnosis and restore a verified backup if needed. The bot does not delete a damaged database automatically.

## Logs

- Path: `logs/bot.log`.
- Rotation: 10 MB per file, with five backups.
- Set `LOG_LEVEL=DEBUG` for detailed diagnostics.
