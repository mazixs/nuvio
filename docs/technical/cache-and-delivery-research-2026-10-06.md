# Cache, delivery, and error-handling research

Date: 2026-10-06. Status: research and proposed contracts, not implemented behavior.

The `file_id` cache is a sound optimization. The main architectural problem is treating a cached artifact, a download attempt, a Telegram delivery, and a user's interaction as if they had the same identity and lifetime. They need separate contracts. SQLite can remain the storage backend; this investigation does not establish a need for Redis or PostgreSQL.

Related documents: [code audit](../audits/cache-and-delivery-audit-2026-10-06.md), [change plan](../plans/cache-and-delivery-changes-2026-10-06.md).

## Method and evidence boundaries

Discovery used eight Exa searches with 45 requested result slots across five topics: Telegram file reuse, asynchronous callbacks, comparable download bots, SQLite concurrency, and downloader retries. Follow-up searches covered asyncio, HTTP idempotence, and in-flight deduplication. This count includes candidates and repeated/irrelevant results; it is not a count of fully verified sources. Context7 located library documentation. The conclusions below use official documentation, version-specific library source, three inspected repository snapshots, and isolated local experiments.

Mirrors, generated documentation sites, older library pages, and snippets without a verifiable implementation were excluded from normative conclusions. Comparable projects were inspected, not executed or certified for production use. No production bot, user media, cookie probe, or Telegram chat was exercised.

## Telegram's actual contract

`file_id` is reusable by the bot that received it. Telegram advises treating these IDs as persistent. `file_unique_id` identifies a file but cannot download or resend it. File IDs are bot-specific; one file can have several valid IDs. Reuse cannot change the media type. These facts support storing bot identity and the actual returned media kind, with multiple aliases for an artifact. Telegram does not impose Nuvio's 90-day eviction rule. [Sending files](https://core.telegram.org/bots/api#sending-files), [file object](https://core.telegram.org/bots/api#file), [persistence FAQ](https://core.telegram.org/bots/faq#can-i-count-on-file-ids-to-be-persistent).

Delivery limits depend on the route: ordinary multipart uploads, Telegram fetching an HTTP URL, and a local Bot API upload are different mechanisms. URL fetching is limited to 5 MB for photos and 20 MB for other content; local uploads allow up to 2000 MB. A local server is not evidence of a larger URL-fetch allowance. [Sending files](https://core.telegram.org/bots/api#sending-files), [local server](https://core.telegram.org/bots/api#using-a-local-bot-api-server).

Albums contain 2-10 items; audio and document groups have type restrictions. Callback acknowledgement dismisses the waiting indicator and does not confirm media delivery. Callback data is limited to 64 bytes. [Albums](https://core.telegram.org/bots/api#sendmediagroup), [callbacks](https://core.telegram.org/bots/api#callbackquery), [buttons](https://core.telegram.org/bots/api#inlinekeyboardbutton).

Photo caching is also supported: `PhotoSize` contains reusable file IDs, dimensions, and optional size. For albums, Nuvio can store an ordered manifest of each item's ID/kind and reconstruct a group on reuse. The message's `media_group_id` identifies a delivered group inside its chat; the documented send method takes media items rather than that identifier. Mixed posts and albums spanning multiple groups therefore need an application-level composition record. [PhotoSize](https://core.telegram.org/bots/api#photosize), [Message](https://core.telegram.org/bots/api#message), [sendMediaGroup](https://core.telegram.org/bots/api#sendmediagroup).

### Delivery certainty and retries

The inspected Bot API send methods expose no caller-supplied idempotency key. This is an inference from the documented method parameters, not a claim about Telegram's internal implementation. Nuvio therefore cannot promise exactly-once delivery across a lost response or a crash between sending and recording the response.

HTTP guidance permits automatic retry of a non-idempotent operation only with evidence that it is safe, such as knowing the original request was not applied. A local operation ID helps coordinate Nuvio's own tasks but does not create a provider-side deduplication contract. [RFC 9110, section 9.2.2](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2), [idempotent API design](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/).

The relevant distinction is:

| Observed result | Proposed action |
| --- | --- |
| Successful response with returned message IDs | Record confirmed delivery before analytics, cache writes, or further UI work. |
| Explicit rate-limit refusal with `retry_after` | Wait within a bounded budget; recheck cancellation and moderation before another request. |
| HTTPX connection-pool timeout before request dispatch | Classify as definitely not sent; bounded retry is eligible. |
| Explicit invalid cached file reference | Invalidate that mapping; prepare a replacement only after a confirmed refusal. |
| Lost response or an ambiguous transport failure | Record unknown outcome; do not automatically resend or fall back to another send method. |
| Confirmed chat-access refusal | Explain delivery failure; downloading the source again will not repair it. |

PTB 22.8 explicitly reports that a request was not sent when HTTPX raises `PoolTimeout`. Other transport failures require cause-specific analysis; the broad `NetworkError` class is insufficient. `BadRequest` itself inherits `NetworkError`. [PTB backend source](https://github.com/python-telegram-bot/python-telegram-bot/blob/v22.8/src/telegram/request/_httpxrequest.py#L288), [error hierarchy](https://docs.python-telegram-bot.org/en/v22.8/telegram.error.html), [Telegram response parameters](https://core.telegram.org/bots/api#responseparameters).

## Concurrency and database work

PTB warns about concurrent updates with stateful handlers. Nuvio needs concurrency so cancellation and new requests can run during a download. Globally serializing updates would undermine that requirement. The proposal is short state transitions with revision checks after awaits, independent download tasks, and per-operation delivery ownership. A lock must not cover an entire download or prevent its cancellation callback. [PTB concurrency setting](https://docs.python-telegram-bot.org/en/v22.8/telegram.ext.applicationbuilder.html#telegram.ext.ApplicationBuilder.concurrent_updates), [concurrency guide](https://github.com/python-telegram-bot/python-telegram-bot/wiki/Concurrency).

WAL permits readers alongside a writer; it does not make a synchronous SQLite call asynchronous or permit multiple simultaneous writers. A busy handler can wait inside the calling thread. `INSERT OR REPLACE` deletes rows conflicting with unique constraints; it is not an in-place update. Use explicit conflict targets and define alias relationships deliberately. [SQLite WAL](https://www.sqlite.org/wal.html), [busy timeout](https://www.sqlite.org/pragma.html#pragma_busy_timeout), [REPLACE](https://www.sqlite.org/lang_conflict.html), [UPSERT](https://www.sqlite.org/lang_upsert.html).

For Nuvio, a bounded database worker or an asynchronous facade over complete synchronous transaction functions is sufficient to investigate first. `aiosqlite` is another option: it queues work on a connection's worker thread. It does not automatically make separately awaited statements one indivisible application transaction. Preserve connection ownership and keep transactions short. [Python thread offloading](https://docs.python.org/3.14/library/asyncio-task.html#asyncio.to_thread), [aiosqlite design](https://aiosqlite.omnilib.dev/en/stable/).

Cancelling an awaiting coroutine cannot stop an already running executor task by itself. Cooperative cancellation and temporary-file ownership must survive until the worker finishes. [Executor cancellation](https://docs.python.org/3.14/library/concurrent.futures.html#concurrent.futures.Future.cancel).

## Comparable projects: useful patterns and counterexamples

The snapshots below are reproducible references, not endorsements. Pyrogram/MTProto behavior must not be transferred directly to the Bot API.

| Project and inspected snapshot | Observed pattern | Appropriate use in Nuvio |
| --- | --- | --- |
| [tgbot-collection/ytdlbot](https://github.com/tgbot-collection/ytdlbot/tree/e03e55eb250b7d90969f7f435c0ea71abc8693ad) | Artifact key includes URL, quality, and format; cache is checked before preparation; IDs and metadata are saved after upload. Redis can fall back to process-local storage. Upload fallback catches broad exceptions. | Include output selection in identity. Do not copy silent durability loss or broad send fallback. [Engine](https://github.com/tgbot-collection/ytdlbot/blob/e03e55eb250b7d90969f7f435c0ea71abc8693ad/src/engine/base.py#L300), [storage](https://github.com/tgbot-collection/ytdlbot/blob/e03e55eb250b7d90969f7f435c0ea71abc8693ad/src/database/cache.py). |
| [antlis/tg-media-bot](https://github.com/antlis/tg-media-bot/tree/7118936cb028f22292ea4bbb2fdb287641059944) | URL plus format cache; entries preserve audio/video/document kind. A cached-send exception triggers eviction and a new download. Storage is optional JSON/in-memory. | Preserve returned kind and distinguish optional cache failure. Broad eviction/fallback can duplicate an ambiguous send and is unsuitable. [Uploader](https://github.com/antlis/tg-media-bot/blob/7118936cb028f22292ea4bbb2fdb287641059944/src/services/uploader.py#L435), [handler fallback](https://github.com/antlis/tg-media-bot/blob/7118936cb028f22292ea4bbb2fdb287641059944/src/bot/handlers.py#L233). |
| [iytdl/iytdl](https://github.com/iytdl/iytdl/tree/99a7ec26e2ab5a28068707a5e2f6f0fb6fef7730) | SQLite/aiosqlite caches search results; upload processing inspects video versus document. This is a different cache purpose and a Pyrogram implementation. | Distinguish metadata/search caching from an artifact index. Do not infer Telegram file retention or delivery idempotence from its search cache. [Cache](https://github.com/iytdl/iytdl/blob/99a7ec26e2ab5a28068707a5e2f6f0fb6fef7730/src/iytdl/sql_cache.py), [upload handling](https://github.com/iytdl/iytdl/blob/99a7ec26e2ab5a28068707a5e2f6f0fb6fef7730/src/iytdl/upload_lib/uploader.py#L200). |

In-flight deduplication is a separate pattern: one preparation per artifact key, with multiple waiting callers. It should share extraction/download work, not a delivery result across recipients. Each waiter retains its own cancellation and delivery state; shared temporary files need explicit lifetime management. [singleflight reference](https://pkg.go.dev/golang.org/x/sync/singleflight).

## Downloader outcomes and honest errors

yt-dlp has distinct download, fragment, and extractor retries. An `ExtractorError` marked `expected` describes an anticipated extraction failure, not proof of permanence or a recovery deadline. Nested `DownloadError.exc_info` can retain the underlying exception. A classifier should inspect structured evidence and the stage before using narrow message signatures. [Embedding and options](https://github.com/yt-dlp/yt-dlp/blob/master/README.md#embedding-yt-dlp), [locally installed exception definitions](https://github.com/yt-dlp/yt-dlp/blob/c7fb478d21e9e59524befbe23f7801bb267fb880/yt_dlp/utils/_utils.py#L979).

Nuvio currently permits skipping unavailable fragments. A synthetic local HLS experiment removed one of four segments: yt-dlp returned success, the result decoded 75 frames rather than the baseline's 100, and both containers still reported a 4-second duration. The skip notification starts with `[download]`, which Nuvio's progress filter excludes from the administrator report tail. File existence, a zero exit result, and duration are therefore insufficient completeness checks. The result was reproduced with both the existing local `2026.09.16.232951` and a temporary isolated installation of the pinned `2026.09.27.232945`. This proves the local preparation mechanism on the pin, not a production incident or live delivery. [Audit evidence and probe](../audits/cache-and-delivery-audit-2026-10-06.md#a14---p2-skipped-fragments-can-look-like-success).

Recommended public behavior:

| Confirmed condition | User explanation | Internal treatment |
| --- | --- | --- |
| Duration, size, unsupported content | State the actual constraint and a usable alternative. Waiting does not change the constraint. | Expected rejection; metrics and matching code, no per-event crash alert. |
| Source reports unavailable | State that the selected source path reported unavailability. Do not assert deletion/privacy. | Keep original evidence; aggregate patterns and extractor diagnostics. |
| Requested format absent | Offer a currently available format. | Record requested and resolved selection. |
| Explicit source rate limit | Explain the reported limit; a later attempt may help without a promised deadline. | Bounded retry budget; aggregate by platform. |
| Download connection failure | Explain the failed connection or deadline. | Preparation retry may be safe; delivery certainty remains separate. |
| Confirmed partial delivery | State what was sent and what stopped. | Preserve returned message IDs and the next unsent part. |
| Unknown delivery result | State that confirmation was lost and ask the user to check the chat. | No automatic resend; retain an operation reference. |
| Internal tooling, storage, cookies, unexpected failure | State that processing failed and provide the diagnostic code. | Technical context for administrators only; no promise that waiting fixes it. |

This refines [the existing error policy](../error-codes.md), rather than replacing its categories. Cookie-health and canary success describe particular probes at particular times; neither establishes availability of every user link.

## Proposed separation of state

| State | Identity and lifetime | Failure policy |
| --- | --- | --- |
| Telegram artifact index | Bot + source identity + output recipe + actual media kind, including photos; indefinite retention by default with optional WebUI policy | Optional optimization: read error becomes a monitored miss; write error after delivery must not reverse success. |
| Complete-post manifest | Source/recipe + ordered reusable items + composition version and related audio; retain with its referenced artifacts | Never present a partial or invalid composition as a complete cached album. |
| Source metadata | Source + access context + probe time; short freshness policy | Explicit source denial cannot be bypassed by a stale artifact automatically. |
| Cookie-health result | Platform + cookie-file revision + probe time; current 10-minute memoization | Diagnostic hint, never per-video authorization proof. |
| Callback/session state | User + session token + revision; bounded active sessions | Expiry rejects stale actions; short transitions must tolerate concurrent awaits. |
| Feedback and CSI submissions | Prompt/poll identity + user + submission identity | Durable deduplication and explicit message binding. |
| Delivery journal | User operation + recipient + part + attempt; durable state | Required before dispatch; uncertain crash recovery does not automatically resend. |
| Output tail | Bounded diagnostic buffer | Eviction is acceptable for logs, but not for artifact integrity or delivery truth. |

The journal and moderation database are authoritative state. They must not inherit the artifact cache's fail-open policy. No universal TTL or external bot implementation removes the need to choose these contracts explicitly.

## Storage and operator-controlled retention

The product decision is to keep valid file IDs, complete album manifests, and source aliases indefinitely by default. Age-based deletion is optional, with an enable switch and idle-retention period saved through the WebUI's existing shared settings database. This is a runtime product preference, not an environment setting. Metadata freshness and specific invalid-reference/output-recipe invalidation remain separate policies.

Disabling automatic artifact cleanup must also remove the current hidden 90-day read expiry. The inspected scheduled cleanup combines artifact deletion with temporary-media and cookie-workfile cleanup; the plan separates those responsibilities so downloaded files still get removed when ID retention is unlimited.

A small synthetic sizing experiment used 10,000 distinct rows in a copy of the current schema with all existing indexes, and measured the main database after WAL checkpointing. It stored metadata and IDs only:

| Scenario | URL / file ID / title bytes per row | Main database | Approximate bytes per row |
| --- | --- | --- | --- |
| Compact fields | 100 / 150 / 160 | 8.90 MiB | 933 |
| Larger fields | 512 / 512 / 1024 | 51.25 MiB | 5,374 |

The other fields were fixed: 32-byte unique IDs, synthetic platform/selection/date, and numeric size/duration. At the same density, 3 GiB corresponds to roughly 0.60-3.45 million rows. This is arithmetic extrapolation from synthetic records, not an annual forecast or a bound. Album items, aliases, future manifests, longer metadata, and runtime WAL usage change density; request volume changes the annual count. There is no measured basis for automatic eviction at 3 GB. The appropriate approach is to expose actual size and record counts, retain valid mappings, and let the operator enable cleanup when needed. [Reproducible sizing probe](../audits/fixtures/run_cache_storage_size_2026_10_06.py).

## Remaining verification gates

The local PTB version matches the pin (`22.8`); the developer environment's yt-dlp is older than the pinned `2026.9.27.232945.dev0`. The pinned package was additionally installed into a temporary directory for isolated fixture and suite checks without modifying that environment. This is not a complete locked-dependency image build. Real Bot API behavior, cloud/local route differences, media-type reuse, client rendering, and crash recovery remain acceptance tests in the change plan.

The local SQLite library reports `3.46.1`. Current upstream WAL documentation describes a rare reset bug and fixes in `3.51.3` or backports `3.44.6`/`3.50.7`. Version alone does not establish whether a vendor backported the fix or whether production is affected. Record actual image/library provenance before release; this research did not reproduce that bug. [Upstream WAL advisory](https://www.sqlite.org/wal.html).
