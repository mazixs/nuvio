"""Тесты быстрого пути TikTok: разбор ответа резолвера без сети.

Формы полезной нагрузки скопированы с реальных ответов резолвера
(см. docs/research/latency-disk-network-research.md). Хосты ссылок оставлены
настоящими, потому что они проверяются по allowlist, но в сеть тесты не ходят:
разбор ответа — чистая функция, а скачивание здесь не выполняется.
"""

import pytest

from utils.tiktok_fast_path import (
    FastPathUnavailable,
    audio_matches_video,
    is_allowed_media_url,
    parse_fast_media,
)


def _payload(**overrides):
    """Ответ резолвера для обычного видео (форма реального ответа)."""
    data = {
        "id": "7639073022762175764",
        "title": "",
        "duration": 60,
        "size": 5576522,
        "wm_size": 5979791,
        "play": "https://v16m.tiktokcdn-us.com/play.mp4",
        "wmplay": "https://v16m.tiktokcdn-us.com/wmplay.mp4",
        "music": "https://www.tikwm.com/music.mp3",
        "cover": "https://www.tikwm.com/cover.jpg",
        "origin_cover": "https://www.tikwm.com/origin.jpg",
        "author": {"unique_id": "user1509008012081", "nickname": "Кто-то"},
        "music_info": {
            "title": "original sound - user1509008012081",
            "duration": 60,
            "original": True,
            "play": "https://www.tikwm.com/music.mp3",
        },
    }
    data.update(overrides)
    return {"code": 0, "msg": "success", "data": data}


# === parse_fast_media ===


@pytest.mark.unit
def test_parse_fast_media_extracts_direct_video_url():
    media = parse_fast_media(_payload())

    assert media.video_url == "https://v16m.tiktokcdn-us.com/play.mp4"
    assert media.size == 5576522
    assert media.duration == 60


@pytest.mark.unit
def test_parse_fast_media_rejects_error_response():
    payload = _payload()
    payload["code"] = -1
    payload["msg"] = "url parse err"

    with pytest.raises(FastPathUnavailable, match="url parse err"):
        parse_fast_media(payload)


@pytest.mark.unit
def test_parse_fast_media_rejects_response_without_play_url():
    payload = _payload()
    del payload["data"]["play"]

    with pytest.raises(FastPathUnavailable):
        parse_fast_media(payload)


@pytest.mark.unit
def test_parse_fast_media_rejects_photo_post():
    """Фото-посты идут своим путём и не должны попадать в быстрый путь видео."""
    payload = _payload(images=["https://www.tikwm.com/1.jpg"])

    with pytest.raises(FastPathUnavailable, match="фото-пост"):
        parse_fast_media(payload)


@pytest.mark.unit
def test_parse_fast_media_rejects_video_url_outside_allowlist():
    """Подменённый ответ не должен уводить бота во внутреннюю сеть Docker."""
    payload = _payload(play="http://telegram-bot-api:8081/secret")

    with pytest.raises(FastPathUnavailable, match="allowlist"):
        parse_fast_media(payload)


@pytest.mark.unit
def test_parse_fast_media_drops_audio_url_outside_allowlist():
    """Негодная ссылка звука не отменяет быстрый путь — звук возьмём из видео."""
    media = parse_fast_media(
        _payload(
            music="http://web:8080/dashboard",
            music_info={
                "title": "original sound - tester",
                "duration": 60,
                "original": True,
                "play": "http://web:8080/dashboard",
            },
        )
    )

    assert media.audio_url is None
    assert media.audio_is_video_sound is True


@pytest.mark.unit
def test_parse_fast_media_keeps_audio_url_for_original_sound():
    media = parse_fast_media(_payload())

    assert media.audio_url == "https://www.tikwm.com/music.mp3"
    assert media.audio_is_video_sound is True


@pytest.mark.unit
def test_parse_fast_media_drops_audio_url_for_licensed_track():
    """Реальный случай: 35-секундное видео с треком SAVAGE отдаёт music на 168 с."""
    media = parse_fast_media(
        _payload(
            duration=35,
            music_info={
                "title": "SAVAGE",
                "duration": 168,
                "original": False,
                "play": "https://www.tikwm.com/savage.mp3",
            },
        )
    )

    assert media.audio_is_video_sound is False
    assert media.audio_url is None


# === audio_matches_video ===


@pytest.mark.unit
def test_audio_matches_video_accepts_original_sound_with_equal_duration():
    music_info = {"original": True, "duration": 166}

    assert audio_matches_video(music_info, video_duration=166) is True


@pytest.mark.unit
def test_audio_matches_video_rejects_licensed_track():
    music_info = {"original": False, "duration": 168}

    assert audio_matches_video(music_info, video_duration=35) is False


@pytest.mark.unit
def test_audio_matches_video_rejects_original_sound_with_diverging_duration():
    """Флаг original недостаточен — длительность тоже должна совпадать."""
    music_info = {"original": True, "duration": 168}

    assert audio_matches_video(music_info, video_duration=35) is False


@pytest.mark.unit
def test_audio_matches_video_tolerates_small_duration_drift():
    music_info = {"original": True, "duration": 60}

    assert audio_matches_video(music_info, video_duration=61) is True


@pytest.mark.unit
def test_audio_matches_video_rejects_missing_music_info():
    assert audio_matches_video({}, video_duration=60) is False


# === is_allowed_media_url ===


@pytest.mark.unit
def test_allowlist_accepts_verified_tiktok_cdn_host():
    """Реальный хост из замеров: подписанная ссылка ведёт на CDN самого TikTok."""
    assert is_allowed_media_url("https://v16m.tiktokcdn-us.com/video/tos/x.mp4") is True


@pytest.mark.unit
def test_allowlist_accepts_resolver_own_host():
    assert is_allowed_media_url("https://www.tikwm.com/video/music/x.mp3") is True


@pytest.mark.unit
def test_allowlist_rejects_internal_docker_service():
    """Главный сценарий угрозы: SSRF во внутреннюю сеть стека."""
    assert is_allowed_media_url("http://telegram-bot-api:8081/x.mp4") is False
    assert is_allowed_media_url("http://web:8080/users") is False


@pytest.mark.unit
def test_allowlist_rejects_plain_http_even_on_allowed_host():
    assert is_allowed_media_url("http://v16m.tiktokcdn-us.com/x.mp4") is False


@pytest.mark.unit
def test_allowlist_rejects_host_that_only_looks_like_cdn():
    """Проверка по суффиксу домена, а не по подстроке."""
    assert is_allowed_media_url("https://tiktokcdn-us.com.evil.test/x.mp4") is False
    assert is_allowed_media_url("https://eviltikwm.com/x.mp4") is False


@pytest.mark.unit
def test_allowlist_rejects_non_http_schemes():
    assert is_allowed_media_url("file:///etc/passwd") is False
    assert is_allowed_media_url("") is False
