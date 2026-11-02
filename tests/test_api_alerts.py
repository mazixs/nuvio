"""Уведомления админов о сбоях HTTP API: правило бота, без спама и без токена в журнале."""

import logging
import threading

import httpx
import pytest

from api import alerts
from utils import telegram_utils
from utils.public_errors import should_notify_admins

TOKEN = "123456:SECRET-TOKEN"


@pytest.fixture
def sent(monkeypatch):
    posts = []

    def post(url, json, timeout):
        posts.append((url, json))
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(alerts, "TELEGRAM_TOKEN", TOKEN)
    monkeypatch.setattr(alerts, "TELEGRAM_BOT_API_BASE_URL", "http://bot-api.test/bot")
    monkeypatch.setattr(alerts, "ADMIN_IDS", [1, 2])
    monkeypatch.setattr(alerts.httpx, "post", post)
    monkeypatch.setattr(alerts, "_last_sent", {})
    return posts


def notify(category="UNEXPECT", platform="youtube", url="https://youtu.be/dQw4w9WgXcQ", error=None):
    alerts.notify_download_failure(
        platform=platform, category=category, reference=f"YT-{category}-ABC123",
        url=url, error=error or RuntimeError("boom"),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("UNEXPECT", True),
        ("MEDIA_FORBIDDEN", True),
        ("ACCESS", True),
        ("DURATION", False),
        ("LARGE", False),
        ("NETWORK", False),
        ("TIMEOUT", False),
        ("BLOCKED", False),
    ],
)
def test_api_and_bot_share_the_notification_rule(category, expected):
    assert should_notify_admins(category) is expected
    assert telegram_utils._should_notify_admins_platform_failure("youtube", category, "format_download") is expected


@pytest.mark.unit
def test_failure_reaches_every_admin_through_the_configured_bot_api(sent):
    notify()
    assert [payload["chat_id"] for _, payload in sent] == [1, 2]
    url, payload = sent[0]
    assert url == f"http://bot-api.test/bot{TOKEN}/sendMessage"
    assert payload["parse_mode"] == "HTML"
    assert "YT-UNEXPECT-ABC123" in payload["text"]


@pytest.mark.unit
def test_expected_outcomes_are_not_reported(sent):
    notify(category="DURATION")
    assert sent == []


@pytest.mark.unit
def test_nothing_is_sent_without_a_token(sent, monkeypatch):
    monkeypatch.setattr(alerts, "TELEGRAM_TOKEN", None)
    notify()
    assert sent == []


@pytest.mark.unit
def test_same_failure_is_reported_once_per_interval(sent, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(alerts.time, "monotonic", lambda: now[0])
    notify()
    notify()
    assert len(sent) == 2
    # Другая категория той же платформы — уже другой сбой.
    notify(category="MEDIA_FORBIDDEN")
    assert len(sent) == 4
    now[0] += alerts.REPEAT_INTERVAL_SECONDS
    notify()
    assert len(sent) == 6


@pytest.mark.unit
def test_message_is_escaped_and_fits_telegram(sent):
    notify(url="https://youtu.be/" + "x" * 5000, error=RuntimeError("<b>" + "y" * 5000))
    text = sent[0][1]["text"]
    assert len(text) < 4096
    assert "&lt;b&gt;" in text


@pytest.mark.unit
def test_send_failure_never_logs_the_token(sent, monkeypatch, caplog):
    def fail(url, json, timeout):
        raise httpx.ConnectError(f"cannot reach {url}")

    monkeypatch.setattr(alerts.httpx, "post", fail)
    with caplog.at_level(logging.WARNING):
        notify()
    assert "ConnectError" in caplog.text
    assert "SECRET" not in caplog.text


@pytest.mark.unit
def test_undelivered_failure_is_retried_after_a_minute(sent, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(alerts.time, "monotonic", lambda: now[0])

    def fail(url, json, timeout):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(alerts.httpx, "post", fail)
    notify()
    monkeypatch.setattr(alerts.httpx, "post", lambda url, json, timeout: sent.append(json) or httpx.Response(200, request=httpx.Request("POST", url)))
    notify()
    assert sent == []
    now[0] += alerts.RETRY_AFTER_FAILED_SEND_SECONDS
    notify()
    assert len(sent) == 2


@pytest.mark.unit
def test_non_network_send_error_is_logged_and_retried(sent, monkeypatch, caplog):
    """`httpx.InvalidURL` не наследует `HTTPError`: в потоке уведомлений он пропал бы молча."""
    now = [1000.0]
    monkeypatch.setattr(alerts.time, "monotonic", lambda: now[0])

    def fail(url, json, timeout):
        raise httpx.InvalidURL(f"bad {url}")

    monkeypatch.setattr(alerts.httpx, "post", fail)
    with caplog.at_level(logging.WARNING):
        notify()
    assert "InvalidURL" in caplog.text
    assert "SECRET" not in caplog.text
    assert alerts._last_sent[("youtube", "UNEXPECT")] < now[0]


@pytest.mark.unit
def test_stuck_workers_are_not_a_quiet_category(sent):
    notify(category="STUCK")
    assert len(sent) == 2


@pytest.mark.unit
def test_restart_notice_reaches_every_admin_before_returning(sent):
    alerts.notify_restart(4)
    assert [payload["chat_id"] for _, payload in sent] == [1, 2]
    assert "4" in sent[0][1]["text"]


@pytest.mark.unit
def test_submit_sends_outside_the_calling_thread(sent, monkeypatch):
    seen = []
    done = threading.Event()

    def post(url, json, timeout):
        seen.append(threading.current_thread().name)
        done.set()
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(alerts.httpx, "post", post)
    alerts.submit_download_failure(
        platform="youtube", category="UNEXPECT", reference="YT-UNEXPECT-ABC123",
        url="https://youtu.be/dQw4w9WgXcQ", error=RuntimeError("boom"),
    )
    assert done.wait(5)
    assert seen[0].startswith("nuvio-api-alerts")
