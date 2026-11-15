#!/usr/bin/env python3
"""git-guard.py - PreToolUse hook (matcher: Bash). Blocks git commands that discard uncommitted work, and process
kills by NAME or window filter.

Why: an agent that "reverts its own edit" with `git checkout -- <file>` on a tree that also holds a member's
uncommitted work wipes that work too. A rule written in prose does not stop it; a hook does. It applies to the lead
session AND to teammates (PreToolUse hooks fire in teammate sessions).

Blocks, when the target repo has uncommitted changes the command would destroy:
  git checkout -- <path> | checkout . | checkout -f | checkout [<ref>] <existing-or-dirty path>   (no -- needed)
  git restore <path>          (except --staged alone, which only touches the index)
  git stash / stash push / stash save     (sets work aside where an agent forgets it)
  git reset --hard
  git switch -f / --discard-changes
  git rm -f / --force
  git clean -f / -fd ...      (when untracked files exist)
Always blocks: git stash drop / stash clear (a stash is often the only copy).

Hardening: paths are normalised before tokenising; commands are split on ; && || | & ( ) backticks and $( ; wrappers
(env, exec, command, timeout, xargs, eval, bash -c, pwsh -Command, shell keywords) are stripped and the inner command
re-scanned; -C / --git-dir / --work-tree / GIT_WORK_TREE resolve the repo; `checkout <file>` without `--` is caught;
a DESTRUCTIVE command whose repo state cannot be read is BLOCKED; heredoc bodies (commit messages) are ignored.

Restore from a COPY instead (cp the file aside before editing, cp it back). If the discard is truly intended and
checked, add the literal marker `# git-guard-ok` to the command; overrides are logged to
.claude/state/git-guard-overrides.log. Internal errors fail OPEN but are logged to .claude/state/git-guard-errors.log.

Review rule: a `git commit` carrying REVIEW_MIN_LINES or more changed lines is blocked unless a reviewer / verifier /
adversary agent was dispatched in this session after the last SUCCESSFUL commit (read from the session transcript).
"Every build gets an independent review" written as prose gets skipped exactly when a build feels finished. The count
comes from the PARSED commit, never a regex over the command text: a pathspec commit (`git commit -m x a.py`) is counted
for those paths' working-tree state, -a / --all count every tracked change, a real `git add` earlier in the same
command line adds exactly what git's own `add --dry-run` says it stages (NEW files at their full length; an unrelated
unstaged file is not counted), --pathspec-from-file counts the whole tracked tree, and a word such as "add" inside the
commit message changes nothing. An unreadable transcript or line count, and a commit whose repo cannot be resolved
(`cd $VAR && git commit`), fail open (logged). `# no-review-ok: <reason>` overrides (logged to git-guard-overrides.log).
Known limits, not chased: git aliases (`git ci`), and merge / cherry-pick / am / rebase, which commit without `commit`.

Self-test (builds a real temporary repo): python3 git-guard.py test
"""
import atexit
import datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _hooklib import append_log, fail_open, read_payload  # noqa: E402
SPLIT = re.compile(r"\s*(?:&&|\|\||;|\||&|\n|\(|\)|`|\$\()\s*")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1[^\n]*\n.*?\n\s*\2\s*(?:\n|$)", re.S)
PS_HERESTRING = re.compile(r"@'\n.*?\n'@|@\"\n.*?\n\"@", re.S)
WRAPPERS = {"env", "exec", "command", "sudo", "time", "nice", "nohup", "call", "&", ".",

            "then", "do", "else", "elif", "if", "while", "until", "{", "}", "!"}
XARGS_ARGFLAGS = {"-n", "-I", "-L", "-P", "-d", "-E", "-s", "-a"}
SHELLS = {"bash", "sh", "bash.exe", "sh.exe", "pwsh", "powershell", "pwsh.exe", "powershell.exe", "cmd", "cmd.exe"}
SHELL_CMD_FLAGS = {"-c", "-command", "/c", "-lc"}


CREDENTIAL_IN_URL = re.compile(r"(://)[^/@\s]+@")


def log(name, line):
    # local, gitignored, rotated; still: never keep a credential that rode along in a URL (https://token@host)
    append_log(name, datetime.datetime.now().isoformat(timespec="seconds") + " " + CREDENTIAL_IN_URL.sub(r"\1***@", line))


def winpath(p):
    """Strip quotes; backslashes -> slashes; on Windows only, /c/dir/x -> C:/dir/x (Git Bash drive spelling).
    On Linux /x/... is an ordinary one-letter top-level directory and must stay as it is."""
    p = p.strip().strip("'\"").replace("\\", "/")
    m = re.match(r"^/([a-zA-Z])/(.*)$", p) if os.name == "nt" else None
    if m:
        p = m.group(1).upper() + ":/" + m.group(2)
    return p


# A backslash that ESCAPES something (a space, a quote, another backslash) is shell syntax and must reach shlex:
# `git checkout a\ b.js` names the file "a b.js". Any other backslash is a Windows path separator, which posix shlex
# would eat (C:\repo\a.js -> C:repoa.js), so it becomes "/". Rewriting every backslash let the escaped form through.
PATH_BACKSLASH = re.compile(r"\\(?![\s'\"\\])")


def words(seg):
    if os.name == "nt":                   # Windows: a lone backslash is a path separator. POSIX: it is an escape
        seg = PATH_BACKSLASH.sub("/", seg)   # (g\it == git), and shlex resolves it exactly as the shell will.
    try:
        return shlex.split(seg, posix=True)
    except ValueError:
        return seg.split()


UNRESOLVED = "<unresolved>"     # a cd target we cannot resolve: the repo is unknown, so a discard there must block


def resolve(base, target):
    """Expand ~ / $HOME / $env:USERPROFILE / %USERPROFILE% and refuse to guess a cd target
    that does not exist."""
    home = os.path.expanduser("~").replace("\\", "/")
    t = target.strip().strip("'\"").replace("\\", "/")
    t = re.sub(r"^~(?=/|$)", home, t)
    t = re.sub(r"(?i)\$\{?HOME\}?|\$env:USERPROFILE|%USERPROFILE%", home, t)
    if "$" in t or "%" in t:
        return UNRESOLVED                                   # another variable: cannot know where this lands
    t = winpath(t)
    if base == UNRESOLVED and not (os.path.isabs(t) or re.match(r"^[A-Za-z]:/", t)):
        return UNRESOLVED
    out = t if os.path.isabs(t) or re.match(r"^[A-Za-z]:/", t) else os.path.join(base or ".", t)
    return os.path.normpath(out).replace("\\", "/")


def classify(argv):
    """argv after 'git' (global options stripped) -> None, 'tracked', 'untracked', 'always' or 'checkout-paths'."""
    if not argv:
        return None
    sub, rest = argv[0], argv[1:]
    flags = [a for a in rest if a.startswith("-")]
    if any(f.startswith("--pathspec-from-file") for f in flags) and sub in ("checkout", "restore"):
        return "tracked"
    if sub == "checkout-index":
        return "tracked" if ("-f" in flags or "--force" in flags) else None
    if sub == "read-tree":
        return "tracked" if ("-u" in flags and ("--reset" in flags or "-m" in flags)) else None
    if sub == "checkout":
        if "--" in rest or "." in rest or "-f" in flags or "--force" in flags:
            return "tracked"
        return "checkout-paths" if [a for a in rest if not a.startswith("-")] else None
    if sub == "restore":
        if "--staged" in flags and "--worktree" not in flags and "-W" not in flags:
            return None
        return "tracked"
    if sub == "stash":
        op = rest[0] if rest and not rest[0].startswith("-") else "push"
        if op in ("drop", "clear"):
            return "always"
        if op in ("push", "save"):
            return "tracked"
        return None                      # list, show, apply, pop, branch
    if sub == "worktree":                 # `worktree remove --force` deletes a worktree with its uncommitted changes
        if rest and rest[0] == "remove" and ("-f" in flags or "--force" in flags):
            return "always"
        return None
    if sub == "reset":
        return "tracked" if "--hard" in flags else None
    if sub == "switch":
        return "tracked" if ("-f" in flags or "--force" in flags or "--discard-changes" in flags) else None
    if sub == "rm":
        return "tracked" if ("-f" in flags or "--force" in flags) else None
    if sub == "clean":
        if any(a.startswith("-") and not a.startswith("--") and "f" in a for a in flags) or "--force" in flags:
            if "-n" in flags or "--dry-run" in flags:
                return None
            return "untracked"
        return None
    return None


def git_calls(cmd, cwd, depth=0):
    """Yield (repo_dir, argv_after_git, raw_segment) for every git invocation, following cd / Set-Location."""
    cmd = PS_HERESTRING.sub("\n", HEREDOC.sub("\n", cmd))     # message bodies are data, not commands
    here = cwd
    for seg in SPLIT.split(cmd):
        w = words(seg)
        env_tree = None
        while w:
            if w[0].lower() in ("eval", "iex", "invoke-expression") and depth < 3:
                yield from git_calls(" ".join(w[1:]), here, depth + 1)
                w = []
            elif w[0].lower() in ("timeout", "stdbuf"):                          # `timeout 5 git ...`, `stdbuf -o0 git ...`
                w = w[1:]
                while w and (w[0].startswith("-") or re.match(r"^\d+(\.\d+)?[smhd]?$", w[0])):
                    w = w[1:]
            elif w[0].lower() in WRAPPERS or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w[0]):
                m = re.match(r"^GIT_WORK_TREE=(.+)$", w[0])
                if m:
                    env_tree = m.group(1)
                w = w[1:]
            elif os.path.basename(w[0]).lower() in ("xargs", "xargs.exe"):   # `... | xargs git checkout --`
                w = w[1:]
                while w and w[0].startswith("-"):
                    w = w[2:] if w[0] in XARGS_ARGFLAGS else w[1:]
            else:
                break
        if not w:
            continue
        head = os.path.basename(w[0]).lower()
        if head in ("cd", "set-location", "pushd", "push-location", "sl", "chdir") and len(w) > 1:
            here = resolve(here, w[-1])
            continue
        if head in SHELLS and depth < 3:
            for i, a in enumerate(w[1:], 1):
                if a.lower() in SHELL_CMD_FLAGS and i + 1 < len(w):
                    yield from git_calls(" ".join(w[i + 1:]), here, depth + 1)
                    break
            continue
        if head not in ("git", "git.exe"):
            continue
        argv, repo = w[1:], (resolve(here, env_tree) if env_tree else here)
        while argv and argv[0].startswith("-"):
            a = argv[0]
            if a == "-C" and len(argv) > 1:
                repo = resolve(repo, argv[1]); argv = argv[2:]
            elif a in ("-c", "--git-dir", "--work-tree", "--namespace") and len(argv) > 1:
                if a == "--work-tree": repo = resolve(repo, argv[1])
                if a == "--git-dir": repo = resolve(repo, os.path.dirname(winpath(argv[1]).rstrip("/")) or ".")
                argv = argv[2:]
            elif a.startswith("--work-tree="):
                repo = resolve(repo, a.split("=", 1)[1]); argv = argv[1:]
            elif a.startswith("--git-dir="):
                repo = resolve(repo, os.path.dirname(winpath(a.split("=", 1)[1]).rstrip("/")) or "."); argv = argv[1:]
            else:
                argv = argv[1:]
        yield repo, argv, seg


def dirt(repo):
    """-> (tracked_changes, untracked_files) | 'notrepo' | None (state could not be read)."""
    if repo == UNRESOLVED:
        return None                                         # unknown location: block a discard rather than guess
    if repo and not os.path.isdir(repo):
        return "notrepo"
    try:
        out = subprocess.run(["git", "-C", repo or ".", "status", "--porcelain"], capture_output=True,
                             text=True, timeout=8, stdin=subprocess.DEVNULL)
    except Exception:
        return None
    if out.returncode != 0:
        return "notrepo" if "not a git repository" in (out.stderr or "").lower() else None
    lines = [l for l in out.stdout.splitlines() if l.strip()]
    return [l[3:] for l in lines if not l.startswith("??")], [l[3:] for l in lines if l.startswith("??")]


def touches(repo, args, tracked):
    """checkout without `--`: does any non-flag argument cover a DIRTY file (the file itself, a dir above it, or .)?
    An existing but clean path loses nothing, and a branch name is not a path."""
    for a in [x for x in args if not x.startswith("-")]:
        p = winpath(a).rstrip("/")
        while p.startswith("./"):
            p = p[2:]
        if p in (".", "*", ":/", ""):
            return True
        if p.startswith(":") or any(c in p for c in "*?["):   # pathspec magic / globs: assume it covers dirt
            return True

        base = os.path.normcase(os.path.normpath(repo or "."))
        ap = os.path.normcase(os.path.normpath(os.path.join(repo or ".", p)))
        for t in tracked:
            at = os.path.normcase(os.path.normpath(os.path.join(base, t.strip('"'))))
            if at == ap or at.startswith(ap.rstrip("\\/") + os.sep):
                return True
    return False


KILL_BROAD = re.compile(
    r"(?i)\btaskkill(\.exe)?\b[^\n;&|]*(/IM\b|/FI\b)"            # taskkill by image name or filter
    r"|\bStop-Process\b[^\n;&|]*-Name\b"                        # Stop-Process -Name chrome
    r"|\b(pkill|killall)\b"                                     # name-based kill on POSIX shells
    r"|Get-Process\b[^\n;&|]*\|\s*Stop-Process")                # Get-Process chrome | Stop-Process


def kill_verdict(cmd):
    """Kill by the PID you started. A by-name or window-filter kill also matches processes a member is using."""
    if "# kill-ok" in cmd or not KILL_BROAD.search(cmd):
        return None
    return ("BLOCKED by git-guard (process rule): this kills processes by NAME or window filter, which also matches "
            "processes the member is using. Kill by the PID you started (kill <pid>), or give headless tools their own "
            "--user-data-dir and stop that process tree. If a by-name kill is truly intended and the member asked for "
            "it, add '# kill-ok' to the command.")


# ---- review rule ----------------------------------------------------------------------------------------------------
REVIEW_MIN_LINES = 150
REVIEW_AGENTS = {"reviewer", "verifier", "adversary"}
# `git commit` options that consume the NEXT argument when no value is attached (every other option is a flag).
COMMIT_VALUE_LONG = {"--message", "--file", "--reuse-message", "--reedit-message", "--author", "--date", "--fixup",
                     "--squash", "--cleanup", "--template", "--trailer", "--pathspec-from-file"}
COMMIT_VALUE_SHORT = set("mFCct")


ADD_INTERACTIVE = {"-p", "--patch", "-i", "--interactive", "-e", "--edit"}   # dropped from the dry run (they prompt)


def _git(repo, args):
    """stdout of `git -C repo <args>` (bytes), or None on any failure."""
    try:
        r = subprocess.run(["git", "-C", repo, "-c", "core.quotepath=false"] + list(args), capture_output=True,
                           timeout=15, stdin=subprocess.DEVNULL)
    except Exception:
        return None
    return r.stdout if r.returncode == 0 else None


def numstat(repo, args):
    """{top-level-relative path: added + deleted lines} for `git diff <args>` (binary files count 0); None if unreadable."""
    out = _git(repo, ["diff", "--numstat", "-z", "--no-renames"] + list(args))
    if out is None:
        return None
    res = {}
    for rec in out.decode("utf-8", "replace").split("\0"):
        p = rec.split("\t", 2)
        if len(p) == 3:
            res[p[2]] = (int(p[0]) if p[0].isdigit() else 0) + (int(p[1]) if p[1].isdigit() else 0)
    return res


def add_set(repo, add_argv):
    """Top-level-relative paths a `git add` (argv after 'add') would stage, from git's own dry run - so -A, -u, ., a
    directory, a glob and --pathspec-from-file mean exactly what git makes of them. Empty when the dry run fails."""
    args = [a for a in add_argv if a not in ADD_INTERACTIVE and a not in ("-n", "--dry-run")]
    out = _git(repo, ["add", "--dry-run", "--ignore-missing"] + args)
    paths = set()
    for line in (out or b"").decode("utf-8", "replace").splitlines():
        for verb in ("add '", "remove '"):
            if line.startswith(verb) and line.endswith("'"):
                paths.add(line[len(verb):-1])
    return paths


def file_lines(path):
    """Line count of a NEW file as `git diff` would count it (a NUL byte in the first 8 KB = binary = 0)."""
    n, last, first = 0, b"\n", True
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                if first and b"\0" in chunk[:8192]:
                    return 0
                first = False
                n += chunk.count(b"\n")
                last = chunk[-1:]
    except OSError:
        return 0
    return n + (last != b"\n")


def commit_lines(repo, shape, add_argvs):
    """Added + deleted lines the commit will carry, or None when unreadable (no HEAD, git error).
    shape = commit_shape(argv); add_argvs = argvs of the `git add` calls that run BEFORE it in the same command line.
    - plain commit: what is staged, plus what those adds stage (tracked files at their worktree state, NEW files at
      their full length - `git diff HEAD` never lists an untracked file, so a build made of new files counted 0);
    - -a / --all: every tracked change, plus the new files the adds stage;
    - pathspec commit (--only, the default): those paths' WORKING-TREE state whatever the index holds, plus new files
      under them that the adds stage; --include adds the index; --pathspec-from-file counts the whole tracked tree.
    Files outside the adds' pathspecs are never counted: an unrelated unstaged change does not block a small commit."""
    all_flag, include, paths, from_file = shape
    head = numstat(repo, ["HEAD"])
    top = _git(repo, ["rev-parse", "--show-toplevel"])
    if head is None or top is None:
        return None
    top = top.decode("utf-8", "replace").strip()
    staged_by_add = set()
    for av in add_argvs:
        staged_by_add |= add_set(repo, av)
    new = {p: file_lines(os.path.join(top, p)) for p in staged_by_add
           if p not in head and os.path.isfile(os.path.join(top, p))}
    if from_file:
        carry = dict(head)
    elif paths and not include:
        carry = numstat(repo, ["HEAD", "--"] + list(paths))
        under = _git(repo, ["ls-files", "-o", "--exclude-standard", "--full-name", "-z", "--"] + list(paths))
        if carry is None or under is None:
            return None
        under = set(under.decode("utf-8", "replace").split("\0"))
        new = {p: n for p, n in new.items() if p in under}
    else:
        carry = numstat(repo, ["--cached"])
        if carry is None:
            return None
        if all_flag:
            carry.update(head)
        carry.update({p: head[p] for p in staged_by_add if p in head})
        if paths:
            more = numstat(repo, ["HEAD", "--"] + list(paths))
            if more is None:
                return None
            carry.update(more)
    carry.update(new)
    return sum(carry.values())


def is_commit(cmd):
    """A real `git commit` call, parsed like every other git call (a regex also matched `grep commit git-guard.py`)."""
    if "commit" not in cmd:
        return False
    try:
        return any(argv and argv[0] == "commit" for _, argv, _ in git_calls(cmd, "."))
    except Exception:
        return False


def reviewed_since_commit(transcript):
    """True when a review agent was dispatched (and the dispatch was not refused) after the last SUCCESSFUL git commit
    in this transcript. A commit the guard blocked does not count, or the retry after the review would block forever.
    None when the transcript cannot be read."""
    try:
        fh = open(transcript, "r", encoding="utf-8", errors="replace")
    except Exception:
        return None
    commits, reviews, pos, last_commit, last_review = set(), set(), 0, -1, -1
    with fh:
        for line in fh:
            pos += 1
            if "tool_" not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            content = (rec.get("message") or {}).get("content") if isinstance(rec, dict) else None
            if not isinstance(content, list):
                continue
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use":
                    inp = c.get("input") if isinstance(c.get("input"), dict) else {}
                    if c.get("name") in ("Agent", "Task") and inp.get("subagent_type") in REVIEW_AGENTS:
                        reviews.add(c.get("id"))
                    elif c.get("name") in ("Bash", "PowerShell") and is_commit(str(inp.get("command") or "")):
                        commits.add(c.get("id"))
                elif c.get("type") == "tool_result" and not c.get("is_error"):
                    tid = c.get("tool_use_id")
                    if tid in reviews:
                        last_review = pos
                    elif tid in commits and "BLOCKED by git-guard" not in json.dumps(c.get("content")):
                        last_commit = pos
    return last_review > last_commit


def commit_shape(argv):
    """(all_flag, include_flag, pathspecs, pathspec_from_file) from a parsed `git commit` argv. Parsed, never regexed
    off the raw command: a regex for 'add' matched the word inside the commit MESSAGE."""
    all_flag = include = from_file = False
    paths, i, args = [], 0, argv[1:]
    while i < len(args):
        a = args[i]
        if a == "--":
            paths.extend(args[i + 1:])
            break
        if a.startswith("--"):
            name = a.split("=", 1)[0]
            if name == "--all":
                all_flag = True
            elif name == "--include":
                include = True
            if name == "--pathspec-from-file":
                from_file = True
            if name in COMMIT_VALUE_LONG and "=" not in a:
                i += 1
        elif a.startswith("-") and len(a) > 1:
            for j, ch in enumerate(a[1:]):
                if ch == "a":
                    all_flag = True
                elif ch == "i":
                    include = True
                elif ch in COMMIT_VALUE_SHORT:
                    if j == len(a) - 2:          # value is the next argument (-m "x", -am "x")
                        i += 1
                    break                        # anything after the letter is its attached value (-m"x")
        else:
            paths.append(a)
        i += 1
    return all_flag, include, paths, from_file


def same_dir(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def review_verdict(cmd, cwd, transcript, lines_fn=commit_lines, reviewed_fn=reviewed_since_commit):
    """-> None (allow) or the block message for a large unreviewed commit."""
    if "# no-review-ok" in cmd:
        return None
    calls = list(git_calls(cmd, cwd))
    for k, (repo, argv, seg) in enumerate(calls):
        if not argv or argv[0] != "commit":
            continue
        if repo == UNRESOLVED:
            log("git-guard-errors.log", "review rule: commit repo unresolved (cd to a variable?), not counted")
            continue
        # `git add` calls that run BEFORE this commit in the same repo stage more; one whose repo is unknown is
        # counted as `git add -A` here (over-count rather than miss)
        adds = [["-A"] if r == UNRESOLVED else av[1:] for r, av, _ in calls[:k]
                if av and av[0] == "add" and (r == UNRESOLVED or same_dir(r, repo))]
        n = lines_fn(repo, commit_shape(argv), adds)
        if n is None:
            log("git-guard-errors.log", "review rule: line count unreadable, commit allowed")
            continue
        if n < REVIEW_MIN_LINES:
            continue
        rv = reviewed_fn(transcript) if transcript else None
        if rv is None:
            log("git-guard-errors.log", "review rule: transcript unreadable, commit allowed")
            continue
        if rv:
            continue
        return ("BLOCKED by git-guard (review rule): this commit carries %d changed lines and no reviewer / verifier / "
                "adversary agent has been dispatched in this session since the last commit. Every code build gets an "
                "independent review. Dispatch Agent(subagent_type=\"reviewer\", name=..., model=...) on the diff, apply "
                "its findings, then commit. If a review is truly not warranted (generated data, a revert, a member "
                "said ship it), add '# no-review-ok: <reason>' to the command (logged)." % n)
    return None


def verdict(cmd, cwd, dirt_fn=dirt):
    """-> None (allow) or the block message."""
    k = kill_verdict(cmd)
    if k:
        return k
    if "# git-guard-ok" in cmd:
        return None
    for repo, argv, seg in git_calls(cmd, cwd):
        kind = classify(argv)
        if kind is None:
            continue
        if kind == "always":
            return ("BLOCKED by git-guard: `" + seg.strip()[:80] + "` deletes set-aside work (a stash, or a worktree "
                    "with its uncommitted changes), which is often the only copy. If you have checked it is safe, add "
                    "'# git-guard-ok' to the command.")
        d = dirt_fn(repo)
        if d == "notrepo":
            continue                                  # git itself will refuse; nothing to protect
        if d is None:
            return ("BLOCKED by git-guard: `" + seg.strip()[:80] + "` can discard work, and the state of " + str(repo) +
                    " could not be read (git status failed or timed out). Check `git status` yourself; if the discard "
                    "is intended, add '# git-guard-ok' to the command.")
        tracked, untracked = d
        if kind == "checkout-paths":
            if not tracked or not touches(repo, argv[1:], tracked):
                continue
            kind = "tracked"
        hit = tracked if kind == "tracked" else untracked
        if not hit:
            continue
        shown = ", ".join(hit[:6]) + (" ..." if len(hit) > 6 else "")
        return ("BLOCKED by git-guard: `" + seg.strip()[:80] + "` would discard UNCOMMITTED work in " + str(repo) +
                " (" + str(len(hit)) + " changed: " + shown + "). This tree may hold a member's "
                "uncommitted work next to yours. To undo "
                "YOUR edit, restore from a copy you took before editing (cp file file.bak first, cp back after), or "
                "re-apply the old text with Edit. If discarding is truly intended and checked, add '# git-guard-ok' "
                "to the command (audited).")
    return None


def selftest():
    """Against a REAL temporary repo with a modified tracked file and an untracked one."""
    tmp = tempfile.mkdtemp(prefix="gitguard-")
    atexit.register(shutil.rmtree, tmp, True)                # removed even if a case raises
    repo = os.path.join(tmp, "repo")
    os.makedirs(os.path.join(repo, "sub"))
    os.makedirs(os.path.join(repo, "my dir"))
    run = lambda *a: subprocess.run(["git", "-C", repo] + list(a), capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)
    subprocess.run(["git", "init", "-q", repo], capture_output=True, stdin=subprocess.DEVNULL, timeout=30)
    run("config", "user.email", "t@t"); run("config", "user.name", "t")
    for f in ("a.js", "sub/b.js", "a b.js", "my dir/c.js"):
        with open(os.path.join(repo, f), "w") as fh:
            fh.write("v1\n")
    run("add", "-A"); run("commit", "-qm", "init")
    for f in ("a.js", "a b.js", "my dir/c.js"):
        with open(os.path.join(repo, f), "w") as fh:
            fh.write("v2 uncommitted\n")
    open(os.path.join(repo, "new.txt"), "w").write("untracked\n")
    clean = os.path.join(tmp, "clean")
    subprocess.run(["git", "init", "-q", clean], capture_output=True, stdin=subprocess.DEVNULL, timeout=30)
    W = repo.replace("/", "\\")                 # Windows spelling
    P = repo.replace("\\", "/")
    cases = [
        ("g''it checkout -- a.js", repo, True),                            # quotes inside the word still run git
        ('"git" checkout -- a.js', repo, True),
        ("g\\it checkout -- a.js", repo, os.name != "nt"),                 # POSIX escape; on Windows \ is a path sep
        ("git checkout a\\ b.js", repo, True),                             # escaped space (was a bypass)
        ("git checkout my\\ dir/c.js", repo, True),
        ('git checkout "a b.js"', repo, True),
        ("git checkout -- a\\ b.js", repo, True),
        ("git worktree remove --force ../wt", repo, True),                 # deletes a worktree's uncommitted work
        ("git worktree remove -f ../wt", repo, True),
        ("git worktree remove ../wt", repo, False),                        # git itself refuses a dirty worktree
        ("git worktree list", repo, False),
        ("git checkout -- a.js 2>/dev/null", repo, True),
        ("git checkout a.js", repo, True),                                  # no --
        ("git checkout HEAD a.js", repo, True),
        ("git checkout HEAD sub/", repo, False),                            # sub/ has no dirt
        ("git checkout .", repo, True),
        ("git checkout feature-branch", repo, False),                       # not a path
        ("Set-Location " + W + "; git checkout -- a.js", tmp, os.name == "nt"),  # Windows spelling: a backslash path exists only there
        ("git -C " + W + " checkout -- a.js", tmp, os.name == "nt"),
        ("git -C '" + W + "' checkout -- a.js", tmp, True),
        ("cd " + P + " && git checkout -- a.js", tmp, True),
        ("(cd " + P + " && git checkout -- a.js)", tmp, True),              # subshell (was a bypass)
        ("(git checkout -- a.js)", repo, True),
        ("& git checkout -- a.js", repo, True),                              # PowerShell call operator
        ("env FOO=1 git checkout -- a.js", repo, True),
        ("FOO=1 git checkout -- a.js", repo, True),
        ("bash -c 'git checkout -- a.js'", repo, True),
        ("pwsh -Command \"git checkout -- a.js\"", repo, True),
        ("git status & git checkout -- a.js", repo, True),
        ("git --git-dir=" + P + "/.git --work-tree=" + P + " checkout -- a.js", tmp, True),
        ("git restore a.js", repo, True),
        ("git restore -W a.js", repo, True),
        ("git restore --staged a.js", repo, False),
        ("git stash", repo, True),
        ("git stash list", repo, False),
        ("git stash pop", repo, False),
        ("git stash drop", clean, True),
        ("git reset --hard HEAD", repo, True),
        ("git reset HEAD a.js", repo, False),
        ("git rm -f a.js", repo, True),
        ("git clean -fd", repo, True),
        ("git clean -n", repo, False),
        ("git switch --discard-changes main", repo, True),
        ("git checkout -- a.js", clean, False),                              # clean repo: nothing to lose
        ("git checkout -- a.js", os.path.join(tmp, "nope"), False),          # not a repo: git refuses anyway
        ("git status --short; git diff --stat", repo, False),
        ("git add -A && git commit -m x", repo, False),
        ("git commit -F - <<'EOF'\nfix\n\ngit checkout -- a.js was the bug\nEOF\n", repo, False),   # heredoc message

        ("git checkout ./a.js", repo, True),
        ("git checkout ./sub", repo, False),                                 # clean dir
        ("if true; then git checkout -- a.js; fi", repo, True),
        ("{ git checkout -- a.js; }", repo, True),
        ("! git checkout -- a.js", repo, True),
        ("Push-Location " + W + "; git checkout -- a.js", tmp, os.name == "nt"),
        ("echo a.js | xargs git checkout --", repo, True),
        ("xargs -n 1 git checkout -- < list.txt", repo, True),
        ("git checkout '*.js'", repo, True),
        ("git checkout :/a.js", repo, True),
        ("git checkout --pathspec-from-file=list.txt", repo, True),
        ("git checkout-index -f -a", repo, True),
        ("git read-tree -u --reset HEAD", repo, True),
        ("for f in x; do echo $f; done", repo, False),

        ("git checkout sub/../a.js", repo, True),
        ("git checkout ./sub/./../A.JS", repo, os.name == "nt"),             # case-insensitive paths only on Windows
        ("cd ../repo && git checkout a.js", repo, True),
        ("cd $SOMEVAR && git checkout -- a.js", tmp, True),                  # unknown target blocks
        ("cd /no/such/dir; git checkout -- a.js", repo, False),              # exists-check: not a repo -> git refuses
        ("git checkout HEAD sub/./", repo, False),
        ("timeout 5 git checkout -- a.js", repo, True),
        ("stdbuf -o0 git checkout -- a.js", repo, True),
        ("eval 'git checkout -- a.js'", repo, True),
        ("iex \"git checkout -- a.js\"", repo, True),
        ("GIT_WORK_TREE=" + P + " GIT_DIR=" + P + "/.git git checkout -- a.js", tmp, True),

        ('taskkill //F //IM chrome.exe //FI "WINDOWTITLE eq "', repo, True),
        ("Stop-Process -Name chrome -Force", repo, True),
        ("Get-Process chrome | Stop-Process", repo, True),
        ("pkill -f headless", repo, True),
        ("Stop-Process -Id 24912 -Force", repo, False),
        ("taskkill /PID 24912 /F", repo, False),
        ("Stop-Process -Name chrome  # kill-ok", repo, False),
        ("git checkout -- a.js  # git-guard-ok", repo, False),
        ("echo git checkout -- a.js", repo, False),
    ]
    bad = 0
    for cmd, cwd, want in cases:
        got = verdict(cmd, cwd) is not None
        bad += got != want
        print(("ok  " if got == want else "BAD ") + repr(cmd[:70]), got)
    # unreadable state on a destructive command must BLOCK, not allow
    got = verdict("git checkout -- a.js", repo, lambda r: None) is not None
    bad += not got
    print(("ok  " if got else "BAD ") + "unreadable repo state blocks", got)
    bad += review_selftest(tmp, repo)
    shutil.rmtree(tmp, ignore_errors=True)
    print("failures:", bad)
    return 1 if bad else 0


def review_selftest(tmp, repo):
    """Review rule: real transcript files, fake line counts, plus the real line counter on the self-test repo."""
    bad = 0
    seq = [0]

    def transcript(*events):
        seq[0] += 1
        tr = os.path.join(tmp, "t%d.jsonl" % seq[0])
        with open(tr, "w", encoding="utf-8") as fh:
            for i, (kind, extra) in enumerate(events):
                if kind == "review":            # extra = (subagent_type, refused?)
                    use = {"type": "tool_use", "id": "a%d" % i, "name": "Agent", "input": {"subagent_type": extra[0]}}
                    res = {"type": "tool_result", "tool_use_id": "a%d" % i, "is_error": extra[1], "content": "ok"}
                else:                           # commit; extra = "ok" or "blocked"
                    use = {"type": "tool_use", "id": "c%d" % i, "name": "Bash", "input": {"command": "git commit -m x"}}
                    res = {"type": "tool_result", "tool_use_id": "c%d" % i, "is_error": extra == "blocked",
                           "content": "BLOCKED by git-guard (review rule)" if extra == "blocked" else "[main abc] x"}
                fh.write(json.dumps({"message": {"content": [use]}}) + "\n")
                fh.write(json.dumps({"message": {"content": [res]}}) + "\n")
        return tr

    big, small = (lambda r, sh, ad: 400), (lambda r, sh, ad: 20)
    # 20 lines staged, 400 in the working tree: anything that reaches past the index counts 400
    dirty = lambda r, sh, ad: 400 if (sh[0] or sh[2] or sh[3] or ad) else 20
    rev = ("review", ("reviewer", False))
    rcases = [
        ("big commit, no review ever", "git commit -m y", transcript(), big, True),
        ("small commit, no review", "git commit -m y", transcript(), small, False),
        ("big commit after a reviewer", "git commit -m y", transcript(rev), big, False),
        ("verifier counts too", "git commit -m y", transcript(("review", ("verifier", False))), big, False),
        ("a REFUSED reviewer spawn is not a review", "git commit -m y", transcript(("review", ("reviewer", True))), big,
         True),
        ("a librarian is not a review", "git commit -m y", transcript(("review", ("memory-librarian", False))), big,
         True),
        ("a review BEFORE the last commit does not cover the next", "git commit -m y", transcript(rev, ("commit", "ok")),
         big, True),
        ("review, blocked attempt, retry: allowed", "git commit -m y", transcript(rev, ("commit", "blocked")), big,
         False),
        ("override marker", "git commit -m y  # no-review-ok: generated data", transcript(), big, False),
        ("not a commit", "git status", transcript(), big, False),
        ("'git commit' inside a grep is not a commit", "grep -n 'git commit' notes.txt", transcript(), big, False),
        ("add && commit in one line", "git add a.js && git commit -m y", transcript(), big, True),
        ("dirty: 'add' in the MESSAGE counts staged only", 'git commit -m "fix: add missing line"', transcript(), dirty,
         False),
        ("dirty: --message=add counts staged only", "git commit --message=add-thing", transcript(), dirty, False),
        ("dirty: -m attached add counts staged only", 'git commit -m"add x"', transcript(), dirty, False),
        ("dirty: -F msgfile is not a pathspec", "git commit -F add.txt", transcript(), dirty, False),
        ("dirty: plain staged commit allowed", 'git commit -m "Fix typo"', transcript(), dirty, False),
        ("dirty: pathspec commit counts the worktree", 'git commit -m "x" big.txt', transcript(), dirty, True),
        ("dirty: -- pathspec commit counts the worktree", 'git commit -m "x" -- big.txt', transcript(), dirty, True),
        ("dirty: -am counts the worktree", 'git commit -am "x"', transcript(), dirty, True),
        ("dirty: --all counts the worktree", 'git commit --all -m "x"', transcript(), dirty, True),
        ("dirty: real git add + commit counts the worktree", 'git add . && git commit -m "x"', transcript(), dirty, True),
        ("dirty: -i pathspec counts index + paths", 'git commit -i -m "x" big.txt', transcript(), dirty, True),
    ]
    shapes = [
        (["commit", "-m", "fix: add x"], (False, False, [], False)),
        (["commit", "-am", "x"], (True, False, [], False)),
        (["commit", "-m", "x", "--", "a b.txt", "c"], (False, False, ["a b.txt", "c"], False)),
        (["commit", "-F", "msg.txt", "big.txt"], (False, False, ["big.txt"], False)),
        (["commit", "-minline", "p"], (False, False, ["p"], False)),
        (["commit", "--author", "A <a@b>", "-i", "x.py"], (False, True, ["x.py"], False)),
        (["commit", "--pathspec-from-file=l.txt", "-m", "x"], (False, False, [], True)),
        (["commit", "--pathspec-from-file", "l.txt", "-m", "x"], (False, False, [], True)),
    ]
    for argv, want in shapes:
        got = commit_shape(argv)
        bad += got != want
        print(("ok  " if got == want else "BAD ") + "commit_shape %r" % argv[1:], got)
    for name, cmd, t, lf, want in rcases:
        got = review_verdict(cmd, repo, t, lines_fn=lf) is not None
        bad += got != want
        print(("ok  " if got == want else "BAD ") + "review rule: " + name, got)
    import _hooklib
    saved_state, _hooklib.STATE = _hooklib.STATE, os.path.join(tmp, "state")   # the logged line stays in the temp dir
    try:
        got = review_verdict("git commit -m y", repo, os.path.join(tmp, "missing.jsonl"), lines_fn=big) is None
    finally:
        _hooklib.STATE = saved_state
    bad += not got
    print(("ok  " if got else "BAD ") + "review rule: unreadable transcript fails open (logged)", got)
    # the real counter on the self-test repo: a.js went v1 -> v2 (1 deleted + 1 added); nothing is staged
    got = (commit_lines(repo, (False, False, ["a.js"], False), []), commit_lines(repo, (False, False, [], False), []),
           commit_lines(repo, (True, False, [], False), []) >= 6)
    want = (2, 0, True)
    bad += got != want
    print(("ok  " if got == want else "BAD ") + "commit_lines on a real repo (pathspec, staged, -a)", got)
    return bad + real_review_selftest(tmp)


def real_review_selftest(tmp):
    """The review rule with the REAL line counter on a REAL repo: a 500-line NEW file, a 300-line unstaged change to a
    tracked file that is NOT being committed, and a 1-line change that is. No review in the transcript."""
    import _hooklib
    bad = 0
    repo = os.path.join(tmp, "rv")
    run = lambda *a: subprocess.run(["git", "-C", repo] + list(a), capture_output=True, text=True,
                                    stdin=subprocess.DEVNULL, timeout=30)
    subprocess.run(["git", "init", "-q", repo], capture_output=True, stdin=subprocess.DEVNULL, timeout=30)
    run("config", "user.email", "t@t"); run("config", "user.name", "t"); run("config", "core.autocrlf", "false")
    for f, body in (("big.py", "x = 1\n"), ("small.txt", "x\n")):
        with open(os.path.join(repo, f), "w", newline="\n") as fh:
            fh.write(body)
    run("add", "-A"); run("commit", "-qm", "init")
    with open(os.path.join(repo, "big.py"), "w", newline="\n") as fh:            # unstaged, never committed below
        fh.write("".join("v%d = 1\n" % i for i in range(300)))
    with open(os.path.join(repo, "small.txt"), "w", newline="\n") as fh:
        fh.write("y\n")
    with open(os.path.join(repo, "new_module.py"), "w", newline="\n") as fh:     # NEW, untracked
        fh.write("".join("w%d = 2\n" % i for i in range(500)))
    with open(os.path.join(repo, "list.txt"), "w", newline="\n") as fh:
        fh.write("big.py\n")
    tr = os.path.join(tmp, "rv-empty.jsonl")
    open(tr, "w").close()
    shape = lambda *argv: commit_shape(["commit"] + list(argv))
    counts = [
        ("new file added in the same line counts its full length", (repo, shape("-m", "x"), [["new_module.py"]]), 500),
        ("only the added small file counts", (repo, shape("-m", "x"), [["small.txt"]]), 2),
        ("-a counts tracked changes plus the added new file", (repo, shape("-am", "x"), [["new_module.py"]]), 803),
    ]
    for name, args, want in counts:
        got = commit_lines(*args)
        bad += got != want
        print(("ok  " if got == want else "BAD ") + "commit_lines real: " + name, got)
    cases = [
        ("git add new_module.py && git commit -m 'feat: parser'", repo, True),          # 500 NEW lines
        ("git add -A && git commit -m x", repo, True),
        ("git add . ; git commit -m x", repo, True),
        ("git add new_module.py && git commit -m x new_module.py", repo, True),          # pathspec + new file
        ("git -C " + repo.replace("\\", "/") + " add new_module.py && git -C " + repo.replace("\\", "/") +
         " commit -m x", tmp, True),
        ("git add -u && git commit -m x", repo, True),                                    # tracked big.py
        ("git commit --pathspec-from-file=list.txt -m x", repo, True),                    # big.py via the file
        ("git commit --pathspec-from-file list.txt -m x", repo, True),
        ("git add small.txt && git commit -m 'fix typo'", repo, False),                   # big.py not in this commit
        ("git add small.txt\ngit commit -m \"$(cat <<'EOF'\nfix: typo\nEOF\n)\"", repo, False),
        ("git add small.txt && git commit -m x small.txt", repo, False),
        ("git add new_module.py && git commit -m x small.txt", repo, False),             # pathspec excludes the new file
        ("git commit -m x && git add new_module.py", repo, False),                        # an add AFTER the commit
        ("git commit -m x", repo, False),                                                 # nothing staged
    ]
    saved_state, _hooklib.STATE = _hooklib.STATE, os.path.join(tmp, "rv-state")
    try:
        for cmd, cwd, want in cases:
            got = review_verdict(cmd, cwd, tr) is not None
            bad += got != want
            print(("ok  " if got == want else "BAD ") + "review rule real: " + repr(cmd[:60]), got)
        got = review_verdict("cd $SOMEVAR && git commit -am x", repo, tr) is None
        errlog = os.path.join(tmp, "rv-state", "git-guard-errors.log")
        logged = os.path.exists(errlog) and "unresolved" in open(errlog, encoding="utf-8").read()
        bad += not (got and logged)
        print(("ok  " if got and logged else "BAD ") + "review rule: unresolved commit repo is skipped AND logged",
              got, logged)
        run("add", "new_module.py")                                                       # control: staged earlier
        got = review_verdict("git commit -m x", repo, tr) is not None
        bad += not got
        print(("ok  " if got else "BAD ") + "review rule real: new file staged in an earlier call", got)
    finally:
        _hooklib.STATE = saved_state
    return bad


def audit_line(data, cmd):
    """What a block/override log keeps: the session, the git subcommands, and a hash to match a report against -
    never the command text itself (paths, URLs and tokens ride along in commands)."""
    subs = re.findall(r"\bgit\b(?:\s+-\S+(?:\s+\S+)?)*\s+([a-z][a-z-]*)", re.sub(r"[\"'\\]", "", cmd))
    return "%s git=%s sha=%s len=%d" % (str(data.get("session_id", "?"))[:36], ",".join(subs[:4]) or "-",
                                        hashlib.sha256(cmd.encode("utf-8", "replace")).hexdigest()[:12], len(cmd))


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        sys.exit(selftest())
    data = read_payload("git-guard")
    cmd = (data.get("tool_input") or {}).get("command") or ""
    # Cheap pre-filter, on the command with quotes and backslashes removed: g''it, "git" and g\it all run git.
    # Beyond that the guard is a belt against an agent's HABITS, not a security boundary: a command built at run time
    # ($(printf gi)t, base64, a python subprocess) is out of reach of any text parser.
    if "git" not in re.sub(r"[\"'\\]", "", cmd) and not KILL_BROAD.search(cmd):   # kill rule runs on git-free commands
        sys.exit(0)
    try:
        cwd = data.get("cwd") or os.getcwd()
        msg = verdict(cmd, cwd) or review_verdict(cmd, cwd, data.get("transcript_path"))
    except Exception as e:
        fail_open("git-guard", e)                             # fail open, but leave a trace
    if msg is None:
        for marker in ("# git-guard-ok", "# no-review-ok"):
            if marker in cmd:
                log("git-guard-overrides.log", marker[2:] + " " + audit_line(data, cmd))
        sys.exit(0)
    log("git-guard-blocks.log", audit_line(data, cmd))
    sys.stderr.write(msg + "\n")
    sys.exit(2)


if __name__ == "__main__":
    main()
