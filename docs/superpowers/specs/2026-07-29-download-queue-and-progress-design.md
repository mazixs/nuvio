# User Load Queue and Fair Progress

Date: 2026-07-29. Status: specification, implementation not started.
This document describes three interrelated issues: accepting new links during processing, limiting the queue to no more than two tasks per user, and providing clear indication of long-running operations. The first issue cannot be changed without altering the update processing mechanism - this is the root cause from which everything begins.

---

## 1. Why we currently cannot cancel or resend a link

### Measured Cause

`main.py` does not call `.concurrent_updates(...)`, so PTB 22.8 uses the default value `max_concurrent_updates = 1` (verified on the installed version). In this mode, the update fetching loop in `Application.__update_fetcher` operates as follows:

```python
if self._update_processor.max_concurrent_updates > 1:
    self.create_task(self.__process_update_wrapper(update), ...)
else:
    await self.__process_update_wrapper(update)   # ждём завершения
```

While `button_callback` → `download_content` is blocked by `await run_blocking(...)`, the next update from the queue is not picked up. Observed on live PTB by replacing handlers:

```
0.0 s  long-running handler starts
0.3 s  user presses "Cancel"
1.5 s  long-running handler finishes
1.5 s  cancellation is processed, 1.2 s too late
```

### Consequences this explains

- **The "Cancel" button is unresponsive** during all long phases. The cancellation mechanism itself is working correctly: the session_id, progress_hooks, and CancelledByUser records are all functioning properly. The issue is simply that the button press never reaches the mechanism. Existing tests (`test_cancel_flow.py`, `test_ytdlp_cancellation.py`) trigger the `button_callback` directly, so they verify the mechanism but not the delivery.
- **The second link is not fully read** until the first task is completed.
- **`DOWNLOAD_WORKERS=8` is effectively unused.** The worker pool serves only one task at a time, because the second task cannot start. The anti-spam rule ("4 requests every 5 seconds") assumes parallelism, which is absent.

### Separate reason for status loss

`_pulsing_chat_action` permanently exits the loop upon the first error:

```python
except telegram.error.TelegramError as error:
    logger.debug("Отметка активности не отправлена: %s", error)
    return
```

One network failure within ten minutes of sending - and the "sending video" status disappears until the end of the operation. Connection pool exhaustion is not the issue: `connection_pool_size` is set to 256 by default and has been verified.

---

## 2. Requirements

1. A link sent during operation is accepted immediately; the current task is not interrupted.
2. The format is requested immediately upon receiving a link. Downloading is queued.
3. No more than **two** tasks per user: one in progress plus one waiting. A third task receives no rejection, but instead a prompt to select the same format again. A link where the format has not yet been selected does **not** occupy a slot. The reason has been measured: sessions have no TTL - `SessionStore` only evicts them when five or more have accumulated by creation time (`utils/callback_fsm.py:90`). Think of it as abandoned menus; one such menu would block half the capacity indefinitely.
4. The state of all tasks is visible in a **single** message summary.
5. During long-running operations, it is visible that the process is alive and how many tasks have been completed.
6. Cancellation works instantly - for both running and waiting tasks.

---

## 3. Boundaries within which this is feasible

### Progress during download is not available

yt-dlp provides download progress via `progress_hooks` - the same hooks are already used for cancellation. Neither PTB nor the local Bot API report how many bytes have been sent during upload. Therefore, during the upload phase, only **size and elapsed time** are shown, not percentages. A progress bar for upload would be speculative.

### Message edits are limited by Telegram

The list is updated no more than **once every 5 seconds per user**, and only when the state or percentage changes. More frequently - rejections and flickering instead of status updates. One message for all tasks is beneficial: one edit stream instead of three. Changes in task state (`downloading` → `uploading` → `done`) are drawn outside of waiting queue - such events are rare, but they are critical.

### The queue does not survive restarts

It exists in memory, just like existing sessions (`context.user_data["sessions"]`). Recovery would require a database, and with it - reconstruction of parsed formats and message associations, i.e., almost the entire state. For two tasks, this is not justified.
**However, a "downloading 45%" message lingering after a restart is unacceptable** - this is exactly the misunderstanding that gave rise to requirement 5. Therefore, message identifiers for active task lists are preserved (in a table in `analytics.db`), and upon startup, the bot rewrites them to "Bot restarted, tasks not preserved, please send the link again", after which the record is deleted. This is the only thing that persists.

---

## 4. Architecture

Two new modules with narrow, focused responsibilities - instead of expanding `telegram_utils.py`, which already contains about 2800 lines.

### `utils/download_queue.py` - data structure

A task (`QueuedTask`): session token, URL, platform, action, format value, state, progress. A user's queue: active task plus at most one waiting task, state transitions. It has no knowledge of Telegram or asyncio - thus it is tested without mocks.

States: `waiting` → `downloading` → `uploading` → `done` | `failed` | `cancelled`.

With a limit of two tasks, slot arithmetic is unnecessary: occupied means there is an active task and one waiting task. The only thing required from the module is to accept a task or reject it, pass the next one, and change the state.

### Execution - without its own lifecycle

There will be no separate executor module. The first version of this spec assumed a long-lived `asyncio.Task` per user, with individual task creation, shutdown handling, and monitoring of orphaned tasks. All of this is already handled by PTB:

`Application.create_task` (verified on 22.8) passes coroutine exceptions through `process_error`, meaning they go to the global error handler - errors inside the queue cannot silently disappear. Tasks created by it **wait for completion during `stop()`**, so bot shutdown does not require a runner registry or manual cancellation of tasks.

Therefore, execution is a short coroutine in `telegram_utils`: it processes the user's queue and then ends. It is launched via `context.application.create_task` when a task is added, if the user has no active task running. No class of errors related to orphaned tasks arises under this design.

### `utils/task_list_view.py` - rendering

A pure function: queue state → text and keyboard. Separated out because this is the only part that will need frequent updates, and it's easy to verify by string comparison.

### Existing changes

| File | Change |
|-----|--------|
| `main.py` | `concurrent_updates` (release 1) |
| `utils/callback_fsm.py` | parsing `q\|{task}\|{action}` - task addressing in the list |
| `utils/telegram_utils.py` | queue placement instead of direct file download; task list instead of status in own message |
| `utils/telegram_utils.py` | `_pulsing_chat_action` does not die from a single error |
| `utils/analytics_db.py` | table of active task lists for message on restart |

---

## 5. Request flow

```
URL -> spam check -> metadata (run_blocking) -> format menu
  -> format selected
  -> slot available? If no, keep menu and ask user to try selection later
  -> if yes, remove format menu, enqueue task, redraw task list
  -> active task for this user? If yes, wait in the queue
  -> if no, application.create_task(process_user_queue)
  -> downloading: progress_hooks update task list at most every 5 s
  -> uploading: show size and elapsed time at most every 5 s
  -> file delivered: remove previous task list and send updated list below
```

The task list is resent after each delivered file so that the status remains aligned with the latest video, rather than drifting upward.

---

## 6. Error Handling

**One failed task does not crash the queue.** A task marked with `✗` and an error code is recorded, and the coroutine proceeds to the next one. The same applies to `BLOCKING_TASK_TIMEOUT` (600s): the task is marked as "failed to complete within 10 minutes," and the queue continues.
**Cancellation.** `✕ N` removes a waiting task from the queue; for an active task, it triggers the existing `request_cancellation(session_id)`, and yt-dlp is interrupted via `progress_hooks`. "Cancel all" performs both actions.
**Task list editing.** Only through `safe_edit_message_text`: the user can delete the task list message, and this is not an error. If the list is deleted, it is resent on the next update.
**Failure to mark activity status** remains non-fatal - the pulse now survives failures and continues retry attempts, rather than exiting permanently.

---

## 7. Testing

Modular, without network access or mocked Telegram:
- `download_queue`: limit of 2, considered as occupied slot (a link without selected format is not counted); state transitions, removal of tasks from the middle.
- `task_list_view`: text and keyboard for each state, including "queue is full" and mixed lists of ready and waiting tasks.
- Execution: one task per user at a time; a failed task does not block the next; the coroutine ends when the queue is empty.

Through handlers:
- **Delivery of a callback during a long-running task** - no test exists for this, and this was exactly the broken case. It is verified that the callback is processed without waiting for the current task to finish.
- A second link received while a task is running shows the format menu.
- A third task is not accepted; the previous two remain unaffected, and the format menu stays active - re-clicking the same format after freeing the slot works without re-sending the link.
- The task list can be edited no more than once every 5 seconds.

Regarding concurrency:
- Concurrent clicks do not break anti-spam or session storage. `_check_spam` is fully synchronous, with no `await` inside - thus it remains atomic within a single event loop; the test confirms this property to ensure that future `await` inside won't go unnoticed.

---

## 8. Intentional Omissions

- Task priorities and reordering within the list. Two items are simpler to cancel and resend.
- Total volume limits. A limit on the number of tasks, not on gigabytes: three two-gigabyte videos still occupy disk space and time.
- Queue recovery after restart (see §3).

---

## 9. Two Releases Instead of One

The work is split into two parts, and this is not just formalism: the first part fixes a previously broken feature, the second introduces new functionality. Mixing them in one release means you won't understand what exactly broke if it breaks again.
**Release 1 - "The cancel button works again."** `concurrent_updates`, checks for shared state during concurrent access, resilient activity status pulse, test for callback delivery during a long-running task. No new features: after this, cancellation works, the second link is read, and several users run in parallel. This is a standalone value and the biggest risk - it's clearly visible separately.
**Release 2 - Queue and task list.** Two new modules, limit of 2, progress tracking, restart notification. Built on a foundation already proven in production.

## 10. Main Risk

Enabling parallel processing of updates is the riskiest aspect of this work.
Currently, the bot physically couldn't perform two tasks at once, which masked any issues with the shared state. Before going to production, we need to check anti-spam, `SessionStore`, and file access for temporary files under concurrent access, and solidify these checks with tests. Data races within a single event loop won't occur, but logical race conditions (read counter → await → write outdated value) are entirely possible.
