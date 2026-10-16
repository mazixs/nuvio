# Cache Coverage and Elimination of Duplicate Requests - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
**Goal:** Enable caching using `file_id` for six actions where it currently does not provide hits, and remove redundant short URL expansion in the TikTok fast path.
**Architecture:** Cache keys reside in a pure function `cache_key_for_main_action` (`utils/platform_actions.py`). For Rutube and VK video actions, it returns `None`, so both read and write operations are inactive - this is fixed by a single function since both ends call it. For audio, write operations follow strictly defined strings, but there is no read block in any of the four handlers - a general cache delivery helper is added. Separately, the TikTok fast path currently re-expands the short URL after the calling code has already done so.
**Tech Stack:** Python 3.14, python-telegram-bot 22.8, SQLite (WAL), pytest 9.1.1, ruff 0.16.0.

## Global Constraints

- Python 3.14+; project environment is `.venv`; all commands must be run as `.venv/bin/python -m …`.
- Ruff policy is fixed: `select = ["E4", "E7", "E9", "F"]` in `pyproject.toml`. Changes must not introduce warnings - `tests/test_ruff.py` runs the linter within the test suite.
- pytest markers are only `syntax`, `unit`, `integration` (with `--strict-markers` enabled). Adding new markers is prohibited - `tests/test_dead_code_contract.py` enforces this.
- Comments and docstrings are in Russian; identifiers are in English.
- `print()` is forbidden in production code (`tests/test_syntax.py::TestCodeQuality::test_no_print_statements`); starred imports are also forbidden there.
- All user-facing texts are taken from `messages.py` and must not be hardcoded in handlers.
- Coverage threshold: `coverage report --fail-under=40` must pass.
- **The format of `callback_data` must not change.** No task may add or rename buttons.
- **Cache key values must not be modified.** In the `video_cache` table, existing records have `format_id` values equal to `"tiktok_audio"`, `"instagram_audio"`, `"rutube_audio"`, `"vk_audio"` - new keys must match these strings byte-for-byte; otherwise existing records will become unreachable.
- Final check for each task: `.venv/bin/python -m ruff check .` and `.venv/bin/python -m pytest` - both must pass without errors.

## File Structure

| File | Responsibility | Changes |
|------|----------------|--------|
| `utils/platform_actions.py` | Pure mapping: "platform + action → cache key" | Extended to include Rutube, VK, and audio actions |
| `utils/telegram_utils.py` | Handler coordination | Adds helper `_deliver_cached_audio` and four cache read blocks |
| `utils/tiktok_instagram_utils.py` | Platform-specific downloaders | Fast path now accepts already-expanded URL |
| `tests/test_platform_actions.py` | Tests for pure logic | New test cases and coverage contract added |
| `tests/test_cached_audio_delivery.py` | New: audio delivery from cache | Created |
| `tests/test_tiktok_fast_download.py` | Tests for fast path | New test cases for single expansion scenario |

## Current State (measured, not assumed)

| Action | Cache read | Cache write | Result |
|-------|------------|-------------|--------|
| `tiktok_download` | yes, `direct_video` | yes, `direct_video` | works |
| `instagram_download` | yes, `direct_video` | yes, `direct_video` | works |
| `tg_video` (YouTube) | yes, `tg_video` | yes, `tg_video` | works |
| `rutube_download` | called with `None` | key `None` → does not write | **dead** |
| `vk_download` | called with `None` | key `None` → does not write | **dead** |
| `tiktok_audio` | **no block** | yes, `"tiktok_audio"` | writes, does not read |
| `instagram_audio` | **no block** | yes, `"instagram_audio"` | writes, does not read |
| `rutube_audio` | **no block** | yes, `"rutube_audio"` | writes, does not read |
| `vk_audio` | **no block** | yes, `"vk_audio"` | writes, does not read |

---

### Task 1: Cache keys for Rutube and VK video actions

The handlers `rutube_download` (`utils/telegram_utils.py:1556`) and `vk_download` (`utils/telegram_utils.py:1656`) already call `_cache_format_id_for_main_action` both on read and on write. Therefore, fixing one clean function will enable caching on both sides - no changes to `telegram_utils.py` are needed in this task.
**Files:**
- Modify: `utils/platform_actions.py:4-13`
- Test: `tests/test_platform_actions.py`
**Interfaces:**
- Consumes: nothing from previous tasks.
- Produces: `cache_key_for_main_action(platform: str, action: str) -> str | None` - now returns `"direct_video"` for `("rutube", "rutube_download")` and `("vk", "vk_download")`. A modular constant `_DIRECT_VIDEO_PLATFORMS: frozenset[str]`. Task 2 extends this same function.
- [ ] **Step 1: Write a failing test**
Append to the end of `tests/test_platform_actions.py`:

```python
def test_main_action_cache_keys_cover_rutube_and_vk():
    assert cache_key_for_main_action("rutube", "rutube_download") == "direct_video"
    assert cache_key_for_main_action("vk", "vk_download") == "direct_video"
```

- [ ] **Step 2: Run the test and ensure it fails**
Run: `.venv/bin/python -m pytest tests/test_platform_actions.py::test_main_action_cache_keys_cover_rutube_and_vk -v`
Expected: FAIL with `AssertionError: assert None == 'direct_video'`
- [ ] **Step 3: Minimal implementation**
In `utils/platform_actions.py`, replace the block on lines 4-13.
Previously:

```python
DIRECT_VIDEO_CACHE_KEY = "direct_video"


def cache_key_for_main_action(platform: str, action: str) -> str | None:
    """Возвращает ключ кэша для основной кнопки платформы."""
    if platform in {"tiktok", "instagram"} and action.endswith("_download"):
        return DIRECT_VIDEO_CACHE_KEY
    if platform == "youtube" and action == "tg_video":
        return "tg_video"
    return None
```

It became:

```python
DIRECT_VIDEO_CACHE_KEY = "direct_video"

# Платформы, у которых основная кнопка отдаёт единственный вариант видео,
# поэтому один ключ кэша на URL достаточен.
_DIRECT_VIDEO_PLATFORMS = frozenset({"tiktok", "instagram", "rutube", "vk"})


def cache_key_for_main_action(platform: str, action: str) -> str | None:
    """Возвращает ключ кэша для основной кнопки платформы."""
    if platform in _DIRECT_VIDEO_PLATFORMS and action.endswith("_download"):
        return DIRECT_VIDEO_CACHE_KEY
    if platform == "youtube" and action == "tg_video":
        return "tg_video"
    return None
```

- [ ] **Step 4: Run the test and ensure it passes**
Run: `.venv/bin/python -m pytest tests/test_platform_actions.py -v`
Expected: PASS, all three tests in the file are green. In particular, the existing test `test_main_action_cache_keys_are_explicit` must remain green: `cache_key_for_main_action("youtube", "audio_m4a")` is still `None`, because `"youtube"` is not in `_DIRECT_VIDEO_PLATFORMS`.
- [ ] **Step 5: Run the full suite and linter**
Run: `.venv/bin/python -m ruff check . && .venv/bin/python -m pytest -q`
Expected: `All checks passed!` and `208 passed` (207 existing tests plus one new one).
- [ ] **Step 6: Commit**

```bash
git add utils/platform_actions.py tests/test_platform_actions.py
git commit -m "fix: включить кэш file_id для видео Rutube и VK

Ключ возвращался None, из-за чего чтение никогда не попадало,
а запись молча пропускалась. Повторный запрос качал файл заново."
```

---

### Task 2: Read audio cache on four platforms

Audio recording to cache already works, but it's hardcoded in four places, and no handler reads the cache before downloading. This task adds keys to a pure function, introduces a shared delivery helper, and connects it to all four handlers, replacing hardcoded calls with function invocations so that read and write keys remain aligned.
**Files:**
- Modify: `utils/platform_actions.py` (function `cache_key_for_main_action` from Task 1)
- Modify: `utils/telegram_utils.py` - add a new helper next to `_cache_format_id_for_main_action` (line 813), then four handlers: `case "tiktok_audio"` (1373), `case "instagram_audio"` (1507), `case "rutube_audio"` (1617), `case "vk_audio"` (1715)
- Create: `tests/test_cached_audio_delivery.py`
- Test: `tests/test_platform_actions.py`
**Interfaces:**
- Consumes: `cache_key_for_main_action` and `_DIRECT_VIDEO_PLATFORMS` from Task 1.
- Produces: `cache_key_for_main_action` now also returns the `action` for actions ending in `_audio`, for platforms in `_DIRECT_VIDEO_PLATFORMS`. A new coroutine `_deliver_cached_audio(query, url: str, cache_key: str) -> bool` is added in `utils/telegram_utils.py` - returns `True` if the file is delivered from cache.
> **Note for implementer:** `tests/test_dead_code_contract.py:28` checks for the absence of the `_try_send_cached` attribute. The new helper is named `_deliver_cached_audio`, which does not conflict with this check. Do not rename it to `_try_send_cached*`.
- [ ] **Step 1: Write a failing test for audio keys**
Extend `tests/test_platform_actions.py` with:

```python
def test_main_action_cache_keys_cover_audio_actions():
    assert cache_key_for_main_action("tiktok", "tiktok_audio") == "tiktok_audio"
    assert cache_key_for_main_action("instagram", "instagram_audio") == "instagram_audio"
    assert cache_key_for_main_action("rutube", "rutube_audio") == "rutube_audio"
    assert cache_key_for_main_action("vk", "vk_audio") == "vk_audio"
```

- [ ] **Step 2: Run and verify that it fails**
Run: `.venv/bin/python -m pytest tests/test_platform_actions.py::test_main_action_cache_keys_cover_audio_actions -v`
Expected: FAIL with `AssertionError: assert None == 'tiktok_audio'`
- [ ] **Step 3: Add audio branch to the pure function**
In `utils/platform_actions.py`, inside `cache_key_for_main_action`, before `return None`, add:

```python
    if platform in _DIRECT_VIDEO_PLATFORMS and action.endswith("_audio"):
        # Значение совпадает с ключами, под которыми записи уже лежат в кэше.
        return action
```

- [ ] **Step 4: Run and verify it passes**
Run: `.venv/bin/python -m pytest tests/test_platform_actions.py -v`
Expected: PASS. `cache_key_for_main_action("youtube", "audio_m4a")` remains `None` - `"youtube"` is not in `_DIRECT_VIDEO_PLATFORMS`.
- [ ] **Step 5: Commit**

```bash
git add utils/platform_actions.py tests/test_platform_actions.py
git commit -m "feat: добавить ключи кэша для аудио-действий"
```

- [ ] **Step 6: Write a failing test for the delivery helper**
Create `tests/test_cached_audio_delivery.py`:

```python
"""Тесты доставки аудио из кэша file_id."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import telegram

from utils import telegram_utils


def _query(reply_audio):
    return SimpleNamespace(message=SimpleNamespace(reply_audio=reply_audio))


@pytest.mark.unit
def test_cached_audio_is_delivered_by_file_id(monkeypatch):
    reply_audio = AsyncMock()
    monkeypatch.setattr(
        telegram_utils.telegram_cache,
        "get",
        lambda url, format_id: SimpleNamespace(file_id="AGADcached"),
    )

    delivered = asyncio.run(
        telegram_utils._deliver_cached_audio(
            _query(reply_audio), "https://example.test/v", "tiktok_audio"
        )
    )

    assert delivered is True
    assert reply_audio.await_args.kwargs["audio"] == "AGADcached"


@pytest.mark.unit
def test_missing_cache_entry_reports_not_delivered(monkeypatch):
    reply_audio = AsyncMock()
    monkeypatch.setattr(
        telegram_utils.telegram_cache, "get", lambda url, format_id: None
    )

    delivered = asyncio.run(
        telegram_utils._deliver_cached_audio(
            _query(reply_audio), "https://example.test/v", "tiktok_audio"
        )
    )

    assert delivered is False
    reply_audio.assert_not_awaited()


@pytest.mark.unit
def test_stale_file_id_is_dropped_from_cache(monkeypatch):
    """Устаревший file_id должен удаляться, чтобы не отдаваться повторно."""
    reply_audio = AsyncMock(side_effect=telegram.error.BadRequest("wrong file_id"))
    deleted: list[str] = []
    monkeypatch.setattr(
        telegram_utils.telegram_cache,
        "get",
        lambda url, format_id: SimpleNamespace(file_id="AGADstale"),
    )
    monkeypatch.setattr(
        telegram_utils.telegram_cache,
        "delete_by_file_id",
        lambda file_id: deleted.append(file_id),
    )

    delivered = asyncio.run(
        telegram_utils._deliver_cached_audio(
            _query(reply_audio), "https://example.test/v", "tiktok_audio"
        )
    )

    assert delivered is False
    assert deleted == ["AGADstale"]
```

- [ ] **Step 7: Run and verify that it fails**
Run: `.venv/bin/python -m pytest tests/test_cached_audio_delivery.py -v`
Expected: FAIL with `AttributeError: module 'utils.telegram_utils' has no attribute '_deliver_cached_audio'`
- [ ] **Step 8: Implement the helper**
In `utils/telegram_utils.py`, immediately after the function `_cache_format_id_for_main_action` (ending on line 815), add:

```python
async def _deliver_cached_audio(query, url: str, cache_key: str) -> bool:
    """Отправляет аудио из кэша по file_id.

    Returns:
        bool: True, если файл доставлен; False, если записи нет или file_id устарел.
    """
    cached = telegram_cache.get(url, format_id=cache_key)
    if not cached:
        return False

    try:
        await query.message.reply_audio(audio=cached.file_id)
    except telegram.error.BadRequest as e:
        logger.warning("file_id аудио устарел (key=%s): %s", cache_key, e)
        telegram_cache.delete_by_file_id(cached.file_id)
        return False

    logger.info("Аудио доставлено из кэша (key=%s)", cache_key)
    return True
```

- [ ] **Step 9: Run and verify it passes**
Run: `.venv/bin/python -m pytest tests/test_cached_audio_delivery.py -v`
Expected: PASS, three tests.
- [ ] **Step 10: Commit**

```bash
git add utils/telegram_utils.py tests/test_cached_audio_delivery.py
git commit -m "feat: добавить доставку аудио из кэша file_id"
```

- [ ] **Step 11: Connect cache reading to the tiktok_audio handler**
In `utils/telegram_utils.py` find `case "tiktok_audio":` (line 1373). Previously:

```python
        case "tiktok_audio":
            await safe_edit_message_text(query, DOWNLOADING_AUDIO_MESSAGE)
            from utils.tiktok_instagram_utils import download_tiktok_audio
```

It became:

```python
        case "tiktok_audio":
            cache_key = _cache_format_id_for_main_action("tiktok", "tiktok_audio")
            if cache_key and await _deliver_cached_audio(query, url, cache_key):
                await query.edit_message_text(FILE_SENT)
                await _cleanup_user_session(user_id, context, session_token)
                return

            await safe_edit_message_text(query, DOWNLOADING_AUDIO_MESSAGE)
            from utils.tiktok_instagram_utils import download_tiktok_audio
```

In the same handler, replace the hardcoded record key. Previously:

```python
                    cache_format_id="tiktok_audio",
```

It became:

```python
                    cache_format_id=cache_key,
```

- [ ] **Step 12: Repeat the same for the other three handlers**
`case "instagram_audio":` (line 1507) - insert before `await safe_edit_message_text(query, DOWNLOADING_AUDIO_MESSAGE)`:

```python
            cache_key = _cache_format_id_for_main_action(
                "instagram", "instagram_audio"
            )
            if cache_key and await _deliver_cached_audio(query, url, cache_key):
                await query.edit_message_text(FILE_SENT)
                await _cleanup_user_session(user_id, context, session_token)
                return

```

and replace `cache_format_id="instagram_audio",` with `cache_format_id=cache_key,`.
`case "rutube_audio":` (line 1617) - insert at the same position:

```python
            cache_key = _cache_format_id_for_main_action("rutube", "rutube_audio")
            if cache_key and await _deliver_cached_audio(query, url, cache_key):
                await query.edit_message_text(FILE_SENT)
                await _cleanup_user_session(user_id, context, session_token)
                return

```

and replace `cache_format_id="rutube_audio",` with `cache_format_id=cache_key,`.
`case "vk_audio":` (line 1715) - insert at the same position:

```python
            cache_key = _cache_format_id_for_main_action("vk", "vk_audio")
            if cache_key and await _deliver_cached_audio(query, url, cache_key):
                await query.edit_message_text(FILE_SENT)
                await _cleanup_user_session(user_id, context, session_token)
                return

```

and replace `cache_format_id="vk_audio",` with `cache_format_id=cache_key,`.
> Line numbers shift after each insertion. Refer to the line `case "<name>":`, not the number.
- [ ] **Step 13: Write a contract test covering all actions**
Add to `tests/test_platform_actions.py`:

```python
def test_every_main_action_has_a_cache_key():
    """Ни одно действие главного меню не должно оставаться без ключа кэша."""
    actions = [
        ("tiktok", "tiktok_download"),
        ("tiktok", "tiktok_audio"),
        ("instagram", "instagram_download"),
        ("instagram", "instagram_audio"),
        ("rutube", "rutube_download"),
        ("rutube", "rutube_audio"),
        ("vk", "vk_download"),
        ("vk", "vk_audio"),
        ("youtube", "tg_video"),
    ]

    without_key = [
        action for platform, action in actions
        if cache_key_for_main_action(platform, action) is None
    ]

    assert without_key == []
```

- [ ] **Step 14: Run and verify it passes**
Run: `.venv/bin/python -m pytest tests/test_platform_actions.py -v`
Expected: PASS. This test was written intentionally after implementation - it protects against future regressions, not drives development. Ensure it actually works: temporarily remove the audio branch from Step 3, observe the drop in uncovered actions, then restore the branch.
- [ ] **Step 15: Verify that handlers are not broken**
Run: `.venv/bin/python -m ruff check . && .venv/bin/python -m pytest -q`
Expected: `All checks passed!` and all tests green. Pay special attention to `tests/test_telegram_csi.py` and `tests/test_audit_regressions.py` - they cover `telegram_utils`.
- [ ] **Step 16: Commit**

```bash
git add utils/telegram_utils.py tests/test_platform_actions.py
git commit -m "fix: читать кэш перед скачиванием аудио на всех платформах

Запись работала, но ни один обработчик не проверял кэш,
поэтому повторный запрос всегда качал файл заново."
```

---

### Task 3: One-time expansion of a short link via TikTok's fast path

`download_tiktok_video` expands the link on line 1417, then calls `download_tiktok_video_fast` (line 1434), which expands it again on line 476. The same pattern applies to the audio pair: lines 1914 and 503. Each expansion involves an HTTP request to TikTok.
**Files:**
- Modify: `utils/tiktok_instagram_utils.py:462-486` (`download_tiktok_video_fast`)
- Modify: `utils/tiktok_instagram_utils.py:488-520` (`download_tiktok_audio_fast`)
- Modify: `utils/tiktok_instagram_utils.py:1434` and `:1923` (call sites)
- Test: `tests/test_tiktok_fast_download.py`
**Interfaces:**
- Consumes: `download_tiktok_video_fast`, `download_tiktok_audio_fast`, `fetch_tiktok_fast_media` from current code.
- Produces: Both fast-path functions now accept an additional named parameter `resolved_url: str | None = None`. If provided, `_resolve_tiktok_url` is not called. Signatures: `download_tiktok_video_fast(url: str, session_id: str, output_dir: Path | None = None, force_local: bool = False, resolved_url: str | None = None) -> Path` and similarly for `download_tiktok_audio_fast`.
- [ ] **Step 1: Write a failing test**
Add to `tests/test_tiktok_fast_download.py`:

```python
@pytest.mark.unit
def test_fast_video_reuses_already_resolved_url(monkeypatch, tmp_path):
    """Ссылку уже развернул вызывающий код — повторный запрос лишний."""
    resolves: list[str] = []

    def _resolve(url):
        resolves.append(url)
        return "https://www.tiktok.com/@tester/video/1"

    monkeypatch.setattr(tiktok_instagram_utils, "_resolve_tiktok_url", _resolve)
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "_download_remote_file",
        lambda url, destination, referer=None: (
            destination.write_bytes(b"media") or destination
        ),
    )
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "_call_tiktok_resolver",
        lambda url: _resolver_payload(),
    )

    tiktok_instagram_utils.download_tiktok_video_fast(
        "https://vt.tiktok.com/short/",
        "session-reuse",
        output_dir=tmp_path,
        resolved_url="https://www.tiktok.com/@tester/video/1",
    )

    assert resolves == []


@pytest.mark.unit
def test_download_tiktok_video_resolves_url_once(monkeypatch, tmp_path):
    """На одну доставку должно приходиться одно развёртывание ссылки."""
    resolves: list[str] = []

    def _resolve(url):
        resolves.append(url)
        return "https://www.tiktok.com/@tester/video/1"

    monkeypatch.setattr(tiktok_instagram_utils, "_resolve_tiktok_url", _resolve)
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "_download_remote_file",
        lambda url, destination, referer=None: (
            destination.write_bytes(b"media") or destination
        ),
    )
    monkeypatch.setattr(
        tiktok_instagram_utils,
        "_call_tiktok_resolver",
        lambda url: _resolver_payload(),
    )
    monkeypatch.setattr(
        tiktok_instagram_utils, "TIKTOK_FAST_PATH", True, raising=False
    )

    tiktok_instagram_utils.download_tiktok_video(
        "https://vt.tiktok.com/short/", "session-once", output_dir=tmp_path
    )

    assert len(resolves) == 1
```

- [ ] **Step 2: Run and verify that tests fail**
Run: `.venv/bin/python -m pytest tests/test_tiktok_fast_download.py -v -k "reuses_already_resolved or resolves_url_once"`
Expected: first test fails with `TypeError: download_tiktok_video_fast() got an unexpected keyword argument 'resolved_url'`; second test fails with `assert 2 == 1`.
- [ ] **Step 3: Add the parameter to the fast-path function**
In `utils/tiktok_instagram_utils.py`, modify the signature and first line of the body of `download_tiktok_video_fast`.
Previously:

```python
def download_tiktok_video_fast(
    url: str,
    session_id: str,
    output_dir: Path | None = None,
    force_local: bool = False,
) -> Path:
```

It became:

```python
def download_tiktok_video_fast(
    url: str,
    session_id: str,
    output_dir: Path | None = None,
    force_local: bool = False,
    resolved_url: str | None = None,
) -> Path:
```

Inside the same function there was:

```python
    resolved_url = _resolve_tiktok_url(url)
    media = fetch_tiktok_fast_media(resolved_url)
```

It became:

```python
    media = fetch_tiktok_fast_media(resolved_url or _resolve_tiktok_url(url))
```

Exactly the same two patches apply to `download_tiktok_audio_fast`.
- [ ] **Step 4: Pass the expanded URL from the calling code**
In `download_tiktok_video` (at the call location on line 1434) it was:

```python
            return download_tiktok_video_fast(
                url, session_id, output_dir, force_local
            )
```

It became:

```python
            return download_tiktok_video_fast(
                url, session_id, output_dir, force_local, resolved_url=resolved_url
            )
```

In `download_tiktok_audio` (line 1923) was:

```python
            return download_tiktok_audio_fast(
                url, session_id, output_dir, force_local
            )
```

It became:

```python
            return download_tiktok_audio_fast(
                url, session_id, output_dir, force_local, resolved_url=resolved_url
            )
```

Both calling functions already have a local variable `resolved_url` - in `download_tiktok_video` it is defined on line 1417, and in `download_tiktok_audio` on line 1914.
- [ ] **Step 5: Run and verify that tests pass**
Run: `.venv/bin/python -m pytest tests/test_tiktok_fast_download.py -v`
Expected: PASS, eight tests from the file.
- [ ] **Step 6: Run the full suite and linter**
Run: `.venv/bin/python -m ruff check . && .venv/bin/python -m coverage run --branch -m pytest tests/ -q && .venv/bin/python -m coverage report --fail-under=40`
Expected: linter clean, all tests green, coverage at least 40%.
- [ ] **Step 7: Live check (optional, requires network)**
Run:

```bash
.venv/bin/python - <<'PY'
from utils import tiktok_instagram_utils as t
calls = []
orig = t._resolve_tiktok_url
t._resolve_tiktok_url = lambda u: (calls.append(u), orig(u))[1]
p = t.download_tiktok_video_fast("<PUBLIC_TIKTOK_SHORT_URL>", "live-check")
print(f"развёртываний: {len(calls)} | файл: {p.stat().st_size} б")
p.unlink(missing_ok=True)
PY
```

Replace `<PUBLIC_TIKTOK_SHORT_URL>` with a public link you are allowed to test. Expected: `deployments: 1`. The link may expire - in case of resolver error, get a fresh one.
- [ ] **Step 8: Commit**

```bash
git add utils/tiktok_instagram_utils.py tests/test_tiktok_fast_download.py
git commit -m "perf: не разворачивать короткую ссылку TikTok повторно

Быстрый путь разворачивал ссылку заново после вызывающего кода,
что давало лишний HTTP-запрос на каждую доставку."
```

---

## Outside the scope of this plan

Intentionally excluded with reasons:
- **Cache check in `process_url` before metadata request.** Saves time waiting, but saves little disk or network: it only avoids downloading, not fetching metadata. Moreover, it requires a product solution - either delivering the file immediately (user loses choice between video and audio) or building a menu from cached `title`/`duration`, which would be insufficient for format submenus in YouTube.
- **Replacing yt-dlp with a resolver in `get_tiktok_info`.** Would eliminate full extraction via yt-dlp during the card phase, but `get_available_formats_tiktok(video_info)` is called from `utils/telegram_utils.py:1100` and relies on yt-dlp’s response structure. A separate parser for what exactly the dictionary consumes would be needed.
- **Passing URL directly to `sendVideo`.** Blocked due to unverified risk: it remains unclear whether the `file_id` obtained from the cloud Bot API remains valid when accessed via the local server later. Validated through a live send using a real token. Additionally, the benefit exists only in cloud mode - in Docker stacks, the fetch will run its own container.
- **tmpfs under `TEMP_DIR`.** Based on measured load (~25 requests per day, ~200 GB of writes per year against an SSD with 300–600 TB capacity), disk wear is less than 0.1% per year. The complexity of Compose and risk of memory shortage are not justified.
- **Repeated `extract_info` in fallback path `download_tiktok_audio`.** Affects a 140-line function that runs only when the resolver is unavailable; without the ability to reproduce failure live, the risk of regression exceeds the benefit.

## Self-review

**Coverage of identified defects:** Rutube and VK videos - Task 1. Audio on four platforms - Task 2. Duplicate link expansion - Task 3. All six non-working actions from the "Current Status" table are closed; the seventh identified item (repeated `extract_info`) is moved to the "Outside Scope" section with justification.
**Placeholders:** Each step with code includes the full code, including "before/after" blocks. Expected error messages in the check steps are stated verbatim.
**Consistency of names and types:** `cache_key_for_main_action` and `_DIRECT_VIDEO_PLATFORMS` are declared in Task 1 and extended in Task 2 under the same names. `_deliver_cached_audio` is declared in Task 2 Step 8 and used in Steps 11–12 with the same signature. `resolved_url` is a parameter name in Task 3 in all four locations. Audio key values match already stored strings, so existing cache entries will not become orphaned.
