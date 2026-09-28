# Code Quality and Data Cleanup Audit

Date: 2026-09-24. Area: Bot handlers, analytics, temporary files, cookies, cache, and logs. This is a supplement to the [yt-dlp and updates audit](ytdlp-and-update-audit-2026-09-23.md), not a confirmation of fixes for those points.

## Conclusion

Media and cache cleanup is implemented, but the lifespans of different data types are inconsistent. The main confirmed error in the reviewed code is zero values in weekly retention due to an incorrect week format. The primary risk for performance degradation and delays is the indefinite storage of analytics events combined with repeated recalculations of this event history on every dashboard request. There are candidates for code simplification, but identifying unused names does not prove a function can be removed - some functions are called via event handlers and in tests.

The review is based on the current source code. `ruff check --output-format concise .` completed without warnings. The size of the working database and production latency were not measured; performance assessment below refers to query structure, not proven response time on the server.

## Findings

### P1 - Incorrect weekly retention metric

In [cohort_retention](../../utils/analytics_db.py#L640), cohort construction uses `strftime('%Y-W%W', first_seen)`, while the internal query uses `strftime('%Y-W%%W', u.first_seen)`. SQLite returns for the date `2026-09-24` respectively `2026-W38` and `2026-W%W`. The values do not match, so `w1`–`w8` equal zero when a cohort exists. This was verified by running both expressions separately in SQLite; no tests in `tests/` check this function.

**Fix**: Use the same week format in both places, then validate the calculation against a small database with known registration and event dates. Also clarify the product meaning of retention: currently, the event week is treated as the number of full seven-day intervals since `first_seen`, not as the calendar week number.

### P1 - The `download` event indicates a link request, not file delivery

In [process_url](../../utils/telegram_utils.py#L1665), `track_event(..., "download")` is written before metadata is retrieved, format is selected, download begins, or the file is sent. This event is used to calculate `total_downloads`, `downloads_by_platform`, popular videos, repeat downloads, and conversion in [analytics_db.py](../../utils/analytics_db.py#L470). Failed links, cancellations, and Telegram errors are counted as downloads. Optimizing SQL without fixing this contract will accelerate inaccurate metrics.

**Fix**: Split the `request_started` and `delivery_succeeded` events. Define what constitutes a successful delivery from cache or photo post. Then shift the metrics to the correct event. Historical `download` events cannot be silently renamed to success - delivery results for older records are unknown.

### P1 - Synchronous analytics logging may delay bot responses

Both [_track_tg_user](../../utils/telegram_utils.py#L344) and [process_url](../../utils/telegram_utils.py#L1665) trigger synchronous SQLite operations directly within async handlers. The [connection](../../utils/analytics_db.py#L30) allows waiting for lock contention up to 30 seconds; writing begins with `BEGIN IMMEDIATE`. During concurrent writes from WebUI or database maintenance, the wait will block the bot's event loop. `_track_tg_user` catches the error, but direct `track_event` in `process_url` can interrupt processing of a user's link. This is a code-level risk; the frequency of lock waits on the production database has not been measured.
Fix: Move database operations out of the event loop with explicit time limits, define behavior during analytical service unavailability, and separately monitor write duration and errors. Product file delivery must not depend on the success of optional event recording.

### P1 - Analytics events and URLs are stored indefinitely

The [track_event](../../utils/analytics_db.py#L187) function writes URLs and metadata to `analytics.db`. The module includes index creation but lacks mechanisms for removing old events or managing the database size. Daily cleanup in [main.py](../../main.py#L137) applies only to `video_cache.db`. Long-term accumulation impacts disk space, user link history, and the cost of analytical queries.

**Fix**: Define a retention period for URLs and raw events. Identify metrics that require long-term history and preserve aggregates for them. Then, remove old records in bulk. Provide separate handling for individual user data deletion. Do not fully clear the table - this would alter existing dashboard values.

### P1 - Dashboard makes many synchronous queries in async handlers

The [dashboard_summary](../../utils/analytics_db.py#L739) function sequentially calls over 20 calculations. Inside [cohort_retention](../../utils/analytics_db.py#L640), there is a nested loop: for each weekly cohort over the last 56 days, it executes eight separate queries to `events` and `users`, resulting in typically 64-72 additional queries beyond the base summary. The conditions use `strftime` and `julianday` on columns. Both the [HTML route](../../web/app.py#L296) and the [API route](../../web/app.py#L483) directly invoke this synchronous logic from within `async def`, blocking the WebUI event loop. The users page also aggregates events before applying `LIMIT` in [get_all_users](../../utils/analytics_db.py#L536).

**Fix**: First, establish table sizes, measure execution time for each calculation, and run `EXPLAIN QUERY PLAN`. Then, consolidate cohort calculations into a single grouping query or a limited set of queries. Add appropriate indexes after measurement. Implement a short-lived cache for the summary. Run SQLite operations outside the event loop or use synchronous FastAPI routes. Criterion: response time and parallel web requests must not degrade as `events` grow.

### P2 - Daily engagement and dashboard snapshot need clarification

The description of [engagement_per_day](../../utils/analytics_db.py#L680) promises a daily MAU (monthly active users) for the past 30 days, but the implementation divides each daily DAU (daily active users) by the same MAU over the entire requested period. As a result, the "stickiness" curve does not match the described rolling DAU/MAU. Additionally, [dashboard_summary](../../utils/analytics_db.py#L739) collects metrics through individual reads without a shared transaction or fixed time slice; under concurrent writes, values on the same page may reflect different database states.

**Fix**: Align the definition of daily MAU with the calculation window and validate values against known data. For the dashboard, establish a single, consistent time of calculation and a synchronized snapshot, or cache the entire snapshot as a whole. Do not cache individual metrics with different update times.

### P2 - Index and query plan should cover more of the dashboard

Currently, the `events` table has separate indexes on `user_id`, `ts`, `event`, and `platform`, but the `users` table lacks indexes on `first_seen` and `last_seen` ([schema](../../utils/analytics_db.py#L100)). Queries such as `event = 'download' AND ts >= ?`, user history with `WHERE user_id = ? ORDER BY ts DESC`, user list sorted by `ORDER BY last_seen DESC`, and CSI filtering by `last_seen` do not benefit from composite indexes covering the full query pattern. Additionally, the video cache has a separate `idx_url` index that potentially duplicates the left prefix of the primary key `(url, format_id)` ([schema](../../utils/video_cache.py#L134)). These are candidates for query plan analysis, not immediate reasons to add or remove indexes: each new index increases write and cleanup costs.

Fix: Collect `EXPLAIN QUERY PLAN` output and measure execution time on a representative database for each common query path. Evaluate composite indexes based on actual selectivity, and remove indexes proven to be redundant via a separate migration. Rewrite the user pagination logic to first limit the set of users, then fetch events only for the selected user IDs. For queries involving `DATE()`, `strftime()`, and `julianday()`, assess performance based on ranges of original timestamps or pre-calculated aggregates.

### P2 - Transaction start error is masked by rollback exception

In [_cursor_write](../../utils/analytics_db.py#L74), `BEGIN IMMEDIATE` is inside a general `try` block, and the `except` unconditionally executes `ROLLBACK`. If transaction start fails due to a lock, there's nothing to rollback - SQLite may then raise a new error `cannot rollback - no transaction is active`, hiding the original cause. The same structure exists in [_write_transaction](../../utils/video_cache.py#L109). This is a concurrency scenario, not a measured failure rate.

Fix: Perform rollback only after successful transaction start, preserving the original exception, and distinguish between database lock, disk error, and integrity violation in the log.

### P2 - SQLite cache maintenance may halt bot processing

[scheduled_cache_cleanup](../../main.py#L137) and [scheduled_cache_vacuum](../../main.py#L148) are declared `async`, but they invoke synchronous `DELETE`, `checkpoint`, and `VACUUM` directly within the event loop. This is a daily and weekly operation; lock duration depends on cache size and concurrent access. There is no current measurement of the pause duration.

Fix: Run maintenance in a separate worker thread, limit lock waiting time, and record duration, number of rows deleted, and database size before and after. Test the bot's response to cancellation and polling during maintenance on a copy of the real-sized database.

### P2 - A new cookie file may not be included in the working copy

[working_cookie_file](../../utils/cookie_workfile.py#L39) copies the original only if its `mtime` is strictly greater than that of the working copy. yt-dlp may update the copy just before an administrator uploads a new original; if the copy's timestamp is equal or newer, the fresh original will be ignored. The [cookie loading handler](../../utils/cookie_manager.py#L403) does not invalidate the copy. This is a direct consequence of the code logic; a specific production case has not been documented.

Fix: After a successful upload, explicitly update or mark the working copy as outdated. When reading, synchronize this operation with yt-dlp to avoid overwriting the file during write. Preserve the ability to use platform-updated cookies across bot restarts.

### P2 - Timeout and cancellation do not guarantee background work completion before cleanup

[run_blocking](../../utils/telegram_utils.py#L361) correctly notes that `asyncio.wait_for` terminates the wait but not the thread. It marks the session as canceled, but [session cleanup](../../utils/telegram_utils.py#L1484) may remove its directory before the worker thread actually stops. The progress hook is not guaranteed to trigger during any blocking phase. Moreover, [application shutdown](../../main.py#L375) cleans the shared directory after the app stops, without explicitly waiting for all loading threads to finish. This may result in file reappearances, write errors in remote directories, and race conditions with a new startup. This is a risk scenario in the code, not a reported incident.

Fix: Mark a session as complete only after the worker actually finishes; for timeouts, mark it as canceled, wait a limited time, and delete files after the worker exits. For abrupt terminations, leave cleanup of old directories to the next startup, but consider process age and ownership if the old process is still running.

### P2 - One deletion error interrupts overall cleanup

In [cleanup_temp_files](../../utils/temp_file_manager.py#L51), a single `try` block wraps the entire loop over `TEMP_DIR`. If deletion of one item raises a `PermissionError` or another `OSError`, control immediately jumps to the `except` block, and the remaining items are not processed. The log reports the error but does not indicate how many files remain. This is confirmed by the function's structure; no specific case involving an inaccessible file has been found.

Fix: Catch each error individually, continue the iteration, and return a summary of how many files were successfully and unsuccessfully removed. Do not hide failed cleanup behind a success message in `main.py`.

### P3 - Remaining session path and complexity of handlers

All worker calls to [_cleanup_user_session](../../utils/telegram_utils.py#L1484) pass a `session_token`. The branch without a token uses the old `context.user_data["session_id"]` and is called only in [regression testing](../../tests/test_audit_regressions.py#L174). This is a candidate for removal after verifying preserved states and migration contracts, and is not safe for automatic deletion. `_handle_main_callback` spans 854 lines ([start](../../utils/telegram_utils.py#L1908)), and `process_url` spans 303 lines ([start](../../utils/telegram_utils.py#L1603)); repeated platform-specific branches complicate changes and error handling. Function size alone does not prove excessive CPU usage.

Fix: Document supported session states; then remove only the unsupported branch and its corresponding test. Split the large callback into smaller, action-based functions with a shared session termination procedure. Preserve behavior for buttons, diagnostics, cache, and temporary file cleanup.

## How current garbage is removed

| Data | Current Mechanism | Gap |
|---|---|---|
| Temporary media in `TEMP_DIR` | Session directory is deleted upon completion of most scenarios; entire directory is cleared at startup and shutdown in [main.py](../../main.py#L320) via [temp_file_manager](../../utils/temp_file_manager.py#L51) | No periodic cleanup of orphaned directories during continuous operation; race condition with incomplete worker |
| `video_cache.db` | Check validity period of 90 days on read, daily `DELETE` of expired entries, weekly checkpoint + `VACUUM`; `/cleanup_cache` removes only expired entries | Maintenance runs in event loop; the command does not clear the entire cache |
| `analytics.db` | Stored in Docker volume; no regular cleanup of raw `events` | No retention period for URLs, events, or explicit deletion policy |
| Working cookies in `DATA_DIR/cookie-work` | Saved between restarts specifically to update platform session | No explicit update on load of new original; automatic deletion is not provided |
| `logs/bot.log` | [Rotation](../../utils/logger.py#L17): 10 MiB with five backup files; Docker JSON logs are limited to 10 MiB and three files per service in [compose.yaml](../../compose.yaml#L34) | Retention period is based on size, not on date |
| Local Telegram Bot API data | Separate persistent volume `telegram-bot-api-data` in [compose.yaml](../../compose.yaml#L104) | Internal cleanup is not verified; cannot be considered part of `TEMP_DIR` cleanup |
| Internal cancellation and diagnostics registries | Cancellation marks are lazily deleted after 600 seconds; diagnostics tail is limited to 64 sessions and 60 lines per session | Limits exist; additional cleanup without indications is not needed |
`.gitignore` hides local databases, media, logs, and secrets from Git, but does not remove them from the disk.

## Work Order

1. Agree on metrics contract: request link, successful delivery, cache, cancellation, daily MAU, and weekly retention. Fix the weekly format; do not treat old attempts as confirmed downloads.
2. Establish baseline metrics: number of rows and table sizes including WAL, read/write duration, bot/WebUI response time, frequency of `database is locked`. On a database copy, run `EXPLAIN QUERY PLAN` for summary, users, and CSI queries.
3. Extract SQLite reads and writes from the event loop, limit waiting time, and ensure handling of unavailable analytics without breaking the load. Fix rollback on failed `BEGIN IMMEDIATE`. For the dashboard, define a unified snapshot of metrics.
4. Define analytics and URL retention policy, preserve necessary historical aggregates, introduce batch cleanup and base size monitoring. Before migration, create a consistent SQLite backup together with recovery policy.
5. Based on measurements, reduce number of SQL queries, restructure user pagination, add only confirmed composite indexes, and check for possible removal of duplicates. Compare query plans, delays, and write costs before and after on identical data.
6. Make worker termination a condition for deleting the temporary directory; add safe cleanup of old orphaned directories during long-running operations and continue cleanup after individual file errors.
7. Synchronize updating of working cookies with loading of a new original.
8. After finalizing session contracts, remove the old branch, simplify large handlers, and recheck errors, cancellations, and cleanup.

## SQL Stage Readiness Criteria

- On pre-defined cases, the dashboard correctly distinguishes between requests and successful deliveries, calculates daily MAU and weekly retention; for older events, it clearly indicates which values cannot be restored.
- For each modified frequent query, the original and new `EXPLAIN QUERY PLAN`, execution time on the same dataset, and the impact of new indexes on record insertion are preserved. The acceptable delay for the WebUI and bot should be set based on original measurements prior to implementation.
- Concurrent access by the bot and WebUI, cache servicing, and temporary SQLite locking do not result in loss of user requests or processing update stalls; the original transaction error remains visible in the logs.
- Batch cleanup preserves consistent historical metrics, does not delete recent records, and can be restored from a verified backup. The database size and WAL size after servicing are visible to the operator.
A measurement or functional verification is required before and after each step; this record itself does not fix anything in the running service.
