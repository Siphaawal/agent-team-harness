#!/usr/bin/env python3
"""team-watchdog.py - wakes the lead when a teammate it is waiting on goes quiet or grows too big.

Why: a teammate can finish its work and sit idle without a report, or hang inside a tool call, while the lead waits.
Nothing can wake a lead whose turn has ended - except a background command: its exit re-invokes the lead. The lead
starts this one in the background (team-stall-gate.py stop refuses the turn end while teammates are active and no
watchdog heartbeat is live):

  python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/team-watchdog.py" <session_id> [--minutes 5] [--max 30]
          [--ctx-limit 200000] [--also NAME=OUTPUT_DIR ...] [--transcript LEAD_TRANSCRIPT]

It watches every named teammate whose transcript changed in the last --minutes when it starts, plus any that becomes
active later. It SKIPS a teammate whose tasks are all completed, a subagent that has returned, and a teammate stopped
with TaskStop (recorded by team-stall-gate.py stopped) until that teammate writes again. A teammate inside one long
command writes no transcript; --also NAME=DIR counts that folder changing as a sign of life.

Prints one verdict and exits 0:
  STALE     a watched teammate has been quiet for --minutes: who, how long, and its last action (the first clue to why)
  OVERSIZE  a watched teammate's last turn carried more than --ctx-limit context tokens: retire it (handoff note, then a
            fresh teammate). Fires ONCE per teammate per session, so a restarted watchdog does not loop on it.
  QUIET     nothing to watch: no watchable teammate active and no --also folder changing within the window
  MAX       --max minutes passed with teammates still active: look at them, then restart the watchdog
  ERROR     the watchdog itself failed (logged to state/hook-errors.log): check the teammates yourself
The first watch set reaches back max(--minutes, the stall gate's 5-minute active window), so a teammate the gate
counts as active is always watched. Exit 64 on a usage error. Heartbeat: .claude/state/watchdog-<session_id>.json while it runs.
Locations and their env overrides: _teamlib.py. TEAM_WATCHDOG_TICK (seconds, default 5) is for tests.
"""
import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _hooklib as H  # noqa: E402
import _teamlib as T  # noqa: E402

CTX_LIMIT = 200000
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def tick_s():
    try:
        return max(0.05, float(os.environ.get("TEAM_WATCHDOG_TICK", "5")))
    except ValueError:
        return 5.0


def clean(text, n):
    """Transcript text is untrusted: one line, no control characters, capped, and ASCII only - a lone surrogate (half
    an emoji from a truncated string) or any character the console cannot encode would otherwise raise on print and
    cost the verdict."""
    text = CONTROL.sub(" ", str(text)).strip()[:n]
    return text.encode("ascii", "backslashreplace").decode("ascii")


def tail_records(path, nbytes):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - nbytes))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def last_action(path):
    for rec in tail_records(path, 262144):
        msg = rec.get("message")
        content = msg.get("content") if rec.get("type") == "assistant" and isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for block in reversed(content):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                if inp.get("run_in_background"):
                    return "started a background command and is waiting on it: " + clean(inp.get("description", ""), 120)
                return "last tool: %s %s" % (clean(block.get("name"), 40), clean(inp.get("description", ""), 120))
            if block.get("type") == "text" and str(block.get("text", "")).strip():
                return "last said: " + clean(block["text"], 200)
    return "no assistant turn found"


def ctx_tokens(path):
    """Context size of the newest assistant turn: input + cache read + cache creation tokens."""
    for rec in tail_records(path, 524288):
        msg = rec.get("message")
        usage = msg.get("usage") if rec.get("type") == "assistant" and isinstance(msg, dict) else None
        if isinstance(usage, dict):
            try:
                return sum(int(usage.get(k, 0) or 0) for k in
                           ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            except (TypeError, ValueError):
                return 0
    return 0


def warned_path(sid):
    return os.path.join(H.STATE, "watchdog-ctx-" + H.sid_safe(sid) + ".json")


def oversize(sid, mem, names, limit):
    """[(name, tokens)] over the limit and not warned before; records them so each fires once."""
    try:
        with open(warned_path(sid), "r", encoding="utf-8") as fh:
            warned = set(json.load(fh))
    except (OSError, ValueError, TypeError):
        warned = set()
    over = [(n, ctx_tokens(mem[n]["path"])) for n in sorted(names) if n in mem and n not in warned]
    over = [(n, t) for n, t in over if t > limit]
    if over:
        T.write_json(warned_path(sid), sorted(warned | {n for n, _ in over}))
    return over


def newest_age_s(folder, now):
    best = float("inf")
    for base, _dirs, files in os.walk(folder):
        for f in files:
            m = T.mtime(os.path.join(base, f))
            if m:
                best = min(best, now - m)
    return best


def beat(sid):
    T.write_json(T.heartbeat_path(sid), {"pid": os.getpid(), "at": H.now()})


def clear(sid):
    try:
        os.remove(T.heartbeat_path(sid))
    except OSError:
        pass


def run(sid, minutes, max_minutes, limit, also=(), transcript=None, out=None):
    out = out or sys.stdout
    say = lambda s: (out.write(s + "\n"), out.flush())
    window = minutes * 60
    # The first watch set reaches back at least as far as the stall gate's "active" window: a lead whose Stop was
    # blocked for a teammate active 3 minutes ago must not get QUIET at once from `--minutes 2` (and be blocked again).
    first = max(minutes, T.ACTIVE_MINUTES) * 60
    start = time.time()
    mem, names = T.watchable(sid, transcript)
    watched = {n for n in names if start - mem[n]["mtime"] < first}
    extra = {n: newest_age_s(d, start) for n, d in also}
    say("WATCHDOG watching %s (quiet limit %.2f min)" % (sorted(watched | set(extra)) or "nobody", minutes))
    if not watched and not any(a < first for a in extra.values()):
        say("QUIET: no teammate (and no --also folder) has been active in the last %.2f min." % (first / 60))
        return "QUIET"
    while True:
        beat(sid)
        now = time.time()
        mem, names = T.watchable(sid, transcript)
        extra = {n: newest_age_s(d, now) for n, d in also}
        quiet = {n: min(now - mem[n]["mtime"], extra.get(n, float("inf"))) for n in names}
        watched |= {n for n in names if quiet[n] < window}
        watched &= names                       # stopped / finished / all-tasks-done since: nothing left to wait for
        over = oversize(sid, mem, watched, limit)
        if over:
            for n, t in over:
                say("OVERSIZE: %s context %dk > %dk tokens. Retire it now: ask it for a handoff note, mark its task "
                    "completed, then spawn a fresh teammate (new name) with that note." % (n, t // 1000, limit // 1000))
            return "OVERSIZE"
        stale = [(n, quiet[n]) for n in sorted(watched) if quiet[n] >= window]
        if stale:
            for n, a in stale:
                say("STALE: %s quiet for %.1f min; %s" % (n, a / 60, last_action(mem[n]["path"])))
            say("If its report has not arrived, find out WHY now: ListAgents, read what it was doing, SendMessage it "
                "directly. A finished teammate that already reported needs nothing.")
            return "STALE"
        if now - start > max_minutes * 60:
            say("MAX: still active after %.0f min: %s. Look at them, then restart the watchdog."
                % (max_minutes, sorted(watched)))
            return "MAX"
        if not watched and not any(a < window for a in extra.values()):
            say("QUIET: every watched teammate finished, returned or was stopped, and no --also folder is changing.")
            return "QUIET"
        time.sleep(tick_s())


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write("team-watchdog.py: error: %s\n" % message)
        sys.exit(64)


def parse(argv):
    p = Parser(prog="team-watchdog.py", description="Wake the lead when a teammate goes quiet or too big. "
               "'team-watchdog.py test' runs the self-test.")
    p.add_argument("session_id")
    p.add_argument("--minutes", type=float, default=T.ACTIVE_MINUTES,
                   help="quiet limit per teammate (default %g, the stall gate's active window)" % T.ACTIVE_MINUTES)
    p.add_argument("--max", type=float, default=30.0, dest="max_minutes", help="give up after this many minutes")
    p.add_argument("--ctx-limit", type=int, default=CTX_LIMIT, help="context tokens before OVERSIZE (default %d)"
                   % CTX_LIMIT)
    p.add_argument("--also", action="append", default=[], metavar="NAME=DIR",
                   help="count DIR changing as a sign of life for teammate NAME")
    p.add_argument("--transcript", help="the lead's transcript path (finds the teammates without a glob)")
    a = p.parse_args(argv)
    also = []
    for item in a.also:
        if "=" not in item:
            p.error("--also needs NAME=DIR, got %r" % item)
        also.append(tuple(item.split("=", 1)))
    if a.minutes <= 0 or a.max_minutes <= 0 or a.ctx_limit <= 0:
        p.error("--minutes, --max and --ctx-limit must be positive")
    a.also = also
    return a


# ---------------------------------------------------------------------------------------------- self-test

def selftest():
    import io
    tmp = tempfile.mkdtemp(prefix="team-watchdog-")
    saved_state = H.STATE
    keys = ("TEAM_CLAUDE_HOME", "TEAM_TASKS_DIR", "TEAM_PROJECTS_DIR", "TEAM_WATCHDOG_TICK")
    saved_env = {k: os.environ.get(k) for k in keys}
    H.STATE = os.path.join(tmp, "state")
    os.environ["TEAM_CLAUDE_HOME"] = os.path.join(tmp, "home")
    os.environ.pop("TEAM_TASKS_DIR", None)
    os.environ.pop("TEAM_PROJECTS_DIR", None)
    os.environ["TEAM_WATCHDOG_TICK"] = "0.05"
    bad = [0]
    counter = [0]

    def check(name, got, want):
        bad[0] += got != want
        print(("ok  " if got == want else "BAD ") + name, got)

    def session():
        counter[0] += 1
        sid = "%08d-0000-4000-8000-000000000000" % counter[0]
        sub = os.path.join(tmp, "home", "projects", "proj", sid, "subagents")
        os.makedirs(sub)
        os.makedirs(os.path.join(tmp, "home", "tasks", "session-" + sid[:8]))
        return sid, sub

    def teammate(sub, name, age_s, tokens=1000, text="working on it", finished=False):
        p = os.path.join(sub, "agent-a%s-9c1d.jsonl" % name)
        rec = {"type": "assistant", "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": tokens},
                                                "content": [{"type": "text", "text": text}]}}
        with open(p, "w") as fh:
            fh.write(json.dumps(rec) + "\n")
        with open(p[:-6] + ".meta.json", "w") as fh:
            json.dump({"name": name, "finished": finished}, fh)
        t = time.time() - age_s
        os.utime(p, (t, t))
        return p

    def task(sid, i, owner, status):
        with open(os.path.join(tmp, "home", "tasks", "session-" + sid[:8], "%s.json" % i), "w") as fh:
            json.dump({"id": str(i), "owner": owner, "status": status}, fh)

    def watch(sid, minutes=0.02, max_minutes=0.05, limit=CTX_LIMIT, also=(), transcript=None):
        buf = io.StringIO()
        try:
            v = run(sid, minutes, max_minutes, limit, also, transcript, buf)
        except Exception as exc:                        # a crash is a FAILED case, never a silent pass
            v = "ERROR " + type(exc).__name__
        finally:
            clear(sid)
        return v, buf.getvalue()

    try:
        sid, sub = session()
        check("nobody to watch -> QUIET", watch(sid)[0], "QUIET")

        sid, sub = session()
        teammate(sub, "builder-api", 0, text="waiting on the lock\x1b[31m")
        v, out = watch(sid)
        check("a teammate that goes quiet -> STALE", v, "STALE")
        check("STALE names the teammate and its last words", "builder-api" in out and "waiting on the lock" in out, True)
        check("control characters from the transcript are removed", "\x1b" in out, False)

        sid, sub = session()
        teammate(sub, "builder-api", 0, text="half an emoji \ud83d and café")
        v, out = watch(sid)
        check("a lone surrogate in the last words still gives STALE", v, "STALE")
        check("the verdict text is ASCII only (printable on any console)", out.isascii() and "half an emoji" in out, True)

        sid, sub = session()
        teammate(sub, "builder-api", 0)
        task(sid, 1, "builder-api", "completed")
        check("a teammate whose tasks are all completed is skipped", watch(sid)[0], "QUIET")

        sid, sub = session()
        teammate(sub, "sam", 0, finished=True)
        check("a returned subagent is skipped", watch(sid)[0], "QUIET")

        sid, sub = session()
        teammate(sub, "reviewer-docs", 0)
        T.record_stop(sid, "reviewer-docs")
        check("a teammate stopped with TaskStop is skipped", watch(sid)[0], "QUIET")
        sid2, sub2 = session()
        teammate(sub2, "reviewer-docs", 0)
        T.record_stop(sid2, "areviewer-docs-9c1d")          # stopped by agent id, not by name
        check("a teammate stopped by agent id is skipped", watch(sid2)[0], "QUIET")
        sid3, sub3 = session()
        teammate(sub3, "alex", 0)
        T.record_stop(sid3, "alex", at=time.time() - 120)  # stopped long ago, then woken by a message
        check("a stopped teammate that wrote again is watched", watch(sid3)[0], "STALE")

        sid, sub = session()
        teammate(sub, "builder-big", 0, tokens=CTX_LIMIT + 5000)
        v, out = watch(sid)
        check("a teammate over the context limit -> OVERSIZE", v, "OVERSIZE")
        check("OVERSIZE fires once per teammate", watch(sid)[0], "STALE")

        sid, sub = session()
        p = teammate(sub, "builder-long", 0)
        live = os.path.join(tmp, "out-dir")
        os.makedirs(live, exist_ok=True)
        stop_at = time.time() + 3.0

        def touch_loop():
            while time.time() < stop_at:
                with open(os.path.join(live, "o.txt"), "w") as fh:
                    fh.write(str(time.time()))
                time.sleep(0.05)
        import threading
        th = threading.Thread(target=touch_loop)
        th.start()
        v, _ = watch(sid, minutes=0.02, max_minutes=0.03, also=[("builder-long", live)])
        th.join()
        check("--also: a changing output dir keeps a teammate alive until MAX", v, "MAX")

        # --also naming a teammate that is not watchable, and its folder unchanged for longer than the window: QUIET
        sid, sub = session()
        stale_dir = os.path.join(tmp, "old-out")
        os.makedirs(stale_dir)
        with open(os.path.join(stale_dir, "o.txt"), "w") as fh:
            fh.write("old")
        old = time.time() - 3600
        os.utime(os.path.join(stale_dir, "o.txt"), (old, old))
        v, out = watch(sid, max_minutes=0.03, also=[("ghost", stale_dir)])
        check("--also with no life anywhere -> QUIET at the start (not MAX)", (v, "active in the last" in out),
              ("QUIET", True))
        sid, sub = session()
        with open(os.path.join(stale_dir, "o.txt"), "w") as fh:          # changed just now, then never again
            fh.write("new")
        check("--also folder that stops changing -> QUIET during the run (not MAX)",
              watch(sid, minutes=0.01, max_minutes=0.1, also=[("ghost", stale_dir)])[0], "QUIET")

        # the stall gate counts a teammate active for ACTIVE_MINUTES; a smaller --minutes must still watch it
        sid, sub = session()
        teammate(sub, "builder-api", 180)
        check("teammate active 3 min ago, --minutes 2 -> watched (STALE), not QUIET at once",
              watch(sid, minutes=2, max_minutes=0.05)[0], "STALE")

        # a malformed record ("message" is a string) is skipped, not a crash
        sid, sub = session()
        p = teammate(sub, "builder-x", 0)
        with open(p, "w") as fh:
            fh.write(json.dumps({"type": "assistant", "message": "oops"}) + "\n")
        check("a malformed transcript record -> STALE, no crash", watch(sid)[0], "STALE")
        check("last_action / ctx_tokens on it", (last_action(p), ctx_tokens(p)), ("no assistant turn found", 0))

        # any crash inside the run: one ERROR line on stdout, logged, exit 0
        saved_w, T.watchable = T.watchable, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        buf, old_out = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            main(["team-watchdog.py", sid, "--minutes", "0.02", "--max", "0.05"])
            code = "returned"
        except SystemExit as e:
            code = e.code
        except Exception as e:                          # escaped the fail-open path: a FAILED case
            code = "raised " + type(e).__name__
        finally:
            sys.stdout, T.watchable = old_out, saved_w
        errs = os.path.join(H.STATE, H.ERRORS)
        logged = os.path.exists(errs) and "team-watchdog RuntimeError" in open(errs, encoding="utf-8").read()
        check("a crash prints an ERROR verdict, is logged and exits 0",
              (buf.getvalue().startswith("ERROR watchdog failed: RuntimeError"), logged, code in (0, None)),
              (True, True, True))

        # --transcript outside the projects dir is not read (a path flag must not make it read any directory)
        sid, _sub = session()
        outside = os.path.join(tmp, "elsewhere")
        osub = os.path.join(outside, sid, "subagents")
        os.makedirs(osub)
        teammate(osub, "intruder", 0)
        check("--transcript outside the projects dir is ignored",
              watch(sid, transcript=os.path.join(outside, sid + ".jsonl"))[0], "QUIET")
        inside = os.path.join(tmp, "home", "projects", "proj")
        sid_in, sub_in = session()
        teammate(sub_in, "builder-in", 0)
        check("--transcript inside the projects dir is read (positive control)",
              watch(sid_in, transcript=os.path.join(inside, sid_in + ".jsonl"))[0], "STALE")

        # a teammate stopped while the watchdog runs leaves the watch set (no STALE, no crash)
        import threading
        sid, sub = session()
        teammate(sub, "alex", 0)
        timer = threading.Timer(0.4, lambda: T.record_stop(sid, "alex"))
        timer.start()
        v, _ = watch(sid, minutes=0.05, max_minutes=0.1)
        timer.join()
        check("a teammate stopped mid-run -> QUIET", v, "QUIET")

        rc_err = io.StringIO()
        old, sys.stderr = sys.stderr, rc_err
        try:
            parse(["s", "--minutes", "0"])
            got = 0
        except SystemExit as e:
            got = e.code
        finally:
            sys.stderr = old
        check("bad --minutes is a usage error (64)", got, 64)
        check("heartbeat removed after a run", os.path.exists(T.heartbeat_path(sid)), False)
    finally:
        H.STATE = saved_state
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
    print("failures:", bad[0])
    return 1 if bad[0] else 0


def main(argv):
    if argv[1:2] == ["test"]:
        return selftest()
    a = parse(argv[1:])
    try:
        run(a.session_id, a.minutes, a.max_minutes, a.ctx_limit, a.also, a.transcript)
    except Exception as exc:
        # Fail open, but never silently: the lead is re-invoked by this exit and must learn the watch is over.
        sys.stdout.write("ERROR watchdog failed: %s - check teammates yourself (ListAgents, the task list).\n"
                         % type(exc).__name__)
        sys.stdout.flush()
        H.fail_open("team-watchdog", exc)                 # logged to state/hook-errors.log, exit 0
    finally:
        clear(a.session_id)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
