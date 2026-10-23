#!/usr/bin/env python3
"""librarian-gate.py - harness-enforced "dispatch the memory-librarian" rule.

Why: a retrieval subagent that nothing forces gets used once and then forgotten, and the session works from tier 0
alone while the team's memory sits unread. This hook nags on EVERY prompt until the librarian has been dispatched in
the current session (or the gate is waived with an auditable reason), and blocks the end of a turn once.

Modes (first argv):
  mark   PostToolUse, matcher "Agent": when tool_input.subagent_type == "memory-librarian", write the session marker.
  check  UserPromptSubmit: no marker -> additionalContext telling the agent to dispatch the librarian first.
  stop   Stop: no marker and not a re-entry -> decision=block, unless the lead made fewer than AUTO_WAIVE_UNDER_CALLS
         tool calls (a trivial session; waived for this turn only and logged).
  waive  python3 .claude/hooks/librarian-gate.py waive <session_id> "<reason>"   (logged to state/waivers.log)
  status python3 .claude/hooks/librarian-gate.py status <session_id>

Teammate and subagent sessions (payload carries agent_id / agent_type) are never nagged or blocked.
State: .claude/state/librarian-<session_id>, pruned after 14 days. Errors fail OPEN and are logged (_hooklib).
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _hooklib import STATE, append_log, fail_open, is_agent_session, now, read_payload, sid_safe  # noqa: E402

PRUNE_AFTER_SECONDS = 14 * 24 * 3600
AUTO_WAIVE_UNDER_CALLS = 5
WAIVE_CMD = 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/librarian-gate.py" waive'

NAG = (
    "LIBRARIAN GATE (harness hook, not a memory note): the memory-librarian subagent has NOT been dispatched in this "
    "session. Tier 0 carries identity and an index, not the team's memory. As soon as the session goal is known from "
    "this or an earlier prompt, dispatch it BEFORE other work: Agent(subagent_type=\"memory-librarian\", "
    "prompt=<session goal + what to sweep for>). This reminder repeats on every prompt until the dispatch is observed. "
    "If the session is genuinely trivial (a one-line question, no repo work), waive it with a reason: "
    + WAIVE_CMD + " <session_id> \"<reason>\""
)


def marker_path(session_id):
    return os.path.join(STATE, "librarian-" + sid_safe(session_id))


def prune_old_markers():
    cutoff = time.time() - PRUNE_AFTER_SECONDS
    try:
        for name in os.listdir(STATE):
            path = os.path.join(STATE, name)
            if name.startswith("librarian-") and os.path.getmtime(path) < cutoff:
                os.remove(path)
    except OSError:
        pass


def write_marker(session_id, text):
    os.makedirs(STATE, exist_ok=True)
    with open(marker_path(session_id), "w", encoding="utf-8") as fh:
        fh.write(text + "\n")


def lead_calls(session_id):
    """The lead's tool-call count from skills-gate.py's state (same sid_safe, so the same file); None when unknown."""
    try:
        with open(os.path.join(STATE, "skills-" + sid_safe(session_id) + ".json"), "r", encoding="utf-8") as fh:
            return int(json.load(fh).get("calls", 0))
    except Exception:
        return None


def cmd_mark():
    data = read_payload("librarian-gate")
    ti = data.get("tool_input") or {}
    if data.get("tool_name") == "Agent" and ti.get("subagent_type") == "memory-librarian":
        write_marker(data.get("session_id", "unknown"), "dispatched " + now())
        print(json.dumps({"systemMessage": "Librarian gate satisfied for this session."}))
    return 0


def cmd_check():
    data = read_payload("librarian-gate")
    if is_agent_session(data):
        return 0
    sid = data.get("session_id", "unknown")
    prune_old_markers()
    if os.path.exists(marker_path(sid)):
        return 0
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                             "additionalContext": NAG + " (session_id=" + str(sid) + ")"}}))
    return 0


def cmd_stop():
    data = read_payload("librarian-gate")
    if is_agent_session(data) or data.get("stop_hook_active"):
        return 0
    sid = data.get("session_id", "unknown")
    if os.path.exists(marker_path(sid)):
        return 0
    calls = lead_calls(sid)
    if calls is not None and calls < AUTO_WAIVE_UNDER_CALLS:
        flag = marker_path(sid) + ".autowaive-logged"
        if not os.path.exists(flag):
            append_log("waivers.log", "%s %s auto: turn waived (%d tool calls; re-arms at %d)"
                       % (now(), sid, calls, AUTO_WAIVE_UNDER_CALLS))
            os.makedirs(STATE, exist_ok=True)
            open(flag, "w").close()
        return 0
    print(json.dumps({"decision": "block", "reason": (
        "LIBRARIAN GATE: this turn cannot end - the memory-librarian has not been dispatched this session. Dispatch it "
        "now (Agent subagent_type=memory-librarian with the session goal), or if this session is genuinely trivial run: "
        + WAIVE_CMD + " " + str(sid) + " \"<reason>\" - then finish.")}))
    return 0


def cmd_waive(argv):
    if len(argv) < 4:
        sys.stderr.write("usage: librarian-gate.py waive <session_id> \"<reason>\"\n")
        return 64
    sid, reason = argv[2], " ".join(argv[3:])
    write_marker(sid, "waived " + now() + " " + reason)
    append_log("waivers.log", "%s %s %s" % (now(), sid, reason))
    print("waived " + sid + ": " + reason)
    return 0


def cmd_status(argv):
    if len(argv) < 3:
        sys.stderr.write("usage: librarian-gate.py status <session_id>\n")
        return 64
    path = marker_path(argv[2])
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            print("marker: " + fh.read().strip())
        return 0
    print("no marker - gate OPEN (nag active)")
    return 1


MODES = {"mark": lambda a: cmd_mark(), "check": lambda a: cmd_check(), "stop": lambda a: cmd_stop(),
         "waive": cmd_waive, "status": cmd_status}


def main(argv):
    mode = argv[1] if len(argv) > 1 else ""
    if mode not in MODES:
        sys.stderr.write("usage: librarian-gate.py mark|check|stop|waive|status\n")
        return 64
    return MODES[mode](argv)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except SystemExit:
        raise
    except Exception as exc:
        fail_open("librarian-gate", exc)
