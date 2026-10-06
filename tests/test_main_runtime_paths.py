"""Проверки запуска и фонового обслуживания без обращения к Telegram."""

import asyncio
from types import SimpleNamespace

import pytest

import main


def test_cache_maintenance_runs_all_cleanup_steps(monkeypatch, tmp_path):
    calls = []
    cache = SimpleNamespace()
    from utils import cache_policy

    monkeypatch.setattr(cache_policy, "cleanup_cache", lambda current: calls.append(current) or 3)
    monkeypatch.setattr(main, "telegram_cache", cache)
    monkeypatch.setattr(main, "cleanup_old_workfiles", lambda: (2, 0))
    monkeypatch.setattr(main, "cleanup_stale_temp_files", lambda **kwargs: (1, 0))
    monkeypatch.setattr(main, "prune_old_event_urls", lambda: (4, tmp_path / "backup"))
    from utils import telegram_utils

    monkeypatch.setattr(telegram_utils, "active_download_sessions", lambda: {"active"})

    asyncio.run(main.scheduled_cache_cleanup(None))

    assert calls == [cache]
    asyncio.run(main.scheduled_housekeeping(None))
    assert calls == [cache]


def test_cache_maintenance_failure_does_not_escape_job(monkeypatch):
    monkeypatch.setattr(
        main,
        "telegram_cache",
        SimpleNamespace(cleanup_expired=lambda **_kwargs: (_ for _ in ()).throw(OSError("db"))),
    )

    asyncio.run(main.scheduled_cache_cleanup(None))


def test_cache_vacuum_uses_worker_and_preserves_file(monkeypatch, tmp_path):
    db = tmp_path / "cache.db"
    db.write_bytes(b"database")
    calls = []
    monkeypatch.setattr(
        main,
        "telegram_cache",
        SimpleNamespace(db_path=db, vacuum=lambda: calls.append("vacuum")),
    )

    asyncio.run(main.scheduled_cache_vacuum(None))

    assert calls == ["vacuum"]
    assert db.read_bytes() == b"database"


def test_csi_dispatch_continues_after_one_user_error(monkeypatch):
    from utils import telegram_utils

    sent = []

    async def send(user_id, _context):
        sent.append(user_id)
        if user_id == 1:
            raise RuntimeError("Telegram недоступен")

    monkeypatch.setattr(main, "get_csi_interval_days", lambda: 14)
    monkeypatch.setattr(main, "get_users_for_csi", lambda **_kwargs: [1, 2])
    monkeypatch.setattr(telegram_utils, "send_csi_request", send)

    asyncio.run(main.scheduled_csi_dispatch(None))

    assert sent == [1, 2]


def test_application_registers_canary_and_handlers_when_enabled(monkeypatch):
    monkeypatch.setattr(main, "TELEGRAM_TOKEN", "123456:abcdefghijklmnopqrstuvwxyz123456")
    monkeypatch.setattr(main, "CANARY_ENABLED", True)

    app = main._build_application()

    assert sum(len(group) for group in app.handlers.values()) >= 10
    assert {job.name for job in app.job_queue.jobs()} >= {"youtube_canary"}


def test_storage_startup_only_cleans_stale_media(monkeypatch):
    calls = []
    monkeypatch.setattr(
        main,
        "cleanup_stale_temp_files",
        lambda: calls.append("stale") or (2, 1),
    )

    main._prepare_runtime_storage()

    assert calls == ["stale"]


def test_invalid_configuration_stops_before_telegram_start(monkeypatch):
    monkeypatch.setattr(main, "_prepare_runtime_storage", lambda: None)
    monkeypatch.setattr(
        main,
        "validate_config",
        lambda: (_ for _ in ()).throw(ValueError("нет токена")),
    )
    monkeypatch.setattr(
        main,
        "_build_application",
        lambda: pytest.fail("бот не должен создаваться"),
    )

    asyncio.run(main.run_bot())


def test_missing_updater_still_shuts_down_application(monkeypatch):
    calls = []

    class App:
        updater = None

        async def initialize(self):
            calls.append("initialize")

        async def start(self):
            calls.append("start")

        async def stop(self):
            calls.append("stop")

        async def shutdown(self):
            calls.append("shutdown")

    monkeypatch.setattr(main, "_prepare_runtime_storage", lambda: None)
    monkeypatch.setattr(main, "validate_config", lambda: None)
    monkeypatch.setattr(main, "runtime_components", lambda: {})
    monkeypatch.setattr(main, "get_installed_yt_dlp_version", lambda: "test")
    monkeypatch.setattr(main, "_build_application", App)
    monkeypatch.setattr(main, "close_connection", lambda: calls.append("close_db"))

    with pytest.raises(RuntimeError, match="Updater не инициализирован"):
        asyncio.run(main.run_bot())

    assert calls == ["initialize", "start", "stop", "shutdown", "close_db"]


def test_running_bot_starts_polling_and_shuts_down_after_cancellation(monkeypatch):
    calls = []
    started = asyncio.Event()

    class Updater:
        async def start_polling(self, **kwargs):
            assert kwargs["bootstrap_retries"] == 3
            assert kwargs["allowed_updates"]
            calls.append("polling")
            started.set()

        async def stop(self):
            calls.append("stop_polling")

    class App:
        updater = Updater()

        async def initialize(self):
            calls.append("initialize")

        async def start(self):
            calls.append("start")

        async def stop(self):
            calls.append("stop")

        async def shutdown(self):
            calls.append("shutdown")

    monkeypatch.setattr(main, "_prepare_runtime_storage", lambda: None)
    monkeypatch.setattr(main, "validate_config", lambda: None)
    monkeypatch.setattr(main, "runtime_components", lambda: {})
    monkeypatch.setattr(main, "get_installed_yt_dlp_version", lambda: "test")
    monkeypatch.setattr(main, "_build_application", App)
    monkeypatch.setattr(
        main, "telegram_cache", SimpleNamespace(get_stats=lambda: {"total_videos": 0})
    )
    monkeypatch.setattr(main, "close_connection", lambda: calls.append("close_db"))

    async def exercise():
        task = asyncio.create_task(main.run_bot())
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    assert calls == [
        "initialize", "start", "polling", "stop_polling", "stop", "shutdown", "close_db"
    ]
