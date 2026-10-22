"""RSS 2.0 and Atom into plain job items. Tolerant: the three Serbian feeds were not read from here,
so the tags below are the usual ones (WordPress for Startit, plain RSS for HelloWorld and Infostud)."""
from __future__ import annotations

import hashlib
import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import unquote

MAX_BYTES = 2 * 1024 * 1024
SUMMARY_CHARS = 1500

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"[ \t\r\f\v]+")
_BREAK = re.compile(r"<\s*(br|/p|/li|/div|/h\d)\s*/?\s*>", re.I)


class FeedError(ValueError):
    pass


@dataclass
class FeedItem:
    external_id: str
    title: str
    url: str
    company: str = ""
    location: str = ""
    summary: str = ""
    categories: list[str] = field(default_factory=list)
    published_at: str | None = None   # ISO, UTC


@dataclass
class Feed:
    title: str
    ttl: int | None                   # minutes, RSS <ttl>
    items: list[FeedItem]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _children(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in el if _local(c.tag) == name]


def _text(el: ET.Element, *names: str) -> str:
    for name in names:
        for child in _children(el, name):
            value = "".join(child.itertext()).strip()
            if value:
                return value
    return ""


def clean(raw: str, limit: int | None = None) -> str:
    """HTML of a description into plain text (entities may be escaped twice in feeds)."""
    text = html.unescape(raw or "")
    text = _BREAK.sub("\n", text)
    text = html.unescape(_TAG.sub(" ", text))
    lines = [_SPACE.sub(" ", line).strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    if limit and len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _date(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)           # RSS: Wed, 22 Oct 2026 10:00:00 +0200
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))  # Atom / dc:date
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


_TITLE_COMPANY = re.compile(r"^(?P<title>.+?)\s+(?:[-–—@]|at)\s+(?P<company>[^-–—|@]{2,80})$")
# HelloWorld and Infostud put the employer into the link: /posao/<title or employer-city>/<Employer_name>/<id>
_URL_COMPANY = re.compile(r"/posao/[^/?#]+/(?P<company>[^/?#]+)/\d+(?:[/?#]|$)")


def _split_title(title: str) -> tuple[str, str]:
    """Some job feeds write "Senior PHP Developer - Acme" or "... @ Acme" in the title. A "|" is not taken:
    on HelloWorld and Startit it separates the title from a city or a team."""
    match = _TITLE_COMPANY.match(title)
    if match and len(match.group("title")) >= 4:
        return match.group("title").strip(), match.group("company").strip()
    return title, ""


def _company_from_url(url: str) -> str:
    match = _URL_COMPANY.search(url)
    if not match:
        return ""
    return re.sub(r"[-_]+", " ", unquote(match.group("company"))).strip()


def _item(el: ET.Element, atom: bool) -> FeedItem | None:
    title = clean(_text(el, "title"))
    if atom:
        url = ""
        for link in _children(el, "link"):
            if link.get("rel", "alternate") == "alternate" and link.get("href"):
                url = link.get("href")
                break
        summary = _text(el, "content", "summary")
        published = _text(el, "published", "updated")
        categories = [c.get("term") or c.get("label") or "" for c in _children(el, "category")]
        company = _text(el, "author")
    else:
        url = _text(el, "link")
        summary = _text(el, "encoded", "description")     # content:encoded first, it is the full text
        published = _text(el, "pubdate", "date")
        categories = ["".join(c.itertext()).strip() for c in _children(el, "category")]
        # Not dc:creator / author: in WordPress feeds (Startit) that is the person who posted it.
        company = _text(el, "company", "employer", "hiringorganization")
    location = _text(el, "location", "city", "joblocation")
    external_id = _text(el, "guid", "id") or url or title
    if not title or not (url or external_id):
        return None
    url = html.unescape(url or "").strip()
    if not company:
        company = _company_from_url(url)
    if not company:
        title, company = _split_title(title)
    if company and "@" in company and " " not in company:   # an RSS <author> e-mail, not a company
        company = ""
    return FeedItem(
        external_id=external_id[:300], title=title[:300], url=(url or external_id).strip()[:500],
        company=clean(company)[:120], location=clean(location)[:120], summary=clean(summary, SUMMARY_CHARS),
        categories=[c for c in dict.fromkeys(clean(c)[:60] for c in categories) if c][:15],
        published_at=_date(published),
    )


def parse(data: bytes) -> Feed:
    if len(data) > MAX_BYTES:
        raise FeedError("лента больше 2 МБ")
    if b"<!ENTITY" in data[:4096]:
        raise FeedError("в ленте объявлены сущности XML, читать не буду")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        snippet = data[:80].decode("utf-8", "replace").strip()
        raise FeedError(f"не XML ({e}); начало ответа: {snippet!r}") from None
    kind = _local(root.tag)
    if kind == "feed":
        items = [_item(e, True) for e in _children(root, "entry")]
        return Feed(clean(_text(root, "title")), None, [i for i in items if i])
    if kind == "rss":
        channel = next(iter(_children(root, "channel")), None)
        if channel is None:
            raise FeedError("в RSS нет <channel>")
    elif kind == "rdf":
        channel = root
    else:
        raise FeedError(f"это не RSS и не Atom (корень <{kind}>)")
    items = [_item(e, False) for e in channel.iter() if _local(e.tag) == "item"]
    ttl = _text(channel, "ttl")
    return Feed(clean(_text(channel, "title")), int(ttl) if ttl.isdigit() else None, [i for i in items if i])


def job_hash(title: str, company: str) -> str:
    """Same job posted on two sites of one group (HelloWorld and Infostud) collapses into one."""
    normalized = re.sub(r"[^\w]+", " ", f"{title} | {company}".lower()).strip()
    return hashlib.sha1(normalized.encode()).hexdigest()
