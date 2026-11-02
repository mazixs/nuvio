"""Проба cookies обязана мерить то, что действительно важно.

История трёх ошибок подряд, все замерены на проде.

Сначала проба читала 8192 байта тела и не находила признаков
неавторизованности, потому что на странице размером 583 077 байт они лежали на
позициях 18107, 18391 и 510926. Админу сообщалось «probe succeeded», пока
YouTube отбивал каждую ссылку.

Затем выяснилось, что для YouTube HTTP-проба непригодна в принципе: с этого
сервера `/feed/subscriptions` отдаёт стену согласия `consent.youtube.com`
**независимо от авторизации**, и на самой этой странице есть и
`accounts.google.com`, и `ServiceLogin`. С полным рабочим набором cookies, на
котором скачивание заведомо работает, проба всё равно рапортовала `stale`.

Поэтому cookies YouTube проверяются тем же способом, которым ими пользуются, —
попыткой извлечь видео. Публичный ролик извлекается и без входа, поэтому `valid`
требует ещё и живой `LOGIN_INFO` в файле, который yt-dlp сохранил после пробы.

Наконец, у Instagram отсутствие маркеров считалось успехом, и любая незнакомая
страница выглядела живой сессией. Теперь вход доказывает только ненулевой
`NON_FACEBOOK_USER_ID`, а всё непонятное даёт `probe_failed`.
"""

import http.client
import inspect
import io
import socket
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest
import yt_dlp

from utils import cookie_health


pytestmark = pytest.mark.unit

HEADER = "# Netscape HTTP Cookie File\n"
SECRET = "FAKE-SECRET-VALUE"

EDIT_URL = cookie_health.PROBE_CONFIG["instagram"]["url"]
LOGIN_URL = "https://www.instagram.com/accounts/login/?next=%2Faccounts%2Fedit%2F"
LOGGED_IN = '"NON_FACEBOOK_USER_ID":"12345"'
ANONYMOUS = '"NON_FACEBOOK_USER_ID":"0"'
MEASURED_PROOF_OFFSET = 160_237
MEASURED_PAGE_SIZE = 528_417

requires_real_jar = pytest.mark.skipif(
    not hasattr(yt_dlp.cookies, "YoutubeDLCookieJar"),
    reason="conftest подставил заглушку yt-dlp без настоящего jar",
)


def _rows(domain: str, *names: str) -> str:
    return "".join(f"{domain}\tTRUE\t/\tTRUE\t0\t{name}\tvalue\n" for name in names)


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Ядро создаёт рабочую копию в `DATA_DIR`: настоящий каталог трогать нельзя."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


@pytest.fixture
def cookie_file(tmp_path):
    path = tmp_path / "cookies.txt"
    # Набор полный: иначе вердикт станет `degraded` ещё до сетевой пробы, и
    # тесты пробы будут проверять не то, что заявляют.
    rows = _rows(".instagram.com", "sessionid", "csrftoken", "ds_user_id")
    path.write_text(HEADER + rows, encoding="utf-8")
    return path


YOUTUBE_ROWS = _rows(
    ".youtube.com",
    "LOGIN_INFO",
    "SID",
    "HSID",
    "SSID",
    "SAPISID",
    "__Secure-1PSID",
    "__Secure-3PSID",
)


@pytest.fixture
def youtube_file(tmp_path):
    path = tmp_path / "www.youtube.com_cookies.txt"
    path.write_text(HEADER + YOUTUBE_ROWS, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def clear_cache():
    cookie_health._COOKIE_HEALTH_CACHE.clear()
    yield
    cookie_health._COOKIE_HEALTH_CACHE.clear()


def test_parallel_forced_checks_share_one_probe(monkeypatch, cookie_file):
    monkeypatch.setitem(cookie_health.COOKIE_PATHS, "instagram", cookie_file)
    barrier = threading.Barrier(2)
    calls = []

    def probe(platform, path):
        calls.append(platform)
        time.sleep(0.1)
        return "valid"

    monkeypatch.setattr(cookie_health, "_probe_authenticated_session", probe)

    def check():
        barrier.wait()
        return cookie_health.check_cookie_health("instagram", force=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: check(), range(2)))

    assert calls == ["instagram"]
    assert results[0] == results[1]


def test_cache_serves_repeat_checks_until_forced(monkeypatch, cookie_file):
    monkeypatch.setitem(cookie_health.COOKIE_PATHS, "instagram", cookie_file)
    calls = []
    monkeypatch.setattr(
        cookie_health,
        "_probe_authenticated_session",
        lambda platform, path: calls.append(platform) or "valid",
    )

    cookie_health.check_cookie_health("instagram")
    cookie_health.check_cookie_health("instagram")
    assert len(calls) == 1

    cookie_health.check_cookie_health("instagram", force=True)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "platform, probe",
    [
        ("youtube", "_probe_with_ytdlp"),
        ("instagram", "_probe_instagram_session"),
        ("tiktok", "_probe_tiktok_session"),
    ],
)
def test_every_platform_gets_a_live_probe(monkeypatch, tmp_path, platform, probe):
    monkeypatch.setattr(cookie_health, probe, lambda *a: "rate_limited")

    status = cookie_health._probe_authenticated_session(platform, tmp_path / "cookies.txt")

    assert status == "rate_limited"


def test_not_supported_status_is_gone():
    """TikTok раньше получал `not_supported`: у него теперь своя проба."""
    assert "not_supported" not in inspect.getsource(cookie_health)
    assert "not_supported" not in cookie_health._PROBE_SUMMARIES


# --- проба YouTube: реальное извлечение --------------------------------------


def _youtube_probe(monkeypatch, youtube_file, failure: Exception | None) -> str:
    def _extract(url, cookiefile, session_id=None):
        if failure:
            raise failure

    monkeypatch.setattr(cookie_health, "_extract_with_cookies", _extract)
    return cookie_health._probe_authenticated_session("youtube", youtube_file)


def test_extraction_with_session_cookie_is_valid_but_promises_no_more(
    monkeypatch, youtube_file
):
    """Публичный ролик извлекается и без входа: summary не вправе обещать сессию."""
    monkeypatch.setitem(cookie_health.COOKIE_PATHS, "youtube", youtube_file)
    monkeypatch.setattr(cookie_health, "_extract_with_cookies", lambda *a: None)

    result = cookie_health.check_cookie_health("youtube", force=True)

    assert result.status == "valid"
    assert result.summary == "session cookies are present and the test video was extracted"
    assert result.summary != cookie_health._PROBE_SUMMARIES["valid"]


def _drop_login_info(path) -> None:
    kept = [
        line
        for line in path.read_text(encoding="utf-8").splitlines(keepends=True)
        if "LOGIN_INFO" not in line
    ]
    path.write_text("".join(kept), encoding="utf-8")


def _blank_login_info(path) -> None:
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("LOGIN_INFO\tvalue", "LOGIN_INFO\t"), encoding="utf-8")


def _corrupt(path) -> None:
    path.write_text(HEADER + "обрезанная строка\n", encoding="utf-8")


@pytest.mark.parametrize(
    "damage, expected",
    [
        pytest.param(_drop_login_info, "stale", id="login-info-removed"),
        pytest.param(_blank_login_info, "stale", id="login-info-blanked"),
        pytest.param(_corrupt, "probe_failed", id="file-corrupted"),
    ],
)
def test_file_saved_by_the_probe_decides_the_verdict(
    monkeypatch, youtube_file, damage, expected
):
    """yt-dlp сохраняет ответ в файл: своя же проба могла лишить его сессии."""
    original_bytes = youtube_file.read_bytes()
    monkeypatch.setitem(cookie_health.COOKIE_PATHS, "youtube", youtube_file)
    monkeypatch.setattr(
        cookie_health,
        "_extract_with_cookies",
        lambda url, cookiefile, session_id=None: damage(cookiefile),
    )

    result = cookie_health.check_cookie_health("youtube", force=True)

    assert result.status == expected
    assert youtube_file.read_bytes() == original_bytes


def test_ytdlp_probe_uses_the_given_path_without_copying_it_again(
    monkeypatch, youtube_file, isolated_data_dir
):
    """Копию создаёт полная проверка: копия копии разорвала бы продление cookies."""
    seen = []
    monkeypatch.setattr(
        cookie_health,
        "_extract_with_cookies",
        lambda url, cookiefile, session_id=None: seen.append(cookiefile),
    )

    assert cookie_health._probe_with_ytdlp("youtube", youtube_file) == "valid"

    assert seen == [youtube_file]
    assert not isolated_data_dir.exists()


def test_bot_check_means_cookies_are_stale(monkeypatch, youtube_file):
    """Ровно то сообщение, которым YouTube отбивал бота на проде."""
    failure = Exception(
        "ERROR: [youtube] q_kjm-MPlps: Sign in to confirm you're not a bot. "
        "Use --cookies-from-browser or --cookies for the authentication."
    )

    assert _youtube_probe(monkeypatch, youtube_file, failure) == "stale"


def test_rate_limit_is_not_blamed_on_cookies(monkeypatch, youtube_file):
    failure = Exception("HTTP Error 429: Too Many Requests")

    assert _youtube_probe(monkeypatch, youtube_file, failure) == "rate_limited"


def test_unrelated_failure_does_not_accuse_the_cookies(monkeypatch, youtube_file):
    """Удалённое видео или сетевой сбой — не повод объявлять cookies мёртвыми.

    Иначе проба превращается в постоянную ложную тревогу, как это уже случилось
    со стеной согласия.
    """
    failure = Exception("Video unavailable. This video has been removed by the uploader")

    assert _youtube_probe(monkeypatch, youtube_file, failure) == "probe_failed"


def test_youtube_no_longer_relies_on_the_consent_walled_page():
    """Стена согласия отдаётся независимо от логина — HTTP-проба тут бесполезна."""
    assert "youtube" not in cookie_health.PROBE_CONFIG
    assert "youtube" in cookie_health.YTDLP_PROBE_URLS


# --- проба Instagram: вход доказывает только значение идентификатора ---------


class _CutBody(io.BytesIO):
    """Тело ответа, оборванное на полпути."""

    def read(self, *_):
        raise http.client.IncompleteRead(b"")


def _http_error(code: int, body: io.BytesIO) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(EDIT_URL, code, "error", None, body)


def _fake_opener(url: str = EDIT_URL, body: str = "", failure=None, read_failure=None):
    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def geturl(self):
            return url

        def read(self, size=None):
            if read_failure:
                raise read_failure
            return body.encode("utf-8")[:size] if size else body.encode("utf-8")

    class _Opener:
        def open(self, _request, timeout=None):
            if failure:
                raise failure
            return _Response()

    return _Opener()


def _instagram_probe(monkeypatch, cookie_file, **response) -> str:
    monkeypatch.setattr(
        urllib.request, "build_opener", lambda *a, **k: _fake_opener(**response)
    )
    return cookie_health._probe_authenticated_session("instagram", cookie_file)


@pytest.mark.parametrize(
    "final_url, body, expected",
    [
        pytest.param(LOGIN_URL, "", "stale", id="redirect-to-login"),
        pytest.param(LOGIN_URL.upper(), "", "stale", id="redirect-in-other-case"),
        pytest.param(LOGIN_URL, LOGGED_IN, "stale", id="redirect-outweighs-the-id"),
        pytest.param(EDIT_URL, LOGGED_IN, "valid", id="id-in-quotes"),
        pytest.param(EDIT_URL, '"NON_FACEBOOK_USER_ID":12345', "valid", id="id-without-quotes"),
        pytest.param(
            EDIT_URL, LOGGED_IN + " Try again later", "valid", id="id-outweighs-the-rate-limit"
        ),
        pytest.param(EDIT_URL, ANONYMOUS, "probe_failed", id="anonymous-id-in-quotes"),
        pytest.param(
            EDIT_URL, '"NON_FACEBOOK_USER_ID":0', "probe_failed", id="anonymous-id-without-quotes"
        ),
        pytest.param(
            EDIT_URL, "Please wait a few minutes", "rate_limited", id="rate-limit-please-wait"
        ),
        pytest.param(EDIT_URL, "try again LATER", "rate_limited", id="rate-limit-other-case"),
        pytest.param(
            EDIT_URL, ANONYMOUS + " Try again later", "rate_limited", id="anonymous-rate-limited"
        ),
        pytest.param(EDIT_URL, "/accounts/login", "probe_failed", id="login-path-only-in-body"),
        pytest.param(EDIT_URL, "<html>проверка аккаунта</html>", "probe_failed", id="unfamiliar"),
    ],
)
def test_instagram_page_verdict(final_url, body, expected):
    assert cookie_health._classify_instagram_page(final_url, body) == expected


@pytest.mark.parametrize(
    "url, body, expected",
    [
        pytest.param(EDIT_URL, LOGGED_IN, "valid", id="logged-in"),
        pytest.param(LOGIN_URL, "", "stale", id="redirect-to-login"),
        pytest.param(
            EDIT_URL, "страница настроек без признаков логина", "probe_failed", id="clean-page"
        ),
    ],
)
def test_instagram_probe_verdict_comes_from_the_page(
    monkeypatch, cookie_file, url, body, expected
):
    status = _instagram_probe(monkeypatch, cookie_file, url=url, body=body)

    assert status == expected


@pytest.mark.parametrize(
    "offset, expected",
    [
        pytest.param(MEASURED_PROOF_OFFSET, "valid", id="measured-offset"),
        pytest.param(
            cookie_health.PROBE_BODY_READ_BYTES, "probe_failed", id="beyond-the-read-window"
        ),
    ],
)
def test_login_proof_is_searched_in_the_whole_read_window(
    monkeypatch, cookie_file, offset, expected
):
    """Замер: страница 528 417 байт, позиция 160 237. Прежние 96 КБ до неё не доставали."""
    body = ("x" * offset + LOGGED_IN).ljust(MEASURED_PAGE_SIZE, "y")

    assert _instagram_probe(monkeypatch, cookie_file, body=body) == expected


@pytest.mark.parametrize(
    "code, body, expected",
    [
        pytest.param(429, b"", "rate_limited", id="http-429"),
        pytest.param(401, b"", "stale", id="http-401"),
        pytest.param(403, b"", "stale", id="http-403"),
        pytest.param(500, b"Please wait a few minutes", "rate_limited", id="http-500-marker"),
        pytest.param(500, b"", "probe_failed", id="http-500"),
    ],
)
def test_instagram_http_error_verdict(monkeypatch, cookie_file, code, body, expected):
    failure = _http_error(code, io.BytesIO(body))

    assert _instagram_probe(monkeypatch, cookie_file, failure=failure) == expected


@pytest.mark.parametrize(
    "response",
    [
        pytest.param({"failure": TimeoutError()}, id="timeout"),
        pytest.param({"read_failure": http.client.IncompleteRead(b"")}, id="cut-body"),
    ],
)
def test_instagram_transport_failure_is_not_a_verdict(monkeypatch, cookie_file, response):
    assert _instagram_probe(monkeypatch, cookie_file, **response) == "probe_failed"


def test_cut_error_body_is_not_a_crash(monkeypatch, cookie_file):
    failure = _http_error(503, _CutBody())

    assert _instagram_probe(monkeypatch, cookie_file, failure=failure) == "probe_failed"


def test_probe_verdict_reaches_the_admin(monkeypatch, cookie_file):
    """Проба может быть честной, но бесполезной, если вердикт её игнорирует."""
    monkeypatch.setitem(cookie_health.COOKIE_PATHS, "instagram", cookie_file)
    monkeypatch.setattr(
        cookie_health, "_probe_authenticated_session", lambda *a: "stale"
    )

    result = cookie_health.check_cookie_health("instagram", force=True)

    assert result.status == "stale"


def test_unreadable_path_is_not_a_crash(monkeypatch, tmp_path):
    """Каталог на месте файла читается так же, как файл без прав: отказом ОС.

    Панель вызывает проверку без `try`, поэтому исключение оставило бы её без ответа.
    """
    directory = tmp_path / "cookies.txt"
    directory.mkdir()
    monkeypatch.setitem(cookie_health.COOKIE_PATHS, "instagram", directory)

    result = cookie_health.check_cookie_health("instagram", force=True)

    assert (result.status, result.summary) == (
        "invalid_format",
        "cookie file cannot be read",
    )
    assert str(tmp_path) not in result.summary


# --- секреты: значение cookie не покидает файл --------------------------------


@pytest.mark.parametrize(
    "row, summary",
    [
        pytest.param(
            f".example.com\tTRUE\t/\t{SECRET}\n",
            "Invalid Netscape cookie row at line 3",
            id="broken-row",
        ),
        pytest.param(
            f".example.com\tTRUE\t/\tTRUE\t{SECRET}\tsessionid\tvalue\n",
            "Invalid cookie expiration value at line 3",
            id="value-in-the-expires-field",
        ),
    ],
)
def test_summary_names_the_line_not_its_content(tmp_path, caplog, row, summary):
    """Summary уходит в панель, `bot.log` и краш-репорт."""
    path = tmp_path / "cookies.txt"
    path.write_text(HEADER + "# комментарий\n" + row, encoding="utf-8")

    result = cookie_health.inspect_cookie_file("instagram", path)

    assert result.status == "invalid_format"
    assert result.summary == summary
    assert SECRET not in caplog.text


@pytest.mark.filterwarnings("ignore:http.cookiejar bug")
@pytest.mark.parametrize("platform", ["instagram", "tiktok"])
def test_unreadable_jar_does_not_quote_the_file(tmp_path, caplog, platform):
    """`LoadError` цитирует строку целиком: `.домен` с FALSE jar читать откажется."""
    path = tmp_path / "cookies.txt"
    path.write_text(
        HEADER + f".example.com\tFALSE\t/\tTRUE\t0\tsessionid\t{SECRET}\n", encoding="utf-8"
    )

    assert cookie_health._probe_authenticated_session(platform, path) == "probe_failed"

    assert "LoadError" in caplog.text
    assert SECRET not in caplog.text


def test_probe_failure_text_stays_out_of_the_log(monkeypatch, youtube_file, caplog):
    """Текст замерен: так yt-dlp отвечает на строку, которую не смог прочитать jar."""
    failure = Exception(
        "ERROR: invalid Netscape format cookies file 'cookies.txt': "
        f"'.youtube.com\\tFALSE\\t/\\tTRUE\\t0\\tLOGIN_INFO\\t{SECRET}'"
    )

    assert _youtube_probe(monkeypatch, youtube_file, failure) == "probe_failed"

    assert SECRET not in caplog.text


@requires_real_jar
@pytest.mark.filterwarnings("ignore:http.cookiejar bug")
def test_row_the_jar_refuses_stays_out_of_the_log(monkeypatch, tmp_path, caplog):
    """Такой файл не доходит до yt-dlp: его jar процитировал бы строку со значением."""
    attempts = []

    def forbid(*args, **kwargs):
        attempts.append(args)
        raise OSError("сеть в тестах запрещена")

    monkeypatch.setattr(socket, "getaddrinfo", forbid)
    monkeypatch.setattr(socket, "create_connection", forbid)
    path = tmp_path / "www.youtube.com_cookies.txt"
    refused = f".youtube.com\tFALSE\t/\tTRUE\t0\tLOGIN_INFO\t{SECRET}\n"
    path.write_text(
        HEADER + YOUTUBE_ROWS.replace(".youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\tvalue\n", refused),
        encoding="utf-8",
    )
    monkeypatch.setitem(cookie_health.COOKIE_PATHS, "youtube", path)

    result = cookie_health.check_cookie_health("youtube", force=True)

    assert result.status == "invalid_format"
    assert attempts == []
    assert SECRET not in caplog.text


@requires_real_jar
def test_row_the_jar_skips_stays_out_of_the_output(monkeypatch, cookie_file, capsys):
    """Jar пропустил бы эту строку, напечатав её со значением в логи контейнера."""
    with cookie_file.open("a", encoding="utf-8") as file:
        file.write(f".instagram.com\tTRUE\t/\tTRUE\t-1\tmid\t{SECRET}\n")
    monkeypatch.setitem(cookie_health.COOKIE_PATHS, "instagram", cookie_file)
    monkeypatch.setattr(
        urllib.request, "build_opener", lambda *a, **k: _fake_opener(body=LOGGED_IN)
    )

    result = cookie_health.check_cookie_health("instagram", force=True)

    assert result.status == "invalid_format"
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
