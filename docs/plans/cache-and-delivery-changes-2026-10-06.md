# Cache and delivery change plan

Date: 2026-10-06. Status: application implementation, local acceptance, and selected cloud Telegram delivery scenarios completed for the core contracts; the remaining external acceptance matrix, release, and measured shared-preparation optimization remain separate gates. See the [acceptance record](../audits/cache-and-delivery-acceptance-2026-10-06.md). The implementation sequence below retains its original acceptance requirements.

Inputs: [research and external contracts](../technical/cache-and-delivery-research-2026-10-06.md), [dated code audit](../audits/cache-and-delivery-audit-2026-10-06.md). Finding identifiers below refer to that audit.

## Target behavior

1. A cache accelerates preparation; its outage does not turn a confirmed delivery into a failure.
2. Reuse identifies the source, output recipe, bot, and actual Telegram media kind.
3. The bot distinguishes confirmed delivery, confirmed refusal, proven not-sent requests, and unknown outcomes. Every user status follows this evidence.
4. Cancellation stops future work where possible. It cannot retract an accepted message or imply that already sent media was never sent.
5. An incomplete download cannot enter the normal success/cache path.
6. Feedback and survey responses bind to the interaction the user actually answered.
7. Restart preserves dispatch evidence. It does not cause automatic replay of uncertain sends.
8. Cache coverage includes individual photos, complete photo albums, and mixed photo/video posts, with their original item order and related audio where supported.
9. Retain valid artifact IDs and source aliases indefinitely by default. Optional age-based cleanup is controlled in the WebUI, with its own enable switch and retention period; no environment variable is required for this product setting.

Retain SQLite, the file-ID optimization, concurrent update processing, platform recovery order, and fresh moderation checks. Evaluate larger infrastructure changes only after measuring contention and deployment requirements.

## Phase 0: Freeze evidence and verify the runtime

**Addresses:** A13; establishes gates for every other phase.

**Work:** build or provision an isolated environment from the hashed lock file. Record actual PTB, HTTPX, yt-dlp/EJS, Deno, FFmpeg, SQLite, Bot API version, and image digest. Check loaded versions, not only declared pins. Verify SQLite vendor patch provenance against the upstream WAL advisory. Repeat the synthetic HLS probe on this runtime. Do not overwrite a developer's existing environment merely to make the inventory match.

**Output:** acceptance record with environment, results, and unresolved differences. Add contract tests to `tests/` as each fix is implemented; the dated audit fixtures remain historical evidence.

**Exit criteria:** pinned dependencies are actually loaded; local/unit, CI/image, and live-chat evidence have separate statuses. A version mismatch is not silently waived.

## Phase 1: Make database access nonblocking and cache failure optional

**Addresses:** A01, A03. **Dependencies:** runtime inventory; no cache schema redesign required.

**Files:** `utils/video_cache.py`, handler call sites in `utils/telegram_utils.py`, `utils/analytics_db.py`, `utils/user_access.py`, relevant scheduled tasks and cache commands.

**Work:** introduce a bounded asynchronous database facade. Run a complete transaction within one worker-owned connection rather than offloading separate statements with uncertain ownership. Inventory all database calls reachable from asynchronous handlers; keep transaction boundaries short. Define time budgets and structured cache read/write failure results. Preserve the existing synchronous interface for callers that genuinely run outside the event loop if needed.

Cache reads may degrade to a monitored miss. Cache writes after successful delivery may fail without changing that delivery's result. Moderation checks remain fail-closed. The later dispatch journal must commit its required state before a send, so its errors cannot be treated as cache misses.

**Exit criteria:** induced SQLite contention does not block unrelated callbacks; cache read failure still allows authorized preparation; write failure after success cannot resend. Shutdown joins or safely drains database work. Tests cover thread ownership and bot/WebUI contention.

**Rollback:** revert the facade only after confirming no schema change depends on it. A rollback must not restore a false success/failure transition; retain delivery-state fixes independently.

## Phase 2: Unify delivery evidence and reject incomplete media

**Addresses:** A04, A05, A14. **Dependencies:** verified backend behavior; database failure boundaries from phase 1.

**Files:** `utils/telegram_utils.py`, `utils/social_delivery.py`, `utils/public_errors.py`, `utils/media_errors.py`, `utils/ytdlp_common.py`, platform downloaders, `utils/download_report.py`, `messages.py`, callback tests.

**Work:** use a common delivery result for all platforms, cache/fresh media, files, albums, subtitles, and description parts. Track operation/part identity, dispatch certainty, confirmed Telegram message IDs, and cancellation state separately from progress text. Record each successful media response immediately, before another await or optional side effect. Decide the user status from confirmed parts plus any outstanding uncertain part.

Classify `PoolTimeout` through the backend cause as proven not-sent. Permit only bounded safe retries with cancellation/moderation checks and correct file rewinding. Explicit `RetryAfter` retains its wait budget. Ambiguous response loss never initiates a second send, cached-file redownload, alternate media-method send, or URL/local fallback. Invalidate a cached reference only after a narrow confirmed invalid-reference refusal; other BadRequest cases need their own handling.

Require complete downloaded media. Start with strict unavailable-fragment behavior for the normal path and retain structured completeness evidence. Review API and CLI option consistency. Preserve skipped-fragment/error evidence outside progress filtering. File existence and container duration are supplementary checks, not the completeness contract.

**Exit criteria:** tests cover before/during/after-send cancellation, a late successful response after cancellation, pool timeout, explicit refusal, ambiguous transport failure, RetryAfter cancellation, and failure after the first album/description part. Every path preserves confirmed messages and refuses unsafe fallback. The missing-segment fixture cannot be delivered or cached as normal success on the pinned runtime.

**Rollback:** keep the truthful status/outcome layer even if strict preparation causes availability regressions. Investigate affected extractor paths; do not silently restore incomplete output as ordinary success.

## Phase 3: Bind feedback and deduplicate survey submissions

**Addresses:** A06, A07. **Dependencies:** short state-transition pattern; can proceed before cache migration.

**Files:** `utils/feedback.py`, `utils/analytics_db.py`, `utils/telegram_utils.py`, `messages.py`, feedback/CSI tests. WebUI rendering changes only if needed to expose the new record identity.

**Work:** bind feedback forms to returned prompt message IDs and a form revision. Prefer an explicit reply association; define a deterministic plain-text response policy when multiple prompts were visible. Revalidate ownership after every prompt send. An obsolete prompt must be visibly expired or require selection, rather than silently using another issue's context. Do not hold a per-user lock across a download.

Create a durable survey instance before sending its prompt, then bind the returned chat/message. Enforce one logical response per survey/user with a unique constraint and an explicit first-vote or change-vote policy. Distinguish callback-query replay from multiple different taps on one poll. Add a migration without deleting historical ratings; handle old unbound prompts explicitly.

**Exit criteria:** reversed prompt completion, old prompt cancellation, double taps, changed votes, and restart/replay cannot misbind or duplicate records. A new survey can collect a new vote. English/Russian WebUI text remains complete where changed.

## Phase 4: Replace ambiguous cache mappings with artifact and alias records

**Addresses:** A02, A08, A09. **Dependencies:** phases 1-2.

**Files:** `utils/video_cache.py`, `utils/platform_actions.py`, `utils/telegram_utils.py`, `utils/social_delivery.py`, downloader result metadata, `utils/cache_commands.py`, schema tests and documentation.

**Proposed model:**

| Record | Required fields |
| --- | --- |
| Source identity | Platform, stable platform content ID when known, conservative canonical URL, restricted-access scope where applicable. |
| Output recipe | Version, output family, container/codec policy, audio/subtitle language where selected, requested quality, processing/fast-path policy. |
| Telegram artifact | Bot identity from `getMe`, recipe/source reference, actual media kind including photo, `file_id`, nonunique `file_unique_id` evidence, resolved format/quality, size, duration, photo dimensions, created/verified/last-successful-use times, completeness state. |
| URL alias | Submitted/canonical URL and selection mapping to an artifact; several aliases may point to one artifact. |
| Post manifest | Source/recipe identity, ordered artifact references and their positions/kinds, expected item count, completeness/version, and optional related audio reference. |

For a photo, store a suitable returned `PhotoSize.file_id` and its actual dimensions, preserving whether the delivery was a photo or document. For an album, store one artifact per item plus the ordered manifest. Reconstruct subsequent sends using those file IDs; `media_group_id` is delivery evidence, not a reusable album-file identifier. Preserve mixed composition and deterministic grouping for more than ten items. A single-photo post uses the single-photo path. Description choice remains independent of reusable media where it only changes text.

Publish a reusable complete-post manifest only once every expected item has a verified artifact and composition is complete. Confirmed individual items from a partial attempt can remain cached, but the partial manifest must not become an ordinary successful full-post hit. During reuse, plan missing-item preparation before sending the post. A confirmed invalid item reference invalidates that item's mapping and complete-manifest eligibility, not every other valid item or unrelated alias. If the refusal happens after some groups were sent, retain confirmed parts and use the common partial-delivery policy; never resend the entire album automatically.

Store the actual resolved output separately from the requested selection. A lower-quality fallback must not satisfy a stricter request silently. Caption/description choice can share media only where it changes delivery text without changing the artifact. Never hash cookies into public keys or logs; use a nonsecret access-scope identity if restricted sources are supported.

Use explicit UPSERT targets. Never make a file unique ID force deletion of another alias. Retain submitted source aliases; canonicalization is a lookup relationship, not deletion of useful links. Preserve meaningful URL parameters; prove individual tracking-parameter normalization with fixtures.

**Migration:** use a SQLite-consistent backup rather than copying only the main file while WAL is active. Create new tables and record a migration version transactionally. Keep legacy tables readable for inspection. Promote an old row only when bot, kind, recipe, and completeness can be established; otherwise treat it as an unverified miss and let a fresh confirmed send create a new record. No blanket metadata inference from action names. Never modify analytics/moderation history as part of cache migration.

**Exit criteria:** alias preservation, cross-bot isolation, actual kind reuse, language/recipe separation, downgraded-format handling, old-data migration, and interrupted migration tests pass. Live Telegram confirms video/audio/document/photo reuse and complete album reuse, including original order, 1/2/10/11+ items, mixed posts, descriptions, and separate related audio. A bad item cannot silently shorten an album or trigger a duplicate of confirmed groups.

**Rollback:** test an old-reader/new-schema matrix before release. Initially keep the legacy table untouched and do not dual-write misleading new recipe records into it. Rolling back the application can lose new cache hits but must retain old readable mappings; re-enable the old reader only after validating its delivery contract. Restoring the cache backup is acceptable when necessary because the cache is disposable. This policy does not apply to authoritative databases.

## Phase 5: Persist operations and recovery evidence

**Addresses:** A10; supports A04-A05. **Dependencies:** common outcome model and nonblocking authoritative storage.

**Files:** a delivery-journal module, `main.py`, `utils/telegram_utils.py`, WebUI diagnostics if needed, migration/recovery tests.

**Work:** assign a logical operation to an authorized user action and a part/attempt identity to each send. Duplicate processing of that same action reuses the operation; a deliberate new user request creates a new one. Persist intent before the external request, dispatch-started state before calling Telegram, and confirmed message IDs immediately after a response. Store known refusal/not-sent/unknown outcomes. Store no token or signed source URL unnecessarily; apply explicit retention to operational records.

Define a durability level appropriate to process crash and power loss. Evaluate a dedicated journal with `synchronous=FULL` rather than assuming the cache's `NORMAL` setting is enough. If the required pre-dispatch commit fails, do not send. If post-send recording fails, retain unknown outcome and surface the operational fault; a cache write cannot repair this evidence gap.

On restart, unfinished dispatch-started attempts become unknown. Lease expiry alone must never make them eligible for resend. Prepared-but-not-dispatched work may resume only after current authorization/cancellation checks and resource validation. Confirmed parts remain sent; later parts need an explicit recovery policy.

**Exit criteria:** process termination before intent commit, before/after dispatch, after Telegram response, and during journal commit does not cause automatic uncertain replay. Recovery communicates evidence accurately. The acceptance record explicitly states the remaining cross-system uncertainty window; no exactly-once claim.

**Rollback:** retain journal data and compatible readers. Disable new dispatch/resumption if a rollback cannot interpret in-flight records; do not drop the table or reinterpret unknown as not sent.

## Phase 6: Optimize freshness and duplicate preparation from measurements

**Addresses:** A11, A12. **Dependencies:** reliable artifact identity, moderation, and delivery state.

### WebUI retention settings

Persist `cache_auto_cleanup_enabled` and `cache_retention_days` in the existing shared settings database so bot and WebUI read one policy. These are proposed internal setting keys, not environment variables. Default automatic artifact cleanup to disabled for new installations and existing installations without an explicit persisted choice; the old hardcoded 90-day rule is not an operator choice. Preserve an operator's subsequently saved settings across upgrades and restarts.

The WebUI provides an enable switch and a positive integer period labeled as days without successful reuse. When disabled, show that cache entries are retained indefinitely and the period is inactive. When enabled, the cleanup worker removes entries unused beyond the configured period, using last successful use or creation for never-reused entries. Keep this policy separate from source-metadata freshness and confirmed invalid-ID/recipe invalidation. Do not discard a record on read solely because the previous `CachedVideo.is_valid` default is 90 days. Manual age-cleanup commands must use the same enabled policy; any explicit targeted removal is a separate action.

Changes take effect without changing `.env` or restarting the bot. Validate and save the policy atomically, use the existing authentication/CSRF pattern, and maintain complete English/Russian UI copy. Recheck the current policy before a scheduled deletion transaction so disabling cleanup is honored by an already queued job; keep policy reads/changes and deletion sufficiently coordinated to avoid a stale snapshot deleting records. Isolate this job from cleanup of downloaded temporary media, cookie workfiles, logs, and analytics so those tasks continue normally.

Show database size, file/manifest/alias counts, and last/next artifact-cleanup status in the WebUI. Include WAL usage in operational disk reporting. No automatic 3 GB quota or age eviction is introduced from an unverified annual-size estimate. Measure future schema/index/manifest growth; retain valid mappings unless the operator has enabled cleanup or a specific record fails its reuse contract.

**Retention acceptance:** records older than 90 days still produce hits when cleanup is disabled; scheduled/manual age cleanup deletes none. Enabling cleanup uses the saved idle period and preserves active artifacts and coherent manifests/aliases. Toggle changes survive restart, affect the bot without restart, and cannot race a queued job using the old policy. Disabling artifact cleanup leaves temporary-media cleanup working. Shared artifacts are removed only when no retained manifest or alias still needs them.

### Preparation performance

**Work:** measure extraction/preparation latency, cache hit/miss reasons, upload latency, event-loop lag, and database waits. Define artifact retention separately from metadata freshness. Evaluate a lightweight early cache lookup only with enough stored metadata to enforce current restrictions and output selection. Explicit source denial still overrides stale reuse. A transient-source-failure policy, if enabled, must disclose use of previously cached media and state its verification time.

Introduce bounded singleflight-style preparation only when duplicate work is observed or a controlled benchmark demonstrates value. Share preparation results, not recipient delivery. Use reference-counted ownership for temporary media and detach a cancelled waiter without stopping others. No shared registry may grow indefinitely.

**Exit criteria:** warm/cold and concurrent equivalent-request benchmarks show the intended benefit; callbacks remain responsive. Restricted scope, differing recipes, individual cancellation, failure cleanup, and last-waiter cancellation remain correct. Artifact retention follows the saved WebUI policy; metadata freshness has its own documented validation policy.

## Cross-cutting error and retry policy

Retain current public category names and diagnostic IDs. Add structured evidence for stage, underlying exception, HTTP status, provider refusal, dispatch certainty, resolved recipe, and completeness. Use text signatures only where stronger evidence is absent. `ExtractorError.expected`, healthy cookies, and a successful canary must not imply permanence or availability of another source.

Budget retries across downloader retries, fragment retries, cookie modes, CLI fallback, and outer backoff, with an overall deadline. Preserve YouTube's without-cookies, with-cookies, then eligible CLI recovery order. A typed failure determines whether the next preparation attempt is eligible; it must not authorize replay of an uncertain Telegram send. Test that cancellation of the awaiting task leaves no worker using deleted media.

Keep expected user constraints as ordinary rejection metrics. Aggregate source/runtime failures and transport problems by platform/category so a cluster remains visible without sending a crash report for every expected event. Unknown delivery and internal faults need administrator evidence. Set alert thresholds from observed traffic rather than invented production rates.

## Phase 7: Controlled acceptance, release, and rollback

**Dependencies:** corresponding implementation phases; this plan does not initiate a deployment.

| Scenario | Required evidence |
| --- | --- |
| Fresh and cached video/audio/document, bot isolation | Actual returned kind, message IDs, file reuse, and consistent user status in a controlled chat. |
| Fresh and cached photo, complete photo/mixed album, related audio | No source download on a valid complete artifact hit; item order, kind, quality, composition and description choice preserved. |
| Cloud upload, local upload, URL handoff | Actual route and configured limits; boundary fixtures without assuming local mode increases URL-fetch limits. |
| 2/10/11+ album items, mixed composition, descriptions | Order, grouping, confirmed-part tracking, rendering in supported desktop/mobile clients. |
| Cancellation and deliberate rate-limit fixture | Prompt acknowledgement, cooperative worker stop, truthful partial/unknown status, no accidental resend. |
| Synthetic invalid file ID, pool exhaustion, lost response | Narrow invalidation; not-sent versus unknown classification; observed send count. |
| Duration, unavailable source, size, missing format, internal failure | Public explanation matches evidence and code; private details stay in administrator diagnostics. |
| SQLite contention/outage, migration, process restart | Event-loop responsiveness, cache fallback, fail-closed authoritative state, retained recovery evidence. |
| WebUI cleanup off/on, changed retention, queued-job race | Persistent policy, immediate application, no hidden 90-day expiry, coherent aliases/manifests, and independent temporary-media cleanup. |
| Missing media fragment | No normal successful delivery/cache entry; completeness evidence survives logging. |
| Concurrent feedback and CSI taps | Correct prompt association and one logical survey response, including restart. |

Use a test bot/chat and synthetic or licensed media. Fault injection belongs in the isolated environment. Do not disrupt the production network to manufacture failures.

Run meaningful unit/integration tests, lint, coverage, image checks, and the controlled acceptance matrix. Record CI and live acceptance separately. Release small phases so failures have a clear rollback boundary. Before schema changes, verify backup restoration. After deployment, compare measured callback latency, cache misses, incomplete-media rejections, unknown outcomes, and aggregate platform failures with the baseline; unexpected shifts require investigation or rollback.

## Implementation order and completion definition

First implement phases 1-3 after the runtime gate: responsiveness, truthful outcomes/completeness, and correct feedback records. Then migrate artifact identity, add durable recovery, and evaluate performance optimizations. Some preparation can overlap, but schema and recovery integration must follow their dependencies.

An individual finding is closed only when its desired behavior is covered by a regression test and any required live acceptance is recorded. A green historical reproduction fixture or unit suite is not closure. Completion requires tested migrations/rollback, updated public error policy and English/Russian documentation where behavior changes, and an explicit list of still-unverified external behavior.
