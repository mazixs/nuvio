#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты канареечной проверки YouTube.

Сети здесь нет: yt-dlp подменяется целиком, а проверяется поведение вокруг него —
честность проверки (продакшн-путь и обход кэша), уведомление админов, одна
попытка обновления в сутки и уборка скачанного файла.
"""

import asyncio
import inspect
from pathlib import Path

import pytest

from utils import canary, runtime_status, temp_file_manager, video_cache


ROOT = Path(__file__).resolve().parents[1]


class FakeBot:
    """Бот из контекста job'ы: только то, чем пользуется канарейка."""

    def __init__(self):
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))


class FakeJobContext:
    def __init__(self):
        self.bot = FakeBot()


def _failure(stage: str = "download") -> canary.CanaryOutcome:
    return canary.CanaryOutcome(
        ok=False,
        stage=stage,
        detail="DownloadError: HTTP Error 403: Forbidden",
        category="MEDIA_FORBIDDEN",
        error_code="YT-MEDIA_FO-ABC123",
        format_id="135+140",
    )


def _success() -> canary.CanaryOutcome:
    return canary.CanaryOutcome(
        ok=True,
        stage="download",
        detail="скачано 30.6 МБ",
        format_id="135+140",
        size_bytes=32_100_000,
    )


@pytest.fixture
def canary_env(monkeypatch, tmp_path):
    """Включенная канарейка с двумя админами."""
    monkeypatch.setattr(canary, "CANARY_ENABLED", True)
    monkeypatch.setattr(canary, "ADMIN_IDS", [111, 222])
    monkeypatch.setattr(
        runtime_status, "_status_path", lambda: tmp_path / "canary-status.json"
    )
    return canary


@pytest.fixture
def scripted_checks(monkeypatch):
    """Подменяет проверку заранее заданной последовательностью исходов."""
    calls: list[str] = []

    def _install(outcomes: list[canary.CanaryOutcome]):
        queue = list(outcomes)

        def _fake_check(session_id: str) -> canary.CanaryOutcome:
            calls.append(session_id)
            return queue.pop(0) if queue else outcomes[-1]

        monkeypatch.setattr(canary, "run_youtube_canary_check", _fake_check)
        return calls

    return _install


@pytest.mark.unit
def test_canary_is_disabled_by_default(canary_env, scripted_checks, monkeypatch):
    """Выключенная канарейка не ходит в сеть и не будит админов.

    Включает её владелец сам, поэтому job регистрируется только под флагом, а
    сама задача проверяет флаг ещё раз — выключаться она должна из одного места.
    """
    monkeypatch.setattr(canary, "CANARY_ENABLED", False)
    calls = scripted_checks([_failure()])
    context = FakeJobContext()

    asyncio.run(canary.youtube_canary_job(context))

    assert calls == []
    assert context.bot.messages == []

    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "if CANARY_ENABLED:" in main_source
    assert "youtube_canary_job," in main_source
    assert "interval=CANARY_INTERVAL_HOURS * 3600" in main_source


@pytest.mark.unit
def test_successful_canary_stays_quiet(canary_env, scripted_checks):
    """Успешная проверка — событие для лога, а не для админов."""
    scripted_checks([_success()])
    context = FakeJobContext()

    asyncio.run(canary.youtube_canary_job(context))

    assert context.bot.messages == []


@pytest.mark.unit
def test_failed_canary_notifies_every_admin(
    canary_env, scripted_checks
):
    """При провале каждый админ получает что проверялось, что упало и код."""
    scripted_checks([_failure(), _failure()])
    context = FakeJobContext()

    asyncio.run(canary.youtube_canary_job(context))

    assert [chat_id for chat_id, _ in context.bot.messages] == [111, 222]
    report = context.bot.messages[0][1]
    assert canary.canary_video_url() in report
    assert "135+140" in report
    assert "download" in report
    assert "YT-MEDIA_FO-ABC123" in report
    assert "MEDIA_FORBIDDEN" in report


@pytest.mark.unit
def test_failure_reports_without_changing_running_package(canary_env, scripted_checks):
    """Отказ канарейки сообщает админам и не меняет пакет в контейнере."""
    calls = scripted_checks([_failure()])
    context = FakeJobContext()

    asyncio.run(canary.youtube_canary_job(context))

    assert len(calls) == 1
    assert "YT-MEDIA_FO-ABC123" in context.bot.messages[0][1]
    assert not hasattr(canary, "ensure_latest_yt_dlp")


@pytest.fixture
def fake_youtube(monkeypatch, tmp_path):
    """Подменяет yt-dlp-часть: разбор ссылки, список форматов и скачивание."""
    monkeypatch.setattr(temp_file_manager, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(
        canary, "get_video_info", lambda url, session_id=None: {"id": "fake"}
    )
    monkeypatch.setattr(
        canary,
        "get_available_formats",
        lambda info: {
            "video_only": [
                {
                    "format_id": "135",
                    "height": 480,
                    "ext": "mp4",
                    "filesize": 26 * 1024 * 1024,
                    "vcodec": "avc1.4d401f",
                }
            ],
            "audio_only": [
                {
                    "format_id": "140",
                    "ext": "m4a",
                    "filesize": 4 * 1024 * 1024,
                    "acodec": "mp4a.40.2",
                }
            ],
            "combined": [],
        },
    )

    downloads: list[tuple[str, str, str]] = []

    def _fake_download(url, format_id, session_id, *args, **kwargs):
        downloads.append((url, format_id, session_id))
        target = temp_file_manager.get_temp_file_path(session_id, "canary.mp4")
        target.write_bytes(b"0" * 4096)
        return target

    monkeypatch.setattr(canary, "download_video", _fake_download)
    return downloads


@pytest.mark.unit
def test_check_downloads_through_the_production_path(fake_youtube):
    """Канарейка качает продакшн-функцией и продакшн-выбором формата.

    Формат приходит от того же селектора, что и кнопка «отправить видео», то
    есть парой «видео + звук» — ровно той, на которой ломался `videoplayback`.
    """
    outcome = canary.run_youtube_canary_check("canary-test")

    assert outcome.ok is True
    assert fake_youtube == [
        (canary.canary_video_url(), "135+140", "canary-test")
    ]


@pytest.mark.unit
def test_check_never_looks_into_the_file_id_cache(fake_youtube, monkeypatch):
    """Кэш `file_id` отдал бы готовый файл при сломанном YouTube.

    В пользовательском потоке кэш читается до скачивания, поэтому канарейка,
    заглянувшая в него, мерила бы кэш, а не YouTube.
    """

    def _forbidden(*args, **kwargs):
        raise AssertionError("канарейка обратилась к кэшу file_id")

    monkeypatch.setattr(video_cache.telegram_cache, "get", _forbidden)
    monkeypatch.setattr(video_cache.telegram_cache, "set", _forbidden)

    assert canary.run_youtube_canary_check("canary-no-cache").ok is True
    assert len(fake_youtube) == 1

    # И на уровне модуля: кэш не импортируется вовсе, так что случайно
    # заглянуть в него неоткуда.
    source = inspect.getsource(canary)
    assert "import video_cache" not in source
    assert "telegram_cache" not in source


@pytest.mark.unit
def test_check_removes_downloaded_file(fake_youtube, tmp_path):
    """Файл нужен был как доказательство и на диске не остаётся."""
    outcome = canary.run_youtube_canary_check("canary-cleanup")

    assert outcome.ok is True
    assert not (tmp_path / "canary-cleanup").exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("size_bytes", "expected_category"),
    [(0, "FILE"), (canary.CANARY_BUDGET_BYTES + 1, "LARGE")],
)
def test_canary_reports_invalid_download_size_by_cause(
    fake_youtube, monkeypatch, size_bytes, expected_category
):
    """Пустой и слишком большой файл получают разные коды отказа."""

    def _download(_url, _format_id, session_id, **_kwargs):
        target = temp_file_manager.get_temp_file_path(session_id, "canary.mp4")
        with target.open("wb") as output:
            output.truncate(size_bytes)
        return target

    monkeypatch.setattr(canary, "download_video", _download)
    outcome = canary.run_youtube_canary_check("canary-invalid-size")

    assert outcome.category == expected_category
    assert outcome.error_code.startswith(f"YT-{expected_category}-")


@pytest.mark.unit
def test_check_removes_downloaded_file_after_failure(
    fake_youtube, tmp_path, monkeypatch
):
    """Упавшая проверка тоже убирает за собой недокачанный файл."""

    def _failing_download(url, format_id, session_id, *args, **kwargs):
        temp_file_manager.get_temp_file_path(session_id, "canary.mp4.part").write_bytes(
            b"0" * 128
        )
        raise RuntimeError("HTTP Error 403: Forbidden — unable to download video data")

    monkeypatch.setattr(canary, "download_video", _failing_download)

    outcome = canary.run_youtube_canary_check("canary-broken")

    assert outcome.ok is False
    assert outcome.stage == "download"
    assert outcome.category == "MEDIA_FORBIDDEN"
    assert outcome.error_code is not None
    assert outcome.error_code.startswith("YT-MEDIA_FO-")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.unit
def test_empty_format_list_is_a_failure_not_a_pass(fake_youtube, monkeypatch):
    """Пустой список форматов — это поломка, а не «нечего проверять»."""
    monkeypatch.setattr(
        canary,
        "get_available_formats",
        lambda info: {"video_only": [], "audio_only": [], "combined": []},
    )

    outcome = canary.run_youtube_canary_check("canary-empty")

    assert outcome.ok is False
    assert outcome.stage == "format_choice"
    assert outcome.category == "FORMAT_UNAVAILABLE"
    assert fake_youtube == []


@pytest.mark.unit
def test_canary_budget_stays_above_one_chunk():
    """Бюджет проверки обязан быть больше одного куска yt-dlp.

    Инцидент проявлялся на запросе очередного куска в 10 МБ, поэтому проверка,
    укладывающаяся в один кусок, прошла бы мимо поломки.
    """
    assert canary.CANARY_BUDGET_BYTES > 3 * 10_485_760
