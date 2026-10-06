# Cache and delivery implementation acceptance

Date: 2026-10-06. Scope: implementation against the [dated audit](cache-and-delivery-audit-2026-10-06.md) and [change plan](../plans/cache-and-delivery-changes-2026-10-06.md). This records local acceptance and the controlled cloud Telegram delivery scenarios listed below. It is not a production deployment or full acceptance of every external integration.

## Implemented contracts

| Audit finding | Implementation and regression evidence |
| --- | --- |
| A01, A03 | Whole SQLite transactions run in bounded worker pools; optional cache has a separate pool. Real write-lock tests demonstrate a running event-loop timer and an independent authoritative operation while cache work waits. Cache outages become misses/optional write failures; access and dispatch intent remain authoritative. |
| A02, A08, A09 | Independent aliases, bot/source/recipe identity, actual returned media kind and photo dimensions, ordered complete posts, related audio, and groups of 1/2/10/11/21 items. Partial compositions never become complete hits. A refused later group does not resend earlier confirmed groups. |
| A04, A05 | Confirmed responses record progress before subsequent awaits; causal HTTPX pool timeout permits bounded safe retry, read timeout remains unknown. Cancellation while waiting for a dispatched RPC retains unknown evidence. Optional cache writes cannot trigger a resend. |
| A06, A07 | Feedback uses prompt identity and an explicit reply after overlapping forms. Pending forms do not fall through into URL handling. CSI tokens bind user and prompt; atomic uniqueness makes repeated taps one immutable vote. Unbound old invitations expire safely. |
| A10 | FULL/WAL dispatch journal persists intent before the request, confirmed message IDs, refusals, not-sent and unknown outcomes. Descriptions also have journal parts without inflating media counts. Restart recovery converts unfinished requests to unknown without replay. Failures before intent prevent sending; failures after a confirmed response preserve in-memory delivery evidence. |
| A11 | Indefinite default retention; persistent EN/RU WebUI toggle and period; authentication, CSRF, atomic validation and immediate policy use. A real queued cleanup/SQLite-lock test disables policy before lock release and confirms no deletion. Shared artifacts survive deletion of an expired alias. Temporary cleanup is independent. |
| A14 | Strict API/CLI unavailable-fragment behavior and preserved fragment diagnostics. A synthetic four-segment HLS fixture with one missing segment rejects both paths and creates no normal final output. CLI and FFmpeg subprocess waits honor the shared preparation deadline. |
| A13 | The hashed runtime lock is loaded in an isolated image; actual SQLite is verified and fixed as described below. Live delivery and CI remain explicit external gates. |

These tests verify code contracts. They do not establish incident frequency, production callback latency, external platform availability, client rendering, or full closure of findings that require controlled live acceptance.

## Runtime verification

The image was built locally from the repository Dockerfile without mounting production data or using production Telegram credentials. Its offline media-runtime check reports:

| Component | Loaded version |
| --- | --- |
| Python | 3.14.8, pinned slim image |
| python-telegram-bot | 22.8 from hashed lock |
| HTTPX used by PTB | 0.28.1 |
| yt-dlp | 2026.9.27.232945.dev0; loaded display 2026.09.27.232945 |
| yt-dlp EJS | 0.8.0 |
| Deno | 2.9.7 |
| SQLite | 3.53.4 |
| FFmpeg | 7.1.5-0+deb13u1 |

Local final image configuration ID: `sha256:9c29f0f0cccd337bb50c14a8d744dbe89cc8079eab4e643be4bad723c8096b1d`. This is a local image identifier, not a published registry digest. The local Telegram Bot API image/server version was not verified. Cloud Bot API protocol behavior was tested as listed below; its deployed server version is not reported by the API.

The original slim/Debian package loaded SQLite 3.46.1. Inspection of Debian source package `3.46.1-7+deb13u2` found no WAL-reset fix in its patch series. The new multistage build uses the [official SQLite 3.53.4 archive](https://www.sqlite.org/2026/sqlite-autoconf-3530400.tar.gz), SHA-256 `0e9483900e92cd5de8fd48d16bf9200145a61f7fd5be542a5ac81d8a9516eb9c`. The build asserts the download checksum and the library actually loaded by Python; the existing image runtime gate asserts that version too. See the [upstream WAL advisory](https://www.sqlite.org/wal.html) and [3.53.4 release notes](https://www.sqlite.org/releaselog/3_53_4.html). This identifies exposure in the old runtime, not evidence of observed production corruption.

The developer environment was not overwritten. Local tests also ran with an isolated pinned yt-dlp import directory. The complete suite then ran inside the built image with hashed development dependencies installed only in the disposable container, confirming compatibility with the fixed SQLite library.

## Verification results

- Final local suite after the live-test correction: 1,144 passed, branch coverage 78%. Final disposable-container suite on Python 3.14.8 / SQLite 3.53.4: 1,144 passed, branch coverage 78%. Eight warnings in each suite are PTB deprecation notices for numeric `RetryAfter.retry_after`. No test failures or skipped HLS acceptance cases.
- Ruff and whitespace checks pass.
- HLS tests use actual FFmpeg, localhost HTTP, and pinned yt-dlp API/CLI. The pinned parallel API abort can raise a narrowly checked `ValueError: write to closed file` instead of `DownloadError`; both cases reject output. Arbitrary `ValueError` is not classified as a proven media failure.
- Migration tests verify a consistent backup and its integrity, legacy read/write compatibility, unverified-row quarantine, and transactional rollback on injected migration failure. A separate local restoration probe copied the pre-migration snapshot into a new database and verified integrity, original records, and the original unique-file constraint using the legacy reader. Production database restoration remains an operator procedure documented in the [cache guide](../guides/cache-and-delivery.md).
- WebUI tests cover disabled default, persistence, valid period, invalid atomic updates, EN/RU content, and CSRF rejection. Initial desktop/mobile browser inspection and saving a policy succeeded on isolated data. A confirmation pass after small input/spacing CSS adjustments was blocked by the browser tool's navigation policy on its generated connection-error page, although the restarted local endpoint returned HTTP 200. Final visual confirmation remains unverified.
- A scoped UI detector found existing gradient text and a width animation outside the new cache controls. No unrelated redesign or suppression rule was introduced.

## Review corrections

Review added regressions and fixed uncertainty retention when an awaiting RPC task is cancelled, separate workers for optional cache contention, policy rereading after a queued cleanup lock, shared subprocess deadlines, cooperative cancellation of an abandoned preparation worker before temporary-file cleanup, actual timedelta duration conversion, dispatch journaling for description parts, and clearing a stale-hit marker before fresh preparation, preserving parameter order and raw encoding on unknown signed source URLs, and recognizing the real cloud API invalid-reference response described below. Review did not authorize a deployment.

## Controlled cloud Telegram acceptance

An explicitly authorized bot and private test chat were used through the built image. Credentials and raw message/file identifiers remained in a private temporary directory outside the checkout and were not copied into this document. No production bot configuration, webhook, polling process, or other chat was changed. The host DNS resolved the API to an unreachable service address; only the isolated test container used a current public API address independently confirmed by two DNS resolvers, with normal hostname/TLS verification. Host network configuration was not modified.

The fixture uses locally generated 480x320 JPEGs, a two-second H.264/AAC MP4, a two-second MP3, and a plain-text document. It invokes the actual application delivery/cache/journal helpers with real PTB `Message` objects and Bot API responses; it does not fabricate user callbacks or receive real user feedback messages.

| Cases | Observed result |
| --- | --- |
| Fresh and cached photo/video/audio/document (8 cases) | Actual returned kind matched the chosen method. Cached sends retained the original `file_unique_id`; photo/video dimensions remained 480x320. Actual responses supplied confirmed message IDs and journal evidence. |
| Fresh and cached photo albums of 2, 10 and 11 items (6 cases) | Ordered artifact identities matched across fresh and cached delivery. The 11-item album split correctly and included separate related audio. Its cached description path completed without failure. |
| Fresh and cached mixed photo/video album (2 cases) | The returned order was photo then video in both paths; cached artifact identities matched. |
| Invalid cached file reference (1 case) | Telegram returned `Wrong remote file identifier specified: wrong padding in the string`. The first live run exposed a missing narrow signature. After adding a regression and the signature, the cache helper returned a miss and invalidated the unusable manifest while retaining its alias. The refusal did not produce media. |
| Cancellation before request and after accepted response (2 cases) | Pre-request cancellation made zero sends. Cancellation flagged after the real RPC response preserved one confirmed photo and its journal record. |
| Pool-timeout fixture followed by a real send (1 case) | An injected pool-acquisition timeout led to two application attempts and only one actual audio request. This is controlled backend-cause injection, not observed real pool exhaustion. |
| Lost-response fixture after real acceptance (1 case) | The actual document RPC succeeded, then the fixture raised a read timeout before returning the result to the application. The application retained unknown, made only one request, and did not resend. A fresh isolated process reopened the journal, preserved unknown, and reported SQLite integrity `ok`. This simulates response loss; it does not claim a measured network outage. |

All 21 listed cases passed across two isolated runs. The first run completed 16 success cases and then stopped at the invalid-reference classifier gap; those already accepted albums were not replayed when continuing the remaining five cases after the fix. Real accepted media totaled 63 items, including the document observed by the response-loss fixture but deliberately unknown to application state. Service/status and description messages are additional. This counts Telegram API evidence, not human visual approval of every message.

## Remaining gates and conditional work

1. Cloud delivery helpers now have the above real API evidence. Local Bot API uploads/2 GB boundaries, public URL handoff, full incoming URL/downloader flows, actual user callback/feedback/CSI interactions, and desktop/mobile client rendering still require their corresponding controlled acceptance. The provided bot token alone does not establish the local Bot API runtime or client UI behavior.
2. Remote CI, image vulnerability scan, publishing, deployment, monitoring comparison, and release rollback rehearsal were not run by this local acceptance. The local image build/runtime check and container tests do not substitute for those gates.
3. A12 shared preparation remains conditional on measured duplicate work. No cross-recipient singleflight registry or shared temporary-file ownership was added. Production extraction/upload latency, duplicate-preparation rates, cache hit/miss causes, and event-loop/database waits still need measurements before choosing this optimization. The existing strict source check remains in front of reuse.
4. The journal preserves uncertainty across restart but does not eliminate the window between external acceptance and local response commit. No exactly-once delivery claim or automatic session replay is made. Unknown operations are retained rather than aged out.
5. Legacy title-search/admin diagnostic APIs report legacy rows; current normalized artifact counters are in the WebUI. Partial artifact storage is durable but is not yet a missing-item repair/resumption feature.

Historical fixtures in `audits/fixtures` retain their original bug assertions and benchmark results. They are not implementation acceptance tests and must not be interpreted as a passing release gate.
