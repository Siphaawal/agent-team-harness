"""_hooklib.py - what every hook shares: the state dir, failing open WITH a trace, capped logs, safe session ids.

Hooks run on every member's machine on every prompt and tool call, so the rule is: a hook's own failure must never
break the session (exit 0 / allow), but it must never be silent either - a dead gate looks exactly like a quiet one.
Every hook routes its errors through fail_open(), which appends to .claude/state/hook-errors.log, and every
append-only log goes through append_log(), which rotates at LOG_CAP so state/ cannot grow without bound.

stdlib only; importing this module must never raise.
"""
import datetime
import json
import os
import re
import sys

HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HOOKS_DIR))
STATE = os.path.join(REPO, ".claude", "state")
ERRORS = "hook-errors.log"
LOG_CAP = 256 * 1024          # bytes per log before it rotates to <name>.1 (one generation kept)

# State and logs hold session ids, skill names and guard verdicts; on a shared host they are this member's business
# only. Everything a hook creates is owner-only (0700 dir, 0600 files), whatever the member's umask is.
os.umask(0o077)


def tighten_state():
    """An EXISTING state dir that is group/world-accessible (made by an older version, or under a loose umask) is
    narrowed to 0700 - on import, so every hook does it, not only the ones that happen to create the dir."""
    try:
        if os.name != "nt" and os.path.isdir(STATE) and (os.stat(STATE).st_mode & 0o077):
            os.chmod(STATE, 0o700)
    except OSError:
        pass


def ensure_state():
    try:
        os.makedirs(STATE, mode=0o700, exist_ok=True)
    except OSError:
        pass
    tighten_state()


tighten_state()


sys.path.insert(0, os.path.join(REPO, "tools"))
try:
    from common import sid_safe as _common_sid_safe   # one definition of "safe session id" for hooks and tools
except Exception:                                      # tools/ broken or absent: hooks must still run
    _common_sid_safe = None


def sid_safe(sid):
    if _common_sid_safe:
        try:
            return _common_sid_safe(sid)
        except Exception:
            pass
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(sid or "")) or "unknown"


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def append_log(name, line):
    """Append one line to state/<name>, rotating at LOG_CAP. Never raises."""
    try:
        ensure_state()
        p = os.path.join(STATE, name)
        try:
            if os.path.getsize(p) > LOG_CAP:
                os.replace(p, p + ".1")
        except OSError:
            pass
        with open(p, "a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")
    except Exception:
        pass


def fail_open(hook, exc, code=0):
    """Log the hook's own failure, then exit with `code` (0 = allow / say nothing). Use in every top-level except."""
    append_log(ERRORS, "%s %s %s: %r" % (now(), hook, type(exc).__name__, exc)[:600])
    sys.exit(code)


def read_payload(hook):
    """The hook's JSON stdin, or {} on a terminal, empty input or bad JSON (bad JSON is logged, not raised)."""
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    except Exception as e:
        append_log(ERRORS, "%s %s stdin: %r" % (now(), hook, e))
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except ValueError as e:
        append_log(ERRORS, "%s %s bad stdin JSON: %r" % (now(), hook, e)[:300])
        return {}


def is_agent_session(data):
    """A subagent or teammate payload. Gates are for the lead session only."""
    return bool(data.get("agent_id") or data.get("agent_type"))
