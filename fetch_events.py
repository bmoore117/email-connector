from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
from imap_tools import MailBox

from parse_events import parse_email, fetch_luma_calendar, is_past, is_in_next_n_days, filter_prospective

load_dotenv()

# Resolve all output paths relative to this script's directory under a "run/" folder.
# This folder should be gitignored.
SCRIPT_DIR = Path(__file__).parent.resolve()
RUN_DIR = SCRIPT_DIR / "run"
RUN_DIR.mkdir(exist_ok=True)

ARCHIVE_DIR = RUN_DIR / "archive"
ARCHIVE_DIR.mkdir(exist_ok=True)

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
GMAIL_LABEL = os.environ.get("GMAIL_LABEL", "miami-social-event-source")
LUMA_CALENDARS = [s.strip() for s in os.environ.get("LUMA_CALENDARS", "").split(",") if s.strip()]

UPCOMING_EVENTS_PATH = RUN_DIR / "upcoming_events.json"
PAST_EVENTS_PATH = RUN_DIR / "past_events.json"
LOG_PATH = RUN_DIR / "connector.log"
PROCESSED_IDS_PATH = RUN_DIR / ".processed_ids"
DELTA_PATH = RUN_DIR / "upcoming-delta.json"
ARCHIVED_DELTA_PATH = ARCHIVE_DIR / "upcoming-delta.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        TimedRotatingFileHandler(LOG_PATH, when="D", interval=1, backupCount=3),
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


def _event_key(event: dict) -> str:
    """Canonical deduplication key for an event."""
    url = (event.get("luma_url") or "").split("?")[0].rstrip("/").lower()
    if url:
        return url
    title = (event.get("title") or "").strip().lower()
    date_val = (event.get("date") or "").strip()
    return f"{title}|{date_val}"


def _rotate_and_classify(
    upcoming: list[dict],
    past: list[dict],
    new_events: list[dict],
) -> tuple[list[dict], list[dict], list[dict]]:
    """
    1. Move any existing upcoming events whose date has now passed into past.
    2. Classify each new event into upcoming or past, skipping duplicates.

    Returns ``(new_upcoming, new_past, newly_added_upcoming)``.
    """
    rotated_to_past = [e for e in upcoming if is_past(e)]
    still_upcoming = [e for e in upcoming if not is_past(e)]

    still_upcoming = filter_prospective(still_upcoming)

    seen_upcoming = {_event_key(e) for e in still_upcoming}
    seen_past = {_event_key(e) for e in past + rotated_to_past}

    newly_added_upcoming: list[dict] = []
    newly_added_past: list[dict] = []
    for e in new_events:
        key = _event_key(e)
        if is_past(e):
            if key not in seen_past:
                newly_added_past.append(e)
                seen_past.add(key)
        else:
            if key not in seen_upcoming and is_in_next_n_days(e):
                newly_added_upcoming.append(e)
                seen_upcoming.add(key)

    return (
        still_upcoming + newly_added_upcoming,
        past + rotated_to_past + newly_added_past,
        newly_added_upcoming,
    )


def _load_previous_consecutive_failures() -> int:
    """Read the last known consecutive_failures from the archived delta, if any."""
    if not ARCHIVED_DELTA_PATH.exists():
        return 0
    try:
        data = json.loads(ARCHIVED_DELTA_PATH.read_text())
        return int(data.get("health", {}).get("consecutive_failures", 0))
    except Exception:
        return 0


def write_delta_artifact(*, new_events: list[dict], health: dict) -> None:
    """Write a minimal delta artifact for the Hermes watcher."""
    has_errors = bool(health.get("consecutive_failures", 0)) or bool(health.get("last_error"))

    delta_data = {
        "triggered_at": datetime.now(timezone.utc).isoformat(),
        "newEvents": new_events,
        "health": health,
        "hasErrors": has_errors,
    }

    DELTA_PATH.write_text(json.dumps(delta_data, indent=2, default=str))
    log.info(
        "Delta artifact written: %d new event(s), hasErrors=%s",
        len(new_events), has_errors
    )


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

        for slug in LUMA_CALENDARS:
            log.info("Fetching from Luma calendar: %s", slug)
            events = fetch_luma_calendar(slug)
            if events:
                new_events.extend(events)
                log.info("  -> extracted %d event(s) via %s", len(events), events[0].get("parse_method", "luma"))

        save_processed_ids(processed_ids)

        upcoming_events, past_events, newly_added = _rotate_and_classify(
            upcoming_events, past_events, new_events
        )
        _write_json_list(UPCOMING_EVENTS_PATH, upcoming_events)
        _write_json_list(PAST_EVENTS_PATH, past_events)

        log.info(
            "Run complete: %d email(s) processed, %d event(s) parsed, %d newly upcoming, %d upcoming total, %d past total",
            emails_processed,
            len(new_events),
            len(newly_added),
            len(upcoming_events),
            len(past_events),
        )

        # Success path always resets consecutive_failures to 0
        health = {
            "last_run": datetime.now(timezone.utc).isoformat(),
            "emails_processed_last_run": emails_processed,
            "upcoming_events": len(upcoming_events),
            "past_events": len(past_events),
            "consecutive_failures": 0,
            "last_error": None,
            "last_error_time": None,
        }

        if newly_added:
            write_delta_artifact(new_events=newly_added, health=health)
        else:
            log.info("No new events — no delta artifact written")

    except Exception as exc:
        log.error("Run failed: %s", exc, exc_info=True)

        prev_failures = _load_previous_consecutive_failures()
        error_health = {
            "last_run": datetime.now(timezone.utc).isoformat(),
            "emails_processed_last_run": emails_processed,
            "upcoming_events": len(upcoming_events),
            "past_events": len(past_events),
            "consecutive_failures": prev_failures + 1,
            "last_error": str(exc),
            "last_error_time": datetime.now(timezone.utc).isoformat(),
        }
        write_delta_artifact(new_events=[], health=error_health)
        sys.exit(1)


if __name__ == "__main__":
    main()