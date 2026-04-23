#!/usr/bin/env python3
"""Event-driven TaskFlow trigger for miami-social radar.

Called directly from fetch_events.py on successful run.
The connector now guarantees that upcoming_events only contains prospective events (next ~7 days).
This file builds the trigger artifact and sends the message that creates the managed TaskFlow.

This implements the exact requirements from the thread.
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

def log(msg: str) -> None:
    timestamp = datetime.now().isoformat()
    print(f"{timestamp} [radar-trigger] {msg}", file=sys.stderr)

WORKSPACE = Path("/home/ubuntu/.openclaw/workspace")
LUMA_DIR = WORKSPACE / "luma-events"
HEALTH_PATH = LUMA_DIR / "health.json"
UPCOMING_PATH = LUMA_DIR / "upcoming_events.json"
TRIGGER_PATH = WORKSPACE / "miami-social-radar-trigger.json"


def load_json(path: Path, default=None):
    if not path.exists():
        return default or {}
    try:
        return json.loads(path.read_text())
    except Exception as e:
        log(f"Failed to load {path.name}: {e}")
        return default or {}

def load_config():
    config_path = LUMA_DIR / "radar-config.json"
    default = {
        "target_user_id": "U0AL1GKMR6J",
        "thread_ts": "1776880604.031709",
        "high_signal_keywords": [
            "claude", "ai", "yr", "young republic", "hack", "notion", "delphica",
            "lab miami", "the dock", "agentic", "founder", "vc", "tech", "miami"
        ],
        "future_days": 7
    }
    cfg = load_json(config_path, default)
    return cfg

def is_high_signal(event: dict, keywords: list) -> bool:
    """Quick filter for events we care about."""
    title = (event.get("title") or "").lower()
    location = (event.get("location") or "").lower()
    desc = (event.get("description") or "").lower()
    return any(k in title or k in desc or k in location for k in keywords)

def trigger_radar(upcoming_events: list | None = None) -> None:
    """Called directly from fetch_events.py.
    The connector now guarantees upcoming_events only contains prospective (next ~7 days) events.
    """
    config = load_config()
    health = load_json(HEALTH_PATH)
    if upcoming_events is None:
        upcoming_events = load_json(UPCOMING_PATH, [])

    errors = health.get("consecutive_failures", 0) > 0 or health.get("last_error")
    # No need to re-filter — connector already did it. Just apply high-signal filter.
    keywords = config.get("high_signal_keywords", [])
    new_events = [e for e in upcoming_events if is_high_signal(e, keywords)][:10]

    trigger_data = {
        "triggered_at": datetime.now().isoformat(),
        "health": health,
        "newEvents": new_events,
        "hasErrors": errors,
        "targetUserId": config.get("user_target", "U0AL1GKMR6J"),
        "requirements": {
            "step1": "Check health.json; if errors, read connector.log, synthesize brief description, message user",
            "step2": "If healthy, run full miami-social radar on the provided newEvents (cross-ref committed-events.md, pm-rhythm grind state, evaluate, flag 1-2 best, headspace/balance suggestion, offer to commit)"
        }
    }

    TRIGGER_PATH.write_text(json.dumps(trigger_data, indent=2, default=str))
    log(f"Trigger file written with {len(new_events)} new high-signal events, errors={errors}")

    # Send the trigger behind the scenes to the agent (not the user).
    # The agent will receive this, create the TaskFlow, run the radar, and then send one clean final response to the user.
    trigger_msg = f"taskflow radar trigger: new miami-social event data ready (see attached trigger file). Create managed TaskFlow with this stateJson and run the requirements above."

    try:
        result = subprocess.run([
            "openclaw", "message", "send",
            "--channel", "slack",
            "--target", config.get("trigger_target", "U0AL7PF0PCJ"),
            "--message", trigger_msg,
            "--path", str(TRIGGER_PATH)
        ], capture_output=True, text=True, timeout=30)

        if result.returncode == 0:
            log("Successfully sent behind-the-scenes trigger to agent")
        else:
            log(f"Send failed: {result.stderr.strip()}")
    except Exception as e:
        log(f"Failed to send trigger: {e}")


def main() -> None:
    trigger_radar()


if __name__ == "__main__":
    main()
