"""Тесты скачивания субтитров с выбранным языком и форматом.

Раньше язык и формат были зашиты: всегда SRT и всегда «русские, иначе
английские». Здесь закреплено, что выбор пользователя доходит до yt-dlp, а
формат TXT собирается из SRT, потому что сам yt-dlp его не отдаёт.
"""

from pathlib import Path

import pytest

from utils import youtube_utils


SRT = """1
00:00:01,000 --> 00:00:03,000
Первая реплика

2
00:00:03,000 --> 00:00:05,000
Вторая реплика
"""

pytestmark = pytest.mark.unit


class FakeYDL:
    """Заглушка yt-dlp, пишущая файл субтитров рядом с шаблоном."""

    captured: list[dict] = []

    def __init__(self, options):
        self.options = options
        FakeYDL.captured.append(options)
        self._info = {
            "id": "abc",
            "title": "video",
            "ext": "mp4",
            "subtitles": {"ru": [{}], "en": [{}]},
            "automatic_captions": {},
        }

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, url, download=False):
        return self._info

    def prepare_filename(self, info):
        template = str(self.options["outtmpl"])
        return template.replace("%(title)s", "video").replace("%(ext)s", "mp4")

    def download(self, urls):
        base = Path(self.prepare_filename(self._info))
        language = self.options["subtitleslangs"][0]
        target = base.with_suffix(f".{language}.{self.options['subtitlesformat']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(SRT, encoding="utf-8")


@pytest.fixture(autouse=True)
def fake_ytdlp(monkeypatch):
    FakeYDL.captured = []
    monkeypatch.setattr(youtube_utils.yt_dlp, "YoutubeDL", FakeYDL)
    # Патчить Path.is_file нельзя: это глобальный pathlib, на нём держатся
    # блокировки самого pytest. Достаточно убрать путь к cookies.
    monkeypatch.setattr(youtube_utils, "YOUTUBE_COOKIES_FILE", "")


def test_chosen_language_reaches_the_downloader(tmp_path):
    result = youtube_utils.download_subtitles(
        "https://youtu.be/abc", "s1", language="en", subtitle_format="srt",
        output_dir=tmp_path,
    )

    assert result is not None
    assert result.name.endswith(".en.srt")
    assert FakeYDL.captured[-1]["subtitleslangs"] == ["en"]


def test_chosen_format_reaches_the_downloader(tmp_path):
    result = youtube_utils.download_subtitles(
        "https://youtu.be/abc", "s1", language="ru", subtitle_format="vtt",
        output_dir=tmp_path,
    )

    assert result.suffix == ".vtt"
    assert FakeYDL.captured[-1]["subtitlesformat"] == "vtt"


def test_text_format_is_built_from_srt(tmp_path):
    """yt-dlp не отдаёт TXT — его собирает бот, убирая таймкоды."""
    result = youtube_utils.download_subtitles(
        "https://youtu.be/abc", "s1", language="ru", subtitle_format="txt",
        output_dir=tmp_path,
    )

    assert result.suffix == ".txt"
    content = result.read_text(encoding="utf-8")
    assert "-->" not in content
    assert content.startswith("Первая реплика")
    assert FakeYDL.captured[-1]["subtitlesformat"] == "srt"


def test_missing_track_reports_nothing(tmp_path, monkeypatch):
    class Empty(FakeYDL):
        def __init__(self, options):
            super().__init__(options)
            self._info["subtitles"] = {}
            self._info["automatic_captions"] = {}

    monkeypatch.setattr(youtube_utils.yt_dlp, "YoutubeDL", Empty)

    assert (
        youtube_utils.download_subtitles(
            "https://youtu.be/abc", "s1", language="ru", subtitle_format="srt",
            output_dir=tmp_path,
        )
        is None
    )


def test_missing_downloaded_file_is_a_technical_failure(tmp_path, monkeypatch):
    class MissingFile(FakeYDL):
        def download(self, urls):
            return None

    monkeypatch.setattr(youtube_utils.yt_dlp, "YoutubeDL", MissingFile)
    with pytest.raises(FileNotFoundError):
        youtube_utils.download_subtitles(
            "https://example.test/video", "missing-subtitle-test", language="en",
            subtitle_format="srt", output_dir=tmp_path,
        )
