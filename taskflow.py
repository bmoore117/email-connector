"""Event-driven TaskFlow trigger for the miami-social radar.

Called directly from fetch_events.py. Pure function: everything it needs is
passed in, nothing is read from disk. Writes the trigger artifact the agent
reads and POSTs to the Gateway's Webhooks plugin to create a managed
TaskFlow plus the subagent task that actually runs it — no Slack
round-trip needed.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import requests

# Inherits handlers from whichever entrypoint configured logging (e.g.
# fetch_events.py's basicConfig). Keeping a "[radar-trigger]" prefix on
# messages so they stay greppable alongside the connector's own log lines.
log = logging.getLogger(__name__)


def _post_webhook(url: str, secret: str, payload: dict) -> dict | None:
    """POST one webhook action and return the parsed body on a 2xx + ok=true.

    Returns ``None`` on transport, parse, or application-level failure, after
    logging the failure. The caller is expected to short-circuit if this
    returns ``None``.
    """
    action = payload.get("action", "?")
    try:
        resp = requests.post(
            url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {secret}",
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        log.error("[radar-trigger] Webhook POST failed (action=%s): %s", action, exc)
        return None

    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text[:500]}

    if resp.ok and body.get("ok"):
        return body

    log.error(
        "[radar-trigger] Webhook rejected %s: status=%s code=%s error=%s",
        action,
        resp.status_code,
        body.get("code"),
        body.get("error") or body,
    )
    return None


def trigger_radar(
    *,
    new_events: list[dict],
    health: dict,
    user_target: str,
    trigger_path: Path,
    webhook_url: str,
    webhook_secret: str,
    child_session_key: str,
) -> None:
    """Build the trigger artifact and create + run a managed TaskFlow.

    Two-step protocol against the Webhooks plugin:
      1. ``create_flow`` registers an empty managed TaskFlow shell.
      2. ``run_task`` attaches a subagent child task to that flow, which is
         what actually causes work to execute. Without step 2 the flow sits
         in ``queued`` forever with zero tasks.

    On failure of step 2 we deliberately leave the orphaned ``queued`` flow
    in place so it is visible via ``openclaw tasks flow list`` for debugging
    — that confirms create_flow succeeded and isolates the run_task issue.
    Periodically prune with ``openclaw tasks flow cancel <id>`` or
    ``openclaw tasks maintenance --apply``.

    Args:
        new_events: events discovered for the first time in this run
            (already filtered to the next ~7 days upstream).
        health: the health dict that was just written for this run.
        user_target: Slack user ID the agent should DM with its final message.
        trigger_path: where to write the JSON trigger artifact the agent reads.
        webhook_url: full URL to the Gateway webhooks route, e.g.
            ``http://localhost:18789/plugins/webhooks/miami-social-radar``.
        webhook_secret: shared secret configured on the webhook route.
        child_session_key: subagent session key the worker runs under, e.g.
            ``agent:main:subagent:miami-social-radar``.
    """
    if not user_target:
        log.warning("[radar-trigger] user_target must be set — skipping trigger")
        return
    if not webhook_url or not webhook_secret:
        log.warning("[radar-trigger] webhook_url and webhook_secret must both be set — skipping trigger")
        return
    if not child_session_key:
        log.warning("[radar-trigger] child_session_key must be set — skipping trigger")
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
                "from health.last_error and DM the user. If hasErrors is false, continue to step 2."
            ),
            "step2": (
                "If healthy, read through newEvents, cross-reference committed-events.md from the miami-social skill, "
                "the grind state from the pm-rhythm skill and any recent conversations with the user, "
                "flag the 1-2 best events that are not already in the committed-events.md file, "
                "and DM the user to highlight the opportunities."
            ),
        },
    }

    trigger_path.parent.mkdir(parents=True, exist_ok=True)
    trigger_path.write_text(json.dumps(trigger_data, indent=2, default=str))
    log.info("[radar-trigger] Trigger file written with %d new event(s), errors=%s", len(new_events), has_errors)

    # Step 1: create_flow. The flow's `goal` is a short summary that shows up
    # in `openclaw tasks flow list` — it's not the prompt the agent receives.
    goal = (
        f"miami-social radar: {len(new_events)} new event(s)"
        f"{' (with errors)' if has_errors else ''}"
    )
    created = _post_webhook(webhook_url, webhook_secret, {
        "action": "create_flow",
        "goal": goal,
        "status": "queued",
    })
    if not created:
        return

    # Response shape is `{ result: { flow: { flowId, ... } } }`. Fall back to a
    # couple of plausible alternates so a future webhook revision that flattens
    # this doesn't silently break us.
    result = created.get("result") or {}
    flow = result.get("flow") or {}
    flow_id = flow.get("flowId")
    if not flow_id:
        log.error("[radar-trigger] create_flow ok but no flowId in result: %s", created)
        return

    log.info("[radar-trigger] Created TaskFlow %s — attaching subagent task", flow_id)

    # Step 2: run_task. The `task` text IS the prompt the subagent receives,
    # so it has to be the full instruction pointing at the trigger artifact.
    task_text = (
        f"New miami-social event data is ready at {trigger_path}. "
        f"Read the JSON there and follow the `requirements` block inside. "
        f"If `hasErrors` is true, prefer the error path; otherwise process "
        f"`newEvents` and DM Slack user {user_target} with the final message."
    )
    ran = _post_webhook(webhook_url, webhook_secret, {
        "action": "run_task",
        "flowId": flow_id,
        "runtime": "subagent",
        "childSessionKey": child_session_key,
        "task": task_text,
    })
    if ran:
        log.info("[radar-trigger] run_task accepted on flow %s (session=%s)", flow_id, child_session_key)
    else:
        log.error(
            "[radar-trigger] run_task rejected on flow %s — leaving queued flow in place for inspection. "
            "Use `openclaw tasks flow show %s` to debug, then `openclaw tasks flow cancel %s` to clean up.",
            flow_id, flow_id, flow_id,
        )
