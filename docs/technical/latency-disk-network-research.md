# Research: delivery latency, disk writes, and network paths

> Research date: 2026-07-25. This is a dated measurement record, not a claim about current CDN behavior or deployment state. The synthetic FFmpeg measurements used FFmpeg 8.0.1 on 16 cores with a 1080 x 1920 `testsrc2` source. Relative differences are more useful than absolute times on other machines and content.

## 1. FFmpeg measurements

The source was a 1080 x 1920 HEVC video representative of TikTok's `hdplay` format.

| Processing path | Time | Output size |
|---|---:|---:|
| Existing `convert_to_format`: `ffmpeg -i in -y out.mp4` | 2.56 s | 10.6 MB |
| H.264: `-c:v libx264 -preset veryfast -crf 23 -c:a copy -movflags +faststart` | 1.72 s | 9.4 MB |
| Remux: `-c copy -movflags +faststart` | 0.042 s | 12.3 MB |
| MP3 audio: `-b:a 192k out.mp3` | 0.096 s | 0.36 MB |
| Copy audio: `-vn -c:a copy out.m4a` | 0.036 s | 0.24 MB |

The measured H.264 transcode took 2.46 s for a 15 s clip and 9.55 s for a 60 s clip, or about 0.16 times the clip duration. Remuxing was about 61 times faster than the original conversion, but it retains HEVC. The iOS compatibility issue in [ADR-001](adr-001-tiktok-audio-hevc-compatibility.md) rules out remuxing HEVC as the general delivery path.

## 2. Earlier TikTok path and its costs

The earlier request path expanded short links with a full `httpx.get`, called yt-dlp for metadata, then called `extract_info` again on download. Video and audio could be downloaded separately and merged; `ffprobe`, possible HEVC conversion, and the Telegram upload followed. For a 10 MB result, intermediate files could write about 35 MB, a 3.5 times write amplification.

For audio, TikTok usually exposed no audio-only yt-dlp format. The bot therefore downloaded a 12-40 MB muxed video to produce about 0.24 MB of sound. A direct audio URL or stream copy avoids most of that traffic. Converting to MP3 still takes about 0.096 s in this synthetic measurement; copying the M4A track takes 0.036 s.

## 3. Direct TikTok media measurements

Three real `play` URLs from the resolver were checked without authentication. Their media identifiers are withheld because they point to unrelated creators' posts. All returned HTTP 200, H.264 High video at 576 x 1024, AAC audio, and a fast-start MP4. The free resolver response had no `hdplay` URL.

| Sample | Duration | `play` size | Bitrate | `hdplay` |
|---|---:|---:|---:|---|
| A | 35 s | 2.05 MB | 470 kbps | `None` |
| B | 60 s | 5.58 MB | 744 kbps | `None` |
| C | 166 s | 19.53 MB | 941 kbps | `None` |

For the 60 s sample, the resolver took 0.83 s and downloading 5.58 MB took 1.13 s: 1.95 s total without running FFmpeg. A 19.53 MB file took 14.29 s on a cold CDN connection and 2.12 s on a repeat request, about a sevenfold difference. Range requests reached 4.4-5.3 MB/s. At 941 kbps, a roughly 170 s clip approaches a 20 MB URL delivery limit; the 166 s sample was already 97.6% of it.

The resolver's `music` link returned a 0.96 MB, 128 kbps MP3 in about 1.6 s. The tested signed URL remained valid for about six hours. The URL pointed to TikTok's CDN, not a copy hosted by the resolver, and our anonymous client did not need a User-Agent or Referer. An `original sound` title matched the audio in the tested video; a library track may differ when the author speaks over it. For that case, copy audio from `play` instead. The resolver introduces its own availability and rate-limit dependency, so yt-dlp remains the fallback.

The free resolver only provided 576 x 1024 video in these samples. Higher quality requires the yt-dlp path when available. The historical proposal was to present a fast option and a separate maximum-quality option rather than imply that the fast path preserves 1080p.

## 4. Changes recorded in this research

The dated implementation notes report the following changes under `TIKTOK_FAST_PATH`, enabled by default:

- Direct `play` download with an `ffprobe` codec check, and direct `music` download when it matches the video sound. Otherwise, audio is copied from the video stream.
- Fallback to yt-dlp if the fast path fails; `FileSizeLimitError` remains a terminal size error.
- Codec-aware conversion: H.264/AAC can be remuxed, other audio can be transcoded while copying H.264 video, and incompatible video is encoded with `libx264 -preset veryfast -crf 23`. MP4 files use `+faststart`. A measured compatible sample improved from 1.015 s to 0.103 s. HEVC is still encoded as H.264 for iOS compatibility.
- `_resolve_tiktok_url` attempts `HEAD` before `GET`. On three tested links this avoided downloading 287-354 KB of unnecessary response data per expansion.
- The FFmpeg availability check is cached. `_audio_format_sort_key` prefers audio-only when bitrate and size are equal.

In a live test of a video containing a library track, extracted audio lasted 35.385 s, matching the video rather than the 168 s library track.

The original follow-up list included checking the cache before metadata, direct URL handoff, a maximum-quality button, shared temporary memory storage, and removal of repeated `extract_info` calls in the fallback audio path. These were proposals in this snapshot; later code or audits must be checked before treating them as open work.

## 5. Request latency in the deployed bot

A logged TikTok request on 2026-07-25 took about 8.5 s from link to video. The phases were:

| Phase | Time |
|---|---:|
| Initial "processing" response | 0.16 s |
| Expand short URL | 1.24 s |
| yt-dlp metadata extraction | 1.08 s |
| Render menu and wait for selection | 1.31 s |
| CDN download | 2.65 s |
| Local Bot API delivery | 1.89 s |

The visible processing phase was about 2.48 s; the user also waited through the subsequent phases. On a full URL, the resolver produced media metadata in 0.70 s, compared with about 2.0 s for yt-dlp with cookies. Without cookies, yt-dlp failed after about 4.9 s in the tested environment. Checking FFmpeg cost 0.06 s on the first call and effectively zero after caching, so it was not a meaningful source of the delay.

For two Instagram reels, using the direct GraphQL `video_url` reduced download time from 7.54 s to 1.85-1.98 s. Metadata extraction remained 2.98 s, and a repeat served from cached `file_id` took 0.14 s. The tested direct videos were H.264/AAC at 720 x 1024.

## 6. URL handoff to Telegram

Direct URL delivery was tested with the first administrator ID. Telegram infrastructure fetched public URLs; the local `telegram-bot-api` service did not fetch the test URL, and internal Compose addresses were unreachable from Telegram. Local Bot API mode raises the upload limit for local files to 2 GB, but does not raise Telegram's URL fetch limit.

| Public MP4 size | Observed URL delivery |
|---:|---|
| 9.7 MB | Accepted |
| 19.5 MB | Accepted |
| 30.6 MB | Rejected with `failed to get HTTP URL content` |
| 37.2 MB | Rejected with the same error |

The observed boundary was between 19.5 and 30.6 MB, consistent with the documented 20 MB URL limit. The documented 5 MB photo limit was not stress-tested; the test images were about 0.13 MB.

| Source | Method | Observed result |
|---|---|---|
| TikTok video, `tiktokcdn-us.com` | `sendVideo` | 1.7-2.1 s; preview present |
| TikTok audio | `sendAudio` | 0.6-1.1 s in three tests |
| Instagram reel | `sendVideo` | 1.4 s for 5.94 MB |
| Instagram photo | `sendPhoto` | 0.4 s |
| YouTube progressive MP4 | `sendVideo` | 1.0-1.3 s for 2.8 and 8.1 MB |
| YouTube audio-only itag 140/251 | `sendAudio` | 15 s timeout |
| YouTube audio-only itag 139 | `sendAudio` | Accepted after 12.45 s |
| VK Video, `okcdn.ru` | `sendVideo` | HTTP 400 to the tested non-original client |
| Rutube | URL handoff | Only `m3u8_native` was available |

Telegram returned a `file_id` for accepted media, so the normal cache could still be populated. Progressive YouTube video worked in these tests; audio-only URL handoff was too slow or failed.

The TikTok resolver allowed one request per second in the test. Consecutive resolution and download decisions therefore had to reuse the same response; the recorded memo TTL was `TIKTOK_RESOLVER_MEMO_TTL_SECONDS=30`.

## 7. Photo albums and CDN refusals

For a 12-frame TikTok carousel with audio, the measured paths were:

| Path | Size probing | Delivery | Total | Local writes |
|---|---:|---:|---:|---:|
| Download through disk | - | 6.97 s download + 9.37 s send | 16.34 s | 2.24 MB |
| URL handoff, sequential probes | 9.95 s | 7.56 s | 17.51 s | 0 |
| URL handoff, parallel probes | 1.10 s | 8.26 s | 9.36 s | 0 |

Sequential one-byte Range requests spent most of their time setting up connections. Parallel probes removed 8.85 s. The handoff decision is made for the entire post so that frame ordering and delivery quality remain consistent.

Later that evening, Telegram rejected a TikTok video CDN URL in six of six attempts within 0.20-0.34 s, although our client still received HTTP 200 with and without headers. TikTok photos and Instagram reels continued to work. This supports treating URL handoff suitability as a changing property of the CDN domain and media type. The recorded refusal cooldown was `HANDOFF_REFUSAL_COOLDOWN_SECONDS=900`, keyed by domain and media type, with local download as fallback.
