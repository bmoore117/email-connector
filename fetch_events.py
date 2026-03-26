from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from imap_tools import MailBox

from parse_events import parse_email, fetch_luma_calendar

load_dotenv()

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
GMAIL_LABEL = os.environ.get("GMAIL_LABEL", "miami-social-event-source")
LUMA_CALENDARS = [s.strip() for s in os.environ.get("LUMA_CALENDARS", "").split(",") if s.strip()]
UPCOMING_EVENTS_PATH = Path(os.environ.get("UPCOMING_EVENTS_PATH", "upcoming_events.json"))
PAST_EVENTS_PATH = Path(os.environ.get("PAST_EVENTS_PATH", "past_events.json"))
HEALTH_OUTPUT_PATH = Path(os.environ.get("HEALTH_OUTPUT_PATH", "health.json"))
LOG_PATH = Path(os.environ.get("LOG_PATH", "connector.log"))
PROCESSED_IDS_PATH = Path(os.environ.get("PROCESSED_IDS_PATH", ".processed_ids"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


def load_processed_ids() -> set[str]:
    if PROCESSED_IDS_PATH.exists():
        return set(line for line in PROCESSED_IDS_PATH.read_text().splitlines() if line)
    return set()


def save_processed_ids(ids: set[str]) -> None:
    PROCESSED_IDS_PATH.write_text("\n".join(sorted(ids)))


def _load_json_list(path: Path) -> list[dict]:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            log.warning("Could not parse %s — starting fresh", path.name)
    return []


def _write_json_list(path: Path, events: list[dict]) -> None:
    path.write_text(json.dumps(events, indent=2, default=str))


def _parse_event_date(date_str: str | None) -> date | None:
    """Parse the event's date field to a date object. Returns None if unparseable."""
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


def _is_past(event: dict) -> bool:
    """Return True if the event date is strictly before today (i.e. it has ended)."""
    event_date = _parse_event_date(event.get("date"))
    if event_date is None:
        return False  # Can't determine — leave in upcoming
    return event_date < date.today()


def _rotate_and_classify(
    upcoming: list[dict],
    past: list[dict],
    new_events: list[dict],
) -> tuple[list[dict], list[dict]]:
    """
    1. Move any existing upcoming events whose date has now passed into past.
    2. Classify each new event into upcoming or past based on its date.
    Returns (new_upcoming, new_past).
    """
    rotated_to_past = [e for e in upcoming if _is_past(e)]
    still_upcoming = [e for e in upcoming if not _is_past(e)]

    new_upcoming = [e for e in new_events if not _is_past(e)]
    new_past = [e for e in new_events if _is_past(e)]

    return (
        still_upcoming + new_upcoming,
        past + rotated_to_past + new_past,
    )


def write_health(*, last_error: str | None, emails_processed: int, upcoming_count: int, past_count: int) -> None:
    now = datetime.now(timezone.utc).isoformat()

    existing: dict = {}
    if HEALTH_OUTPUT_PATH.exists():
        try:
            existing = json.loads(HEALTH_OUTPUT_PATH.read_text())
        except Exception:
            pass

    if last_error:
        consecutive_failures = existing.get("consecutive_failures", 0) + 1
        last_success = existing.get("last_success")
        last_error_time = now
    else:
        consecutive_failures = 0
        last_success = now
        last_error_time = existing.get("last_error_time")

    health = {
        "last_run": now,
        "last_success": last_success,
        "consecutive_failures": consecutive_failures,
        "last_error": last_error,
        "last_error_time": last_error_time,
        "emails_processed_last_run": emails_processed,
        "upcoming_events": upcoming_count,
        "past_events": past_count,
    }
    HEALTH_OUTPUT_PATH.write_text(json.dumps(health, indent=2))


def main() -> None:
    log.info("--- email-connector run started ---")

    processed_ids = load_processed_ids()
    upcoming_events = _load_json_list(UPCOMING_EVENTS_PATH)
    past_events = _load_json_list(PAST_EVENTS_PATH)
    new_events: list[dict] = []
    emails_processed = 0

    try:
        with MailBox("imap.gmail.com").login(GMAIL_USER, GMAIL_APP_PASSWORD) as mailbox:
            mailbox.folder.set(GMAIL_LABEL)
            log.info("Connected to Gmail, reading label '%s'", GMAIL_LABEL)

            for msg in mailbox.fetch():
                msg_id = (msg.headers.get("message-id") or [None])[0]

                if not msg_id:
                    msg_id = f"{msg.from_}|{msg.subject}|{msg.date}"

                if msg_id in processed_ids:
                    continue

                log.info("Processing: [%s] from %s", msg.subject, msg.from_)
                events = parse_email(msg)

                if events:
                    new_events.extend(events)
                    log.info("  -> extracted %d event(s) via %s", len(events), events[0].get("parse_method"))
                else:
                    log.info("  -> no events found in this email")

                processed_ids.add(msg_id)
                emails_processed += 1

        # Fetch directly from key Luma calendars to broaden scope beyond Gmail emails only.
        # Uses existing parsing logic from parse_events.py.
        for slug in LUMA_CALENDARS:
            log.info("Fetching from Luma calendar: %s", slug)
            events = fetch_luma_calendar(slug)
            if events:
                new_events.extend(events)
                log.info("  -> extracted %d event(s) via %s", len(events), events[0].get("parse_method", "luma"))

        save_processed_ids(processed_ids)

        upcoming_events, past_events = _rotate_and_classify(upcoming_events, past_events, new_events)
        _write_json_list(UPCOMING_EVENTS_PATH, upcoming_events)
        _write_json_list(PAST_EVENTS_PATH, past_events)

        log.info(
            "Run complete: %d email(s) processed, %d new event(s), %d upcoming, %d past",
            emails_processed,
            len(new_events),
            len(upcoming_events),
            len(past_events),
        )
        write_health(
            last_error=None,
            emails_processed=emails_processed,
            upcoming_count=len(upcoming_events),
            past_count=len(past_events),
        )

    except Exception as exc:
        log.error("Run failed: %s", exc, exc_info=True)
        write_health(
            last_error=str(exc),
            emails_processed=emails_processed,
            upcoming_count=len(upcoming_events),
            past_count=len(past_events),
        )
        sys.exit(1)


if __name__ == "__main__":
    main()