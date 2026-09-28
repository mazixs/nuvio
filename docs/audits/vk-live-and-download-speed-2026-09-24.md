# VK live archives and download speed

Date: 2026-09-24. Source: a public VK live archive. Its identifier is withheld to avoid identifying an unrelated publisher.

## Findings

1. Before the fix, [VK URL validation](../../utils/rutube_vk_utils.py) rejected the `vk.ru` domain and `/live-...` path. The request received a generic invalid-link response. The [message](../../messages.py) listed only YouTube, TikTok, and Instagram even though the bot already handled regular VK and Rutube URLs.
2. Pinned yt-dlp `2026.08.18.122307` did not recognize the source path and returned `Unsupported URL`. An upstream yt-dlp issue for this VK live URL format was open at the time. The corresponding `vk.com/video-...` archive yielded title, formats, and a duration of 28,522 seconds in the local environment.
3. Even after URL normalization, the old three-hour limit would have rejected this roughly 7-hour-55-minute recording. Duration alone does not determine whether the file fits within Telegram limits.
4. The previous VK selector, `best[protocol=https]/best`, chose direct `url1080` without a known yt-dlp size. CDN HEAD requests measured 10,273,644,286 bytes at 1080p; 2,813,877,807 at 720p; 1,413,429,913 at 480p; and 880,846,165 at 360p. The local bot limit is 2,000 MiB, so 480p fits. A request for the first kilobyte of 480p returned HTTP 206 with range `0-1023/1413429913`.

These checks ran from the current computer without cookies. The full file was not downloaded or sent to Telegram at this stage. CDN access and speed from production may differ.

## Changes in the working copy

- VK live URLs are recognized and normalized to `vk.com/video-...` archive URLs for yt-dlp. An active live stream receives a separate, clear response.
- Before displaying the menu for a direct MP4, the bot reads `Content-Length` and selects the highest resolution within 95% of the file limit. A long archive can therefore be accepted when its file fits. If no safe direct option exists, the bot reports the size limit before downloading.
- The selected format ID is used after the button press and included in the `file_id` cache key. Large-file download timeouts are based on size, cancellation reaches the downloader, and a resumed direct download uses `.part` and HTTP Range instead of restarting a gigabyte file.
- Removed an artificial one-second delay before delivery. Updated the platform hint and stale claims about file size.

These changes had not yet been published or verified with a complete Telegram delivery of this recording.

## Where time is spent

| Stage | Observation | Action |
|---|---|---|
| VK extraction | Local metadata extraction and safe format selection took about three seconds. | Extract again before download to obtain a fresh signed media URL. Do not pass an old CDN URL directly from the menu. |
| Archive download | The 480p file is about 1.41 GB, so most time depends on the CDN and server connection. | Direct MP4 avoids HLS fragments and merging. Check size before download. |
| yt-dlp network options | Shared options set `http_chunk_size=10 MiB` and `continuedl=False`. That caused many Range requests and a complete retry after failure; the CDN supports Range. | Direct MP4 resume is enabled. Compare chunking on the same file segment and server before disabling it: chunking can help when a CDN throttles speed. |
| Video processing | [ensure_ios_compatible_video](../../utils/media_processor.py#L560) runs `ffprobe` and transcodes only incompatible codecs. A compatible direct MP4 is not transcoded. [Geometry](../../utils/telegram_utils.py#L3450) is probed again before delivery. | Time both probes on a real file and combine them only if delay is confirmed. |
| Telegram delivery | In local mode, the bot passes a file path to the Bot API, which then uploads the large file to Telegram. | Measure delivery separately from VK download. The artificial delay was removed. |
| MP3 audio | The current button promises MP3 and always uses FFmpeg conversion. | A direct source M4A could be offered as a separate action after checking quality and limits, while keeping MP3 as an explicit choice. |

A custom universal downloader is not justified by these measurements. yt-dlp handles signed URL extraction, headers, retries, and platform changes. The main improvement for this recording is selecting a direct file of suitable size. The [yt-dlp documentation](https://github.com/yt-dlp/yt-dlp#download-options) describes concurrent fragment downloads for DASH/HLS, not direct MP4.

## Dependencies and complexity

Direct dependencies are listed in [requirements.in](../../requirements.in). This VK path uses yt-dlp, an HTTP client for a short size probe, optional FFmpeg/ffprobe, and the Telegram client for delivery. FastAPI and Uvicorn serve the separate WebUI and are not part of file downloading. Locally, yt-dlp was pinned to an August 2026 version and FFmpeg was 8.0.1; package age alone does not explain speed. Updating yt-dlp alone does not solve `/live-...`, which remained an open upstream issue at the time. Replacing yt-dlp with a custom extractor would require maintaining changing VK URLs and signatures without measurements to justify that cost.

## Next checks

1. On a server with the local Bot API, verify URL -> 480p menu -> file -> Telegram delivery -> cache hit. Test cloud mode separately: the recording should be rejected before download under the actual 50 MiB limit.
2. Record time and traffic for extraction, HEAD, file download, codec probe, and Telegram delivery. Compare direct MP4 with HLS only for the same recording and quality. Compare direct downloads with `http_chunk_size=10 MiB` and without chunking separately.
3. Test a VK archive without `Content-Length`, an expired CDN URL, private access, and an active live stream. Recheck `/live-...` normalization whenever the extractor changes.
4. After measurement, decide whether to combine ffprobe calls or offer source M4A audio. Preserve iOS compatibility and Telegram limits even if a shortcut saves a little time.
