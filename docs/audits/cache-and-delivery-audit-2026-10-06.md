# Cache and delivery audit

Date: 2026-10-06. Scope: artifact caching, Telegram delivery outcomes, callback concurrency, feedback/CSI binding, and error evidence. This is not a whole-repository security audit.

The audit examined the working tree based on commit `f8a92b12453f87ef840a6d366cf17e0237119bcc`, including the existing uncommitted error-handling and feedback/moderation changes. Application code was not modified during this investigation. Findings describe this dated snapshot and should be revalidated after implementation.

Basis: [primary-source research](../technical/cache-and-delivery-research-2026-10-06.md). Remediation: [phased change plan](../plans/cache-and-delivery-changes-2026-10-06.md).

## Verdict and validation

The file-ID optimization is appropriate. Cache identity, synchronous storage access, and delivery truth need changes. Fourteen findings are listed below: one P1, ten P2, and three P3. They include eight reproduced defects/contract mismatches, one additional local media-integrity experiment, and five source-based design or validation gaps. No P0 was established.

| Check | Result | What it establishes |
| --- | --- | --- |
| Existing test suite in isolated data/temp directories | 1096 passed; eight PTB deprecation warnings | Existing assertions pass; not production or real Telegram acceptance. |
| Existing suite with a temporary pinned yt-dlp package | 1096 passed; eight PTB deprecation warnings | Suite compatibility with the pin; still not a complete locked-image or external extraction test. |
| Eight isolated audit fixtures | 8 passed with both the existing and pinned yt-dlp packages | The fixtures assert the problematic behavior; passing confirms its presence, not a fix. |
| Synthetic local HLS probe | Both versions: success result `0`; 75/100 frames retained after one missing segment | Local downloader options can yield incomplete media without an error, including on the pin. |
| PTB runtime | `22.8`, matches the dependency pin | Library-specific timeout analysis matches this local version. |
| yt-dlp runtime | Existing environment `2026.9.16.232951.dev0`; temporary isolated package `2026.9.27.232945.dev0` | A complete locked-dependency image check remains required. |
| SQLite runtime | `3.46.1` | Local runtime identity only; production/vendor patch provenance unverified. |

No production incident frequency, database contention distribution, natural event-loop latency, Telegram resend behavior, or client rendering was measured. Prior crash reports motivated the review but their private identifiers and media links are not reproduced here.

## Cache and state inventory

| Component | Current role | Assessment |
| --- | --- | --- |
| [video_cache.py](../../utils/video_cache.py) | Persistent Telegram IDs keyed by raw URL and format/action; 90-day TTL; WAL | Keep the optimization, repair identity, aliases, kind, asynchronous access, and failure boundaries. |
| [cookie_health.py](../../utils/cookie_health.py) | Process-local probe memoization for 600 seconds, invalidated by file size/mtime | Reasonable diagnostic cache; display probe timestamp and scope, not a universal authorization guarantee. |
| [callback_fsm.py](../../utils/callback_fsm.py) and [telegram_utils.py](../../utils/telegram_utils.py) | Bounded per-user sessions, per-session action guard, volatile delivery flags | Suitable interaction state; insufficient as durable delivery evidence. |
| [download_report.py](../../utils/download_report.py) | Bounded diagnostic tails and resolved format side channel | Bounded logs are reasonable. Integrity and recipe selection must not depend only on an evictable diagnostic buffer. |
| [feedback.py](../../utils/feedback.py) | Expiring issue context and one active feedback form per user | The intended bounds are useful; concurrent prompts can disagree with active form identity. |
| [analytics_db.py](../../utils/analytics_db.py) | Durable user restrictions, feedback, and CSI results | Authoritative records, not disposable cache. Preserve fail-closed moderation and idempotent submissions. |

## Findings

### A01 - P1: SQLite cache operations block the event loop

**Evidence:** reproduced. [Connection and transaction code](../../utils/video_cache.py), `_deliver_cached_audio`, `_deliver_cached_video`, and `_cache_sent_media` in [telegram_utils.py](../../utils/telegram_utils.py) call synchronous database operations from asynchronous handlers. Connections permit a 30-second busy wait.

**Reproduction:** hold `BEGIN IMMEDIATE` on a second connection for 250 ms, schedule an asynchronous heartbeat, then call `cache.set`. The heartbeat was delayed approximately 329 ms in the recorded run. This proves a contention mechanism, not a measured 30-second production stall.

**Impact:** one blocked write can delay unrelated callbacks and cancellation. WAL does not remove this wait.

**Remediation:** offload complete database units to a bounded worker; preserve thread/connection ownership and short transactions. Include handler-side analytics and moderation calls in the call-site review without weakening moderation semantics.

**Acceptance:** an induced 250 ms lock does not prevent a heartbeat or cancellation callback from running; database wait is bounded and separately observable.

### A02 - P2: An alias replaces another URL's cache entry

**Evidence:** reproduced. [video_cache.py](../../utils/video_cache.py) declares `file_unique_id UNIQUE` and writes with `INSERT OR REPLACE`.

**Reproduction:** insert two different URLs with the same file unique ID. The second remains and the first disappears. SQLite's REPLACE semantics explain the result.

**Impact:** equivalent links repeatedly miss the cache, and cache hit statistics understate reuse.

**Remediation:** separate artifact identity from URL aliases or remove the inappropriate uniqueness constraint in a safe interim migration. Use a conflict target that updates only the intended mapping.

**Acceptance:** both aliases remain readable; updating one mapping does not delete unrelated mappings; migration preserves existing valid data and WAL behavior.

### A03 - P2: A cache read error aborts ordinary preparation

**Evidence:** reproduced through the real audio callback in [telegram_utils.py](../../utils/telegram_utils.py).

**Reproduction:** make cache `get` raise `sqlite3.OperationalError`. The normal downloader is never called and the callback produces a failure.

**Impact:** failure of an optional optimization becomes loss of the core service.

**Remediation:** catch cache-storage faults at the cache facade, report a monitored miss, and continue after the normal access checks. A cache write failure after confirmed delivery must retain success. Do not apply this policy to moderation or the proposed dispatch journal.

**Acceptance:** failed cache read still reaches preparation; failed cache write cannot trigger a second delivery; moderation-storage failure still prevents unauthorized dispatch.

### A04 - P2: Cancellation can deny a confirmed cached send

**Evidence:** reproduced. `_deliver_cached_audio` and `_deliver_cached_video` do not consistently record delivery progress; cancellation in [telegram_utils.py](../../utils/telegram_utils.py) relies on this progress. Some other platform paths already record it.

**Reproduction:** let cached audio return a successful Telegram message, pause subsequent analytics, then press cancel. Progress remains zero and the response says nothing was downloaded.

**Impact:** the user receives false feedback and may request a duplicate.

**Remediation:** record confirmed messages immediately after every successful media call, before analytics, caching, description sends, or awaits. Use one delivery-state mechanism for all platforms and cached/fresh paths.

**Acceptance:** cancellation before dispatch, during dispatch, after one confirmed part, and after full success produce distinct truthful statuses.

### A05 - P2: A known pre-dispatch timeout is marked unknown

**Evidence:** reproduced and verified against PTB 22.8 source. `_call_telegram_with_retry_after` marks all non-BadRequest `NetworkError` cases unknown in [telegram_utils.py](../../utils/telegram_utils.py).

**Reproduction:** raise `TimedOut` with `httpx.PoolTimeout` as its cause. `_delivery_outcome_unknown` becomes true even though the backend knows the request was not sent.

**Impact:** the user is told to check for a potentially delivered file when no request was dispatched; a safe recovery path is suppressed.

**Remediation:** cause-specific dispatch certainty, with a bounded retry budget for proven not-sent requests. Preserve no automatic resend for ambiguous transport failure.

**Acceptance:** pool timeout stays not-sent; lost response stays unknown; explicit Telegram refusals stay refused; tests also cover nested causes and library-version changes.

### A06 - P2: Concurrent feedback prompts refer to different issues

**Evidence:** reproduced in [feedback.py](../../utils/feedback.py).

**Reproduction:** start issue A's prompt and delay its send; start and complete B's prompt; then finish A. The last visible prompt shows A while the active form and stored response refer to B.

**Impact:** feedback can be attached to the wrong error and source link.

**Remediation:** bind forms to returned prompt message IDs and revisions; use short state coordination and revalidate after sends. Define how a plain text response chooses an active prompt, with explicit ambiguity handling.

**Acceptance:** reversed send completion cannot silently save to another issue; cancel or failure of an old prompt cannot close the current form.

### A07 - P2: One CSI poll accepts duplicate ratings

**Evidence:** reproduced. The `csi` callback in [telegram_utils.py](../../utils/telegram_utils.py) saves before editing; [save_csi_rating](../../utils/analytics_db.py) has no poll identity constraint.

**Reproduction:** invoke the same poll's rating callback twice while the first message edit waits. Two database rows are inserted.

**Impact:** repeated taps distort survey results. Different callback-query IDs do not mean different polls.

**Remediation:** persist a survey/prompt identity; atomically claim or update a response under an explicit one-vote/change-vote policy. Callback acknowledgement alone is not deduplication.

**Acceptance:** two concurrent submissions and a replay after restart produce one logical response for the same poll/user; a new poll remains independent.

### A08 - P2: Artifact keys omit bot and output-recipe identity

**Evidence:** source-confirmed design gap, not a reproduced wrong-file incident. [video_cache.py](../../utils/video_cache.py) uses `(url, format_id)`; [platform_actions.py](../../utils/platform_actions.py) uses action keys such as `direct_video`, `tg_video`, and `combined:<format_id>`.

**Trigger:** reuse a database with another bot, change the fast-path/output recipe, or request a selection whose language/container/processing recipe differs. Tracking variants also remain separate raw URLs.

**Impact:** the key cannot establish that a stored artifact satisfies the new request. The documented fast-path toggle already retains old cached quality.

**Remediation:** include bot identity, canonical source identity, versioned recipe, requested output and resolved output evidence. Scope access-restricted artifacts separately. Strip only proven tracking parameters; preserve parameters affecting content.

**Acceptance:** bot namespaces, recipe revisions, language selections, and genuine content parameters never collide; supported tracking aliases share a mapping. Existing records are not assigned guessed semantics.

### A09 - P2: The cache loses Telegram's returned media kind

**Evidence:** reproduced contract mismatch. `_cache_sent_media` accepts `video`, `audio`, or `document`, but [CachedVideo](../../utils/video_cache.py) stores no kind. `_deliver_cached_video` calls `reply_video` unconditionally.

**Reproduction:** supply a mocked successful message with `document` and a video action key. Caching succeeds, and reuse sends that document ID to `reply_video`. The experiment proves application behavior, not live Telegram acceptance of that first send.

**Impact:** the application cannot follow Telegram's same-media-type reuse contract for such a record. The inspected cache helper also ignores returned photos, and the current row schema has no ordered album/post manifest. Photo and album reuse therefore need explicit coverage in the replacement model; this extension is source-based, not an additional live reproduction.

**Remediation:** record actual message kind and choose the matching method. Include returned photo IDs/dimensions and ordered complete-post manifests, retaining separate reusable items and related audio where supported. Unknown legacy kind needs a miss/quarantine policy rather than inference from the button name.

**Acceptance:** live accepted video/audio/document/photo fixtures retain their type on reuse; complete albums retain composition and order. Incompatible/unknown mappings cause controlled preparation, never a broad send fallback or silent partial-album success.

### A10 - P2: Delivery uncertainty is lost across restart

**Evidence:** source-confirmed recovery gap. Delivery flags and confirmed-message lists live in session dictionaries; [main.py](../../main.py) does not configure durable session persistence or a dispatch journal.

**Trigger:** Telegram accepts a send, then the response is lost or the process dies before storing it. Restart clears the volatile evidence.

**Impact:** Nuvio cannot explain or recover that operation reliably. This finding does not assert that current code automatically replays it; a new user request can independently duplicate the file.

**Remediation:** persist intent and attempt state before dispatch, confirmed message IDs after response, and uncertain recovery state. A journal narrows uncertainty but cannot close the cross-system crash gap.

**Acceptance:** injected restarts at every dispatch boundary retain honest state and do not automatically resend uncertain attempts.

### A11 - P3: Retention and freshness are one implicit policy

**Evidence:** source-confirmed product decision gap. The artifact TTL is 90 days, both in cached-row validity and the scheduled cleanup call; YouTube [process_url](../../utils/telegram_utils.py) extracts current metadata before offering cached actions. The inspected WebUI settings do not expose artifact cleanup policy. Artifact pruning and temporary-media/cookie-workfile cleanup currently share one scheduled function.

**Impact:** warm file reuse still depends on fresh extraction, and expiry forces a new upload even though Telegram does not define that expiry. These choices may be deliberate; they are not inherently defects.

**Remediation:** retain valid IDs and source aliases indefinitely by default, with optional age cleanup enabled and configured through persisted WebUI settings. Respect the same policy in lookup and deletion paths; disabling the job alone must not leave hidden 90-day expiry on reads. Separate artifact pruning from temporary-file cleanup. Name metadata freshness independently, decide explicitly whether a transient source failure may use a previously verified artifact, and do not bypass current moderation/duration/size limits or confirmed source denial.

**Acceptance:** WebUI settings persist and take effect without restart. Cleanup disabled preserves records older than 90 days, and queued/manual age cleanup cannot delete them; temporary-media cleanup continues. Enabled retention preserves coherent manifests/aliases. Warm-path latency and source-check frequency are measured; stale-data behavior is documented and tested rather than assumed.

### A12 - P3: Preparation is not deduplicated across sessions

**Evidence:** source-confirmed optimization gap. The guard is per session in [telegram_utils.py](../../utils/telegram_utils.py); no shared artifact-preparation registry was found.

**Trigger:** concurrent cache misses for equivalent requests create independent extraction/download jobs.

**Impact:** potential redundant work; production load and savings are not measured.

**Remediation:** consider bounded singleflight-style preparation after identity fixes and instrumentation. Each recipient still owns its delivery. Shared work cancellation and temporary-file cleanup require explicit waiter ownership.

**Acceptance:** equivalent simultaneous requests prepare once, differing recipes remain independent, and cancelling one waiter cannot break another recipient's delivery.

### A13 - P3: Local green tests do not validate the pinned image

**Evidence:** measured runtime mismatch and missing acceptance evidence. The local yt-dlp version predates [requirements.in](../../requirements.in); the suite mocks platform extraction and Telegram delivery.

**Impact:** local success cannot establish deployed extractor behavior, SQLite patch provenance, local Bot API limits, or client rendering. The additional temporary pinned-package checks narrow the library-version gap, but do not replace a clean locked-dependency image or actual platform extraction.

**Remediation:** run a clean pinned-runtime gate and the controlled acceptance matrix in the plan. Verify SQLite vendor patch status against the upstream WAL advisory without assuming version alone establishes exposure.

**Acceptance:** record image digest, loaded versions, dependency checks, library provenance, and actual controlled-chat results separately from unit/CI results.

### A14 - P2: Skipped fragments can look like success

**Evidence:** local synthetic media experiment plus source inspection. [ytdlp_common.py](../../utils/ytdlp_common.py) enables skipping unavailable fragments. Its progress filter removes `[download]` lines from the diagnostic report tail, including the observed skipped-fragment notification.

**Reproduction:** create four one-second HLS segments, record a baseline, delete the second segment, and download again with the same fragment policy. Local yt-dlp returns `0`; the baseline has 100 decoded frames and the result 75. Both report 4.0 seconds because timestamps retain the missing interval.

**Impact:** a successful return and plausible duration can conceal missing content. The same result was reproduced on a temporary installation of pinned yt-dlp `2026.09.27.232945`. No production delivery or caching of this fixture was performed; the complete image remains a gate.

**Remediation:** require complete media for normal success; abort or explicitly propagate incomplete extraction before delivery/cache insertion. Preserve completeness evidence as structured output, outside the progress-log filter. Do not infer completeness from duration alone.

**Acceptance:** the pinned-runtime missing-segment fixture fails preparation or produces an explicit unsupported-partial result; it cannot become a normal successful cached artifact.

## What is already correct

The current duration exception and unavailable-video category give safer feedback than the original generic retry message. Cached IDs are generally saved after delivery. SQL inputs are parameterized and databases use WAL. Per-session guards, callback parsing, cancellation checks, and narrowly bounded explicit `RetryAfter` handling provide useful foundations. Fresh moderation checks are present before delivery attempts. These are retained in the plan; the findings do not justify replacing every cache or serializing all updates.

## Reproducing this audit safely

The [eight historical fixtures](fixtures/test_cache_delivery_contract_2026_10_06.py) are outside the default `tests/` suite. They assert the dated faulty behavior. After remediation, convert each relevant assertion into a regression test for the desired contract rather than expecting this audit fixture to keep passing.

Run from the repository root in a separate process with temporary database directories and a dummy token:

```bash
.venv/bin/python - <<'PY'
import os
import subprocess
import tempfile

with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as media:
    audit_env = dict(os.environ, DATA_DIR=data, TEMP_DIR=media,
                     TELEGRAM_TOKEN="123456:synthetic-audit-token", ADMIN_IDS="999",
                     LOG_LEVEL="CRITICAL")
    subprocess.run([
        ".venv/bin/python", "-m", "pytest", "-q",
        "docs/audits/fixtures/test_cache_delivery_contract_2026_10_06.py",
    ], env=audit_env, check=True)
PY
```

The [HLS probe](fixtures/run_hls_completeness_2026_10_06.py) requires real yt-dlp, FFmpeg, and ffprobe. It uses generated media, temporary files, and a loopback HTTP server only:

```bash
.venv/bin/python docs/audits/fixtures/run_hls_completeness_2026_10_06.py
```

These commands do not send Telegram messages or contact source platforms. Normal logger initialization can create or append the ignored local bot log. Keep fixture observations distinct from live acceptance.

For the full suite, retain `LOG_LEVEL=INFO`: six tests assert warning logs and fail under `CRITICAL` solely because those records are disabled. The temporary pinned-package run was repeated with the normal log level and passed all 1096 tests. These harness-setting failures were not classified as application regressions.
