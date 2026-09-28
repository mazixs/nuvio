# YouTube audio language and iOS video processing

Date: 2026-09-24. The review used the then-current source code and a public test video. Its URL is withheld to avoid identifying an unrelated uploader. The user did not provide their own YouTube URL, so the available tracks for that video were not verified.

## Why the bot selected English audio

For the tested video, yt-dlp returned `140-0` with `language=en-US` and `140-1` with `language=ru`. Before the fix, the bot dropped the `language` field while preparing formats. The primary video button selected `160+140-0`, and the audio button took the first M4A track, `140-0`. The video downloader tried changing an audio format suffix to `-1`, but a suffix does not guarantee a language. That workaround also did not cover the audio button, direct URL delivery, or cache.

Selection now uses `language` and `language_preference` from yt-dlp metadata. Among formats within the size limit, Russian audio takes priority, followed by original audio and then another available track. The exact selected `format_id` is retained for a video and audio pair, and the audio menu shows the language. Russian Opus is converted to compatible AAC when needed; available Russian M4A is sent without transcoding. Re-extracting metadata for the public video after the fix selected `160+140-1` for the primary button, with `140-1` and `139-1` first in the audio menu. A previously sent Telegram file does not change tracks after a code fix, so startup removes only affected YouTube entries from the `file_id` cache.

If the extractor does not find a Russian track, the bot cannot create a Russian voiceover. yt-dlp has had cases where an HTTPS variant of a track was absent from its format list. That is a [separate extraction issue](https://github.com/yt-dlp/yt-dlp/issues/15756) that format selection cannot solve. In the local environment, yt-dlp also reported a missing JavaScript runtime and possible loss of formats. Deno was added to the Docker image, as [yt-dlp recommends for YouTube](https://github.com/yt-dlp/yt-dlp/wiki/EJS); installing `yt-dlp-ejs` without a runtime is insufficient. The `language^=ru` filter and `lang` sort syntax were checked against the [yt-dlp documentation](https://github.com/yt-dlp/yt-dlp/blob/master/README.md#filtering-formats).

## FFmpeg and iOS

[ADR-002](../technical/adr-002-ios-video-compatibility.md) records the iPhone checks. Incorrect dimensions recorded by Telegram as `320x320` and a black screen with some video codecs were confirmed. A separate frame-loss issue was not reproduced. The bot now sends explicit width, height, and duration and probes the finished file with ffprobe. Compatible H.264/AAC passes without transcoding; FFmpeg handles incompatible streams. A custom media engine would still need to select YouTube audio tracks and produce Telegram-compatible files.

Two related defects in the current code were found and fixed: the iOS check missed Opus inside MP4, and conversion of an already named `.mp4` file could pass the same path to FFmpeg as both input and output. The second issue prevented processing even when transcoding was required. The `-pix_fmt yuv420p` and `+faststart` settings and Telegram geometry metadata were preserved.

## Next steps

1. On the server, inspect the user's specific video: which tracks yt-dlp sees, which `format_id` is selected, and what reaches the iPhone after both first delivery and a cache hit. This needs the video URL and access to a running bot.
2. Measure extraction, download, ffprobe verification, FFmpeg processing, and Telegram delivery separately. Consider combining the two ffprobe checks in local delivery only if they cause noticeable delay.
3. Compare quality and processing time for Russian AAC and converted Russian Opus. Prefer the direct track when it works and use FFmpeg where it solves a concrete compatibility issue.

The changes described here were local. The server, container, and iPhone were not rechecked after them.
