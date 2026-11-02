"""Проба TikTok спрашивает у платформы, чья это сессия, и сохраняет продление.

Публичные видео скачиваются и без входа, поэтому извлечение ролика сессию не
проверяет. `passport/web/account/info` отвечает прямо и заодно продлевает сессию:
01.11.2026 токен остался прежним, а срок сдвинулся с 12.02.2027 на 30.04.2027. Кроме
этого ответа дату в файле никто не обновляет, и в указанный там день cookie перестала
бы отправляться. Поэтому продление сохраняется, но только в рабочую копию и только
после подтверждённой сессии.

Формы ответов замерены, значения выдуманы. В настоящем ответе лежат email, телефон и
`session_key`: ни в лог, ни в результат они попасть не должны.
"""

import email.message
import email.utils
import http.client
import io
import json
import urllib.request
import urllib.response

import pytest

from utils import cookie_health
from utils.cookie_workfile import working_cookie_file


pytestmark = pytest.mark.unit

HEADER = "# Netscape HTTP Cookie File\n"
OLD_EXPIRY = 4_102_444_800
NEW_EXPIRY = OLD_EXPIRY + 77 * 24 * 60 * 60
SESSION_VALUE = "fake-session-cookie"

LIVE_BODY = (
    b'{"message":"success","data":{"user_id_str":"111","screen_name":"x",'
    b'"email":"e@example.com","mobile":"+10000000000","session_key":"fake-session-key"}}'
)
DEAD_BODY = (
    b'{"data":{"description":"session expired, please sign in again",'
    b'"error_code":13,"name":"session_expired"},"message":"error"}'
)
PRIVATE_VALUES = ("e@example.com", "+10000000000", "fake-session-key", SESSION_VALUE)

# Тот же токен с новым сроком: так TikTok продлил сессию в замере.
RENEWAL = (
    f"sessionid={SESSION_VALUE}; Domain=.tiktok.com; Path=/; "
    f"Expires={email.utils.formatdate(NEW_EXPIRY, usegmt=True)}; Secure"
)


def _row(name: str, expires: int, value: str = "fake-value") -> str:
    return f".tiktok.com\tTRUE\t/\tTRUE\t{expires}\t{name}\t{value}\n"


COOKIES = (
    HEADER
    + _row("sessionid", OLD_EXPIRY, SESSION_VALUE)
    + _row("sessionid_ss", OLD_EXPIRY)
    + _row("sid_tt", 0)
    + _row("uid_tt", OLD_EXPIRY)
)


class _FakeTikTok(urllib.request.BaseHandler):
    """Подменяет только сокет: заголовки и cookies разбирает настоящий urllib."""

    handler_order = 100  # Раньше штатного HTTPSHandler, до сети дело не доходит.

    def __init__(self, status=200, body=LIVE_BODY, set_cookie=None, failure=None, read_failure=None):
        self.status = status
        self.body = body
        self.set_cookie = set_cookie
        self.failure = failure
        self.read_failure = read_failure

    def https_open(self, request):
        if self.failure:
            raise self.failure
        headers = email.message.Message()
        if self.set_cookie:
            headers["Set-Cookie"] = self.set_cookie
        read_failure = self.read_failure

        class _Body(io.BytesIO):
            def read(self, *args):
                if read_failure:
                    raise read_failure
                return super().read(*args)

        response = urllib.response.addinfourl(
            _Body(self.body), headers, request.full_url, self.status
        )
        response.msg = http.client.responses.get(self.status, "")
        return response


@pytest.fixture
def respond_with(monkeypatch):
    """Подставляет ответ TikTok, оставляя в цепочке настоящий `HTTPCookieProcessor`."""
    real_build_opener = urllib.request.build_opener

    def install(**response):
        monkeypatch.setattr(
            urllib.request,
            "build_opener",
            lambda *handlers: real_build_opener(
                urllib.request.ProxyHandler({}), *handlers, _FakeTikTok(**response)
            ),
        )

    return install


@pytest.fixture(autouse=True)
def clear_cache():
    cookie_health._COOKIE_HEALTH_CACHE.clear()
    yield
    cookie_health._COOKIE_HEALTH_CACHE.clear()


@pytest.fixture
def original(tmp_path, monkeypatch):
    """Файл админа; рабочая копия уходит во временный `DATA_DIR`, а не в настоящий."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    path = tmp_path / "www.tiktok.com_cookies.txt"
    path.write_text(COOKIES, encoding="utf-8")
    monkeypatch.setitem(cookie_health.COOKIE_PATHS, "tiktok", path)
    return path


@pytest.fixture
def working_copy(original):
    return working_cookie_file(original)


def _cookies_in(path) -> dict:
    return {record["name"]: record for record in cookie_health._read_netscape_cookies(path)}


# --- вердикт -----------------------------------------------------------------


@pytest.mark.parametrize(
    "payload, expected",
    [
        pytest.param(None, "probe_failed", id="null"),
        pytest.param(["success"], "probe_failed", id="not-a-dict"),
        pytest.param({"message": "success"}, "probe_failed", id="no-data"),
        pytest.param({"message": "success", "data": "x"}, "probe_failed", id="data-not-a-dict"),
        pytest.param(
            {"message": "success", "data": {"screen_name": "x"}},
            "probe_failed",
            id="success-without-user-id",
        ),
        pytest.param(
            {"message": "success", "data": {"user_id_str": ""}},
            "probe_failed",
            id="success-with-empty-user-id",
        ),
        pytest.param(
            {"message": "error", "data": {"user_id_str": "111"}},
            "probe_failed",
            id="user-id-without-success",
        ),
        pytest.param(
            {"message": "error", "data": {"name": "other", "error_code": 99}},
            "probe_failed",
            id="unknown-error",
        ),
        pytest.param(json.loads(LIVE_BODY), "valid", id="measured-live-session"),
        pytest.param(json.loads(DEAD_BODY), "stale", id="measured-dead-session"),
        pytest.param(
            {"message": "error", "data": {"name": "session_expired"}}, "stale", id="expired-by-name"
        ),
        pytest.param({"message": "error", "data": {"error_code": 13}}, "stale", id="expired-by-code"),
    ],
)
def test_account_info_verdict(payload, expected):
    assert cookie_health._classify_tiktok_account_info(payload) == expected


@pytest.mark.parametrize(
    "response, expected",
    [
        pytest.param({"body": LIVE_BODY}, "valid", id="live-session"),
        pytest.param({"body": DEAD_BODY}, "stale", id="dead-session"),
        pytest.param({"status": 429}, "rate_limited", id="http-429"),
        pytest.param({"status": 403}, "probe_failed", id="http-403"),
        pytest.param({"status": 500}, "probe_failed", id="http-500"),
        pytest.param({"body": b"<html>not json</html>"}, "probe_failed", id="not-json"),
        pytest.param(
            {"read_failure": http.client.IncompleteRead(b"")}, "probe_failed", id="cut-body"
        ),
        pytest.param({"failure": TimeoutError()}, "probe_failed", id="timeout"),
    ],
)
def test_probe_verdict(working_copy, respond_with, response, expected):
    respond_with(**response)

    assert cookie_health._probe_authenticated_session("tiktok", working_copy) == expected


# --- продление сессии --------------------------------------------------------


def test_renewal_lands_in_the_working_copy_and_nowhere_else(original, working_copy, respond_with):
    original_bytes = original.read_bytes()
    respond_with(set_cookie=RENEWAL)

    result = cookie_health.check_cookie_health("tiktok", force=True)

    assert result.status == "valid"
    assert result.session_expires_at == NEW_EXPIRY
    renewed = _cookies_in(working_copy)["sessionid"]
    assert (renewed["expires"], renewed["value"]) == (NEW_EXPIRY, SESSION_VALUE)
    assert original.read_bytes() == original_bytes


def test_session_cookie_without_a_date_survives_the_save(working_copy, respond_with):
    respond_with(set_cookie=RENEWAL)

    assert cookie_health._probe_tiktok_session(working_copy) == "valid"

    cookies = _cookies_in(working_copy)
    assert cookies.keys() == {"sessionid", "sessionid_ss", "sid_tt", "uid_tt"}
    assert (cookies["sid_tt"]["expires"], cookies["sid_tt"]["value"]) == (0, "fake-value")


@pytest.mark.parametrize(
    "response, expected",
    [
        pytest.param({"body": DEAD_BODY}, "stale", id="stale"),
        pytest.param({"status": 429}, "rate_limited", id="rate-limited"),
        pytest.param({"body": b"not json"}, "probe_failed", id="probe-failed"),
    ],
)
def test_only_a_confirmed_session_is_saved(working_copy, respond_with, response, expected):
    """`Set-Cookie` от отказа не должен затирать рабочие cookies."""
    copy_bytes = working_copy.read_bytes()
    respond_with(set_cookie=RENEWAL, **response)

    assert cookie_health._probe_tiktok_session(working_copy) == expected

    assert working_copy.read_bytes() == copy_bytes


def test_original_is_not_written_when_no_copy_could_be_made(original, respond_with, monkeypatch):
    """`working_cookie_file` отдаёт сам оригинал, если каталог для копии недоступен."""
    original_bytes = original.read_bytes()
    monkeypatch.setattr(cookie_health, "working_cookie_file", lambda path: path)
    respond_with(set_cookie=RENEWAL)

    result = cookie_health.check_cookie_health("tiktok", force=True)

    assert result.status == "valid"
    assert result.session_expires_at == OLD_EXPIRY
    assert original.read_bytes() == original_bytes


def test_failed_save_keeps_the_verdict(working_copy, respond_with, monkeypatch, caplog):
    def refuse(self, *args, **kwargs):
        raise OSError("диск заполнен")

    jar_class = type(cookie_health._load_cookie_jar("tiktok", working_copy))
    monkeypatch.setattr(jar_class, "save", refuse)
    copy_bytes = working_copy.read_bytes()
    respond_with(set_cookie=RENEWAL)

    assert cookie_health._probe_tiktok_session(working_copy) == "valid"

    assert "диск заполнен" in caplog.text
    assert working_copy.read_bytes() == copy_bytes


# --- личные данные из ответа -------------------------------------------------


@pytest.mark.parametrize(
    "response, logged",
    [
        pytest.param({"body": LIVE_BODY}, "", id="success"),
        pytest.param({"body": LIVE_BODY[:-5]}, "JSONDecodeError", id="cut-json"),
        pytest.param(
            {"read_failure": http.client.IncompleteRead(LIVE_BODY)},
            "IncompleteRead",
            id="broken-read",
        ),
        pytest.param({"status": 500, "body": LIVE_BODY}, "HTTP 500", id="http-error"),
    ],
)
def test_private_values_stay_out_of_logs_and_result(original, respond_with, caplog, response, logged):
    respond_with(**response)

    result = cookie_health.check_cookie_health("tiktok", force=True)

    assert logged in caplog.text
    exposed = caplog.text + repr(result)
    assert [value for value in PRIVATE_VALUES if value in exposed] == []
