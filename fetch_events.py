from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import requests
from dotenv import load_dotenv
from imap_tools import MailBox

from parse_events import parse_email, fetch_luma_calendar, is_past, is_in_next_n_days, filter_prospective

load_dotenv()

# Resolve all output paths relative to this script's directory under a "run/" folder.
# This folder should be gitignored.
SCRIPT_DIR = Path(__file__).parent.resolve()
RUN_DIR = SCRIPT_DIR / "run"
RUN_DIR.mkdir(exist_ok=True)

DEFAULT_HERMES_WEBHOOK_URL = "http://127.0.0.1:8644/webhooks/luma-events"
DEFAULT_HERMES_WEBHOOK_SECRET = "INSECURE_NO_AUTH"

UPCOMING_EVENTS_PATH = RUN_DIR / "upcoming_events.json"
PAST_EVENTS_PATH = RUN_DIR / "past_events.json"
LOG_PATH = RUN_DIR / "connector.log"
PROCESSED_IDS_PATH = RUN_DIR / ".processed_ids"
CONNECTOR_STATE_PATH = RUN_DIR / ".connector-state.json"
DEFAULT_AGENT_PROMPT_PATH = SCRIPT_DIR / "hermes" / "agent-prompt.txt"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        TimedRotatingFileHandler(LOG_PATH, when="D", interval=1, backupCount=3),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


def _resolve_hermes_webhook_config() -> tuple[str, str, bool, bool]:
    enabled = os.environ.get("HERMES_WEBHOOK_ENABLED", "true").strip().lower() not in (
        "0",
        "false",
        "no",
    )
    url = os.environ.get("HERMES_WEBHOOK_URL", "").strip()
    secret = os.environ.get("HERMES_WEBHOOK_SECRET", "").strip()

    if enabled:
        if not url:
            url = DEFAULT_HERMES_WEBHOOK_URL
            log.info("HERMES_WEBHOOK_URL unset — using default %s", url)
        if not secret:
            secret = DEFAULT_HERMES_WEBHOOK_SECRET
            log.info(
                "HERMES_WEBHOOK_SECRET unset — using default %s (loopback gateway only)",
                secret,
            )

    no_auth = not secret or secret == "INSECURE_NO_AUTH"
    return url, secret, enabled, no_auth


HERMES_WEBHOOK_URL, HERMES_WEBHOOK_SECRET, HERMES_WEBHOOK_ENABLED, HERMES_NO_AUTH = (
    _resolve_hermes_webhook_config()
)


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
    if not CONNECTOR_STATE_PATH.exists():
        return 0
    try:
        return int(json.loads(CONNECTOR_STATE_PATH.read_text()).get("consecutive_failures", 0))
    except Exception:
        return 0


def _save_connector_state(*, consecutive_failures: int) -> None:
    CONNECTOR_STATE_PATH.write_text(
        json.dumps({"consecutive_failures": consecutive_failures}, indent=2)
    )


def _load_agent_prompt() -> str:
    path = Path(os.environ.get("HERMES_AGENT_PROMPT_PATH", DEFAULT_AGENT_PROMPT_PATH)).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Agent prompt not found: {path}")
    return path.read_text().strip()


def build_run_data(*, new_events: list[dict], health: dict) -> dict:
    has_errors = bool(health.get("consecutive_failures", 0)) or bool(health.get("last_error"))
    return {
        "triggered_at": datetime.now(timezone.utc).isoformat(),
        "newEvents": new_events,
        "health": health,
        "hasErrors": has_errors,
    }


def build_hermes_webhook_payload(
    *, new_events: list[dict], health: dict
) -> tuple[dict, dict]:
    """Webhook body: instructions + structured run fields for the Hermes route template."""
    run_data = build_run_data(new_events=new_events, health=health)
    body = {
        "instructions": _load_agent_prompt(),
        "triggered_at": run_data["triggered_at"],
        "hasErrors": run_data["hasErrors"],
        "newEvents": run_data["newEvents"],
        "health": run_data["health"],
    }
    return body, run_data


def _hermes_webhook_signature(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post_hermes_payload(payload: dict) -> bool:
    if not HERMES_WEBHOOK_ENABLED:
        log.error("Hermes webhook disabled (HERMES_WEBHOOK_ENABLED)")
        return False
    if not HERMES_WEBHOOK_URL:
        log.error("Hermes webhook not configured (set HERMES_WEBHOOK_URL)")
        return False

    body = json.dumps(payload, default=str, separators=(",", ":")).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Request-ID": payload["triggered_at"],
    }
    if not HERMES_NO_AUTH:
        headers["X-Webhook-Signature"] = _hermes_webhook_signature(body, HERMES_WEBHOOK_SECRET)
    try:
        resp = requests.post(
            HERMES_WEBHOOK_URL,
            data=body,
            headers=headers,
            timeout=30,
        )
    except requests.RequestException as exc:
        log.error("Hermes webhook request failed: %s", exc)
        return False

    if resp.ok:
        log.info("Hermes webhook accepted (%s)", resp.status_code)
        return True

    log.error(
        "Hermes webhook rejected (%s): %s",
        resp.status_code,
        (resp.text or "")[:500],
    )
    return False


def notify_hermes(*, new_events: list[dict], health: dict) -> bool:
    """POST instructions and run data to the local Hermes webhook route."""
    try:
        payload, run_data = build_hermes_webhook_payload(new_events=new_events, health=health)
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return False

    ok = _post_hermes_payload(payload)
    if ok:
        log.info(
            "Notified Hermes: %d new event(s), hasErrors=%s",
            len(new_events),
            run_data["hasErrors"],
        )
    return ok


def _sample_test_events() -> list[dict]:
    event_date = (date.today() + timedelta(days=3)).isoformat()
    return [
        {
            "title": "[TEST] Miami Social Radar — webhook check",
            "date": event_date,
            "time": "19:00",
            "location": "Test venue (synthetic — safe to ignore)",
            "description": "Synthetic event from `fetch_events.py test`.",
            "luma_url": "https://lu.ma/test-webhook-check",
            "parse_method": "test",
            "source": "fetch_events.py test",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    ]


def _sample_test_health(*, simulate_error: bool) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    if simulate_error:
        return {
            "last_run": now,
            "emails_processed_last_run": 0,
            "upcoming_events": 0,
            "past_events": 0,
            "consecutive_failures": 1,
            "last_error": "Synthetic error from fetch_events.py test --error",
            "last_error_time": now,
        }
    return {
        "last_run": now,
        "emails_processed_last_run": 0,
        "upcoming_events": 1,
        "past_events": 0,
        "consecutive_failures": 0,
        "last_error": None,
        "last_error_time": None,
    }


def run_hermes_test(*, simulate_error: bool = False, dry_run: bool = False) -> None:
    """POST a synthetic payload to verify the Hermes webhook route (no Gmail/Luma)."""
    log.info("--- Hermes webhook test (error=%s, dry_run=%s) ---", simulate_error, dry_run)
    log.info("Target: %s", HERMES_WEBHOOK_URL or "(not configured)")

    new_events = [] if simulate_error else _sample_test_events()
    health = _sample_test_health(simulate_error=simulate_error)

    try:
        payload, run_data = build_hermes_webhook_payload(new_events=new_events, health=health)
    except FileNotFoundError as exc:
        log.error("%s", exc)
        sys.exit(1)

    if dry_run:
        print(json.dumps(payload, indent=2, default=str))
        log.info("Dry run — payload printed, not sent (hasErrors=%s)", run_data["hasErrors"])
        return

    if _post_hermes_payload(payload):
        log.info("Test webhook succeeded (hasErrors=%s)", run_data["hasErrors"])
        return

    sys.exit(1)


def main() -> None:
    log.info("--- email-connector run started ---")

    gmail_user = os.environ["GMAIL_USER"]
    gmail_password = os.environ["GMAIL_APP_PASSWORD"]
    gmail_label = os.environ.get("GMAIL_LABEL", "miami-social-event-source")
    luma_calendars = [s.strip() for s in os.environ.get("LUMA_CALENDARS", "").split(",") if s.strip()]

    processed_ids = load_processed_ids()
    upcoming_events = _load_json_list(UPCOMING_EVENTS_PATH)
    past_events = _load_json_list(PAST_EVENTS_PATH)
    new_events: list[dict] = []
    emails_processed = 0

    try:
        with MailBox("imap.gmail.com").login(gmail_user, gmail_password) as mailbox:
            mailbox.folder.set(gmail_label)
            log.info("Connected to Gmail, reading label '%s'", gmail_label)

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

        for slug in luma_calendars:
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

        _save_connector_state(consecutive_failures=0)

        if newly_added:
            notify_hermes(new_events=newly_added, health=health)
        else:
            log.info("No new events — Hermes webhook not sent")

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
        _save_connector_state(consecutive_failures=error_health["consecutive_failures"])
        notify_hermes(new_events=[], health=error_health)
        sys.exit(1)


def _parse_test_cli() -> argparse.Namespace | None:
    if len(sys.argv) <= 1:
        return None
    if sys.argv[1] != "test":
        log.error("Unknown command %r — use: fetch_events.py test", sys.argv[1])
        sys.exit(2)

    parser = argparse.ArgumentParser(
        description="POST a synthetic Hermes webhook payload (no Gmail/Luma). Exits 1 on failure.",
    )
    parser.add_argument(
        "--error",
        action="store_true",
        help="Simulate a failed scraper run (hasErrors=true, empty newEvents).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the JSON payload to stdout without POSTing.",
    )
    return parser.parse_args(sys.argv[2:])


if __name__ == "__main__":
    test_args = _parse_test_cli()
    if test_args is None:
        main()
    else:
        run_hermes_test(simulate_error=test_args.error, dry_run=test_args.dry_run)