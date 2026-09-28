# Release 1: Concurrent update processing and live status

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
**Goal:** The "Cancel" button starts working during download, the second link is accepted without waiting for the first one, and the active status marker does not disappear after a single network error.
**Architecture:** By default, PTB has `max_concurrent_updates = 1`, and the update fetcher waits for the current handler to complete. We explicitly enable concurrent processing via a number, ensure delivery of the "Cancel" button press during a long-running task is tested, make the active status pulsing resilient, and solidify tests for the shared state properties that now depend on concurrency.
**Tech Stack:** Python 3.14, python-telegram-bot 22.8, pytest 9 (markers `unit`/`integration`, `--strict-markers`), ruff.

## Global Constraints

- The project is Russian-language: comments, docstrings, and user-facing text must be in Russian; identifiers remain in English.
- No new functionality in this release. Task queue, to-do list, and progress are covered in Release 2, separate plan.
- `ruff check --output-format=github .` must be clean; the rule policy in `pyproject.toml` (`select = ["E4", "E7", "E9", "F"]`) is not changed.
- All test suites run completely (`pytest`), without network access.
- Imports in `main.py` are intentionally placed below `load_dotenv()`; per-file-ignore `E402` is enabled. Do not move them to the top.
- `print()` is forbidden in production code (`tests/test_syntax.py`); logging is done via `utils/logger.py`.
- Conventional commits: `feat:`, `fix:`, `docs:`.

## Already Verified – Do Not Re-Discover

- PTB's `connection_pool_size` is **256** by default; pool exhaustion does not lead to loss of active status marker.
- `_check_spam` (`utils/telegram_utils.py:304`) is fully synchronous, with no `await` inside - it is atomic within a single event loop.
- `SessionStore.create` and other its methods are synchronous - there is no `await` inside.
- `utils/temp_file_manager.py` maps files by `session_id` and does not hold any shared mutable state.
- Methods of `SessionStore` that modify storage are named `create` and `remove` (`utils/callback_fsm.py:68` and `:105`), both synchronous.
- `cleanup_temp_files()` **without arguments** deletes all temporary folders, but all three such calls (`main.py:285`, `main.py:341`, `main.py:359`) occur only during process startup and shutdown. During update processing, only `cleanup_temp_files(session_id)` is called. No change is needed.

---

## File Structure

| File | Responsibility | Action |
|-----|----------------|--------|
| `main.py` | Constant `UPDATE_CONCURRENCY` and its passing to the builder | Modify |
| `tests/test_concurrent_update_delivery.py` | Delivery of update during a long-running task | Create |
| `utils/telegram_utils.py` | `_pulsing_chat_action` survives failed message send | Modify |
| `tests/test_chat_action_resilience.py` | Resilience of pulsing status | Create |
| `tests/test_shared_state_under_concurrency.py` | Properties of shared state that concurrency now depends on | Create |
| `CLAUDE.md`, `AGENTS.md` | Rule "handler does not hold fetcher" | Modify |

---

### Task 1: Delivery of click during a long-running task

The heart of this release. The test initially fails under current configuration - this is proof of the bug, not speculation about it.
**Files:**
- Create: `tests/test_concurrent_update_delivery.py`
- Modify: `main.py:172-196` (function `_configure_application_builder`)
- Modify: `main.py` - new constant alongside other settings
**Interfaces:**
- Produces: `main.UPDATE_CONCURRENCY: int` - maximum number of updates processed concurrently. The value is read from the test, so it must be a module-level constant, not a literal inside a function call.
- [ ] **Step 1: Write a failing test**
Create `tests/test_concurrent_update_delivery.py`:

```python
"""Нажатие кнопки обязано доходить до хэндлера, пока идёт долгая задача.

Замерено на PTB 22.8: у `Application` по умолчанию
`max_concurrent_updates = 1`, и `__update_fetcher` ждёт завершения текущего
апдейта, прежде чем взять следующий. Пока `download_content` висит на
`run_blocking`, нажатие «Отменить» лежит в очереди:

    0.0с  обработчик длинной задачи начал
    0.3с  нажата кнопка «Отменить»
    1.5с  обработчик длинной задачи кончил
    1.5с  ОТМЕНА обработана        ← уже впустую

Механизм отмены при этом исправен — до него не доходит сигнал. Остальные
тесты отмены вызывают `button_callback` напрямую и поэтому проверяют
механизм, но не доставку.
"""

import asyncio
import contextlib

import pytest
from telegram.ext import ApplicationBuilder, TypeHandler

import main


pytestmark = pytest.mark.integration

LONG_TASK_SECONDS = 0.6
PRESS_AFTER_SECONDS = 0.1


class _LongTask:
    """Апдейт, который обрабатывается долго, как скачивание."""


class _Press:
    """Апдейт от кнопки, который должен обработаться не дожидаясь первого."""


def _run_two_updates(concurrency) -> dict[str, float]:
    """Возвращает моменты событий относительно старта долгой задачи."""
    marks: dict[str, float] = {}

    async def scenario() -> None:
        builder = ApplicationBuilder().token("123:ABC")
        if concurrency is not None:
            builder = builder.concurrent_updates(concurrency)
        app = builder.build()
        # `initialize()` ходит в сеть за `get_me`, а нам нужен только фетчер.
        app._initialized = True

        started = asyncio.get_running_loop().time()

        async def long_task(_update, _context) -> None:
            marks["задача началась"] = asyncio.get_running_loop().time() - started
            await asyncio.sleep(LONG_TASK_SECONDS)
            marks["задача кончилась"] = asyncio.get_running_loop().time() - started

        async def press(_update, _context) -> None:
            marks["нажатие обработано"] = (
                asyncio.get_running_loop().time() - started
            )

        app.add_handler(TypeHandler(_LongTask, long_task))
        app.add_handler(TypeHandler(_Press, press))

        fetcher = asyncio.create_task(app._update_fetcher())
        await app.update_queue.put(_LongTask())
        await asyncio.sleep(PRESS_AFTER_SECONDS)
        await app.update_queue.put(_Press())
        await asyncio.sleep(LONG_TASK_SECONDS + 0.4)
        fetcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await fetcher

    asyncio.run(scenario())
    return marks


def test_press_is_handled_before_the_long_task_finishes():
    """Настроенная параллельность обязана доставлять нажатие сразу."""
    marks = _run_two_updates(main.UPDATE_CONCURRENCY)

    assert "нажатие обработано" in marks, "нажатие не дошло до хэндлера"
    assert marks["нажатие обработано"] < marks["задача кончилась"], (
        f"нажатие обработано на {marks['нажатие обработано']:.2f}с, "
        f"а задача кончилась на {marks['задача кончилась']:.2f}с — "
        "значит апдейт ждал в очереди"
    )


def test_default_configuration_is_the_one_that_was_broken():
    """Фиксирует саму причину: без настройки нажатие ждёт конца задачи.

    Тест страхует от «починки», которая на самом деле ничего не меняет:
    если однажды PTB сменит поведение по умолчанию, он упадёт и заставит
    перечитать этот план.
    """
    marks = _run_two_updates(None)

    assert marks["нажатие обработано"] >= marks["задача кончилась"]


def test_concurrency_limit_leaves_room_for_navigation():
    """Предел обязан быть выше числа воркеров скачивания.

    Иначе восемь занятых загрузок съедают все слоты, и девятый апдейт —
    нажатие «Отменить» — снова ждёт в очереди.
    """
    from config import DOWNLOAD_WORKERS

    assert main.UPDATE_CONCURRENCY > DOWNLOAD_WORKERS
```

- [ ] **Step 2: Run the test and verify it fails**
Run: `pytest tests/test_concurrent_update_delivery.py -v`
Expected: FAIL. The tests `test_press_is_handled_before_the_long_task_finishes` and `test_concurrency_limit_leaves_room_for_navigation` fail with `AttributeError: module 'main' has no attribute 'UPDATE_CONCURRENCY'`. The test `test_default_configuration_is_the_one_that_was_broken` passes - it describes the current behavior.
- [ ] **Step 3: Add the constant to `main.py`**
Insert after the imports and logger setup block (after the line `logger = setup_logger(__name__, level=LOG_LEVEL)`):

```python
# Сколько апдейтов бот обрабатывает одновременно.
#
# По умолчанию PTB обрабатывает апдейты строго по одному: фетчер ждёт
# завершения текущего обработчика. Из-за этого нажатие «Отменить» лежало в
# очереди всё скачивание и обрабатывалось, когда файл уже отправлен, а вторая
# ссылка не читалась вовсе.
#
# Значение заведомо выше DOWNLOAD_WORKERS: скачивания занимают слоты надолго, и
# на навигацию по меню с отменой должно оставаться место, иначе ограничение
# вернёт ту же поломку под нагрузкой.
UPDATE_CONCURRENCY = 32
```

- [ ] **Step 4: Pass the value to the builder**
In `_configure_application_builder` (`main.py:177`), add a call to the chain  -
first, so it won't be lost among the timeouts:

```python
    builder = (
        builder.token(TELEGRAM_TOKEN)
        .concurrent_updates(UPDATE_CONCURRENCY)
        .connect_timeout(10.0)
        .read_timeout(120.0)
```

- [ ] **Step 5: Run tests and ensure they pass**
Run: `pytest tests/test_concurrent_update_delivery.py -v`
Expected: PASS, all three tests.
- [ ] **Step 6: Verify that the contract test for the builder did not break**
Run: `pytest tests/test_local_bot_api_application.py -v`
Expected: PASS. `_BuilderRecorder` accepts any method, so a new call in the chain does not break it. If it fails, it means the test lists calls exactly as specified; in this case, add `concurrent_updates` to the expectations without removing the others.
- [ ] **Step 7: Run the full suite**
Run: `pytest -q`
Expected: PASS overall. Pay special attention to `tests/test_main_polling.py` - it builds the application and might have relied on the previous configuration.
- [ ] **Step 8: Commit**

```bash
git add main.py tests/test_concurrent_update_delivery.py
git commit -m "fix: нажатие кнопки доходит до бота во время скачивания"
```

---

### Task 2: Pulse of activity marker experiences a failure

**Files:**
- Modify: `utils/telegram_utils.py:747-778` (`_pulsing_chat_action`)
- Create: `tests/test_chat_action_resilience.py`
**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: behavior of `_pulsing_chat_action` - the loop continues upon sending error; exit only on task cancellation.
- [ ] **Step 1: Write a failing test**
Create `tests/test_chat_action_resilience.py`:

```python
"""Отметка «отправляет видео…» не должна пропадать на длинной отправке.

Прежний цикл выходил навсегда при первой же ошибке отправки:

    except telegram.error.TelegramError as error:
        logger.debug(...)
        return

Один сетевой сбой за десять минут выгрузки — и в шапке чата больше ничего нет,
хотя работа идёт. Именно из этого выросла жалоба «не понимаешь, сломалось или
видео всё-таки придёт». Исчерпание пула соединений тут не при чём:
`connection_pool_size` по умолчанию 256.
"""

import asyncio

import pytest
import telegram

from utils import telegram_utils


pytestmark = pytest.mark.unit


class _Chat:
    """Чат, у которого первые `failures` отправок падают."""

    def __init__(self, failures: int):
        self._failures = failures
        self.sent = 0

    async def send_action(self, action: str) -> None:
        self.sent += 1
        if self.sent <= self._failures:
            raise telegram.error.TimedOut()


def _pulses_during(chat, seconds: float) -> int:
    async def scenario() -> None:
        async with telegram_utils._pulsing_chat_action(chat, "upload_video"):
            await asyncio.sleep(seconds)

    asyncio.run(scenario())
    return chat.sent


def test_pulse_continues_after_a_failed_send(monkeypatch):
    """Сбой отправки не повод бросать отметку до конца работы."""
    monkeypatch.setattr(telegram_utils, "_CHAT_ACTION_REFRESH_SECONDS", 0.05)
    chat = _Chat(failures=1)

    sent = _pulses_during(chat, 0.3)

    assert sent > 1, "после ошибки пульс больше не пытался"


def test_pulse_does_not_hot_loop_when_every_send_fails(monkeypatch):
    """Пауза обязана соблюдаться и на ошибках, иначе это busy-loop."""
    monkeypatch.setattr(telegram_utils, "_CHAT_ACTION_REFRESH_SECONDS", 0.05)
    chat = _Chat(failures=1000)

    sent = _pulses_during(chat, 0.3)

    assert sent <= 8, f"за 0.3с при паузе 0.05с не может быть {sent} попыток"


def test_pulse_stops_when_the_work_is_over(monkeypatch):
    """Отметка живёт ровно столько, сколько работа."""
    monkeypatch.setattr(telegram_utils, "_CHAT_ACTION_REFRESH_SECONDS", 0.05)
    chat = _Chat(failures=0)

    sent = _pulses_during(chat, 0.12)
    after_exit = chat.sent

    asyncio.run(asyncio.sleep(0.2))

    assert sent >= 1
    assert chat.sent == after_exit, "пульс продолжился после выхода из блока"


def test_disabled_pulse_sends_nothing():
    """На дешёвых действиях отметка не нужна и не должна шуметь."""
    chat = _Chat(failures=0)

    async def scenario() -> None:
        async with telegram_utils._pulsing_chat_action(chat, "upload_video", False):
            await asyncio.sleep(0.05)

    asyncio.run(scenario())

    assert chat.sent == 0
```

- [ ] **Step 2: Run the test and ensure it fails**
Run: `pytest tests/test_chat_action_resilience.py -v`
Expected: FAIL on `test_pulse_continues_after_a_failed_send`  -
`assert 1 > 1`, because after the first error the loop exits.
The other three tests pass.
- [ ] **Step 3: Make the loop resilient**
In `utils/telegram_utils.py`, replace the body of `_pulse`:

```python
    async def _pulse() -> None:
        failures = 0
        while True:
            try:
                await chat.send_action(action)
                failures = 0
            except telegram.error.TelegramError as error:
                failures += 1
                # Первый сбой — рядовое дело на длинной выгрузке, поэтому
                # шумим только когда отметка не проходит подряд: это уже
                # означает, что пользователь всё время видит пустую шапку.
                if failures == _CHAT_ACTION_FAILURES_BEFORE_WARNING:
                    logger.warning(
                        "Отметка активности не проходит %s раз подряд: %s",
                        failures,
                        error,
                    )
                else:
                    logger.debug("Отметка активности не отправлена: %s", error)
            await asyncio.sleep(_CHAT_ACTION_REFRESH_SECONDS)
```

Next to `_CHAT_ACTION_REFRESH_SECONDS` (`utils/telegram_utils.py:736`) add:

```python
# Сколько подряд неудачных отметок терпим молча. Одна — рядовой сбой на
# длинной выгрузке; серия означает, что шапка чата пуста всё время работы.
_CHAT_ACTION_FAILURES_BEFORE_WARNING = 3
```

Also fix the docstring of the context manager - the previous statement "failure does not lose" remains correct, but we should add that failure does not stop attempts:

```python
    """Держит отметку «отправляет видео…» в шапке чата на всё время работы.

    Это единственная анимация, доступная боту: рисовать «крутилку» правкой
    текста значило бы запрос на каждый кадр и затирание статусов. Отметка —
    украшение, поэтому её отказ работу не роняет и не прекращает попыток:
    прежний цикл выходил навсегда после первой ошибки, и на длинной отправке
    шапка чата пустела до самого конца.
    """
```

- [ ] **Step 4: Run tests and ensure they pass**
Run: `pytest tests/test_chat_action_resilience.py -v`
Expected: PASS, all four tests.
- [ ] **Step 5: Check previous pulse tests**
Run: `pytest tests/test_chat_action_pulse.py -v`
Expected: PASS without modifications.
- [ ] **Step 6: Commit**

```bash
git add utils/telegram_utils.py tests/test_chat_action_resilience.py
git commit -m "fix: отметка активности не пропадает после сетевого сбоя"
```

---

### Task 3: Lock shared state properties

Concurrency relies on the fact that the shared state changes without `await` in the middle. This is currently true, but it's held by chance - the test will turn this into a requirement.
**Files:**
- Create: `tests/test_shared_state_under_concurrency.py`
**Interfaces:**
- Consumes: `main.UPDATE_CONCURRENCY` from Task 1.
- [ ] **Step 1: Write a test**
Create `tests/test_shared_state_under_concurrency.py`:

```python
"""Общее состояние обязано меняться без `await` посередине.

Апдейты теперь обрабатываются параллельно (`main.UPDATE_CONCURRENCY`). Гонок
данных в одном event loop не бывает, а логическое переплетение — бывает:
прочитал счётчик → `await` → записал устаревшее значение. Пока все правки
общего состояния синхронны, переплетаться нечему. Тесты закрепляют именно это
свойство, чтобы будущий `await` внутри не проехал незамеченным.
"""

import inspect

import pytest

from utils import callback_fsm, telegram_utils


pytestmark = pytest.mark.unit


def _has_await(func) -> bool:
    return "await " in inspect.getsource(func)


def test_antispam_check_is_synchronous():
    """Между чтением и записью списка запросов не должно быть точки переключения."""
    assert not inspect.iscoroutinefunction(telegram_utils._check_spam)
    assert not _has_await(telegram_utils._check_spam)


def test_session_store_mutations_are_synchronous():
    """Создание и удаление сессии — атомарные операции для event loop."""
    for method in (
        callback_fsm.SessionStore.create,
        callback_fsm.SessionStore.remove,
    ):
        assert not inspect.iscoroutinefunction(method), method.__name__
        assert not _has_await(method), method.__name__


def test_antispam_counts_every_request():
    """Ни один запрос не теряется: четвёртый за окно упирается в лимит."""

    class _Context:
        def __init__(self):
            self.user_data: dict = {}

    context = _Context()
    verdicts = [
        telegram_utils._check_spam(1, context, now=100.0 + index * 0.1)
        for index in range(telegram_utils._SPAM_REQUEST_LIMIT)
    ]

    assert verdicts[:-1] == [False] * (telegram_utils._SPAM_REQUEST_LIMIT - 1)
    assert verdicts[-1] is True


def test_two_users_do_not_share_antispam_state():
    """Счётчики живут в user_data, поэтому один пользователь не блокирует другого."""

    class _Context:
        def __init__(self):
            self.user_data: dict = {}

    first, second = _Context(), _Context()
    for index in range(telegram_utils._SPAM_REQUEST_LIMIT):
        telegram_utils._check_spam(1, first, now=100.0 + index * 0.1)

    assert telegram_utils._check_spam(2, second, now=100.5) is False
```

- [ ] **Step 2: Run the test**
Run: `pytest tests/test_shared_state_under_concurrency.py -v`
Expected: PASS. The tests describe already-existing properties - this is a safeguard, not a fix. **If something fails - stop:** this indicates a real concurrency issue, and it must be resolved before continuing the release, not bypassed by manually editing the test.
- [ ] **Step 3: Commit**

```bash
git add tests/test_shared_state_under_concurrency.py
git commit -m "test: закрепить синхронность правок общего состояния"
```

---

### Task 4: Documentation of the Rule

The rule is subtle and easily violated: now, the handler that holds the update is no longer just "slow" - it again breaks cancellation.
**Files:**
- Modify: `CLAUDE.md` - section "Key Patterns"
- Modify: `AGENTS.md` - next to the architecture description
- [ ] **Step 1: Add the rule to `CLAUDE.md`**
In the `## Key Patterns` section, immediately after the entry about `Async + ThreadPoolExecutor`, add:

```markdown
- **Параллельная обработка апдейтов**: `UPDATE_CONCURRENCY` в `main.py` (32,
  заведомо больше `DOWNLOAD_WORKERS`). До её включения PTB обрабатывал апдейты
  строго по одному, и нажатие «Отменить» лежало в очереди всё скачивание —
  механизм отмены был исправен, но сигнал до него не доходил. Отсюда правило:
  **хэндлер не имеет права держать фетчер апдейтов дольше необходимого**.
  Блокирующая работа — только через `run_blocking` с `session_id=`. Тест
  доставки — `tests/test_concurrent_update_delivery.py`, он падает, если
  параллельность снова выключат
```

- [ ] **Step 2: Add to `AGENTS.md`**
Find the line about the task scheduler and graceful shutdown in the description of `main.py` and add next to it:

```markdown
- Обрабатывает апдейты параллельно (`UPDATE_CONCURRENCY = 32`): последовательная
  обработка по умолчанию лишала смысла кнопку отмены и не давала принять вторую
  ссылку во время скачивания.
```

- [ ] **Step 3: Check that the documentation tests haven't broken**
Run: `pytest tests/test_documentation_consistency.py -v`
Expected: PASS.
- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md AGENTS.md
git commit -m "docs: правило про параллельную обработку апдейтов"
```

---

### Task 5: Full check and release

- [ ] **Step 1: Complete set and linting**

```bash
ruff check --output-format=github .
pytest -q
```

Expected: ruff with no issues, all tests pass.
- [ ] **Step 2: Coverage did not drop below the CI threshold**

```bash
coverage run --branch -m pytest tests/
coverage report --fail-under=40
```

Expected: PASS.
- [ ] **Step 3: Manually verify on the live bot**
Mandatory, because the automated test checks the PTB fetcher, not the real Telegram:
1. Send a link to a large video and start downloading.
2. Click "Cancel" - the status must change to "Cancelled" within a second, not after the file is sent.
3. Before the download completes, send a second link - the format menu must appear immediately.
4. Check the logs to ensure the activity marker does not disappear: `docker compose logs bot | grep -i "activity marker"`.
- [ ] **Step 4: Release**
Branch, PR, wait for green CI, rebase and merge, tag `v1.7.1` (bug fixes without new functionality), wait for the image build, then on the server:

Replace the example host and directory with the values for your deployment.

```bash
ssh server.example 'cd /path/to/nuvio && docker compose pull && docker compose up -d'
```

- [ ] **Step 5: Check the store after update**

```bash
ssh server.example 'cd /path/to/nuvio && docker compose ps && docker compose logs bot --since 5m | grep -ciE "error|traceback"'
```

Expected: containers are `Up`, 0 errors. Repeat the cancellation check from Step 3 on the production bot.

---

## What this release does not do

Task queue, three-task limit, task list sent in a single message, download progress percentages, message about lost queue after restart - all of this is part of release 2 according to the spec `docs/superpowers/specs/2026-07-29-download-queue-and-progress-design.md`.
After this release, the second link is accepted, but downloads happen simultaneously rather than sequentially: users can start as many parallel downloads as desired within the `DOWNLOAD_WORKERS` limit. The three-task limit will appear in release 2. The risk during this period is acknowledged: anti-spam (4 requests every 5 seconds) remains the only constraint.
