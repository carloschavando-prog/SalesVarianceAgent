"""
Best-effort reporter to the Agent Control Board.

Sends an agent's lifecycle events (started / finished / failed / heartbeat /
output) to the control board's /api/report endpoint.

Design rules:
  * No-ops silently unless CONTROL_BOARD_URL is set.
  * NEVER raises — monitoring must never break the agent it monitors.
  * Pure standard library (urllib), no extra dependencies.

Env vars:
  CONTROL_BOARD_URL     e.g. https://agent-control-board.vercel.app
  CONTROL_BOARD_SECRET  must match REPORT_SECRET on the control board (optional)
"""

import os
import json
import urllib.request


def report(agent_key, event, message=None, output=None, current_task=None, next_run=None):
    base = os.environ.get("CONTROL_BOARD_URL")
    if not base:
        return  # not configured — do nothing

    payload = {"agent_key": agent_key, "event": event}
    if message:
        payload["message"] = message
    if output:
        payload["output"] = output
    if current_task:
        payload["current_task"] = current_task
    if next_run:
        payload["next_run"] = next_run

    try:
        req = urllib.request.Request(
            base.rstrip("/") + "/api/report",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "x-report-secret": os.environ.get("CONTROL_BOARD_SECRET", ""),
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass  # best-effort only
