# Quality, CI/CD and FSM Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.
**Goal:** Eliminate eight confirmed audit risks: incomplete test suite in CI/CD, lack of coverage control, flawed FSM session lifecycle, non-reproducible yt-dlp, unreliable release order, dead code, monolithic Telegram handlers, and missing WebUI/FFmpeg/Docker smoke tests.
**Architecture:** Behavior is fixed by tests before any production code changes. FSM receives typed event parsing and has a separate session storage module; platform-level solutions, public errors, and low-level delivery are decoupled from the Telegram dispatcher. CI becomes the primary verification loop, and CD publishes releases only after successful image build and push.
**Tech Stack:** Python 3.14, pytest 9.1.1, coverage.py 7.15.2, ruff, python-telegram-bot 22.8, FastAPI 0.139.2, Docker Compose, GitHub Actions.

## Global Constraints

- Running `pytest tests/` must execute all local networked tests.
- Minimum line and branch coverage on the first phase is 40%.
- `YTDLP_AUTO_UPDATE` is disabled by default; enabling remains an explicit configuration.
- Delivery failure preserves the FSM session for the "Back" button, but clears temporary files of the current attempt.
- A GitHub Release is created only after a successful Docker image push.
- Release tag must follow `vMAJOR.MINOR.PATCH` format with an optional semver pre-release suffix.
- User messages remain in `messages.py`.
- No `print()` statements are added to production code.

---

### Task 1: Complete test suite and coverage control

**Files:**
- Modify: `requirements.txt`
- Create: `.coveragerc`
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/release.yml`
- Create: `tests/test_workflow_quality_gates.py`
**Interfaces:**
- Consumes: existing `pytest tests/`.
- Produces: a unified command `coverage run --branch -m pytest tests/ && coverage report --fail-under=40`.
- [ ] **Step 1: Write failing workflow tests**

```python
def test_ci_runs_full_suite_with_coverage():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert '-m "syntax or unit"' not in workflow
    assert "coverage run --branch -m pytest tests/" in workflow
    assert "coverage report --fail-under=40" in workflow


def test_release_runs_full_suite_with_coverage_and_ruff():
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    assert '-m "syntax or unit"' not in workflow
    assert "ruff check" in workflow
    assert "coverage report --fail-under=40" in workflow
```

- [ ] **Step 2: Verify RED**
Run:
`pytest tests/test_workflow_quality_gates.py -v`
Expected: FAIL because workflows use the marker filter and have no coverage gate.
- [ ] **Step 3: Add coverage configuration and commands**
Add `coverage==7.15.2` to test dependencies. Configure source packages
`main`, `config`, `messages`, `utils`, `web`, branch coverage, and omit tests.
Replace both filtered pytest commands with the full coverage command and report.
- [ ] **Step 4: Verify GREEN**
Run:
`coverage run --branch -m pytest tests/ && coverage report --fail-under=40`
Expected: all tests pass and total coverage is at least 40%.

### Task 2: Correct FSM session lifecycle

**Files:**
- Modify: `tests/test_audit_regressions.py`
- Modify: `utils/telegram_utils.py`
**Interfaces:**
- Consumes: `_cleanup_user_session()` and `cleanup_temp_files(session_id)`.
- Produces: failed delivery retains `_get_session(context, token)`; successful
  delivery removes it.
- [ ] **Step 1: Replace the incorrect failure test**

```python
def test_send_file_keeps_session_but_cleans_media_on_send_failure(monkeypatch):
    query = _DummyQuery()
    context = SimpleNamespace(user_data={})
    cleaned_session_ids = []
    session_token = telegram_utils._store_session(
        context,
        url="https://example.com/1",
        video_info={"title": "One"},
        session_id="session-1",
        platform="youtube",
        formats={"combined": []},
    )
    session_data = telegram_utils._get_session(context, session_token)

    async def fake_send_single_file(*args, **kwargs):
        return False

    monkeypatch.setattr(telegram_utils, "send_single_file", fake_send_single_file)
    monkeypatch.setattr(
        telegram_utils,
        "cleanup_temp_files",
        lambda session_id: cleaned_session_ids.append(session_id),
    )

    asyncio.run(
        telegram_utils.send_file(
            query, Path("fake.mp4"), session_token, session_data, context
        )
    )

    assert telegram_utils._get_session(context, session_token) is session_data
    assert cleaned_session_ids == ["session-1"]
```

Add a separate success test asserting the session is removed.
- [ ] **Step 2: Verify RED**
Run the two focused tests. Expected: failure test reports that the session was
removed.
- [ ] **Step 3: Implement outcome-aware cleanup**
Track delivery success. On success call `_cleanup_user_session`; on failure call
only `cleanup_temp_files(session_id)`. Apply the same rule to photo-post errors.
- [ ] **Step 4: Verify GREEN**
Run focused FSM tests and all `test_audit_regressions.py`.

### Task 3: Reproducible yt-dlp runtime

**Files:**
- Modify: `tests/test_local_bot_api_config.py`
- Modify: `config.py`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `docs/guides/configuration.md`
- Modify: `docs/PRD.md`
**Interfaces:**
- Produces: `YTDLP_AUTO_UPDATE is False` when the environment variable is absent.
- [ ] **Step 1: Write failing default test**
Reload `config` without `YTDLP_AUTO_UPDATE` and assert the value is `False`.
- [ ] **Step 2: Verify RED**
Run focused config test. Expected: actual value is `True`.
- [ ] **Step 3: Change the default and documentation**
Set
`YTDLP_AUTO_UPDATE = _parse_bool(os.environ.get("YTDLP_AUTO_UPDATE"), default=False)`,
set `YTDLP_AUTO_UPDATE=false` in the
template, and document explicit opt-in to nightly updates.
- [ ] **Step 4: Verify GREEN**
Run config, environment-template and yt-dlp runtime tests.

### Task 4: Atomic CD and validated release tags

**Files:**
- Modify: `tests/test_workflow_quality_gates.py`
- Modify: `.github/workflows/release.yml`
**Interfaces:**
- Produces: job dependency chain `test -> docker -> release`.
- [ ] **Step 1: Write failing release-order tests**

```python
def test_release_is_published_after_image():
    workflow = RELEASE.read_text()
    assert "docker:\n" in workflow
    assert "needs: [test]" in docker_job
    assert "release:\n" in workflow
    assert "needs: [docker]" in release_job
    assert "VALID_TAG_REGEX" in workflow
```

- [ ] **Step 2: Verify RED**
Expected: current dependency chain is `test -> release -> docker`.
- [ ] **Step 3: Reorder jobs and validate tags**
Validate `$GITHUB_REF_NAME` before tests. Make Docker depend on tests and
release depend on Docker. Preserve semver image tags and changelog generation.
- [ ] **Step 4: Verify GREEN**
Run workflow structural tests and `docker compose config --quiet`.

### Task 5: Remove confirmed dead code

**Files:**
- Modify: `messages.py`
- Modify: `utils/telegram_utils.py`
- Modify: `utils/video_cache.py`
- Modify: `tests/test_audit_regressions.py`
- Modify: `tests/conftest.py`
- Modify: `pytest.ini`
**Interfaces:**
- Removes: `_try_send_cached`, `CachedVideo.to_dict`, seven unused message
  constants, unused `--run-slow`/`--run-network` hooks.
- [ ] **Step 1: Add an AST regression test**
Assert removed public names are absent from their modules and source files.
- [ ] **Step 2: Verify RED**
Expected: constants, method and helper are still present.
- [ ] **Step 3: Remove definitions and obsolete test scaffolding**
Delete only symbols confirmed by `rg` and vulture. Keep FastAPI route functions,
dataclass fields and decorator registrations.
- [ ] **Step 4: Verify GREEN**
Run dead-code regression test, ruff and vulture at 80% confidence.

### Task 6: Extract the FSM, platform and delivery boundaries

**Files:**
- Create: `utils/callback_fsm.py`
- Create: `utils/platform_actions.py`
- Create: `utils/file_delivery.py`
- Create: `utils/public_errors.py`
- Modify: `utils/telegram_utils.py`
- Create: `tests/test_callback_fsm.py`
- Create: `tests/test_platform_actions.py`
- Create: `tests/test_file_delivery.py`
- Create: `tests/test_public_errors.py`
**Interfaces:**
- Produces:
  - `CallbackEvent.parse(data: str) -> CallbackEvent | None`;
  - `SessionStore` with `create`, `get`, `remove`;
  - `cache_key_for_main_action(platform, action)`;
  - `cache_key_for_format_selection(content_type, format_id)`;
  - `media_kind_for_suffix(suffix) -> Literal["video", "audio", "document"]`;
  - pure public error classification/building functions.
- [ ] **Step 1: Write unit tests for each extracted pure interface**
Cover valid and invalid callback shapes, five-session eviction, cache key
mapping, file-kind mapping and safe platform error text.
- [ ] **Step 2: Verify RED**
Expected: imports fail because the modules do not exist.
- [ ] **Step 3: Implement pure modules and delegate from telegram_utils**
Move code without changing public Telegram handler signatures. Keep temporary
compatibility imports only where tests import an old private name.
- [ ] **Step 4: Verify GREEN**
Run new tests plus all Telegram regression tests.

### Task 7: WebUI and FFmpeg tests

**Files:**
- Create: `tests/test_web_smoke.py`
- Create: `tests/test_media_processor_unit.py`
- Modify: `web/app.py` only if tests expose an actual defect.
**Interfaces:**
- Consumes: FastAPI `app`, `/health`, media processor subprocess functions.
- [ ] **Step 1: Write WebUI smoke tests**
Use `TestClient` to verify `/health`, unauthenticated dashboard redirect and a
valid login session against temporary databases.
- [ ] **Step 2: Write FFmpeg unit tests**
Mock subprocess boundaries and test codec detection, missing ffprobe, audio
stream detection, size-limit rejection and output-path validation.
- [ ] **Step 3: Verify RED or characterize existing behavior**
Every defect-reproducing test must fail for the expected reason. Tests that
characterize already-correct external boundaries may pass immediately and do
not authorize a production change.
- [ ] **Step 4: Implement only exposed fixes and verify GREEN**
Run both new test files and remeasure module coverage.

### Task 8: Docker smoke checks and final verification

**Files:**
- Modify: `tests/test_workflow_quality_gates.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/release.yml`
- Modify: `README.md`
- Modify: `AGENTS.md`
**Interfaces:**
- Produces: CI smoke commands for WebUI health and Telegram Bot API executable.
- [ ] **Step 1: Write failing workflow smoke assertions**
Require an image import check, WebUI container health check with bounded retry,
and `telegram-bot-api --version`.
- [ ] **Step 2: Verify RED**
Expected: current workflow only builds images.
- [ ] **Step 3: Add bounded Docker smoke commands**
Start `nuvio:test` as WebUI, poll `/health`, inspect logs on failure, stop the
container in an `always()` cleanup step, and execute the local API version.
- [ ] **Step 4: Run final verification**
Run:

```bash
ruff check .
coverage erase
coverage run --branch -m pytest tests/
coverage report --fail-under=40
docker compose -f compose.yaml config --quiet
docker build -t nuvio:test .
docker build -f Dockerfile.telegram-bot-api -t nuvio-telegram-bot-api:test .
```

Review `git diff --check`, vulture output and all eight requirements before
committing.
