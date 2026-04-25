from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from imap_tools import MailBox

from parse_events import parse_email, fetch_luma_calendar, is_past, is_in_next_n_days, filter_prospective
from taskflow import trigger_radar

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

# Taskflow / agent routing — everything trigger_radar needs comes from here
# so taskflow.py never touches disk for config.
TASKFLOW_USER_TARGET = os.environ.get("TASKFLOW_USER_TARGET", "")
TASKFLOW_TRIGGER_PATH = Path(os.environ.get("TASKFLOW_TRIGGER_PATH", "miami-social-radar-trigger.json"))
TASKFLOW_WEBHOOK_URL = os.environ.get("TASKFLOW_WEBHOOK_URL", "")
TASKFLOW_WEBHOOK_SECRET = os.environ.get("TASKFLOW_WEBHOOK_SECRET", "")

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


def _event_key(event: dict) -> str:
    """Canonical deduplication key for an event.

    Uses the Luma event URL (query string stripped) when available, since that
    is stable across runs and across email vs. calendar-crawl sources.  Falls
    back to ``title|date`` for events that have no URL.
    """
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

    Returns ``(new_upcoming, new_past, newly_added_upcoming)`` — the third
    element is just the events added to the upcoming list this run, which is
    what the TaskFlow cares about.
    """
    rotated_to_past = [e for e in upcoming if is_past(e)]
    still_upcoming = [e for e in upcoming if not is_past(e)]

    # Also drop anything beyond the prospective window (next 7 days)
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


def build_health(*, last_error: str | None, emails_processed: int, upcoming_count: int, past_count: int) -> dict:
    """Build the health dict for this run, rolling in prior state from disk."""
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

    return {
        "last_run": now,
        "last_success": last_success,
        "consecutive_failures": consecutive_failures,
        "last_error": last_error,
        "last_error_time": last_error_time,
        "emails_processed_last_run": emails_processed,
        "upcoming_events": upcoming_count,
        "past_events": past_count,
    }


def write_health(health: dict) -> None:
    HEALTH_OUTPUT_PATH.write_text(json.dumps(health, indent=2))


def _fire_trigger(*, new_events: list[dict], health: dict) -> None:
    """Wrap trigger_radar with env-driven routing and a last-resort safety net.

    trigger_radar handles its own expected failure modes (missing config,
    webhook errors, non-2xx responses) at appropriate log levels. The
    try/except here only catches *unexpected* exceptions — programming bugs,
    truly novel network errors, etc. — so the connector run isn't aborted by
    a flaw in the trigger path. Those are logged at error level with a full
    traceback so they're impossible to miss.
    """
    try:
        trigger_radar(
            new_events=new_events,
            health=health,
            user_target=TASKFLOW_USER_TARGET,
            trigger_path=TASKFLOW_TRIGGER_PATH,
            webhook_url=TASKFLOW_WEBHOOK_URL,
            webhook_secret=TASKFLOW_WEBHOOK_SECRET,
        )
    except Exception:
        log.error("Unexpected error in radar trigger (non-fatal)", exc_info=True)


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
        health = build_health(
            last_error=None,
            emails_processed=emails_processed,
            upcoming_count=len(upcoming_events),
            past_count=len(past_events),
        )
        write_health(health)

        # Only wake the agent when there is something new to surface or an
        # unresolved error to explain. Otherwise successful-but-empty runs
        # would DM the user every 30 minutes.
        has_errors = bool(health.get("consecutive_failures", 0)) or bool(health.get("last_error"))
        if newly_added or has_errors:
            log.info("Triggering radar TaskFlow (%d new event(s), errors=%s)", len(newly_added), has_errors)
            _fire_trigger(new_events=newly_added, health=health)
        else:
            log.info("No new events and no errors — skipping TaskFlow trigger")

    except Exception as exc:
        log.error("Run failed: %s", exc, exc_info=True)
        health = build_health(
            last_error=str(exc),
            emails_processed=emails_processed,
            upcoming_count=len(upcoming_events),
            past_count=len(past_events),
        )
        write_health(health)
        # Still fire a trigger on error so the agent can DM about the failure.
        _fire_trigger(new_events=[], health=health)
        sys.exit(1)


if __name__ == "__main__":
    main()
