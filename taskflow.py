"""Event-driven TaskFlow trigger for the miami-social radar.

Called directly from fetch_events.py. Pure function: everything it needs is
passed in, nothing is read from disk. Writes the trigger artifact the agent
reads and POSTs to the Gateway's Webhooks plugin to create a managed
TaskFlow — no Slack round-trip needed.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import requests


def _log(msg: str) -> None:
    timestamp = datetime.now().isoformat()
    print(f"{timestamp} [radar-trigger] {msg}", file=sys.stderr)


def trigger_radar(
    *,
    new_events: list[dict],
    health: dict,
    user_target: str,
    trigger_path: Path,
    webhook_url: str,
    webhook_secret: str,
) -> None:
    """Build the trigger artifact and create a managed TaskFlow via webhook.

    Args:
        new_events: events discovered for the first time in this run
            (already filtered to the next ~7 days upstream).
        health: the health dict that was just written for this run.
        user_target: Slack user ID the agent should DM with its final message.
        trigger_path: where to write the JSON trigger artifact the agent reads.
        webhook_url: full URL to the Gateway webhooks route, e.g.
            ``http://localhost:18790/plugins/webhooks/miami-social-radar``.
        webhook_secret: shared secret configured on the webhook route.
    """
    if not user_target:
        _log("user_target must be set — skipping trigger")
        return
    if not webhook_url or not webhook_secret:
        _log("webhook_url and webhook_secret must both be set — skipping trigger")
        return

    has_errors = bool(health.get("consecutive_failures", 0)) or bool(health.get("last_error"))

    trigger_data = {
        "triggered_at": datetime.now().isoformat(),
        "health": health,
        "newEvents": new_events,
        "hasErrors": has_errors,
        "targetUserId": user_target,
        "requirements": {
            "step1": (
                "If hasErrors, synthesize a brief human-readable description "
                "from health.last_error and DM the user."
            ),
            "step2": (
                "If healthy, read through newEvents yourself (no pre-filter), "
                "cross-reference committed-events.md and pm-rhythm grind state, "
                "flag the 1-2 best, include a headspace/balance suggestion, "
                "and DM the user with an offer to commit."
            ),
        },
    }

    trigger_path.parent.mkdir(parents=True, exist_ok=True)
    trigger_path.write_text(json.dumps(trigger_data, indent=2, default=str))
    _log(f"Trigger file written with {len(new_events)} new event(s), errors={has_errors}")

    # The webhook's create_flow only accepts a string `goal`, so pass a short
    # natural-language instruction that points the agent at the artifact.
    goal = (
        f"New miami-social event data is ready at {trigger_path}. "
        f"Read the JSON there and follow the requirements block inside. "
        f"DM user {user_target} with the final message."
    )

    payload = {
        "action": "create_flow",
        "goal": goal,
        "status": "queued",
    }

    try:
        resp = requests.post(
            webhook_url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {webhook_secret}",
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        _log(f"Webhook POST failed: {exc}")
        return

    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text[:500]}

    if resp.ok and body.get("ok"):
        _log(f"Created TaskFlow via webhook (status {resp.status_code})")
    else:
        _log(
            f"Webhook rejected request: status={resp.status_code} "
            f"code={body.get('code')} error={body.get('error') or body}"
        )
