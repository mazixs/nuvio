# ADR-002: Video compatibility with Telegram on iOS

## Status

Accepted and implemented.

## Context

Users reported three problems seen **only on iPhone**. The same messages looked correct on Android and Desktop:

1. Distorted aspect ratio: 16:9 video compressed horizontally and 9:16 video stretched horizontally.
2. A black screen while audio played.
3. Apparent stutter or low frame rate.

The investigation used production `bot.log`, files downloaded through the bot's real functions, and controlled sends through the local Bot API. The symptoms had distinct causes or evidence paths.

### Cause 1: video dimensions were omitted

All seven `reply_video` call sites in `utils/telegram_utils.py` omitted `width`, `height`, and `duration`. The Bot API does not compute these fields itself; it defaults them to zero. In the project-pinned Bot API revision (`Dockerfile.telegram-bot-api`, commit `adfd7f6`), `Client::process_send_video_query` uses:

```cpp
int32 width  = get_integer_arg(query.get(), "width",  0, 0, MAX_LENGTH);
int32 height = get_integer_arg(query.get(), "height", 0, 0, MAX_LENGTH);
```

Telegram's server then tries to infer dimensions. It succeeded for smaller files but stored **`320x320` for larger ones**, also losing the thumbnail (`thumbnail: null`). A controlled send of the same file showed:

| Send | Dimensions stored by Telegram |
|---|---|
| Without `width` and `height` | `320x320` |
| With `width=1920 height=1080` | `1920x1080` |

Measurements through the production local Bot API:

| File | Automatic dimensions |
|---|---|
| 124 KB, 6 s, synthetic | Correct |
| 4.9 MB, 18 s, TikTok 1080x1920 | Correct |
| 35.8 MB, about 2 min, YouTube 1920x1080 | **`320x320`** |

The iOS client derives geometry from document attributes (`ChatMessageInteractiveMediaNode.swift`, `TelegramMediaFile` branch). It uses the thumbnail only to decide whether to swap width and height, not to recover missing dimensions. Android and Desktop measure the stream themselves, explaining the platform difference.

**Methodological note:** The first test used the 124 KB synthetic video, for which automatic detection worked. That false negative initially caused the dimensions hypothesis to be rejected. Testing a real file exposed the defect.

### Cause 2: MP4 could contain VP9 or AV1

Telegram's iOS player uses VideoToolbox. It does not decode VP9, and AV1 support starts with A17 Pro devices. Audio in AAC can therefore play over a black screen.

The affected path was:

1. `utils/youtube_utils.py` set `merge_output_format: "mp4"`. In yt-dlp's `get_compatible_ext`, the explicit `preferences=['mp4']` set `allow_mkv=False`, so it selected MP4 **even for VP9 + M4A**, an unsuitable pair for that container.
2. FFmpeg merged the streams without warning.
3. `_convert_webm_if_needed` checked the **filename extension**. Because the result was `.mp4`, it did nothing.
4. The YouTube path lacked a finished-file codec check like `_ensure_ios_compatible_video` on TikTok and Instagram. `rutube_vk_utils.py` lacked one too.

The production log showed a real download of format `400+140`; format `400` had codec `av01.0.12M.08`.

`utils/tg_video_choice.py` deliberately prioritized resolution over codec suitability. For a video whose highest H.264 resolution was below the maximum, the top menu choices could therefore fail on iOS. One production example was a high-resolution game trailer at 3840x1600, with H.264 only up to 1920x800:

```text
1600p button                -> 401 av01  (unplayable on iOS)
1066p button                -> 400 av01  (unplayable on iOS)
 800p button                -> 299 avc1
Send to Telegram button     -> 400 av01  (default was unplayable on iOS)
```

### Cause 3: transcoding did not force 8-bit output

The transcoding branch of `_build_mp4_command` in `utils/media_processor.py` did not pass `-pix_fmt yuv420p`. libx264 inherited the source pixel format, producing H.264 **High 10** from a 10-bit input:

```text
Input:  vp9,  Profile 2, yuv420p10le
Output: h264, High 10,   yuv420p10le
```

**This was not a reproduced playback defect.** A `h264/High 10/1920x800/60fps` file sent to an iPhone through the production setup played smoothly. The initial assumption that iOS could not decode High 10 was disproved and is recorded here to prevent repeating it.

The decision still requires `-pix_fmt yuv420p` as a conservative compatibility measure. Eight-bit video is broadly supported; 10-bit support depends on device and OS version. This change should not be described as the fix for the observed defect.

### Stutter: no separate cause confirmed

The report that video looked as though it had a low frame rate did **not** yield an independent reproduced defect:

| Hypothesis | Test | Result |
|---|---|---|
| Software VP9 decoding drops frames | VP9 1920x800 60 fps on iPhone | Rejected: it showed a black screen, not stutter |
| H.264 High 10 overloads the processor | H.264 High 10 1920x800 60 fps | Rejected: it played smoothly |

The tested player showed either a picture or a black screen, with no intermediate state.

A specific TikTok video reported as stuttering was then examined. The file was sound: `h264/High level 3.1, 576x1024, yuv420p`, constant frame rate (2,678 frames at 0.03333 s intervals), and about 1 Mbps. Telegram had nevertheless stored **`320x320`** dimensions for this 10.67 MB file. Sending the same file with explicit `576x1024` dimensions made it look correct. The reported example thus traced to the dimensions defect.

This does not prove that every stutter complaint is solved. Actual stutter was not observed in the controlled test. After deploying the dimensions fix, any remaining complaint should be investigated from a new concrete example.

The 10.67 MB case also lowered the known size threshold for the dimensions defect:

| File | Dimensions stored by Telegram |
|---|---|
| 124 KB, 6 s | Correct |
| 4.9 MB, 18 s | Correct |
| **10.67 MB, 89 s** | **`320x320`** |
| 35.8 MB, about 2 min | `320x320` |

## Decision

Three rules apply to every delivery path:

1. **Always send dimensions.** Every video send must include `width`, `height`, and `duration`. Telegram's automatic detection is undocumented, size-dependent, and can silently produce a square.
2. **Send only 8-bit H.264 video to Telegram.** Inspect the **finished file** with `ffprobe`, not its extension or source metadata. Re-encode anything other than H.264/`yuv420p`.
3. **Force the pixel format when transcoding.** Every libx264 command must include `-pix_fmt yuv420p`. Codec selection alone does not ensure bit depth.

## Alternatives considered

- **Rely on Telegram's inferred dimensions:** Rejected because the undocumented behavior failed reproducibly on larger files.
- **Transcode every file to H.264:** Rejected because it adds seconds per video (ADR-001 measured 9.5 s for a 60-second clip), whereas most files already use H.264. `ffprobe` takes tens of milliseconds.
- **Remove AV1 and VP9 from the menu:** Rejected as the sole measure because it would deny Android and Desktop users available quality. Re-encoding retains those choices, with codec suitability considered during selection.

## Consequences

**Benefits:** Videos play consistently across clients, without depending on file size or the source's format list.

**Costs and follow-up:**

- **Reset the `file_id` cache.** It has a 90-day TTL and already contains documents stored with `320x320`. Resending a `file_id` retains the saved attributes, so those files would remain distorted without a reset.
- **URL delivery needs separate work.** `UrlHandoff` (`utils/url_delivery.py`) contains only `url`, `kind`, and `size`; the bot does not download that file and cannot measure its geometry. Dimensions must come from source metadata: Instagram `video_versions`, YouTube yt-dlp format data, and potentially the TikTok resolver response.
- **Some videos now require transcoding**, adding seconds and FFmpeg load.

## Implementation

| Change | Location |
|---|---|
| `get_video_geometry`: display dimensions accounting for rotation and non-square pixels | `utils/media_processor.py` |
| `needs_ios_reencode`: decision based on finished-file codec and bit depth | `utils/media_processor.py` |
| `ensure_ios_compatible_video`: shared protection across platforms | `utils/media_processor.py` |
| `-pix_fmt yuv420p` on the transcoding path | `utils/media_processor.py` |
| Send `width`, `height`, and `duration` for files | `utils/telegram_utils.py` |
| Pass dimensions for URL delivery | `utils/url_delivery.py`, `utils/instagram_fast_path.py` |
| Check codecs on YouTube, Rutube, and VK paths | `utils/youtube_utils.py`, `utils/rutube_vk_utils.py` |
| One-time cache video cleanup using `PRAGMA user_version` | `utils/video_cache.py` |
| Classify unavailable Instagram posts as ACCESS | `utils/public_errors.py` |

`tests/test_ios_video_compatibility.py` covers these requirements.

### Intentional unchanged behavior

- **`utils/tg_video_choice.py` was left unchanged.** Resolution outranking codec is a deliberate product choice covered by `test_resolution_outranks_codec_when_h264_forces_a_downgrade` and `test_h264_wins_within_the_same_resolution`. Post-download transcoding ensures compatibility. Choosing 4K AV1 may therefore cost extra transcoding time.
- **The TikTok resolver does not report dimensions.** TikTok URL delivery still omits them, as before. That path is limited to 20 MB; Telegram inferred dimensions correctly for the URL-delivery samples tested at the time, although the 10.67 MB file sent by the separate file path demonstrated that size alone is no guarantee. Instagram and YouTube report dimensions, which are passed through.
- **A `file_id` send does not accept dimensions.** Telegram reuses the saved document attributes. Clearing the cache is therefore required; adding dimensions to cache-hit send sites would not fix old files.
