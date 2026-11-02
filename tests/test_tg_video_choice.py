"""Тесты выбора формата для кнопки «отправить видео в Telegram»."""

import pytest

from utils.tg_video_choice import (
    list_audio_options,
    list_video_options,
    select_tg_video_format,
    select_unsized_video_format,
)


MB = 1024 * 1024

# Форматы реального видео (youtube.com/watch?v=NXNazpSQl6Q, 1924 с). Значения
# vcodec/acodec и размеры сняты с ответа yt-dlp, не придуманы: именно на этом
# наборе бот выбирал AV1 480p со звуком opus.
VIDEO_ONLY = [
    {"format_id": "315", "height": 2160, "ext": "webm", "vcodec": "vp9", "filesize": int(925.8 * MB)},
    {"format_id": "401", "height": 2160, "ext": "mp4", "vcodec": "av01.0.13M.08", "filesize": int(467.6 * MB)},
    {"format_id": "308", "height": 1440, "ext": "webm", "vcodec": "vp9", "filesize": int(374.0 * MB)},
    {"format_id": "400", "height": 1440, "ext": "mp4", "vcodec": "av01.0.12M.08", "filesize": int(216.3 * MB)},
    {"format_id": "299", "height": 1080, "ext": "mp4", "vcodec": "avc1.64002a", "filesize": int(301.2 * MB)},
    {"format_id": "303", "height": 1080, "ext": "webm", "vcodec": "vp9", "filesize": int(135.1 * MB)},
    {"format_id": "399", "height": 1080, "ext": "mp4", "vcodec": "av01.0.09M.08", "filesize": int(103.7 * MB)},
    {"format_id": "298", "height": 720, "ext": "mp4", "vcodec": "avc1.640020", "filesize": int(183.9 * MB)},
    {"format_id": "135", "height": 480, "ext": "mp4", "vcodec": "avc1.4d401f", "filesize": int(67.1 * MB)},
    {"format_id": "397", "height": 480, "ext": "mp4", "vcodec": "av01.0.04M.08", "filesize": int(29.9 * MB)},
]

AUDIO_ONLY = [
    {"format_id": "140", "ext": "m4a", "acodec": "mp4a.40.2", "filesize": int(29.7 * MB)},
    {"format_id": "251", "ext": "webm", "acodec": "opus", "filesize": int(25.0 * MB)},
    {"format_id": "249", "ext": "webm", "acodec": "opus", "filesize": int(11.5 * MB)},
    {"format_id": "139", "ext": "m4a", "acodec": "mp4a.40.5", "filesize": int(11.2 * MB)},
]

COMBINED = [
    {"format_id": "18", "height": 360, "ext": "mp4", "vcodec": "avc1.42001E", "filesize": int(61.7 * MB)},
]

LOCAL_BUDGET = 2000 * MB
CLOUD_BUDGET = 50 * MB


@pytest.mark.unit
def test_local_mode_picks_h264_1080p_instead_of_av1_480p():
    """Главный дефект: при лимите 2 ГБ бот отдавал 480p AV1 со звуком opus.

    Прежние границы были захардкожены под облачные 50 МБ — 35 МБ на видео и
    15 МБ на звук, — поэтому единственным подходящим видео оказывался AV1 480p
    на 29.9 МБ, а звуком — opus на 11.5 МБ. При бюджете 2 ГБ доступен
    1080p H.264 (301 МБ) с дорожкой AAC.
    """
    choice = select_tg_video_format(VIDEO_ONLY, AUDIO_ONLY, COMBINED, LOCAL_BUDGET)

    assert choice is not None
    assert choice.format_id == "299+140"
    assert choice.height == 1080


@pytest.mark.unit
def test_prefers_h264_over_higher_resolution_in_other_codecs():
    """1080p H.264 важнее 2160p VP9/AV1 — это кнопка «отправить в Telegram».

    Меню форматов по-прежнему позволяет взять 4K осознанно, а у этой кнопки
    задача другая: чтобы файл гарантированно проигрался, в том числе на iOS,
    где ADR-001 уже фиксировал проблему с нестандартным кодеком.
    """
    choice = select_tg_video_format(VIDEO_ONLY, AUDIO_ONLY, COMBINED, LOCAL_BUDGET)

    assert choice.format_id.startswith("299")


@pytest.mark.unit
def test_prefers_aac_audio_over_opus():
    """Opus внутри MP4 — тот же риск несовместимости, что и экзотическое видео.

    Прежний код брал первый звук под 15 МБ, то есть opus на 11.5 МБ, хотя рядом
    лежал AAC на 11.2 МБ.
    """
    choice = select_tg_video_format(VIDEO_ONLY, AUDIO_ONLY, COMBINED, LOCAL_BUDGET)

    assert choice.format_id.endswith("+140")


@pytest.mark.unit
def test_cloud_budget_still_fits_fifty_megabytes():
    """В облачном режиме лимит настоящий, и выбор обязан в него укладываться."""
    choice = select_tg_video_format(VIDEO_ONLY, AUDIO_ONLY, COMBINED, CLOUD_BUDGET)

    assert choice is not None
    assert choice.total_size <= CLOUD_BUDGET


@pytest.mark.unit
def test_cloud_budget_falls_back_to_available_codec():
    """Под 50 МБ H.264 не влезает — тогда допустим и AV1, лишь бы отправилось."""
    choice = select_tg_video_format(VIDEO_ONLY, AUDIO_ONLY, COMBINED, CLOUD_BUDGET)

    assert choice.format_id == "397+139"


@pytest.mark.unit
def test_returns_none_when_nothing_fits():
    """Ничего не влезло — решение принимает вызывающий код, а не догадка здесь."""
    assert select_tg_video_format(VIDEO_ONLY, AUDIO_ONLY, COMBINED, 5 * MB) is None


@pytest.mark.unit
def test_combined_format_used_when_no_pair_fits():
    """Готовый combined не требует склейки, но по качеству уступает паре.

    Поэтому он берётся только когда пара «видео + звук» в бюджет не влезла.
    """
    choice = select_tg_video_format([], [], COMBINED, LOCAL_BUDGET)

    assert choice is not None
    assert choice.format_id == "18"
    assert choice.kind == "combined"


@pytest.mark.unit
def test_pair_wins_over_combined_when_both_fit():
    choice = select_tg_video_format(VIDEO_ONLY, AUDIO_ONLY, COMBINED, LOCAL_BUDGET)

    assert choice.kind == "combined_manual"


@pytest.mark.unit
def test_formats_without_known_size_are_ignored():
    """Формат без размера нельзя сверить с бюджетом — угадывать здесь нечего."""
    choice = select_tg_video_format(
        [{"format_id": "999", "height": 2160, "ext": "mp4", "vcodec": "avc1.640033"}],
        [{"format_id": "998", "ext": "m4a", "acodec": "mp4a.40.2"}],
        [],
        LOCAL_BUDGET,
    )

    assert choice is None


@pytest.mark.unit
def test_smaller_file_wins_between_equal_height_and_codec_class():
    """При равной высоте и равной пригодности кодека лишние байты не нужны."""
    choice = select_tg_video_format(
        [
            {"format_id": "big", "height": 1080, "ext": "webm", "vcodec": "vp9", "filesize": 400 * MB},
            {"format_id": "small", "height": 1080, "ext": "mp4", "vcodec": "av01.0.09M.08", "filesize": 100 * MB},
        ],
        [{"format_id": "a", "ext": "m4a", "acodec": "mp4a.40.2", "filesize": 10 * MB}],
        [],
        LOCAL_BUDGET,
    )

    assert choice.format_id == "small+a"


@pytest.mark.unit
def test_total_size_reports_pair_sum():
    choice = select_tg_video_format(VIDEO_ONLY, AUDIO_ONLY, COMBINED, LOCAL_BUDGET)

    assert choice.total_size == int(301.2 * MB) + int(29.7 * MB)


@pytest.mark.unit
def test_get_available_formats_carries_codecs():
    """Выбор формата опирается на кодек, значит он обязан доходить до него.

    Раньше `vcodec`/`acodec` читались из ответа yt-dlp только для разделения на
    группы и в итоговые словари не попадали, поэтому обработчик о кодеках
    ничего не знал и склеивал AV1 с opus в MP4.
    """
    from utils.youtube_utils import get_available_formats

    info = {
        "formats": [
            {
                "format_id": "299",
                "ext": "mp4",
                "height": 1080,
                "vcodec": "avc1.64002a",
                "acodec": "none",
                "filesize": 301 * MB,
            },
            {
                "format_id": "140",
                "ext": "m4a",
                "audio_channels": 2,
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "filesize": 29 * MB,
            },
            {
                "format_id": "22",
                "ext": "mp4",
                "height": 720,
                "vcodec": "avc1.42001E",
                "acodec": "mp4a.40.2",
                "filesize": 61 * MB,
            },
        ]
    }

    formats = get_available_formats(info, filter_by_size=False)

    assert formats["video_only"][0]["vcodec"] == "avc1.64002a"
    assert formats["audio_only"][0]["acodec"] == "mp4a.40.2"
    assert formats["combined"][0]["vcodec"] == "avc1.42001E"
    assert formats["combined"][0]["acodec"] == "mp4a.40.2"


@pytest.mark.unit
def test_progressive_itag_18_is_not_offered():
    """itag 18 без PO-токена всегда даёт 403, поэтому в меню он не попадает."""
    from utils.youtube_utils import get_available_formats

    info = {
        "formats": [
            {
                "format_id": "18",
                "ext": "mp4",
                "height": 360,
                "vcodec": "avc1.42001E",
                "acodec": "mp4a.40.2",
                "filesize": 61 * MB,
            },
            {
                "format_id": "134",
                "ext": "mp4",
                "height": 360,
                "vcodec": "avc1.4d401e",
                "acodec": "none",
                "filesize": 20 * MB,
            },
        ]
    }

    formats = get_available_formats(info, filter_by_size=False)

    assert formats["combined"] == []
    assert [fmt["format_id"] for fmt in formats["video_only"]] == ["134"]


@pytest.mark.unit
def test_resolution_ceiling_keeps_4k_out_of_the_button():
    """2160p влезает в 2 ГБ, но кнопка обещает «просто отправь».

    4K на 32-минутном ролике — 467 МБ в AV1 или 926 МБ в VP9 и минуты отправки,
    причём H.264 в таком разрешении YouTube не отдаёт вовсе. Кто хочет больше,
    берёт формат в меню осознанно.
    """
    choice = select_tg_video_format(VIDEO_ONLY, AUDIO_ONLY, COMBINED, LOCAL_BUDGET)

    assert choice.height == 1080


@pytest.mark.unit
def test_resolution_outranks_codec_when_h264_forces_a_downgrade():
    """Кодек — тай-брейк, а не приоритет.

    Под облачные 50 МБ H.264 влезает только в 240p, а AV1 даёт 480p. Ставить
    кодек выше разрешения означало бы уронить качество там, где его никто не
    просил ухудшать.
    """
    video = [
        {"format_id": "135", "height": 480, "ext": "mp4", "vcodec": "avc1.4d401f", "filesize": int(67.1 * MB)},
        {"format_id": "397", "height": 480, "ext": "mp4", "vcodec": "av01.0.04M.08", "filesize": int(29.9 * MB)},
        {"format_id": "133", "height": 240, "ext": "mp4", "vcodec": "avc1.4d4015", "filesize": int(20.8 * MB)},
    ]
    audio = [{"format_id": "139", "ext": "m4a", "acodec": "mp4a.40.5", "filesize": int(11.2 * MB)}]

    choice = select_tg_video_format(video, audio, [], CLOUD_BUDGET)

    assert choice.height == 480
    assert choice.format_id == "397+139"


@pytest.mark.unit
def test_h264_wins_within_the_same_resolution():
    """При равном разрешении выбирается кодек, который играет везде."""
    video = [
        {"format_id": "399", "height": 1080, "ext": "mp4", "vcodec": "av01.0.09M.08", "filesize": int(103.7 * MB)},
        {"format_id": "299", "height": 1080, "ext": "mp4", "vcodec": "avc1.64002a", "filesize": int(301.2 * MB)},
    ]
    audio = [{"format_id": "140", "ext": "m4a", "acodec": "mp4a.40.2", "filesize": int(29.7 * MB)}]

    choice = select_tg_video_format(video, audio, [], LOCAL_BUDGET)

    assert choice.format_id == "299+140"


@pytest.mark.unit
def test_long_video_cascades_down_to_the_next_fitting_resolution():
    """Многочасовое видео в 1080p не влезает в 2 ГБ — нужен шаг вниз.

    Размеры пересчитаны с измеренных на реальном 32-минутном ролике битрейтов
    (1080p H.264 — 301.2 МБ, 720p — 183.9 МБ, 480p — 67.1 МБ) на 4 часа: это
    2.26 ГБ, 1.38 ГБ и 504 МБ. Первое в лимит локального Bot API не проходит,
    поэтому выбор обязан опуститься до 720p, а не отказать.
    """
    four_hours = 240 / 32  # во столько раз длиннее замеренного ролика
    video = [
        {"format_id": "299", "height": 1080, "ext": "mp4", "vcodec": "avc1.64002a",
         "filesize": int(301.2 * MB * four_hours)},
        {"format_id": "298", "height": 720, "ext": "mp4", "vcodec": "avc1.640020",
         "filesize": int(183.9 * MB * four_hours)},
        {"format_id": "135", "height": 480, "ext": "mp4", "vcodec": "avc1.4d401f",
         "filesize": int(67.1 * MB * four_hours)},
    ]
    audio = [{"format_id": "140", "ext": "m4a", "acodec": "mp4a.40.2",
              "filesize": int(29.7 * MB * four_hours)}]

    choice = select_tg_video_format(video, audio, [], LOCAL_BUDGET)

    assert choice is not None, "вместо отказа нужен шаг вниз по качеству"
    assert choice.height == 720
    assert choice.total_size <= LOCAL_BUDGET


@pytest.mark.unit
def test_cascade_continues_until_something_fits():
    """Если и 720p не влезает — идём ниже, пока не найдётся проходной вариант."""
    video = [
        {"format_id": "a", "height": 1080, "ext": "mp4", "vcodec": "avc1", "filesize": 1900 * MB},
        {"format_id": "b", "height": 720, "ext": "mp4", "vcodec": "avc1", "filesize": 1200 * MB},
        {"format_id": "c", "height": 480, "ext": "mp4", "vcodec": "avc1", "filesize": 400 * MB},
    ]
    audio = [{"format_id": "s", "ext": "m4a", "acodec": "mp4a.40.2", "filesize": 900 * MB}]

    choice = select_tg_video_format(video, audio, [], LOCAL_BUDGET)

    assert choice.height == 480
# --- список разрешений для меню --------------------------------------------


@pytest.mark.unit
def test_menu_lists_every_available_resolution_from_high_to_low():
    """Меню обещает выбор, поэтому показывает все проходные разрешения."""
    options = list_video_options(VIDEO_ONLY, AUDIO_ONLY, COMBINED, LOCAL_BUDGET)

    assert [option.resolution for option in options] == [2160, 1440, 1080, 720, 480, 360]


@pytest.mark.unit
def test_menu_pairs_1080p_with_the_telegram_ready_pair():
    options = list_video_options(VIDEO_ONLY, AUDIO_ONLY, COMBINED, LOCAL_BUDGET)
    option = next(o for o in options if o.resolution == 1080)

    assert option.format_id == "299+140"
    assert option.size == int(301.2 * MB) + int(29.7 * MB)


@pytest.mark.unit
def test_ready_single_file_needs_no_audio_pairing():
    """У готового combined-формата звук уже внутри — склеивать нечего."""
    options = list_video_options(VIDEO_ONLY, AUDIO_ONLY, COMBINED, LOCAL_BUDGET)
    option = next(o for o in options if o.resolution == 360)

    assert option.format_id == "18"
    assert option.size == int(61.7 * MB)


@pytest.mark.unit
def test_tight_budget_leaves_only_what_actually_fits():
    """В облачном режиме под 50 МБ проходит лишь 480p, и то не на H.264."""
    options = list_video_options(VIDEO_ONLY, AUDIO_ONLY, COMBINED, CLOUD_BUDGET)

    assert [option.resolution for option in options] == [480]
    assert options[0].format_id == "397+139"
    assert options[0].size <= CLOUD_BUDGET


@pytest.mark.unit
def test_resolution_without_a_known_size_is_still_offered():
    """Размер неизвестен — это не повод скрывать разрешение от пользователя."""
    video_only = [
        {"format_id": "271", "height": 1440, "ext": "webm", "vcodec": "vp9"},
    ]

    options = list_video_options(video_only, AUDIO_ONLY, [], LOCAL_BUDGET)

    assert [option.resolution for option in options] == [1440]
    assert options[0].size == 0
    assert options[0].format_id == "271+140"


@pytest.mark.unit
def test_nothing_available_gives_an_empty_list():
    assert list_video_options([], [], [], LOCAL_BUDGET) == []


# --- список аудиодорожек для меню ------------------------------------------


@pytest.mark.unit
def test_audio_menu_offers_only_tracks_telegram_plays():
    """Opus в WebM Telegram аудио не считает — предлагать его нельзя."""
    options = list_audio_options(AUDIO_ONLY, LOCAL_BUDGET)

    assert [option.format_id for option in options] == ["140", "139"]
    assert all(option.ext == "m4a" for option in options)


@pytest.mark.unit
def test_audio_menu_puts_the_better_track_first():
    options = list_audio_options(AUDIO_ONLY, LOCAL_BUDGET)

    assert options[0].size > options[1].size


@pytest.mark.unit
def test_audio_menu_drops_tracks_over_the_budget():
    options = list_audio_options(AUDIO_ONLY, 20 * MB)

    assert [option.format_id for option in options] == ["139"]


@pytest.mark.unit
def test_audio_menu_keeps_a_track_without_a_known_size():
    options = list_audio_options(
        [{"format_id": "140", "ext": "m4a", "acodec": "mp4a.40.2"}], LOCAL_BUDGET
    )

    assert [option.size for option in options] == [0]


@pytest.mark.unit
def test_audio_menu_is_empty_without_a_playable_track():
    webm_only = [{"format_id": "251", "ext": "webm", "acodec": "opus", "filesize": MB}]

    assert list_audio_options(webm_only, LOCAL_BUDGET) == []


@pytest.mark.unit
def test_vertical_video_is_labelled_by_its_short_side():
    """У Shorts высота — длинная сторона: «1920p» вместо «1080p» сбивает с толку.

    Значения сняты с реального Shorts (youtube.com/shorts/oNmeHx8TIkk), где
    меню показывало 2560p, 1920p и 1280p.
    """
    video_only = [
        {"format_id": "400", "height": 2560, "width": 1440, "ext": "mp4",
         "vcodec": "av01", "filesize": 32 * MB},
        {"format_id": "299", "height": 1920, "width": 1080, "ext": "mp4",
         "vcodec": "avc1", "filesize": 27 * MB},
        {"format_id": "298", "height": 1280, "width": 720, "ext": "mp4",
         "vcodec": "avc1", "filesize": 17 * MB},
    ]
    audio = [{"format_id": "140", "ext": "m4a", "acodec": "mp4a.40.2", "filesize": MB}]

    options = list_video_options(video_only, audio, [], LOCAL_BUDGET)

    assert [option.resolution for option in options] == [1440, 1080, 720]


@pytest.mark.unit
def test_drc_twin_is_dropped_when_original_exists():
    """YouTube отдаёт `140` и `140-drc` одного размера — в меню нужен один."""
    from utils.youtube_utils import get_available_formats

    info = {
        "formats": [
            {
                "format_id": "140-drc",
                "ext": "m4a",
                "audio_channels": 2,
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "filesize": 107 * MB,
            },
            {
                "format_id": "140",
                "ext": "m4a",
                "audio_channels": 2,
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "filesize": 107 * MB,
            },
        ]
    }

    formats = get_available_formats(info, filter_by_size=False)

    assert [fmt["format_id"] for fmt in formats["audio_only"]] == ["140"]


@pytest.mark.unit
def test_lonely_drc_track_is_kept():
    """Без оригинала сжатая дорожка — единственный звук, выбрасывать нельзя."""
    from utils.youtube_utils import get_available_formats

    info = {
        "formats": [
            {
                "format_id": "141-drc",
                "ext": "m4a",
                "audio_channels": 2,
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "filesize": 200 * MB,
            }
        ]
    }

    formats = get_available_formats(info, filter_by_size=False)

    assert [fmt["format_id"] for fmt in formats["audio_only"]] == ["141-drc"]


@pytest.mark.unit
def test_unsized_fallback_tolerates_formats_without_height():
    """yt-dlp отдаёт `height: None`, и сортировка не должна падать на сравнении."""
    combined = [
        {"format_id": "a", "height": None, "filesize": None},
        {"format_id": "b", "height": 720, "filesize": None},
        {"format_id": "c", "height": 360, "filesize": None},
    ]

    # Без высоты формат встаёт в начало: a, c, b — и берётся нижняя треть.
    assert select_unsized_video_format(combined)["format_id"] == "c"
