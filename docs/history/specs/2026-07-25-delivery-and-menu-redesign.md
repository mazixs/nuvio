# Disk-free delivery and format menu redesign

Date: 2026-07-25. Status: specification; implementation has not started.

This document records four requirements and the **measured limits** within which they can be met. All measurements came from a live system: a production host, the local Bot API, and real links. The host name is withheld; documentation values were not used without verification.

---

## 1. Quality fallback within the size limit

### Requirement

A multi-hour 1080p video can exceed the delivery limit. In that case, try the next lower resolution that fits instead of rejecting the video.

### Status: **already works**

`select_tg_video_format` considers formats from high to low resolution and selects the first video/audio pair that fits. The tests `test_long_video_cascades_down_to_the_next_fitting_resolution` and `test_cascade_continues_until_something_fits` both passed without code changes.

Measured bitrates from a 32-minute video, projected to four hours:

| Resolution | 32 minutes | 4 hours | Fits within 2,000 MB? |
|---|---:|---:|---|
| 1080p H.264 | 301.2 MB | 2.26 GB | No |
| 720p H.264 | 183.9 MB | 1.38 GB | Yes, selected |
| 480p H.264 | 67.1 MB | 504 MB | Yes |

The owner confirmed the 1080p ceiling. It is set by `TG_VIDEO_MAX_HEIGHT` in `utils/tg_video_choice.py`.

---

## 2. Send a link instead of a file

### Requirement

Pass a direct media URL to Telegram so it fetches the file, avoiding a local download followed by another upload.

### Measured limit: **20 MB, including with the local Bot API**

The following results came from `sendVideo` requests with HTTP URLs, known file sizes, and correct `content-length` headers through the local Bot API:

| File | Result | Time |
|---|---|---:|
| 9.7 MB, `video/mp4` | Accepted | 0.1 s |
| 19.5 MB, `video/mp4` | Accepted | 0.1 s |
| 30.6 MB, `video/mp4` | Rejected: `failed to get HTTP URL content` | 0.5 s |
| Instagram CDN, 4.6 MB | Accepted | 1.0 s |

The Bot API documentation gives a 5 MB URL limit for photos and 20 MB for other media. The local server raises the limit for **file uploads** to 2,000 MB; it does not raise the URL limit.

The local server also rejected an internal Compose media URL with `wrong HTTP URL specified`. This Bot API behavior does not replace Nuvio's own domain allowlist.

### Expected benefit

The 20 MB limit covers most measured TikTok and Instagram traffic:

| Source | Measured sizes | Below 20 MB? |
|---|---|---|
| TikTok fast path | 0.27, 2.5, 3.2, 3.3, 5.3, 18.6 MB | Almost always |
| Instagram reels | 4.6, 5.9 MB | Yes |
| YouTube 1080p | 331 MB | No |

For one measured TikTok request, 2.65 s downloading plus 1.89 s sending, or **4.54 s**, could be replaced with a **0.1-1.0 s** URL delivery without using local disk.

### Link expiration

The owner's reasoning was that a link which remains valid long enough for Nuvio to download should also remain valid while Telegram fetches it. Telegram fetched the measured link in 0.1 s, compared with Nuvio's 2.65 s download. This addressed the objection in the measured case.

### Accepted tradeoffs

- **The codec cannot be checked locally.** The media does not pass through Nuvio, so `ffprobe` has no file to inspect. ADR-001 requires H.264. The TikTok resolver returned it in three tested videos, and two Instagram reels used H.264 + AAC, but this relies on a third party's output.
- **The `file_id` cache still works.** `sendVideo` returns a `Message` with `video.file_id`, which can be cached as before.
- **The domain allowlist remains required.** Nuvio sends the URL directly to Telegram.

### Decision

Try URL delivery when the size is known and at most 20 MB. If Telegram rejects it, fall back to the existing disk-based path, as the fast paths already do.

---

## 3. Hierarchical format menu

### Current menu

The first screen offers `Скачать видео для ТГ` (download video for Telegram), `Скачать только звук` (download audio only), and `Дополнительно` (more options).

The more-options screen, built by `_build_youtube_more_menu`, is a flat list with arbitrary limits:

| Item | Limit | Problem |
|---|---|---|
| Combined `📹+🔊 {H}p` | At most 3 | YouTube usually offers just one, at 360p |
| Video only `📹 {H}p (без звука)` | At most 3 | Only 3 of 22 formats are visible |
| Audio only `🔊 Только аудио - {EXT}` | At most 2 | No quality in the label; WebM/Opus can appear |
| `MP3 (минимальный размер)` | None | Unnecessary transcoding |
| `Лучшее качество (видео + аудио)` | None | Duplicates the first screen |
| `Лучшее аудио` | None | Duplicates the original-audio option |
| `Скачать субтитры (SRT)` | None | Neither format nor language can be selected |

Formats are deduplicated **by button label**. Two formats at the same resolution can therefore hide each other; 1080p H.264 could disappear entirely.

### Target structure

Replace the flat list with three sections:

```text
More options
├── 🎬 Video     -> available 8K / 4K / 1440p / 1080p / 720p / 480p / 360p
├── 🎵 Audio     -> original track (M4A/AAC)
└── 📝 Subtitles -> available languages and formats
```

Video section rules:

- Show one button per resolution, not per format. Within a resolution, use the same selection rule as the Telegram button, preferring H.264 at equal height.
- Show estimated size, for example `1080p · 301 MB`.
- Hide resolutions that exceed the delivery limit.
- Remove the separate video-without-audio branch.

Audio section rules:

- Offer **only the original track**, usually YouTube M4A/AAC format 140.
- Do not offer WebM/Opus, which Telegram does not accept as audio here.
- Remove MP3 transcoding; the original track is already usable.
- Remove `Лучшее аудио`, which duplicates that track.

Subtitle section rules:

- The current `subtitlesformat: "srt"` is fixed and offers no language selection.
- Offer the languages and formats that YouTube makes available.

### Remove

Remove the buttons `Лучшее качество (видео + аудио)`, `Лучшее аудио`, and `MP3 (минимальный размер)`, the video-only branch, and the two arbitrary item limits.

Removing buttons changes `callback_data`. Update `callback_fsm.py` and event parsing tests; buttons already sent to users will stop working. See the cautionary section in `CLAUDE.md`.

---

## 4. Audio on the first screen

### Requirement

Place an original-audio button beside the video button, but show it only when a distinct audio track is available.

### Current behavior

`Скачать только звук` (`audio_m4a`) downloads M4A. The button is always shown without checking track availability.

### Decision

Show the button only if an `audio_only` track has a Telegram-compatible codec (`mp4a` or `aac`). Otherwise, hide it instead of rejecting the request after the click.

---

## Open questions

1. **Subtitles:** Which formats should be offered: SRT, VTT, TXT? If a video has dozens of languages, should the menu show all of them or only the original language and automatic translations?
2. **Sizes on video buttons:** Should the size always appear, or only when YouTube reports one?
3. **Implementation order:** Link delivery saved a measured 4.5 s for a TikTok request; the menu changes usability. The proposed first step is link delivery.
