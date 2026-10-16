# Network outage of 2026-10-15 and local Bot API experiments

Status: findings recorded 2026-10-16. Times are UTC. Host addresses, chat and message IDs, and production settings are omitted.

The fixes are tracked in the [network resilience plan](../plans/network-resilience-2026-10-16.md).

## Summary

On 2026-10-15 the internet provider's DNS failed for about nine hours. The bot was not the cause. Two controlled experiments on the production stack on 2026-10-16 showed that the Telegram side recovers by itself. The real defects were elsewhere:

- The bot does not notice an outage and does not report it.
- A DNS failure was classified as `DOWNLOAD`, so every request walked the whole fallback path against the same resolver.
- The canary's worker thread outlived its timeout.
- A Telegram `Timed out` through the local Bot API does not mean "not delivered".

## Incident timeline

| Time (UTC) | Event |
|---|---|
| 13:58 | `dockerd` starts logging `[resolver] failed to query external DNS server ... i/o timeout`. The host and Docker share one upstream resolver; `daemon.json` sets none. |
| 14:16:26 | The scheduled canary starts. |
| 14:26:26 | The canary fails on its 600 s timeout with `YT-NETWORK_-…`. Its thread keeps running until 14:27:09. |
| 14:28:26 | Sending the canary report raises `Timed out` after the 120 s `read_timeout`. The administrator received the report anyway. |
| 14:28–22:50 | No user activity in `analytics.db`. Silence in `bot.log` is idleness, not a hang. |
| 20:25 | `systemd-networkd-wait-online` still times out on the host. |
| 22:50 | An administrator broadcast through `/admin` is delivered: the old stack had already recovered. |
| 22:53, 22:55 | Manual restarts land on a still-broken network: `googlevideo.com` does not resolve. |
| 22:59 | Third start. The canary at 23:04 and a user download at 23:05 succeed. |

The `telegram-bot-api` logs for the window were lost: the `json-file` log driver deletes them with a recreated container. With its current flags the service also writes nothing to stdout.

## Findings in code

1. **DNS failures were not network errors.** The yt-dlp message was:

   ```text
   ERROR: [download] Got error: HTTPSConnection(host='rr…googlevideo.com', port=443):
   Failed to resolve '…' ([Errno -5] No address associated with hostname).
   Giving up after 5 retries
   ```

   No marker matched it, so it became `DOWNLOAD`. A bare `socket.gaierror` reached the `OSError` branch and became `IO`, because its `errno` is negative.

2. **Four retry layers multiplied the wait:** yt-dlp's own retries, `execute_with_backoff` (three attempts, no jitter), the cookie attempt, and up to six CLI combinations. Renaming the category to `NETWORK` alone would have added backoff retries.

3. **The exception chain carries the cause only while parsing a link.** Measured inside the production container with the pinned yt-dlp: `DownloadError.exc_info[1]` → `TransportError.cause` → `urllib3.NameResolutionError.__cause__` → `socket.gaierror`. `DownloadError.__context__` also holds an `ExtractorError`. The failure on 2026-10-15 happened during a download, where yt-dlp's `RetryManager` raises a `DownloadError` with an empty `exc_info`. Only the text markers recognize that message.

4. **The canary bypassed `work_budget`.** It ran through `asyncio.to_thread`, so `work_budget.check()` saw no deadline, CLI runs were bounded only by `YTDLP_CLI_TIMEOUT`, and cancellation reached only the progress hook. The canary on 2026-10-15 did not fail on DNS. It failed on `Read timed out` (40 s socket timeout) to `www.youtube.com`, retried three times per request across both cookie modes, and exceeded 600 s.

5. **Polling timeouts are invisible.** PTB 22.8 (`telegram/ext/_utils/networkloop.py`) does not call `error_callback` for `RetryAfter` and `TimedOut` during polling. It logs at `DEBUG` and retries. A hung Bot API looks like silence at `INFO`, and those branches of `_classify_polling_error` in `main.py` are unreachable for polling.

6. **Nothing restarts a hung bot.** `bot` has no healthcheck, `telegram-bot-api` checks only its port, and `unless-stopped` acts only when the process exits.

7. **Reclassification removes crash reports.** `_should_notify_admins_platform_failure()` already excludes `NETWORK`, `NETWORK_TIMEOUT`, and `TIMEOUT`. After the fix, user-facing DNS failures no longer reach administrators until outage summaries exist. The canary reports any failure, so a long outage is still visible once per canary interval, but that report is itself sent during the outage and can end in `Timed out`, as at 14:28:26.

## Experiment 1: bot and Bot API without internet for 20 minutes

09:24:50–09:44:51 UTC on 2026-10-16, with the owner's consent. `iptables` rules in `DOCKER-USER` dropped traffic from the `bot` and `telegram-bot-api` containers to anything outside the stack network. Traffic inside the stack was untouched. Probes ran inside the bot container with a 30 s client timeout.

| Method | Before | During (minutes 1–19) | After unblocking |
|---|---|---|---|
| `getMe` | ok, 0.0 s | **ok, 0.0 s: served from cache** | ok |
| `getChat(admin)` | ok, 0.0 s | **hangs, 30 s timeout, no error** | ok after 0.4 s |
| `sendChatAction` | ok, 0.1 s | hangs, 30 s timeout | ok after 0.3 s |
| `sendMessage` | ok, 0.1 s | hangs, 30 s timeout, **delivered after recovery** | ok |
| `getWebhookInfo` | ok, pending 0 | ok, pending 0 (local) | ok, pending 0 |

Conclusions:

1. `getMe` is useless as a liveness probe. `getChat(<admin_id>)` with a short timeout is end-to-end and invisible to users, unlike `sendChatAction`.
2. The local Bot API does not return an error without Telegram; it stays silent. "Telegram unavailable" can be detected only by a client timeout.
3. TDLib reconnected in under a second after 20 minutes. Restarting `telegram-bot-api` is unnecessary for such an outage. A nine-hour outage was not reproduced.
4. The bot wrote nothing to `bot.log` or `docker logs` during the 20 minutes.
5. Nobody messaged the bot during the outage, so delivery of incoming updates sent during an outage was not tested. 2026-10-15 is indirect evidence: after nine hours the old stack accepted `/admin` and ran a broadcast.
6. **A client timeout does not cancel a send.** A `sendMessage` issued at 09:28:50 timed out on the client after 30 s. It reached the chat at 09:44, right after unblocking and before the later "unblocked" message, in order. TDLib held it for about 16 minutes after the client disconnected. Through the local Bot API, `TimedOut` means "send deferred", and a retry after it produces a duplicate after recovery. Media delivery already accounts for this: `_is_proven_not_sent` treats only `PoolTimeout` as not sent, and an `unknown` outcome is never retried. Keep that logic.

## Experiment 2: only the Bot API without internet for 3 minutes

10:34:06–10:37:06 UTC on 2026-10-16, with the owner's consent. Only `telegram-bot-api` was cut off; the bot kept its internet access. Requests ran inside the bot container with a 900 s client timeout.

| Request | Started | Response | After |
|---|---|---|---|
| `sendMessage` | 10:34:36 | **ok, a real `Message`** with `message_id` and `date` 10:37:13 | 156.6 s |
| `getChat` | 10:34:51 | ok | 141.6 s |

Conclusions:

1. The Bot API keeps the HTTP request open for the whole outage and has no timeout of its own, verified up to 2.5 minutes. After reconnecting it returns the real result. A client that waits learns that the message was delivered, with its ID and time.
2. Responses arrived about 7 s after unblocking, against 0.3–0.4 s in experiment 1. This is probably a TDLib reconnect delay; it was not verified.
3. Whether the HTTP layer holds a request longer than 2.5 minutes was not tested. Code that waits must handle a premature disconnect and keep such a record `unknown`.
4. `bot.log` stayed empty during the window; no user was affected.

The Bot API has no method that reads a bot's private chat or fetches a message by `message_id`. `getUserPersonalChatMessages` reads the channel linked to a user's profile. The response to the original request is the only source of truth.

## Reproducing the experiments

Run only with the owner's consent: experiment 1 makes the bot unavailable for its whole duration.

1. Find the stack network and container addresses with `docker network inspect <stack>_default` and `docker inspect`.
2. Install a dead-man switch before blocking, for example `systemd-run --on-active=25m` with a script that deletes the rules. It must remove every copy of the rule in a loop.
3. Block egress and tag the rule so it can be found:

   ```sh
   iptables -I DOCKER-USER 1 -s <container_ip> ! -d <stack_subnet> \
     -m comment --comment nuvio-exp -j DROP
   ```

4. Probe from inside the bot container, so the token comes from its environment and never reaches the output: `docker exec -i <bot_container> python -I - < probe.py`. Call the Bot API with `urllib`, record the outcome and duration of every call, and replace the token in printed text.
5. Delete the rules with `iptables -D` and the same arguments. Measure the time to the first successful `getChat`.
6. Collect `bot.log` and `docker logs telegram-bot-api` for the window. Mask tokens with `sed -E 's/[0-9]{8,12}:[A-Za-z0-9_-]{30,}/***/g'`.

Warn the chat being probed before starting: messages sent during the outage arrive in a burst after recovery.

## Open questions

- How long does TDLib keep its outgoing queue? At least 16 minutes was observed. On 2026-10-15 a report sent at 14:26 was delivered, but its delivery time was not recorded.
- Are incoming updates sent during an outage delivered after recovery?
- Which `telegram-bot-api` flags (`--log`, `--verbosity`, `--http-stat-port`) does the pinned build support? Check `telegram-bot-api --help` in the container.
