"""Требования ADR-002: геометрия и кодек видео, уезжающего в Telegram.

Все три дефекта из ADR-002 закреплены здесь, чтобы починка не откатилась молча.
"""

import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from utils import media_processor, telegram_utils
from utils.public_errors import classify_internal_error_category
from utils.video_cache import CachedVideo


@pytest.fixture(autouse=True)
def _isolate_ffmpeg_probe_cache():
    media_processor.reset_ffmpeg_probe_cache()
    yield
    media_processor.reset_ffmpeg_probe_cache()


class _Process:
    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

    def communicate(self, timeout=None):
        return self.stdout, self.stderr

    def kill(self):
        pass


def _probe(monkeypatch, payload: dict):
    """Подменяет ffprobe заранее заданным ответом."""
    monkeypatch.setattr(media_processor, "check_ffmpeg_installed", lambda: True)
    monkeypatch.setattr(
        media_processor.subprocess,
        "Popen",
        lambda *a, **k: _Process(json.dumps(payload)),
    )


def _stream(**over):
    stream = {
        "codec_name": "h264",
        "width": 1920,
        "height": 1080,
        "pix_fmt": "yuv420p",
        "sample_aspect_ratio": "1:1",
    }
    stream.update(over)
    return {"streams": [stream], "format": {"duration": "132.4"}}


# --- Дефект 1: размеры не доходили до Telegram -------------------------------


@pytest.mark.unit
def test_geometry_reports_width_height_and_duration(monkeypatch, tmp_path):
    _probe(monkeypatch, _stream())

    assert media_processor.get_video_geometry(tmp_path / "clip.mp4") == {
        "width": 1920,
        "height": 1080,
        "duration": 132,
    }


@pytest.mark.unit
def test_geometry_swaps_sides_for_rotated_video(monkeypatch, tmp_path):
    """Матрица поворота на 90 градусов меняет стороны местами.

    Кодированный кадр лежит горизонтально, а показывается вертикально. Отдать
    Telegram кодированные размеры значит записать в документ ту же ложь, из-за
    которой дефект и возник.
    """
    _probe(
        monkeypatch,
        _stream(width=1920, height=1080, side_data_list=[{"rotation": -90}]),
    )

    geometry = media_processor.get_video_geometry(tmp_path / "clip.mp4")

    assert (geometry["width"], geometry["height"]) == (1080, 1920)


@pytest.mark.unit
def test_geometry_applies_non_square_pixels(monkeypatch, tmp_path):
    """SAR 4:3 растягивает кадр по ширине — Telegram нужен размер для показа."""
    _probe(monkeypatch, _stream(width=1440, height=1080, sample_aspect_ratio="4:3"))

    geometry = media_processor.get_video_geometry(tmp_path / "clip.mp4")

    assert (geometry["width"], geometry["height"]) == (1920, 1080)


@pytest.mark.unit
def test_geometry_survives_unknown_aspect_ratio(monkeypatch, tmp_path):
    """`0:1` означает «SAR неизвестен» — это не повод отказываться от размеров."""
    _probe(monkeypatch, _stream(sample_aspect_ratio="0:1"))

    geometry = media_processor.get_video_geometry(tmp_path / "clip.mp4")

    assert (geometry["width"], geometry["height"]) == (1920, 1080)


@pytest.mark.unit
def test_geometry_returns_none_without_dimensions(monkeypatch, tmp_path):
    """Без размеров лучше не передавать ничего, чем передать нули."""
    _probe(monkeypatch, {"streams": [], "format": {}})

    assert media_processor.get_video_geometry(tmp_path / "clip.mp4") is None


@pytest.mark.unit
def test_send_single_file_passes_dimensions(monkeypatch, tmp_path):
    """Главное требование ADR-002.

    Без явных размеров Telegram на тяжёлых файлах записывает `320x320`, и плеер
    на iOS рисует видео по этому квадрату.
    """
    file_path = tmp_path / "clip.mp4"
    file_path.write_bytes(b"video")
    sent: dict = {}

    async def fake_reply_video(*args, **kwargs):
        sent.update(kwargs)
        return SimpleNamespace(
            video=SimpleNamespace(
                file_id="f", file_unique_id="u", file_size=5, duration=132
            )
        )

    monkeypatch.setattr(
        telegram_utils,
        "get_video_geometry",
        lambda path: {"width": 1080, "height": 1920, "duration": 132},
    )
    query = SimpleNamespace(
        message=SimpleNamespace(reply_video=fake_reply_video),
        edit_message_text=None,
    )

    result = asyncio.run(
        telegram_utils.send_single_file(query, file_path, "token", {"platform": "tiktok"})
    )

    assert result is True
    assert sent["width"] == 1080
    assert sent["height"] == 1920
    assert sent["duration"] == 132


@pytest.mark.unit
def test_send_single_file_survives_unprobeable_video(monkeypatch, tmp_path):
    """Сбой ffprobe не должен отменять отправку — размеры лишь улучшают её."""
    file_path = tmp_path / "clip.mp4"
    file_path.write_bytes(b"video")
    sent: dict = {}

    async def fake_reply_video(*args, **kwargs):
        sent.update(kwargs)
        return SimpleNamespace(
            video=SimpleNamespace(
                file_id="f", file_unique_id="u", file_size=5, duration=0
            )
        )

    monkeypatch.setattr(telegram_utils, "get_video_geometry", lambda path: None)
    query = SimpleNamespace(
        message=SimpleNamespace(reply_video=fake_reply_video),
        edit_message_text=None,
    )

    result = asyncio.run(
        telegram_utils.send_single_file(query, file_path, "token", {"platform": "tiktok"})
    )

    assert result is True
    assert "width" not in sent


# --- Дефект 2: VP9 и AV1 внутри MP4 ------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "codec, pix_fmt, expected",
    [
        ("h264", "yuv420p", False),
        ("h264", "yuvj420p", False),
        ("h264", "yuv420p10le", True),   # профиль High 10, iOS не декодирует
        ("hevc", "yuv420p", True),
        ("vp9", "yuv420p", True),
        ("av1", "yuv420p", True),
    ],
)
def test_ios_reencode_decision(monkeypatch, tmp_path, codec, pix_fmt, expected):
    """Пригодность определяется парой «кодек + битность», а не расширением файла."""
    _probe(monkeypatch, _stream(codec_name=codec, pix_fmt=pix_fmt))

    assert media_processor.needs_ios_reencode(tmp_path / "clip.mp4") is expected


@pytest.mark.unit
def test_unprobeable_file_is_not_reencoded(monkeypatch, tmp_path):
    """Неизвестный кодек не повод жечь процессор: отправка важнее догадки."""
    _probe(monkeypatch, {"streams": [], "format": {}})

    assert media_processor.needs_ios_reencode(tmp_path / "clip.mp4") is False


# --- Дефект 3: перекодирование теряло 8-битность -----------------------------


@pytest.mark.unit
def test_reencode_pins_eight_bit_pixel_format():
    """Без `-pix_fmt yuv420p` из 10-битного источника выходит H.264 High 10."""
    command = media_processor._build_mp4_command(
        input_path="in.webm", output_path="out.mp4", video_codec="vp9", audio_codec="aac"
    )

    assert "-pix_fmt" in command
    assert command[command.index("-pix_fmt") + 1] == "yuv420p"


@pytest.mark.unit
def test_stream_copy_does_not_force_pixel_format():
    """Копирование потока не перекодирует, и `-pix_fmt` там был бы ошибкой."""
    command = media_processor._build_mp4_command(
        input_path="in.mp4", output_path="out.mp4", video_codec="h264", audio_codec="aac"
    )

    assert "-pix_fmt" not in command
    assert "-c" in command and "copy" in command


# --- Классификация недоступного поста Instagram ------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("message", "category"),
    [
        ("ERROR: [Instagram] X: Instagram sent an empty media response.", "DATA"),
        ("ERROR: [Instagram] X: Video info extraction failed: HTTP Error 400: Bad Request", "API"),
        ("ERROR: [Instagram] X: Instagram API is not granting access", "ACCESS"),
        ('{"message":"Media not found or unavailable","status":"fail"}', "ACCESS"),
    ],
)
def test_instagram_failure_requires_evidence_for_access_category(message, category):
    """HTTP 400 и пустой ответ сами по себе не подтверждают ограничение доступа."""
    assert classify_internal_error_category("instagram", message) == category


# --- Общая защита и её подключение ко всем платформам ------------------------


class _FakeYDL:
    """Минимальный двойник YoutubeDL: пишет файл и отдаёт его путь."""

    def __init__(self, options=None):
        self.options = options or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        info = {"title": "clip", "ext": "mp4", "format_id": "137+140"}
        if download:
            path = Path(self.prepare_filename(info))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("stub")
        return info

    def prepare_filename(self, info):
        template = self.options.get("outtmpl", "%(title)s.%(ext)s")
        return template.replace("%(title)s", info["title"]).replace(
            "%(ext)s", info["ext"]
        )


@pytest.mark.unit
def test_shared_guard_converts_incompatible_video(monkeypatch, tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_text("av1")
    converted = tmp_path / "clip-h264.mp4"
    converted.write_text("h264")

    monkeypatch.setattr(media_processor, "needs_ios_reencode", lambda path: True)
    monkeypatch.setattr(
        media_processor, "convert_to_format", lambda p, fmt, sid: converted
    )

    result = media_processor.ensure_ios_compatible_video(source, "sess", "youtube")

    assert result == converted
    assert not source.exists(), "исходник должен быть удалён, диск не резиновый"


@pytest.mark.unit
def test_shared_guard_leaves_compatible_video_alone(monkeypatch, tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_text("h264")
    monkeypatch.setattr(media_processor, "needs_ios_reencode", lambda path: False)

    def _forbidden(*a, **k):
        raise AssertionError("перекодирование пригодного файла — потерянные секунды")

    monkeypatch.setattr(media_processor, "convert_to_format", _forbidden)

    assert media_processor.ensure_ios_compatible_video(source, "s", "tiktok") == source


@pytest.mark.unit
def test_shared_guard_survives_conversion_failure(monkeypatch, tmp_path):
    """Сбой FFmpeg не должен отменять доставку: лучше файл с риском, чем ничего."""
    source = tmp_path / "clip.mp4"
    source.write_text("vp9")
    monkeypatch.setattr(media_processor, "needs_ios_reencode", lambda path: True)

    def _boom(*a, **k):
        raise RuntimeError("ffmpeg упал")

    monkeypatch.setattr(media_processor, "convert_to_format", _boom)

    assert media_processor.ensure_ios_compatible_video(source, "s", "vk") == source


@pytest.mark.unit
def test_youtube_download_runs_the_ios_guard(monkeypatch, tmp_path):
    """У YouTube проверки кодека не было вовсе — именно оттуда шли чёрные экраны."""
    from utils import youtube_utils

    seen: list = []
    monkeypatch.setattr(youtube_utils.yt_dlp, "YoutubeDL", _FakeYDL)
    monkeypatch.setattr(
        youtube_utils.Path, "is_file", lambda *a, **k: False, raising=False
    )
    monkeypatch.setattr(
        youtube_utils,
        "ensure_ios_compatible_video",
        lambda path, session_id, source: seen.append(path) or path,
    )

    youtube_utils.download_video(
        "https://youtu.be/abc123def45",
        "137+140",
        session_id="s",
        output_dir=tmp_path,
        force_local=True,
    )

    assert seen, "скачанный файл обязан пройти проверку кодека"


@pytest.mark.unit
def test_rutube_and_vk_share_the_same_guard():
    """Один и тот же модуль на всех платформах — расходиться реализациям нельзя."""
    from utils import rutube_vk_utils, tiktok_instagram_utils, youtube_utils

    assert (
        rutube_vk_utils.ensure_ios_compatible_video
        is youtube_utils.ensure_ios_compatible_video
        is tiktok_instagram_utils.ensure_ios_compatible_video
        is media_processor.ensure_ios_compatible_video
    )


# --- Кэш: старые документы не чинятся правкой кода ---------------------------


@pytest.mark.unit
def test_stale_video_documents_are_dropped_once(tmp_path):
    """Старые размеры и выбор языка YouTube очищаются при первом запуске.

    Пересылка по `file_id` берёт размеры из сохранённого документа, а там уже
    записано `320x320`. Правка кода такие записи не чинит — их нужно выбросить,
    иначе сломанное видео будет приезжать до конца TTL, то есть 90 дней.
    """
    from utils.video_cache import DELIVERY_CONTRACT_VERSION, TelegramVideoCache

    db = tmp_path / "cache.db"
    first = TelegramVideoCache(db_path=db)
    rows = [
        ("https://a/1", "direct_video"),
        ("https://a/2", "tg_video"),
        ("https://a/3", "combined:137+140"),
        ("https://a/4", "audio_m4a"),
        ("https://a/5", "tiktok_audio"),
    ]
    for index, (url, format_id) in enumerate(rows):
        first.set(
            CachedVideo(
                url=url,
                file_id=f"file-{index}",
                file_unique_id=f"uniq-{index}",
                platform="youtube",
                format_id=format_id,
                cached_at=datetime.now(),
                file_size=1,
                duration=1,
                title="t",
            )
        )
    # Откатываем отметку версии, изображая базу, созданную до починки.
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA user_version = 0")

    reopened = TelegramVideoCache(db_path=db)

    assert reopened.get("https://a/1", format_id="direct_video") is None
    assert reopened.get("https://a/2", format_id="tg_video") is None
    assert reopened.get("https://a/3", format_id="combined:137+140") is None
    assert reopened.get("https://a/4", format_id="audio_m4a") is None, (
        "старое аудио YouTube могло содержать английскую дорожку вместо русской"
    )
    assert reopened.get("https://a/5", format_id="tiktok_audio") is not None

    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == (
            DELIVERY_CONTRACT_VERSION
        )


@pytest.mark.unit
def test_purge_runs_only_once(tmp_path):
    """Повторный запуск не должен выбрасывать уже пересозданные записи."""
    from utils.video_cache import TelegramVideoCache

    db = tmp_path / "cache.db"
    TelegramVideoCache(db_path=db)
    fresh = TelegramVideoCache(db_path=db)
    fresh.set(
        CachedVideo(
            url="https://a/6",
            file_id="file-6",
            file_unique_id="uniq-6",
            platform="youtube",
            format_id="tg_video",
            cached_at=datetime.now(),
            file_size=1,
            duration=1,
            title="t",
        )
    )

    assert TelegramVideoCache(db_path=db).get("https://a/6", format_id="tg_video") is not None


# --- Доставка ссылкой: размеры берутся из метаданных источника ----------------


@pytest.mark.unit
def test_handoff_carries_geometry():
    """Файл при доставке ссылкой не скачивается, померить его нечем.

    Значит размеры должны прийти из метаданных источника, иначе на этом пути
    остаётся ровно тот дефект, ради которого всё и затевалось.
    """
    from utils.url_delivery import plan_url_handoff

    plan = plan_url_handoff(
        "https://scontent.cdninstagram.com/v/clip.mp4",
        "video",
        1024,
        geometry={"width": 720, "height": 1280, "duration": 15},
    )

    assert plan is not None
    assert (plan.width, plan.height, plan.duration) == (720, 1280, 15)


@pytest.mark.unit
def test_handoff_without_geometry_stays_valid():
    """Резолвер TikTok размеров не сообщает — это не повод отменять доставку."""
    from utils.url_delivery import plan_url_handoff

    plan = plan_url_handoff(
        "https://scontent.cdninstagram.com/v/clip.mp4", "video", 1024
    )

    assert plan is not None
    assert plan.width is None and plan.height is None


@pytest.mark.unit
def test_instagram_fast_path_reports_dimensions():
    """`video_versions` несёт размеры рядом со ссылкой — грех не взять."""
    from utils.instagram_fast_path import parse_instagram_fast_media

    media = parse_instagram_fast_media(
        {
            "media_type": 2,
            "video_versions": [
                {
                    "width": 720,
                    "height": 1280,
                    "type": 101,
                    "url": "https://scontent.cdninstagram.com/v/clip.mp4",
                }
            ],
            "caption": {"text": "рилс"},
            "video_duration": 15.4,
        }
    )

    assert (media.width, media.height) == (720, 1280)
    assert media.duration == 15


@pytest.mark.unit
def test_youtube_format_geometry_read_from_info():
    """У progressive-формата YouTube размеры лежат прямо в словаре yt-dlp."""
    from utils.url_delivery import find_format_geometry

    info = {
        "duration": 212,
        "formats": [
            {"format_id": "18", "url": "https://x/", "width": 640, "height": 360},
            {"format_id": "22", "url": "https://y/", "width": 1280, "height": 720},
        ],
    }

    assert find_format_geometry(info, "22") == {
        "width": 1280,
        "height": 720,
        "duration": 212,
    }
    assert find_format_geometry(info, "137+140") is None
