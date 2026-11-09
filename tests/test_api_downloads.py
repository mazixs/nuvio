"""HTTP API загрузок: ключи, лимиты, владелец задачи и жизненный цикл файла."""

import asyncio
import re
import threading
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import get_args
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from api import app as api_app
from api import jobs as api_jobs
from api import schemas
from api.jobs import JobManager
from config import API_MAX_FILE_SIZE_MB
from utils import analytics_db, api_keys, media_fetch, temp_file_manager, work_budget
from utils.media_fetch import PreparedDownload

YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


class FakeDownloads:
    """Подмена загрузчика: пишет файл в каталог задачи и умеет ждать сигнала."""

    def __init__(self, media: Path):
        self.media = media
        self.gate = threading.Event()
        self.gate.set()
        self.started = threading.Event()
        self.error: BaseException | None = None
        self.prepare_error: BaseException | None = None
        self.result_override: Path | None = None
        self.calls: list[tuple[str, str, str]] = []
        self.size_limits: list[int] = []

    def prepare(self, url, platform, media_type, session_id):
        self.calls.append((url, platform, media_type))
        self.size_limits.append(work_budget.size_limit(0))
        if self.prepare_error is not None:
            raise self.prepare_error
        return PreparedDownload(lambda: self._run(media_type, session_id), timeout=30)

    def _run(self, media_type, session_id):
        self.size_limits.append(work_budget.size_limit(0))
        self.started.set()
        # Как progress hook yt-dlp: ожидание прерывается отменой сессии.
        while not self.gate.wait(0.01):
            work_budget.check()
        if self.error is not None:
            raise self.error
        if self.result_override is not None:
            return self.result_override
        folder = self.media / session_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ("track.m4a" if media_type == "audio" else "clip.mp4")
        path.write_bytes(b"media-bytes")
        return path


@pytest.fixture
def api(tmp_path, monkeypatch):
    analytics_db.close_connection()
    monkeypatch.setattr(analytics_db, "_DB_PATH", tmp_path / "analytics.db")
    media = tmp_path / "media"
    media.mkdir()
    monkeypatch.setattr(temp_file_manager, "TEMP_DIR", media)
    downloads = FakeDownloads(media)
    monkeypatch.setattr(media_fetch, "prepare_download", downloads.prepare)
    alerts = []
    monkeypatch.setattr(
        api_jobs.alerts, "submit_download_failure", lambda **kwargs: alerts.append(kwargs)
    )
    monkeypatch.setattr(
        api_app, "JobManager",
        lambda: JobManager(workers=4, max_active_per_key=2, result_ttl_seconds=3600, temp_dir=media),
    )
    api_app._rate_limiter.clear()
    api_app._auth_failures.clear()
    with TestClient(api_app.app) as client:
        key_id, key = api_keys.create_api_key("Partner", None)
        other_id, other_key = api_keys.create_api_key("Other", None)
        yield SimpleNamespace(
            client=client, media=media, downloads=downloads, jobs=api_app.app.state.jobs, alerts=alerts,
            key_id=key_id, key=key, other_id=other_id, other_key=other_key,
        )
        downloads.gate.set()
    analytics_db.close_connection()


def auth(key):
    return {"Authorization": f"Bearer {key}"}


def start(api, url=YOUTUBE_URL, media_type="video", key=None):
    return api.client.post(
        "/api/v1/downloads", json={"url": url, "type": media_type}, headers=auth(key or api.key)
    )


def wait_for(api, job_id, statuses=("succeeded", "failed", "cancelled"), key=None):
    deadline = time.monotonic() + 5
    body = None
    while time.monotonic() < deadline:
        body = api.client.get(f"/api/v1/downloads/{job_id}", headers=auth(key or api.key)).json()
        if body["status"] in statuses:
            return body
        time.sleep(0.02)
    raise AssertionError(f"задача {job_id} не дошла до {statuses}: {body}")


# ── Ключи ─────────────────────────────────────────────────────────


def test_health_needs_no_key(api):
    response = api.client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_missing_key_is_a_401_with_the_error_envelope(api):
    response = api.client.get("/api/v1/me")
    assert response.status_code == 401
    assert response.json() == {
        "error": {"code": "missing_api_key", "message": "Send the API key in the Authorization: Bearer header."}
    }
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_key_in_the_query_string_is_ignored(api):
    response = api.client.get(f"/api/v1/me?api_key={api.key}")
    assert response.json()["error"]["code"] == "missing_api_key"


@pytest.mark.parametrize("headers", ["bearer", "x-api-key"])
def test_me_describes_the_key_and_limits(api, headers):
    sent = auth(api.key) if headers == "bearer" else {"X-API-Key": api.key}
    response = api.client.get("/api/v1/me", headers=sent)
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Partner"
    assert body["key_prefix"] == api.key[:12]
    assert body["expires_at"] is None
    assert set(body["limits"]) == {"requests_per_minute", "active_downloads", "result_ttl_minutes", "max_file_size_mb"}
    assert body["limits"]["max_file_size_mb"] == API_MAX_FILE_SIZE_MB
    assert api.key not in response.text


@pytest.mark.parametrize("token", ["nuvio_bogus", "not-a-key", api_keys.generate_key()])
def test_unknown_key_is_rejected(api, token):
    response = api.client.get("/api/v1/me", headers=auth(token))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


def test_revoked_key_is_rejected_immediately(api):
    api_keys.revoke_api_key(api.key_id)
    response = api.client.get("/api/v1/me", headers=auth(api.key))
    assert response.json()["error"]["code"] == "invalid_api_key"


def test_expired_key_has_its_own_code(api, monkeypatch):
    api_keys.set_api_key_expiry(api.key_id, api_keys._now() + timedelta(days=1))
    later = api_keys._now() + timedelta(days=2)
    monkeypatch.setattr(api_keys, "_now", lambda: later)
    response = api.client.get("/api/v1/me", headers=auth(api.key))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "api_key_expired"


def test_rotated_key_in_its_grace_period_still_works(api):
    new_key = api_keys.rotate_api_key(api.key_id, grace_hours=1)
    for key in (api.key, new_key):
        assert api.client.get("/api/v1/me", headers=auth(key)).status_code == 200


def test_repeated_invalid_keys_are_throttled_but_valid_keys_pass(api):
    for _ in range(api_app.AUTH_FAILURE_LIMIT):
        assert api.client.get("/api/v1/me", headers=auth(api_keys.generate_key())).status_code == 401
    response = api.client.get("/api/v1/me", headers=auth(api_keys.generate_key()))
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "too_many_failed_attempts"
    assert int(response.headers["retry-after"]) > 0
    assert api.client.get("/api/v1/me", headers=auth(api.key)).status_code == 200


def test_blocking_is_logged_once_not_per_attempt(api, caplog):
    with caplog.at_level("WARNING"):
        for _ in range(api_app.AUTH_FAILURE_LIMIT + 5):
            api.client.get("/api/v1/me", headers=auth(api_keys.generate_key()))
    assert caplog.text.count("Много неверных API-ключей") == 1


def test_rate_limit_per_key(api, monkeypatch):
    monkeypatch.setattr(api_app, "_rate_limiter", api_app.SlidingWindowLimiter(3, 60))
    remaining = [
        api.client.get("/api/v1/me", headers=auth(api.key)).headers["x-ratelimit-remaining"]
        for _ in range(3)
    ]
    assert remaining == ["2", "1", "0"]
    response = api.client.get("/api/v1/me", headers=auth(api.key))
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"
    assert 0 < int(response.headers["retry-after"]) <= 60
    # Лимит считается на ключ: другой ключ не страдает.
    assert api.client.get("/api/v1/me", headers=auth(api.other_key)).status_code == 200


def test_sliding_window_frees_slots_over_time():
    limiter = api_app.SlidingWindowLimiter(2, 10)
    assert limiter.hit("k", now=0) == (True, 1)
    assert limiter.hit("k", now=4) == (True, 0)
    assert limiter.hit("k", now=5) == (False, 0)
    assert limiter.retry_after("k", now=5) == 5
    assert limiter.hit("k", now=10) == (True, 0)


# ── Загрузки ──────────────────────────────────────────────────────


def test_download_lifecycle(api):
    response = start(api, media_type="audio")

    assert response.status_code == 202
    job = response.json()
    assert response.headers["location"] == f"/api/v1/downloads/{job['id']}"
    assert response.headers["retry-after"] == "2"
    assert job["status"] in {"queued", "running", "succeeded"}
    assert job["platform"] == "youtube"
    assert job["type"] == "audio"
    assert api.downloads.calls == [(YOUTUBE_URL, "youtube", "audio")]

    done = wait_for(api, job["id"])
    assert done["status"] == "succeeded"
    assert done["error"] is None
    assert done["file"]["name"] == "track.m4a"
    assert done["file"]["size"] == len(b"media-bytes")
    assert done["file"]["expires_at"] is not None
    assert done["links"]["file"] == f"/api/v1/downloads/{job['id']}/file"

    file_response = api.client.get(done["links"]["file"], headers=auth(api.key))
    assert file_response.status_code == 200
    assert file_response.content == b"media-bytes"
    assert 'filename="track.m4a"' in file_response.headers["content-disposition"]
    assert api_keys.list_api_keys()[1]["downloads_total"] == 1


def test_job_passes_the_api_file_limit_to_both_steps(api):
    job_id = start(api).json()["id"]
    assert wait_for(api, job_id)["status"] == "succeeded"
    assert api.downloads.size_limits == [API_MAX_FILE_SIZE_MB * 1024 * 1024] * 2


def test_downloads_are_listed_per_key(api):
    first = start(api).json()["id"]
    second = start(api, media_type="audio").json()["id"]
    wait_for(api, first)
    wait_for(api, second)
    items = api.client.get("/api/v1/downloads", headers=auth(api.key)).json()["items"]
    assert [item["id"] for item in items] == [second, first]
    assert api.client.get("/api/v1/downloads", headers=auth(api.other_key)).json() == {"items": []}


def test_another_key_cannot_see_or_touch_a_download(api):
    job_id = start(api).json()["id"]
    wait_for(api, job_id)
    for method, path in [
        ("get", f"/api/v1/downloads/{job_id}"),
        ("get", f"/api/v1/downloads/{job_id}/file"),
        ("delete", f"/api/v1/downloads/{job_id}"),
    ]:
        response = api.client.request(method.upper(), path, headers=auth(api.other_key))
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
    assert api.client.get(f"/api/v1/downloads/{job_id}/file", headers=auth(api.key)).status_code == 200


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/watch?v=dQw4w9WgXcQ",
        "ftp://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://user:secret@www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com:8443/watch?v=dQw4w9WgXcQ",
        "https://youtube.com.evil.example/watch?v=dQw4w9WgXcQ",
        "http://169.254.169.254/latest/meta-data/",
        "file:///etc/passwd",
    ],
)
def test_unsupported_urls_are_rejected_before_any_work(api, url):
    response = start(api, url=url)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_url"
    assert api.downloads.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"url": YOUTUBE_URL, "type": "gif"},
        {"url": YOUTUBE_URL, "quality": "4k"},
        {"type": "video"},
        {"url": "x" * 2049},
    ],
)
def test_invalid_bodies_use_the_error_envelope(api, body):
    response = api.client.post("/api/v1/downloads", json=body, headers=auth(api.key))
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["details"]
    assert "x" * 100 not in response.text


def test_active_downloads_are_capped_per_key(api):
    api.downloads.gate.clear()
    first = start(api).json()["id"]
    start(api)
    response = start(api)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "too_many_active_downloads"
    assert response.headers["retry-after"] == "2"
    # Чужие задачи в этот лимит не входят.
    assert start(api, key=api.other_key).status_code == 202
    api.downloads.gate.set()
    wait_for(api, first)


def test_file_is_not_ready_while_running(api):
    api.downloads.gate.clear()
    job_id = start(api).json()["id"]
    assert api.downloads.started.wait(5)

    status = api.client.get(f"/api/v1/downloads/{job_id}", headers=auth(api.key))
    assert status.json()["status"] == "running"
    assert status.headers["retry-after"] == "2"
    response = api.client.get(f"/api/v1/downloads/{job_id}/file", headers=auth(api.key))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_ready"
    api.downloads.gate.set()
    wait_for(api, job_id)


def test_deleting_a_running_download_cancels_it(api):
    api.downloads.gate.clear()
    job_id = start(api).json()["id"]
    assert api.downloads.started.wait(5)
    job = api.jobs._jobs[job_id]

    assert api.client.delete(f"/api/v1/downloads/{job_id}", headers=auth(api.key)).status_code == 204

    job.future.result(timeout=5)
    assert api.client.get(f"/api/v1/downloads/{job_id}", headers=auth(api.key)).status_code == 404
    assert not (api.media / job.session_id).exists()
    # Отмена освобождает место в лимите активных задач.
    assert start(api).status_code == 202


def test_deleted_running_download_holds_its_slot_until_the_thread_stops(api, monkeypatch):
    stop = threading.Event()
    stopped = threading.Event()

    def run(media_type, session_id):
        api.downloads.started.set()
        # Поток не дошёл до progress hook: отмену он увидит только по сигналу.
        stop.wait(5)
        stopped.set()
        work_budget.check()
        return None

    monkeypatch.setattr(api.downloads, "_run", run)
    first = start(api).json()["id"]
    assert api.downloads.started.wait(5)
    second = start(api).json()["id"]
    assert api.client.delete(f"/api/v1/downloads/{first}", headers=auth(api.key)).status_code == 204

    # Для клиента задачи нет, но место в лимите она держит.
    assert api.client.get(f"/api/v1/downloads/{first}", headers=auth(api.key)).status_code == 404
    assert [job["id"] for job in api.client.get("/api/v1/downloads", headers=auth(api.key)).json()["items"]] == [second]
    assert start(api).status_code == 429

    job = api.jobs._jobs[first]
    stop.set()
    job.future.result(timeout=5)
    assert stopped.is_set()
    assert first not in api.jobs._jobs
    api.jobs._jobs[second].future.result(timeout=5)
    assert start(api).status_code == 202


def test_deleting_a_finished_download_removes_the_file(api):
    job_id = start(api).json()["id"]
    wait_for(api, job_id)
    folder = api.media / f"api-{job_id}"
    assert folder.exists()
    assert api.client.delete(f"/api/v1/downloads/{job_id}", headers=auth(api.key)).status_code == 204
    assert not folder.exists()
    assert api.client.delete(f"/api/v1/downloads/{job_id}", headers=auth(api.key)).status_code == 404


def test_unsupported_media_is_reported_plainly(api):
    api.downloads.prepare_error = media_fetch.UnsupportedMediaError("фото-пост")
    job_id = start(api).json()["id"]
    done = wait_for(api, job_id)
    assert done["status"] == "failed"
    assert done["error"]["code"] == "unsupported_media"
    response = api.client.get(f"/api/v1/downloads/{job_id}/file", headers=auth(api.key))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_file"


def test_unexpected_failure_hides_internals_and_gives_a_reference(api):
    api.downloads.error = RuntimeError("/app/data/cookies.txt exploded")
    done = wait_for(api, start(api).json()["id"])
    assert done["error"]["code"] == "download_failed"
    assert done["error"]["reference"].startswith("YT-UNEXPECT-")
    assert "cookies" not in str(done)


def test_unexpected_failure_notifies_admins(api):
    api.downloads.error = RuntimeError("boom")
    done = wait_for(api, start(api).json()["id"])
    assert len(api.alerts) == 1
    alert = api.alerts[0]
    assert alert["category"] == "UNEXPECT"
    assert alert["reference"] == done["error"]["reference"]
    assert alert["url"] == YOUTUBE_URL
    assert isinstance(alert["error"], RuntimeError)


def test_client_side_refusals_do_not_notify_admins(api):
    api.downloads.prepare_error = media_fetch.UnsupportedMediaError("фото-пост")
    wait_for(api, start(api).json()["id"])
    assert api.alerts == []


def test_failure_while_describing_an_error_still_finishes_the_job(api, monkeypatch):
    def broken(*args):
        raise RuntimeError("классификатор сломался")

    monkeypatch.setattr(api_jobs, "describe_failure", broken)
    api.downloads.error = RuntimeError("boom")
    job_id = start(api).json()["id"]
    api.jobs._jobs[job_id].future.exception(timeout=5)

    done = api.client.get(f"/api/v1/downloads/{job_id}", headers=auth(api.key)).json()
    assert done["status"] == "failed"
    assert done["error"]["code"] == "download_failed"
    # Задача не осталась `running` и не держит место в лимите ключа.
    assert start(api).status_code == 202
    assert start(api).status_code == 202


def test_malformed_key_is_rejected_without_a_database_lookup(api, monkeypatch):
    async def no_db(*args, **kwargs):
        raise AssertionError("запрос к базе для заведомо неверного ключа")

    monkeypatch.setattr(api_app, "run_db", no_db)
    response = api.client.get("/api/v1/me", headers=auth("nuvio_" + "A" * 38))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


def test_known_failure_category_is_mapped(api):
    api.downloads.error = Exception("ERROR: [youtube] abc: Private video. Sign in if you have been granted access")
    done = wait_for(api, start(api).json()["id"])
    assert done["error"]["code"] == "media_unavailable"


def test_a_file_outside_the_job_folder_is_never_served(api, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    api.downloads.result_override = outside
    done = wait_for(api, start(api).json()["id"])
    assert done["status"] == "failed"
    assert outside.exists()


def test_expired_result_is_gone(api):
    job_id = start(api).json()["id"]
    wait_for(api, job_id)
    api.jobs.sweep(now=time.time() + 3601)

    assert api.client.get(f"/api/v1/downloads/{job_id}", headers=auth(api.key)).json()["status"] == "expired"
    response = api.client.get(f"/api/v1/downloads/{job_id}/file", headers=auth(api.key))
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "file_expired"
    assert not (api.media / f"api-{job_id}").exists()


@pytest.mark.parametrize("chunked", [False, True])
def test_oversized_body_is_refused_before_the_key_check(api, chunked):
    body = b'{"url": "' + b"x" * api_app.MAX_BODY_BYTES + b'"}'
    content = iter([body[:4096], body[4096:]]) if chunked else body
    response = api.client.post(
        "/api/v1/downloads", content=content, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert response.headers["cache-control"] == "no-store"


def test_body_within_the_limit_is_accepted(api):
    url = YOUTUBE_URL + "&t=" + "1" * (2048 - len(YOUTUBE_URL) - 3)
    assert start(api, url=url).status_code == 202


def test_unknown_routes_use_the_error_envelope(api):
    response = api.client.get("/api/v1/nothing", headers=auth(api.key))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_trailing_slash_is_not_redirected(api, method):
    # Переадресация за TLS-прокси вела бы на http:// и уносила туда ключ.
    response = api.client.request(
        method, "/api/v1/downloads/", headers=auth(api.key), follow_redirects=False
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert "location" not in response.headers


def test_openapi_documents_the_public_routes(api):
    schema = api.client.get("/api/v1/openapi.json").json()
    assert {"/api/v1/me", "/api/v1/downloads", "/api/v1/downloads/{job_id}", "/api/v1/downloads/{job_id}/file"} <= set(schema["paths"])
    assert "/health" not in schema["paths"]
    assert api.client.get("/api/docs").status_code == 200


# ── Контракт ──────────────────────────────────────────────────────


def assert_matches(model, response):
    """Настоящий ответ укладывается в модель и не несёт лишних полей."""
    model.model_validate(response.json(), extra="forbid")


def test_job_responses_match_the_schema(api):
    created = start(api)
    assert_matches(schemas.Download, created)
    job_id = created.json()["id"]
    wait_for(api, job_id)
    assert_matches(schemas.Download, api.client.get(f"/api/v1/downloads/{job_id}", headers=auth(api.key)))
    assert_matches(schemas.DownloadList, api.client.get("/api/v1/downloads", headers=auth(api.key)))
    assert_matches(schemas.KeyInfo, api.client.get("/api/v1/me", headers=auth(api.key)))

    api.downloads.error = RuntimeError("boom")
    failed = start(api).json()["id"]
    wait_for(api, failed)
    assert_matches(schemas.Download, api.client.get(f"/api/v1/downloads/{failed}", headers=auth(api.key)))


def test_error_responses_match_the_schema(api):
    job_id = start(api).json()["id"]
    wait_for(api, job_id)
    api.jobs.sweep(now=time.time() + 3601)
    responses = [
        api.client.get("/api/v1/me"),
        api.client.get("/api/v1/me", headers=auth("nuvio_bogus")),
        api.client.get("/api/v1/nothing"),
        api.client.delete("/api/v1/me", headers=auth(api.key)),
        api.client.get(f"/api/v1/downloads/{job_id}/file", headers=auth(api.key)),
        api.client.post("/api/v1/downloads", json={"type": "gif"}, headers=auth(api.key)),
        start(api, url="https://example.com/video"),
        api.client.post("/api/v1/downloads", content=b"x" * (api_app.MAX_BODY_BYTES + 1)),
    ]
    assert [response.status_code for response in responses] == [401, 401, 404, 405, 410, 422, 422, 413]
    for response in responses:
        assert_matches(schemas.ErrorEnvelope, response)


def test_documented_error_codes_match_the_schema():
    guide = (Path(__file__).resolve().parents[1] / "docs/guides/api.md").read_text(encoding="utf-8")
    table = set(re.findall(r"^\| \d{3} \| `(\w+)` \|", guide, re.MULTILINE))
    assert table == set(get_args(schemas.ErrorCode))
    failed_jobs = guide.split("A failed job reports why in `error.code`:")[1].split(".")[0]
    assert set(re.findall(r"`(\w+)`", failed_jobs)) == set(get_args(schemas.DownloadErrorCode))


def test_every_public_failure_code_is_in_the_schema():
    codes = {code for code, _ in api_jobs._PUBLIC_FAILURES.values()} | {api_jobs._GENERIC_FAILURE[0]}
    assert codes <= set(get_args(schemas.DownloadErrorCode))


def test_detected_platforms_match_the_schema():
    assert set(media_fetch._SINGLE_MEDIA_ROUTES) == set(get_args(schemas.Platform))


def test_schema_describes_keys_headers_and_the_file(api):
    schema = api.client.get("/api/v1/openapi.json").json()
    assert set(schema["components"]["securitySchemes"]) == {"HTTPBearer", "APIKeyHeader"}
    assert "HTTPValidationError" not in schema["components"]["schemas"]
    operations = {
        operation["operationId"]
        for path in schema["paths"].values()
        for operation in path.values()
    }
    assert operations == {
        "get_me", "create_download", "list_downloads", "get_download", "get_download_file", "delete_download",
    }
    created = schema["paths"]["/api/v1/downloads"]["post"]["responses"]
    assert {"Location", "Retry-After", "X-RateLimit-Remaining"} <= set(created["202"]["headers"])
    assert created["202"]["content"]["application/json"]["schema"]["$ref"].endswith("/Download")
    assert created["422"]["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorEnvelope")
    assert "Retry-After" in created["429"]["headers"]
    file_ok = schema["paths"]["/api/v1/downloads/{job_id}/file"]["get"]["responses"]["200"]
    assert file_ok["content"]["application/octet-stream"]["schema"]["format"] == "binary"


def test_docs_page_pins_swagger_ui_and_loads_no_icon_from_outside(api):
    page = api.client.get("/api/docs").text
    assert f"swagger-ui-dist@{api_app.SWAGGER_UI_VERSION}/swagger-ui-bundle.js" in page
    assert "/api/v1/openapi.json" in page
    assert "tiangolo" not in page


def test_unexpected_failure_uses_the_error_envelope_and_hides_details(api, monkeypatch, caplog):
    def broken(key_id):
        raise RuntimeError("секретный путь /app/data")

    monkeypatch.setattr(api.jobs, "list", broken)
    client = TestClient(api_app.app, raise_server_exceptions=False)
    with caplog.at_level("ERROR"):
        response = client.get("/api/v1/downloads", headers=auth(api.key))

    assert response.status_code == 500
    assert_matches(schemas.ErrorEnvelope, response)
    assert response.json()["error"]["code"] == "internal_error"
    assert "секретный" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "секретный путь" in caplog.text


@pytest.mark.parametrize(
    ("name", "fallback"),
    [
        ("clip.mp4", "clip.mp4"),
        ("Видео про котов.mp4", "download.mp4"),
        ("Clip Видео 1.m4a", "Clip  1.m4a"),
        ('a"b\\c\r\nX-Evil: 1.mp4', "abcX-Evil 1.mp4"),
    ],
)
def test_content_disposition_gives_every_client_a_usable_name(name, fallback):
    header = api_app._content_disposition(name)
    assert f'filename="{fallback}"' in header
    assert f"filename*=UTF-8''{quote(name, safe='')}" in header
    assert "\r" not in header and "\n" not in header


def test_file_response_supports_resuming(api):
    job_id = start(api).json()["id"]
    wait_for(api, job_id)
    response = api.client.get(
        f"/api/v1/downloads/{job_id}/file", headers={**auth(api.key), "Range": "bytes=0-4"}
    )
    assert response.status_code == 206
    assert response.content == b"media"
    assert 'filename="clip.mp4"' in response.headers["content-disposition"]


# ── Зависшие воркеры ──────────────────────────────────────────────


@pytest.fixture
def hung_download(api, monkeypatch):
    """Поток игнорирует и отмену, и срок, пока тест его не отпустит."""
    release = threading.Event()

    def run(media_type, session_id):
        api.downloads.started.set()
        release.wait(10)
        folder = api.media / session_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "clip.mp4"
        path.write_bytes(b"late")
        return path

    monkeypatch.setattr(api.downloads, "_run", run)
    yield release
    release.set()


def past_the_grace_period():
    # Шаг подмены длится не дольше 30 секунд (`PreparedDownload(timeout=30)`).
    return time.monotonic() + 30 + api_jobs.STUCK_GRACE_SECONDS + 1


def test_a_download_within_its_deadline_is_left_alone(api, hung_download):
    job_id = start(api).json()["id"]
    assert api.downloads.started.wait(5)
    api.jobs.fail_stuck_jobs()
    assert api.jobs._jobs[job_id].status == "running"
    assert api.alerts == []


def test_stuck_download_fails_for_the_client_and_frees_its_slot(api, hung_download):
    job_id = start(api).json()["id"]
    start(api)
    assert api.downloads.started.wait(5)
    assert start(api).status_code == 429

    api.jobs.fail_stuck_jobs(now=past_the_grace_period())

    body = api.client.get(f"/api/v1/downloads/{job_id}", headers=auth(api.key)).json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "timeout"
    assert body["error"]["reference"].startswith("YT-STUCK-")
    assert [alert["category"] for alert in api.alerts] == ["STUCK", "STUCK"]
    assert api.alerts[0]["reference"] == body["error"]["reference"]
    assert start(api).status_code == 202


def test_a_late_thread_does_not_revive_the_job_and_removes_its_files(api, hung_download):
    job_id = start(api).json()["id"]
    assert api.downloads.started.wait(5)
    job = api.jobs._jobs[job_id]
    api.jobs.fail_stuck_jobs(now=past_the_grace_period())
    assert api.jobs._stuck_workers == 1

    hung_download.set()
    job.future.result(timeout=5)

    assert api.jobs._stuck_workers == 0
    assert api.jobs._jobs[job_id].status == "failed"
    assert api.jobs._jobs[job_id].file_path is None
    assert not (api.media / job.session_id).exists()


def test_a_deleted_download_that_hangs_still_frees_its_slot_and_record(api, hung_download):
    job_id = start(api).json()["id"]
    assert api.downloads.started.wait(5)
    job = api.jobs._jobs[job_id]
    assert api.client.delete(f"/api/v1/downloads/{job_id}", headers=auth(api.key)).status_code == 204
    api.jobs.fail_stuck_jobs(now=past_the_grace_period())
    assert api.jobs._stuck_workers == 1

    hung_download.set()
    job.future.result(timeout=5)

    assert api.jobs._stuck_workers == 0
    assert job_id not in api.jobs._jobs
    assert not (api.media / job.session_id).exists()


def test_process_exits_when_every_worker_is_stuck(api, hung_download, monkeypatch):
    exits = []
    restarts = []
    monkeypatch.setattr(api_jobs.os, "_exit", exits.append)
    monkeypatch.setattr(api_jobs.alerts, "notify_restart", restarts.append)
    start(api)
    assert api.downloads.started.wait(5)

    api.jobs.fail_stuck_jobs(now=past_the_grace_period())
    assert exits == []

    api.jobs._workers = 1
    api.downloads.started.clear()
    start(api)
    assert api.downloads.started.wait(5)
    api.jobs.fail_stuck_jobs(now=past_the_grace_period())
    assert restarts == [2]
    assert exits == [1]


def test_process_exits_even_if_the_restart_notice_fails(api, hung_download, monkeypatch):
    def broken(stuck_workers):
        raise RuntimeError("alerts down")

    exits = []
    monkeypatch.setattr(api_jobs.os, "_exit", exits.append)
    monkeypatch.setattr(api_jobs.alerts, "notify_restart", broken)
    api.jobs._workers = 1
    start(api)
    assert api.downloads.started.wait(5)
    with pytest.raises(RuntimeError):
        api.jobs.fail_stuck_jobs(now=past_the_grace_period())
    assert exits == [1]


# ── Очередь без HTTP ──────────────────────────────────────────────


def test_old_results_beyond_the_per_key_limit_expire_early(api, monkeypatch):
    monkeypatch.setattr(api_jobs, "STORED_RESULTS_PER_KEY", 2)
    ids = []
    for _ in range(3):
        job_id = start(api).json()["id"]
        wait_for(api, job_id)
        ids.append(job_id)
    statuses = [api.jobs._jobs[job_id].status for job_id in ids]
    assert statuses == ["expired", "succeeded", "succeeded"]
    assert not (api.media / f"api-{ids[0]}").exists()


def test_storage_limit_expires_the_oldest_results_of_any_key(api):
    # Каждый файл — 11 байт: два помещаются, третий вытесняет самый старый.
    api.jobs.storage_limit_bytes = 25
    other = start(api, key=api.other_key).json()["id"]
    wait_for(api, other, key=api.other_key)
    ids = []
    for _ in range(2):
        job_id = start(api).json()["id"]
        wait_for(api, job_id)
        ids.append(job_id)

    assert api.jobs._jobs[other].status == "expired"
    assert not (api.media / f"api-{other}").exists()
    assert [api.jobs._jobs[job_id].status for job_id in ids] == ["succeeded", "succeeded"]


def test_just_finished_result_survives_a_tiny_storage_limit(api):
    api.jobs.storage_limit_bytes = 1
    job_id = start(api).json()["id"]
    assert wait_for(api, job_id)["status"] == "succeeded"
    assert (api.media / f"api-{job_id}").exists()


def test_revoked_key_loses_its_jobs_and_files_on_the_next_sweep(api):
    finished = start(api, key=api.other_key).json()["id"]
    wait_for(api, finished, key=api.other_key)
    api.downloads.gate.clear()
    api.downloads.started.clear()
    running = start(api).json()["id"]
    assert api.downloads.started.wait(5)
    job = api.jobs._jobs[running]

    # Ключи отзывают в WebUI, другом процессе: API узнаёт об этом из базы.
    api_keys.revoke_api_key(api.other_id)
    api_keys.revoke_api_key(api.key_id)
    asyncio.run(api_app.sweep_once(api.jobs))

    assert api.jobs.list(api.other_id) == []
    assert not (api.media / f"api-{finished}").exists()
    assert api.jobs.list(api.key_id) == []
    # Загрузку остановила отмена через progress hook, а не открытый gate.
    job.future.result(timeout=5)
    assert running not in api.jobs._jobs
    assert not (api.media / job.session_id).exists()


def test_sweep_keeps_jobs_of_active_keys(api):
    job_id = start(api).json()["id"]
    wait_for(api, job_id)
    asyncio.run(api_app.sweep_once(api.jobs))
    assert api.client.get(f"/api/v1/downloads/{job_id}", headers=auth(api.key)).json()["status"] == "succeeded"


def test_failed_records_are_capped_per_key(api, monkeypatch):
    monkeypatch.setattr(api_jobs, "JOB_RECORDS_PER_KEY", 3)
    api.downloads.prepare_error = media_fetch.UnsupportedMediaError("фото-пост")
    ids = []
    for _ in range(5):
        job_id = start(api).json()["id"]
        wait_for(api, job_id)
        ids.append(job_id)
    items = api.client.get("/api/v1/downloads", headers=auth(api.key)).json()["items"]
    assert [item["id"] for item in items] == ids[:1:-1]


def test_file_past_its_expiry_is_gone_before_the_sweep(api):
    job_id = start(api).json()["id"]
    wait_for(api, job_id)
    api.jobs._jobs[job_id].finished_at -= 3601
    response = api.client.get(f"/api/v1/downloads/{job_id}/file", headers=auth(api.key))
    assert response.status_code == 410


def test_finished_records_are_forgotten_after_a_day(api):
    job_id = start(api).json()["id"]
    wait_for(api, job_id)
    api.jobs.sweep(now=time.time() + api_jobs.JOB_RECORD_TTL_SECONDS + 1)
    assert api.client.get(f"/api/v1/downloads/{job_id}", headers=auth(api.key)).status_code == 404


def test_startup_purges_only_api_folders(tmp_path):
    (tmp_path / "api-old").mkdir()
    (tmp_path / "api-old" / "clip.mp4").write_bytes(b"x")
    (tmp_path / "bot-session").mkdir()
    manager = JobManager(workers=1, temp_dir=tmp_path)
    try:
        manager.purge_orphans()
    finally:
        manager.shutdown()
    assert not (tmp_path / "api-old").exists()
    assert (tmp_path / "bot-session").exists()
