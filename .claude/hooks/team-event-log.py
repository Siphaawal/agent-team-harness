#!/usr/bin/env python3
"""team-event-log.py - TaskCompleted / TeammateIdle hook that only OBSERVES (exit 0, never blocks).

Records the SHAPE of each real payload (field names, event, teammate name, task id) to .claude/state/team-events.log,
so any future gate on these events is keyed on fields that have actually been seen. Never logs free text (task
descriptions may carry sensitive material). Fails open, logged (_hooklib).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _hooklib import append_log, fail_open, now, read_payload  # noqa: E402


def main():
    data = read_payload("team-event-log")
    task = data.get("task") if isinstance(data.get("task"), dict) else {}
    append_log("team-events.log", json.dumps({
        "at": now(),
        "event": data.get("hook_event_name"),
        "keys": sorted(data.keys()),
        "teammate_name": data.get("teammate_name"),
        "agent_id_present": "agent_id" in data,
        "task_id": data.get("task_id") or task.get("id"),
        "session": str(data.get("session_id", ""))[:8],
    }))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        fail_open("team-event-log", exc)
