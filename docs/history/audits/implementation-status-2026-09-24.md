# Implementation status after the audits

Date: 2026-09-24. This document records implementation of findings from the four audits in this directory. The original findings remain as audit history and do not describe the current code state.

## Completed

- Fixed recognition and normalization of `vk.ru/live-...` URLs, selection of a direct MP4 by verified size, and download continuation after a network interruption. The source URL described an archive of 28,522 seconds and a usable `url480` stream of 1,413,429,913 bytes.
- YouTube audio selection now uses the track language. Russian takes priority when the file size limit permits it. For a public test video (identifier withheld), the bot selected `160+140-1` with Russian audio. A schema update removes older YouTube cache entries that may contain English audio.
- Preserved iOS codec checks. Opus in MP4 is detected, compatible H.264/AAC files pass without transcoding, and conversion does not overwrite the source file. Two final-video probes were combined into one ffprobe call.
- Added safe escaping to the YouTube menu and a plain-text fallback when Telegram rejects Markdown. Download logs are separated by session.
- Pinned yt-dlp to `2026.9.16.232951.dev0` with EJS and Deno in the image. Removed updates from the running Python process. The update script prepares an exact pin and lock files; a weekly workflow proposes a PR, and another workflow fully downloads a control video. The canary records its result and alerts an administrator.
- Fixed weekly cohorts, rolling engagement, indexes, and paginated user queries. The dashboard reads a consistent snapshot; SQLite operations run outside async handlers. Historical requests are distinguished from confirmed deliveries.
- Session cleanup now waits for the actual end of the worker. Abandoned media, old cookie copies, and event URLs are removed according to their retention periods. A verified backup is created before analytics data changes.

## Verified

- `pytest tests/ -q`: 717 checks passed. `ruff check .`, `actionlint`, and `git diff --check` also passed.
- A local image build loaded yt-dlp `2026.09.16.232951`, EJS `0.8.0`, and Deno `2.9.7`. The bot created handlers with a test token.
- The control YouTube video was downloaded in full from a fresh image: 37.0 MB; its temporary file was deleted. A public video with selected Russian audio track `140-1` was also downloaded in full: 38,299,221 bytes. yt-dlp labels `140-1` as `ru`, "Russian original (default)". The original M4A has an `eng` metadata tag that persists in MP4; the tag does not establish the spoken language.
- The original VK URL was fully downloaded in local Bot API mode: 1,413,429,913 bytes, H.264 at 852x480 with AAC. Cloud mode correctly rejects selection of that file under its 50 MB limit. The temporary file was deleted after the check.
- Re-running `scripts/update_ytdlp.py` with the already pinned version changed no files. The then-current PyPI version matched the pin. After extending tests, combined line and branch coverage was about 70.4%, above the 70% CI threshold.
- On a synthetic database with 100,000 events, median recent-event query time fell from 5.621 to 0.234 ms. Writes with the new indexes took 217 ms instead of 180 ms. These are local synthetic measurements, not production-server results.

## Still requires production verification

- Send the original 1.4 GB VK archive through the local Telegram Bot API and deliver it again from cache. It was downloaded locally but not sent to Telegram.
- Reproduce the failure for the user's specific YouTube URL, which was absent from the conversation. A different public video with a Russian track was tested.
- Play a delivered video on an iPhone and verify frame rate, dimensions, and audio.
- Measure production database size, query latency, and bot responsiveness during cleanup. Local SQL timings do not replace these observations.
- Release a new image, update the production container, and observe the canary afterward. The production server was unreachable over the network during this check. GitHub workflows and PR permissions were checked syntactically but were not run on GitHub.
