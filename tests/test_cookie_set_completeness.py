"""Неполный набор auth-cookies обязан быть виден до того, как всё сломается.

Замер с прода: в рабочем файле YouTube осталась одна auth-cookie из шести, и всё
это время статус был зелёным. Сетевая проба тут не помощник — она отвечает на
вопрос «скачивается ли прямо сейчас», а скачиваться может и анонимно: то же
видео в разные часы одного дня и отбивалось с бот-чеком, и извлекалось без
cookies вовсе. Полнота набора — сигнал устойчивый, его и проверяем.

Вердикт локальный, поэтому здесь же правила чтения файла: они обязаны совпадать с
тем, что прочитает и отправит настоящий jar yt-dlp.
"""

import http.cookiejar
import time
import urllib.request

import pytest
import yt_dlp

from utils import cookie_health
from utils.cookie_workfile import WORK_DIR_NAME, working_cookie_file


pytestmark = pytest.mark.unit

HEADER = "# Netscape HTTP Cookie File\n"
PAST = 1_000_000_000
FUTURE = 4_102_444_800

YOUTUBE_AUTH = ("SID", "HSID", "SSID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID")
INSTAGRAM_AUTH = ("sessionid", "csrftoken", "ds_user_id")
TIKTOK_AUTH = ("sessionid", "sessionid_ss", "sid_tt", "uid_tt")

NO_RECORDS = "no cookie records found"
NO_AUTH = "required auth cookies are missing"
AUTH_EXPIRED = "all auth cookies are expired"
SESSION_EXPIRED = "session cookies are expired"
INCOMPLETE = "auth cookie set is incomplete, re-upload is needed"
ACTIVE = "auth cookies are active"
NO_HEADER = "Cookie file has no Netscape header"

requires_real_jar = pytest.mark.skipif(
    not hasattr(yt_dlp.cookies, "YoutubeDLCookieJar"),
    reason="conftest подставил заглушку yt-dlp без настоящего jar",
)


def _expired(*names):
    return [(name, PAST) for name in names]


def _text(cookies) -> str:
    """Имя без срока даёт сессионную cookie, пара `(имя, срок)` — срочную."""
    rows = []
    for cookie in cookies:
        name, expires = (cookie, 0) if isinstance(cookie, str) else cookie
        rows.append(f".example.com\tTRUE\t/\tTRUE\t{expires}\t{name}\tvalue\n")
    return HEADER + "".join(rows)


def _cookie_file(tmp_path, cookies):
    path = tmp_path / "cookies.txt"
    path.write_text(_text(cookies), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Ядро создаёт рабочую копию в `DATA_DIR`: настоящий каталог трогать нельзя."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


@pytest.fixture(autouse=True)
def clear_cache():
    cookie_health._COOKIE_HEALTH_CACHE.clear()
    yield
    cookie_health._COOKIE_HEALTH_CACHE.clear()


@pytest.fixture
def probe_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cookie_health,
        "_probe_authenticated_session",
        lambda platform, path: calls.append(platform) or "valid",
    )
    return calls


def test_eroded_set_is_reported_as_degraded(tmp_path, monkeypatch, probe_calls):
    """Ровно замеренный случай: осталась одна auth-cookie из шести и нет `LOGIN_INFO`."""
    path = _cookie_file(tmp_path, ["__Secure-3PSID", "PREF", "YSC"])
    monkeypatch.setitem(cookie_health.COOKIE_PATHS, "youtube", path)

    result = cookie_health.check_cookie_health("youtube", force=True)

    assert result.status == "degraded"
    assert result.auth_cookie_count == 1


def test_degraded_verdict_does_not_waste_a_network_probe(
    tmp_path, monkeypatch, probe_calls
):
    """Набор уже неполон — идти в сеть незачем, вердикт от этого не изменится."""
    path = _cookie_file(tmp_path, ["__Secure-3PSID"])
    monkeypatch.setitem(cookie_health.COOKIE_PATHS, "youtube", path)

    cookie_health.check_cookie_health("youtube", force=True)

    assert probe_calls == []


def test_full_set_is_probed_as_before(tmp_path, monkeypatch, probe_calls):
    path = _cookie_file(tmp_path, ["LOGIN_INFO", *YOUTUBE_AUTH])
    monkeypatch.setitem(cookie_health.COOKIE_PATHS, "youtube", path)

    result = cookie_health.check_cookie_health("youtube", force=True)

    assert probe_calls == ["youtube"]
    assert result.status == "valid"
    assert result.auth_cookie_count == 6


def test_most_of_the_set_still_counts_as_healthy(tmp_path, monkeypatch, probe_calls):
    """Экспорты слегка различаются — нельзя дёргать админа из-за одной cookie."""
    path = _cookie_file(tmp_path, ["LOGIN_INFO", *YOUTUBE_AUTH[:-1]])
    monkeypatch.setitem(cookie_health.COOKIE_PATHS, "youtube", path)

    assert cookie_health.check_cookie_health("youtube", force=True).status == "valid"


def test_admin_panel_can_render_the_new_status():
    """Статус, который админ увидит как сырое английское слово, бесполезен."""
    from utils import cookie_manager

    source = __import__("inspect").getsource(cookie_manager._build_cookie_health_text)

    assert '"degraded"' in source
    assert cookie_manager._format_health_icon("degraded") != ""


# --- проверяется рабочая копия, а не оригинал ---------------------------------


def test_eroded_working_copy_outweighs_a_full_original(tmp_path, monkeypatch, probe_calls):
    """Замер 01.11.2026: в оригинале всё, а в копии одна auth-cookie и нет `LOGIN_INFO`."""
    path = _cookie_file(tmp_path, ["LOGIN_INFO", *YOUTUBE_AUTH])
    original_bytes = path.read_bytes()
    working_cookie_file(path).write_text(_text(["__Secure-3PSID"]), encoding="utf-8")
    monkeypatch.setitem(cookie_health.COOKIE_PATHS, "youtube", path)

    result = cookie_health.check_cookie_health("youtube", force=True)

    assert result.status == "degraded"
    assert result.auth_cookie_count == 1
    assert path.read_bytes() == original_bytes


def test_cookie_renewed_in_the_working_copy_counts_as_live(
    tmp_path, monkeypatch, isolated_data_dir
):
    """Замер 01.11.2026: в оригинале cookie истекла, а в копии платформа её продлила."""
    path = _cookie_file(tmp_path, [("sessionid", PAST), "csrftoken", "ds_user_id"])
    assert cookie_health.inspect_cookie_file("instagram", path).status == "expired"
    copy = working_cookie_file(path)
    copy.write_text(_text([("sessionid", FUTURE), "csrftoken", "ds_user_id"]), encoding="utf-8")
    monkeypatch.setitem(cookie_health.COOKIE_PATHS, "instagram", path)
    probed = []
    monkeypatch.setattr(
        cookie_health,
        "_probe_authenticated_session",
        lambda platform, target: probed.append(target) or "valid",
    )

    result = cookie_health.check_cookie_health("instagram", force=True)

    assert result.status == "valid"
    assert result.session_expires_at == FUTURE
    assert probed == [copy]
    assert copy.parent == isolated_data_dir / WORK_DIR_NAME


# --- таблица локальных вердиктов ----------------------------------------------


LOCAL_VERDICTS = {
    "youtube-no-records": ("youtube", [], "invalid_format", NO_RECORDS),
    "youtube-no-auth": ("youtube", ["PREF", "LOGIN_INFO"], "invalid_format", NO_AUTH),
    "youtube-no-live-auth": (
        "youtube",
        ["LOGIN_INFO", *_expired(*YOUTUBE_AUTH)],
        "expired",
        AUTH_EXPIRED,
    ),
    "youtube-session-expired": (
        "youtube",
        [*_expired("LOGIN_INFO"), *YOUTUBE_AUTH],
        "expired",
        SESSION_EXPIRED,
    ),
    "youtube-no-session": ("youtube", YOUTUBE_AUTH, "degraded", INCOMPLETE),
    "youtube-under-half": ("youtube", ["LOGIN_INFO", "SID", "HSID"], "degraded", INCOMPLETE),
    "youtube-exactly-half": ("youtube", ["LOGIN_INFO", "SID", "HSID", "SSID"], "valid", ACTIVE),
    "youtube-full": ("youtube", ["LOGIN_INFO", *YOUTUBE_AUTH], "valid", ACTIVE),
    "instagram-no-records": ("instagram", [], "invalid_format", NO_RECORDS),
    "instagram-no-auth": ("instagram", ["mid"], "invalid_format", NO_AUTH),
    "instagram-no-live-auth": ("instagram", _expired(*INSTAGRAM_AUTH), "expired", AUTH_EXPIRED),
    "instagram-session-expired": (
        "instagram",
        [*_expired("sessionid"), "csrftoken", "ds_user_id"],
        "expired",
        SESSION_EXPIRED,
    ),
    "instagram-no-session": ("instagram", ["csrftoken", "ds_user_id"], "degraded", INCOMPLETE),
    "instagram-under-half": ("instagram", ["sessionid"], "degraded", INCOMPLETE),
    "instagram-over-half": ("instagram", ["sessionid", "csrftoken"], "valid", ACTIVE),
    "instagram-full": ("instagram", INSTAGRAM_AUTH, "valid", ACTIVE),
    "tiktok-no-records": ("tiktok", [], "invalid_format", NO_RECORDS),
    "tiktok-no-auth": ("tiktok", ["tt_webid"], "invalid_format", NO_AUTH),
    "tiktok-no-live-auth": ("tiktok", _expired(*TIKTOK_AUTH), "expired", AUTH_EXPIRED),
    "tiktok-all-sessions-expired": (
        "tiktok",
        [*_expired("sessionid", "sid_tt"), "sessionid_ss", "uid_tt"],
        "expired",
        SESSION_EXPIRED,
    ),
    "tiktok-uid-alone-is-no-session": ("tiktok", ["uid_tt"], "degraded", INCOMPLETE),
    "tiktok-no-session": ("tiktok", ["sessionid_ss", "uid_tt"], "degraded", INCOMPLETE),
    "tiktok-under-half": ("tiktok", ["sessionid"], "degraded", INCOMPLETE),
    "tiktok-sid-tt-is-a-session": ("tiktok", ["sid_tt", "uid_tt"], "valid", ACTIVE),
    "tiktok-one-of-two-sessions-expired": (
        "tiktok",
        [*_expired("sessionid"), "sid_tt", "sessionid_ss", "uid_tt"],
        "valid",
        ACTIVE,
    ),
    "tiktok-full": ("tiktok", TIKTOK_AUTH, "valid", ACTIVE),
}


@pytest.mark.parametrize(
    "platform, cookies, status, summary",
    list(LOCAL_VERDICTS.values()),
    ids=list(LOCAL_VERDICTS),
)
def test_local_verdict(tmp_path, platform, cookies, status, summary):
    result = cookie_health.inspect_cookie_file(platform, _cookie_file(tmp_path, cookies))

    assert (result.status, result.summary) == (status, summary)


def test_expired_cookies_do_not_count_towards_completeness(tmp_path):
    """Строка в файле осталась, но запрос такая cookie уже не поддержит."""
    path = _cookie_file(tmp_path, ["LOGIN_INFO", "SID", *_expired(*YOUTUBE_AUTH[1:])])

    result = cookie_health.inspect_cookie_file("youtube", path)

    assert result.status == "degraded"
    assert (result.auth_cookie_count, result.active_auth_cookie_count) == (6, 1)


@pytest.mark.parametrize(
    "cookies, expected",
    [
        pytest.param(
            [("sessionid", FUTURE), ("sid_tt", FUTURE + 100), "uid_tt"], FUTURE, id="earliest"
        ),
        pytest.param(["sessionid", ("sid_tt", FUTURE), "uid_tt"], FUTURE, id="zero-is-skipped"),
        pytest.param(
            [("sessionid", PAST), ("sid_tt", FUTURE), "uid_tt"], FUTURE, id="dead-is-skipped"
        ),
        pytest.param(
            [("sessionid", FUTURE), ("uid_tt", FUTURE - 100)], FUTURE, id="only-session-cookies"
        ),
        pytest.param(["sessionid", "sid_tt", "uid_tt"], None, id="no-date"),
    ],
)
def test_session_expiry(tmp_path, cookies, expected):
    result = cookie_health.inspect_cookie_file("tiktok", _cookie_file(tmp_path, cookies))

    assert result.status == "valid"
    assert result.session_expires_at == expected


# --- парсер читает файл по правилам jar ---------------------------------------


@pytest.mark.parametrize(
    "row, expected",
    [
        pytest.param(
            ".example.com\tTRUE\t/\tTRUE\t0\tname\t\n",
            (".example.com", "name", "", 0),
            id="empty-value-with-trailing-tab",
        ),
        pytest.param(
            ".example.com\tTRUE\t/\tTRUE\t4102444800.5\tname\tv\n",
            (".example.com", "name", "v", 4_102_444_800),
            id="float-expires",
        ),
        pytest.param(
            ".example.com\tTRUE\t/\tTRUE\t\tname\tv\n",
            (".example.com", "name", "v", 0),
            id="empty-expires",
        ),
        pytest.param(
            "#HttpOnly_.example.com\tTRUE\t/\tTRUE\t0\tname\tv\n",
            (".example.com", "name", "v", 0),
            id="http-only-prefix",
        ),
    ],
)
def test_parser_reads_the_row(tmp_path, row, expected):
    path = tmp_path / "cookies.txt"
    path.write_text(HEADER + row, encoding="utf-8")

    records = cookie_health._read_netscape_cookies(path)

    assert [(r["domain"], r["name"], r["value"], r["expires"]) for r in records] == [expected]


def test_http_only_auth_cookie_is_counted(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text(
        HEADER
        + "#HttpOnly_.example.com\tTRUE\t/\tTRUE\t0\tsessionid\tv\n"
        + "#HttpOnly_.example.com\tTRUE\t/\tTRUE\t0\tcsrftoken\tv\n",
        encoding="utf-8",
    )

    assert cookie_health.inspect_cookie_file("instagram", path).status == "valid"


def test_empty_value_is_not_an_auth_cookie(tmp_path):
    """Jar пишет такую строку с табом на конце: она читается, но сессии в ней нет."""
    path = tmp_path / "cookies.txt"
    path.write_text(HEADER + ".example.com\tTRUE\t/\tTRUE\t0\tsessionid\t\n", encoding="utf-8")

    result = cookie_health.inspect_cookie_file("instagram", path)

    assert (result.status, result.summary) == ("invalid_format", NO_AUTH)


def test_crlf_does_not_get_in_the_way(tmp_path):
    """Файл, сохранённый редактором в Windows."""
    path = tmp_path / "cookies.txt"
    path.write_bytes(_text(INSTAGRAM_AUTH).replace("\n", "\r\n").encode("utf-8"))

    result = cookie_health.inspect_cookie_file("instagram", path)
    records = cookie_health._read_netscape_cookies(path)

    assert result.status == "valid"
    assert {record["value"] for record in records} == {"value"}


@pytest.mark.parametrize(
    "content, summary",
    [
        pytest.param("", NO_RECORDS, id="empty-file"),
        pytest.param(HEADER, NO_RECORDS, id="only-header"),
        pytest.param(
            ".example.com\tTRUE\t/\tTRUE\t0\tsessionid\tv\n", NO_HEADER, id="no-header"
        ),
        pytest.param(
            HEADER + ".example.com\tTRUE\t/\tTRUE\tinf\tsessionid\tv\n",
            "Invalid cookie expiration value at line 2",
            id="inf-expires",
        ),
        pytest.param(
            HEADER + ".example.com\tTRUE\t/\tTRUE\tnan\tsessionid\tv\n",
            "Invalid cookie expiration value at line 2",
            id="nan-expires",
        ),
        pytest.param(
            HEADER + ".example.com\tTRUE\t/\tTRUE\tsoon\tsessionid\tv\n",
            "Invalid cookie expiration value at line 2",
            id="garbage-expires",
        ),
        pytest.param(
            HEADER + "только\tчетыре\tполя\tтут\n",
            "Invalid Netscape cookie row at line 2",
            id="short-row",
        ),
    ],
)
def test_unreadable_file_is_invalid_format(tmp_path, content, summary):
    path = tmp_path / "cookies.txt"
    path.write_text(content, encoding="utf-8")

    result = cookie_health.inspect_cookie_file("instagram", path)

    assert (result.status, result.summary) == ("invalid_format", summary)


# --- вердикт обязан совпадать с настоящим jar ---------------------------------


@requires_real_jar
@pytest.mark.parametrize(
    "first_line, readable",
    [
        pytest.param("# Netscape HTTP Cookie File\n", True, id="regular"),
        pytest.param("# HTTP Cookie File\n", True, id="no-netscape-word"),
        pytest.param("# NETSCAPE http COOKIE file\n", True, id="other-case"),
        pytest.param("﻿# Netscape HTTP Cookie File\n", False, id="bom"),
        pytest.param("\n", False, id="empty-first-line"),
        pytest.param("#LWP-Cookies-2.0\n", False, id="lwp-header"),
        pytest.param("# Netſcape HTTP Cookie File\n", False, id="unicode-case-folding"),
    ],
)
def test_header_verdict_matches_what_the_jar_reads(tmp_path, first_line, readable):
    path = tmp_path / "cookies.txt"
    path.write_text(first_line + _text(INSTAGRAM_AUTH)[len(HEADER) :], encoding="utf-8")

    jar = yt_dlp.cookies.YoutubeDLCookieJar(str(path))
    try:
        jar.load()
        jar_reads_it = True
    except http.cookiejar.LoadError:
        jar_reads_it = False
    result = cookie_health.inspect_cookie_file("instagram", path)

    assert jar_reads_it is readable
    assert result.status == ("valid" if readable else "invalid_format")
    assert (result.summary == NO_HEADER) is not readable


@requires_real_jar
@pytest.mark.filterwarnings("ignore:http.cookiejar bug")
@pytest.mark.parametrize(
    "domain, flag",
    [
        pytest.param(".example.com", "FALSE", id="dot-with-false"),
        pytest.param("example.com", "TRUE", id="no-dot-with-true"),
        pytest.param(" .example.com", "TRUE", id="leading-space"),
    ],
)
def test_row_the_jar_refuses_does_not_pass_the_local_check(tmp_path, domain, flag):
    path = tmp_path / "cookies.txt"
    path.write_text(
        _text(INSTAGRAM_AUTH) + f"{domain}\t{flag}\t/\tTRUE\t0\tmid\tv\n", encoding="utf-8"
    )

    with pytest.raises(http.cookiejar.LoadError):
        yt_dlp.cookies.YoutubeDLCookieJar(str(path)).load()

    assert cookie_health.inspect_cookie_file("instagram", path).status == "invalid_format"


@requires_real_jar
@pytest.mark.parametrize(
    "expires",
    [
        pytest.param("-1", id="negative"),
        pytest.param("1e10", id="exponent"),
        pytest.param(f" {FUTURE}", id="leading-space"),
    ],
)
def test_expiry_the_jar_skips_does_not_pass_the_local_check(tmp_path, capsys, expires):
    """Jar пропускает такую строку и печатает её целиком вместе со значением."""
    path = tmp_path / "cookies.txt"
    path.write_text(
        _text(INSTAGRAM_AUTH) + f".example.com\tTRUE\t/\tTRUE\t{expires}\tmid\tfake-secret\n",
        encoding="utf-8",
    )

    yt_dlp.cookies.YoutubeDLCookieJar(str(path)).load()
    assert "fake-secret" in capsys.readouterr().err

    result = cookie_health.inspect_cookie_file("instagram", path)

    assert result.status == "invalid_format"
    assert result.summary.startswith("Invalid cookie expiration value at line ")
    assert "fake-secret" not in result.summary


@requires_real_jar
def test_non_utf8_file_is_refused_as_by_the_jar(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_bytes(
        _text(INSTAGRAM_AUTH).encode()
        + b".example.com\tTRUE\t/\tTRUE\t0\tmid\tfake-\xff-value\n"
    )

    with pytest.raises(UnicodeDecodeError):
        yt_dlp.cookies.YoutubeDLCookieJar(str(path)).load()

    result = cookie_health.inspect_cookie_file("instagram", path)

    assert (result.status, result.summary) == (
        "invalid_format",
        "Cookie file is not valid UTF-8",
    )


@requires_real_jar
def test_indented_comment_is_ignored_as_by_the_jar(tmp_path):
    path = tmp_path / "cookies.txt"
    text = _text(INSTAGRAM_AUTH)
    path.write_text(text.replace(HEADER, HEADER + "  # заметка\n", 1), encoding="utf-8")

    jar = yt_dlp.cookies.YoutubeDLCookieJar(str(path))
    jar.load()

    assert {cookie.name for cookie in jar} == set(INSTAGRAM_AUTH)
    assert cookie_health.inspect_cookie_file("instagram", path).status == "valid"


@requires_real_jar
@pytest.mark.parametrize(
    "expires, sent",
    [
        pytest.param("0", True, id="zero-is-a-session-cookie"),
        pytest.param("", True, id="empty-is-a-session-cookie"),
        pytest.param(str(FUTURE), True, id="future"),
        pytest.param(f"{FUTURE}.5", True, id="fractional"),
        pytest.param(str(PAST), False, id="past"),
    ],
)
def test_live_verdict_matches_what_the_jar_sends(tmp_path, expires, sent):
    """`_is_live` обещает ровно то, что уйдёт в заголовке `Cookie`."""
    path = tmp_path / "cookies.txt"
    path.write_text(
        HEADER + f".tiktok.com\tTRUE\t/\tTRUE\t{expires}\tsessionid\tfake-value\n",
        encoding="utf-8",
    )
    jar = cookie_health._load_cookie_jar("tiktok", path)
    assert isinstance(jar, yt_dlp.cookies.YoutubeDLCookieJar)
    request = urllib.request.Request(cookie_health.TIKTOK_ACCOUNT_INFO_URL)

    jar.add_cookie_header(request)

    record = cookie_health._read_netscape_cookies(path)[0]
    assert (request.get_header("Cookie") == "sessionid=fake-value") is sent
    assert cookie_health._is_live(record, time.time()) is sent


@requires_real_jar
def test_parser_reads_what_the_jar_wrote(tmp_path):
    """Пустое значение и сессионная cookie в той форме, в какой их пишет yt-dlp."""
    path = tmp_path / "cookies.txt"
    path.write_text(
        HEADER
        + ".example.com\tTRUE\t/\tTRUE\t0\tempty\t\n"
        + f".example.com\tTRUE\t/\tTRUE\t{FUTURE}\tdated\tv\n",
        encoding="utf-8",
    )
    jar = yt_dlp.cookies.YoutubeDLCookieJar(str(path))
    jar.load()

    jar.save()

    assert "empty\t\n" in path.read_text(encoding="utf-8")
    records = cookie_health._read_netscape_cookies(path)
    assert {(r["name"], r["value"], r["expires"]) for r in records} == {
        ("empty", "", 0),
        ("dated", "v", FUTURE),
    }
