"""Event-driven trigger for the miami-social radar.

Called directly from fetch_events.py. Pure function: everything it needs is
passed in, nothing is read from disk. Writes the trigger artifact the agent
reads, then invokes an agent turn via ``openclaw agent`` so the agent can
process the artifact and DM the user — no webhook plumbing, no Slack round
trip.

Earlier revisions of this module went through the Webhooks plugin's
``create_flow``/``run_task`` actions. That path turned out to be a passive
bookkeeping API: it records flow/task entries but cannot actually start
agent work (``sessions_spawn`` is on the gateway HTTP deny list, and
``run_task`` does not reconcile with existing runtime sessions). Use
``openclaw agent`` instead — it runs a real agent turn and the agent's own
tools handle the Slack DM.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path

# Inherits handlers from whichever entrypoint configured logging (e.g.
# fetch_events.py's basicConfig). Keeping a "[radar-trigger]" prefix on
# messages so they stay greppable alongside the connector's own log lines.
log = logging.getLogger(__name__)


def trigger_radar(
    *,
    new_events: list[dict],
    health: dict,
    user_target: str,
    trigger_path: Path,
    agent_id: str,
    timeout_seconds: int = 600,
) -> None:
    """Build the trigger artifact and invoke an agent turn to process it.

    The agent reads the trigger artifact at ``trigger_path`` and follows the
    embedded ``requirements`` block: DM the user with an error summary if
    something failed, otherwise summarize ``newEvents`` and DM with an offer
    to commit.

    Args:
        new_events: events discovered for the first time in this run
            (already filtered to the next ~7 days upstream).
        health: the health dict that was just written for this run.
        user_target: Slack user ID the agent should DM with its final message.
        trigger_path: where to write the JSON trigger artifact the agent reads.
        agent_id: openclaw agent id to invoke. Use ``main`` for the default
            agent; check ``openclaw sessions --all-agents --json`` for others.
        timeout_seconds: max seconds to let the agent turn run before giving
            up. ``openclaw agent``'s own ``--timeout`` flag is honored too;
            we add a small buffer to the subprocess timeout for the CLI's
            startup/teardown.
    """
    if not user_target:
        log.warning("[radar-trigger] user_target must be set — skipping trigger")
        return
    if not agent_id:
        log.warning("[radar-trigger] agent_id must be set — skipping trigger")
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
    log.info(
        "[radar-trigger] Trigger file written with %d new event(s), errors=%s",
        len(new_events), has_errors,
    )

    message = (
        f"New miami-social event data is ready at {trigger_path}. "
        f"Read the JSON there and follow the `requirements` block inside. "
        f"If `hasErrors` is true, prefer the error path; otherwise process "
        f"`newEvents` and DM Slack user {user_target} with the final message."
    )

    cmd = [
        "openclaw", "agent",
        "--agent", agent_id,
        "--message", message,
        "--json",
        "--timeout", str(timeout_seconds),
    ]
    log.info(
        "[radar-trigger] Invoking agent turn (agent=%s, timeout=%ds)",
        agent_id, timeout_seconds,
    )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            # Small buffer past --timeout so the CLI's own teardown has time
            # to finalize without us SIGKILLing it.
            timeout=timeout_seconds + 30,
        )
    except subprocess.TimeoutExpired as exc:
        log.error("[radar-trigger] openclaw agent subprocess timed out after %ds", exc.timeout)
        return
    except FileNotFoundError:
        log.error("[radar-trigger] `openclaw` CLI not found on PATH")
        return

    if result.returncode != 0:
        log.error(
            "[radar-trigger] openclaw agent exited %d. stderr: %s",
            result.returncode,
            result.stderr.strip()[:1000],
        )
        return

    # On success, the JSON body has run/runner metadata that's worth
    # surfacing in the connector log — it lets us tell at a glance whether
    # the turn went through the Gateway or fell back to embedded, and how
    # many tool calls the agent made.
    try:
        body = json.loads(result.stdout)
        meta = (body.get("result") or {}).get("meta") or {}
        runner = (meta.get("executionTrace") or {}).get("runner") or "?"
        tool_summary = meta.get("toolSummary") or {}
        log.info(
            "[radar-trigger] Agent turn complete (runner=%s, runId=%s, tools=%d, failures=%d, durationMs=%s)",
            runner,
            body.get("runId") or "?",
            tool_summary.get("calls", 0),
            tool_summary.get("failures", 0),
            meta.get("durationMs", "?"),
        )
    except ValueError:
        log.info(
            "[radar-trigger] Agent turn complete (non-JSON stdout: %s)",
            result.stdout.strip()[:500],
        )
