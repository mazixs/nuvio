"""Проверки сжатия: размер, сбои ffprobe/FFmpeg и очистка процессов."""

import json
import subprocess
import time
from pathlib import Path

import pytest

from utils import media_processor, work_budget


@pytest.fixture
def compression(monkeypatch, tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"original" * 100)
    monkeypatch.setattr(media_processor, "check_ffmpeg_installed", lambda: True)
    monkeypatch.setattr(
        media_processor,
        "get_temp_file_path",
        lambda _session, filename: tmp_path / filename,
    )
    return source


def _processes(monkeypatch, *, metadata=None, probe_error=None, encode_error=None, size=20):
    processes = []

    class Process:
        def __init__(self, command, **_kwargs):
            self.command = command
            self.is_probe = command[0] == "ffprobe"
            self.returncode = 1 if (probe_error if self.is_probe else encode_error) else 0
            self.killed = False
            processes.append(self)

        def communicate(self, timeout=None):
            error = probe_error if self.is_probe else encode_error
            if error == "timeout" and timeout is not None:
                raise subprocess.TimeoutExpired(self.command, timeout)
            if self.is_probe:
                return metadata or json.dumps(
                    {"format": {"duration": "2", "bit_rate": "400000"}}
                ), str(error or "")
            if self.returncode == 0 and size is not None:
                Path(self.command[-1]).write_bytes(b"x" * size)
            return "", str(error or "")

        def kill(self):
            self.killed = True

    monkeypatch.setattr(media_processor.subprocess, "Popen", Process)
    return processes


def test_compression_creates_file_with_safe_bitrate(monkeypatch, compression):
    processes = _processes(monkeypatch)

    result = media_processor.compress_file(compression, "compress", target_size=100)

    assert result.read_bytes() == b"x" * 20
    command = processes[-1].command
    assert command[command.index("-b:v") + 1] == "100000"
    assert command[-1] == str(result)


@pytest.mark.parametrize(
    ("probe_error", "message"),
    [("timeout", "FFprobe процесс"), ("invalid", "Ошибка при получении информации")],
)
def test_probe_failure_stops_before_encoding(
    monkeypatch, compression, probe_error, message
):
    processes = _processes(monkeypatch, probe_error=probe_error)

    with pytest.raises(Exception, match=message):
        media_processor.compress_file(compression, "compress", target_size=100)

    assert len(processes) == 1
    assert processes[0].killed == (probe_error == "timeout")


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        ("not-json", "Ошибка при разборе метаданных"),
        ('{"format":{"duration":"0"}}', "Не удалось определить длительность"),
        ('{"format":{"duration":"bad"}}', "Ошибка при разборе метаданных"),
    ],
)
def test_invalid_media_metadata_is_rejected(monkeypatch, compression, metadata, message):
    processes = _processes(monkeypatch, metadata=metadata)

    with pytest.raises(Exception, match=message):
        media_processor.compress_file(compression, "compress", target_size=100)

    assert len(processes) == 1


def test_unknown_bitrate_uses_real_file_size(monkeypatch, compression):
    processes = _processes(monkeypatch, metadata='{"format":{"duration":"2"}}')

    result = media_processor.compress_file(compression, "compress", target_size=100)

    assert result.stat().st_size <= 100
    assert len(processes) == 2


@pytest.mark.parametrize(
    ("error", "message"),
    [("timeout", "FFmpeg процесс"), ("invalid", "Ошибка при сжатии файла")],
)
def test_encoder_failure_is_reported(monkeypatch, compression, error, message):
    processes = _processes(monkeypatch, encode_error=error)

    with pytest.raises(Exception, match=message):
        media_processor.compress_file(compression, "compress", target_size=100)

    assert len(processes) == 2
    assert processes[-1].killed == (error == "timeout")


@pytest.mark.parametrize(
    ("size", "message"),
    [(None, "Сжатый файл не был создан"), (101, "Не удалось сжать файл")],
)
def test_missing_or_oversized_output_is_rejected(monkeypatch, compression, size, message):
    _processes(monkeypatch, size=size)

    with pytest.raises(Exception, match=message) as error:
        media_processor.compress_file(compression, "compress", target_size=100)

    assert any("session_id=compress" in note for note in error.value.__notes__)


def test_compression_targets_the_job_limit_by_default(monkeypatch, compression):
    monkeypatch.setattr(media_processor, "MAX_FILE_SIZE", 1000)
    _processes(monkeypatch, size=150)

    assert media_processor.compress_file(compression, "compress").stat().st_size == 150
    with pytest.raises(Exception, match="Не удалось сжать файл"):
        work_budget.execute(
            media_processor.compress_file, (compression, "compress"),
            time.monotonic() + 30, None, 100,
        )


def test_ffmpeg_is_required(monkeypatch, compression):
    monkeypatch.setattr(media_processor, "check_ffmpeg_installed", lambda: False)

    with pytest.raises(Exception, match="FFmpeg не установлен"):
        media_processor.compress_file(compression, "compress", target_size=100)
