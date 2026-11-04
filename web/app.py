"""
WebUI дашборд аналитики бота.
"""

import hmac
import logging
import os
import hashlib
import secrets
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from web.i18n import day_unit, translate

import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.analytics_db import (  # noqa: E402
    CSI_INTERVAL_DAYS_MAX,
    CSI_INTERVAL_DAYS_MIN,
    close_connection,
    init_db,
    dashboard_summary,
    get_all_users,
    get_csi_interval_days,
    get_csi_metrics,
    get_user_detail,
    get_users_for_csi,
    set_csi_interval_days,
    get_feedback,
    set_feedback_status,
    set_user_blocked,
)

from utils.db_worker import run_db  # noqa: E402
from utils.cache_policy import get_cache_policy, set_cache_policy  # noqa: E402
from utils.api_keys import (  # noqa: E402
    DEFAULT_EXPIRY_DAYS,
    EXPIRY_PRESET_DAYS,
    NAME_MAX_LENGTH,
    ROTATION_GRACE_HOURS,
    create_api_key,
    delete_api_key,
    list_api_keys,
    parse_expiry,
    revoke_api_key,
    rotate_api_key,
    set_api_key_expiry,
)

logger = logging.getLogger("nuvio.web")

WEB_DIR = Path(__file__).resolve().parent

# ── Безопасность ──────────────────────────────────────────────

MAX_INPUT_LENGTH = 128  # макс. длина логина/пароля


def _parse_duration(value: str) -> int:
    """Парсит строку вида '15m', '1h', '300s', '300' → секунды."""
    value = value.strip().lower()
    if value.endswith("m"):
        return int(value[:-1]) * 60
    if value.endswith("h"):
        return int(value[:-1]) * 3600
    if value.endswith("s"):
        return int(value[:-1])
    return int(value)


# `or`, а не умолчание get(): env_file передаёт и пустые строки.
LOGIN_RATE_LIMIT = int(os.environ.get("FAIL2BAN_RETRIES") or "5")
LOGIN_LOCKOUT = _parse_duration(os.environ.get("FAIL2BAN_TIME") or "10m")
SESSION_KEY_MIN_LENGTH = 32


def _session_secret_key() -> str:
    """Ключ подписи сессий: пустой или короткий заменяется случайным.

    С ключом-заглушкой из документации сессию администратора мог бы подделать
    кто угодно. Случайный ключ лишь просит войти заново после перезапуска.
    """
    key = os.environ.get("WEB_SECRET_KEY", "")
    if len(key) >= SESSION_KEY_MIN_LENGTH:
        return key
    if key:
        logger.warning(
            "WEB_SECRET_KEY короче %s символов и не используется: ключ будет случайным",
            SESSION_KEY_MIN_LENGTH,
        )
    return secrets.token_hex(32)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
ADMIN_IDS = [
    uid.strip() for uid in os.environ.get("ADMIN_IDS", "").split(",") if uid.strip()
]

# Хранилище неудачных попыток: {ip: [(timestamp, username), ...]}
_login_attempts: dict[str, list[tuple[float, str]]] = defaultdict(list)
# Множество IP, по которым уже отправлено уведомление (чтобы не спамить)
_notified_ips: set[str] = set()
# Лимит отслеживаемых IP (защита от исчерпания памяти при DDoS)
_MAX_TRACKED_IPS = 10_000


def _cleanup_old_ips() -> None:
    """Удаляет записи с истёкшими попытками, ограничивает общий размер."""
    now = time.time()
    expired = [
        ip
        for ip, attempts in _login_attempts.items()
        if all(now - t >= LOGIN_LOCKOUT for t, _ in attempts)
    ]
    for ip in expired:
        del _login_attempts[ip]
        _notified_ips.discard(ip)
    # Если всё ещё слишком много — удаляем самые старые
    if len(_login_attempts) > _MAX_TRACKED_IPS:
        by_oldest = sorted(_login_attempts, key=lambda ip: _login_attempts[ip][0][0])
        for ip in by_oldest[: len(_login_attempts) - _MAX_TRACKED_IPS]:
            del _login_attempts[ip]
            _notified_ips.discard(ip)


def _check_rate_limit(ip: str) -> bool:
    """Возвращает True если IP заблокирован из-за превышения лимита."""
    now = time.time()
    _login_attempts[ip] = [
        (t, u) for t, u in _login_attempts[ip] if now - t < LOGIN_LOCKOUT
    ]
    if not _login_attempts[ip]:
        del _login_attempts[ip]
        _notified_ips.discard(ip)
        return False
    # Периодическая чистка
    if len(_login_attempts) > _MAX_TRACKED_IPS:
        _cleanup_old_ips()
    return len(_login_attempts[ip]) >= LOGIN_RATE_LIMIT


def _record_failed_attempt(ip: str, username: str) -> None:
    _login_attempts[ip].append((time.time(), username))


def _clear_attempts(ip: str) -> None:
    _login_attempts.pop(ip, None)
    _notified_ips.discard(ip)


async def _notify_admins_brute_force(ip: str) -> None:
    """Отправляет уведомление админам в Telegram при срабатывании fail2ban."""
    if ip in _notified_ips:
        return
    if not TELEGRAM_TOKEN or not ADMIN_IDS:
        logger.warning(
            "Fail2ban сработал для %s, но TELEGRAM_TOKEN/ADMIN_IDS не заданы", ip
        )
        return

    _notified_ips.add(ip)

    attempts = _login_attempts.get(ip, [])
    logins_used = list(
        dict.fromkeys(u for _, u in attempts)
    )  # уникальные, сохраняя порядок
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lockout_min = LOGIN_LOCKOUT // 60

    text = (
        f"🚨 <b>Fail2ban: IP заблокирован</b>\n\n"
        f"<b>IP:</b> <code>{ip}</code>\n"
        f"<b>Время:</b> {now}\n"
        f"<b>Попыток:</b> {len(attempts)}\n"
        f"<b>Логины:</b> <code>{'</code>, <code>'.join(logins_used)}</code>\n"
        f"<b>Блокировка:</b> {lockout_min} мин.\n\n"
        f"⚠️ Возможен brute-force на WebUI дашборд."
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as client:
        for admin_id in ADMIN_IDS:
            try:
                await client.post(
                    url,
                    json={
                        "chat_id": admin_id,
                        "text": text,
                        "parse_mode": "HTML",
                    },
                )
            except Exception:
                logger.error(
                    "Не удалось отправить fail2ban уведомление админу %s", admin_id
                )


def _sanitize_input(value: str) -> str:
    """Обрезает и ограничивает длину ввода."""
    return value.strip()[:MAX_INPUT_LENGTH]


# ── Приложение ────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await run_db(init_db)
        yield
    finally:
        close_connection()


# Схема OpenAPI отключена вместе со Swagger и ReDoc: иначе `/openapi.json` без
# входа перечисляет все маршруты админки.
app = FastAPI(
    title="Nuvio Analytics",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
app.add_middleware(SessionMiddleware, secret_key=_session_secret_key())
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


def _language(request: Request) -> str:
    return "ru" if request.session.get("language") == "ru" else "en"


def _render(request: Request, template: str, context: dict):
    language = _language(request)
    csrf_token = request.session.setdefault("csrf_token", secrets.token_urlsafe(32))
    return templates.TemplateResponse(
        request,
        template,
        {
            **context,
            "language": language,
            "csrf_token": csrf_token,
            "t": lambda message, **values: translate(language, message, **values),
        },
    )


WEB_USERNAME = os.environ.get("WEB_USERNAME", "admin")
SALT = secrets.token_bytes(16)


def _web_password_hash() -> bytes:
    """Хэш пароля входа; без пароля вход закрыт: форма отвергает пустое поле.

    Умолчания нет: пароль из шаблона знает любой читатель репозитория, а за входом
    лежат ключи HTTP API.
    """
    password = os.environ.get("WEB_PASSWORD", "")
    if not password:
        logger.warning("WEB_PASSWORD не задан: вход в WebUI закрыт")
    return hashlib.pbkdf2_hmac("sha256", password.encode(), SALT, 100000)


WEB_PASSWORD_HASH = _web_password_hash()


def _check_auth(request: Request) -> bool:
    return request.session.get("authenticated") is True


def require_auth(request: Request):
    if not _check_auth(request):
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return True


@app.exception_handler(HTTPException)
async def redirect_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 303 and "Location" in (exc.headers or {}):
        return RedirectResponse(exc.headers["Location"], status_code=303)
    return HTMLResponse(content=str(exc.detail), status_code=exc.status_code)


# ── Healthcheck ─────────────────────────────────────────────────


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Auth ────────────────────────────────────────────────────────


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _check_auth(request):
        return RedirectResponse("/", status_code=303)
    return _render(request, "login.html", {"error": None})


@app.get("/language/{language}")
async def set_language(request: Request, language: str, next: str = "/"):
    if language not in {"en", "ru"}:
        raise HTTPException(status_code=404, detail="Language not found")
    request.session["language"] = language
    destination = (
        next
        if (
            next in {"/", "/login", "/users", "/settings", "/feedback", "/api-keys"}
            or (next.startswith("/users/") and next.removeprefix("/users/").isdigit())
        )
        else "/"
    )
    return RedirectResponse(destination, status_code=303)


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request, username: str = Form(...), password: str = Form(...)
):
    client_ip = request.client.host if request.client else "unknown"

    # Санитизация ввода
    username = _sanitize_input(username)
    password = _sanitize_input(password)

    # Rate limiting
    if _check_rate_limit(client_ip):
        await _notify_admins_brute_force(client_ip)
        lockout_min = LOGIN_LOCKOUT // 60
        return _render(
            request,
            "login.html",
            {
                "error": translate(
                    _language(request),
                    "Too many attempts. Try again in {minutes} min.",
                    minutes=lockout_min,
                )
            },
        )

    if not username or not password:
        return _render(
            request,
            "login.html",
            {"error": translate(_language(request), "Fill in all fields")},
        )

    # Timing-safe сравнение с использованием PBKDF2 (защита от timing attack и brute-force)
    password_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), SALT, 100000)
    username_ok = hmac.compare_digest(username, WEB_USERNAME)
    password_ok = hmac.compare_digest(password_hash, WEB_PASSWORD_HASH)

    if username_ok and password_ok:
        _clear_attempts(client_ip)
        request.session["authenticated"] = True
        return RedirectResponse("/", status_code=303)

    _record_failed_attempt(client_ip, username)

    # Проверяем, не превышен ли лимит после этой попытки
    if _check_rate_limit(client_ip):
        await _notify_admins_brute_force(client_ip)

    return _render(
        request,
        "login.html",
        {"error": translate(_language(request), "Invalid username or password")},
    )


@app.get("/logout")
async def logout(request: Request):
    language = _language(request)
    request.session.clear()
    if language == "ru":
        request.session["language"] = language
    return RedirectResponse("/login", status_code=303)


# ── Dashboard ───────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, _=Depends(require_auth)):
    data = await run_db(dashboard_summary)
    return _render(request, "dashboard.html", data)


@app.get("/users", response_class=HTMLResponse)
async def users_list(request: Request, page: int = 1, _=Depends(require_auth)):
    page = max(1, page)
    per_page = 50
    offset = (page - 1) * per_page
    users = await run_db(get_all_users, limit=per_page, offset=offset)
    return _render(
        request,
        "users.html",
        {
            "users": users,
            "page": page,
            "has_next": len(users) == per_page,
        },
    )


@app.get("/users/{user_id}", response_class=HTMLResponse)
async def user_detail(request: Request, user_id: int, _=Depends(require_auth)):
    user = await run_db(get_user_detail, user_id)
    if not user:
        return HTMLResponse(
            translate(_language(request), "User not found"), status_code=404
        )
    feedback = await run_db(get_feedback, limit=10, status="all", user_id=user_id)
    return _render(request, "user_detail.html", {
        "user": user, "feedback": feedback, "is_protected": str(user_id) in ADMIN_IDS,
        "saved": request.query_params.get("saved") == "1",
    })


def _require_csrf(request: Request, token: str) -> None:
    """Проверяет подпись формы перед административным изменением."""
    expected = request.session.get("csrf_token")
    if not expected or not hmac.compare_digest(expected.encode("utf-8"), token.encode("utf-8")):
        raise HTTPException(403, detail=translate(_language(request), "Form expired. Refresh the page and try again."))


@app.post("/users/{user_id}/access")
async def user_access_submit(
    request: Request, user_id: int, action: str = Form(...), reason: str = Form(""),
    csrf_token: str = Form(""), _=Depends(require_auth),
):
    _require_csrf(request, csrf_token)
    if action not in {"block", "unblock"}:
        raise HTTPException(422, detail=translate(_language(request), "Invalid access action"))
    try:
        await run_db(set_user_blocked, user_id, action == "block", reason)
    except ValueError as error:
        raise HTTPException(422, detail=translate(_language(request), str(error))) from error
    except LookupError as error:
        raise HTTPException(404, detail=translate(_language(request), "User not found")) from error
    return RedirectResponse(f"/users/{user_id}?saved=1", status_code=303)


@app.get("/feedback", response_class=HTMLResponse)
async def feedback_list(request: Request, status: str = "open", page: int = 1, _=Depends(require_auth)):
    if status not in {"open", "closed", "all"}:
        raise HTTPException(422, detail=translate(_language(request), "Invalid feedback status"))
    page = max(page, 1)
    entries = await run_db(get_feedback, limit=50, offset=(page - 1) * 50, status=status)
    return _render(request, "feedback.html", {
        "feedback": entries, "status": status, "page": page, "has_next": len(entries) == 50,
    })


@app.post("/feedback/{feedback_id}/status")
async def feedback_status_submit(
    request: Request, feedback_id: int, status: str = Form(...),
    csrf_token: str = Form(""), _=Depends(require_auth),
):
    _require_csrf(request, csrf_token)
    try:
        await run_db(set_feedback_status, feedback_id, status)
    except ValueError as error:
        raise HTTPException(422, detail=translate(_language(request), "Invalid feedback status")) from error
    except LookupError as error:
        raise HTTPException(404, detail=translate(_language(request), "Feedback not found")) from error
    return RedirectResponse("/feedback", status_code=303)


# ── Настройки ───────────────────────────────────────────────────

# Готовые варианты частоты опроса. Ручной ввод тоже разрешён — пресеты нужны,
# чтобы не приходилось считать дни в неделях.
CSI_INTERVAL_PRESETS = [
    (7, "week"),
    (14, "2 weeks"),
    (30, "month"),
    (90, "quarter"),
]

# Горизонт дорожки отправок. 90 дней — тот масштаб, на котором разница между
# «раз в неделю» и «раз в месяц» видна глазами: 13 отправок против 3.
CSI_TRACK_HORIZON_DAYS = 90

# Границы зон. Недельный опрос — та частота, из-за которой бота блокируют;
# реже раза в полтора месяца обратная связь перестаёт успевать за релизами.
CSI_ZONE_DENSE_MAX = 7
CSI_ZONE_BALANCED_MAX = 45


def _csi_zone(days: int, language: str = "en") -> tuple[str, str]:
    """Зона частоты: машинное имя для стиля и слово для оператора."""
    if days <= CSI_ZONE_DENSE_MAX:
        return "dense", translate(language, "frequent")
    if days <= CSI_ZONE_BALANCED_MAX:
        return "balanced", translate(language, "balanced")
    return "sparse", translate(language, "infrequent")


def _csi_cadence_phrase(days: int, language: str = "en") -> str:
    """Интервал словами. Считается на сервере, чтобы формулировка была одна.

    Страница пересчитывает подпись через `/api/csi/preview`, а не дублирует
    правило в JavaScript.
    """
    named = {
        1: "every day",
        7: "once a week",
        14: "every two weeks",
        21: "every three weeks",
        30: "once a month",
        60: "every two months",
        90: "once a quarter",
        180: "every six months",
        365: "once a year",
    }
    return translate(
        language, named[days] if days in named else "every {days} days", days=days
    )


def _csi_dispatch_offsets(days: int) -> list[int]:
    """Дни внутри горизонта, в которые опрос уйдёт одному человеку."""
    if days < 1:
        return [0]
    return list(range(0, CSI_TRACK_HORIZON_DAYS + 1, days))


def _csi_queue_size(days: int) -> int | None:
    """Сколько человек получит опрос при следующей рассылке.

    Тот же запрос, которым пользуется рассылка, поэтому число точное, а не
    оценочное. При сбое БД возвращаем None — страница покажет прочерк вместо
    выдуманной цифры.
    """
    try:
        return len(get_users_for_csi(days_since_last=days, min_active_days=1))
    except Exception:
        logger.exception("Не удалось посчитать очередь CSI для интервала %s", days)
        return None


def _queue_hint(language: str, count: int | None) -> str:
    if count is None:
        return translate(language, "Could not read the database")
    message = (
        "person will receive a survey at the next check"
        if count == 1
        else "people will receive a survey at the next check"
    )
    return translate(language, message)


def _settings_context(
    *, language: str = "en", error: str | None = None, saved: bool = False
) -> dict:
    days = get_csi_interval_days()
    zone, zone_label = _csi_zone(days, language)
    try:
        csi = get_csi_metrics()
    except Exception:
        logger.exception("Не удалось получить метрики CSI")
        csi = {"total_responses": 0, "avg_rating": 0.0}
    queue_size = _csi_queue_size(days)
    return {
        "csi_interval_days": days,
        "csi_interval_unit": day_unit(language, days),
        "csi_interval_min": CSI_INTERVAL_DAYS_MIN,
        "csi_interval_max": CSI_INTERVAL_DAYS_MAX,
        "csi_interval_presets": [
            (value, translate(language, label)) for value, label in CSI_INTERVAL_PRESETS
        ],
        "csi_zone": zone,
        "csi_zone_label": zone_label,
        "csi_cadence": _csi_cadence_phrase(days, language),
        "csi_track_horizon": CSI_TRACK_HORIZON_DAYS,
        "csi_dispatch_offsets": _csi_dispatch_offsets(days),
        "csi_queue_size": queue_size,
        "csi_queue_hint": _queue_hint(language, queue_size),
        "csi_per_year": round(365 / days) if days else 0,
        "csi_total_responses": csi.get("total_responses", 0),
        "csi_avg_rating": csi.get("avg_rating", 0.0),
        "error": error,
        "saved": saved,
        **_cache_settings_context(),
    }


def _cache_settings_context() -> dict:
    """Сводка включает только размер локальных указателей, а не файлов Telegram."""
    from utils import analytics_db
    from utils.artifact_cache import ArtifactCache

    policy = get_cache_policy()
    cache_path = analytics_db._DB_PATH.parent / "telegram_cache.db"
    stats = {"artifacts": 0, "manifests": 0, "artifact_aliases": 0, "bytes": 0}
    available = True
    if cache_path.exists():
        try:
            stats = ArtifactCache(cache_path).stats()
        except Exception:
            logger.exception("Статистика кеша недоступна")
            available = False
    return {"cache_policy": policy, "cache_stats": stats, "cache_available": available,
            "cache_saved": False, "cache_error": None}


@app.post("/settings/cache", response_class=HTMLResponse)
async def settings_cache(request: Request, cache_retention_days: str | None = Form(None),
                         cache_auto_cleanup_enabled: str = Form(""),
                         csrf_token: str = Form(""), _=Depends(require_auth)):
    _require_csrf(request, csrf_token)
    error = None
    status = 422
    try:
        days = int(cache_retention_days) if cache_retention_days is not None else (await run_db(get_cache_policy))["days"]
        await run_db(set_cache_policy, cache_auto_cleanup_enabled == "true", days)
    except ValueError:
        error = translate(_language(request), "Retention must be between 1 and 36500 days.")
    except Exception:
        logger.exception("Не удалось сохранить политику кеша")
        error = translate(_language(request), "Cache settings could not be saved. Your changes were not applied.")
        status = 503
    if error:
        data = await run_db(_settings_context, language=_language(request))
        data["cache_error"] = error
        data["cache_policy"] = {**data["cache_policy"], "enabled": cache_auto_cleanup_enabled == "true", "days": cache_retention_days}
        response = _render(request, "settings.html", data)
        response.status_code = status
        return response
    return RedirectResponse("/settings?cache_saved=1", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, _=Depends(require_auth)):
    saved = request.query_params.get("saved") == "1"
    data = await run_db(_settings_context, language=_language(request), saved=saved)
    data["cache_saved"] = request.query_params.get("cache_saved") == "1"
    return _render(request, "settings.html", data)


@app.get("/api/csi/preview")
async def api_csi_preview(request: Request, days: int, _=Depends(require_auth)):
    """Пересчёт последствий интервала до сохранения.

    Нужен, чтобы оператор видел размер очереди на том значении, которое он
    только примеряет ползунком, а не на уже сохранённом.
    """
    if not CSI_INTERVAL_DAYS_MIN <= days <= CSI_INTERVAL_DAYS_MAX:
        raise HTTPException(
            status_code=422,
            detail=translate(_language(request), "Interval is out of range"),
        )
    zone, zone_label = _csi_zone(days, _language(request))
    queue_size = await run_db(_csi_queue_size, days)
    return {
        "days": days,
        "unit": day_unit(_language(request), days),
        "zone": zone,
        "zone_label": zone_label,
        "cadence": _csi_cadence_phrase(days, _language(request)),
        "queue_size": queue_size,
        "queue_hint": _queue_hint(_language(request), queue_size),
        "per_year": round(365 / days),
        "offsets": _csi_dispatch_offsets(days),
    }


@app.post("/settings", response_class=HTMLResponse)
async def settings_submit(
    request: Request,
    csi_interval_days: str = Form(...),
    _=Depends(require_auth),
):
    """Меняет частоту CSI-опросов.

    Защита от CSRF — cookie сессии со `SameSite=lax`, которую браузер не
    отправляет при межсайтовом POST.
    """
    try:
        await run_db(
            set_csi_interval_days, int(_sanitize_input(csi_interval_days))
        )
    except ValueError:
        message = translate(
            _language(request),
            "Interval must be between {min_days} and {max_days} days",
            min_days=CSI_INTERVAL_DAYS_MIN,
            max_days=CSI_INTERVAL_DAYS_MAX,
        )
        return _render(
            request,
            "settings.html",
            await run_db(
                _settings_context, language=_language(request), error=message
            ),
        )
    return RedirectResponse("/settings?saved=1", status_code=303)


# ── API-ключи ───────────────────────────────────────────────────

# Открытый ключ ждёт первого показа в памяти процесса не дольше пяти минут.
# Cookie сессии подписана, но не зашифрована, а URL оседает в истории и
# журналах, поэтому в сессии лежит только одноразовый указатель.
_REVEAL_TTL_SECONDS = 300
_revealed_keys: dict[str, tuple[float, int, str]] = {}

_API_KEY_NOTICES = {
    "created": "API key created. Copy it now: it will not be shown again.",
    "rotated": "New key issued. Copy it now: it will not be shown again.",
    "expiry": "Expiry updated. It applies to the next request.",
    "revoked": "API key revoked. Requests with it are rejected immediately.",
    "deleted": "API key deleted.",
}


def _stash_revealed_key(request: Request, key_id: int, key: str) -> None:
    now = time.time()
    for token, (expires, _, _) in list(_revealed_keys.items()):
        if expires <= now:
            del _revealed_keys[token]
    token = secrets.token_urlsafe(16)
    _revealed_keys[token] = (now + _REVEAL_TTL_SECONDS, key_id, key)
    request.session["api_key_reveal"] = token


def _pop_revealed_key(request: Request) -> dict | None:
    token = request.session.pop("api_key_reveal", None)
    entry = _revealed_keys.pop(token, None) if token else None
    if entry is None or entry[0] <= time.time():
        return None
    return {"id": entry[1], "key": entry[2]}


def _api_port() -> int:
    """Порт API для примеров на странице ключей.

    WebUI не импортирует config, поэтому проверка повторена здесь: мусор и
    выход за 1–65535 дают 8090, как `config.API_PORT` у самого API.
    """
    raw = os.environ.get("API_PORT", "").strip()
    if raw.isascii() and raw.isdigit() and 1 <= int(raw) <= 65535:
        return int(raw)
    return 8090


async def _api_keys_page(
    request: Request, *, error: str | None = None, status: int = 200
) -> HTMLResponse:
    language = _language(request)
    notice = _API_KEY_NOTICES.get(request.query_params.get("saved", ""))
    response = _render(request, "api_keys.html", {
        "keys": await run_db(list_api_keys),
        "revealed": None if error else _pop_revealed_key(request),
        "notice": None if error else (translate(language, notice) if notice else None),
        "error": error,
        "expiry_presets": EXPIRY_PRESET_DAYS,
        "default_expiry": DEFAULT_EXPIRY_DAYS,
        "grace_hours": ROTATION_GRACE_HOURS,
        "name_max_length": NAME_MAX_LENGTH,
        "api_port": _api_port(),
        "today": datetime.now(timezone.utc).date().isoformat(),
    })
    response.status_code = status
    # Страница может нести открытый ключ: ни браузер, ни прокси не должны её хранить.
    response.headers["Cache-Control"] = "no-store"
    return response


class _ApiKeyFormError(Exception):
    """Отказ в изменении ключа, который показывается на странице ключей."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.message = message
        self.status = status


@app.exception_handler(_ApiKeyFormError)
async def api_key_form_error_handler(request: Request, exc: _ApiKeyFormError):
    return await _api_keys_page(
        request, error=translate(_language(request), exc.message), status=exc.status
    )


def _form_expiry(expiry: str, expiry_date: str):
    try:
        return parse_expiry(expiry, expiry_date)
    except ValueError as error:
        raise _ApiKeyFormError(str(error)) from error


async def _change_api_key(action, *args):
    try:
        return await run_db(action, *args)
    except LookupError as error:
        raise _ApiKeyFormError("API key not found", 404) from error
    except ValueError as error:
        raise _ApiKeyFormError(str(error)) from error


def _api_keys_redirect(notice: str) -> RedirectResponse:
    return RedirectResponse(f"/api-keys?saved={notice}", status_code=303)


@app.get("/api-keys", response_class=HTMLResponse)
async def api_keys_page(request: Request, _=Depends(require_auth)):
    return await _api_keys_page(request)


@app.post("/api-keys", response_class=HTMLResponse)
async def api_key_create(
    request: Request, name: str = Form(""), expiry: str = Form(str(DEFAULT_EXPIRY_DAYS)),
    expiry_date: str = Form(""), csrf_token: str = Form(""), _=Depends(require_auth),
):
    _require_csrf(request, csrf_token)
    key_id, key = await _change_api_key(create_api_key, name, _form_expiry(expiry, expiry_date))
    _stash_revealed_key(request, key_id, key)
    return _api_keys_redirect("created")


@app.post("/api-keys/{key_id}/rotate", response_class=HTMLResponse)
async def api_key_rotate(
    request: Request, key_id: int, grace_hours: str = Form("0"),
    csrf_token: str = Form(""), _=Depends(require_auth),
):
    _require_csrf(request, csrf_token)
    # Сравнение со строками, а не isdigit(): тот пропускает «²», и int() падает.
    if grace_hours not in {str(hours) for hours in ROTATION_GRACE_HOURS}:
        raise _ApiKeyFormError("Unknown grace period")
    key = await _change_api_key(rotate_api_key, key_id, int(grace_hours))
    _stash_revealed_key(request, key_id, key)
    return _api_keys_redirect("rotated")


@app.post("/api-keys/{key_id}/expiry", response_class=HTMLResponse)
async def api_key_expiry(
    request: Request, key_id: int, expiry: str = Form(""), expiry_date: str = Form(""),
    csrf_token: str = Form(""), _=Depends(require_auth),
):
    _require_csrf(request, csrf_token)
    await _change_api_key(set_api_key_expiry, key_id, _form_expiry(expiry, expiry_date))
    return _api_keys_redirect("expiry")


@app.post("/api-keys/{key_id}/revoke", response_class=HTMLResponse)
async def api_key_revoke(
    request: Request, key_id: int, csrf_token: str = Form(""), _=Depends(require_auth),
):
    _require_csrf(request, csrf_token)
    await _change_api_key(revoke_api_key, key_id)
    return _api_keys_redirect("revoked")


@app.post("/api-keys/{key_id}/delete", response_class=HTMLResponse)
async def api_key_delete(
    request: Request, key_id: int, csrf_token: str = Form(""), _=Depends(require_auth),
):
    _require_csrf(request, csrf_token)
    await _change_api_key(delete_api_key, key_id)
    return _api_keys_redirect("deleted")


# ── API (JSON) ──────────────────────────────────────────────────


@app.get("/api/summary")
async def api_summary(request: Request, _=Depends(require_auth)):
    return await run_db(dashboard_summary)


def run():
    import uvicorn

    port = int(os.environ.get("WEB_PORT") or "8080")
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    run()
