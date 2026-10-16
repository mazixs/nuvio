# GitHub Actions DevOps Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
**Goal:** Restore CI to a green state and make container builds reproducible, minimally privileged, and validating the same artifact that is published to GHCR.
**Architecture:** CI uses a single fixed set of development tools, validates workflows via `actionlint`, and builds containers using Buildx with GHA caching. Releases first publish the image only by digest, run a smoke test on that digest, and only then create user-defined tags and a GitHub Release.
**Tech Stack:** GitHub Actions, Ruff 0.16.0, pytest/coverage, Docker Buildx, GHCR, actionlint 1.7.12.

## Global Constraints

- The workflow runs on the current `main` branch explicitly as requested by the user.
- The runtime image does not include pytest, coverage, or Ruff.
- All external GitHub Actions are pinned to a full commit SHA.
- `GITHUB_TOKEN` permissions are granted to each job individually on a principle of minimal privilege.
- Releases are allowed only for semver tags whose commit is part of `origin/main`.
- Smoke testing is performed on the same digest that later receives GHCR tags.

---

### Task 1: Reproducible Development Tools

**Files:**
- Create: `requirements-dev.txt`
- Modify: `requirements.txt`
- Modify: `pyproject.toml`
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/release.yml`
- Test: `tests/test_workflow_quality_gates.py`

**Interfaces:**
- Consumes: current set of runtime dependencies.
- Produces: a single command `python -m pip install --requirement requirements-dev.txt` and a fixed linting policy.

- [ ] **Step 1: Add failing checks**
Verify that Ruff is pinned to `ruff==0.16.0`, pytest/coverage are present only in the dev file, and the workflow does not run `pip install ruff` without a version.

- [ ] **Step 2: Ensure correct RED**
Run: `.venv/bin/pytest tests/test_workflow_quality_gates.py -q`
Expected: FAIL due to absence of `requirements-dev.txt` and unpinning of Ruff.

- [ ] **Step 3: Separate runtime and dev dependencies and pin linting policy**
Create `requirements-dev.txt`, move pytest/coverage into it, and add Ruff 0.16.0. In `pyproject.toml`, explicitly set the previous linting rules: `E4`, `E7`, `E9`, `F`.

- [ ] **Step 4: Verify GREEN**
Run: `.venv/bin/python -m pip install --requirement requirements-dev.txt && .venv/bin/ruff check . && .venv/bin/pytest tests/test_workflow_quality_gates.py -q`
Expected: PASS.

---

### Task 2: CI Hardening

**Files:**
- Modify: `.github/workflows/ci.yml`
- Test: `tests/test_workflow_quality_gates.py`

**Interfaces:**
- Consumes: `requirements-dev.txt`.
- Produces: minimal permissions, concurrency, timeouts, actionlint, and Buildx cache.

- [ ] **Step 1: Add failing contract checks**
Verify the following:
- `permissions: contents: read`
- Concurrency settings
- `timeout-minutes`
- PRs for `develop` branch
- External Actions pinned by SHA
- actionlint validation by digest
- GHA Buildx cache enabled

- [ ] **Step 2: Ensure correct RED**
Run: `.venv/bin/pytest tests/test_workflow_quality_gates.py -q`
Expected: FAIL due to missing DevOps safeguards.

- [ ] **Step 3: Implement minimal CI**
Pin all Actions to a full commit SHA, add actionlint, set timeouts and concurrency, and replace `docker build` with Buildx using `load: true`, `cache-from`, and `cache-to`.

- [ ] **Step 4: Verify GREEN and actionlint**
Run: `.venv/bin/pytest tests/test_workflow_quality_gates.py -q`
Run: `docker run --rm -v "$PWD:/repo:ro" -w /repo rhysd/actionlint@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667 -color .github/workflows/*.yml`
Expected: PASS without warnings.

### Task 3: Release pipeline under verification

**Files:**
- Modify: `.github/workflows/release.yml`
- Test: `tests/test_workflow_quality_gates.py`

**Interfaces:**
- Consumes: semver tag and a single Nuvio Dockerfile.
- Produces: canonical image digest, verified smoke test before tag creation.

- [ ] **Step 1: Add failing checks**
  Verify job-level permissions, `production` environment, tag reachability from `origin/main`, build-by-digest, smoke test by digest, and tag creation via `imagetools create`.

- [ ] **Step 2: Ensure correct RED**
  Run: `.venv/bin/pytest tests/test_workflow_quality_gates.py -q`
  Expected: FAIL on rebuild and global write permissions.

- [ ] **Step 3: Rebuild the release pipeline**
  Build and publish the canonical digest with provenance/SBOM, verify the digest, then create semver tags. Grant `packages: write` only to the Docker job and `contents: write` only to the release job.

- [ ] **Step 4: Verify GREEN**
  Run: `.venv/bin/pytest tests/test_workflow_quality_gates.py -q`
  Run: actionlint for both workflows.
  Expected: PASS.

### Task 4: Reproducible Docker foundations and documentation

**Files:**
- Modify: `Dockerfile`
- Modify: `Dockerfile.telegram-bot-api`
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `docs/PRD.md`
- Modify: `docs/development/contributing.md`
- Test: `tests/test_environment_template.py`
- Test: `tests/test_workflow_quality_gates.py`

**Interfaces:**
- Consumes: verified digests of current base images.
- Produces: reproducible Docker FROM statements and up-to-date development commands.

- [ ] **Step 1: Add failing digest checks**
  Verify that both base images use `@sha256:`.

- [ ] **Step 2: Lock base images and update instructions**
  Lock Python and Debian base images by digest, replace development `requirements.txt` with `requirements-dev.txt`, and document release-by-digest.

- [ ] **Step 3: Perform full validation**
  Run: `.venv/bin/ruff check .`
  Run: `.venv/bin/python -m coverage erase && .venv/bin/python -m coverage run --branch -m pytest tests/ && .venv/bin/python -m coverage report --fail-under=40`
  Run: actionlint, Compose config, and build both Docker images.
  Expected: All commands exit with code 0.

### Task 5: GitHub settings

**Files:**
- External: repository Actions permissions
- External: `main` branch protection/ruleset
- External: `production` environment

**Interfaces:**
- Consumes: successfully published workflow with existing check names.
- Produces: mandatory CI checks, prohibition of dangerous direct changes, and restrictions on Actions.

- [ ] **Step 1: After push, wait for green CI**
  Run: `gh run watch <run-id> --repo mazixs/nuvio --exit-status`
  Expected: success.

- [ ] **Step 2: Enable SHA pinning and restrict allowed Actions**
  Allow GitHub-owned Actions, `docker/*`, and `softprops/action-gh-release`, requiring full SHA.

- [ ] **Step 3: Protect the main branch**
  Require PR and successful lint, tests, and Docker checks; prohibit force-push and deletion.

- [ ] **Step 4: Configure the production environment**
  Create the environment and restrict releases to protected tags after verifying available API rules.
