#!/usr/bin/env python3
"""session-log.py - SessionEnd + PreCompact hook. Appends a SKELETON entry to the member's daily log so something
survives a session that ends without /wrap.

Path: memory/log/YYYY-MM/YYYY-MM-DD.<member>.md - one file per member per day, so two members never conflict.
Entry: "## HH:MM <zone> - auto (<event>) session <id8>" + files written this session (Write/Edit/NotebookEdit
file_path from the transcript JSONL, unique, repo-relative where possible, max 25), one `git diff --stat` summary
line, and the marker "(auto skeleton - /wrap enriches it)".

Only file paths and git stat lines are written - never transcript text. Fails OPEN: errors go to
.claude/state/session-log-errors.log and the hook exits 0. Skips teammates/subagents (agent_id / agent_type).
Dedupe: PreCompact at most once per session per 15 min; SessionEnd once per session.
Env SESSION_LOG_DIR overrides the log root (state goes beside it) for tests. Self-test: python3 session-log.py test
"""
import datetime
import json
import os
import subprocess
import sys
import tempfile
import time

os.umask(0o077)                 # state + logs are this member's business only, on a shared host too
HOOKS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HOOKS))
sys.path.insert(0, os.path.join(REPO, "tools"))
sys.path.insert(0, HOOKS)
try:
    import _hooklib  # noqa: F401,E402 - importing it narrows an existing loose .claude/state to 0700
except Exception:
    pass
DEFAULT_LOG_ROOT = os.path.join(REPO, "memory", "log")
HOOK_STATE = os.path.join(REPO, ".claude", "state")
MAX_FILES = 25
PARSE_BUDGET_S = 2.5
MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024
PRECOMPACT_GAP_S = 15 * 60
WRITE_TOOLS = {"Write", "Edit", "NotebookEdit", "MultiEdit"}


def member():
    try:
        from common import member as m          # tools/common.py - the one definition of "who is this"
        return m()
    except Exception:
        import re
        raw = os.environ.get("AGENT_MEMBER") or os.environ.get("USER") or "unknown"
        return (re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-") or "unknown")[:32]


def paths():
    override = os.environ.get("SESSION_LOG_DIR")
    if override:
        return override, os.path.join(override, "_state")
    return DEFAULT_LOG_ROOT, HOOK_STATE


def log_error(state_dir, exc):
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, "session-log-errors.log"), "a", encoding="utf-8") as fh:
            fh.write("%s %r\n" % (datetime.datetime.now().isoformat(timespec="seconds"), exc))
    except Exception:
        pass


def zone(now):
    return now.astimezone().strftime("%Z") or "local"


def files_written(transcript_path):
    """Unique file paths of Write/Edit/NotebookEdit tool calls, in first-seen order."""
    seen, out = set(), []
    if not transcript_path or not os.path.isfile(transcript_path):
        return out
    deadline = time.time() + PARSE_BUDGET_S
    size = os.path.getsize(transcript_path)
    with open(transcript_path, "rb") as fh:
        if size > MAX_TRANSCRIPT_BYTES:
            fh.seek(size - MAX_TRANSCRIPT_BYTES)
            fh.readline()  # drop partial line
        for raw in fh:
            if time.time() > deadline:
                break
            if b'"tool_use"' not in raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            msg = obj.get("message") if isinstance(obj, dict) else None
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for blk in content:
                if not (isinstance(blk, dict) and blk.get("type") == "tool_use" and blk.get("name") in WRITE_TOOLS):
                    continue
                inp = blk.get("input") or {}
                p = inp.get("file_path") or inp.get("notebook_path")
                if isinstance(p, str) and p and p not in seen:
                    seen.add(p)
                    out.append(p)
    return out


def rel(p, root):
    """Repo-relative path, or None for anything outside the repo. The daily log is COMMITTED, so a path like
    /home/<user>/.ssh/config or /srv/clients/<client>/notes.md must never be written into it - only a count."""
    try:
        r = os.path.relpath(os.path.abspath(p), os.path.abspath(root))
    except ValueError:                 # another drive on Windows
        return None
    return None if (r == ".." or r.startswith(".." + os.sep) or r.startswith("../") or os.path.isabs(r)) \
        else r.replace("\\", "/")


def git_stat(cwd):
    try:
        r = subprocess.run(["git", "-C", cwd, "diff", "--stat"], capture_output=True, text=True, timeout=2,
                           stdin=subprocess.DEVNULL)
        lines = r.stdout.strip().splitlines() if r.returncode == 0 else []
        return lines[-1].strip() if lines else "no unstaged changes"
    except Exception as e:
        return "git diff failed (%s)" % type(e).__name__


def load_state(state_dir):
    try:
        with open(os.path.join(state_dir, "session-log.json"), encoding="utf-8") as fh:
            d = json.load(fh)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_state(state_dir, st):
    os.makedirs(state_dir, exist_ok=True)
    cutoff = time.time() - 7 * 86400
    st = {k: v for k, v in st.items() if isinstance(v, dict) and max(v.values() or [0]) > cutoff}
    fd, tmp = tempfile.mkstemp(dir=state_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(st, fh)
        os.replace(tmp, os.path.join(state_dir, "session-log.json"))
    except BaseException:
        try:
            os.remove(tmp)                       # never leave a temp file behind
        except OSError:
            pass
        raise


def build_entry(payload, now):
    event = payload.get("hook_event_name") or "unknown"
    sid = str(payload.get("session_id") or "unknown")[:8]
    cwd = payload.get("cwd") or REPO
    files = files_written(payload.get("transcript_path") or "")
    inside = [r for r in (rel(f, cwd) for f in files) if r is not None]
    outside = len(files) - len(inside)
    lines = ["## %s %s - auto (%s) session %s" % (now.strftime("%H:%M"), zone(now), event, sid), ""]
    if inside:
        extra = " (+%d more)" % (len(inside) - MAX_FILES) if len(inside) > MAX_FILES else ""
        lines.append("- files written in this repo (%d%s):" % (len(inside), extra))
        lines += ["  - `%s`" % r for r in inside[:MAX_FILES]]
    else:
        lines.append("- files written in this repo: none seen")
    if outside:
        lines.append("- files written outside this repo: %d (paths not recorded - this log is committed)" % outside)
    lines.append("- git diff --stat: %s" % git_stat(cwd))
    lines.append("- (auto skeleton - /wrap enriches it)")
    return "\n".join(lines) + "\n"


def append_entry(log_root, now, who, entry):
    d = os.path.join(log_root, now.strftime("%Y-%m"))
    os.makedirs(d, exist_ok=True)
    fp = os.path.join(d, "%s.%s.md" % (now.strftime("%Y-%m-%d"), who))
    new = not os.path.isfile(fp)
    with open(fp, "a", encoding="utf-8", newline="\n") as fh:
        fh.write("# %s - %s\n\n" % (now.strftime("%Y-%m-%d"), who) if new else "\n")
        fh.write(entry)
    return fp


def handle(payload, now=None):
    """Returns the log path written, or None when skipped."""
    log_root, state_dir = paths()
    if "agent_id" in payload or "agent_type" in payload:
        return None
    event = payload.get("hook_event_name")
    if event not in ("SessionEnd", "PreCompact"):
        return None
    sid = str(payload.get("session_id") or "")
    now = now or datetime.datetime.now()
    st = load_state(state_dir)
    rec = st.get(sid, {})
    ts = time.time()
    if event == "SessionEnd" and rec.get("SessionEnd"):
        return None
    if event == "PreCompact" and ts - rec.get("PreCompact", 0) < PRECOMPACT_GAP_S:
        return None
    fp = append_entry(log_root, now, member(), build_entry(payload, now))
    rec[event] = ts
    st[sid] = rec
    save_state(state_dir, st)
    return fp


def main():
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        payload = json.loads(raw) if raw.strip() else {}
        if isinstance(payload, dict):
            handle(payload)
    except Exception as e:
        log_error(paths()[1], e)
    return 0


def selftest():
    import shutil
    tmp = tempfile.mkdtemp(prefix="sessionlog-test-")
    os.environ["SESSION_LOG_DIR"] = os.path.join(tmp, "log")
    os.environ["AGENT_MEMBER"] = "Test User"
    try:
        proj = os.path.join(tmp, "proj")
        target = os.path.join(proj, "a.txt")
        os.makedirs(proj)
        subprocess.run(["git", "-C", proj, "init", "-q"], check=True, stdin=subprocess.DEVNULL)
        tr = os.path.join(tmp, "t.jsonl")
        with open(tr, "w", encoding="utf-8") as fh:
            for name, p in (("Write", target), ("Edit", target), ("Read", "/nope"), ("NotebookEdit", "/x/n.ipynb")):
                inp = {"notebook_path": p} if name == "NotebookEdit" else {"file_path": p}
                fh.write(json.dumps({"message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}) + "\n")
            fh.write("not json tool_use\n")
        now = datetime.datetime(2026, 1, 10, 1, 2)
        pl = {"session_id": "abcdef1234567", "transcript_path": tr, "cwd": proj, "hook_event_name": "SessionEnd"}
        fp = handle(dict(pl), now)
        assert fp.replace("\\", "/").endswith("2026-01/2026-01-10.test-user.md"), "per-member path: " + fp
        text = open(fp, encoding="utf-8").read()
        print(text)
        assert text.startswith("# 2026-01-10 - test-user\n"), "header"
        assert "## 01:02 " in text and "auto (SessionEnd) session abcdef12" in text, "heading"
        assert text.count("`a.txt`") == 1 and "/nope" not in text, "files"
        assert "n.ipynb" not in text and "files written outside this repo: 1" in text, "outside path must not leak"
        assert "git diff --stat" in text and "(auto skeleton - /wrap enriches it)" in text, "git/marker"
        assert handle(dict(pl), now) is None, "SessionEnd dedupe"
        pc = dict(pl, hook_event_name="PreCompact")
        assert handle(dict(pc), now), "precompact first"
        assert handle(dict(pc), now) is None, "precompact dedupe"
        assert handle(dict(pl, agent_id="x", hook_event_name="PreCompact", session_id="zz1"), now) is None, "agent skip"
        r = subprocess.run([sys.executable, "-I", os.path.abspath(__file__)], input="{{garbage", text=True,
                           env=dict(os.environ), capture_output=True)
        assert r.returncode == 0, "fail open rc"
        assert "JSONDecodeError" in open(os.path.join(tmp, "log", "_state", "session-log-errors.log")).read(), "err log"
        print("SELFTEST OK")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        sys.exit(selftest())
    sys.exit(main())
