#!/usr/bin/env python3
"""subagent-core.py - SubagentStart hook: inject the shared agent core into every spawned subagent and teammate.

Source of truth: agents-src/CORE-AGENTS.md (edit it, never this script's output). Emits
hookSpecificOutput.additionalContext (SubagentStart is a context-only event). Logs every firing with the agent
type/id to .claude/state/subagent-start.log - silent success and silent failure look identical otherwise. Never
blocks a spawn: any error -> exit 0 with no output, and the error is logged (_hooklib).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _hooklib import REPO, append_log, fail_open, now, read_payload  # noqa: E402

CORE = os.path.join(REPO, "agents-src", "CORE-AGENTS.md")
MAX_BYTES = 6000   # keep the core near 3 KB; this is the hard stop


def main():
    data = read_payload("subagent-core")
    who = "type=%s id=%s" % (data.get("agent_type", "?"), data.get("agent_id", "?"))
    with open(CORE, encoding="utf-8") as f:
        core = f.read()
    raw = core.encode("utf-8")
    if len(raw) > MAX_BYTES:
        core = raw[:MAX_BYTES].decode("utf-8", "ignore") + "\n[agent core TRUNCATED at %d bytes]" % MAX_BYTES
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": core}}))
    append_log("subagent-start.log", "%s injected %d bytes %s" % (now(), len(core.encode("utf-8")), who))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        fail_open("subagent-core", exc)
