# Nuvio callback state machine: architectural analysis

> Analysis date: 2026-05-29. This document records the observed design and proposed improvements at that time. Check the current handlers before using a proposal as an implementation task.

## 1. Session state model

The bot used a session-oriented, implicit state machine rather than one central state enum. A callback such as `s|{token}|{scope}|{action}` identified a session and an action. The inline keyboard showed the available transitions, while `context.user_data["sessions"]` held up to five active sessions per user. The oldest session was evicted when that limit was exceeded.

The main path was:

```text
User sends URL
  -> spam check
  -> metadata extraction in ThreadPoolExecutor
  -> main menu
  -> format menu or direct action
  -> cache lookup for selected format
       -> hit: send cached file_id
       -> miss: download, process, and send
  -> remove completed session
```

On an error, the session was cleaned up and the user could submit another URL. Cancellation marks the download's `session_id`; the yt-dlp progress hook observes that mark and stops the download. The post-analysis step also checks that the session still exists before showing a menu. A live 301 MB download was interrupted at 23% immediately after the cancel action in the original test.

The subtitle branch was a cascade: `main|subtitles` -> language (`format|subs_lang|<code>`) -> format (`format|subs|<code>:<format>`). Back navigation from format returned to languages. The format menu also had category and selection levels for video, audio, and subtitles.

A representative session looked like this:

```python
context.user_data["sessions"] = {
    "a1b2c3d4": {
        "url": "...",
        "video_info": {...},
        "session_id": "{user_id}_{uuid}",
        "platform": "youtube",
        "formats": [...],
        "created_at": 1716921600.0,
    },
}
```

| Stage | Operation | Handler |
|---|---|---|
| Create | `uuid.uuid4().hex[:8]` | `_store_session` in `process_url` |
| Parse | Split callback data | `button_callback` |
| Validate | `_get_session(...)` | Main or format callback handler |
| Remove | `_cleanup_user_session(...)` | Completion, failure, or eviction |

Eight hexadecimal characters give 2^32 possible tokens. The token is a routing key, not an authorization boundary: handlers must continue to check the user and session. Python's `uuid4()` uses a cryptographically secure source, so predictability was not the observed issue.

## 2. Download pipeline at the time of analysis

```text
process_url() [event loop]
  -> spam and analytics
  -> run_blocking(get_video_info) [worker, yt-dlp/network]
  -> build inline menu
  -> button_callback() [event loop]
  -> cache lookup for selected format
       -> hit: reply with file_id
       -> miss: run_blocking(download_video) [worker]
                -> yt-dlp, files, FFmpeg
                -> size check and send_file()
                -> store returned file_id in SQLite cache
```

The study identified synchronous SQLite calls in async handlers, a shared worker pool for metadata and downloads, and memory-only session state as possible bottlenecks. These were observations at the analysis date. Current implementations may have moved work or introduced additional modules.

## 3. Bottlenecks and proposals

### Callback complexity

State was spread across menus, callbacks, and session data. A new step could require changes to several handlers. The analysis proposed typed callback events and a handler registry. Later code had already moved some event parsing and session handling into `utils/callback_fsm.py`; that partial refactor should be considered before adding another dispatcher.

### SQLite calls in the event loop

The original handlers called `telegram_cache.get()`, `telegram_cache.set()`, and `track_event()` synchronously. SQLite WAL improves concurrency, but a busy write can still stall other callbacks. The proposal was to move relevant calls to `asyncio.to_thread()` or a dedicated write queue, then measure contention.

### Shared worker pool

The configured pool had `DOWNLOAD_WORKERS=8` by default. Metadata extraction, yt-dlp downloads, and FFmpeg jobs competed for those workers. Eight long jobs could make a ninth request wait. Sending an already prepared file through the local Bot API did not itself consume a download worker.

### Cache timing

At the analysis date, `process_url` fetched metadata before the selected-format cache lookup in the callback. A repeat URL therefore still incurred metadata latency. A pre-metadata cache lookup could help when the requested format is unambiguous; it must preserve format selection and avoid sending a cached file in the wrong format.

### Timeouts, progress, and cleanup

The proposed improvements included a bounded metadata timeout, more specific progress updates, cleanup of orphaned temporary files, and a visible conversion stage. A short timeout must be measured against real platform latency so it does not turn slow valid requests into failures. Delayed cleanup must preserve files until Telegram has finished reading them.

### Restart behavior

`context.user_data` was process memory, so restarting the bot expired active callback sessions. Durable session state in SQLite or Redis was proposed, with migration and expiry rules.

## 4. Fragment downloads and delivery

yt-dlp already supports fragments for HLS and DASH and Range requests where the source allows them. The recorded YouTube configuration used `concurrent_fragment_downloads=4` and `fragment_retries=5`. This does not mean the bot can send an incomplete file to Telegram: the local file must be ready before upload. Useful future work would be reliable resume or partial-content preview, subject to extractor and file-format support.

```text
Existing full-file path:
download -> convert -> upload

Possible fragment optimization:
parallel source fragments -> assemble a valid media file -> upload
```

The illustrative 5 + 2 + 1 minute versus 1.5 + 1 + 1 minute timings in the original proposal were hypothetical, not measured Nuvio performance.

## 5. Historical ICE prioritization

The 2026-05-29 study scored each proposal with `(Impact + Confidence + Ease) / 3`, each component on a 1-10 scale. Scores express the author's planning judgment, not verified gains or current priority.

| Proposal | Priority in study | Impact | Confidence | Ease | ICE |
|---|---|---:|---:|---:|---:|
| Cache check before metadata where format is known | P0 | 9 | 10 | 10 | 9.67 |
| 30 s metadata timeout | P0 | 8 | 9 | 10 | 9.00 |
| Move SQLite work off the event loop | P0 | 8 | 8 | 8 | 8.00 |
| Background cleanup | P1 | 6 | 9 | 9 | 8.00 |
| Show thumbnail before menu | P2 | 5 | 8 | 9 | 7.33 |
| Periodic orphan cleanup | P1 | 6 | 8 | 7 | 7.00 |
| Persist FSM sessions | P1 | 7 | 7 | 5 | 6.33 |
| Typed callback events | Partly implemented | 5 | 7 | 6 | 6.00 |
| Resume interrupted downloads | P2 | 5 | 6 | 5 | 5.33 |
| Periodic progress message | P2 | 6 | 5 | 4 | 5.00 |
| Separate conversion stage | P1 | 5 | 6 | 4 | 5.00 |
| Predictive download before format choice | P3 | 4 | 3 | 3 | 3.33 |
| Partial upload to Telegram | P3 | 3 | 2 | 2 | 2.33 |

The high-scoring ideas targeted repeated-request delay and event-loop responsiveness. Pre-downloading can waste bandwidth when the user selects another format; partial Telegram uploads were not supported by the bot API used in the study. Reassess all scores against the current code and live timings before scheduling changes.
