from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests
from bs4 import BeautifulSoup
from icalendar import Calendar

log = logging.getLogger(__name__)

# Matches luma.com/<slug> or lu.ma/<slug>, capturing slug and optional query string.
# Plain text from imap-tools is already QP-decoded, so URLs are clean at this point.
_LUMA_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:luma\.com|lu\.ma)/([a-zA-Z0-9]+)(\?[^\s<>\"']*)?"
)

_REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; email-connector/1.0)"
}


def parse_email(msg: Any) -> list[dict[str, Any]]:
    """Extract events from an imap-tools MailMessage. Returns a list of event dicts."""
    for attachment in msg.attachments:
        if attachment.filename and attachment.filename.lower().endswith(".ics"):
            events = _parse_ics(attachment.payload)
            if events:
                return _enrich(events, msg)

    luma_url = _extract_luma_url(msg.text or "")
    if luma_url:
        log.info("  -> found Luma URL: %s", luma_url)
        events = _fetch_luma_event(luma_url)
        if events:
            return _enrich(events, msg)

    # Last resort: date heuristics from plain text body
    events = _parse_body(msg.text or "")
    return _enrich(events, msg)


# --- Luma-specific parsing ---

def _extract_luma_url(text: str) -> str | None:
    """Find the first Luma event URL in decoded plain text and normalise to lu.ma."""
    m = _LUMA_URL_RE.search(text)
    if not m:
        return None
    slug = m.group(1)
    query = m.group(2) or ""
    return f"https://lu.ma/{slug}{query}"


def _fetch_luma_event(url: str) -> list[dict[str, Any]]:
    try:
        resp = requests.get(url, timeout=15, headers=_REQUEST_HEADERS)
        resp.raise_for_status()
    except Exception as exc:
        log.warning("Failed to fetch Luma page %s: %s", url, exc)
        return []
    return _parse_luma_page(resp.text, url)


def _parse_luma_page(html: str, source_url: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")

    # Tier 1: JSON-LD (standard event schema, present on some Luma events)
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            events = _from_jsonld(json.loads(tag.string or ""), source_url)
            if events:
                return events
        except Exception:
            continue

    # Tier 2: Next.js __NEXT_DATA__ (contains full server-side props)
    next_tag = soup.find("script", id="__NEXT_DATA__")
    if next_tag:
        try:
            events = _from_next_data(json.loads(next_tag.string or ""), source_url)
            if events:
                return events
        except Exception:
            pass

    # Tier 3: HTML scraping (best-effort fallback)
    return _scrape_luma_html(soup, source_url)


def _from_jsonld(data: Any, source_url: str) -> list[dict[str, Any]]:
    if isinstance(data, list):
        for item in data:
            result = _from_jsonld(item, source_url)
            if result:
                return result
        return []

    if not isinstance(data, dict):
        return []

    if data.get("@type") not in ("Event", "SocialEvent", "BusinessEvent", "MusicEvent"):
        return []

    start = data.get("startDate")
    end = data.get("endDate")
    loc = data.get("location", {})
    loc_name = loc.get("name", "") if isinstance(loc, dict) else str(loc)
    loc_address = ""
    if isinstance(loc, dict) and isinstance(loc.get("address"), dict):
        loc_address = loc["address"].get("streetAddress", "")

    return [{
        "title": data.get("name", ""),
        "date": _iso_date(start),
        "time": _iso_time(start),
        "end_date": _iso_date(end),
        "end_time": _iso_time(end),
        "location": ", ".join(filter(None, [loc_name, loc_address])) or None,
        "description": data.get("description") or None,
        "luma_url": source_url,
        "parse_method": "luma_jsonld",
    }]


def _from_next_data(data: dict, source_url: str) -> list[dict[str, Any]]:
    """Navigate Luma's Next.js pageProps to find the event object."""
    props = data.get("props", {}).get("pageProps", {})
    initial_data = props.get("initialData") or {}

    # Calendar / group pages (e.g. lu.ma/hello_miami) store a list of featured
    # events rather than a single event object.
    if initial_data.get("kind") == "calendar":
        return _from_luma_calendar_data(initial_data.get("data") or {}, source_url)

    # Individual event pages — Luma has used several key names across versions.
    event = (
        props.get("event")
        or props.get("initialEvent")
        or props.get("eventData")
        or (initial_data.get("data") or {}).get("event")
        or initial_data.get("event")
    )
    if not event or not isinstance(event, dict):
        return []

    start_at = event.get("start_at")
    end_at = event.get("end_at")
    geo = event.get("geo_address_json") or event.get("geo_address_info") or {}
    location = geo.get("full_address") or geo.get("short_address") or event.get("location") or None

    return [{
        "title": event.get("name", ""),
        "date": _iso_date(start_at),
        "time": _iso_time(start_at),
        "end_date": _iso_date(end_at),
        "end_time": _iso_time(end_at),
        "location": location,
        "description": event.get("description") or None,
        "luma_url": source_url,
        "parse_method": "luma_nextdata",
    }]


def _from_luma_calendar_data(data: dict, source_url: str) -> list[dict[str, Any]]:
    """Extract featured upcoming events from a Luma calendar page's Next.js data.

    Calendar pages embed up to ~3 featured events in ``featured_items``.  Each
    entry's ``event.url`` is a short slug that maps to the full event page at
    ``https://lu.ma/<slug>``.  Descriptions are not included in the calendar
    listing, so callers should follow those URLs to retrieve full details.
    """
    featured_items = data.get("featured_items") or []
    events = []
    for item in featured_items:
        ev = item.get("event") or {}
        if not ev or not ev.get("name"):
            continue

        start_at = ev.get("start_at") or item.get("start_at")
        end_at = ev.get("end_at")

        # Calendar listings use geo_address_info; event pages use geo_address_json.
        geo = ev.get("geo_address_info") or ev.get("geo_address_json") or {}
        location = geo.get("full_address") or geo.get("short_address") or None

        slug = ev.get("url") or ""
        event_url = f"https://lu.ma/{slug}" if slug else source_url

        events.append({
            "title": ev.get("name", ""),
            "date": _iso_date(start_at),
            "time": _iso_time(start_at),
            "end_date": _iso_date(end_at),
            "end_time": _iso_time(end_at),
            "location": location,
            "description": ev.get("description") or None,
            "luma_url": event_url,
            "parse_method": "luma_calendar_nextdata",
        })
    return events


def _scrape_luma_html(soup: BeautifulSoup, source_url: str) -> list[dict[str, Any]]:
    """Tier-3 fallback: scrape visible text from a rendered Luma page."""
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else None
    if not title:
        return []

    text = soup.get_text(separator="\n")

    date_m = re.search(
        r"((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*,\s+\w+ \d{1,2})", text
    )
    time_m = re.search(
        r"(\d{1,2}:\d{2}(?:\s*[–\-]\s*\d{1,2}:\d{2})?\s*(?:AM|PM))", text, re.IGNORECASE
    )
    addr_m = re.search(
        r"\d+\s+[A-Z][^\n]+(?:St|Ave|Blvd|Rd|Dr|Ln|Way|Pl|Place)[^\n]*", text
    )

    return [{
        "title": title,
        "date": date_m.group(1) if date_m else None,
        "time": time_m.group(1) if time_m else None,
        "end_date": None,
        "end_time": None,
        "location": addr_m.group(0).strip() if addr_m else None,
        "description": None,
        "luma_url": source_url,
        "parse_method": "luma_html_scrape",
    }]


# --- ICS attachment parsing ---

def _parse_ics(payload: bytes) -> list[dict[str, Any]]:
    events = []
    try:
        cal = Calendar.from_ical(payload)
        for component in cal.walk():
            if component.name != "VEVENT":
                continue

            dtstart = component.get("DTSTART")
            dtend = component.get("DTEND")

            events.append({
                "title": str(component.get("SUMMARY", "")),
                "date": _fmt_date(dtstart.dt if dtstart else None),
                "time": _fmt_time(dtstart.dt if dtstart else None),
                "end_date": _fmt_date(dtend.dt if dtend else None),
                "end_time": _fmt_time(dtend.dt if dtend else None),
                "location": str(component.get("LOCATION", "")) or None,
                "description": str(component.get("DESCRIPTION", "")) or None,
                "luma_url": None,
                "parse_method": "ics",
            })
    except Exception:
        pass
    return events


# --- Plain-text body fallback (non-Luma emails) ---

_DATE_PATTERNS = [
    r"\b(\w+ \d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})\b",
    r"\b(\d{4}-\d{2}-\d{2})\b",
    r"\b(\d{1,2}/\d{1,2}/\d{4})\b",
]

_TIME_PATTERNS = [
    r"\b(\d{1,2}:\d{2}\s*(?:am|pm))\b",
    r"\b(\d{1,2}\s*(?:am|pm))\b",
]


def _parse_body(text: str) -> list[dict[str, Any]]:
    if not text.strip():
        return []

    date_val = None
    for pattern in _DATE_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            date_val = m.group(1)
            break

    if not date_val:
        return []

    time_val = None
    for pattern in _TIME_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            time_val = m.group(1)
            break

    return [{
        "title": None,
        "date": date_val,
        "time": time_val,
        "end_date": None,
        "end_time": None,
        "location": None,
        "description": text[:500].strip(),
        "luma_url": None,
        "parse_method": "body_text",
    }]


# --- Shared enrichment ---

def _enrich(events: list[dict], msg: Any = None, *, source: str | None = None) -> list[dict]:
    """Stamp provenance fields onto a list of events.

    When ``msg`` is provided (email path) we fill in ``source_email``,
    ``source_message_id``, and a title fallback from the email subject.
    When ``source`` is provided (e.g. ``luma_calendar:FTLYR``) we stamp it
    onto every event so the agent can explain where each event came from.
    ``fetched_at`` is always stamped.
    """
    fetched_at = datetime.now(timezone.utc).isoformat()
    msg_id = None
    msg_from = None
    msg_subject = None
    if msg is not None:
        msg_id = (msg.headers.get("message-id") or [None])[0]
        msg_from = msg.from_
        msg_subject = msg.subject

    for event in events:
        if msg is not None and not event.get("title"):
            event["title"] = msg_subject
        event["source_email"] = msg_from
        event["source_message_id"] = msg_id
        if source is not None:
            event["source"] = source
        event["fetched_at"] = fetched_at
    return events


# --- Date/time helpers ---

def _iso_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date().isoformat()
    except Exception:
        return value


def _iso_time(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).strftime("%H:%M")
    except Exception:
        return None


def _fmt_date(dt: date | datetime | None) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.date().isoformat()
    if isinstance(dt, date):
        return dt.isoformat()
    return None


def _fmt_time(dt: date | datetime | None) -> str | None:
    if isinstance(dt, datetime):
        return dt.strftime("%H:%M")
    return None


def fetch_luma_calendar(slug_or_url: str) -> list[dict[str, Any]]:
    """Fetch upcoming events from a Luma calendar / group page.

    Strategy:
    1. Fetch the calendar page and extract the featured events from its
       ``__NEXT_DATA__`` JSON (up to ~3 upcoming events).
    2. For each featured event, follow its individual event URL to retrieve
       full details — in particular the description, which is absent from the
       calendar listing.
    3. Fall back to the stub data from step 1 if an individual fetch fails.
    """
    if not slug_or_url.startswith(("http://", "https://")):
        url = f"https://lu.ma/{slug_or_url}"
    else:
        url = slug_or_url

    try:
        resp = requests.get(url, timeout=15, headers=_REQUEST_HEADERS)
        resp.raise_for_status()
    except Exception as exc:
        log.warning("Failed to fetch Luma calendar %s: %s", url, exc)
        return []

    stub_events = _parse_luma_page(resp.text, url)
    if not stub_events:
        return []

    # Discard events that have already passed — calendar pages sometimes surface
    # very recently-ended events in their "featured" list.  ISO date strings are
    # lexicographically sortable, so plain string comparison is safe here.
    today = _iso_date(datetime.now(timezone.utc).isoformat())  # "YYYY-MM-DD"
    upcoming_stubs = [
        s for s in stub_events
        if not s.get("date") or s["date"] >= today  # keep if date unknown or future
    ]
    if not upcoming_stubs:
        log.info("    -> no upcoming featured events on calendar page")
        return []

    enriched: list[dict[str, Any]] = []
    for stub in upcoming_stubs:
        event_url = stub.get("luma_url", "")
        # Only follow through to the individual page when we have a distinct
        # event URL (not just the calendar URL itself).
        if event_url and event_url != url and stub.get("parse_method") == "luma_calendar_nextdata":
            log.info("    -> fetching event page: %s", event_url)
            detail = _fetch_luma_event(event_url)
            if detail:
                enriched.extend(detail)
                continue
        enriched.append(stub)

    return _enrich(enriched, source=f"luma_calendar:{slug_or_url}")


def _parse_event_date(date_str: str | None) -> date | None:
    """Parse the event's date field to a date object. Returns None if unparseable.
    (Moved from fetch_events.py so filtering logic lives with the parsers.)"""
    if not date_str:
        return None
    # ISO format (primary source from Luma/ICS)
    try:
        return date.fromisoformat(date_str)
    except ValueError:
        pass
    # Natural-language fallbacks (body_text / html_scrape parse methods)
    for fmt in ("%B %d, %Y", "%A, %B %d", "%A, %B %d, %Y"):
        try:
            parsed = datetime.strptime(date_str, fmt)
            # For formats without a year, assume the next occurrence
            if "%Y" not in fmt:
                today = date.today()
                parsed = parsed.replace(year=today.year)
                if parsed.date() < today:
                    parsed = parsed.replace(year=today.year + 1)
            return parsed.date()
        except ValueError:
            continue
    return None


def is_past(event: dict) -> bool:
    """Return True if the event date is strictly before today."""
    event_date = _parse_event_date(event.get("date"))
    if event_date is None:
        return False  # Can't determine — keep it
    return event_date < date.today()


def is_in_next_n_days(event: dict, days: int = 7) -> bool:
    """Return True if the event is today or within the next N days (prospective only)."""
    event_date = _parse_event_date(event.get("date"))
    if event_date is None:
        return True  # unknown date — keep it for safety
    today = date.today()
    cutoff = today + timedelta(days=days)
    return today <= event_date <= cutoff


def filter_prospective(events: list[dict], days: int = 7) -> list[dict]:
    """Filter to only events in the next N days."""
    return [e for e in events if is_in_next_n_days(e, days)]