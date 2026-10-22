"""`jobpilot web`: the site. One small aiohttp process next to the workers, behind a password."""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import jinja2
from aiohttp import web

from ..config import Config
from ..core import interviews, stages
from ..core.db import Database
from ..hh.accounts import load_accounts
from ..hh.limits import daily_limit
from ..hh.pipeline import captcha_hold
from . import charts, queries

log = logging.getLogger(__name__)

COOKIE = "jp_session"
MAX_FAILURES = 5          # wrong passwords from one address before it has to wait
LOCKOUT_SECONDS = 15 * 60
OPEN_PATHS = ("/login", "/static/")


@dataclass
class Site:
    db: Database
    password: str
    secret: bytes
    tz: str
    accounts_path: Path
    session_hours: int = 24
    failures: dict[str, list[float]] = field(default_factory=dict)

    # --- session cookie: "<expiry>.<hmac>" ---

    def make_token(self) -> str:
        expires = str(int(time.time()) + self.session_hours * 3600)
        return f"{expires}.{self._sign(expires)}"

    def valid_token(self, token: str | None) -> bool:
        if not token or "." not in token:
            return False
        expires, sig = token.split(".", 1)
        return hmac.compare_digest(sig, self._sign(expires)) and expires.isdigit() and int(expires) > time.time()

    def _sign(self, value: str) -> str:
        return hmac.new(self.secret, value.encode(), hashlib.sha256).hexdigest()

    # --- login throttling ---

    def locked(self, ip: str) -> bool:
        now = time.time()
        recent = [t for t in self.failures.get(ip, []) if now - t < LOCKOUT_SECONDS]
        self.failures[ip] = recent
        return len(recent) >= MAX_FAILURES

    def configured_accounts(self) -> dict:
        return load_accounts(self.accounts_path) if self.accounts_path.exists() else {}


SITE = web.AppKey("site", Site)
TEMPLATES = web.AppKey("templates", jinja2.Environment)


def _secret(data_dir: Path) -> bytes:
    """Kept in the data folder so sessions survive a restart; never in the repository."""
    path = data_dir / "web_secret"
    if not path.exists():
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600)
    return path.read_text().strip().encode()


# --- templates ---

def _templates(tz: str) -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.PackageLoader("jobpilot.web", "templates"),
        autoescape=jinja2.select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    zone = ZoneInfo(tz)

    def local(value: str | None, fmt: str = "%d.%m %H:%M") -> str:
        if not value:
            return ""
        try:
            return datetime.fromisoformat(value).astimezone(zone).strftime(fmt)
        except ValueError:
            return value

    env.filters["local"] = local
    env.globals["bar_path"] = charts.bar_path
    env.globals.update(
        LABELS=stages.LABELS,
        STAGE_ORDER=queries.STAGE_ORDER,
        NOT_APPLIED=queries.NOT_APPLIED,
        label=lambda s: stages.LABELS.get(s, s) if s else "не отправлен",
    )
    return env


def _render(request: web.Request, template: str, **context) -> web.Response:
    site: Site = request.app[SITE]
    context.setdefault("accounts", queries.accounts(site.db.conn, list(site.configured_accounts())))
    context.setdefault("path", request.path)
    context.setdefault("last_event", queries.last_event_id(site.db.conn))
    context.setdefault("unread", queries.unread_total(site.db.conn))
    html = request.app[TEMPLATES].get_template(template).render(**context)
    return web.Response(text=html, content_type="text/html")


# --- auth ---

@web.middleware
async def auth(request: web.Request, handler):
    if request.path.startswith(OPEN_PATHS):
        return await handler(request)
    site: Site = request.app[SITE]
    if site.valid_token(request.cookies.get(COOKIE)):
        response = await handler(request)
        response.headers.setdefault("Cache-Control", "no-store")
        return response
    if request.path.startswith("/api/"):
        return web.json_response({"error": "login"}, status=401)
    raise web.HTTPFound("/login?" + urlencode({"next": request.path_qs}))


@web.middleware
async def headers(request: web.Request, handler):
    response = await handler(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


def _safe_next(value: str | None) -> str:
    return value if value and value.startswith("/") and not value.startswith("//") else "/"


async def login_page(request: web.Request) -> web.Response:
    return _render(request, "login.html", error=None, next=_safe_next(request.query.get("next")),
                   accounts=[], last_event=0)


async def login(request: web.Request) -> web.Response:
    site: Site = request.app[SITE]
    form = await request.post()
    nxt = _safe_next(form.get("next"))
    ip = request.remote or "?"
    if site.locked(ip):
        return _render(request, "login.html", error="Слишком много попыток, подождите 15 минут.", next=nxt,
                       accounts=[], last_event=0)
    if not hmac.compare_digest(str(form.get("password", "")).encode(), site.password.encode()):
        site.failures.setdefault(ip, []).append(time.time())
        log.warning("web: wrong password from %s", ip)
        return _render(request, "login.html", error="Неверный пароль.", next=nxt, accounts=[], last_event=0)
    site.failures.pop(ip, None)
    response = web.HTTPFound(nxt)
    response.set_cookie(COOKIE, site.make_token(), max_age=site.session_hours * 3600, httponly=True,
                        samesite="Strict", secure=request.secure, path="/")
    raise response


async def logout(request: web.Request) -> web.Response:
    response = web.HTTPFound("/login")
    response.del_cookie(COOKIE, path="/")
    raise response


# --- pages ---

def _filter(request: web.Request, account: str = "") -> queries.Filter:
    q = request.query
    try:
        page = int(q.get("page", "1"))
    except ValueError:
        page = 1
    return queries.Filter(account=account or q.get("account", ""), stage=q.get("stage", ""),
                          q=q.get("q", "").strip(), page=page)


def _table_context(request: web.Request, f: queries.Filter) -> dict:
    conn = request.app[SITE].db.conn
    rows, total = queries.applications(conn, f)
    pages = max((total + queries.PAGE - 1) // queries.PAGE, 1)

    def page_url(n: int) -> str:
        params = {k: v for k, v in {"account": f.account if request.path == "/stages" else "",
                                    "stage": f.stage, "q": f.q, "page": n}.items() if v}
        return f"{request.path}?{urlencode(params)}"

    return {"rows": rows, "total": total, "filter": f, "pages": pages, "page_url": page_url,
            "custom_stages": queries.custom_stages(conn)}


def _hold(site: Site) -> dict:
    until = captcha_hold(site.db, datetime.now(timezone.utc))
    return {"paused": site.db.get("paused") == "1", "captcha_until": until.isoformat() if until else None,
            "resume_lang": site.db.get("resume_lang", "ru")}


async def overview(request: web.Request) -> web.Response:
    site: Site = request.app[SITE]
    conn = site.db.conn
    names = queries.accounts(conn, list(site.configured_accounts()))
    per_account = {name: queries.summary(conn, site.tz, name) for name in names}
    return _render(
        request, "overview.html",
        accounts=names,
        total=queries.summary(conn, site.tz),
        per_account=per_account,
        daily=queries.daily(conn, site.tz),
        changes=queries.recent_changes(conn),
        hold=_hold(site),
        upcoming=queries.upcoming_interviews(conn, 5),
    )


async def account_page(request: web.Request) -> web.Response:
    site: Site = request.app[SITE]
    name = request.match_info["name"]
    names = queries.accounts(site.db.conn, list(site.configured_accounts()))
    if name not in names:
        raise web.HTTPNotFound(text="нет такого аккаунта")
    account = site.configured_accounts().get(name)
    limit = None
    if account is not None:
        first = site.db.get(f"hh_start:{name}")
        if first:
            limit = daily_limit(account, datetime.fromisoformat(first), datetime.now(ZoneInfo(site.tz)))
    f = _filter(request, name)
    return _render(
        request, "account.html",
        name=name,
        summary=queries.summary(site.db.conn, site.tz, name),
        daily=queries.daily(site.db.conn, site.tz, account=name),
        limit=limit,
        per_pass=account.per_pass if account else None,
        hold=_hold(site),
        **_table_context(request, f),
    )


async def stages_page(request: web.Request) -> web.Response:
    return _render(request, "stages.html", **_table_context(request, _filter(request)))


async def application_page(request: web.Request) -> web.Response:
    db: Database = request.app[SITE].db
    app_id = int(request.match_info["id"])
    row = db.application(app_id)
    if row is None:
        raise web.HTTPNotFound(text="нет такого отклика")
    chat = queries.chat_for_application(db.conn, app_id)
    return _render(request, "application.html", row=row, history=db.stage_history(app_id),
                   custom_stages=queries.custom_stages(db.conn), chat=chat,
                   messages=db.chat_messages(chat["id"]) if chat else [],
                   interviews=queries.interviews_for_application(db.conn, app_id))


async def chats_page(request: web.Request) -> web.Response:
    """Chat list on the left, the open chat on the right (or alone on a phone). Read-only until v8."""
    site: Site = request.app[SITE]
    db = site.db
    account, q = request.query.get("account", ""), request.query.get("q", "").strip()
    chat = messages = app = None
    if "id" in request.match_info:
        chat = db.chat(int(request.match_info["id"]))
        if chat is None:
            raise web.HTTPNotFound(text="нет такого чата")
        messages = db.chat_messages(chat["id"])
        app = db.application(chat["application_id"]) if chat["application_id"] else None
    params = urlencode({k: v for k, v in {"account": account, "q": q}.items() if v})
    return _render(request, "chats.html", chats=queries.chats(db.conn, account, q), chat=chat,
                   messages=messages or [], app=app, account=account, q=q, qs=f"?{params}" if params else "",
                   custom_stages=queries.custom_stages(db.conn))


MONTHS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
          "ноябрь", "декабрь")


def _month(value: str | None, today: date) -> date:
    try:
        year, month = (value or "").split("-")
        return date(int(year), int(month), 1)
    except ValueError:
        return today.replace(day=1)


async def calendar_page(request: web.Request) -> web.Response:
    """Month grid, the next interviews, and applications at the interview stage still without a date."""
    site: Site = request.app[SITE]
    zone = ZoneInfo(site.tz)
    today = datetime.now(zone).date()
    first = _month(request.query.get("month"), today)
    nxt = (first + timedelta(days=32)).replace(day=1)
    prev = (first - timedelta(days=1)).replace(day=1)
    start = first - timedelta(days=first.weekday())
    end = nxt + timedelta(days=(7 - nxt.weekday()) % 7)
    items: dict[date, list] = {}
    for row in queries.interviews_between(site.db.conn, datetime.combine(start, datetime.min.time(), zone),
                                          datetime.combine(end, datetime.min.time(), zone)):
        items.setdefault(datetime.fromisoformat(row["starts_at"]).astimezone(zone).date(), []).append(row)
    days = [start + timedelta(days=i) for i in range((end - start).days)]
    weeks = [[{"day": d, "in_month": d.month == first.month, "today": d == today, "events": items.get(d, [])}
              for d in days[i:i + 7]] for i in range(0, len(days), 7)]
    pending = []
    for app_id, found in interviews.candidates(site.db, site.tz):
        row = site.db.application(app_id)
        pending.append({"app": row, "found": found})
    return _render(
        request, "calendar.html",
        weeks=weeks, title=f"{MONTHS[first.month - 1]} {first.year}",
        prev=prev.strftime("%Y-%m"), next=nxt.strftime("%Y-%m"), this=today.strftime("%Y-%m"),
        upcoming=queries.upcoming_interviews(site.db.conn, 20), pending=pending,
    )


async def interview_page(request: web.Request) -> web.Response:
    """Add or edit form; ?app=ID ties a new interview to an application, ?at= prefills the date."""
    site: Site = request.app[SITE]
    db = site.db
    row = None
    if "id" in request.match_info:
        row = db.interview(int(request.match_info["id"]))
        if row is None:
            raise web.HTTPNotFound(text="нет такого собеседования")
    app_id = row["application_id"] if row else (int(request.query["app"]) if request.query.get("app", "").isdigit()
                                                 else None)
    app = db.application(app_id) if app_id else None
    if row:
        when = interviews.local_input(datetime.fromisoformat(row["starts_at"]), site.tz)
    else:
        when = request.query.get("at", "")
        if not when and app is not None:
            found = interviews.find_when(app["note"], site.tz, datetime.fromisoformat(app["updated_at"]))
            when = interviews.local_input(found.starts_at, site.tz) if found else ""
    return _render(
        request, "interview.html", row=row, app=app, when=when, tz=site.tz,
        link=row["link"] if row else "", options=queries.linkable_applications(db.conn, app_id),
        back=_safe_next(request.query.get("back")) if request.query.get("back") else "/calendar",
    )


async def stats_page(request: web.Request) -> web.Response:
    site: Site = request.app[SITE]
    conn = site.db.conn
    try:
        days = int(request.query.get("days", "30"))
    except ValueError:
        days = 30
    days = days if days in (7, 30, 90) else 30
    names = queries.accounts(conn, list(site.configured_accounts()))
    account = request.query.get("account", "")
    account = account if account in names else ""
    shown = [account] if account else names
    applied = queries.applied_by_day(conn, site.tz, days, names)
    applied_chart = charts.stacked(applied["days"], {a: applied["series"].get(a, [0] * days) for a in shown},
                                   {a: a for a in shown})
    answers = queries.reactions_by_day(conn, site.tz, days, account)
    answers_chart = charts.stacked(answers["days"], answers["series"],
                                   {key: label for key, label, _ in queries.REACTIONS})
    return _render(
        request, "stats.html", days=days, account=account, applied=applied_chart, answers=answers_chart,
        funnel=queries.funnel(conn, site.tz, days, account), levels=queries.REACHED,
        colors={a: f"c{i % 2 + 1}" for i, a in enumerate(names)},
    )


# --- api for the inline edits ---

def _row_json(row) -> dict:
    return {"id": row["id"], "stage": row["stage"], "label": stages.LABELS.get(row["stage"], row["stage"] or ""),
            "manual": bool(row["stage_manual"]), "note": row["note"]}


async def _json_body(request: web.Request) -> dict:
    # Only JSON is accepted: a cross-site form cannot send it, which (with SameSite cookies) stops CSRF.
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(text="json only")
    body = await request.json()
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="object expected")
    return body


async def api_stage(request: web.Request) -> web.Response:
    db: Database = request.app[SITE].db
    app_id = int(request.match_info["id"])
    body = await _json_body(request)
    if db.application(app_id) is None:
        raise web.HTTPNotFound()
    if body.get("auto"):
        db.release_stage(app_id)
    else:
        stage = str(body.get("stage", "")).strip()[:40]
        if not stage:
            raise web.HTTPBadRequest(text="stage is empty")
        db.set_stage(app_id, stage, str(body.get("comment", "")).strip()[:500])
    return web.json_response(_row_json(db.application(app_id)))


async def api_note(request: web.Request) -> web.Response:
    db: Database = request.app[SITE].db
    app_id = int(request.match_info["id"])
    body = await _json_body(request)
    try:
        db.set_note(app_id, str(body.get("note", "")).strip()[:2000])
    except LookupError:
        raise web.HTTPNotFound()
    return web.json_response(_row_json(db.application(app_id)))


def _interview_fields(site: Site, body: dict) -> dict:
    try:
        starts_at = interviews.parse_local(str(body.get("starts_at", "")), site.tz)
    except ValueError:
        raise web.HTTPBadRequest(text="дата и время нужны в виде 2026-10-24T15:00")
    app = body.get("application_id")
    if app in ("", None):
        app = None
    elif not str(app).isdigit():
        raise web.HTTPBadRequest(text="application_id")
    link = str(body.get("link", "")).strip()[:500]
    if link and not link.startswith(("https://", "http://")):
        raise web.HTTPBadRequest(text="ссылка должна начинаться с https://")
    return {"starts_at": starts_at, "application_id": int(app) if app is not None else None,
            "title": str(body.get("title", "")).strip()[:200], "link": link,
            "note": str(body.get("note", "")).strip()[:2000]}


async def api_interview_save(request: web.Request) -> web.Response:
    site: Site = request.app[SITE]
    fields = _interview_fields(site, await _json_body(request))
    try:
        if "id" in request.match_info:
            interview_id = int(request.match_info["id"])
            site.db.update_interview(interview_id, fields.pop("starts_at"), **fields)
        else:
            interview_id = site.db.add_interview(fields.pop("starts_at"), **fields)
    except LookupError:
        raise web.HTTPNotFound()
    return web.json_response({"id": interview_id})


async def api_interview_delete(request: web.Request) -> web.Response:
    await _json_body(request)
    try:
        request.app[SITE].db.delete_interview(int(request.match_info["id"]))
    except LookupError:
        raise web.HTTPNotFound()
    return web.json_response({"ok": True})


async def api_last_event(request: web.Request) -> web.Response:
    return web.json_response({"id": queries.last_event_id(request.app[SITE].db.conn)})


def build_app(site: Site) -> web.Application:
    app = web.Application(middlewares=[headers, auth], client_max_size=64 * 1024)
    app[SITE] = site
    app[TEMPLATES] = _templates(site.tz)
    app.add_routes([
        web.get("/login", login_page),
        web.post("/login", login),
        web.post("/logout", logout),
        web.get("/", overview),
        web.get("/stages", stages_page),
        web.get("/account/{name}", account_page),
        web.get(r"/application/{id:\d+}", application_page),
        web.get("/chats", chats_page),
        web.get(r"/chats/{id:\d+}", chats_page),
        web.get("/calendar", calendar_page),
        web.get("/calendar/new", interview_page),
        web.get(r"/calendar/{id:\d+}", interview_page),
        web.get("/stats", stats_page),
        web.post("/api/interviews", api_interview_save),
        web.post(r"/api/interviews/{id:\d+}", api_interview_save),
        web.post(r"/api/interviews/{id:\d+}/delete", api_interview_delete),
        web.post(r"/api/applications/{id:\d+}/stage", api_stage),
        web.post(r"/api/applications/{id:\d+}/note", api_note),
        web.get("/api/last-event", api_last_event),
        web.static("/static", Path(__file__).parent / "static"),
    ])
    return app


def main() -> None:
    cfg = Config.from_env(need_telegram=False)
    password = os.environ.get("WEB_PASSWORD", "")
    if len(password) < 8:
        raise SystemExit("Set WEB_PASSWORD in .env (at least 8 characters) to start the site")
    site = Site(
        db=Database(cfg.db_path),
        password=password,
        secret=_secret(cfg.data_dir),
        tz=cfg.tz,
        accounts_path=cfg.accounts_path,
        session_hours=int(os.environ.get("WEB_SESSION_HOURS", "24")),
    )
    moved = interviews.import_once(site.db, site.tz)
    if moved:
        log.info("calendar: %d interview dates moved from notes", moved)
    port = int(os.environ.get("WEB_PORT", "8080"))
    log.info("site on port %d", port)
    web.run_app(build_app(site), host=os.environ.get("WEB_HOST", "0.0.0.0"), port=port, access_log=None,
                print=None)
