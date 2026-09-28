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
- Cookies expire. Check their current health and look for `cookie_status` in the administrative logs.

### YouTube returns `HTTP Error 403` during download

The yt-dlp version may be outdated. On 18 August 2026, YouTube changed direct `videoplayback` delivery for clients used by stable yt-dlp 2026.7.4: format listing still worked, but the first CDN request returned 403. This was reproduced from two outgoing IP addresses.

- Update yt-dlp by building or pulling a new image with the exact version pinned in `requirements.in` and `requirements.txt`. Follow the [YouTube runbook](../technical/youtube-download-runbook.md).
- A 403 from the media CDN is recorded as `MEDIA_FORBIDDEN` and may be retried; `ACCESS_RESTRICTED` indicates private, paid, or authorization-gated media.
- For a manual probe, run `docker compose exec bot python -m yt_dlp -f 140 <URL>`. Do not use `--test`: its 10 KB range request can succeed while the full download fails.
- The [runbook](../technical/youtube-download-runbook.md) records the incident, false-negative probe traps, commands, and pin-update procedure.

### TikTok video does not download

- Check for regional blocking and, if applicable, server-side VPN connectivity.
- The bot tries multiple API hosts with exponential backoff. If all fail, check the server network.

### Instagram rate limit

- Instagram limits request volume. Authorized cookies can raise the available limit.
- The bot inserts pauses between requests. Private accounts require cookies from an authorized session.

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

Users see a safe explanation and an ID in `PREFIX-CATEGORY-RANDOM` format. Internal diagnostics stay in the logs. See the [error code reference](../error-codes.md).

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

- `/cleanup_cache` removes entries older than 90 days.
- `VACUUM` runs automatically once a week.
- `/cache_stats` shows current cache statistics.

### A previously sent video downloads again

- The cache stores Telegram `file_id` values in SQLite and survives bot restarts.
- Check that `telegram_cache.db` has not been removed.
- A damaged database is recreated on the next start, but its cache entries are lost.

## Logs

- Path: `logs/bot.log`.
- Rotation: 10 MB per file, with five backups.
- Set `LOG_LEVEL=DEBUG` for detailed diagnostics.
