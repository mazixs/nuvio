"""Cookie health checks for admin diagnostics."""

from __future__ import annotations

import http.client
import http.cookiejar
import json
import re
import time
import threading
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yt_dlp

from config import INSTAGRAM_COOKIES_PATH, TIKTOK_COOKIES_PATH, YOUTUBE_COOKIES_PATH
from utils.cookie_workfile import working_cookie_file
from utils.download_report import forget as forget_download_report
from utils.logger import setup_logger
from utils.ytdlp_common import output_capture_opts

logger = setup_logger(__name__)

COOKIE_HEALTH_CACHE_TTL_SECONDS = 600
HTTP_PROBE_TIMEOUT_SECONDS = 8
# То же правило, что у `MozillaCookieJar` при чтении. Его собственный `magic_re` —
# это заголовок формата LWP, а константа с этим правилом недокументирована.
NETSCAPE_HEADER_PATTERN = re.compile(
    r"#( Netscape)? HTTP Cookie File", re.IGNORECASE | re.ASCII
)
# Срок в том виде, какой принимает jar yt-dlp.
NETSCAPE_EXPIRES_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+)?")
# Сколько страницы Instagram читать в поисках доказательства входа: 01.11.2026 на
# странице размером 528 417 байт оно лежало на позиции 160 237, а без входа — на
# 163 065. Раньше окно было 96 КБ и сигнал в него не попадал.
PROBE_BODY_READ_BYTES = 256 * 1024
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/137.0.0.0 Safari/537.36"
)

COOKIE_PATHS = {
    "youtube": YOUTUBE_COOKIES_PATH,
    "instagram": INSTAGRAM_COOKIES_PATH,
    "tiktok": TIKTOK_COOKIES_PATH,
}
AUTH_COOKIE_NAMES = {
    "youtube": {"SID", "HSID", "SSID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID"},
    "instagram": {"sessionid", "csrftoken", "ds_user_id"},
    "tiktok": {"sessionid", "sessionid_ss", "sid_tt", "uid_tt"},
}
# Cookies, которые несут саму сессию: без живой из этого списка платформа запрос
# как авторизованный не примет, сколько бы остальных ни осталось. YouTube при
# ротации удаляет `LOGIN_INFO`, и без неё yt-dlp считает запрос анонимным. У TikTok
# вход подтверждается и по `sessionid`, и по `sid_tt` по отдельности, а с одной
# `uid_tt` приходит `session_expired` (замер 01.11.2026).
SESSION_COOKIE_NAMES = {
    "youtube": {"LOGIN_INFO"},
    "instagram": {"sessionid"},
    "tiktok": {"sessionid", "sid_tt"},
}
# Платформы, cookies которых проверяются попыткой извлечь видео. Для YouTube
# HTTP-проба непригодна: с датацентрового адреса `/feed/subscriptions` отдаёт
# стену согласия `consent.youtube.com` независимо от авторизации, и на самой
# этой странице присутствуют `accounts.google.com` и `ServiceLogin`. С полным
# рабочим набором cookies такая проба всё равно рапортовала «stale».
# Извлечение видео меряет ровно то, ради чего cookies и нужны.
YTDLP_PROBE_URLS = {
    # Первое видео в истории YouTube (2005) — вряд ли когда-нибудь исчезнет.
    "youtube": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
}
YTDLP_PROBE_TIMEOUT_SECONDS = 20
# Сообщения, которые означают именно проблему с авторизацией.
YTDLP_AUTH_FAILURE_MARKERS = (
    "sign in to confirm",
    "not a bot",
    "login required",
    "private video",
    "confirm your age",
)
YTDLP_RATE_LIMIT_MARKERS = ("429", "too many requests", "rate-limit", "rate limit")

PROBE_CONFIG = {
    "instagram": {
        "url": "https://www.instagram.com/accounts/edit/",
        "login_path": "/accounts/login",
        # Без входа значение равно "0", с входом — идентификатор аккаунта. Маркеры
        # вроде `Log in` бесполезны: 01.11.2026 в теле их не было ни на странице
        # входа, ни на странице с живой сессией, а `USER_ID` равен "0" на обеих.
        "user_id_pattern": re.compile(r'"NON_FACEBOOK_USER_ID":"?(\d+)"?'),
        "rate_limit_markers": ("Please wait a few minutes", "Try again later"),
    },
}

# Недокументированный эндпоинт, поэтому незнакомый ответ — это `probe_failed`, а не
# вердикт о сессии. Ответ содержит email, телефон и `session_key`: читаем только поля
# для вердикта и никуда его не пишем.
TIKTOK_ACCOUNT_INFO_URL = "https://www.tiktok.com/passport/web/account/info/"
TIKTOK_ACCOUNT_INFO_READ_BYTES = 64 * 1024
TIKTOK_SESSION_EXPIRED_NAME = "session_expired"
TIKTOK_SESSION_EXPIRED_CODE = 13

# Сетевые сбои HTTP-проб. `http.client.HTTPException` не наследует `OSError`, а
# обрыв посреди тела приходит именно им.
_HTTP_PROBE_ERRORS = (OSError, http.client.HTTPException)

_LOCAL_CHECK_PASSED_SUMMARY = "auth cookies are active"
_PROBE_SUMMARIES = {
    "valid": "auth cookies are active and probe succeeded",
    "stale": "auth cookies exist but authenticated probe failed",
    "rate_limited": "platform temporarily rate-limited the validation probe",
    "probe_failed": "auth cookies are active but live probe could not complete",
}
# Публичный ролик извлекается и без входа, поэтому успех здесь сессию не доказывает.
_YTDLP_PROBE_VALID_SUMMARY = "session cookies are present and the test video was extracted"


@dataclass(frozen=True)
class CookieHealthResult:
    platform: str
    status: str
    summary: str
    checked_at: float
    auth_cookie_count: int
    active_auth_cookie_count: int
    # Самый ранний срок среди живых сессионных cookies; None — срока нет или не определён.
    session_expires_at: int | None = None


@dataclass
class _CookieHealthCacheEntry:
    result: CookieHealthResult
    file_size: int | None
    file_mtime_ns: int | None


_COOKIE_HEALTH_CACHE: dict[str, _CookieHealthCacheEntry] = {}
_COOKIE_PROBE_LOCKS = {platform: threading.Lock() for platform in COOKIE_PATHS}


def _read_netscape_cookies(file_path: Path) -> list[dict[str, Any]]:
    """Читает cookie-файл и отказывает во всём, что jar yt-dlp не прочитает молча.

    Строку, которая ему не нравится, jar цитирует вместе со значением: при отказе
    читать файл — в тексте ошибки, а пропуская строку — в stderr. Поэтому такой файл
    не должен дойти до yt-dlp, а в наших сообщениях только номер строки: текст уходит
    в панель, `bot.log` и краш-репорт, а в строке может лежать живая сессия.
    """
    try:
        with file_path.open("r", encoding="utf-8") as file:
            lines = file.readlines()
    except UnicodeDecodeError:
        raise ValueError("Cookie file is not valid UTF-8") from None

    records: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(lines, 1):
        if line_number == 1 and not NETSCAPE_HEADER_PATTERN.match(raw_line):
            raise ValueError("Cookie file has no Netscape header")
        # Только перевод строки: jar пишет cookie с пустым значением с табом на
        # конце, и `strip()` оставил бы шесть полей вместо семи.
        line = raw_line.rstrip("\r\n")
        if not line.strip():
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        elif line.lstrip().startswith("#"):
            continue

        parts = line.split("\t")
        if len(parts) != 7 or (parts[1] == "TRUE") != parts[0].startswith("."):
            raise ValueError(f"Invalid Netscape cookie row at line {line_number}")

        expires_raw = parts[4]
        try:
            if expires_raw and not NETSCAPE_EXPIRES_PATTERN.fullmatch(expires_raw):
                raise ValueError
            # Пустой срок jar считает сессионным, как и 0.
            expires = int(float(expires_raw)) if expires_raw else 0
        except (ValueError, OverflowError):
            raise ValueError(
                f"Invalid cookie expiration value at line {line_number}"
            ) from None

        records.append(
            {
                "domain": parts[0],
                "path": parts[2],
                "expires": expires,
                "name": parts[5],
                "value": parts[6],
            }
        )
    return records


def _get_file_signature(file_path: Path) -> tuple[int | None, int | None]:
    if not file_path.exists():
        return None, None
    stat = file_path.stat()
    return stat.st_size, stat.st_mtime_ns


def _is_cache_valid(platform: str, file_path: Path, now: float) -> bool:
    entry = _COOKIE_HEALTH_CACHE.get(platform)
    if not entry:
        return False
    if now - entry.result.checked_at > COOKIE_HEALTH_CACHE_TTL_SECONDS:
        return False
    file_size, file_mtime_ns = _get_file_signature(file_path)
    return entry.file_size == file_size and entry.file_mtime_ns == file_mtime_ns


def _cache_result(
    platform: str, file_path: Path, result: CookieHealthResult
) -> CookieHealthResult:
    file_size, file_mtime_ns = _get_file_signature(file_path)
    _COOKIE_HEALTH_CACHE[platform] = _CookieHealthCacheEntry(
        result=result,
        file_size=file_size,
        file_mtime_ns=file_mtime_ns,
    )
    return result


def _extract_with_cookies(
    url: str, cookiefile: Path | str | None, session_id: str | None = None
) -> None:
    """Пробует получить информацию о видео. Поднимает исключение при неудаче."""
    # Предупреждения yt-dlp уходят в лог и в отчёт, но в вердикте не участвуют: о
    # потере cookies он говорит только в момент потери, а потом молчит, потому что
    # без `LOGIN_INFO` считает запрос анонимным с самого начала. Вердикт берётся из
    # исключения и из файла, который yt-dlp сохранил после запроса.
    options: dict[str, Any] = {
        "quiet": True,
        "skip_download": True,
        "socket_timeout": YTDLP_PROBE_TIMEOUT_SECONDS,
        **output_capture_opts(session_id),
    }
    if cookiefile:
        options["cookiefile"] = str(cookiefile)
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.extract_info(url, download=False)


def _is_live(record: dict[str, Any], now: float) -> bool:
    return bool(record["value"]) and (record["expires"] == 0 or record["expires"] > now)


def _has_live_session_cookie(platform: str, file_path: Path) -> bool:
    names = SESSION_COOKIE_NAMES[platform]
    now = time.time()
    return any(
        record["name"] in names and _is_live(record, now)
        for record in _read_netscape_cookies(file_path)
    )


def _probe_with_ytdlp(platform: str, file_path: Path) -> str:
    """Проверяет cookies тем же способом, которым ими пользуются загрузчики.

    Отдаётся рабочая копия: проверять надо именно тот файл, с которым работает
    скачивание, а оригинал админа не должен меняться от проверки.
    """
    session_id = f"cookie-probe-{platform}-{uuid.uuid4().hex[:8]}"
    try:
        _extract_with_cookies(YTDLP_PROBE_URLS[platform], file_path, session_id)
    except Exception as exc:  # noqa: BLE001
        reason = str(exc).lower()
        if any(marker in reason for marker in YTDLP_RATE_LIMIT_MARKERS):
            return "rate_limited"
        if any(marker in reason for marker in YTDLP_AUTH_FAILURE_MARKERS):
            return "stale"
        # Удалённое видео или сетевой сбой — не повод объявлять cookies мёртвыми:
        # ложная тревога обесценивает проверку так же, как ложное «всё хорошо».
        # Только тип: текст ошибки yt-dlp сам пишет в лог, а при сбое чтения файла
        # в нём цитируется строка cookie.
        logger.warning(
            "Cookie probe for %s failed for another reason: %s",
            platform,
            type(exc).__name__,
        )
        return "probe_failed"
    finally:
        forget_download_report(session_id)

    # Публичный ролик извлекается и без входа, а ответ самой пробы мог удалить
    # сессионную cookie: yt-dlp уже сохранил его в файл.
    try:
        return "valid" if _has_live_session_cookie(platform, file_path) else "stale"
    except (OSError, ValueError) as exc:
        logger.warning("Failed to re-read %s cookies after probe: %s", platform, exc)
        return "probe_failed"


def _load_cookie_jar(
    platform: str, file_path: Path
) -> http.cookiejar.FileCookieJar | None:
    """Загружает файл тем же jar, что и yt-dlp. Файл меняется только явным `save()`."""
    # Берётся при вызове: `cookie_workfile` подменяет класс на пишущий атомарно.
    jar_class = getattr(
        yt_dlp.cookies, "YoutubeDLCookieJar", http.cookiejar.MozillaCookieJar
    )
    cookie_jar = jar_class(str(file_path))
    try:
        cookie_jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, ValueError) as exc:
        # Только тип: `LoadError` цитирует строку файла вместе со значением cookie.
        logger.warning(
            "Failed to load cookie jar for %s: %s", platform, type(exc).__name__
        )
        return None
    return cookie_jar


def _build_probe_request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        },
    )


def _classify_tiktok_account_info(payload: object) -> str:
    """Вердикт по ответу TikTok: `valid` — только по прямому доказательству входа."""
    if not isinstance(payload, dict):
        return "probe_failed"
    data = payload.get("data")
    if not isinstance(data, dict):
        return "probe_failed"
    if payload.get("message") == "success" and data.get("user_id_str"):
        return "valid"
    if (
        data.get("name") == TIKTOK_SESSION_EXPIRED_NAME
        or data.get("error_code") == TIKTOK_SESSION_EXPIRED_CODE
    ):
        return "stale"
    return "probe_failed"


def _probe_tiktok_session(file_path: Path) -> str:
    """Спрашивает у TikTok, чья это сессия, и сохраняет продление в рабочую копию.

    Публичные видео скачиваются и без входа, поэтому извлечение ролика сессию не
    проверяет. Этот же ответ продлевает сессию: 01.11.2026 токен остался прежним, а
    срок сдвинулся с 12.02.2027 на 30.04.2027. Дату в файле кроме этого ответа никто
    не обновляет, и в указанный там день cookie перестала бы отправляться.
    """
    cookie_jar = _load_cookie_jar("tiktok", file_path)
    if cookie_jar is None:
        return "probe_failed"

    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    try:
        with opener.open(
            _build_probe_request(TIKTOK_ACCOUNT_INFO_URL),
            timeout=HTTP_PROBE_TIMEOUT_SECONDS,
        ) as response:
            payload = json.loads(response.read(TIKTOK_ACCOUNT_INFO_READ_BYTES))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            return "rate_limited"
        logger.warning("Cookie probe failed for tiktok: HTTP %s", exc.code)
        return "probe_failed"
    except (*_HTTP_PROBE_ERRORS, ValueError) as exc:
        # Только тип: в `ValueError` от разбора JSON может попасть кусок ответа.
        logger.warning("Cookie probe failed for tiktok: %s", type(exc).__name__)
        return "probe_failed"

    status = _classify_tiktok_account_info(payload)
    if status == "probe_failed":
        # Так выглядит смена формата эндпоинта: продление при этом останавливается.
        logger.warning("Cookie probe failed for tiktok: unfamiliar response")
    # Если копию создать не удалось, сюда приходит оригинал админа, а он не меняется.
    if status == "valid" and file_path != COOKIE_PATHS["tiktok"]:
        try:
            cookie_jar.save(ignore_discard=True, ignore_expires=True)
        except OSError as exc:
            logger.warning("Failed to save refreshed TikTok cookies: %s", exc)
    return status


def _classify_instagram_page(final_url: str, body: str) -> str:
    """Вердикт по странице настроек: `valid` — только по прямому доказательству входа.

    Раньше отсутствие маркера считалось успехом, и любая незнакомая страница, вроде
    проверки аккаунта без редиректа на логин, выглядела живой сессией.
    """
    config = PROBE_CONFIG["instagram"]
    if config["login_path"] in final_url.lower():
        return "stale"
    match = config["user_id_pattern"].search(body)
    if match and match.group(1) != "0":
        return "valid"
    body_lower = body.lower()
    if any(marker.lower() in body_lower for marker in config["rate_limit_markers"]):
        return "rate_limited"
    return "probe_failed"


def _probe_instagram_session(file_path: Path) -> str:
    config = PROBE_CONFIG["instagram"]
    cookie_jar = _load_cookie_jar("instagram", file_path)
    if cookie_jar is None:
        return "probe_failed"

    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    try:
        with opener.open(
            _build_probe_request(config["url"]), timeout=HTTP_PROBE_TIMEOUT_SECONDS
        ) as response:
            final_url = response.geturl()
            body = response.read(PROBE_BODY_READ_BYTES).decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            return "stale"
        if exc.code == 429:
            return "rate_limited"
        try:
            body = exc.read(PROBE_BODY_READ_BYTES).decode("utf-8", errors="ignore")
        except _HTTP_PROBE_ERRORS:
            return "probe_failed"
        if any(
            marker.lower() in body.lower() for marker in config["rate_limit_markers"]
        ):
            return "rate_limited"
        return "probe_failed"
    except _HTTP_PROBE_ERRORS as exc:
        logger.warning("Cookie probe failed for instagram: %s", exc)
        return "probe_failed"

    return _classify_instagram_page(final_url, body)


def _probe_authenticated_session(platform: str, file_path: Path) -> str:
    if platform in YTDLP_PROBE_URLS:
        return _probe_with_ytdlp(platform, file_path)
    if platform == "tiktok":
        return _probe_tiktok_session(file_path)
    return _probe_instagram_session(file_path)


def _result(
    platform: str,
    status: str,
    summary: str,
    auth_cookie_count: int = 0,
    active_auth_cookie_count: int = 0,
    session_expires_at: int | None = None,
) -> CookieHealthResult:
    return CookieHealthResult(
        platform=platform,
        status=status,
        summary=summary,
        checked_at=time.time(),
        auth_cookie_count=auth_cookie_count,
        active_auth_cookie_count=active_auth_cookie_count,
        session_expires_at=session_expires_at,
    )


def inspect_cookie_file(platform: str, file_path: Path) -> CookieHealthResult:
    """Проверяет файл без сети. Статус `valid` значит, что локально претензий нет.

    Статусы те же, что у полной проверки, поэтому годится и для загрузки нового
    файла до того, как он заменит рабочий.
    """
    try:
        records = _read_netscape_cookies(file_path)
    except ValueError as exc:
        return _result(platform, "invalid_format", str(exc))
    except OSError as exc:
        # Только тип: текст `OSError` содержит путь, а summary уходит в панель.
        logger.warning("Failed to read %s cookies: %s", platform, type(exc).__name__)
        return _result(platform, "invalid_format", "cookie file cannot be read")

    if not records:
        return _result(platform, "invalid_format", "no cookie records found")

    auth_names = AUTH_COOKIE_NAMES[platform]
    auth_records = [
        record for record in records if record["name"] in auth_names and record["value"]
    ]
    if not auth_records:
        return _result(
            platform, "invalid_format", "required auth cookies are missing"
        )

    now = time.time()
    active_auth_records = [record for record in auth_records if _is_live(record, now)]
    counts = {
        "auth_cookie_count": len(auth_records),
        "active_auth_cookie_count": len(active_auth_records),
    }
    if not active_auth_records:
        return _result(platform, "expired", "all auth cookies are expired", **counts)

    session_names = SESSION_COOKIE_NAMES[platform]
    session_records = [
        record for record in records if record["name"] in session_names and record["value"]
    ]
    live_session_records = [record for record in session_records if _is_live(record, now)]
    if session_records and not live_session_records:
        return _result(platform, "expired", "session cookies are expired", **counts)

    # Неполный набор — самый устойчивый признак того, что файл пора обновить.
    # Сетевая проба отвечает на другой вопрос («скачивается ли сейчас») и может
    # быть зелёной на анонимном доступе: замерено, что одно и то же видео за
    # день и отбивалось бот-чеком, и извлекалось вообще без cookies. Полнота же
    # деградирует необратимо — с шести из шести до одной. Истёкшие cookies запрос
    # не поддержат, поэтому считаются только живые.
    if not live_session_records or len(active_auth_records) * 2 < len(auth_names):
        return _result(
            platform,
            "degraded",
            "auth cookie set is incomplete, re-upload is needed",
            **counts,
        )

    dated_expiries = [
        record["expires"] for record in live_session_records if record["expires"] > 0
    ]
    return _result(
        platform,
        "valid",
        _LOCAL_CHECK_PASSED_SUMMARY,
        session_expires_at=min(dated_expiries, default=None),
        **counts,
    )


def check_cookie_health(platform: str, *, force: bool = False) -> CookieHealthResult:
    """Checks the health of a cookie file for the requested platform."""
    if platform not in COOKIE_PATHS:
        raise ValueError(f"Unsupported platform: {platform}")

    started_at = time.time()
    with _COOKIE_PROBE_LOCKS[platform]:
        entry = _COOKIE_HEALTH_CACHE.get(platform)
        if (
            entry is not None
            and entry.result.checked_at >= started_at
            and _is_cache_valid(platform, COOKIE_PATHS[platform], time.time())
        ):
            return entry.result
        return _check_cookie_health_unlocked(platform, force=force)


def _check_cookie_health_unlocked(platform: str, *, force: bool) -> CookieHealthResult:
    """Выполняет единственную пробу платформы под ее блокировкой."""

    file_path = COOKIE_PATHS[platform]
    now = time.time()
    if not force and _is_cache_valid(platform, file_path, now):
        return _COOKIE_HEALTH_CACHE[platform].result

    if not file_path.exists():
        return _cache_result(
            platform, file_path, _result(platform, "missing", "file not found")
        )

    # Читается рабочая копия: yt-dlp меняет только её, и потерю cookies в оригинале
    # не увидеть. 01.11.2026 в оригинале YouTube было всё, а в копии осталась одна
    # auth-cookie из шести и не было `LOGIN_INFO`. И наоборот, `ds_user_id` Instagram
    # в оригинале истекла, а в копии была продлена на 78 дней.
    working_path = working_cookie_file(file_path) or file_path
    local = inspect_cookie_file(platform, working_path)
    if local.status != "valid":
        return _cache_result(platform, file_path, local)

    status = _probe_authenticated_session(platform, working_path)
    summary = _PROBE_SUMMARIES[status]
    if status == "valid" and platform in YTDLP_PROBE_URLS:
        summary = _YTDLP_PROBE_VALID_SUMMARY
    # Проба могла продлить сессию, поэтому срок читается после неё.
    session_expires_at = inspect_cookie_file(platform, working_path).session_expires_at
    result = replace(
        local,
        status=status,
        summary=summary,
        checked_at=time.time(),
        session_expires_at=session_expires_at,
    )
    return _cache_result(platform, file_path, result)


def check_all_cookie_health(*, force: bool = False) -> dict[str, CookieHealthResult]:
    """Checks cookie health for all supported platforms."""
    return {
        platform: check_cookie_health(platform, force=force)
        for platform in COOKIE_PATHS
    }
