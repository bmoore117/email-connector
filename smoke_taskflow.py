"""Smoke test for the taskflow.py agent invocation path.

Run from the same directory as fetch_events.py / taskflow.py:

    python3 smoke_taskflow.py

Requires the same .env as the connector (TASKFLOW_USER_TARGET at minimum;
TASKFLOW_AGENT_ID falls back to ``main`` if unset).

The smoke deliberately forces the error-path requirements block in the
trigger artifact so the agent has explicit, deterministic instructions to
DM the configured user with ``SMOKE-OK``. A successful run produces:

  * connector log lines from taskflow (look for ``[radar-trigger]``)
  * a Slack DM containing exactly ``SMOKE-OK``
  * raw stdout/stderr from ``openclaw agent`` printed below

If the DM doesn't arrive but the subprocess exited 0, the agent decided
not to send — check the captured stdout/stderr for clues.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path


def _load_env_file(path: Path) -> None:
    """Minimal .env loader so the smoke runs without python-dotenv installed.

    Handles ``KEY=VALUE`` lines, optional surrounding quotes, ``#`` comments,
    blank lines, and CRLF line endings. Does not handle multi-line values or
    variable interpolation, which is fine for the connector's .env shape.
    """
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().rstrip("\r")
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _patch_subprocess_for_visibility() -> None:
    """Monkey-patch subprocess.run inside taskflow so we can see the raw
    openclaw agent stdout/stderr without changing production code.

    Production taskflow.py logs a one-line summary of the agent response;
    that's deliberate so the connector log stays compact. For smoke tests
    we want the full payload.
    """
    import taskflow

    original_run = taskflow.subprocess.run

    def wrapped(*args, **kwargs):
        result = original_run(*args, **kwargs)
        print("\n--- openclaw agent stdout ---")
        print(result.stdout or "(empty)")
        print("\n--- openclaw agent stderr ---")
        print(result.stderr or "(empty)")
        print(f"--- exit: {result.returncode} ---\n")
        return result

    taskflow.subprocess.run = wrapped  # type: ignore[assignment]


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    _load_env_file(Path(".env"))

    user_target = os.environ.get("TASKFLOW_USER_TARGET", "").strip()
    if not user_target:
        print("error: TASKFLOW_USER_TARGET is not set in .env", file=sys.stderr)
        return 2

    agent_id = os.environ.get("TASKFLOW_AGENT_ID", "main").strip() or "main"

    _patch_subprocess_for_visibility()

    # Importing after the patch so the logger config is already in place.
    from taskflow import trigger_radar

    # Force the error path with an unambiguous instruction. This avoids the
    # "agent saw nothing newsworthy and stayed silent" failure mode that an
    # empty-events / healthy smoke would otherwise produce.
    trigger_radar(
        new_events=[],
        health={
            "consecutive_failures": 1,
            "last_error": (
                "smoke test from gateway shell — please DM the user with "
                "the exact text SMOKE-OK and nothing else"
            ),
            "last_run": "2026-05-03",
        },
        user_target=user_target,
        trigger_path=Path("/tmp/smoke-trigger.json"),
        agent_id=agent_id,
        timeout_seconds=180,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
