#!/usr/bin/env python3
"""team-stall-gate.py - a teammate may not go idle while it still owns an in_progress task, and a lead may not end its
turn with active teammates and nothing that will wake it.

Why: a teammate can stop working while it still holds a task - "waiting" on a background watcher, a lock or a message
that will never come - and its idle looks the same as being done. And once the lead's turn has ended nothing wakes it:
a teammate that finishes or stalls without reporting is only noticed when a human asks. A background command can wake
the lead (its exit re-invokes the lead), so the lead is made to start one: team-watchdog.py.

Modes (first argv):
  idle     TeammateIdle. If the teammate owns a task with status in_progress, exit 2 with a message telling it to check
           its blocker directly now (exit 2 keeps a teammate working). At most MAX_BLOCKS times per (teammate, task
           set); after that the idle is allowed and recorded as a stall for the lead.
  stop     Stop (lead). (1) A recorded stall whose task is still in_progress blocks the turn end ONCE, with the list.
           A repeated stall on the same task set is reported to the lead once per session, by design.
           (2) Otherwise, teammates active in the last ACTIVE_MINUTES (not stopped, not all tasks completed) and no
           live watchdog heartbeat -> block, telling the lead the exact command to start the watchdog in the background.
  stopped  PostToolUse, matcher "TaskStop": records the stopped teammate (name or agent id) so the watchdog and this
           gate skip it until it writes again.
  status   python3 .claude/hooks/team-stall-gate.py status <session_id>   (prints the recorded state)
  test     self-test against temporary state, task and transcript dirs

Payload keys used (TeammateIdle): session_id, teammate_name, team_name; (Stop): session_id, transcript_path,
stop_hook_active; (PostToolUse): tool_input.task_id, tool_response. Locations: _teamlib.py. State:
.claude/state/stall-<session_id>-<teammate>.json (one file per teammate: concurrent idles never lose an update).
Errors fail OPEN and are logged (_hooklib).
"""
import glob
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _hooklib as H  # noqa: E402
import _teamlib as T  # noqa: E402

MAX_BLOCKS = 3
ACTIVE_MINUTES = T.ACTIVE_MINUTES          # one window shared with the watchdog (see _teamlib)
WATCHDOG_CMD = 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/team-watchdog.py"'

TEAMMATE_MSG = (
    "STALL GATE (harness hook): you still own task(s) %s with status in_progress, so you may not go idle yet. Do NOT "
    "wait on a background watcher, a lock or a notification - check your blocker DIRECTLY now (read the lock, the file, "
    "the other agent's task status). If the work is finished, TaskUpdate it completed and report to the lead. If you are "
    "genuinely blocked on another agent or a human, SendMessage the lead naming the exact blocker and what unblocks it. "
    "(block %d of %d; after that the idle is allowed and the lead is told)"
)


def stall_prefix(sid):
    return os.path.join(H.STATE, "stall-" + H.sid_safe(sid) + "-")


def stall_path(sid, name):
    return stall_prefix(sid) + H.sid_safe(name) + ".json"


def load_stall(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            st = json.load(fh)
        if isinstance(st, dict):
            st.setdefault("blocks", {})
            st.setdefault("stalls", [])
            return st
    except Exception:
        pass
    return {"blocks": {}, "stalls": []}


def cmd_idle():
    data = H.read_payload("team-stall-gate")
    name = str(data.get("teammate_name") or "")
    sid = data.get("session_id") or ""
    if not name or not sid:
        return 0
    ids = T.open_task_ids(T.load_tasks(sid, data.get("team_name"), data.get("transcript_path")), name)
    if not ids:
        return 0
    path = stall_path(sid, name)
    st = load_stall(path)
    key = ",".join(ids)
    n = int(st["blocks"].get(key, 0) or 0)
    if n < MAX_BLOCKS:
        st["blocks"][key] = n + 1
        T.write_json(path, st)
        sys.stderr.write(TEAMMATE_MSG % ("#" + ", #".join(ids), n + 1, MAX_BLOCKS) + "\n")
        return 2
    if not any(s.get("key") == key for s in st["stalls"]):
        st["stalls"].append({"key": key, "teammate": name, "tasks": ids, "team": T.safe_name(data.get("team_name")),
                             "at": H.now(), "reported": False})
        H.append_log("team-stalls.log", "%s %s stall: %s idle holding #%s" % (H.now(), sid, name, ", #".join(ids)))
    st["blocks"][key] = 0                     # a later idle cycle gets the full nudge again
    T.write_json(path, st)
    return 0


def watchdog_gate(sid, transcript_path):
    names = T.active(sid, ACTIVE_MINUTES, transcript_path)
    if not names or T.watchdog_alive(sid):
        return 0
    print(json.dumps({"decision": "block", "reason": (
        "WATCHDOG GATE (harness hook): teammate(s) %s are active and nothing will wake you if they go quiet once this "
        "turn ends. Before ending the turn, start the watchdog as a BACKGROUND command (run_in_background): %s %s  - its "
        "exit re-invokes you and names who went quiet and what it last did. If they have already reported and you are "
        "not waiting on them, still start it: it exits as soon as they stay quiet." % (", ".join(names), WATCHDOG_CMD,
                                                                                        sid))}))
    return 0


def cmd_stop():
    data = H.read_payload("team-stall-gate")
    if H.is_agent_session(data) or data.get("stop_hook_active"):
        return 0
    sid = data.get("session_id") or ""
    if not sid:
        return 0
    tp = data.get("transcript_path")
    live = []
    for path in sorted(glob.glob(glob.escape(stall_prefix(sid)) + "*.json")):
        st = load_stall(path)
        changed = False
        for s in st["stalls"]:
            if s.get("reported"):
                continue
            tasks = T.load_tasks(sid, s.get("team"), tp)
            still = [i for i in s.get("tasks", []) if i in T.open_task_ids(tasks, s.get("teammate"))]
            if still:
                live.append("%s idle since %s holding #%s" % (s.get("teammate"), s.get("at"), ", #".join(still)))
            s["reported"] = True
            changed = True
        if changed:
            T.write_json(path, st)
    if not live:
        return watchdog_gate(sid, tp)
    print(json.dumps({"decision": "block", "reason": (
        "STALL GATE: teammate(s) went idle while still owning in_progress tasks: " + "; ".join(live) + ". Do not relay "
        "their 'waiting' as status: check the blocker yourself now (ListAgents, the task list, the file or lock it "
        "names), then wake the teammate with SendMessage or reassign the task. Tell a human if it cannot move.")}))
    return 0


def cmd_stopped():
    data = H.read_payload("team-stall-gate")
    sid = data.get("session_id") or ""
    inp = data.get("tool_input")
    key = str((inp if isinstance(inp, dict) else {}).get("task_id") or "").strip()
    # Only a stop that worked: wrongly marking a live teammate "stopped" would silence the watchdog about it. Keyed on
    # the message PREFIX, never a word search: the success message quotes the teammate's description, and one
    # described as "review error handling" was never recorded.
    if sid and key and stop_succeeded(data.get("tool_response")):
        T.record_stop(sid, key[:128])
    return 0


STOP_OK = "successfully stopped task"


def stop_succeeded(resp):
    """True for TaskStop's success result: {"message": "Successfully stopped task: <id> (<description>)", ...}, or
    the same text as a plain string (or a JSON string of that dict)."""
    if isinstance(resp, str):
        try:
            parsed = json.loads(resp)
            resp = parsed if isinstance(parsed, dict) else resp
        except ValueError:
            pass
    msg = resp.get("message", "") if isinstance(resp, dict) else resp
    return isinstance(msg, str) and msg.strip().lower().startswith(STOP_OK)


def cmd_status(argv):
    if len(argv) < 3:
        sys.stderr.write("usage: team-stall-gate.py status <session_id>\n")
        return 64
    sid = argv[2]
    out = {"stalls": {}, "stopped": T.stopped(sid), "watchdog_alive": T.watchdog_alive(sid)}
    for path in sorted(glob.glob(glob.escape(stall_prefix(sid)) + "*.json")):
        out["stalls"][os.path.basename(path)] = load_stall(path)
    print(json.dumps(out, indent=1))
    return 0


# ---------------------------------------------------------------------------------------------- self-test

def selftest():
    import io
    tmp = tempfile.mkdtemp(prefix="team-stall-")
    saved_state, saved_io = H.STATE, (sys.stdin, sys.stdout, sys.stderr)
    saved_env = {k: os.environ.get(k) for k in ("TEAM_CLAUDE_HOME", "TEAM_TASKS_DIR", "TEAM_PROJECTS_DIR")}
    H.STATE = os.path.join(tmp, "state")
    os.environ["TEAM_CLAUDE_HOME"] = os.path.join(tmp, "home")
    os.environ.pop("TEAM_TASKS_DIR", None)
    os.environ.pop("TEAM_PROJECTS_DIR", None)
    sid = "0123abcd-0000-4000-8000-000000000000"
    team = "session-0123abcd"
    tdir = os.path.join(tmp, "home", "tasks", team)
    sub = os.path.join(tmp, "home", "projects", "proj", sid, "subagents")
    os.makedirs(tdir)
    os.makedirs(sub)
    bad = [0]

    def task(i, owner, status):
        with open(os.path.join(tdir, "%s.json" % i), "w") as fh:
            json.dump({"id": str(i), "owner": owner, "status": status, "subject": "x"}, fh)

    def teammate(name, age_s=0, finished=False):
        p = os.path.join(sub, "agent-a%s-1f2e.jsonl" % name)
        with open(p, "w") as fh:
            fh.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}) + "\n")
        with open(p[:-6] + ".meta.json", "w") as fh:
            json.dump({"name": name, "teamName": team, "finished": finished}, fh)
        t = time.time() - age_s
        os.utime(p, (t, t))

    def run(fn, payload):
        sys.stdin, sys.stdout, sys.stderr = io.StringIO(json.dumps(payload)), io.StringIO(), io.StringIO()
        try:
            rc = fn()
            return rc, sys.stdout.getvalue(), sys.stderr.getvalue()
        finally:
            sys.stdin, sys.stdout, sys.stderr = saved_io

    def check(name, got, want):
        bad[0] += got != want
        print(("ok  " if got == want else "BAD ") + name, got)

    idle = {"session_id": sid, "teammate_name": "builder-api", "team_name": team}
    try:
        task(1, "builder-api", "in_progress")
        task(2, "reviewer-docs", "completed")
        for k in range(1, MAX_BLOCKS + 1):
            rc, _, err = run(cmd_idle, idle)
            check("idle with an in_progress task is refused (%d)" % k, (rc, "#1" in err), (2, True))
        rc, _, _ = run(cmd_idle, idle)
        check("after MAX_BLOCKS the idle is allowed", rc, 0)
        check("idle of a teammate with only completed tasks is allowed",
              run(cmd_idle, dict(idle, teammate_name="reviewer-docs"))[0], 0)
        check("idle without a team_name finds the tasks by session", run(cmd_idle, {"session_id": sid,
              "teammate_name": "builder-api"})[0], 2)
        check("a hostile team_name is not used as a path", run(cmd_idle, dict(idle, team_name="../../x"))[0], 2)
        check("a name or session id ending in a newline is rejected / cleaned",
              (T.safe_name("builder-api\n"), T.safe_name("builder-api"), T.sid_clean("0123abcd\n"),
               "\n" in H.sid_safe("0123abcd\n")), (None, "builder-api", "0123abcd", False))
        check("idle with no task dir fails open", run(cmd_idle, {"session_id": "ffffffff-x", "teammate_name": "builder-api"})[0], 0)

        rc, out, _ = run(cmd_stop, {"session_id": sid})
        check("lead stop BLOCKS on a recorded stall", '"decision": "block"' in out and "builder-api" in out, True)
        rc, out, _ = run(cmd_stop, {"session_id": sid})
        check("the stall is reported once", "STALL GATE" in out, False)
        check("teammate stop never blocked", run(cmd_stop, {"session_id": sid, "agent_id": "a1"})[1], "")

        # watchdog gate
        teammate("builder-api", age_s=10)
        rc, out, _ = run(cmd_stop, {"session_id": sid})
        check("active teammate + no watchdog -> stop BLOCKS", '"decision": "block"' in out, True)
        check("the block names the background watchdog command",
              WATCHDOG_CMD + " " + sid in json.loads(out)["reason"], True)
        check("re-entry allowed", run(cmd_stop, {"session_id": sid, "stop_hook_active": True})[1], "")
        T.write_json(T.heartbeat_path(sid), {"pid": 1})
        check("live watchdog heartbeat -> allowed", run(cmd_stop, {"session_id": sid})[1], "")
        os.remove(T.heartbeat_path(sid))
        task(1, "builder-api", "completed")
        check("teammate whose tasks are all completed -> allowed", run(cmd_stop, {"session_id": sid})[1], "")
        task(1, "builder-api", "in_progress")
        teammate("builder-api", age_s=ACTIVE_MINUTES * 60 + 30)
        check("teammate quiet longer than the window -> allowed", run(cmd_stop, {"session_id": sid})[1], "")
        teammate("builder-api", age_s=10)
        run(cmd_stopped, {"session_id": sid, "tool_name": "TaskStop", "tool_input": {"task_id": "builder-api"},
                          "tool_response": {"message": "Failed: no such task"}})
        check("a FAILED TaskStop is not recorded", '"decision": "block"' in run(cmd_stop, {"session_id": sid})[1], True)
        run(cmd_stopped, {"session_id": sid, "tool_name": "TaskStop", "tool_input": {"task_id": "builder-api"},
                          "tool_response": "Error: No task found with ID: builder-api (stopped earlier?)"})
        check("an error STRING mentioning 'stopped' is not recorded",
              '"decision": "block"' in run(cmd_stop, {"session_id": sid})[1], True)
        # the REAL success shape: the message quotes the teammate's description, here one full of "error" and "fail"
        desc = "Review error handling and fix failing tests"
        run(cmd_stopped, {"session_id": sid, "tool_name": "TaskStop", "tool_input": {"task_id": "builder-api"},
                          "tool_response": {"message": "Successfully stopped task: t1abc (%s)" % desc,
                                            "task_id": "t1abc", "task_type": "in_process_teammate", "command": desc}})
        check("a STOPPED teammate (real response, description says error/fail) -> allowed",
              run(cmd_stop, {"session_id": sid})[1], "")
        check("the success text as a plain string counts too",
              (stop_succeeded("Successfully stopped task: t2 (x)"), stop_succeeded({"message": "Task failed"})),
              (True, False))
        later = time.time() + T.STOP_GRACE_S + 5
        os.utime(os.path.join(sub, "agent-abuilder-api-1f2e.jsonl"), (later, later))
        check("a stopped teammate that writes again counts again",
              '"decision": "block"' in run(cmd_stop, {"session_id": sid})[1], True)
        teammate("sam", age_s=5, finished=True)
        os.remove(os.path.join(sub, "agent-abuilder-api-1f2e.jsonl"))
        check("a returned (finished) subagent -> allowed", run(cmd_stop, {"session_id": sid})[1], "")
    finally:
        H.STATE = saved_state
        sys.stdin, sys.stdout, sys.stderr = saved_io
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
    print("failures:", bad[0])
    return 1 if bad[0] else 0


MODES = {"idle": lambda a: cmd_idle(), "stop": lambda a: cmd_stop(), "stopped": lambda a: cmd_stopped(),
         "status": cmd_status, "test": lambda a: selftest()}


def main(argv):
    mode = argv[1] if len(argv) > 1 else ""
    if mode not in MODES:
        sys.stderr.write("usage: team-stall-gate.py idle|stop|stopped|status|test\n")
        return 64
    return MODES[mode](argv)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except SystemExit:
        raise
    except Exception as exc:
        H.fail_open("team-stall-gate", exc)
