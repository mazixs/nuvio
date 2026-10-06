# ADR-001: TikTok audio and HEVC compatibility on iOS

## Status

Accepted. A later amendment changes the default path; see below.

## Context

Nuvio had two related playback problems:

1. **Silent TikTok videos:** Some downloaded videos, particularly high-resolution HEVC/1080p files, were sent without an audio track.
2. **Incorrect aspect ratio on Telegram for iOS:** The same videos appeared compressed horizontally and stretched vertically in Telegram's built-in iPhone player. Android displayed them correctly.

### Causes

1. **Incorrect TikTok format metadata in yt-dlp:** TikTok HEVC (`bytevc1`) streams are video-only fragmented/DASH streams, but yt-dlp reported them with `acodec: aac`. With `-f bestvideo+bestaudio/best`, yt-dlp therefore treated the HEVC video as a complete muxed file and did not fetch separate audio.
2. **HEVC/MP4 playback behavior on Telegram for iOS:** The built-in iOS player mishandled aspect-ratio and rotation metadata for some H.265/HEVC video in MP4, distorting playback.

## Decision

Two changes addressed the issues:

1. **`create_tiktok_ytdl` factory with a dynamic `yt_dlp.YoutubeDL` subclass:**
   - A factory creates a subclass at runtime and intercepts `process_video_result` and `process_info`.
   - For TikTok, format metadata sets `acodec` to `'none'` for HEVC/bytevc1 streams and `/media-video` URLs.
   - The code finds an H.264 format known to contain audio and derives a virtual audio format, `virtual_audio_from_muxed`.
   - yt-dlp then downloads the highest-quality video and audio separately and merges them with FFmpeg.
   - Dynamic subclassing preserves compatibility with yt-dlp mocks in integration tests.
2. **Codec detection and HEVC-to-H.264 conversion:**
   - `utils/media_processor.py` adds `get_video_codec(file_path: Path) -> str | None`, using ffprobe to read the first video stream's codec.
   - After `download_tiktok_video` and `download_instagram_video`, a file encoded as `hevc` or `h265` is converted to H.264 with `convert_to_format`.
   - The result is a broadly compatible H.264/AVC MP4 with sound and correct iPhone playback.

## Alternatives considered

1. **Limit downloads to 540p H.264:** Excluding HEVC would prevent 1080p delivery.
2. **Transcode every downloaded video:** This would slow down H.264 videos that had no compatibility problem.

## Amendment, 2026-07-25: the fast path partly replaces the decision

This section was added without rewriting the earlier decision history.

The previously rejected quality tradeoff became the **default behavior**. `TIKTOK_FAST_PATH` is enabled by default and uses a third-party resolver's direct `play` URL, reliably delivering **576×1024 H.264 High + AAC** regardless of video duration. Measurements and rationale are in [latency, disk, and network research](latency-disk-network-research.md), sections 4 and 5.3.

The change favors speed:

- The HEVC path spent **9.5 seconds transcoding a 60-second clip**.
- The fast path took **about 2 seconds** without video transcoding. It still uses one FFmpeg `ffprobe` call to check the codec.
- The resolver's free tier did not provide 1080p: `hdplay` and `hd_size` were empty.

Requirements retained from the original decision:

- **HEVC must still be converted to H.264.** The yt-dlp path checks and converts HEVC. The fast path also checks the resulting file through `_ensure_ios_compatible_video`, because the contents of `play` are not under Nuvio's control and a resolver backend change must not reintroduce the defect.
- **yt-dlp remains the fallback** when the resolver is unavailable. Setting `TIKTOK_FAST_PATH=false` also restores the yt-dlp path and access to 1080p.

Tradeoffs to account for:

- The default delivers 576×1024 instead of 1080p.
- Historical behavior: changing the flag did not alter previously cached URLs, and the cache used a 90-day TTL. Since the artifact-recipe migration, the flag is part of the cache recipe; valid references have no expiration unless WebUI cleanup is enabled. See [cache operation](../guides/cache-and-delivery.md).

## Consequences of the original decision

- **Benefits:** TikTok 1080p files can be downloaded with audio through the yt-dlp path; TikTok and Instagram output is compatible with iOS; dynamic inheritance preserves existing test mocks.
- **Cost:** HEVC transcoding takes additional time according to duration, resolution, and server capacity. The local Bot API raises the delivery limit but does not reduce transcoding cost.
