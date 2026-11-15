"""_teamlib.py - what the three team hooks share: where Claude Code keeps an agent team's task list and teammate
transcripts, which teammates count as active, which were stopped, and the watchdog heartbeat.

Locations (learned from real payloads and from what Claude Code writes on disk; every one is overridable by env so tests
never touch a real home directory, and every reader treats a missing location as "nothing there" - fail open):
  claude home       TEAM_CLAUDE_HOME, else CLAUDE_CONFIG_DIR, else ~/.claude
  task list         TEAM_TASKS_DIR, else <claude home>/tasks/<team name>/<id>.json  ({id, subject, owner, status, ...})
                    team name = the hook payload's team_name, else the teamName in a teammate's .meta.json, else
                    "session-" + the first 8 characters of the session id
  transcripts       TEAM_PROJECTS_DIR, else <claude home>/projects; a teammate writes
                    <projects>/<project slug>/<session id>/subagents/agent-<id>.jsonl next to agent-<id>.meta.json
                    ({"name": ..., "teamName": ..., "finished": true for a subagent that has returned})

Only transcripts whose .meta.json carries a "name" are team members here: an unnamed background subagent re-invokes the
lead by itself when it returns, so nobody has to watch it.

stdlib only, pure ASCII, importing it never raises.
"""
import glob
import json
import os
import re
import time

import _hooklib as H

DONE = ("completed", "deleted")
STOP_GRACE_S = 15.0        # a stopped teammate's transcript may get one last write just after the stop
LIVE_S = 60.0              # a watchdog heartbeat younger than this means a watchdog is running
# A teammate whose transcript changed within this many minutes is "active": the stall gate blocks the lead's Stop for
# it, and the watchdog always takes it into its watch set (even with a smaller --minutes), so a gate block can never
# be answered by a watchdog that exits QUIET at once.
ACTIVE_MINUTES = 5.0
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")     # fullmatch: `$` would accept "name\n"


def claude_home():
    return (os.environ.get("TEAM_CLAUDE_HOME") or os.environ.get("CLAUDE_CONFIG_DIR")
            or os.path.join(os.path.expanduser("~"), ".claude"))


def projects_root():
    return os.environ.get("TEAM_PROJECTS_DIR") or os.path.join(claude_home(), "projects")


def sid_clean(sid):
    return "".join(c for c in str(sid or "") if (c.isascii() and c.isalnum()) or c in "-_")


def safe_name(name):
    """A team or teammate name usable as ONE path component, or None (a payload value is not trusted as a path)."""
    name = str(name or "")
    return name if SAFE_NAME.fullmatch(name) and name not in (".", "..") else None


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def write_json(path, obj):
    """Atomic: a unique temp file, then os.replace, so a reader never sees half a file."""
    H.ensure_state()
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------------------------- transcripts

def subagent_dirs(sid, transcript_path=None):
    s = sid_clean(sid)
    if not s:
        return []
    dirs = []
    if transcript_path:                               # the lead's own transcript: <projects>/<slug>/<sid>.jsonl
        d = os.path.join(os.path.dirname(str(transcript_path)), s, "subagents")
        root = os.path.realpath(projects_root())
        # only inside the projects dir: a path flag must not turn the watchdog into a reader of any directory
        if os.path.realpath(d).startswith(root + os.sep) and os.path.isdir(d):
            dirs.append(d)
    for d in glob.glob(os.path.join(glob.escape(projects_root()), "*", s, "subagents")):
        if d not in dirs:
            dirs.append(d)
    return dirs


def mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def members(sid, transcript_path=None):
    """{name: {"path", "id", "mtime", "finished", "team"}} for every NAMED teammate transcript of the session. A name used
    twice (a respawn) maps to its newest transcript."""
    out = {}
    for d in subagent_dirs(sid, transcript_path):
        for p in glob.glob(os.path.join(glob.escape(d), "*.jsonl")):
            meta = _read_json(p[:-6] + ".meta.json", {})
            if not isinstance(meta, dict):
                continue
            name = str(meta.get("name") or "").strip()
            if not name:
                continue
            base = os.path.basename(p)[:-6]
            rec = {"path": p, "id": base[6:] if base.startswith("agent-") else base, "mtime": mtime(p),
                   "finished": meta.get("finished") is True, "team": meta.get("teamName")}
            if name not in out or rec["mtime"] > out[name]["mtime"]:
                out[name] = rec
    return out


def team_name(sid, payload_team=None, transcript_path=None):
    t = safe_name(payload_team)
    if t:
        return t
    for rec in members(sid, transcript_path).values():
        t = safe_name(rec.get("team"))
        if t:
            return t
    return "session-" + sid_clean(sid)[:8]


# ---------------------------------------------------------------------------------------------- tasks

def tasks_dir(sid, payload_team=None, transcript_path=None):
    return os.environ.get("TEAM_TASKS_DIR") or os.path.join(claude_home(), "tasks",
                                                            team_name(sid, payload_team, transcript_path))


def load_tasks(sid, payload_team=None, transcript_path=None):
    out = []
    for p in glob.glob(os.path.join(glob.escape(tasks_dir(sid, payload_team, transcript_path)), "*.json")):
        t = _read_json(p, None)
        if isinstance(t, dict):
            out.append(t)
    return out


def open_task_ids(tasks, owner):
    ids = [str(t.get("id")) for t in tasks if t.get("owner") == owner and t.get("status") == "in_progress"]
    return sorted(ids, key=lambda s: (len(s), s))


def finished_owners(tasks):
    """Names that own at least one task, every one of them completed (or deleted): nothing left to report."""
    owned = {}
    for t in tasks:
        if t.get("owner"):
            owned.setdefault(str(t["owner"]), []).append(t.get("status"))
    return {n for n, sts in owned.items() if all(s in DONE for s in sts)}


# ---------------------------------------------------------------------------------------------- stopped teammates

def stopped_path(sid):
    return os.path.join(H.STATE, "team-stopped-" + H.sid_safe(sid) + ".json")


def record_stop(sid, key, at=None):
    stops = stopped(sid)
    stops[str(key)] = float(at if at is not None else time.time())
    write_json(stopped_path(sid), stops)


def stopped(sid):
    d = _read_json(stopped_path(sid), {})
    return d if isinstance(d, dict) else {}


def is_stopped(name, rec, stops):
    """Stopped by TaskStop (by name or agent id) and silent since: a teammate woken again by a message writes to its
    transcript after the stop and counts as live again."""
    for key in (name, rec.get("id")):
        at = stops.get(key)
        if isinstance(at, (int, float)) and rec.get("mtime", 0) <= at + STOP_GRACE_S:
            return True
    return False


def watchable(sid, transcript_path=None, payload_team=None):
    """(members, names that can still owe the lead something): not a returned subagent, not stopped, and not a teammate
    whose tasks are all completed."""
    mem = members(sid, transcript_path)
    stops = stopped(sid)
    done = finished_owners(load_tasks(sid, payload_team, transcript_path))
    names = {n for n, r in mem.items() if not r["finished"] and n not in done and not is_stopped(n, r, stops)}
    return mem, names


def active(sid, minutes, transcript_path=None, now=None):
    now = now or time.time()
    mem, names = watchable(sid, transcript_path)
    return sorted(n for n in names if now - mem[n]["mtime"] < minutes * 60)


# ---------------------------------------------------------------------------------------------- watchdog heartbeat

def heartbeat_path(sid):
    return os.path.join(H.STATE, "watchdog-" + H.sid_safe(sid) + ".json")


def watchdog_alive(sid):
    return time.time() - mtime(heartbeat_path(sid)) < LIVE_S
