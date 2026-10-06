# Media cache and delivery

## Retention and reuse

Nuvio keeps Telegram file identifiers rather than copies of uploaded media. Supported artifacts include videos, audio, documents, individual photos, and complete ordered photo or mixed posts with related audio when provided by the source. The actual media kind returned by Telegram determines the method used for later delivery. Albums are split into compatible groups of at most ten items without changing their order.

New and existing installations without an explicitly saved policy retain references indefinitely. Open **Settings > Media cache** in the WebUI to enable automatic cleanup and choose 1-36,500 days without successful reuse. Turning cleanup off preserves the chosen period but makes it inactive. Settings apply without a bot restart or environment changes. The daily worker and `/cleanup_cache` use the same policy. Temporary downloaded files, cookie workfiles, and analytics housekeeping have separate jobs and still run when artifact cleanup is disabled.

The WebUI shows file, usable-publication, and source-alias counts, SQLite size including its WAL file, and cleanup status. These counters exclude quarantined legacy rows. There is no automatic size quota; an annual 3 GB estimate is not a verified capacity guarantee.

Reuse requires the same bot identity, canonical source, and output recipe. The recipe includes its version, output selection, and applicable fast-path setting. Changing a fast-path setting prevents hits from the previous recipe without deleting its identifiers or aliases. Explicitly rejected file references invalidate the affected publication for reuse while preserving its stored records by default. Unknown hosts retain query parameters, including potentially signed parameters.

Source metadata is checked before presenting actions. An indefinite file-ID lifetime does not grant permission to bypass a current source refusal, duration limit, moderation decision, or output selection. A complete cached post skips media preparation after this check. No early reuse on an unavailable source is enabled.

Only a fully confirmed ordered composition becomes a normal album hit. Confirmed pieces of a partial send remain stored individually but do not masquerade as a complete post. Missing source/download items and unavailable HLS fragments reject normal success. yt-dlp API and CLI use strict fragment handling; container duration alone is not a completeness check.

## Delivery evidence and recovery

The authoritative dispatch journal is `delivery_journal.db` in `DATA_DIR`. SQLite WAL with `synchronous=FULL` commits an intent before each tracked media request. It records confirmed message IDs, explicit refusals, proven not-sent requests, and uncertain outcomes. Startup converts unfinished dispatches to unknown; it never automatically replays them. Known completed operations can be pruned after 180 days; operations containing unknown outcomes are retained.

A pool-acquisition timeout is evidence that the request was not sent. It permits a bounded retry with current moderation and cancellation checks. A lost response or cancellation while waiting for a dispatched request does not prove non-delivery and does not authorize a resend or alternate transport. Confirmed media progress is saved immediately upon the response, before journal/cache side effects. A cache outage degrades to a miss or a failed optional write; it cannot erase a confirmed delivery. Access and journal failures remain authoritative.

There is a cross-system uncertainty window between Telegram accepting a request and persisting its response locally. This journal does not provide exactly-once delivery. Cancellation cannot retract accepted Telegram messages. In-memory sessions do not resume automatically after restart.

Database transactions run outside the event loop. Optional cache work has its own bounded executor so lock waits do not occupy the workers used for moderation and dispatch evidence. Download retries, cookie modes, CLI fallback, and FFmpeg waits share the preparation deadline; worker-owned temporary files remain protected while work is active.

## Migration, backup, and rollback

The normalized artifact tables are added alongside the legacy `video_cache` reader. Existing rows lack verified bot identity and actual media kind and are not promoted blindly into the new bot reuse path. Legacy tables and the pre-migration snapshot remain available for inspection. Legacy diagnostic APIs read the compatible active table, including only rows retained by historical delivery-contract migrations; they do not imply eligibility for new bot reuse. New references are learned from confirmed deliveries. Legacy administrative title search reports legacy records; use WebUI counters for current artifact coverage.

When the legacy unique-file constraint needs replacement, startup first creates a consistent SQLite backup named `telegram_cache.db.before-artifacts-<revision>.bak`. The migration preserves the old table under a legacy name and retains URL/output aliases independently. Backup files can contain submitted URLs and must remain private. They are excluded from Git and Docker build contexts.

Before deploying, take a consistent backup of the entire data directory, including analytics and journal data. Use SQLite's backup API for live databases, or stop both bot and WebUI before copying database files and their sidecars. Do not copy only a live WAL database's main file.

For rollback, stop both processes, preserve the current data and journal as a separate snapshot, and restore the selected pre-upgrade backup to a separate location for inspection first. Verify its integrity and legacy reader before replacing the cache database while both processes remain stopped. Restoring this snapshot loses cache entries learned after it was taken; keep the newer snapshot for recovery. Do not drop analytics/moderation or journal state as part of a cache rollback, and do not reinterpret uncertain dispatches as safe to replay. An older application does not understand the new delivery guarantees, so reverting the image alone is not an equivalent safe recovery strategy.

The image builds pinned SQLite 3.53.4 from the official source archive and verifies its checksum and Python's loaded version. This addresses the upstream WAL-reset race absent from the inspected Debian 3.46.1 package. Direct installations must verify their own SQLite version and vendor fixes; a Python version alone is insufficient.

## Verification boundaries

See the [implementation acceptance record](../audits/cache-and-delivery-acceptance-2026-10-06.md) for tested cases and remaining external checks. Shared preparation across recipients is not enabled: it requires measured duplicate work plus explicit temporary-file ownership and cancellation tests. Recipient delivery is never shared.
