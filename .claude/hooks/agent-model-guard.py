#!/usr/bin/env python3
"""agent-model-guard.py - PreToolUse hook on Agent: no teammate or subagent spawns without a deliberate model choice.

Why: a staffing policy written in a skill is applied only when someone remembers it; a spawn without `model` silently
inherits the lead's model. This makes the choice explicit on every spawn.

Contract for every Agent call except the exempt built-ins below:
  - `name` set (unnamed agents are hard to tell apart on the team graph)
  - `model` set explicitly
  - the prompt carries one line   MODEL-BY-JOB: <job> -> <model>   naming a job class from POLICY; the model on that
    line equals the `model` parameter and is allowed for that job.
Blocks (exit 2, reason on stderr) on any miss; logs every spawn to .claude/state/agent-spawns.log. Fails OPEN on bad
input. The team edits POLICY to match the models it has access to (see the team-lead skill, section 1).
"""
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _hooklib import append_log, fail_open, read_payload  # noqa: E402

EXEMPT = {"memory-librarian", "explore", "claude-code-guide", "statusline-setup", "fork", "plan"}
# job class -> allowed model aliases. A verifier is never weaker than the author it checks.
POLICY = {
    "red-team": {"opus"},                 # attacks a conclusion; strongest model available
    "verifier": {"opus"},                 # independent pass B / pass C / release check
    "live-tool-builder": {"opus"},        # drives a running shared tool or production (read-mostly)
    "risky-builder": {"opus"},            # data rewrites, frontmatter, hooks, generators of shipped data
    "modeller": {"opus"},                 # analysis / proposal author
    "auditor": {"opus"},                  # judgement over many measures
    "builder": {"sonnet", "opus"},        # contained code, UI, scripts with tests
    "counting": {"sonnet", "haiku"},      # mechanical recount, file matching, doc checks
    "lookup": {"haiku", "sonnet"},        # trivially bounded fetch
}
LINE = re.compile(r"MODEL-BY-JOB:\s*([a-z-]+)\s*->\s*([a-z0-9.-]+)", re.I)


def table():
    return "\n".join("  %-18s -> %s" % (job, " | ".join(sorted(m))) for job, m in POLICY.items())


def block(msg):
    sys.stderr.write("BLOCKED by agent-model-guard: " + msg + "\nAllowed (team-lead skill section 1):\n" + table() +
                     "\nAdd one prompt line 'MODEL-BY-JOB: <job> -> <model>' and pass the same `model` and a `name`.\n")
    sys.exit(2)


def log(row):
    append_log("agent-spawns.log", json.dumps(row))


def main():
    data = read_payload("agent-model-guard")                     # bad stdin is logged and reads as {} (allow)
    if data.get("tool_name") != "Agent":
        sys.exit(0)
    ti = data.get("tool_input") or {}
    stype = (ti.get("subagent_type") or "").strip().lower()
    name, model, prompt = ti.get("name"), (ti.get("model") or "").strip().lower(), ti.get("prompt") or ""
    m = LINE.search(prompt)
    row = {"at": datetime.datetime.now().isoformat(timespec="seconds"), "type": stype or "general-purpose",
           "name": name, "model": model or None, "job": m.group(1).lower() if m else None,
           "session": str(data.get("session_id", ""))[:8]}
    if stype in EXEMPT:
        log(dict(row, exempt=True))
        sys.exit(0)
    if not name:
        log(dict(row, blocked="no name"))
        block("the spawn has no `name`.")
    if not model:
        log(dict(row, blocked="no model"))
        block("`%s` has no explicit `model` - it would silently inherit the lead's." % name)
    if not m:
        log(dict(row, blocked="no MODEL-BY-JOB line"))
        block("`%s`'s prompt has no 'MODEL-BY-JOB: <job> -> <model>' line - state the job class." % name)
    job, said = m.group(1).lower(), m.group(2).lower()
    if job not in POLICY:
        log(dict(row, blocked="unknown job"))
        block("`%s`: job class '%s' is not in the policy." % (name, job))
    if said != model:
        log(dict(row, blocked="line != param"))
        block("`%s`: the prompt says %s but the `model` parameter is %s." % (name, said, model))
    if model not in POLICY[job]:
        log(dict(row, blocked="model not allowed"))
        block("`%s`: %s is not an allowed model for a %s." % (name, model, job))
    log(dict(row, ok=True))
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        fail_open("agent-model-guard", exc)
