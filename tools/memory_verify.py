#!/usr/bin/env python3
"""memory_verify.py - read-only health check of the agent's memory and wiring. Run at every /wrap and in CI.

    python3 tools/memory_verify.py [--quick]

Checks (each prints PASS / FAIL / WARN with the evidence):
   1 lint          every memory's frontmatter is valid               (tools/memory_lint.py)
   2 index         memory/_gen/ matches what memory_index.py would write now (memory_index.py --check; stale = WARN:
                   the next session rebuilds it; any other exit - a crashing or refusing indexer - is a FAIL)
   3 tier0         tier 0 fits the configured SessionStart parts      (load-persona.sh check N, N = common.tier0_parts)
   4 agents        generated role agents match agents-src + lessons  (agent_gen.py --check)
   5 leaks         nothing personal or secret is committable          (leak_scan.py; WARN without a local name list)
   6 gitignore     generated/local paths ARE ignored, and team paths (.claude/skills, agents, hooks...) are NOT
   7 size          no file in memory/ is over 64 KB (memory/_gen excluded; symlinks are check 12's)
   8 hooks         settings.json parses; every hook script it names exists and compiles (py: ast, sh: bash -n)
   9 persona       agent/ has no template placeholders left           (WARN only)
  10 policy        no user/local/managed setting disables project hooks (WARN only: disableAllHooks, allowManagedHooksOnly)
  11 paths         no control character in any path git would commit, nor in any name under memory/ or agent/
                   (a file NAME with a newline forges tier-0 text)
  12 symlinks      no symlink under memory/, agent/ or .claude/agent-memory/ - on disk or committed (git mode 120000)
  13 suspect       no description/trigger shaped like a command or an instruction (memory_lint SUSPECT warnings;
                   WARN only: a reviewer decides)
Every detail printed is escaped (common.printable): names in this repo are untrusted text.
--quick skips 5 (the slowest). Never writes anything. Exit 0 when nothing FAILs, 1 otherwise, 64 on a usage error.
"""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
from common import read_text, repo_root  # noqa: E402

ROOT = repo_root()
PY = sys.executable or "python3"


def run(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 99, "%s: %s" % (type(e).__name__, e)


def run_bytes(cmd, timeout=60):
    """(exit code, raw stdout bytes): for `git ... -z`, whose names may hold any byte."""
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout
    except Exception:
        return 99, b""


LINK_DIRS = ("memory", "agent", os.path.join(".claude", "agent-memory"))


def walk_names(top):
    """(path relative to ROOT, is a symlink) for every entry under ROOT/top, never descending into a link."""
    base = os.path.join(ROOT, top)
    if os.path.islink(base):
        return [(top, True)]
    out = []
    for d, dirs, files in os.walk(base):
        for x in dirs + files:
            p = os.path.join(d, x)
            out.append((os.path.relpath(p, ROOT).replace(os.sep, "/"), os.path.islink(p)))
    return out


def tail(text, k=3):
    lines = [l for l in text.splitlines() if l.strip()]
    return " | ".join(lines[-k:]) if lines else "(no output)"


def main(argv=None):
    ap = common.ArgParser(prog="memory_verify.py", description="Read-only health check of memory and wiring.")
    ap.add_argument("--quick", action="store_true", help="skip check 5 (the leak scan, the slowest)")
    quick = ap.parse_args(argv).quick
    results = []                      # per call: an importer that runs main() twice must not see the first run's

    def report(n, name, status, detail):
        results.append(status)
        print(common.printable("%2d %-10s %-4s %s" % (n, name, status, detail)))

    rc, lint_out = run([PY, "-I", "tools/memory_lint.py"])
    report(1, "lint", "PASS" if rc == 0 else "FAIL", tail(lint_out))

    # --check, not --print: --print exits 0 even when the indexer crashes, which read as merely "stale"
    rc, out = run([PY, "-I", "tools/memory_index.py", "--check", "--quiet"])
    if rc == 0:
        report(2, "index", "PASS", "fresh")
    elif rc == 1 and "--check: STALE" in out:     # a crash also exits 1: only the indexer's own verdict is "stale"
        report(2, "index", "WARN", "%s - run python3 tools/memory_index.py" % tail(out, 1))
    else:
        report(2, "index", "FAIL", "memory_index.py --check exited %d: %s" % (rc, tail(out, 2)))

    n = common.tier0_parts(ROOT, default=None)
    if not n:
        report(3, "tier0", "FAIL", "no load-persona.sh K N hooks found in .claude/settings.json")
    else:
        rc, out = run(["bash", ".claude/hooks/load-persona.sh", "check", str(n)])
        head = out.splitlines()[0] if out else "(no output)"
        report(3, "tier0", "PASS" if rc == 0 else "FAIL", head)

    rc, out = run([PY, "-I", "tools/agent_gen.py", "--check"])
    report(4, "agents", "PASS" if rc == 0 else "FAIL", tail(out, 4).replace("\n", " "))

    if quick:
        report(5, "leaks", "WARN", "skipped (--quick)")
    else:
        rc, out = run([PY, "-I", "tools/leak_scan.py"], timeout=120)
        # with no local pattern list only the generic patterns ran: names were never looked for, so not a PASS
        no_local = "no tools/leak_patterns.txt" in out
        report(5, "leaks", "FAIL" if rc != 0 else ("WARN" if no_local else "PASS"),
               tail(out, 6) + (" - names were NOT checked on this clone" if rc == 0 and no_local else ""))

    # Both halves, or the check has no positive control: some paths MUST be ignored, others must NOT be (a member's
    # global excludes once silently dropped .claude/skills/ from a commit while this check said PASS). Run with THIS
    # machine's excludes on: a commit made here uses them, so that is what decides what reaches the repo.
    must_ignore = ["memory/_gen/INDEX.md", ".claude/state/x", "tools/leak_patterns.txt", ".claude/settings.local.json"]
    must_commit = [".claude/settings.json", ".claude/skills/wrap/SKILL.md", ".claude/skills/team-lead/SKILL.md",
                   ".claude/agents/memory-librarian.md", ".claude/agents/reviewer.md",
                   ".claude/agent-memory/reviewer/MEMORY.md", ".claude/hooks/load-persona.sh",
                   "memory/lessons/x.md", "memory/log/x.md", "agent/IDENTITY.md"]
    rc, out = run(["git", "check-ignore", "--no-index"] + must_ignore + must_commit)
    ignored = set(l.strip() for l in out.splitlines())
    leaked = [p for p in must_ignore if p not in ignored]
    dropped = [p for p in must_commit if p in ignored]
    problems = (["NOT ignored: %s" % ", ".join(leaked)] if leaked else []) + \
               (["IGNORED but must be committed: %s (a global git exclude? see .gitignore)" % ", ".join(dropped)]
                if dropped else [])
    report(6, "gitignore", "FAIL" if problems else "PASS",
           "; ".join(problems) or "%d must-ignore ignored, %d must-commit committable" % (len(must_ignore), len(must_commit)))

    big = []
    mem_root = os.path.join(ROOT, "memory")
    for d, dirs, names in os.walk(mem_root):
        if d == mem_root:             # skip memory/_gen only; a substring test also skipped memory/x_gen/
            dirs[:] = [x for x in dirs if x != "_gen"]
        for nm in names:
            p = os.path.join(d, nm)
            size = os.lstat(p).st_size      # lstat: never follow a link (a dangling one made getsize raise)
            if not os.path.islink(p) and size > 64 * 1024:
                big.append("%s (%d KB)" % (os.path.relpath(p, ROOT), size // 1024))
    report(7, "size", "PASS" if not big else "FAIL", "all under 64 KB" if not big else ", ".join(big))

    try:
        s = json.loads(read_text(os.path.join(ROOT, ".claude", "settings.json")))
        absent, broken, seen = [], [], set()
        for ev, arr in s.get("hooks", {}).items():
            for m in arr:
                for h in m.get("hooks", []):
                    for rel in re.findall(r"\$CLAUDE_PROJECT_DIR/([^\"\s]+)", h.get("command", "")):
                        p = os.path.join(ROOT, rel)
                        if not os.path.isfile(p):
                            absent.append("%s: %s" % (ev, rel))
                        elif rel not in seen:
                            seen.add(rel)
                            # a hook with a syntax error fails OPEN on every call - silently. Compile it here.
                            cmd = ["bash", "-n", rel] if rel.endswith(".sh") else \
                                  [PY, "-c", "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())", rel]
                            rc, out = run(cmd)
                            if rc != 0:
                                broken.append("%s (%s)" % (rel, tail(out, 1)))
        bad = ["missing: " + "; ".join(absent)] if absent else []
        bad += ["does not compile: " + "; ".join(broken)] if broken else []
        report(8, "hooks", "FAIL" if bad else "PASS",
               " | ".join(bad) or "%d events wired, %d scripts present and compile" % (len(s.get("hooks", {})), len(seen)))
    except Exception as e:
        report(8, "hooks", "FAIL", "settings.json: %s" % e)

    left = []
    for f in ("IDENTITY.md", "SOUL.md", "TEAM.md"):
        p = os.path.join(ROOT, "agent", f)
        if os.path.isfile(p) and "{{" in read_text(p):
            left.append(f)
    report(9, "persona", "PASS" if not left else "WARN",
           "initialised" if not left else "placeholders in %s - run python3 tools/init_agent.py" % ", ".join(left))

    # 10: settings that switch every project hook off. Then there is no persona, no gate and no guard - and nothing
    # in the session says so. Known managed-settings locations per OS plus the user/local layers; WARN only, because
    # an admin's policy is not this repo's to fail.
    layers = [os.path.expanduser("~/.claude/settings.json"), os.path.join(ROOT, ".claude", "settings.local.json"),
              "/etc/claude-code/managed-settings.json",
              "/Library/Application Support/ClaudeCode/managed-settings.json",
              os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "ClaudeCode", "managed-settings.json")]
    off = []
    for p in layers:
        try:
            if os.path.isfile(p):
                s = json.loads(read_text(p))
                for key in ("disableAllHooks", "allowManagedHooksOnly"):
                    if s.get(key) is True:
                        off.append("%s in %s" % (key, p))
        except Exception as e:
            off.append("unreadable %s (%s)" % (p, type(e).__name__))
    report(10, "policy", "WARN" if off else "PASS",
           ("project hooks may be OFF: " + "; ".join(off)) if off else "no setting found that disables project hooks")

    # 11: a control character in a path. git's list covers everything a commit would carry; the walk covers ignored
    # and untracked-but-read files under memory/ and agent/ (the tools and the loader read those, committed or not).
    rc, raw = run_bytes(["git", "ls-files", "-z", "-c", "-o", "--exclude-standard"])
    paths = set(p.decode("utf-8", "replace") for p in raw.split(b"\0") if p) if rc == 0 else set()
    for top in ("memory", "agent"):
        paths.update(rel for rel, _ in walk_names(top))
    ctl = sorted(common.shown(p) for p in paths if common.has_control(p))
    report(11, "paths", "FAIL" if ctl else "PASS",
           ("control character in %d path(s): %s" % (len(ctl), ", ".join(ctl[:5]))) if ctl else
           "%d paths, no control characters%s" % (len(paths), "" if rc == 0 else " (not a git repo: walk only)"))

    # 12: symlinks where the tools read memory and persona text. On disk (islink) and committed (mode 120000: on a
    # clone with core.symlinks=false a committed link is a plain file holding the target path, invisible to islink).
    links = set(rel for top in LINK_DIRS for rel, is_link in walk_names(top) if is_link)
    rc, raw = run_bytes(["git", "ls-files", "-s", "-z", "--"] + [t.replace(os.sep, "/") for t in LINK_DIRS])
    for entry in raw.split(b"\0") if rc == 0 else []:
        if entry.startswith(b"120000 "):
            links.add(entry.split(b"\t", 1)[-1].decode("utf-8", "replace"))
    links = sorted(common.shown(l) for l in links)
    report(12, "symlinks", "FAIL" if links else "PASS",
           ("symlink(s), never followed - replace with real files: %s" % ", ".join(links[:5])) if links else
           "none under memory/, agent/, .claude/agent-memory/")

    # 13: description/trigger text shaped like a command or an instruction to the agent. Tier 0 carries it into
    # every session; WARN only, because real memories cite URLs - a reviewer decides.
    suspect = [l.split(":", 1)[0].replace("WARN", "").strip() for l in lint_out.splitlines()
               if l.startswith("WARN") and "SUSPECT" in l]
    report(13, "suspect", "WARN" if suspect else "PASS",
           ("%d field(s) look like a command or an instruction - review: %s" % (len(suspect), ", ".join(suspect[:5])))
           if suspect else "no description or trigger shaped like a command or an instruction")

    fails = results.count("FAIL")
    print("memory_verify: %d PASS, %d WARN, %d FAIL" % (results.count("PASS"), results.count("WARN"), fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
