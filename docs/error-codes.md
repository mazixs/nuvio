# Error codes

Users receive a short platform-specific message and an error code. For a confirmed access error, the bot may explain that the link requires authorization or that the media was removed. For an internal failure, it reports that the media could not be processed without attributing an unconfirmed cause to the platform. Cookie status, internal addresses, and tracebacks stay in administrative logs.

Code format:

```text
<PREFIX>-<CATEGORY>-<RANDOM>
```

Examples:

- `YT-ACCESS-A1B2C3`
- `IG-RATE_LI-Z9X8Y7`
- `TG-NETWORK-Q1W2E3`

## Prefixes

- `YT` - YouTube extraction, metadata, download, or merge pipeline.
- `TT` - TikTok extraction or download pipeline.
- `IG` - Instagram extraction or download pipeline.
- `RU` - Rutube extraction or download pipeline.
- `VK` - VK Video extraction or download pipeline.
- `TG` - Telegram API, delivery, or network path.
- `FILE` - local filesystem, temporary files, permissions, or a missing file.
- `BOT` - bot orchestration, callback handling, or general runtime flow.

## Categories

The category segment is limited to eight characters. Logs and codes may therefore use the abbreviated form.

- `ACCESS` - confirmed access error, usually involving cookies, restricted media, or file permissions.
- `ACCESS_R` - YouTube restricted access to the video.
- `API` - Telegram API or another internal API layer failed.
- `COOKIE` - a cookie file could not be read or parsed.
- `DATA` - incomplete or corrupt data from an extractor.
- `DOWNLOAD` - the downloader refused without enough evidence for a more specific cause.
- `EXTRACTO` - extractor or runtime logic failed.
- `FFMPEG` - FFmpeg media processing failed.
- `FFMPEG_M` - FFmpeg is missing or the merge pipeline cannot use it.
- `FILE` - an expected local file is missing.
- `FORMAT_U` - the requested format is unavailable or obsolete.
- `IO` - an I/O error that cannot be classified more precisely from `errno`.
- `LARGE` - the file exceeds the limit for the selected delivery path.
- `MEDIA_FO` - the CDN rejected an issued media URL. This does not establish that the video is private: an individual stream may have expired or platform delivery rules may have changed. A cluster of these codes for one platform suggests the latter; investigate with the [YouTube download runbook](technical/youtube-download-runbook.md).
- `NETWORK` - temporary network error.
- `NETWORK_` - timeout or network refusal during a YouTube download.
- `RATE_LI` - platform rate limit.
- `RENDER` - Telegram rejected message markup.
- `ROUTE` - the handler received a post type that needs another delivery path.
- `STORAGE` - insufficient disk space.
- `STORY_UN` - this handler does not support Instagram Stories.
- `TIMEOUT` - operation timed out.
- `UNEXPECT` - unexpected exception with no confirmed cause in its type or message. Do not present this category as an access restriction or network failure; record the exception type and stage in administrative logs.

New errors must not use the `UNKNOWN` category. Previously issued `UNKNOWN` codes remain only in old logs and messages. When a connection breaks after a Telegram request, the delivery outcome may be unknown; do not retry automatically even when the network error type is known.

## Diagnosis in the running environment

1. Ask the user for the short error code.
2. Search for it in `journalctl` or your log sink.
3. Inspect the log entry for:
   - platform
   - stage
   - session_id
   - URL
   - category and exception
   - cookie_status and cookie_summary
   - traceback.

Example systemd search:

```bash
journalctl -u nuvio.service -n 500 --no-pager | grep "YT-ACCESS-A1B2C3"
```

## Notes

- Cookie status is logged as `cookie_status`; it is not shown to users.
- Do not send users tracebacks, extractor error text, file paths, module names, or server structure.
- If errors of one category increase across many codes, check yt-dlp, cookies, and server network conditions first.
