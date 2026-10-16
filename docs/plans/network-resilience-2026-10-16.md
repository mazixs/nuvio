# Network resilience plan

Status: PR A implemented; later stages planned. Evidence: [network outage of 2026-10-15](../audits/network-outage-2026-10-15.md).

## Goals

- The bot recovers by itself after a long outage.
- While the network is down, the bot does not start a cascade of retries.
- Administrators learn about the outage.

The outage of 2026-10-15 was on the provider's side. Hypotheses that the bot overloaded the network, rate limiting, SQM, and conntrack measurements were dropped.

## Order

0 → A → B → C → D → F → documentation. Stage E is not needed: TDLib reconnected by itself in both experiments.

## Stage 0: experiments and logs

- [x] Experiment 1: bot and Bot API without internet for 20 minutes.
- [x] Experiment 2: Bot API without internet; does a waiting request get its real response?
- [ ] Keep Bot API logs on a volume so they survive a recreated container: log file and verbosity flags plus rotation. Update `tests/test_compose_configuration.py`.

## PR A: correct classification and one retry layer

- [x] Classify by exception type first, with text markers as a fallback for download errors and CLI stderr. Walk `__cause__`, yt-dlp `.cause`, `DownloadError.exc_info`, and `__context__` unless suppressed. Markers are matched only in `error:` lines, and an `OSError` only by type, so a video title in a path or a `Destination` line cannot match. Implemented as `is_connectivity_failure()` in `utils/public_errors.py`.
- [x] Classify `socket.gaierror` as `NETWORK`, not `IO`.
- [x] For DNS, unreachable network or host, and refused connections, keep only yt-dlp's own retries: no `execute_with_backoff`, cookie attempt, non-HLS selector, or CLI. The first failing YouTube CLI run raises its own error with the original as `__cause__`, so the user sees the network cause. `NETWORK_TIMEOUT` keeps CLI, which recovered downloads during the 2026-08-18 incident.
- [x] Full jitter in `execute_with_backoff`.
- [x] Run the canary through `work_budget.execute` with its `session_id`, so the deadline and cancellation reach CLI.
- [x] Offline tests: chained `gaierror` → `NETWORK`; no CLI on DNS failure; CLI still used on timeout; canary timeout stops its CLI process.

## PR B: liveness that does not depend on the internet

- [ ] Heartbeat from polling. A `HTTPXRequest` subclass passed through `ApplicationBuilder.get_updates_request(...)` records two timestamps: any `getUpdates` outcome (the bot's loop is alive) and any HTTP response (the local Bot API is alive). The Bot API is local, so the check does not depend on the provider and cannot cause a restart loop during a long outage.
  PTB rejects `get_updates_request` together with `get_updates_*_timeout` in the builder; move those timeouts into the `HTTPXRequest` constructor and update `tests/test_main_runtime_paths.py`.
- [ ] Exit on a hang with `faulthandler.dump_traceback_later(timeout, exit=True)`, re-armed on each heartbeat, threshold 10–15 minutes. It does not take the logging lock and dumps all thread stacks before exiting; Docker restarts the process. A custom thread calling `os._exit` was rejected because it can hang on the logging lock.
- [ ] `bot` healthcheck in `compose.yaml` based on a heartbeat file under `/tmp` in the container, with a Python one-liner like the `web` check. It provides visibility in `docker ps`; it restarts nothing.
- [ ] `health.json` in `DATA_DIR`: last Bot API response and polling error counters. Log one line every N minutes with the age of the last Bot API response, which compensates for PTB's silence.
- [ ] Log the reason for the previous exit at startup (watchdog, SIGTERM, or crash) from a marker in `DATA_DIR`.
- [ ] `stop_grace_period: 60s` for `bot`, so SIGTERM does not cut off a file upload.
- [ ] End-to-end Telegram probe: `getChat(<first ADMIN_ID>)` with a timeout of about 10 s every 2–5 minutes. A timeout opens an outage summary (PR D); it does not restart anything. Do not use `getMe`, which is served from cache.
- [ ] Tests with a fake clock and a fake `dump_traceback_later`, without a real exit or network.

## PR C: "no DNS" and "no egress" states

- [ ] Two states instead of one flag: `dns_down` from a resolution probe, and `egress_down` from TCP to two or three IP addresses such as `1.1.1.1:443` and `8.8.8.8:443`. Hostname probes (`youtube.com`, `api.telegram.org`) confuse "no internet" with "platform unavailable"; TDLib connects to Telegram data centres by IP over MTProto.
- [ ] Only the event loop changes state. Worker threads report events through `loop.call_soon_threadsafe`; at most one probe runs at a time; threads read an immutable snapshot under `threading.Lock`. Probes block in `getaddrinfo`, so they run in the executor with a hard socket timeout rather than `wait_for`.
- [ ] Hysteresis: offline after two consecutive failures, online after two successes. Probe interval grows exponentially from 15 s to 5 minutes with jitter.
- [ ] While offline: answer new links immediately with "the server network is unavailable" and a `<PREFIX>-NETWORK-<RANDOM>` code (text in `messages.py`); skip CSI surveys and the canary.
- [ ] Tests: hysteresis, the thread-reports/loop-changes race, and a probe timeout.

## PR D: administrators learn about the outage

- [ ] One persistent summary instead of an in-memory queue, which would not survive a watchdog exit. Write the outage start and an unsent canary result atomically, like `save_canary_status`. After recovery send **one** message "the network was unavailable from … to …", at most once per N minutes.
- [ ] Send the summary only after a successful `getChat` probe from PR B.
- [ ] Retry only when non-delivery is proven (`PoolTimeout`, refused connection to the local Bot API). Treat `TimedOut` after the request was sent as "sent, unconfirmed" and do not retry: TDLib delivers it by itself. An earlier draft retried six times per hour, which would have produced up to six identical alerts after recovery.
- [ ] Rerun the canary after recovery if it failed for a network reason, instead of waiting for the next interval.
- [ ] The summary replaces crash reports for DNS failures, which PR A removed.

## PR F: wait for the delivery result instead of guessing

Current behaviour: `read_timeout` is 120 s for messages and 1,800 s for files in `main.py`. A timeout marks the attempt `unknown` in `utils/delivery_journal.py`, and nothing reconciles it later. The user sees `DELIVERY_OUTCOME_UNKNOWN`; the canary records an error; a broadcast counts the message as failed. The Bot API offers no way to look the message up later, so the response to the original HTTP request is the only source of truth. Experiment 2 confirmed that the local Bot API eventually returns it.

- [ ] Background sends (outage summary, canary, broadcast) use a separate `HTTPXRequest` with a `read_timeout` measured in hours, so long requests do not occupy the shared pool. Record "delivered at HH:MM" from the response's `message_id` and `date`.
- [ ] User delivery: the interface waits as now (120 s), then changes the status to "Telegram is unavailable, the upload is waiting for a connection". The request keeps waiting in a background task (`context.application.create_task`). A `Message` marks the journal `delivered`, stores the `file_id`, and updates the status; an error marks it `refused` and allows a retry.
- [ ] Limit: a process restart loses the response, and the record stays `unknown` as today. The PR B watchdog watches polling, not sends, so it must not fire while such requests wait.
- [ ] Tests: a fake Bot API that answers late; the `unknown` → `delivered` transition; no second send; a response that arrives after the interface stopped waiting.

## Stage E: not needed

If TDLib ever stops reconnecting by itself, a host systemd timer could run `docker compose restart telegram-bot-api` when `health.json` shows "Bot API alive, no updates from Telegram" while egress works, at most once per 15 minutes. A PID 1 wrapper in the Bot API image (risky for SIGTERM and TDLib database shutdown) and autoheal with the Docker socket (root on the host) were rejected.

## Documentation

- [ ] Runbook: outage symptoms, `health.json`, the experiment procedure, and a fallback resolver through `dns:` in Compose as an operator option, not a default.
- [ ] `CLAUDE.md`, "Behavior that must remain intact": PTB is silent on polling `TimedOut`; a `TimedOut` send through the local Bot API is not proof of non-delivery; liveness is checked against the local Bot API, not the internet.

## Deferred

- **Interrupted downloads after a restart.** Sessions in `user_data` are lost, and the user's "downloading" status stays forever. A minimal `(chat_id, message_id)` registry in `DATA_DIR` would let startup edit those messages to "the bot restarted, send the link again". Keep the `unknown` mark in `delivery_journal.recover`. This matters more once the watchdog exists. On 2026-10-15 a SIGTERM during CLI fallback left such a session folder in `shared-media`.
- **Administrator actions are missing from `bot.log`.** `/admin` and broadcasts write only to `analytics.db`; the broadcast time on 2026-10-15 had to be recovered from the database. One `INFO` line at the start and end of a broadcast would suffice.
- **Broadcasts count `TimedOut` as a failure**, although the message is probably delivered later. Report "unconfirmed" separately.
- **Canary at 23:04 on 2026-10-15:** a 403, then the non-HLS fallback reported "final file not found", and only the cookie attempt succeeded. Probably the file path after the fallback; investigate separately.

## Rejected

| Idea | Reason |
|---|---|
| Traffic measurements, conntrack, SQM, `tc` | The outage was on the provider's side |
| `YTDLP_RATE_LIMIT`, fragment concurrency limits, download slots | They do not address recovery |
| Custom watchdog thread with `os._exit` | `faulthandler` is more reliable |
| Hostname probes | Confuse "no network" with "platform unavailable" |
| In-memory alert queue | Does not survive a restart |
| "8.5 hours of silence means a hang" | Production traffic is low; silence is normal |

## Constraints

- Comments, docstrings, and bot texts stay in Russian; user texts live in `messages.py`.
- Blocking work goes through `run_blocking(..., session_id=...)`; long background work through `context.application.create_task`.
- The YouTube order "without cookies → cookies → CLI" stays for non-connectivity causes; yt-dlp warnings are not suppressed.
- Error codes follow `PREFIX-CATEGORY-RANDOM`; unconfirmed causes use `UNEXPECT`.
- Tests run without network; `ruff check` is clean; imports in `main.py` stay below `load_dotenv()`; Compose and `.env.example` changes update their contract tests.
