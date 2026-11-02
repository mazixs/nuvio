"""Плановая проверка cookies зовёт админов, только когда им есть что делать.

Живая проверка TikTok заодно продлевает сессию, поэтому задача нужна и без тревог.
Сетевой сбой — не повод будить админа: ложная тревога обесценивает настоящую.
"""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from utils import cookie_health, cookie_manager
from utils.cookie_health import CookieHealthResult


pytestmark = pytest.mark.unit

NOW = 2_000_000_000
FOURTEEN_DAYS = 14 * 24 * 60 * 60
SUMMARIES = {
    "expired": "all auth cookies are expired",
    "stale": "auth cookies exist but authenticated probe failed",
    "degraded": "auth cookie set is incomplete, re-upload is needed",
    "invalid_format": "Invalid Netscape cookie row at line 5",
    "valid": "auth cookies are active and probe succeeded",
    "rate_limited": "platform temporarily rate-limited the validation probe",
    "probe_failed": "auth cookies are active but live probe could not complete",
    "missing": "file not found",
}
TRANSLATED = {
    "expired": "срок действия всех cookies авторизации истёк",
    "stale": "cookies есть, но тест авторизации не прошёл",
    "degraded": "часть cookies авторизации потеряна",
    "invalid_format": "строка 5 не похожа на запись cookies",
}


def _result(platform, status, session_expires_at=None):
    return CookieHealthResult(
        platform, status, SUMMARIES[status], NOW, 2, 2, session_expires_at=session_expires_at
    )


def _healthy(**overrides):
    results = {
        platform: _result(platform, "valid") for platform in ("youtube", "instagram", "tiktok")
    }
    results.update(overrides)
    return results


class _Bot:
    def __init__(self, failing=()):
        self.failing = set(failing)
        self.attempts = []
        self.sent = []

    async def send_message(self, chat_id, text):
        self.attempts.append(chat_id)
        if chat_id in self.failing:
            raise RuntimeError(f"Telegram отказал для {chat_id}")
        self.sent.append((chat_id, text))


@pytest.fixture
def run_job(monkeypatch):
    monkeypatch.setattr(cookie_manager, "ADMIN_IDS", [1, 2])
    monkeypatch.setattr(cookie_manager, "time", SimpleNamespace(time=lambda: NOW))

    def run(results, bot=None):
        bot = bot or _Bot()
        monkeypatch.setattr(cookie_manager, "check_all_cookie_health", lambda **_kwargs: results)
        asyncio.run(cookie_manager.scheduled_cookie_health_check(SimpleNamespace(bot=bot)))
        return bot

    return run


@pytest.mark.parametrize("status", ["expired", "stale", "degraded", "invalid_format"])
def test_broken_cookies_alert_admins(run_job, status):
    bot = run_job(_healthy(instagram=_result("instagram", status)))

    assert [chat_id for chat_id, _text in bot.sent] == [1, 2]
    text = bot.sent[0][1]
    assert "Instagram" in text
    assert TRANSLATED[status] in text
    assert "YouTube" not in text
    assert "/admin" in text


@pytest.mark.parametrize("status", ["valid", "rate_limited", "probe_failed", "missing"])
def test_status_alone_does_not_wake_admins(run_job, status):
    bot = run_job(_healthy(tiktok=_result("tiktok", status)))

    assert bot.attempts == []


@pytest.mark.parametrize(
    ("seconds_left", "alerted"),
    [
        (0, True),
        (FOURTEEN_DAYS - 1, True),
        (FOURTEEN_DAYS, False),
        (FOURTEEN_DAYS + 1, False),
    ],
)
def test_session_expiry_threshold_is_fourteen_days(run_job, seconds_left, alerted):
    result = _result("tiktok", "valid", session_expires_at=NOW + seconds_left)

    bot = run_job(_healthy(tiktok=result))

    assert bool(bot.sent) is alerted


def test_expiry_alert_names_platform_and_date_in_utc(run_job):
    # NOW — это 18.05.2033 03:33:20 UTC.
    result = _result("tiktok", "valid", session_expires_at=NOW + 3 * 24 * 60 * 60)

    bot = run_job(_healthy(tiktok=result))

    assert "- TikTok: сессия истекает 21.05.2033" in bot.sent[0][1]


@pytest.mark.parametrize("status", ["rate_limited", "probe_failed"])
def test_failed_probe_does_not_hide_a_session_about_to_expire(run_job, status):
    """Если TikTok изменит `passport`, проба будет молча падать и продление встанет.

    Тогда приближение срока из файла останется единственным сигналом.
    """
    result = _result("tiktok", status, session_expires_at=NOW + FOURTEEN_DAYS - 1)

    bot = run_job(_healthy(tiktok=result))

    assert "сессия истекает" in bot.sent[0][1]
    assert "сетевой тест" not in bot.sent[0][1]
    assert "ограничила запросы" not in bot.sent[0][1]


@pytest.mark.parametrize("status", ["rate_limited", "probe_failed"])
def test_failed_probe_with_a_distant_expiry_stays_silent(run_job, status):
    result = _result("tiktok", status, session_expires_at=NOW + FOURTEEN_DAYS)

    bot = run_job(_healthy(tiktok=result))

    assert bot.attempts == []


def test_broken_status_and_near_expiry_share_one_line(run_job):
    result = _result("tiktok", "stale", session_expires_at=NOW + 60)

    bot = run_job(_healthy(tiktok=result))

    line = next(line for line in bot.sent[0][1].splitlines() if "TikTok" in line)
    assert TRANSLATED["stale"] in line
    assert "сессия истекает" in line


def test_silence_when_everything_is_healthy(run_job):
    far_future = NOW + 365 * 24 * 60 * 60

    bot = run_job(_healthy(youtube=_result("youtube", "valid", session_expires_at=far_future)))

    assert bot.attempts == []


def test_several_platforms_fit_in_one_message(run_job):
    bot = run_job(
        {
            "youtube": _result("youtube", "degraded"),
            "instagram": _result("instagram", "valid"),
            "tiktok": _result("tiktok", "valid", session_expires_at=NOW + 60),
        }
    )

    assert [chat_id for chat_id, _text in bot.sent] == [1, 2]
    assert bot.sent[0][1] == bot.sent[1][1]
    text = bot.sent[0][1]
    assert "YouTube" in text
    assert "TikTok" in text
    assert "Instagram" not in text


def test_one_admin_failure_does_not_stop_the_others(run_job, caplog):
    bot = run_job(_healthy(youtube=_result("youtube", "expired")), _Bot(failing=[1]))

    assert bot.attempts == [1, 2]
    assert [chat_id for chat_id, _text in bot.sent] == [2]
    assert "Telegram отказал для 1" in caplog.text


def test_check_failure_is_logged_and_does_not_escape(monkeypatch, caplog):
    def crash(**_kwargs):
        raise OSError("диск недоступен")

    bot = _Bot()
    monkeypatch.setattr(cookie_manager, "ADMIN_IDS", [1])
    monkeypatch.setattr(cookie_manager, "check_all_cookie_health", crash)

    asyncio.run(cookie_manager.scheduled_cookie_health_check(SimpleNamespace(bot=bot)))

    assert bot.attempts == []
    assert "Плановая проверка cookies не удалась" in caplog.text
    assert "диск недоступен" in caplog.text


def test_check_is_forced_and_runs_off_the_event_loop(monkeypatch):
    seen = []

    def check(**kwargs):
        seen.append((kwargs, threading.current_thread() is threading.main_thread()))
        return _healthy()

    monkeypatch.setattr(cookie_manager, "check_all_cookie_health", check)

    asyncio.run(cookie_manager.scheduled_cookie_health_check(SimpleNamespace(bot=_Bot())))

    assert seen == [({"force": True}, False)]


def test_job_runs_daily_with_a_delayed_start(monkeypatch):
    import main
    from telegram.ext import JobQueue

    registered = {}
    really_register = JobQueue.run_repeating

    def spy(self, callback, *args, **kwargs):
        registered[callback] = kwargs
        return really_register(self, callback, *args, **kwargs)

    monkeypatch.setattr(JobQueue, "run_repeating", spy)
    monkeypatch.setattr(main, "TELEGRAM_TOKEN", "123456:abcdefghijklmnopqrstuvwxyz123456")

    main._build_application()

    assert main.scheduled_cookie_health_check is cookie_manager.scheduled_cookie_health_check
    assert registered[main.scheduled_cookie_health_check] == {"interval": 86400, "first": 900}


def test_real_check_alerts_from_the_working_copy_without_cookie_values(monkeypatch, tmp_path, run_job):
    """Весь путь без сети: тревога идёт по рабочей копии, значений cookies в ней нет."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    header = "# Netscape HTTP Cookie File\n"
    youtube = tmp_path / "www.youtube.com_cookies.txt"
    youtube.write_text(
        header + ".youtube.com\tTRUE\t/\tTRUE\t0\t__Secure-3PSID\tfake-secret-value\n",
        encoding="utf-8",
    )
    instagram = tmp_path / "www.instagram.com_cookies.txt"
    instagram.write_text(
        header + ".instagram.com\tTRUE\t/\tTRUE\tfake-secret-value\tsessionid\tfake\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(cookie_health.COOKIE_PATHS, "youtube", youtube)
    monkeypatch.setitem(cookie_health.COOKIE_PATHS, "instagram", instagram)
    monkeypatch.setitem(cookie_health.COOKIE_PATHS, "tiktok", tmp_path / "absent.txt")
    monkeypatch.setattr(cookie_health, "_probe_authenticated_session", lambda *_args: "valid")
    cookie_health._COOKIE_HEALTH_CACHE.clear()
    bot = _Bot()

    results = cookie_health.check_all_cookie_health(force=True)
    run_job(results, bot)
    cookie_health._COOKIE_HEALTH_CACHE.clear()

    assert results["youtube"].status == "degraded"
    assert results["instagram"].status == "invalid_format"
    assert results["tiktok"].status == "missing"
    text = bot.sent[0][1]
    assert "YouTube: часть cookies авторизации потеряна" in text
    assert "Instagram: неверный срок действия cookie в строке 2" in text
    assert "TikTok" not in text
    assert "fake-secret-value" not in text
