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
commit message changes nothing. The dry run reads a private copy of the index, so it never takes the repo's
index.lock; only the diffs a commit's shape needs are run (a plain commit: `git diff --cached` alone); a pure rename
counts 0. Only adds in the commit's own REPOSITORY count (`cd sub && git add f && cd .. && git commit` stages f in the
same repo; an add in another repo stages nothing there). A pathspec git would read as magic (`:(glob)...`, `:!x`), or
a quote fragment left by the parser, counts the whole tree (logged). Budget: the whole hook run gets COUNT_BUDGET_S
(3 s) for counting (every commit in the line and every add shares it), cut to fit HOOK_BUDGET_S (9 s) after the
dirty-tree check, never below COUNT_FLOOR_S (0.5 s); reading new files stops as soon as the total reaches the
threshold. A private index copy left behind by a killed hook is removed at a later hook start (after 10 min). A git call
that fails or times out while counting, or a budget that runs out, counts as OVER the threshold (logged). An unreadable
transcript, a repo with no commit yet, and a commit whose repo cannot be resolved (`cd $VAR && git commit`), fail
open (logged). `# no-review-ok: <reason>` overrides (logged to git-guard-overrides.log).
Known limits, not chased: git aliases (`git ci`), and merge / cherry-pick / am / rebase, which commit without `commit`.

Self-test (builds a real temporary repo): python3 git-guard.py test
"""
import atexit
import datetime
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

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


def brace_groups(seg):
    """The pieces of one segment between BARE `{` / `}` tokens (outside quotes): `if ($?) { git commit -m x }` and
    `{ git add f && git commit -m x }` end the command at the brace, which was otherwise read as a pathspec "}".
    A brace inside a quoted message (`-m "fix {braces}"`) or inside a word is not a token."""
    if "{" not in seg and "}" not in seg:
        return [seg]
    parts, start, q, i, n = [], 0, None, 0, len(seg)
    while i < n:
        c = seg[i]
        if q:
            if c == q:
                q = None
            elif c == "\\" and q == '"':
                i += 1
        elif c in "'\"":
            q = c
        elif c == "\\":
            i += 1
        elif c in "{}" and (i == 0 or seg[i - 1].isspace()) and (i + 1 == n or seg[i + 1].isspace()):
            parts.append(seg[start:i])
            start = i + 1
        i += 1
    parts.append(seg[start:])
    return parts


def git_calls(cmd, cwd, depth=0):
    """Yield (repo_dir, argv_after_git, raw_segment) for every git invocation, following cd / Set-Location."""
    cmd = PS_HERESTRING.sub("\n", HEREDOC.sub("\n", cmd))     # message bodies are data, not commands
    here = cwd
    for seg, w in ((s, words(g)) for s in SPLIT.split(cmd) for g in brace_groups(s)):
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


def _git_env(env=None):
    """The environment of EVERY git this hook starts (env: the caller's, else ours), with GIT_OPTIONAL_LOCKS=0: git
    status and git diff otherwise refresh the index opportunistically, which takes .git/index.lock in the user's repo -
    and a hook must never contend for that lock with the user's or a teammate's own git command."""
    return dict(os.environ if env is None else env, GIT_OPTIONAL_LOCKS="0")


def dirt(repo):
    """-> (tracked_changes, untracked_files) | 'notrepo' | None (state could not be read)."""
    if repo == UNRESOLVED:
        return None                                         # unknown location: block a discard rather than guess
    if repo and not os.path.isdir(repo):
        return "notrepo"
    t = STATUS_BUDGET_S
    if _STATUS_DEADLINE[0] is not None:                 # every status call of this hook run shares one budget
        t = min(t, _STATUS_DEADLINE[0] - time.time())
        if t <= 0:
            return None                                 # unread state: a discard blocks
    try:
        out = subprocess.run(["git", "-C", repo or ".", "status", "--porcelain"], capture_output=True,
                             text=True, timeout=t, stdin=subprocess.DEVNULL, env=_git_env())
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
GIT_TIMEOUT_S = 8            # one counting git call
COUNT_BUDGET_S = 3.0         # every counting call of ONE hook run together (all commits and adds in the line)
COUNT_FLOOR_S = 0.5          # the least the count gets, however long the dirty-tree check took
HOOK_BUDGET_S = 9.0          # dirty-tree check + count; the harness kills the hook at 10 s
STATUS_BUDGET_S = 8.0        # every dirty-tree `git status` of one hook run together
PATHS_PER_CALL = 300         # more files than this: one whole-tree numstat instead of path lists
UNCOUNTED = -1               # the count failed: treated as OVER the review threshold
INDEX_COPY_PREFIX = "git-guard-index-"   # temp dirs holding a private index copy (add_set)
STALE_INDEX_COPY_S = 600     # a copy this old was left by a killed hook: removed at the next hook start
SHELL_TRAILERS = {"}", ")", "fi", "done", "then", "else", "esac"}   # a trailing one of these is never a pathspec
_DEADLINE = [None]           # the count deadline of the current hook run (review_verdict / commit_lines)
_STATUS_DEADLINE = [None]    # the dirty-tree deadline of the current hook run (decide)
_STOP_AT = [None]            # review_verdict: stop reading new files once this many lines are counted
_CAPPED = [False]            # set when reading stopped at _STOP_AT: the count is a lower bound


class CountError(Exception):
    """A counting git call failed or timed out, or the index copy failed. The commit then counts as OVER the review
    threshold: an over-count asks for a review, an under-count silently skips one."""


def _git(repo, args, env=None):
    """stdout of `git -C repo <args>` (bytes), or None when git exits non-zero. Raises CountError on a timeout (one
    call, or the shared budget of the count in progress) or when git cannot be started."""
    t = GIT_TIMEOUT_S
    if _DEADLINE[0] is not None:
        t = min(t, _DEADLINE[0] - time.time())
        if t <= 0:
            raise CountError("timeout")
    try:
        r = subprocess.run(["git", "-C", repo, "-c", "core.quotepath=false"] + list(args), capture_output=True,
                           timeout=t, stdin=subprocess.DEVNULL, env=_git_env(env))
    except subprocess.TimeoutExpired:
        raise CountError("timeout")
    except OSError:
        raise CountError("git-start")
    return r.stdout if r.returncode == 0 else None


def numstat(repo, args, literal=False, renames=None):
    """{top-level-relative path: added + deleted lines} for `git diff <args>` (binary files count 0). Rename detection
    is on (-M): a pure `git mv` counts 0, and a rename record counts under its NEW path (renames, when given, gets
    {new: old}). literal=True: the paths are file names, not pathspecs (a name holding * or : means itself). Raises
    CountError when git fails."""
    out = _git(repo, (["--literal-pathspecs"] if literal else []) + ["diff", "--numstat", "-z", "-M"] + list(args))
    if out is None:
        raise CountError("git-exit")
    res, toks, i = {}, out.decode("utf-8", "replace").split("\0"), 0
    while i < len(toks):
        p = toks[i].split("\t", 2)
        i += 1
        if len(p) != 3:
            continue
        path = p[2]
        if not path and i + 1 < len(toks):             # rename / copy: "<add>\t<del>\t\0<old>\0<new>\0"
            path, i = toks[i + 1], i + 2
            if renames is not None:
                renames[path] = toks[i - 2]
        res[path] = (int(p[0]) if p[0].isdigit() else 0) + (int(p[1]) if p[1].isdigit() else 0)
    return res


def _chunks(files, max_chars=6000, max_n=100):
    """Path lists short enough for one command line on every platform."""
    cur, size = [], 0
    for f in files:
        if cur and (size + len(f) > max_chars or len(cur) >= max_n):
            yield cur
            cur, size = [], 0
        cur.append(f)
        size += len(f) + 3
    if cur:
        yield cur


def add_set(repo, add_argv):
    """Top-level-relative paths a `git add` (argv after 'add') would stage, from git's own dry run - so -A, -u, ., a
    directory, a glob and --pathspec-from-file mean exactly what git makes of them. The dry run reads a PRIVATE COPY of
    the index (GIT_INDEX_FILE): even a dry run takes the index lock, and on the real index that made a concurrent git
    fail with "index.lock exists", or made the dry run itself fail while another git held the lock. None when the dry
    run fails (logged by the class of failure, never the command); the caller then counts the add as `git add -A`."""
    args = [a for a in add_argv if a not in ADD_INTERACTIVE and a not in ("-n", "--dry-run")]
    tmpdir = None
    try:
        rel = _git(repo, ["rev-parse", "--git-path", "index"])
        if rel is None:
            raise CountError("git-path")
        real = os.path.join(repo, rel.decode("utf-8", "replace").strip())
        tmpdir = tempfile.mkdtemp(prefix=INDEX_COPY_PREFIX)
        private = os.path.join(tmpdir, "index")
        if os.path.isfile(real):                       # no index yet (fresh repo): git starts from an empty one
            # copy2 keeps the index's mtime: git's racy-entry check compares entry times with the INDEX FILE's time,
            # and a fresh mtime makes a same-second edit of a same-size file look unchanged (counted 0)
            shutil.copy2(real, private)
        out = _git(repo, ["add", "--dry-run", "--ignore-missing"] + args, env=dict(os.environ, GIT_INDEX_FILE=private))
        if out is None:
            raise CountError("git-exit")
    except (CountError, OSError) as e:
        kind = str(e) if isinstance(e, CountError) else "index-copy"
        log("git-guard-errors.log", "review rule: git add dry run failed (%s), counted as git add -A" % kind)
        return None
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    paths = parse_dry_run(out)
    if paths is None:
        log("git-guard-errors.log", "review rule: git add dry run output not parsed (a file name with a newline or "
            "non-UTF-8 bytes?), counted as git add -A")
    return paths


def parse_dry_run(out):
    """Paths from `git add --dry-run` output (bytes): one `add '<path>'` / `remove '<path>'` per line. None when ANY
    line has another shape or the bytes are not UTF-8: a file name holding a newline splits its line in two, and a
    non-UTF-8 name decodes to U+FFFD - either way the path read back names no file, and that add would count 0."""
    text = out.decode("utf-8", "replace")
    if chr(0xFFFD) in text:                            # the replacement character: bytes that are not UTF-8
        return None
    paths = set()
    for line in text.splitlines():
        if not line:
            continue
        for verb in ("add '", "remove '"):
            if line.startswith(verb) and line.endswith("'") and len(line) > len(verb):
                paths.add(line[len(verb):-1])
                break
        else:
            return None
    return paths


def file_lines(path, cap=None):
    """Line count of a NEW file as `git diff` would count it (a NUL byte in the first 8 KB = binary = 0). Reading
    stops once cap lines are seen (the review verdict cannot change after that), and the count deadline is checked
    per chunk: a deadline hit raises CountError (= over the threshold)."""
    n, last, first = 0, b"\n", True
    try:
        with open(path, "rb") as fh:
            while True:
                if _DEADLINE[0] is not None and time.time() > _DEADLINE[0]:
                    raise CountError("timeout")
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                if first and b"\0" in chunk[:8192]:
                    return 0
                first = False
                n += chunk.count(b"\n")
                last = chunk[-1:]
                if cap is not None and n >= cap:
                    _CAPPED[0] = True
                    return n
    except OSError:
        return 0
    return n + (last != b"\n")


def _new_lines(top, files):
    """{path: lines} for files git does not track yet, read from disk; the shared count budget applies, and reading
    stops once the files read so far reach _STOP_AT lines (set by review_verdict: the verdict is then fixed)."""
    out, total = {}, 0
    for p in files:
        if _DEADLINE[0] is not None and time.time() > _DEADLINE[0]:
            raise CountError("timeout")
        if _STOP_AT[0] is not None and total >= _STOP_AT[0]:
            _CAPPED[0] = True
            break
        full = os.path.join(top, p)
        if os.path.isfile(full):
            out[p] = file_lines(full, None if _STOP_AT[0] is None else _STOP_AT[0] - total)
            total += out[p]
    return out


def _add_counts(top, files, head=None, renames=None, keep=None):
    """{path: lines} for the files the `git add` calls in the line stage. Tracked (or already staged) ones count their
    working-tree state vs HEAD from `git diff --numstat HEAD -- <those files>` (head: a whole-tree numstat already
    made); files NOT IN THE INDEX count their full length from disk, plus what HEAD held at that path (a file
    `git rm`ed and written again: the diff saw only the deletion). Whether git knows a file comes from the index
    (`git ls-files`), never from a whole-tree diff. renames ({new: old}, staged renames): the old path joins the
    diff, or a renamed-then-edited file would count as entirely new. keep: a filter for the paths that are in
    neither the index nor head (new files), applied BEFORE any of them is read."""
    files = sorted(files)
    if not files:
        return {}
    sources = sorted({renames[f] for f in files if renames and f in renames} - set(files))
    known = set()
    if len(files) > PATHS_PER_CALL:
        if head is None:
            head = numstat(top, ["HEAD"])
        out = _git(top, ["ls-files", "-z", "--full-name"])
        if out is None:
            raise CountError("git-exit")
        known = set(out.decode("utf-8", "replace").split("\0"))
    else:
        if head is None:
            head = {}
            for part in _chunks(files + sources):      # a pair split across two calls over-counts, never under
                head.update(numstat(top, ["HEAD", "--"] + part, literal=True))
        for part in _chunks(files):
            out = _git(top, ["--literal-pathspecs", "ls-files", "-z", "--full-name", "--"] + part)
            if out is None:
                raise CountError("git-exit")
            known |= set(out.decode("utf-8", "replace").split("\0"))
    res = {p: head[p] for p in files + sources if p in head}
    new = [p for p in files if p not in head and p not in known]
    if keep is not None:
        new = sorted(keep(new))
    regrown = [p for p in files if p in head and p not in known]   # in HEAD, out of the index, back on disk
    for p, n in _new_lines(top, regrown + new).items():
        res[p] = res.get(p, 0) + n
    return res


def _under(repo, new, paths):
    """The NEW files (top-relative) that a pathspec commit's paths (relative to repo) cover. Matched in Python, no
    git walk: a directory covers what is below it, a glob matches across '/', as in git. Pathspec magic (':...')
    covers every file - an over-count asks for a review, an under-count skips one."""
    if any(p.startswith(":") for p in paths):
        return set(new)
    prefix = _git(repo, ["rev-parse", "--show-prefix"])          # where repo sits below the top, as git sees it
    if prefix is None:
        raise CountError("git-exit")
    prefix = prefix.decode("utf-8", "replace").strip()
    specs = []
    for p in paths:
        w = winpath(p)
        if os.path.isabs(w) or re.match(r"^[A-Za-z]:/", w):     # an absolute path: not resolved here, covers all
            return set(new)
        s = os.path.normpath(os.path.join(prefix or ".", w)).replace("\\", "/")
        specs.append("" if s == "." else s)
    norm = str.lower if os.name == "nt" else str          # not normcase: on Windows it turns '/' into '\'
    hit = set()
    for f in new:
        nf = norm(f)
        if any(not s or nf == norm(s) or nf.startswith(norm(s) + "/") or fnmatch.fnmatchcase(nf, norm(s))
               for s in specs):
            hit.add(f)
    return hit


def _untracked_all(top):
    out = _git(top, ["ls-files", "-o", "--exclude-standard", "-z", "--full-name"])
    if out is None:
        raise CountError("git-exit")
    # surrogateescape, not replace: a non-UTF-8 file name must still name the file on disk (POSIX)
    return _new_lines(top, [p for p in out.decode("utf-8", "surrogateescape").split("\0") if p])


def commit_lines(repo, shape, add_argvs):
    """Added + deleted lines the commit will carry; None when there is nothing to count against (not a repo, no
    commit yet). Raises CountError when a git call fails or the shared budget runs out: the caller treats that as OVER.
    shape = commit_shape(argv); add_argvs = argvs of the `git add` calls that run BEFORE it in the same command line.
    Only what the commit's shape needs is computed - a whole-tree diff took seconds on a tree with ~100 dirty files:
    - plain commit: `git diff --cached` only, plus what the adds stage (tracked files at their worktree state via a
      numstat of THOSE files, NEW files at their full length - `git diff HEAD` never lists an untracked file);
    - -a / --all: the whole tracked tree vs HEAD (unavoidable), plus the new files the adds stage;
    - pathspec commit (--only, the default): those paths' WORKING-TREE state whatever the index holds, plus new files
      under them that the adds stage; --include adds the index; --pathspec-from-file counts the whole tracked tree.
    Files outside the adds' pathspecs are never counted: an unrelated unstaged change does not block a small commit.
    A dry run that fails counts its add as `git add -A` (the whole tree, untracked files included).
    An add_argvs item may be (directory, argv): that add runs (and is dry-run) in its own directory of the same repo.
    The deadline of a hook run already in progress (review_verdict) is kept; otherwise this count gets its own."""
    own = _DEADLINE[0] is None
    if own:
        _DEADLINE[0] = time.time() + COUNT_BUDGET_S
    try:
        return _commit_lines(repo, shape, add_argvs)
    finally:
        if own:
            _DEADLINE[0] = None


def _commit_lines(repo, shape, add_argvs):
    all_flag, include, paths, from_file = shape
    top = _git(repo, ["rev-parse", "--show-toplevel"])
    if top is None or _git(repo, ["rev-parse", "--verify", "-q", "HEAD"]) is None:
        return None
    top = top.decode("utf-8", "replace").strip()
    sets = [add_set(*(a if isinstance(a, tuple) else (repo, a))) for a in add_argvs]
    if any(s is None for s in sets):                   # a dry run failed: that add counts as `git add -A`
        carry = numstat(repo, ["HEAD"])
        carry.update(_untracked_all(top))
        return sum(carry.values())
    staged_by_add = set().union(*sets) if sets else set()
    # a pathspec git reads as magic (`:(glob)x`, `:!x` - the parser also cuts `:(...)` at its parentheses), or a
    # quote fragment the parser left behind, is not resolved here: the whole tree is counted (over, never under)
    odd = [p for p in paths if p.startswith(":") or "'" in p or '"' in p]
    if odd:
        log("git-guard-errors.log", "review rule: pathspec magic or quote fragment in a commit, whole tree counted")
    if from_file or odd:                               # the path file is not read: the whole tracked tree
        carry = numstat(repo, ["HEAD"])
        carry.update(_add_counts(top, staged_by_add, head=carry))
    elif paths and not include:
        carry = numstat(repo, ["HEAD", "--"] + list(paths))
        # tracked files the adds stage are already in carry when the paths cover them (head=carry); one in carry but
        # out of the index (git rm, then written again) is read from disk on top of its deletion; of the files git
        # does not know at all only the ones under the paths are read (keep) - only those are committed
        carry.update(_add_counts(top, staged_by_add, head=carry, keep=lambda new: _under(repo, new, paths)))
    else:
        renames = {}
        carry = numstat(repo, ["--cached"], renames=renames)
        head = None
        if all_flag:
            head = numstat(repo, ["HEAD"])
            carry.update(head)
        if paths:                                      # --include with paths: the index plus those paths
            carry.update(numstat(repo, ["HEAD", "--"] + list(paths)))
        carry.update(_add_counts(top, staged_by_add, head=head, renames=renames))
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
    while paths and paths[-1] in SHELL_TRAILERS:       # `... git commit -m x }` / `fi`: shell syntax, not a path
        paths.pop()
    return all_flag, include, paths, from_file


def same_dir(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _toplevel(d, cache):
    """The repository top of directory d (normalised), None when it cannot be resolved; cached per directory. The
    git call runs inside the count budget."""
    key = os.path.normcase(os.path.realpath(d))
    if key not in cache:
        try:
            out = _git(d, ["rev-parse", "--show-toplevel"]) if os.path.isdir(d) else None
        except CountError:
            out = None
        top = out.decode("utf-8", "replace").strip() if out else ""
        cache[key] = os.path.normcase(os.path.realpath(top)) if top else None
    return cache[key]


def same_repo(a, b, cache):
    """True when directories a and b are in the same repository - or when either cannot be resolved: the add is then
    counted (an over-count asks for a review, an under-count skips one). Comparing DIRECTORIES missed
    `cd sub && git add f && cd .. && git commit` and `git -C sub add f && git commit`."""
    if same_dir(a, b):
        return True
    ta, tb = _toplevel(a, cache), _toplevel(b, cache)
    return ta is None or tb is None or ta == tb


def commit_adds(calls, k, repo, cache):
    """The `git add` calls that run BEFORE calls[k] (a commit in repo) in the same repository, as (directory, argv
    after 'add'): each is dry-run in its own directory. One whose directory is unknown (`cd $VAR`) is counted as
    `git add -A` in the commit's repo (over-count rather than miss)."""
    return [(repo, ["-A"]) if r == UNRESOLVED else (r, av[1:]) for r, av, _ in calls[:k]
            if av and av[0] == "add" and (r == UNRESOLVED or same_repo(r, repo, cache))]


def count_budget(spent=0.0):
    """Seconds the count of this hook run may use when the dirty-tree check already used `spent`: COUNT_BUDGET_S, cut
    so that both fit in HOOK_BUDGET_S, but never below COUNT_FLOOR_S (nor above COUNT_BUDGET_S)."""
    return max(min(COUNT_FLOOR_S, COUNT_BUDGET_S), min(COUNT_BUDGET_S, HOOK_BUDGET_S - spent))


def review_verdict(cmd, cwd, transcript, lines_fn=commit_lines, reviewed_fn=reviewed_since_commit, spent=0.0):
    """-> None (allow) or the block message for a large unreviewed commit. ONE count deadline covers every commit and
    add in the line (spent: seconds the dirty-tree check of this hook run already used; see count_budget)."""
    if "# no-review-ok" in cmd:
        return None
    calls = list(git_calls(cmd, cwd))
    if not any(argv and argv[0] == "commit" for _, argv, _ in calls):
        return None
    _DEADLINE[0], _STOP_AT[0] = time.time() + count_budget(spent), REVIEW_MIN_LINES
    try:
        return _review_verdict(calls, transcript, lines_fn, reviewed_fn)
    finally:
        _DEADLINE[0], _STOP_AT[0] = None, None


def _review_verdict(calls, transcript, lines_fn, reviewed_fn):
    cache = {}
    for k, (repo, argv, seg) in enumerate(calls):
        if not argv or argv[0] != "commit":
            continue
        if repo == UNRESOLVED:
            log("git-guard-errors.log", "review rule: commit repo unresolved (cd to a variable?), not counted")
            continue
        _CAPPED[0] = False
        try:
            adds = commit_adds(calls, k, repo, cache)
            n = lines_fn(repo, commit_shape(argv), adds)
        except CountError as e:                        # a git call failed or timed out: over-count, never 0
            log("git-guard-errors.log", "review rule: line count failed (%s), treated as over the threshold" % e)
            n = UNCOUNTED
        if n is None:
            log("git-guard-errors.log", "review rule: line count unreadable, commit allowed")
            continue
        if n != UNCOUNTED and n < REVIEW_MIN_LINES:
            continue
        rv = reviewed_fn(transcript) if transcript else None
        if rv is None:
            log("git-guard-errors.log", "review rule: transcript unreadable, commit allowed")
            continue
        if rv:
            continue
        size = (("at least %d changed lines" if _CAPPED[0] else "%d changed lines") % n if n != UNCOUNTED else
                "a size that could not be counted (a git call failed or timed out; see git-guard-errors.log)")
        return ("BLOCKED by git-guard (review rule): this commit carries %s and no reviewer / verifier / "
                "adversary agent has been dispatched in this session since the last commit. Every code build gets an "
                "independent review. Dispatch Agent(subagent_type=\"reviewer\", name=..., model=...) on the diff, apply "
                "its findings, then commit. If a review is truly not warranted (generated data, a revert, a member "
                "said ship it), add '# no-review-ok: <reason>' to the command (logged)." % size)
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
    # every private index copy of the counting self-tests lands in tt, which must be empty of them afterwards
    tt = os.path.join(tmp, "tt")
    os.makedirs(tt)
    saved_tempdir, tempfile.tempdir = tempfile.tempdir, tt
    try:
        bad += real_review_selftest(tmp) + count_selftest(tmp) + round3_selftest(tmp, tt)
    finally:
        tempfile.tempdir = saved_tempdir
    left = [n for n in os.listdir(tt) if n.startswith(INDEX_COPY_PREFIX)]
    bad += bool(left)
    print(("ok  " if not left else "BAD ") + "counter: no private index copy is left in the temp dir", len(left))
    return bad


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
        # an add in ANOTHER repo stages nothing here (same_dir filter): counted here, -A would take new_module.py
        ("git -C '" + os.path.join(tmp, "clean").replace("\\", "/") + "' add -A && git commit -m x", repo, False),
        ("git add new_module.py && git commit -m x 'new_*.py'", repo, True),             # a glob covers ONLY it
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


def count_selftest(tmp):
    """The counter's side effects and shape on a REAL repo: the dry run never takes the repo's index.lock and never
    touches the real index, a dry run that fails over-counts, a plain commit diffs the index only, a git call that
    times out counts as OVER, and a pure rename counts 0."""
    import _hooklib
    global _git, COUNT_BUDGET_S
    bad = [0]

    def check(name, ok, *shown):
        bad[0] += not ok
        print(("ok  " if ok else "BAD ") + "counter: " + name, *shown)

    repo = os.path.join(tmp, "ct")
    run = lambda *a: subprocess.run(["git", "-C", repo] + list(a), capture_output=True, text=True,
                                    stdin=subprocess.DEVNULL, timeout=30)
    subprocess.run(["git", "init", "-q", repo], capture_output=True, stdin=subprocess.DEVNULL, timeout=30)
    run("config", "user.email", "t@t"); run("config", "user.name", "t"); run("config", "core.autocrlf", "false")

    def write(name, lines, word="v"):
        with open(os.path.join(repo, name), "w", newline="\n") as fh:
            fh.write("".join("%s%d = 1\n" % (word, i) for i in range(lines)))
    write("small.txt", 1)
    write("big400.py", 400)
    run("add", "-A"); run("commit", "-qm", "init")
    write("small.txt", 1, "w")                                     # 2 changed lines, unstaged
    write("n.py", 300)                                             # NEW, untracked
    tr = os.path.join(tmp, "ct-empty.jsonl")
    open(tr, "w").close()
    state = os.path.join(tmp, "ct-state")
    errlog = os.path.join(state, "git-guard-errors.log")
    index, lock = os.path.join(repo, ".git", "index"), os.path.join(repo, ".git", "index.lock")
    real_git, saved_budget, saved_state = _git, COUNT_BUDGET_S, _hooklib.STATE
    _hooklib.STATE = state
    calls = []

    def recording(r, args, env=None):
        calls.append((list(args), dict(env or {})))
        return real_git(r, args, env)
    try:
        # (a) another git holds .git/index.lock - the 300-line new file is still counted, the lock untouched
        with open(lock, "w") as fh:
            fh.write("held by another git")
        before = (open(index, "rb").read(), os.stat(index).st_mtime_ns)
        got = review_verdict("git add n.py && git commit -m x", repo, tr) is not None
        check("index.lock held by another git: new file still counted (blocked)", got, got)
        check("index.lock held by another git: left untouched", open(lock).read() == "held by another git")
        os.remove(lock)
        # (b) every dry run uses a private index; (c) the real index is byte- and mtime-identical
        _git = recording
        del calls[:]
        commit_lines(repo, commit_shape(["commit", "-m", "x"]), [["n.py"], ["-A"]])
        _git = real_git
        dry = [(a, e) for a, e in calls if "add" in a and "--dry-run" in a]
        priv = [e.get("GIT_INDEX_FILE") for a, e in dry]
        ok = len(dry) == 2 and all(p and not same_dir(p, index) and not p.startswith(os.path.dirname(index))
                                   for p in priv)
        check("every add --dry-run runs on a private index (GIT_INDEX_FILE)", ok, len(dry), priv[:1])
        check("the repo's index.lock does not exist after the count", not os.path.exists(lock))
        check("the real index is byte- and mtime-identical", (open(index, "rb").read(), os.stat(index).st_mtime_ns)
              == before)
        # (d) a dry run that fails is logged and counts the add as `git add -A` (n.py, 300 lines): blocked
        got = review_verdict("git add --no-such-flag small.txt && git commit -m x", repo, tr) is not None
        logged = os.path.exists(errlog) and "dry run failed (git-exit)" in open(errlog, encoding="utf-8").read()
        check("a failed dry run is logged and over-counts as git add -A", got and logged, got, logged)
        # which diffs each commit form runs
        def diffs(cmd):
            del calls[:]
            globals()["_git"] = recording
            try:
                review_verdict(cmd, repo, tr)
            finally:
                globals()["_git"] = real_git
            return [a[a.index("diff"):] for a, e in calls if "diff" in a]
        d = diffs("git commit -m x")
        check("plain commit: exactly one numstat, and it is --cached", len(d) == 1 and "--cached" in d[0], d)
        d = diffs("git commit -m x small.txt")
        check("pathspec commit: its numstat carries the pathspec", len(d) == 1 and d[0][-2:] == ["--", "small.txt"], d)
        d = diffs("git add small.txt && git commit -m x")
        check("add + commit: --cached plus a numstat of the added files only",
              len(d) == 2 and "--cached" in d[0] and d[1][-2:] == ["--", "small.txt"], d)
        d = diffs("git commit -am x")
        check("-a: the one whole-tree diff", any("--cached" not in x and "--" not in x for x in d), d)
        for cmd in ("git commit -m x", "git commit -m x small.txt", "git add small.txt && git commit -m x",
                    "git add -A && git commit -m x"):
            d = diffs(cmd)
            check("no whole-tree diff for " + repr(cmd), all("--cached" in x or "--" in x for x in d), d)
        # every git the hook starts - count, dry run and the dirty-tree read - runs with GIT_OPTIONAL_LOCKS=0, recorded
        # at subprocess.run itself (the env git really got, not the env a caller handed to _git)
        real_run, seen = subprocess.run, []

        def rec_run(argv, *a, **kw):
            if argv and argv[0] == "git":
                seen.append((list(argv), dict(kw.get("env") or {})))
            return real_run(argv, *a, **kw)

        def git_envs(fn):
            del seen[:]
            subprocess.run = rec_run
            try:
                fn()
            finally:
                subprocess.run = real_run
            return list(seen)
        for name, fn in (("plain commit verdict", lambda: review_verdict("git commit -m x", repo, tr)),
                         ("add + commit verdict", lambda: review_verdict("git add small.txt && git commit -m x",
                                                                         repo, tr)),
                         ("discard check (git checkout -- f)", lambda: verdict("git checkout -- small.txt", repo)),
                         ("set-aside check (git stash)", lambda: verdict("git stash", repo))):
            got = git_envs(fn)
            check("every git call of the %s has GIT_OPTIONAL_LOCKS=0" % name,
                  got and all(e.get("GIT_OPTIONAL_LOCKS") == "0" for a, e in got), len(got),
                  [[x for x in a[3:] if x not in ("-c", "core.quotepath=false")][:2]
                   for a, e in got if e.get("GIT_OPTIONAL_LOCKS") != "0"][:2])
            if name.startswith("add"):
                dry = [e for a, e in got if "--dry-run" in a]
                check("the add dry run carries BOTH GIT_INDEX_FILE and GIT_OPTIONAL_LOCKS=0", len(dry) == 1 and
                      dry[0].get("GIT_INDEX_FILE") and dry[0].get("GIT_OPTIONAL_LOCKS") == "0", len(dry))
        # a git call that times out: logged, and the commit counts as OVER the threshold (2 lines would pass)
        if os.path.exists(errlog):
            os.remove(errlog)
        COUNT_BUDGET_S = 0
        got = review_verdict("git add small.txt && git commit -m x", repo, tr)
        COUNT_BUDGET_S = saved_budget
        logged = os.path.exists(errlog) and "treated as over" in open(errlog, encoding="utf-8").read()
        check("a timed-out count is logged and blocks as OVER", got is not None and logged and "could not be counted"
              in got, got is not None, logged)
        got = review_verdict("git add small.txt && git commit -m x", repo, tr)
        check("control: the same small commit with time to count is allowed", got is None, got is None)
        # a pure `git mv` of a 400-line file counts 0; a rename plus 100 changed lines counts 200
        os.remove(os.path.join(repo, "n.py"))
        write("small.txt", 1)
        run("mv", "big400.py", "moved.py")
        n = commit_lines(repo, commit_shape(["commit", "-m", "x"]), [])
        check("pure rename counts 0 and is allowed", n == 0 and review_verdict("git commit -m mv", repo, tr) is None, n)
        with open(os.path.join(repo, "moved.py"), "w", newline="\n") as fh:
            fh.write("".join(("z%d = 2\n" if i < 100 else "v%d = 1\n") % i for i in range(400)))
        n = commit_lines(repo, commit_shape(["commit", "-m", "x"]), [["moved.py"]])
        check("rename + 100 changed lines counts 200 and blocks",
              n == 200 and review_verdict("git add moved.py && git commit -m mv", repo, tr) is not None, n)
        # racy index entry: a same-size edit whose mtime equals the index file's own mtime is caught by git only
        # through its racy check, which needs the index COPY to keep the index's mtime (copyfile counted this 0)
        rr = os.path.join(tmp, "racy")
        subprocess.run(["git", "init", "-q", rr], capture_output=True, stdin=subprocess.DEVNULL, timeout=30)
        for k, v in (("user.email", "t@t"), ("user.name", "t"), ("core.autocrlf", "false"),
                     ("core.trustctime", "false"), ("core.checkStat", "minimal")):
            subprocess.run(["git", "-C", rr, "config", k, v], capture_output=True, stdin=subprocess.DEVNULL, timeout=30)
        f = os.path.join(rr, "r.txt")
        with open(f, "w", newline="\n") as fh:
            fh.write("aaaa\n" * 200)
        t0 = int(time.time()) - 100                                # old: git records it without smudging it racy
        os.utime(f, (t0, t0))
        subprocess.run(["git", "-C", rr, "add", "r.txt"], capture_output=True, stdin=subprocess.DEVNULL, timeout=30)
        subprocess.run(["git", "-C", rr, "commit", "-qm", "i"], capture_output=True, stdin=subprocess.DEVNULL,
                       timeout=30)
        with open(f, "w", newline="\n") as fh:
            fh.write("bbbb\n" * 200)                               # same size
        os.utime(f, (t0, t0))
        os.utime(os.path.join(rr, ".git", "index"), (t0, t0))     # racy: entry time == index time
        n = commit_lines(rr, commit_shape(["commit", "-m", "x"]), [["r.txt"]])
        check("racy same-size edit is still counted (index copy keeps its mtime)", n == 400, n)
    finally:
        _git, COUNT_BUDGET_S, _hooklib.STATE = real_git, saved_budget, saved_state
    return bad[0]


def round3_selftest(tmp, tt):
    """Same-repo adds, shell braces, magic pathspecs, regrown files, unparsed dry runs, the read cap, one budget per
    hook run, and the index copies: on REAL repos, no review in the transcript (a block = counted over 150)."""
    import _hooklib
    global _git, HOOK_BUDGET_S, COUNT_BUDGET_S
    bad = [0]

    def check(name, ok, *shown):
        bad[0] += not ok
        print(("ok  " if ok else "BAD ") + "round3: " + name, *shown)

    def mkrepo(name):
        d = os.path.join(tmp, name)
        subprocess.run(["git", "init", "-q", d], capture_output=True, stdin=subprocess.DEVNULL, timeout=30)
        for k, v in (("user.email", "t@t"), ("user.name", "t"), ("core.autocrlf", "false")):
            subprocess.run(["git", "-C", d, "config", k, v], capture_output=True, stdin=subprocess.DEVNULL, timeout=30)
        return d

    def git(d, *a):
        return subprocess.run(["git", "-C", d] + list(a), capture_output=True, stdin=subprocess.DEVNULL, timeout=30)

    def write(d, name, lines, word="v"):
        p = os.path.join(d, name)
        if os.path.dirname(p) and not os.path.isdir(os.path.dirname(p)):
            os.makedirs(os.path.dirname(p))
        with open(p, "w", newline="\n") as fh:
            fh.write("".join("%s%d = 1\n" % (word, i) for i in range(lines)))

    repo = mkrepo("r3")
    write(repo, "base.txt", 1)
    write(repo, "small.txt", 1)
    write(repo, "sub/s.py", 10)
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "init")
    tr = os.path.join(tmp, "r3-empty.jsonl")
    open(tr, "w").close()
    state = os.path.join(tmp, "r3-state")
    errlog = os.path.join(state, "git-guard-errors.log")
    real_git, saved_hook, saved_state = _git, HOOK_BUDGET_S, _hooklib.STATE
    _hooklib.STATE = state
    blocked = lambda cmd, cwd=repo: review_verdict(cmd, cwd, tr) is not None

    def errlog_has(text):
        return os.path.exists(errlog) and text in open(errlog, encoding="utf-8").read()
    try:
        # same REPOSITORY, not same directory: an add in a subdirectory of the commit's repo stages there
        write(repo, "sub/n.py", 300)
        for cmd in ("cd sub && git add n.py && cd .. && git commit -m x", "git -C sub add n.py && git commit -m x"):
            check("add in a subdirectory, commit at the top: blocked " + repr(cmd), blocked(cmd))
        other = mkrepo("r3-other")                 # another repo holding the SAME untracked name: stages nothing here
        write(other, "base.txt", 1)
        git(other, "add", "-A"); git(other, "commit", "-qm", "init")
        write(other, "sub/n.py", 300)
        oq = "'" + other.replace("\\", "/") + "'"
        check("control: an add in ANOTHER repo is not counted",
              not blocked("git -C " + oq + " add sub/n.py && git commit -m x"))
        check("same_repo: a subdirectory is the same repo, another repo is not, an unknown dir counts",
              same_repo(os.path.join(repo, "sub"), repo, {}) and not same_repo(other, repo, {}) and
              same_repo(os.path.join(tmp, "no-such-dir"), repo, {}))
        os.remove(os.path.join(repo, "sub", "n.py"))
        # bare braces end a command; braces inside a quoted message do not
        write(repo, "psnew.py", 300)
        for cmd in ("git add psnew.py; if ($?) { git commit -m 'x' }", "{ git add psnew.py && git commit -m x; }",
                    "{ git add psnew.py && git commit -m x }",
                    "git add psnew.py; if ($?) { git commit -m 'x' } else { echo no }"):
            check("brace group around add + commit: blocked " + repr(cmd), blocked(cmd))
        git(repo, "add", "psnew.py")
        check("brace group, file already staged: blocked", blocked("if ($?) { git commit -m 'x' }"))
        check("bash brace group, file already staged: blocked", blocked("{ git commit -m x }"))
        git(repo, "rm", "-q", "--cached", "psnew.py")
        os.remove(os.path.join(repo, "psnew.py"))
        got = [argv for _, argv, _ in git_calls('git commit -m "fix {braces}"', repo)]
        check("braces inside a quoted message stay in the message", got == [["commit", "-m", "fix {braces}"]], got)
        got = [argv for _, argv, _ in git_calls("git commit -m 'a { b }' && git status", repo)]
        check("spaced braces inside quotes are not tokens", got == [["commit", "-m", "a { b }"], ["status"]], got)
        check("a trailing fi / done / ) is not a pathspec",
              commit_shape(["commit", "-m", "x", "fi"])[2] == [] and commit_shape(["commit", "-m", "x", ")"])[2] == [])
        # magic pathspecs and quote fragments: the whole tree, logged (a commit of 300 changed lines in s.py)
        write(repo, "sub/s.py", 150, "w")
        for spec in ("':(glob)sub/*.py'", "':(top)sub/s.py'", "':!base.txt'"):
            if os.path.exists(errlog):
                os.remove(errlog)
            try:
                calls = list(git_calls("git commit -m x " + spec, repo))
                n = commit_lines(repo, commit_shape(calls[0][1]), [])
            except CountError as e:
                n = "CountError %s" % e
            check("magic pathspec %s counts the whole tree (160) and is logged" % spec,
                  n == 160 and errlog_has("pathspec magic"), n)
        # directory coverage in a pathspec commit: `git add d/x.py && git commit d` commits d/x.py
        write(repo, "d/x.py", 300)
        check("a directory pathspec covers the new file under it: blocked",
              blocked("git add d/x.py && git commit -m x d"))
        check("control: a pathspec beside it does not", not blocked("git add d/x.py && git commit -m x base.txt"))
        os.remove(os.path.join(repo, "d", "x.py"))
        write(repo, "sub/s.py", 10)
        # a path in HEAD, removed from the index, written again: HEAD's deletion plus the new file's length
        git(repo, "rm", "-q", "base.txt")
        write(repo, "base.txt", 300, "q")
        for cmd in ("git add base.txt && git commit -m x", "git add base.txt && git commit -m x base.txt"):
            calls = list(git_calls(cmd, repo))
            n = commit_lines(repo, commit_shape(calls[-1][1]), [(repo, ["base.txt"])])
            check("git rm, rewrite, add + commit counts 301 and blocks: " + repr(cmd), n == 301 and blocked(cmd), n)
        git(repo, "add", "base.txt"); git(repo, "commit", "-qm", "regrown")
        # dry-run output that cannot be parsed: logged, the add counts as `git add -A`
        cases = [(b"add 'a.py'\nremove 'b c.py'\n", {"a.py", "b c.py"}), (b"", set()), (b"add 'it's.py'\n", {"it's.py"}),
                 (b"add 'new\nline.py'\n", None), (b"add 'lat\xe9.py'\n", None), (b"add 'x.py'\nwarning: y\n", None)]
        got = [parse_dry_run(o) for o, _ in cases]
        check("dry-run parser: a newline or non-UTF-8 name is NOT parsed", got == [w for _, w in cases], got)
        weird = None
        try:
            with open(os.path.join(repo, "new\nline.py"), "w", newline="\n") as fh:
                fh.write("x = 1\n" * 300)
            weird = "new\nline.py"
        except OSError as e:
            print("skip round3: a file name with a newline (this filesystem refuses it: %s)" % e.__class__.__name__)
        if weird:
            if os.path.exists(errlog):
                os.remove(errlog)
            got = blocked("git add -A && git commit -m x")
            check("a new file whose name holds a newline: logged and blocked", got and errlog_has("not parsed"), got)
            os.remove(os.path.join(repo, weird))
        if os.name != "nt" and sys.platform != "darwin":
            try:
                with open(os.path.join(os.fsencode(repo), b"lat\xe9.py"), "wb") as fh:
                    fh.write(b"x = 1\n" * 300)
                got = blocked("git add -A && git commit -m x")
                check("a new file with a non-UTF-8 name: blocked (read through surrogateescape)", got)
                os.remove(os.path.join(os.fsencode(repo), b"lat\xe9.py"))
            except OSError as e:
                print("skip round3: a non-UTF-8 file name (refused: %s)" % e.__class__.__name__)
        else:
            print("skip round3: a non-UTF-8 file name (Windows and macOS file systems store names as Unicode)")
        # reading new files stops at the threshold, and the deadline is checked inside a file
        big = os.path.join(repo, "huge.txt")
        with open(big, "w", newline="\n") as fh:
            fh.write("x\n" * 1500000)                                  # ~3 MB, 3 chunks
        _CAPPED[0] = False
        n = file_lines(big, 150)
        check("file_lines stops after the chunk that reaches the cap", 150 <= n < 1500000 and _CAPPED[0], n)
        check("control: without a cap the whole file is counted", file_lines(big) == 1500000)
        write(repo, "n1.txt", 300); write(repo, "n2.txt", 300)
        _STOP_AT[0], _CAPPED[0] = 150, False
        try:
            got = _new_lines(repo, ["n1.txt", "n2.txt"])
        finally:
            _STOP_AT[0] = None
        check("_new_lines stops reading once the threshold is reached", list(got) == ["n1.txt"] and _CAPPED[0], got)
        _DEADLINE[0] = time.time() - 1
        try:
            file_lines(big)
            got = "no error"
        except CountError as e:
            got = str(e)
        finally:
            _DEADLINE[0] = None
        check("a deadline hit while reading a file raises the timeout (= over)", got == "timeout", got)
        msg = review_verdict("git add huge.txt && git commit -m x", repo, tr) or ""
        check("a huge new file blocks as 'at least' the threshold", "at least" in msg, msg[:80])
        for f in ("huge.txt", "n1.txt", "n2.txt"):
            os.remove(os.path.join(repo, f))
        # ONE count deadline for the whole hook run, shared by every commit; the dirty-tree time is taken off it
        seen = []

        def rec(r, args, env=None):
            seen.append(_DEADLINE[0])
            return real_git(r, args, env)
        write(repo, "a1.txt", 2); write(repo, "a2.txt", 2)
        _git = rec
        try:
            review_verdict("git add a1.txt && git commit -m x; git add a2.txt && git commit -m y", repo, tr)
        finally:
            _git = real_git
        check("every count call of a two-commit line shares ONE deadline", len(seen) > 4 and len(set(seen)) == 1 and
              seen[0] is not None, len(seen), len(set(seen)))
        check("count_budget: 3 s, cut to fit 9 s after the status check, floor 0.5",
              (count_budget(0), count_budget(7), count_budget(8.8), count_budget(60)) == (3.0, 2.0, 0.5, 0.5),
              (count_budget(0), count_budget(7), count_budget(8.8), count_budget(60)))
        saved_budget, COUNT_BUDGET_S = COUNT_BUDGET_S, 0
        try:
            commit_lines(repo, commit_shape(["commit", "-m", "x"]), [["a1.txt"]])
            got = "no error"
        except CountError as e:
            got = str(e)
        finally:
            COUNT_BUDGET_S = saved_budget
        check("commit_lines called on its own gets its own budget (0 s: timeout)", got == "timeout", got)
        del seen[:]
        HOOK_BUDGET_S = 2.0
        _git = rec

        def slow_dirt(r):
            time.sleep(0.8)
            return [], []
        t0 = time.time()
        try:
            decide("git checkout -- a1.txt && git add a1.txt && git commit -m x", repo, tr, dirt_fn=slow_dirt)
        finally:
            _git, HOOK_BUDGET_S = real_git, saved_hook
        left = (seen[0] - t0) if seen and seen[0] else None
        check("a slow dirty-tree check shortens the count budget (deadline <= hook budget)",
              left is not None and left <= 2.3, left)
        _STATUS_DEADLINE[0] = time.time() - 1
        try:
            got = dirt(repo)
        finally:
            _STATUS_DEADLINE[0] = None
        check("a used-up status budget reads as unknown state (a discard blocks)", got is None and
              isinstance(dirt(repo), tuple), got)
        for f in ("a1.txt", "a2.txt"):
            os.remove(os.path.join(repo, f))
        # private index copies: made in the temp dir (positive control), stale ones swept, young ones kept
        envs = []

        def rec_env(r, args, env=None):
            envs.append((env or {}).get("GIT_INDEX_FILE"))
            return real_git(r, args, env)
        _git = rec_env
        try:
            commit_lines(repo, commit_shape(["commit", "-m", "x"]), [["small.txt"]])
        finally:
            _git = real_git
        made = [e for e in envs if e]
        check("control: the add dry run made its index copy in the temp dir under test",
              len(made) == 1 and os.path.basename(os.path.dirname(made[0])).startswith(INDEX_COPY_PREFIX) and
              same_dir(os.path.dirname(os.path.dirname(made[0])), tt), made)
        sw = os.path.join(tmp, "sweep")
        os.makedirs(sw)
        old, young, other_old = (os.path.join(sw, n) for n in (INDEX_COPY_PREFIX + "old", INDEX_COPY_PREFIX + "young",
                                                               "unrelated-old"))
        for d in (old, young, other_old):
            os.makedirs(d)
            open(os.path.join(d, "index"), "w").close()
        t_old = time.time() - STALE_INDEX_COPY_S - 60
        for d in (old, other_old):
            os.utime(d, (t_old, t_old))
        sweep_index_copies(sw)
        check("sweep removes a stale index copy, keeps a young one and an unrelated dir",
              not os.path.exists(old) and os.path.isdir(young) and os.path.isdir(other_old),
              os.path.exists(old), os.path.isdir(young), os.path.isdir(other_old))
        sweep_index_copies(os.path.join(tmp, "no-such-dir"))
        check("sweep of a missing temp dir does not raise", True)
    finally:
        _git, HOOK_BUDGET_S, _hooklib.STATE = real_git, saved_hook, saved_state
        _DEADLINE[0], _STOP_AT[0], _STATUS_DEADLINE[0] = None, None, None
    return bad[0]


def audit_line(data, cmd):
    """What a block/override log keeps: the session, the git subcommands, and a hash to match a report against -
    never the command text itself (paths, URLs and tokens ride along in commands)."""
    subs = re.findall(r"\bgit\b(?:\s+-\S+(?:\s+\S+)?)*\s+([a-z][a-z-]*)", re.sub(r"[\"'\\]", "", cmd))
    return "%s git=%s sha=%s len=%d" % (str(data.get("session_id", "?"))[:36], ",".join(subs[:4]) or "-",
                                        hashlib.sha256(cmd.encode("utf-8", "replace")).hexdigest()[:12], len(cmd))


def sweep_index_copies(root=None, now=None):
    """Best effort: remove the private index copies (INDEX_COPY_PREFIX dirs in the temp dir) older than
    STALE_INDEX_COPY_S - a hook killed mid-count leaves its copy behind. A younger one may belong to a hook running
    right now and is never touched; symlinks and files are skipped; nothing here raises."""
    try:
        root = root or tempfile.gettempdir()
        now = time.time() if now is None else now
        with os.scandir(root) as it:
            for e in it:
                try:
                    if (e.name.startswith(INDEX_COPY_PREFIX) and e.is_dir(follow_symlinks=False)
                            and now - e.stat(follow_symlinks=False).st_mtime > STALE_INDEX_COPY_S):
                        shutil.rmtree(e.path, ignore_errors=True)
                except OSError:
                    continue
    except Exception:
        pass


def decide(cmd, cwd, transcript, dirt_fn=dirt):
    """The whole verdict of one hook run: the discard rule, then the review rule. Every dirty-tree read shares
    STATUS_BUDGET_S, and the count gets what is left of HOOK_BUDGET_S (count_budget), so the two fit the harness's
    hook timeout together."""
    t0 = time.time()
    _STATUS_DEADLINE[0] = t0 + STATUS_BUDGET_S
    try:
        msg = verdict(cmd, cwd, dirt_fn)
    finally:
        _STATUS_DEADLINE[0] = None
    return msg or review_verdict(cmd, cwd, transcript, spent=time.time() - t0)


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
    sweep_index_copies()
    try:
        cwd = data.get("cwd") or os.getcwd()
        msg = decide(cmd, cwd, data.get("transcript_path"))
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
