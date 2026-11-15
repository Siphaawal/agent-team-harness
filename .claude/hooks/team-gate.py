#!/usr/bin/env python3
"""team-gate.py - harness-enforced "parallel work gets named teammates" rule.

Why: with agent teams switched on, a lead still tends to build multi-part work alone, one file after another, and a rule
in prose does not change that habit. The trigger here is a fact the hook can see - the lead has made N tool calls and
spawned no named teammate - so it asks, and the answer is either a spawn or an audited waiver saying why the work is
serial. It cannot know whether the work IS serial; the waiver is where the lead says so.

Modes (first argv):
  mark   PostToolUse, matcher "Agent". A spawn with a `name` and a subagent_type that is not a read-only helper
         satisfies the gate for the session. A spawn replaces a waiver (a waiver can expire, a spawn cannot).
  nag    PostToolUse, no matcher. Past NAG_AFTER_CALLS lead calls with no teammate, adds context every NAG_EVERY calls.
         Under a waiver it also counts the project files the lead writes: at WAIVER_BUILD_FILES distinct files the
         waiver EXPIRES (the work has become a build) and the nag fires at once.
  stop   Stop. Blocks ONCE past BLOCK_AFTER_CALLS lead calls with no teammate and no live waiver. Re-entry safe.
  waive  python3 .claude/hooks/team-gate.py waive <session_id> "<why the work is serial>"   (state/waivers.log)
  status python3 .claude/hooks/team-gate.py status <session_id>     exit 0 satisfied, 1 gate open
  test   self-test against a temporary state dir

Lead call count: skills-gate.py's state (skills-<session_id>.json "calls"; it counts the lead only). Files that do not
make a build: anything outside the project, and memory/, .claude/state/, .claude/agent-memory/ inside it.
Teammate and subagent sessions are never nagged or blocked. State: .claude/state/team-<session_id>[.json].
Errors fail OPEN and are logged (_hooklib).
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _hooklib as H  # noqa: E402

NAG_AFTER_CALLS = 20
NAG_EVERY = 10
BLOCK_AFTER_CALLS = 40
WAIVER_BUILD_FILES = 3
HELPER_TYPES = {"memory-librarian", "Explore", "Plan", "claude-code-guide", "statusline-setup", "fork"}
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
NOT_BUILD_DIRS = ("memory", os.path.join(".claude", "state"), os.path.join(".claude", "agent-memory"))
WAIVE_CMD = 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/team-gate.py" waive'

NAG = (
    "TEAM GATE (harness hook, not a memory note): %d tool calls this session and ZERO named teammates spawned. Where "
    "the work does not have to be serial, it gets teammates. Name the independent pieces of this task (different files, "
    "build vs verify, research vs review) and spawn named teammates for them now with the team-lead skill (model by "
    "job, owned files per teammate); every build also gets a reviewer. If the work is genuinely serial, say why in one "
    "line and run: " + WAIVE_CMD + " %s \"<why it is serial>\""
)
EXPIRED = ("TEAM GATE: your waiver has EXPIRED - you have written %d project files since it, so this is now a build, "
           "not the serial work the waiver described. ")


def marker_path(sid):
    return os.path.join(H.STATE, "team-" + H.sid_safe(sid))


def state_path(sid):
    return marker_path(sid) + ".json"


def read_marker(sid):
    try:
        with open(marker_path(sid), "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def write_marker(sid, text):
    H.ensure_state()
    with open(marker_path(sid), "w", encoding="utf-8") as fh:
        fh.write(text + "\n")


def load_state(sid):
    try:
        with open(state_path(sid), "r", encoding="utf-8") as fh:
            st = json.load(fh)
        if isinstance(st, dict):
            return st
    except Exception:
        pass
    return {}


def save_state(sid, st):
    H.ensure_state()
    tmp = "%s.%d.tmp" % (state_path(sid), os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh)
    os.replace(tmp, state_path(sid))


def lead_calls(sid):
    try:
        with open(os.path.join(H.STATE, "skills-" + H.sid_safe(sid) + ".json"), "r", encoding="utf-8") as fh:
            return int(json.load(fh).get("calls", 0))
    except Exception:
        return None


def is_teammate_spawn(data):
    if data.get("tool_name") != "Agent":
        return False
    ti = data.get("tool_input") or {}
    name = str(ti.get("name") or "").strip()
    return bool(name) and str(ti.get("subagent_type") or "general-purpose") not in HELPER_TYPES


def build_file(data):
    """The project file a lead Write/Edit touched, normalised; None when it does not count as building."""
    if data.get("tool_name") not in WRITE_TOOLS:
        return None
    ti = data.get("tool_input") or {}
    raw = str(ti.get("file_path") or ti.get("notebook_path") or "").strip()
    if not raw:
        return None
    root = os.path.normcase(os.path.realpath(H.REPO))
    path = os.path.normcase(os.path.realpath(os.path.join(data.get("cwd") or H.REPO, raw)))
    if path == root or not path.startswith(root + os.sep):
        return None                                   # outside the project: scratch, temp, home
    rel = path[len(root) + 1:]
    if any(rel == d or rel.startswith(os.path.normcase(d) + os.sep) for d in NOT_BUILD_DIRS):
        return None
    return rel.replace(os.sep, "/")


def waiver_expired_by(sid, data):
    """True when this call writes the WAIVER_BUILD_FILES-th distinct project file under a waiver. On expiry the marker
    and the once-only stop flag are cleared, so nag and stop work again."""
    marker = read_marker(sid)
    if not marker.startswith("waived"):
        return False
    rel = build_file(data)
    if rel is None:
        return False
    st = load_state(sid)
    seen = st.get("waiver_writes") or []
    if rel not in seen:
        seen.append(rel)
    st["waiver_writes"] = seen
    if len(seen) < WAIVER_BUILD_FILES:
        save_state(sid, st)
        return False
    try:
        os.remove(marker_path(sid))
    except OSError:
        pass
    st.pop("waiver_writes", None)
    st.pop("blocked_once", None)
    st["nagged_at"] = None
    save_state(sid, st)
    H.append_log("waivers.log", "%s %s team-gate: WAIVER EXPIRED after %d project files (was: %s)"
                 % (H.now(), sid, len(seen), marker[:200]))
    return True


def cmd_mark():
    data = H.read_payload("team-gate")
    if H.is_agent_session(data) or not is_teammate_spawn(data):
        return 0
    sid = data.get("session_id", "unknown")
    old = read_marker(sid)
    if not old or old.startswith("waived"):
        name = str((data.get("tool_input") or {}).get("name"))[:64]
        write_marker(sid, "spawned %s %s" % (name, H.now()))
        print(json.dumps({"systemMessage": "Team gate satisfied: teammate '%s' spawned." % name}))
    return 0


def cmd_nag():
    data = H.read_payload("team-gate")
    if H.is_agent_session(data):
        return 0
    sid = data.get("session_id", "unknown")
    expired = False
    if read_marker(sid):
        if not waiver_expired_by(sid, data):
            return 0
        expired = True
    calls = lead_calls(sid)
    st = load_state(sid)
    if not expired:
        # "at least NAG_EVERY since the last nag", not "calls % NAG_EVERY == 0": hooks of one event run in parallel, so
        # the count this hook reads can skip a value and an exact-modulus test would then stay silent for good.
        if calls is None or calls < NAG_AFTER_CALLS:
            return 0
        last = st.get("nagged_at")
        if isinstance(last, int) and 0 <= calls - last < NAG_EVERY:     # a counter that went BACK nags again
            return 0
    st["nagged_at"] = calls or 0
    save_state(sid, st)
    text = (EXPIRED % WAIVER_BUILD_FILES if expired else "") + NAG % (calls or 0, sid)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}))
    return 0


def cmd_stop():
    data = H.read_payload("team-gate")
    if H.is_agent_session(data) or data.get("stop_hook_active"):
        return 0
    sid = data.get("session_id", "unknown")
    if read_marker(sid):
        return 0
    calls = lead_calls(sid)
    if calls is None or calls < BLOCK_AFTER_CALLS:
        return 0
    st = load_state(sid)
    if st.get("blocked_once"):
        return 0
    st["blocked_once"] = True
    save_state(sid, st)
    print(json.dumps({"decision": "block", "reason": NAG % (calls, sid)}))
    return 0


def cmd_waive(argv):
    if len(argv) < 4 or not " ".join(argv[3:]).strip():
        sys.stderr.write("usage: team-gate.py waive <session_id> \"<why the work is serial>\"\n")
        return 64
    sid, reason = argv[2], " ".join(argv[3:]).replace("\n", " ")[:300]
    write_marker(sid, "waived " + H.now() + " " + reason)
    st = load_state(sid)
    st.pop("waiver_writes", None)
    save_state(sid, st)
    H.append_log("waivers.log", "%s %s team-gate: %s" % (H.now(), sid, reason))
    print("team gate waived for %s: %s" % (sid, reason))
    return 0


def cmd_status(argv):
    if len(argv) < 3:
        sys.stderr.write("usage: team-gate.py status <session_id>\n")
        return 64
    m = read_marker(argv[2])
    if m:
        print("marker: " + m)
        return 0
    print("no marker - gate OPEN (lead calls: %s)" % lead_calls(argv[2]))
    return 1


# ---------------------------------------------------------------------------------------------- self-test

def selftest():
    import io
    tmp = tempfile.mkdtemp(prefix="team-gate-")
    saved = (H.STATE, H.REPO, sys.stdin, sys.stdout)
    H.STATE, H.REPO = os.path.join(tmp, "state"), os.path.join(tmp, "repo")
    os.makedirs(os.path.join(H.REPO, "memory"))
    bad = [0]

    def run(fn, payload):
        sys.stdin, sys.stdout = io.StringIO(json.dumps(payload)), io.StringIO()
        try:
            fn()
            return sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = saved[2], saved[3]

    def calls(sid, n):
        H.ensure_state()
        with open(os.path.join(H.STATE, "skills-" + H.sid_safe(sid) + ".json"), "w") as fh:
            json.dump({"calls": n}, fh)

    def check(name, got, want):
        bad[0] += got != want
        print(("ok  " if got == want else "BAD ") + name, got)

    def write(sid, path):
        return run(cmd_nag, {"session_id": sid, "tool_name": "Write", "tool_input": {"file_path": path}})

    try:
        s = "s-quiet"
        calls(s, 5)
        check("quiet under NAG_AFTER_CALLS", run(cmd_nag, {"session_id": s}), "")
        calls(s, NAG_AFTER_CALLS)
        check("nags at NAG_AFTER_CALLS", "TEAM GATE" in run(cmd_nag, {"session_id": s}), True)
        calls(s, NAG_AFTER_CALLS + 1)
        check("silent right after a nag", run(cmd_nag, {"session_id": s}), "")
        calls(s, NAG_AFTER_CALLS + NAG_EVERY + 3)            # a skipped count still nags (no exact modulus)
        check("nags again after NAG_EVERY even if a count was skipped", "TEAM GATE" in run(cmd_nag, {"session_id": s}), True)
        check("teammate session is never nagged", run(cmd_nag, {"session_id": s, "agent_id": "a1"}), "")
        calls(s, NAG_AFTER_CALLS + 2)                        # the counter went BACK (its state was reset)
        check("nags again after the counter went backwards", "TEAM GATE" in run(cmd_nag, {"session_id": s}), True)

        s = "s-stop"
        calls(s, BLOCK_AFTER_CALLS - 1)
        check("stop allowed under BLOCK_AFTER_CALLS", run(cmd_stop, {"session_id": s}), "")
        calls(s, BLOCK_AFTER_CALLS)
        out = run(cmd_stop, {"session_id": s})
        check("stop BLOCKS past BLOCK_AFTER_CALLS with no teammate", '"decision": "block"' in out, True)
        check("stop blocks only once", run(cmd_stop, {"session_id": s}), "")
        check("stop re-entry is allowed", run(cmd_stop, {"session_id": "s-other", "stop_hook_active": True}), "")

        s = "s-spawn"
        calls(s, 99)
        run(cmd_mark, {"session_id": s, "tool_name": "Agent", "tool_input": {"subagent_type": "Explore", "name": "x"}})
        check("a named read-only helper does not satisfy", read_marker(s), "")
        run(cmd_mark, {"session_id": s, "tool_name": "Agent", "tool_input": {"subagent_type": "reviewer"}})
        check("an unnamed agent does not satisfy", read_marker(s), "")
        run(cmd_mark, {"session_id": s, "tool_name": "Agent", "tool_input": {"name": "builder-api"}})
        check("a named teammate satisfies", read_marker(s).startswith("spawned builder-api"), True)
        check("no stop block after a spawn", run(cmd_stop, {"session_id": s}), "")
        check("no nag after a spawn", run(cmd_nag, {"session_id": s}), "")

        s = "s-waive"
        calls(s, 99)
        old_err, sys.stderr = sys.stderr, io.StringIO()
        try:
            check("waive without a reason is a usage error", cmd_waive(["x", "waive", s]), 64)
        finally:
            sys.stderr = old_err
        sys.stdout = io.StringIO()
        try:
            cmd_waive(["x", "waive", s, "one-line fix"])
        finally:
            sys.stdout = saved[3]
        check("waiver logged", "one-line fix" in open(os.path.join(H.STATE, "waivers.log")).read(), True)
        check("no stop block under a waiver", run(cmd_stop, {"session_id": s}), "")
        check("scratch write does not count", write(s, os.path.join(tmp, "scratch", "a.py")), "")
        check("memory write does not count", write(s, os.path.join(H.REPO, "memory", "topics", "m.md")), "")
        check("state write does not count", write(s, os.path.join(H.REPO, ".claude", "state", "x")), "")
        check("project file 1", write(s, os.path.join(H.REPO, "src", "a.py")), "")
        check("same file again does not count twice", write(s, os.path.join(H.REPO, "src", "a.py")), "")
        check("project file 2", write(s, os.path.join(H.REPO, "src", "b.py")), "")
        out = write(s, os.path.join(H.REPO, "src", "c.py"))
        check("3rd project file EXPIRES the waiver and nags at once", "EXPIRED" in out, True)
        check("waiver marker removed", read_marker(s), "")
        check("stop can block again after expiry", '"decision": "block"' in run(cmd_stop, {"session_id": s}), True)
        run(cmd_mark, {"session_id": s, "tool_name": "Agent", "tool_input": {"name": "builder-ui"}})
        check("a spawn after expiry satisfies", read_marker(s).startswith("spawned"), True)

        s = "s-replace"
        sys.stdout = io.StringIO()
        try:
            cmd_waive(["x", "waive", s, "serial"])
        finally:
            sys.stdout = saved[3]
        run(cmd_mark, {"session_id": s, "tool_name": "Agent", "tool_input": {"name": "sam"}})
        check("a spawn replaces a waiver", read_marker(s).startswith("spawned sam"), True)
        for f in ("a", "b", "c", "d"):
            write(s, os.path.join(H.REPO, f + ".py"))
        check("a spawn marker never expires", read_marker(s).startswith("spawned sam"), True)
    finally:
        H.STATE, H.REPO, sys.stdin, sys.stdout = saved
        shutil.rmtree(tmp, ignore_errors=True)
    print("failures:", bad[0])
    return 1 if bad[0] else 0


MODES = {"mark": lambda a: cmd_mark(), "nag": lambda a: cmd_nag(), "stop": lambda a: cmd_stop(),
         "waive": cmd_waive, "status": cmd_status, "test": lambda a: selftest()}


def main(argv):
    mode = argv[1] if len(argv) > 1 else ""
    if mode not in MODES:
        sys.stderr.write("usage: team-gate.py mark|nag|stop|waive|status|test\n")
        return 64
    return MODES[mode](argv)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except SystemExit:
        raise
    except Exception as exc:
        H.fail_open("team-gate", exc)
