"""Регрессионные тесты качества CI/CD."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
DEPENDABOT_CONFIG = ROOT / ".github" / "dependabot.yml"
RUNTIME_REQUIREMENTS = ROOT / "requirements.txt"
DEV_REQUIREMENTS = ROOT / "requirements-dev.txt"
DEV_INPUT = ROOT / "requirements-dev.in"
RUFF_CONFIG = ROOT / "pyproject.toml"
DOCKERFILE = ROOT / "Dockerfile"
BOT_API_DOCKERFILE = ROOT / "Dockerfile.telegram-bot-api"
COMPOSE_FILE = ROOT / "compose.yaml"
RELEASE_NOTES_SCRIPT = ROOT / "scripts" / "release_notes.py"


def _job_body(workflow: str, job_name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(job_name)}:\n(?P<body>.*?)(?=^  [a-z][a-z0-9_-]*:\n|\Z)",
        workflow,
    )
    assert match, f"Задание {job_name!r} не найдено"
    return match.group("body")


def test_ci_runs_full_suite_with_coverage():
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert '-m "syntax or unit"' not in workflow
    assert "pip install ruff" not in workflow
    assert "requirements-dev.txt" in workflow
    assert "coverage run --branch -m pytest tests/" in workflow
    assert "coverage report --fail-under=70" in workflow


def test_release_runs_full_suite_with_coverage_and_ruff():
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert '-m "syntax or unit"' not in workflow
    assert "pip install ruff" not in workflow
    assert "requirements-dev.txt" in workflow
    assert "ruff check --output-format=github ." in workflow
    assert "coverage run --branch -m pytest tests/" in workflow
    assert "coverage report --fail-under=70" in workflow


def test_release_is_published_only_after_image():
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    docker_job = _job_body(workflow, "docker")
    release_job = _job_body(workflow, "release")

    assert "needs: [test]" in docker_job
    assert "needs: [docker]" in release_job


def test_release_validates_semver_tag():
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    test_job = _job_body(workflow, "test")

    assert "VALID_TAG_REGEX" in workflow
    assert "GITHUB_REF_NAME" in test_job


def test_ci_smoke_tests_built_images():
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "docker run --detach --name nuvio-web-smoke" in workflow
    assert "http://127.0.0.1:18080/health" in workflow
    assert "docker rm --force nuvio-web-smoke" in workflow
    assert "if: always()" in workflow
    assert "nuvio-telegram-bot-api:test --version" in workflow


def test_youtube_smoke_only_tolerates_runner_antibot_challenge():
    workflow = (ROOT / ".github" / "workflows" / "youtube-smoke.yml").read_text(
        encoding="utf-8"
    )

    assert "python -m scripts.check_media_runtime --download" in workflow
    assert 'grep -Fq "Sign in to confirm"' in workflow
    assert 'exit "$result"' in workflow


def test_release_smoke_tests_application_before_push():
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    docker_job = _job_body(workflow, "docker")

    assert "docker build -t nuvio:release-test ." not in docker_job
    assert "push-by-digest=true" in docker_job
    assert "name-canonical=true" in docker_job
    assert "docker run --detach --name nuvio-release-smoke" in docker_job
    assert '"$REGISTRY_IMAGE@$IMAGE_DIGEST"' in docker_job
    assert "http://127.0.0.1:18080/health" in docker_job
    assert "docker rm --force nuvio-release-smoke" in docker_job
    assert "docker buildx imagetools create" in docker_job


def test_dev_tools_are_pinned_and_not_installed_in_runtime_image():
    runtime = RUNTIME_REQUIREMENTS.read_text(encoding="utf-8")
    dev = DEV_REQUIREMENTS.read_text(encoding="utf-8")
    dev_input = DEV_INPUT.read_text(encoding="utf-8")
    ruff_config = RUFF_CONFIG.read_text(encoding="utf-8")

    for tool in ("pytest", "coverage", "ruff"):
        assert re.search(rf"^{tool}==", runtime, re.MULTILINE) is None
        pin = re.search(rf"^{tool}==([^\s]+)$", dev_input, re.MULTILINE)
        assert pin, f"{tool} не закреплен в requirements-dev.in"
        assert re.search(
            rf"^{tool}=={re.escape(pin.group(1))}\s", dev, re.MULTILINE
        ), f"Версия {tool} не совпадает с dev lock-файлом"
    assert "--hash=sha256:" in runtime
    assert "--hash=sha256:" in dev
    assert 'select = ["E4", "E7", "E9", "F"]' in ruff_config


def test_all_external_actions_are_pinned_to_full_commit_sha():
    for path in (CI_WORKFLOW, RELEASE_WORKFLOW):
        workflow = path.read_text(encoding="utf-8")
        action_refs = re.findall(r"uses:\s+[^@\s]+@([^\s#]+)", workflow)

        assert action_refs, f"В {path.name} не найдены внешние Actions"
        assert all(
            re.fullmatch(r"[0-9a-f]{40}", ref) for ref in action_refs
        ), f"В {path.name} есть Action без полного commit SHA: {action_refs}"


def test_ci_has_least_privilege_concurrency_timeouts_and_actionlint():
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "permissions:\n  contents: read" in workflow
    assert "concurrency:" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "branches: [main, develop]" in workflow
    assert workflow.count("timeout-minutes:") == 3
    assert "rhysd/actionlint@sha256:" in workflow


def test_ci_uses_buildx_cache_for_both_images():
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "docker/setup-buildx-action@" in workflow
    assert workflow.count("docker/build-push-action@") == 2
    assert workflow.count("cache-from: type=gha") == 2
    assert workflow.count("cache-to: type=gha") == 2
    assert workflow.count("load: true") == 2


def test_ci_and_release_scan_images_with_pinned_trivy():
    ci = CI_WORKFLOW.read_text(encoding="utf-8")
    release = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    for workflow in (ci, release):
        assert "aquasec/trivy@sha256:" in workflow
        assert "--exit-code 1" in workflow
        assert "--severity HIGH,CRITICAL" in workflow

    assert "nuvio:test" in ci
    assert "nuvio-telegram-bot-api:test" in ci
    assert '"$REGISTRY_IMAGE@$IMAGE_DIGEST"' in release


def test_release_limits_permissions_and_serializes_publication():
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    test_job = _job_body(workflow, "test")
    docker_job = _job_body(workflow, "docker")
    release_job = _job_body(workflow, "release")

    assert "permissions:\n  contents: read" in workflow
    assert "concurrency:" in workflow
    assert "group: release" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "contents: write" not in test_job
    assert "packages: write" not in test_job
    assert "packages: write" in docker_job
    assert "contents: write" not in docker_job
    assert "contents: write" in release_job
    assert "packages: write" not in release_job
    assert workflow.count("timeout-minutes:") == 3


def test_release_requires_main_commit_and_production_environment():
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    test_job = _job_body(workflow, "test")
    docker_job = _job_body(workflow, "docker")

    assert "git merge-base --is-ancestor" in test_job
    assert "origin/main" in test_job
    assert "environment:" in docker_job
    assert "name: production" in docker_job
    assert "provenance: mode=max" in docker_job
    assert "sbom: true" in docker_job


def test_release_notes_are_generated_by_the_tested_script():
    """Changelog собирается скриптом, а не `git log --grep` по всему сообщению.

    Тело коммита часто содержит слова другой категории, поэтому прежняя
    реализация относила один коммит сразу к двум разделам: на диапазоне
    v1.2.2..HEAD так двоились `feat: быстрый путь TikTok` и
    `fix: валидировать ссылки резолвера TikTok`.
    """
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    release_job = _job_body(workflow, "release")

    assert "python3 scripts/release_notes.py" in release_job
    assert "--invert-grep" not in workflow
    assert '--grep="добавл"' not in workflow
    assert RELEASE_NOTES_SCRIPT.exists()


def test_release_image_is_lowercase_and_matches_compose():
    """Docker-ref обязан быть в нижнем регистре, а compose — тянуть тот же образ.

    Имя образа закреплено отдельно от имени репозитория: оно должно оставаться
    в нижнем регистре и совпадать с `compose.yaml`. Иначе пользователь мог бы
    тянуть образ, который релиз не публикует.
    """
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    compose = COMPOSE_FILE.read_text(encoding="utf-8")

    match = re.search(r"^  REGISTRY_IMAGE: (?P<image>.+)$", workflow, re.M)
    assert match, "REGISTRY_IMAGE не найден"
    image = match.group("image").strip()

    assert "${{" not in image, f"имя образа зависит от регистра выражения: {image}"
    assert image == image.lower(), image
    assert f"image: {image}:${{TAG:-latest}}" in compose


def test_latest_tag_is_never_assigned_to_a_prerelease():
    """`compose.yaml` по умолчанию тянет latest, поэтому RC туда попасть не должен.

    VALID_TAG_REGEX допускает теги вида v1.3.0-rc.1, а `latest=auto` оставляет
    решение на усмотрение docker/metadata-action — здесь оно задано явно.
    """
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    compose = COMPOSE_FILE.read_text(encoding="utf-8")

    assert "flavor: latest=auto" not in workflow
    assert "flavor: latest=${{ !contains(github.ref_name, '-') }}" in workflow
    assert "${TAG:-latest}" in compose


def test_docker_base_images_are_pinned_by_digest():
    app_dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    bot_api_dockerfile = BOT_API_DOCKERFILE.read_text(encoding="utf-8")

    assert re.search(r"^FROM python:3\.14-slim@sha256:[0-9a-f]{64}$", app_dockerfile, re.M)
    debian_from = re.findall(
        r"^FROM debian:bookworm-slim@sha256:[0-9a-f]{64}",
        bot_api_dockerfile,
        re.M,
    )
    assert len(debian_from) == 2


def test_dependabot_groups_minor_and_patch_updates_per_ecosystem():
    config = DEPENDABOT_CONFIG.read_text(encoding="utf-8")

    assert config.count("applies-to: version-updates") == 3
    assert config.count('- "minor"') == 3
    assert config.count('- "patch"') == 3


def test_dependabot_leaves_ytdlp_to_the_update_workflow():
    """Dependabot поднимал yt-dlp без версии в документах, и тесты падали (#55)."""
    config = DEPENDABOT_CONFIG.read_text(encoding="utf-8")
    pip_block = config.split("- package-ecosystem: docker", 1)[0]

    assert "- package-ecosystem: pip" in pip_block
    assert 'ignore:\n      - dependency-name: "yt-dlp"' in pip_block
