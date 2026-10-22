"""`jobpilot web`: the site. One small aiohttp process next to the workers, behind a password."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
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
from ..feeds import sources as feed_sources
from ..hh import accounts as hh_accounts
from ..hh.accounts import load_accounts
from ..hh.limits import daily_limit
from ..hh.live import LIVE_KEY, live_on
from ..hh.pipeline import captcha_hold, clear_captcha
from ..tg import sources as tg_sources
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
    tg_folder: str | None = None              # TG_FOLDER and TG_SOURCES from .env, shown next to the site's own
    tg_sources: tuple[str, ...] = ()
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

    def configured_accounts(self, *, file_only: bool = False) -> dict:
        """Accounts from accounts.toml with the changes made on the settings page (or without them)."""
        if not self.accounts_path.exists():
            return {}
        return load_accounts(self.accounts_path, None if file_only else self.db)


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

    def plural(n: int, one: str, few: str, many: str) -> str:
        n = abs(int(n))
        if n % 10 == 1 and n % 100 != 11:
            return one
        return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many

    env.filters["local"] = local
    env.filters["plural"] = plural
    env.globals["bar_path"] = charts.bar_path
    env.globals.update(
        LABELS=stages.LABELS,
        STAGE_ORDER=queries.STAGE_ORDER,
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

def _page(request: web.Request) -> int:
    try:
        return max(int(request.query.get("page", "1")), 1)
    except ValueError:
        return 1


def _hold(site: Site) -> dict:
    until = captcha_hold(site.db, datetime.now(timezone.utc))
    return {"paused": site.db.get("paused") == "1", "captcha_until": until.isoformat() if until else None,
            "resume_lang": site.db.get("resume_lang", "ru"), "chats_live": live_on(site.db),
            "accounts_paused": [n for n in site.configured_accounts() if hh_accounts.paused(site.db, n)]}


def _attention(site: Site) -> list[dict]:
    """What needs a look today, newest worry first; empty when nothing does."""
    conn = site.db.conn
    items = []
    hold = captcha_hold(site.db, datetime.now(timezone.utc))
    if hold:
        items.append({"n": "!", "text": "hh показал капчу, отклики стоят до " + hold.astimezone(ZoneInfo(site.tz))
                      .strftime("%d.%m %H:%M"), "href": "/settings/captcha", "go": "Настройки"})
    unread, my_turn = queries.unread_total(conn), queries.my_turn_chats(conn)
    if unread or my_turn:
        text = "непрочитанных сообщений в чатах hh" if unread else "чатов ждут вашего ответа"
        if unread and my_turn:
            text += f", в {my_turn} ждут вашего ответа"
        items.append({"n": unread or my_turn, "text": text, "href": "/chats?status=" + ("unread" if unread else "my_turn"),
                      "go": "Чаты"})
    found = queries.new_since(conn, site.tz)
    if found["total"]:
        items.append({"n": found["total"], "text": "новых вакансий из Телеграма и лент сегодня, ждут 👍 или 👎",
                      "href": "/jobs?st=new", "go": "Вакансии"})
    zone = ZoneInfo(site.tz)
    week = datetime.now(timezone.utc) + timedelta(days=7)
    for i in queries.upcoming_interviews(conn, 3):
        at = datetime.fromisoformat(i["starts_at"])
        if at > week:
            break
        who = i["employer"] or i["title"] or i["name"] or "собеседование"
        items.append({"n": at.astimezone(zone).strftime("%d.%m"), "text": f"в {at.astimezone(zone):%H:%M} собеседование, "
                      + who + (f", аккаунт {i['account']}" if i["account"] else ""),
                      "href": f"/calendar/{i['id']}", "go": "Календарь"})
    pending = len(interviews.candidates(site.db, site.tz))
    if pending:
        items.append({"n": pending, "text": "собеседований без даты в календаре", "href": "/calendar", "go": "Календарь"})
    return items


async def overview(request: web.Request) -> web.Response:
    """Summary of the day, or the charts (?view=charts)."""
    if request.query.get("view") == "charts":
        return await stats_page(request)
    site: Site = request.app[SITE]
    conn = site.db.conn
    names = queries.accounts(conn, list(site.configured_accounts()))
    configured = site.configured_accounts()
    per_account = {}
    for name in names:
        s = queries.summary(conn, site.tz, name)
        s["limit"] = _today_limit(site, configured[name]) if name in configured else None
        per_account[name] = s
    return _render(
        request, "overview.html",
        accounts=names,
        total=queries.summary(conn, site.tz),
        per_account=per_account,
        daily=queries.daily(conn, site.tz),
        changes=queries.recent_changes(conn),
        hold=_hold(site),
        attention=_attention(site),
        found=queries.new_since(conn, site.tz),
    )


# Chips each kind of tab shows, in this order.
TAB_BUCKETS = {
    "all": ("", "new", "liked", "applied", "process", "offer", "rejected", "skip"),
    "hh": ("", "applied", "process", "offer", "rejected", "skip"),
    "tg": ("", "new", "liked", "applied", "process", "offer", "rejected", "skip"),
    "feed": ("", "new", "liked", "applied", "process", "offer", "rejected", "skip"),
}
APPLIED_BUCKETS = ("applied", "process", "offer", "rejected")


def _tabs(site: Site, counts: dict) -> list[dict]:
    """Все, one tab per hh account, Telegram, one tab per feed country."""
    names = queries.accounts(site.db.conn, list(site.configured_accounts()))
    tabs = [{"key": "", "label": "Все", "kind": "all"}]
    tabs += [{"key": n, "label": f"hh · {n}", "kind": "hh"} for n in names]
    tabs.append({"key": queries.TG_TAB, "label": "Телеграм", "kind": "tg"})
    countries = [s.country for s in feed_sources.all_sources(site.db) if s.enabled] + queries.feed_countries(site.db.conn)
    for code in dict.fromkeys(c.lower() for c in countries if c):
        if code not in names and code != queries.TG_TAB:
            tabs.append({"key": code, "label": feed_sources.country_name(code.upper()), "kind": "feed"})
    for tab in tabs:
        tab["n"] = counts.get(tab["key"], {}).get("", 0)
    return tabs


def _tab_summary(site: Site, tab: dict) -> dict:
    """The numbers above the list of one source."""
    db, conn = site.db, site.db.conn
    if tab["kind"] == "hh":
        name = tab["key"]
        account = site.configured_accounts().get(name)
        return {"s": queries.summary(conn, site.tz, name), "limit": _today_limit(site, account) if account else None,
                "per_pass": account.per_pass if account else None, "paused": hh_accounts.paused(db, name),
                "configured": account is not None}
    if tab["kind"] == "tg":
        return {"s": queries.tg_summary(conn, site.tz), "seen": tg_sources.seen(db),
                "paused": db.get(tg_sources.PAUSED) == "1", "mode": db.get("mode"), "digest_time": db.get("digest_time")}
    if tab["kind"] == "feed":
        feeds = [{"source": s, "state": feed_sources.state(db, s.key)} for s in feed_sources.all_sources(db)
                 if s.country.lower() == tab["key"]]
        return {"s": queries.feed_summary(conn, site.tz, tab["key"]), "feeds": feeds,
                "paused": db.get(feed_sources.PAUSED) == "1"}
    return {}


async def jobs_page(request: web.Request) -> web.Response:
    """Every vacancy in one list: hh applications, Telegram posts and job-site feeds, a tab per source."""
    site: Site = request.app[SITE]
    conn = site.db.conn
    query = request.query
    counts = queries.jobs_counts(conn)
    tabs = _tabs(site, counts)
    tab = next((t for t in tabs if t["key"] == query.get("src", "")), tabs[0])
    bucket = query.get("st", "")
    bucket = bucket if bucket in TAB_BUCKETS[tab["kind"]] else ""
    f = queries.JobsFilter(src=tab["key"], bucket=bucket,
                           stage=query.get("stage", "")[:40] if tab["kind"] == "hh" else "",
                           origin=query.get("from", "")[:64] if tab["kind"] in ("tg", "feed") else "",
                           q=query.get("q", "").strip()[:100], page=_page(request))
    rows, total = queries.jobs(conn, f)
    pages = max((total + queries.JOBS_PAGE - 1) // queries.JOBS_PAGE, 1)
    params = {"src": f.src, "st": f.bucket, "stage": f.stage, "from": f.origin, "q": f.q}

    def url(**changes) -> str:
        values = {k: v for k, v in {**params, **changes}.items() if v}
        return "/jobs" + (f"?{urlencode(values)}" if values else "")

    here = counts.get(tab["key"], {})
    applied_here = sum(here.get(b, 0) for b in APPLIED_BUCKETS)
    chips = [{"key": b, "label": queries.BUCKETS[b], "n": here.get(b, 0)} for b in TAB_BUCKETS[tab["kind"]]]
    chips = [c for c in chips if c["n"] or not c["key"] or c["key"] == f.bucket]
    feed_names = {s.key: s.name for s in feed_sources.all_sources(site.db)}
    return _render(
        request, "jobs.html", tabs=tabs, tab=tab, rows=rows, total=total, f=f, pages=pages, url=url, chips=chips,
        summary=_tab_summary(site, tab), hold=_hold(site), custom_stages=queries.custom_stages(conn),
        origins=queries.jobs_origins(conn, tab["key"]) if tab["kind"] in ("tg", "feed") else [],
        feed_names=feed_names, country_name=feed_sources.country_name, applied_here=applied_here,
    )


# --- old addresses: the pages that the Вакансии section and the settings replaced ---

def _moved(target: str, **params) -> web.HTTPFound:
    values = {k: v for k, v in params.items() if v}
    return web.HTTPFound(target + (f"?{urlencode(values)}" if values else ""))


_VERDICT_BUCKET = {"new": "new", "1": "liked", "0": "skip"}


async def old_stages(request: web.Request) -> web.Response:
    q = request.query
    raise _moved("/jobs", src=q.get("account", ""), stage=q.get("stage", "") if q.get("stage") != "none" else "",
                 st="skip" if q.get("stage") == "none" else "", q=q.get("q", ""))


async def old_account(request: web.Request) -> web.Response:
    q = request.query
    raise _moved("/jobs", src=request.match_info["name"], stage=q.get("stage", ""), q=q.get("q", ""))


async def old_telegram(request: web.Request) -> web.Response:
    q = request.query
    raise _moved("/jobs", src=queries.TG_TAB, st=_VERDICT_BUCKET.get(q.get("verdict", ""), ""),
                 **{"from": q.get("source", ""), "q": q.get("q", "")})


async def old_feeds(request: web.Request) -> web.Response:
    q = request.query
    countries = q.get("country", "") or next(iter(queries.feed_countries(request.app[SITE].db.conn)), "")
    countries = countries or next((s.country for s in feed_sources.all_sources(request.app[SITE].db)), "")
    raise _moved("/jobs", src=countries.lower(), st="skip" if q.get("show") == "other" else
                 _VERDICT_BUCKET.get(q.get("verdict", ""), ""), **{"from": q.get("source", ""), "q": q.get("q", "")})


async def old_stats(request: web.Request) -> web.Response:
    q = request.query
    raise _moved("/", view="charts", days=q.get("days", ""), account=q.get("account", ""))


async def application_page(request: web.Request) -> web.Response:
    db: Database = request.app[SITE].db
    app_id = int(request.match_info["id"])
    row = db.application(app_id)
    if row is None:
        raise web.HTTPNotFound(text="нет такого отклика")
    chat = queries.chat_for_application(db.conn, app_id)
    return _render(request, "application.html", row=row, history=db.stage_history(app_id),
                   back=_safe_next(request.query.get("back")) if request.query.get("back") else
                   "/jobs?" + urlencode({"src": row["account"]}),
                   custom_stages=queries.custom_stages(db.conn), chat=chat,
                   messages=db.chat_messages(chat["id"]) if chat else [],
                   interviews=queries.interviews_for_application(db.conn, app_id),
                   country_name=feed_sources.country_name)


async def chats_page(request: web.Request) -> web.Response:
    """Chat list on the left, the open chat with the reply box on the right (or alone on a phone)."""
    site: Site = request.app[SITE]
    db = site.db
    account, q = request.query.get("account", ""), request.query.get("q", "").strip()
    status = request.query.get("status", "")
    status = status if status in queries.CHAT_FILTERS else ""
    chat = messages = app = None
    outbox = []
    if "id" in request.match_info:
        chat = db.chat(int(request.match_info["id"]))
        if chat is None:
            raise web.HTTPNotFound(text="нет такого чата")
        messages = db.chat_messages(chat["id"])
        app = db.application(chat["application_id"]) if chat["application_id"] else None
        outbox = db.outbox_for_chat(chat["id"])
    params = urlencode({k: v for k, v in {"account": account, "status": status, "q": q}.items() if v})
    return _render(request, "chats.html", chats=queries.chats(db.conn, account, q, status), chat=chat,
                   messages=messages or [], app=app, account=account, q=q, status=status,
                   filters=queries.CHAT_FILTERS, counts=queries.chat_filter_counts(db.conn, account),
                   states=queries.CHAT_STATES, outbox=outbox, hold=_hold(site),
                   qs=f"?{params}" if params else "", custom_stages=queries.custom_stages(db.conn))


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


# --- settings (v6, split into sections in v10): the site writes settings, the bot and the workers read them ---

def _today_limit(site: Site, account) -> int | None:
    first = site.db.get(f"hh_start:{account.name}")
    return daily_limit(account, datetime.fromisoformat(first), datetime.now(ZoneInfo(site.tz))) if first else None


def _account_card(site: Site, name: str, base) -> dict:
    db = site.db
    start_of_day = datetime.now(ZoneInfo(site.tz)).replace(hour=0, minute=0, second=0, microsecond=0)
    account = hh_accounts.with_overrides(base, db)
    return {"name": name, "base": base, "account": account, "changed": hh_accounts.overrides(db, name),
            "paused": hh_accounts.paused(db, name), "limit": _today_limit(site, account),
            "today": db.hh_applied_since(name, start_of_day.astimezone(timezone.utc))}


def _telegram_context(site: Site) -> dict:
    db = site.db
    f = db.filters()
    return dict(
        by_source=queries.tg_by_source(db.conn, site.tz),
        mode=db.get("mode"), digest_time=db.get("digest_time"), include=f.include, exclude=f.exclude,
        folder=tg_sources.folder(site.tg_folder, db), env_folder=site.tg_folder, env_sources=site.tg_sources,
        extra=tg_sources.extra(db), seen=tg_sources.seen(db), tg_paused=db.get(tg_sources.PAUSED) == "1",
        digest_requested=db.get(tg_sources.DIGEST_REQUEST) == "1",
    )


def _feeds_context(site: Site) -> dict:
    db = site.db
    counts = queries.feed_by_source(db.conn, site.tz)
    feeds = [{"source": s, "state": feed_sources.state(db, s.key), "counts": counts.get(s.key)}
             for s in feed_sources.all_sources(db)]
    return dict(
        feeds=feeds, country_name=feed_sources.country_name,
        include=feed_sources.words(db, feed_sources.INCLUDE), exclude=feed_sources.words(db, feed_sources.EXCLUDE),
        feeds_paused=db.get(feed_sources.PAUSED) == "1", mode=db.get("mode"), digest_time=db.get("digest_time"),
        extra_lines=feed_sources.extra_lines(db), min_interval=feed_sources.MIN_INTERVAL,
    )


async def settings_page(request: web.Request) -> web.Response:
    """/settings (general), /settings/captcha, /settings/hh/<account>, /settings/telegram, /settings/feeds."""
    site: Site = request.app[SITE]
    files = site.configured_accounts(file_only=True)
    section = request.match_info.get("section", "general")
    context: dict = {}
    if section == "hh":
        name = request.match_info["name"]
        if name not in files:
            raise web.HTTPNotFound(text="нет такого аккаунта в accounts.toml")
        context["card"] = _account_card(site, name, files[name])
    elif section == "captcha":
        context["strikes"] = int(site.db.get("hh_captcha_strikes", "0") or 0)
    elif section == "telegram":
        context.update(_telegram_context(site))
    elif section == "feeds":
        context.update(_feeds_context(site))
    return _render(request, "settings.html", section=section, account_names=list(files), hold=_hold(site),
                   file=str(site.accounts_path), **context)


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


async def api_applied(request: web.Request) -> web.Response:
    """"Откликнулся" on a Telegram post or a feed item: from now on it is an application with stages."""
    db: Database = request.app[SITE].db
    await _json_body(request)
    kind, item_id = request.match_info["kind"], int(request.match_info["id"])
    if kind == "tg":
        row = db.conn.execute("SELECT * FROM tg_posts WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise web.HTTPNotFound()
        title, _ = queries._title_of_post(row["text"])
        app_id = db.add_manual_application("tg", queries.TG_TAB, str(item_id), title, row["source"], row["url"])
        db.conn.execute("UPDATE tg_posts SET verdict = 1 WHERE id = ?", (item_id,))
    else:
        row = db.conn.execute("SELECT * FROM feed_jobs WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise web.HTTPNotFound()
        employer = row["company"] or {s.key: s.name for s in feed_sources.all_sources(db)}.get(row["source"], "")
        app_id = db.add_manual_application("feed", (row["country"] or "").lower(), str(item_id), row["title"],
                                           employer, row["url"])
        db.feed_set_verdict(item_id, 1)
    return web.json_response({"id": app_id})


async def api_application_delete(request: web.Request) -> web.Response:
    """Undo "Откликнулся"; applications from hh cannot be deleted."""
    await _json_body(request)
    try:
        request.app[SITE].db.delete_manual_application(int(request.match_info["id"]))
    except LookupError:
        raise web.HTTPNotFound(text="удалить можно только отклик, отмеченный вручную")
    return web.json_response({"ok": True})


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


def _words(value, *, lower: bool = True, lines: bool = False, limit: int = 100) -> list[str]:
    """A textarea (comma or line separated; `lines` = one per line, for search queries) or a JSON list,
    cleaned and capped."""
    if isinstance(value, list):
        items = [str(v) for v in value]
    else:
        items = re.split(r"\n" if lines else r"[,\n]", str(value or ""))
    items = [(i.strip().lower() if lower else i.strip())[:80] for i in items]
    items = [i for i in items if i]
    return list(dict.fromkeys(items))[:limit]


def _flag(value) -> bool:
    return value in (True, 1, "1", "on", "true")


async def api_telegram(request: web.Request) -> web.Response:
    site: Site = request.app[SITE]
    db = site.db
    body = await _json_body(request)
    if "mode" in body:
        if body["mode"] not in ("realtime", "digest"):
            raise web.HTTPBadRequest(text="режим: realtime или digest")
        db.set("mode", body["mode"])
    if "digest_time" in body:
        value = str(body["digest_time"]).strip()
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
            raise web.HTTPBadRequest(text="время дайджеста в виде 09:00")
        db.set("digest_time", value)
    for key in ("include", "exclude"):
        if key in body:
            db.set_keywords(key, _words(body[key]))
    if "folder" in body:
        db.set(tg_sources.FOLDER, str(body["folder"]).strip()[:64])
    if "sources" in body:
        raw = body["sources"] if isinstance(body["sources"], list) else re.split(r"[\s,]+", str(body["sources"]))
        good, bad = [], []
        for item in raw:
            if not str(item).strip():
                continue
            value = tg_sources.normalize(str(item))
            (good if value else bad).append(value or str(item).strip())
        if bad:
            raise web.HTTPBadRequest(text="не похоже на канал или чат: " + ", ".join(bad[:5])
                                     + ". Нужен @username, ссылка t.me/username или числовой id.")
        db.set(tg_sources.EXTRA, json.dumps(list(dict.fromkeys(good))[:200], ensure_ascii=False))
    if "paused" in body:
        db.set(tg_sources.PAUSED, "1" if _flag(body["paused"]) else "0")
    db.event("settings", part="telegram")
    return web.json_response({"ok": True})


async def api_tg_verdict(request: web.Request) -> web.Response:
    db: Database = request.app[SITE].db
    body = await _json_body(request)
    value = body.get("verdict")
    if value not in (0, 1, None):
        raise web.HTTPBadRequest(text="verdict: 1, 0 или null")
    cur = db.conn.execute("UPDATE tg_posts SET verdict = ? WHERE id = ?", (value, int(request.match_info["id"])))
    if cur.rowcount == 0:
        raise web.HTTPNotFound()
    return web.json_response({"verdict": value})


async def api_tg_digest(request: web.Request) -> web.Response:
    await _json_body(request)
    request.app[SITE].db.set(tg_sources.DIGEST_REQUEST, "1")
    return web.json_response({"ok": True})


async def api_settings(request: web.Request) -> web.Response:
    db: Database = request.app[SITE].db
    body = await _json_body(request)
    if "paused" in body:
        db.set("paused", "1" if _flag(body["paused"]) else "0")
    if "chats_live" in body:
        db.set(LIVE_KEY, "1" if _flag(body["chats_live"]) else "0")
    if "resume_lang" in body:
        if body["resume_lang"] not in ("ru", "en"):
            raise web.HTTPBadRequest(text="резюме: ru или en")
        db.set("resume_lang", body["resume_lang"])
    db.event("settings", part="general")
    return web.json_response({"ok": True})


async def api_account_settings(request: web.Request) -> web.Response:
    site: Site = request.app[SITE]
    name = request.match_info["name"]
    base = site.configured_accounts(file_only=True).get(name)
    if base is None:
        raise web.HTTPNotFound(text="нет такого аккаунта в accounts.toml")
    body = await _json_body(request)
    if "paused" in body:
        hh_accounts.set_paused(site.db, name, _flag(body["paused"]))
    if body.get("reset"):
        hh_accounts.set_overrides(site.db, base, {})
    else:
        values = {}
        for field in hh_accounts.LIST_FIELDS:
            if field in body:
                values[field] = _words(body[field], lower=field in ("include", "exclude"),
                                       lines=field == "queries")
        if "queries" in values and not values["queries"]:
            raise web.HTTPBadRequest(text="нужен хотя бы один поисковый запрос")
        for field, (low, high) in hh_accounts.INT_FIELDS.items():
            if field in body:
                try:
                    number = int(body[field])
                except (TypeError, ValueError):
                    raise web.HTTPBadRequest(text=f"{field}: нужно число")
                if not low <= number <= high:
                    raise web.HTTPBadRequest(text=f"{field}: от {low} до {high}")
                values[field] = number
        start = values.get("start_limit", base.start_limit)
        if values.get("max_limit", base.max_limit) < start:
            raise web.HTTPBadRequest(text="лимит в день не может быть меньше стартового")
        if values:
            current = hh_accounts.with_overrides(base, site.db)
            merged = {f: getattr(current, f) for f in (*hh_accounts.LIST_FIELDS, *hh_accounts.INT_FIELDS)}
            hh_accounts.set_overrides(site.db, base, {**merged, **values})
    site.db.event("settings", name, part="account")
    return web.json_response({"changed": hh_accounts.overrides(site.db, name),
                              "paused": hh_accounts.paused(site.db, name)})


async def api_captcha_clear(request: web.Request) -> web.Response:
    await _json_body(request)
    clear_captcha(request.app[SITE].db)
    request.app[SITE].db.event("settings", part="captcha")
    return web.json_response({"ok": True})


MAX_REPLY = 4000


async def api_chat_reply(request: web.Request) -> web.Response:
    """Queue a reply; the hh worker types it into the chat within a few seconds (or after a captcha hold)."""
    db: Database = request.app[SITE].db
    body = await _json_body(request)
    chat = db.chat(int(request.match_info["id"]))
    if chat is None:
        raise web.HTTPNotFound(text="нет такого чата")
    if chat["source"] != "hh":
        raise web.HTTPBadRequest(text="отвечать можно только в чаты hh")
    text = str(body.get("text", "")).strip()
    if not text:
        raise web.HTTPBadRequest(text="пустой ответ")
    if len(text) > MAX_REPLY:
        raise web.HTTPBadRequest(text=f"слишком длинно: больше {MAX_REPLY} символов")
    return web.json_response({"id": db.outbox_add(chat["id"], text)})


async def api_outbox(request: web.Request) -> web.Response:
    """retry (an error goes back to the queue) or delete (a reply that has not gone out)."""
    db: Database = request.app[SITE].db
    await _json_body(request)
    outbox_id, action = int(request.match_info["id"]), request.match_info["action"]
    row = db.outbox_item(outbox_id)
    if row is None or row["status"] == "sent":
        raise web.HTTPNotFound(text="такого неотправленного ответа нет")
    if action == "retry":
        db.outbox_mark(outbox_id, "pending")
    else:
        db.outbox_delete(outbox_id)
    return web.json_response({"ok": True})


async def api_feeds(request: web.Request) -> web.Response:
    """Pause and words of the feeds, and the feed list: built-in ones (on/off, minutes, address) and added ones."""
    db: Database = request.app[SITE].db
    body = await _json_body(request)
    if "paused" in body:
        db.set(feed_sources.PAUSED, "1" if _flag(body["paused"]) else "0")
    for name, key in (("include", feed_sources.INCLUDE), ("exclude", feed_sources.EXCLUDE)):
        if name in body:
            db.set_keywords(key, _words(body[name]))
    changes = feed_sources.overrides(db)
    for source in feed_sources.BUILTIN:
        change = dict(changes.get(source.key) or {})
        if f"enabled:{source.key}" in body:
            change["enabled"] = _flag(body[f"enabled:{source.key}"])
        if f"interval:{source.key}" in body:
            try:
                minutes = int(body[f"interval:{source.key}"])
            except (TypeError, ValueError):
                raise web.HTTPBadRequest(text=f"{source.name}: интервал нужен числом минут")
            if not feed_sources.MIN_INTERVAL <= minutes <= feed_sources.MAX_INTERVAL:
                raise web.HTTPBadRequest(text=f"{source.name}: интервал от {feed_sources.MIN_INTERVAL} минут"
                                              f" до {feed_sources.MAX_INTERVAL // 1440} дней")
            change["interval"] = minutes
        if f"url:{source.key}" in body:
            url = str(body[f"url:{source.key}"]).strip()
            if url and not feed_sources.valid_url(url):
                raise web.HTTPBadRequest(text=f"{source.name}: адрес ленты должен начинаться с https://")
            if url and url != source.url:
                change["url"] = url
            else:
                change.pop("url", None)
        # Only what differs from the built-in feed is kept.
        change = {k: v for k, v in change.items() if v != getattr(source, k)}
        if change:
            changes[source.key] = change
        else:
            changes.pop(source.key, None)
    db.set(feed_sources.OVERRIDES, json.dumps(changes, ensure_ascii=False))
    if "extra" in body:
        good, bad = feed_sources.parse_extra_lines(str(body["extra"]))
        if bad:
            raise web.HTTPBadRequest(text="не понял строки: " + "; ".join(bad[:3])
                                     + ". Нужно: RS | Название | https://адрес-ленты | минуты")
        feed_sources.save_extra(db, good[:30])
    db.event("settings", part="feeds")
    return web.json_response({"ok": True})


async def api_feed_now(request: web.Request) -> web.Response:
    """Read a feed at the worker's next look (within 5 minutes), but not sooner than MIN_INTERVAL after the last read."""
    db: Database = request.app[SITE].db
    await _json_body(request)
    key = request.match_info["key"]
    if key not in {s.key for s in feed_sources.all_sources(db)}:
        raise web.HTTPNotFound(text="нет такой ленты")
    state = feed_sources.state(db, key)
    now = datetime.now(timezone.utc)
    earliest = now
    if state.get("last_at"):
        earliest = max(now, datetime.fromisoformat(state["last_at"]) + timedelta(minutes=feed_sources.MIN_INTERVAL))
    state["next_at"] = earliest.isoformat()
    feed_sources.save_state(db, key, state)
    return web.json_response({"next_at": state["next_at"]})


async def api_feed_verdict(request: web.Request) -> web.Response:
    db: Database = request.app[SITE].db
    body = await _json_body(request)
    value = body.get("verdict")
    if value not in (0, 1, None):
        raise web.HTTPBadRequest(text="verdict: 1, 0 или null")
    if not db.feed_set_verdict(int(request.match_info["id"]), value):
        raise web.HTTPNotFound()
    return web.json_response({"verdict": value})


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
        web.get("/jobs", jobs_page),
        web.get("/stages", old_stages),
        web.get("/account/{name}", old_account),
        web.get(r"/application/{id:\d+}", application_page),
        web.get("/chats", chats_page),
        web.get(r"/chats/{id:\d+}", chats_page),
        web.get("/calendar", calendar_page),
        web.get("/calendar/new", interview_page),
        web.get(r"/calendar/{id:\d+}", interview_page),
        web.get("/stats", old_stats),
        web.get("/telegram", old_telegram),
        web.get("/feeds", old_feeds),
        web.get("/settings", settings_page),
        web.get(r"/settings/{section:general|captcha|telegram|feeds}", settings_page),
        web.get("/settings/{section:hh}/{name}", settings_page),
        web.post("/api/feeds", api_feeds),
        web.post(r"/api/feeds/{key:[a-z0-9][a-z0-9_-]{1,31}}/now", api_feed_now),
        web.post(r"/api/feeds/jobs/{id:\d+}/verdict", api_feed_verdict),
        web.post("/api/telegram", api_telegram),
        web.post(r"/api/telegram/posts/{id:\d+}/verdict", api_tg_verdict),
        web.post("/api/telegram/digest", api_tg_digest),
        web.post("/api/settings", api_settings),
        web.post("/api/settings/account/{name}", api_account_settings),
        web.post("/api/hh/captcha/clear", api_captcha_clear),
        web.post("/api/interviews", api_interview_save),
        web.post(r"/api/interviews/{id:\d+}", api_interview_save),
        web.post(r"/api/interviews/{id:\d+}/delete", api_interview_delete),
        web.post(r"/api/applications/{id:\d+}/stage", api_stage),
        web.post(r"/api/applications/{id:\d+}/note", api_note),
        web.post(r"/api/applications/{id:\d+}/delete", api_application_delete),
        web.post(r"/api/jobs/{kind:tg|feed}/{id:\d+}/applied", api_applied),
        web.post(r"/api/chats/{id:\d+}/reply", api_chat_reply),
        web.post(r"/api/outbox/{id:\d+}/{action:retry|delete}", api_outbox),
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
        tg_folder=cfg.folder,
        tg_sources=cfg.sources,
    )
    moved = interviews.import_once(site.db, site.tz)
    if moved:
        log.info("calendar: %d interview dates moved from notes", moved)
    port = int(os.environ.get("WEB_PORT", "8080"))
    log.info("site on port %d", port)
    web.run_app(build_app(site), host=os.environ.get("WEB_HOST", "0.0.0.0"), port=port, access_log=None,
                print=None)
