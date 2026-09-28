# yt-dlp Audit, Updates, and Diagnostics

Date: 2026-09-23.
Area: Installation of yt-dlp and JS components, update lifecycle, YouTube canary, diagnostics, and current incident errors.

## Summary

The crash report indicates that the direct cause of the failure was a Telegram menu being rejected due to Markdown, not a crash of yt-dlp itself. This does not negate two independent operational issues: the Docker image lacks a supported JavaScript runtime, and automatic package updates occur after the yt-dlp code is imported, so they do not guarantee that the current process will start using the new version.

The version reported in the crash log - `2026.8.18.122307.dev0` - is behind the published nightly build `2026.09.16.232951`, available as of the audit date. The base image reproducibly installs an outdated, pinned version from `requirements.txt`. Enabling `YTDLP_AUTO_UPDATE` temporarily changes the container but does not modify the original lock file or image.

Proposed model for Nuvio: yt-dlp version and runtime are part of an immutable Docker image; scheduled CI automatically prepares a testable version update; a canary monitors actual loading and alerts on issues. Do not update the package via pip within an already-running process. Preparing a PR, publishing the image, and updating the container on the server are three distinct steps.

## Basis of Conclusions and Solution Status

| Item | Basis | Status |
|-----|------|--------|
| Escaping text via `telegram.helpers.escape_markdown(version=1)` | Standard Python-Telegram-Bot API for Markdown v1; [version 22.8 documentation](https://docs.python-telegram-bot.org/en/v22.8/telegram.helpers.html#telegram.helpers.escape_markdown) | Changed in working copy; actual menu delivery not verified |
| Deno and `yt-dlp-ejs` for YouTube | [Official yt-dlp documentation](https://github.com/yt-dlp/yt-dlp/wiki/EJS) requires a JS runtime and EJS scripts, recommends Deno; `yt-dlp[default]` includes EJS | EJS is pinned in the lock file; Deno is not included in the image |
| Loading EJS from GitHub | Official documentation describes `ejs:github`; in Nuvio, this option is enabled in the API and CLI | Configuration fact; removing the option after local EJS validation is a proposed action |
| Update via pin, verified image, and PR | Conclusion from this repository's device: lock file, import before pip update, release by tag, manual server update | Proposed architecture; not a requirement for yt-dlp; not implemented |
| P0/P1/P2 priorities, canary, `/admin`, status format | Risk assessment and solution options for Nuvio | Proposals requiring implementation and verification |

The conclusions about current behavior are based on code review and the provided crash report. Priorities and plans are engineering assessments and do not constitute confirmation of a finalized solution. The production configuration, working image, and actual file delivery were not verified by this audit.

## Identified Issues

### P0 – Auto-update does not restart already-imported yt-dlp

`main.py` imports `utils.canary` before calling `ensure_latest_yt_dlp`; `utils.canary` at the module level imports `utils.youtube_utils`, which immediately imports `yt_dlp`. Then, `pip install --upgrade` modifies files on disk, but Python continues to use already-loaded modules. The same occurs when updating from the canary in a running process. Logs may show a new version from package metadata, yet the loaded code remains unchanged. Additionally, pip may alter transitive dependencies already used by other handlers.
Proof: [main.py](../../main.py#L43), [utils/canary.py](../../utils/canary.py#L50), [utils/youtube_utils.py](../../utils/youtube_utils.py#L10), [utils/ytdlp_runtime.py](../../utils/ytdlp_runtime.py#L86).

### P0 - The image does not contain a JavaScript runtime for YouTube

The dependency `yt-dlp[default]` installs the `yt-dlp-ejs` package, but the Dockerfile does not install Deno, Node, or any other supported runtime. The crash report directly contains a warning about the missing runtime, and the solution to the challenge failed. According to the current yt-dlp documentation, Deno 2.3+ is recommended as the runtime, and the EJS component is required.
Proof: [Dockerfile](../../Dockerfile#L1), [requirements.in](../../requirements.in#L3), attached crash report. Runtime requirements: [yt-dlp EJS setup](https://github.com/yt-dlp/yt-dlp/wiki/EJS).

### P0 - Telegram Markdown can break the menu before loading starts

In the incident, the menu handler collected the menu and passed it to `edit_text(parse_mode="Markdown")`; Telegram returned `Can't parse entities`. The general `except Exception` classified the rendering error as `YT-UNKNOWN` and displayed the error instead of the buttons. As a result, the user could not start the download. In the working copy, the header escaping has already been replaced with a helper Telegram function; however, the change has not yet been verified or tested with a real menu.
Proof: [utils/telegram_utils.py](../../utils/telegram_utils.py#L1682) and the general error handler below. The header escaping change is currently in an uncommitted state.

### P1 - Automatic updates are disabled by default, and their results are hard to interpret

`YTDLP_AUTO_UPDATE=false` is set in the environment template and is the default setting. If updates are disabled, `ensure_latest_yt_dlp()` returns `attempted=False` and simultaneously `succeeded=True`; this may appear as a successful check, even though the new version was not actually verified. When enabled, every run triggers pip regardless of whether a new version is available. The code considers a zero exit code from pip as success and does not re-check YouTube functionality.
Proof: [config.py](../../config.py#L161), [.env.example](../../.env.example#L68), [utils/ytdlp_runtime.py](../../utils/ytdlp_runtime.py#L76).

### P1 - Channel switching does not guarantee transition to the selected release

Channels describe not a fixed version, but arguments for `pip install --upgrade`. When switching from a newer nightly to an older stable version without explicitly specifying a version number, the command may leave the installed nightly version and finish successfully. The `master` channel uses a moving GitHub archive with `--force-reinstall`; the result cannot be reproduced from a single value of `YTDLP_RELEASE_CHANNEL`. None of the modes verify the actual release against the selected policy after installation.
Proof: [utils/ytdlp_runtime.py](../../utils/ytdlp_runtime.py#L44) and the pip exit code check below only.

### P1 - Container updates are temporary and not reproducible

The Docker image takes a pinned version from the lock file. Installing via pip changes the container layer during its lifetime; recreating the container returns the pinned version. The project already has a manual procedure for updating the pin, but the version is also repeated in the test and documentation. There is no single command that updates the input pin, recalculates hashes, and brings related checks into a consistent state.
Proof: [requirements.in](../../requirements.in#L3), [Dockerfile](../../Dockerfile#L25), [youtube-download-runbook.md](../../docs/technical/youtube-download-runbook.md#L203).

### P1 - the path from PR to a running server is missing in the update mechanism

CI on push checks the code and image, but does not publish the new image. The release workflow publishes an image only for tags starting with `v*`. Even a successful release does not automatically update the container on the server: Compose uses a locally available image until the operator manually runs a `pull` and `recreate`. Therefore, a scheduled PR from the plan will generate an automatic update suggestion, not automatic live bot updates.
**Evidence**: [.github/workflows/ci.yml](../../.github/workflows/ci.yml#L1), [.github/workflows/release.yml](../../.github/workflows/release.yml#L1), [compose.yaml](../../compose.yaml#L40), and the manual step on the server in [youtube-download-runbook.md](../../docs/technical/youtube-download-runbook.md#L224).

### P1 - the release checks the WebUI but not the bot's readiness

The `web` service has a `/health` endpoint and a Compose healthcheck; the `bot` service lacks a healthcheck. CI and release smoke tests only launch `python -m web` from the image and verify the WebUI response. Thus, a successful image publication confirms WebUI startup but does not verify polling, Deno availability, or actual bot downloads. Automatic delivery of a new image without an independent readiness signal for the bot relies on an incorrect success criterion.
**Evidence**: [compose.yaml](../../compose.yaml#L40), [compose.yaml](../../compose.yaml#L86), [.github/workflows/ci.yml](../../.github/workflows/ci.yml#L124), [.github/workflows/release.yml](../../.github/workflows/release.yml#L135).

### P1 - automatic problem detection signal is disabled by default

The canary downloads a reference video through production flow, bypassing the cache - this is a useful validation, not just a metadata check. However, `CANARY_ENABLED=false` by default, so the bot will not detect regressions automatically without explicit configuration. When a failure occurs, the canary performs a forced pip update and retries, but the retry runs under the same process with already loaded yt-dlp. The daily update limit is stored only in memory and resets on restart.
**Evidence**: [config.py](../../config.py#L169), [utils/canary.py](../../utils/canary.py#L245).

### P1 - the canary may skip a failure or terminate without notification

The canary selects a format with a 50 MB budget and calls `download_video()`. However, if the primary attempt fails, the same downloader may switch to another format without that budget and then fall back to CLI. The CLI fallback does not receive `http_chunk_size=10 MB`, which is specified in the API download. The August incident specifically depended on the Range size, so a successful CLI fallback does not prove the primary API path is healthy. Moreover, a fallback format without size limits could sharply increase traffic and the size of the check file.
`get_available_formats()` and format selection are outside error handlers in `run_youtube_canary_check()`. If either raises an exception, `_check_in_thread()` only catches timeouts, and `youtube_canary_job()` terminates before notifying administrators. On timeout, thread waiting stops, but the thread itself may continue running; the next pip update and retry may coincide with it in time.
**Evidence**: [utils/canary.py](../../utils/canary.py#L123), [utils/youtube_utils.py](../../utils/youtube_utils.py#L400), [utils/youtube_utils.py](../../utils/youtube_utils.py#L296), [utils/ytdlp_common.py](../../utils/ytdlp_common.py#L19). The difference between CLI and API is already noted in [youtube-download-runbook.md](../../docs/technical/youtube-download-runbook.md#L108).

### P1 - report tail mixes metadata from different sessions

The metadata analysis is called without `session_id`, and its entire output is written under a shared key. Then, `output_tail(session_id)` appends this shared tail to the session-specific tail. During parallel requests, warnings about other videos and platforms appear in the report. In the attached report, alongside Shorts, there are entries for TikTok, Instagram, and a public YouTube video used for cookie validation. Based on this tail, it's not possible to confidently assign each warning to a specific Shorts issue.

Proof: [utils/youtube_utils.py](../../utils/youtube_utils.py#L72), [utils/ytdlp_common.py](../../utils/ytdlp_common.py#L149), [utils/download_report.py](../../utils/download_report.py#L60), [utils/cookie_health.py](../../utils/cookie_health.py#L51).

### P1 - Stuck metadata parsing may consume a thread after user timeout

For YouTube, `process_url()` already has a `session_id`, but calls `run_blocking(get_video_info, url)` without it. In `get_video_info()`, network options are applied without `session_id`, so no cancellation hook is set. `asyncio.wait_for()` terminates waiting after `BLOCKING_TASK_TIMEOUT`, but does not stop the yt-dlp thread. A series of stuck parses can exhaust the download pool, even if users have already received timeout errors.

Proof: [utils/telegram_utils.py](../../utils/telegram_utils.py#L1673), [utils/telegram_utils.py](../../utils/telegram_utils.py#L361), [utils/youtube_utils.py](../../utils/youtube_utils.py#L72), [utils/ytdlp_common.py](../../utils/ytdlp_common.py#L149).

### P1 - Two EJS mechanisms allow unexpected network sources

`yt-dlp-ejs` is already locked in the lockfile, but API options and CLI fallbacks additionally allow `ejs:github`. If the local EJS is missing or no longer compatible with yt-dlp versions, the application may download scripts from GitHub during URL processing. This compromises reproducibility, availability, and diagnostics: results depend on access to GitHub and remote releases. The fallback should be removed after confirming that compatible local scripts are present in the image.

Proof: [requirements.txt](../../requirements.txt#L1194), [utils/ytdlp_common.py](../../utils/ytdlp_common.py#L29), [utils/youtube_utils.py](../../utils/youtube_utils.py#L323). Value of `ejs:github`: [yt-dlp EJS setup](https://github.com/yt-dlp/yt-dlp/wiki/EJS).

### P1 - `/admin` and crash report show incomplete update status

The crash report includes the installed version and channel, but does not show whether the updater is enabled, when the last attempt was made, how it ended, whether a JS runtime is present, or whether a canary has passed after restart. Therefore, from a single report, it's impossible to distinguish between a current package without runtime and a failed installation or a successful update that hasn't yet taken effect.

Proof: report field collection in [utils/telegram_utils.py](../../utils/telegram_utils.py#L281); runtime version is logged at startup in [main.py](../../main.py#L344).

### P1 - Notification delivery failure is not visible to administrators

The canary and crash report catch errors in sending notifications to administrators and log them only at DEBUG level. If Telegram is unavailable or sending is blocked, an operator may not receive a signal about a YouTube failure, and standard `LOG_LEVEL=INFO` will not reveal the reason for delivery failure. A visible status of the last notification attempt and logging of the error at operational log level are needed; additionally, a separate notification channel can be selected.
Proof: [utils/canary.py](../../utils/canary.py#L304), [utils/telegram_utils.py](../../utils/telegram_utils.py#L321), [.env.example](../../.env.example#L39).

### P2 - Updates introduce unnecessary startup delay and modify dependencies on the fly

When the setting is enabled, every startup waits for pip with a timeout of up to 240 seconds. Updates from canary can occur during user uploads. pip modifies the package code on disk while the application continues running; the update result is not isolated and is not activated in a coordinated manner.
Proof: [utils/ytdlp_runtime.py](../../utils/ytdlp_runtime.py#L94), [utils/canary.py](../../utils/canary.py#L247).

### P2 - Current automated checks do not enforce the update contract

A build test command for stable/nightly/master channels was found, but no full behavior checks for the updater were detected: disabled state, pip error, timeout, version difference before/after, restart/update application, and presence of JS runtime in the final image.
Proof: The only direct coverage of the builder is in [test_audit_regressions.py](../../tests/test_audit_regressions.py#L1899); CI builds a Docker image, but the described job lacks verification that the runtime is actually available and resolves the JS challenge.

### P2 - Cookie diagnostics may exacerbate widespread failures

On every YouTube, TikTok, or Instagram error, the background report triggers `check_cookie_health()`. The result is cached for 10 minutes, but before a record appears, parallel errors may trigger multiple identical network probes. For YouTube, the probe accesses a separate video via yt-dlp and may take up to network timeouts, adding load precisely during a widespread platform outage. The `probe_failed` entry in the report refers to this additional check and does not establish the cause of the user-level failure.
Proof: [utils/telegram_utils.py](../../utils/telegram_utils.py#L1062), [utils/cookie_health.py](../../utils/cookie_health.py#L22), [utils/cookie_health.py](../../utils/cookie_health.py#L274).

### P2 - Runbook contradicts current routing of `MEDIA_FORBIDDEN`

The runbook states twice that admins should not receive crash reports when `MEDIA_FORBIDDEN` occurs. The function `_should_notify_admins_platform_failure()` returns `True` for this category if the stage is not a timeout. This creates incorrect expectations during investigations of widespread 403 errors. Before correcting the documentation, the actual message format must be verified and agreement on the required notification frequency must be reached.
Proof: [youtube-download-runbook.md](../../docs/technical/youtube-download-runbook.md#L80), [utils/telegram_utils.py](../../utils/telegram_utils.py#L1035).

## Fix Plan

### Stage 0 - Freeze the initial state

1. Record production settings without secrets: digest and tag of the running image, version of yt-dlp from the package and loaded module, Deno/EJS, `YTDLP_AUTO_UPDATE`, `CANARY_ENABLED`, time of the last successful load.
2. Capture at least baseline metrics before changes: percentage of successful YouTube loads, delay from link to menu and to first byte, full download duration, number of errors by category, loader pool utilization, canary time and traffic. If no ready metrics exist, collect a short reproducible measurement and do not present it as long-term statistics.
3. Reproduce the original menu failure on the problematic Shorts and separately verify a long control video without cache. Save the current image digest as a rollback point.
**Ready when:** working versions, configurations, and initial results of two custom scenarios are known; a rollback image is selected.

### Step 1 - Stop current menu and YouTube failures

1. Confirm that Telegram Markdown headers are properly escaped using special characters, Unicode, backslashes, and empty fields. If Telegram still rejects the formatting, re-display the menu in plain text with the same keyboard. Classify Telegram rejections separately from YouTube errors. Add regression tests for this scenario.

2. Install a pinned Deno version in the Docker image, keep `yt-dlp[default]` aligned with the EJS version, and ensure the runtime is accessible from PATH for unprivileged processes.

3. Align the required minimum with the supported Deno version from upstream documentation and pin the version/SHA of the installation source in the Dockerfile.

4. Add CI checks for the built image: yt-dlp and EJS versions, Deno availability, successful resolution of the JS challenge, and presence of expected formats on a reference YouTube video. If network smoke tests are unstable for each PR, split component offline checks from regular network validation.

5. Pass `session_id` via metadata parsing and cookie diagnostics to ensure strings from different users do not appear in the same report. Explicitly label auxiliary test runs.

6. Make all canary stages exception-safe. Limit both budget and format for fallbacks, verify the Range size on the path the canary reports as successful. After timeout, wait for either completion or safe termination of the stream before retrying.

7. Pass `session_id` through the blocking YouTube parsing and ensure cancellation after timeout; verify that the connection pool exhaustion does not occur when a series of stalled links are encountered.

**Ready when:** Telegram accepts the menu with problematic characters; the container passes runtime and EJS diagnostics; smoke test of the reference video confirms full YouTube extractor functionality; the canary notifies the administrator of any stage failure and does not exceed its budget.

### Step 2 - Make updates simple and reproducible

1. Remove pip updates from the running process. Consider the Docker image as an immutable release unit.

2. Add one command to update yt-dlp that updates the pin in `requirements.in`, regenerates the lock file with hashes, updates all version references, runs checks, and shows a diff. The script must specify a concrete version; changing the channel without a version is not a rollback.

3. Add a scheduled workflow that periodically proposes an update to the latest nightly (or stable, based on selected policy) via a separate PR. The PR must pass CI, build the image, and run YouTube smoke tests before merging. This is automated preparation, not server deployment.

4. Document separate commands for release publication and container update on the server, including digest verification, canary status checks, and rollback to the previous image. If a fully automated production rollout is needed, run these steps only after a verified build with health checks and automatic rollback.

5. Simplify channels to only those actually supported by the product; `master` requires a separate risk and reproducibility policy since the original archive URL does not guarantee an immutable artifact.

6. After local verification of `yt-dlp-ejs`, remove the `ejs:github` permission from the API and CLI, or explicitly treat it as a separate emergency mode with visible status.

7. Fix the versioning policy: when to choose stable or nightly, how often to propose updates, what constitutes an emergency release, and when to revert to a temporary nightly and return to stable. For each release, store both the previous and new digest.

8. Add a separate check for service startup and readiness in CI and after delivery. The `/health` response from the WebUI should not terminate YouTube or polling checks. After an update, compare metrics from stage 0; if the check fails, revert to the previous digest.

**Ready when:** updates can be opened with a single command or scheduled PR; clean builds install exactly the version shown in the report; rollbacks revert to the previous pin/image.

### Stage 3 - Make the state visible to the owner

1. Introduce a unified local status: image version and digest, yt-dlp version from the package and from the loaded module, EJS and Deno versions, date and result of the last canary run. Track the preparation status of a new image separately in CI and in the release.

2. Display the local status in `/admin` and include a snapshot in crash reports; distinguish between missing runtime, non-working extractor, unknown canary status, and healthy load.

3. Perform a brief local diagnostic of installed components upon startup. Leave network checks to the canary to avoid slowing down every startup.

4. Configure the canary with an interval matching traffic budget and anti-bot risk, and send the administrator a clear status: last check, version, and failure stage.

5. Remove the promise that `force=True` fixes the current process. The canary must diagnose and report; delivery of a new build can only be initiated as a separate, controlled process.

6. Preserve the last canary result and last notification attempt between restarts so `/admin` does not show empty status after container recreation. Log delivery errors at WARNING or ERROR level and display them in the status.

**Ready when:** the administrator sees on one screen a running image, the actually loaded version, runtime status, and the result of the last load; the status of a new build is available in CI/release.

### Stage 4 - Cover errors in updater and release path

1. Add checks for update commands for pin/lock: version matching, hashes, dependency resolution errors, and prevent redundant restarts without unnecessary diff.
2. Add checks to ensure that simultaneously used versions of yt-dlp and `yt-dlp-ejs` are compatible.
3. Build a clean image and verify it after update; do not consider a green unit suite as proof of YouTube functionality.
4. Add an acceptance test: URL → menu → selection → actual file download → temporary file cleanup; separately verify unavailability of the video, truncated formats, expired cookies, Telegram BadRequest, and cancellation.
5. Replace manual documentation for `YTDLP_AUTO_UPDATE=true` with the standard update path via image rebuild after in-place updater removal.
6. Resolve the inconsistency between the runbook and code regarding `MEDIA_FORBIDDEN` notifications; lock in the selected behavior through verification.
7. Limit parallel cookie attempts to one request per platform and maintain distinction between user operation errors and results from auxiliary probing.

## What is not confirmed by this audit

- Secrets, actual production environment variables, and live container logs were not checked. Therefore, it cannot be concluded whether `YTDLP_AUTO_UPDATE` was enabled or why the instance remained on the version from August 18.
- A successful real request to a control video in production was not verified. The provided report only shows that runtime was missing in the yt-dlp context and that the Telegram menu was not accepted.
- Tests, image build, and network smoke tests were not run during the audit. The current Markdown change has not been verified yet.

## Sources

- [yt-dlp EJS: runtime and scripts](https://github.com/yt-dlp/yt-dlp/wiki/EJS)
- [Official nightly releases](https://github.com/yt-dlp/yt-dlp-nightly-builds/releases) - as of audit date, the latest found nightly: `2026.09.16.232951`.
- Code and documentation mentioned in the above sections.
