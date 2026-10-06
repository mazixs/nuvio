# Error codes

Users receive a short platform-specific message and an error code. For a confirmed access error, the bot may explain that the link requires authorization or that the media was removed. For an internal failure, it reports that the media could not be processed without attributing an unconfirmed cause to the platform or promising that waiting will resolve it. Cookie status, internal addresses, and tracebacks stay in administrative logs.

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

Error messages also offer a feedback button. The report contains the error code and source URL, while operational diagnostics remain in administrator logs. General reports can be sent with `/feedback`; see [Feedback and moderation](guides/feedback-and-moderation.md).

## Categories

The category segment is limited to eight characters. Logs and codes may therefore use the abbreviated form.

- `ACCESS` - confirmed platform access refusal or unavailable media.
- `ACCESS_R` - YouTube restricted access to the video.
- `API` - Telegram API or another internal API layer failed.
- `BLOCKED` - an administrator restricted access to the bot; no retry advice or crash alert.
- `COOKIE` - a cookie file could not be read or parsed.
- `DATA` - incomplete or corrupt data from an extractor.
- `DOWNLOAD` - the downloader refused without enough evidence for a more specific cause.
- `DURATION` - video exceeds the bot's duration limit. YouTube and Rutube metadata enforce the current 180-minute limit; VK recordings retain their separate file-size policy.
- `EXTRACTO` - extractor or runtime logic failed.
- `FFMPEG` - FFmpeg media processing failed.
- `FFMPEG_M` - FFmpeg is missing or the merge pipeline cannot use it.
- `FILE` - an expected local file is missing.
- `FILE_ACC` - local file permissions denied access; this does not establish a platform access restriction.
- `FORMAT_U` - the requested format is unavailable or obsolete.
- `IO` - an I/O error that cannot be classified more precisely from `errno`.
- `LARGE` - the file exceeds the limit for the selected delivery path.
- `MEDIA_FO` - the CDN rejected an issued media URL. This does not establish that the video is private: an individual stream may have expired or platform delivery rules may have changed. A cluster of these codes for one platform suggests the latter; investigate with the [YouTube download runbook](technical/youtube-download-runbook.md).
- `NETWORK` - connection or transport failure; recovery is not guaranteed.
- `NETWORK_` - timeout during a YouTube operation.
- `RATE_LI` - platform rate limit.
- `RENDER` - Telegram rejected message markup.
- `ROUTE` - the handler received a post type that needs another delivery path.
- `STORAGE` - insufficient disk space.
- `STORY_UN` - this handler does not support Instagram Stories.
- `TELEGRAM` - Telegram denied the bot access to the destination chat; this does not establish a source-platform restriction.
- `TIMEOUT` - operation timed out.
- `UNAVAILA` - YouTube reported that the video is unavailable to the selected extraction path. This alone does not establish deletion, privacy, or permanent unavailability.
- `UNEXPECT` - unexpected exception with no confirmed cause in its type or message. Do not present this category as an access restriction or network failure; record the exception type and stage in administrative logs.

New errors must not use the `UNKNOWN` category. Previously issued `UNKNOWN` codes remain only in old logs and messages. When a connection breaks after a Telegram request, the delivery outcome may be unknown; do not retry automatically even when the network error type is known.

## Public feedback and administrator diagnostics

| Confirmed condition | User feedback | Crash notification |
| --- | --- | --- |
| Duration limit (`DURATION`) | State the configured limit in minutes and ask for a shorter video. Do not recommend waiting. | No; retain a warning and the matching code in the log. |
| File size limit (`LARGE`) | State that the file exceeds the bot's Telegram delivery limit. | No; retain a warning. |
| Unsupported Stories (`STORY_UN`) | Explain the unsupported content type and offer a regular post or Reel. | No; retain a warning if it reaches the classifier. |
| Format unavailable (`FORMAT_U`) | Offer another available video/audio format or link, with the error code. | Yes; repeated failures can indicate an extractor regression. |
| YouTube reports video unavailable (`UNAVAILA`) | State the reported unavailability without guessing its cause. Offer another link or administrator help if the video opens for the user. Do not recommend waiting. | Yes; a generic response can also reflect an extraction/client regression. |
| Platform access refusal (`ACCESS`, `ACCESS_R`) | Explain that access failed; authentication, removal, and an expired link are possibilities, not confirmed facts. | Yes; retain the original exception and cookie diagnostics. |
| Rate limit (`RATE_LI`) | Explain the reported request limit and suggest a later attempt without a recovery deadline. | Yes. |
| Connection failure or deadline (`NETWORK`, `NETWORK_`, `TIMEOUT`) | Describe the connection failure or exceeded deadline. A repeat is optional and does not promise success. | No; retain a warning. Unconfirmed Telegram delivery still uses its separate no-resend message. |
| Media URL refused (`MEDIA_FO`) | Explain that the platform refused the media download. Do not call it a network problem or assume the video is private. | Yes; investigate expired streams or changed delivery rules. |
| Local permissions, storage, cookies, processing tools, extractor/runtime, rendering, routing | Explain that the service could not prepare the file and ask for the error code to be passed to an administrator. No installation instructions or raw exception text. | Yes; technical details stay in diagnostics. |
| Missing file (`FILE`) or Telegram API refusal (`API`, `TELEGRAM`) | Explain the file/delivery failure without claiming it is temporary. | Yes. |
| Incomplete data, unspecified download failure, unexpected exception (`DATA`, `DOWNLOAD`, `UNEXPECT`) | Explain that processing failed and refer to an administrator using the code. Do not guess why or suggest that waiting will fix it. | Yes. |

Exception types take precedence for local filesystem and HTTP transport/status failures. A bare Instagram HTTP 400 is an API refusal, not proof of private or removed media. Words such as `SSL`, `EOF`, or `network` alone do not establish a transport failure. A downloader's list of possible causes must not promote a speculative rate limit to a confirmed category; diagnostic wrappers use the original exception when available. The callback and metadata handlers use the same public classifier and matching diagnostic code.

YouTube recovery uses the same category classifier as public responses. A missing fragment alone is a download failure, not evidence of a network timeout. Explicit transport failures retain bounded download retries.

All download callbacks share a per-session concurrency guard. Actions and formats must belong to the session's platform and offered menu. Cancellation and menu navigation do not consume the download rate limit. A failed callback acknowledgement does not prevent the authorized action from running.

The absence of a subtitle track is an expected empty result. Subtitle download failures and missing downloaded files use the common error classification and diagnostics. Unconfirmed Telegram delivery is distinct from a subtitle download timeout and must not invite an automatic resend.

`Video unavailable` and `This video is unavailable` share the `UNAVAILABLE` category in the public classifier and downloader. This category does not disable the existing without-cookies, with-cookies, and eligible CLI recovery paths. A healthy canary verifies its reference video, not availability of every requested video. Cookie probe success likewise does not prove access to an individual video.

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
