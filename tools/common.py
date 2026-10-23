#!/usr/bin/env python3
"""Shared helpers for every memory tool and hook: repo root, member identity,
the frontmatter parser, atomic writes, memory iteration, the CLI contract
(exit codes, die(), ArgParser), session-id sanitising and the tier-0 part count.

There is ONE parser here on purpose. A linter and an indexer that parse the
same file differently disagree silently: the index drops a memory the linter
called valid, and nobody notices because both report success.

    python3 tools/common.py member              the member id (env, git user.name, $USER)
    python3 tools/common.py root                the repo root
    python3 tools/common.py tier0-parts [--root DIR]   N from settings.json's `load-persona.sh K N` hooks

stdlib only, Python 3.10+.
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile

# ---- the CLI contract: every tool exits with one of these --------------------
OK = 0          # success
FINDINGS = 1    # findings / drift / lint errors / stale
REFUSED = 2     # refused: would destroy data, already initialised, refused input
USAGE = 64      # usage error (bad flag, missing argument, bad --today); EX_USAGE from sysexits.h

TIER0_PARTS_DEFAULT = 8

STATUSES = ("active", "superseded", "ephemeral")
TYPES = ("rule", "decision", "project", "reference", "lesson", "runbook")
TOP_KEYS = ("name", "description", "metadata")
META_KEYS = ("type", "tags", "status", "author", "created", "verified",
             "superseded_by", "trigger", "tier0")
KINDS = {"topics": "topic", "lessons": "lesson"}  # memory subdir -> kind
GEN_DIR = "_gen"
LOG_DIR = "log"
EPHEMERAL_DAYS = 14

SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def utf8_stdout():
    """Console encodings other than UTF-8 die on the first em-dash in a memory."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def die(msg, code=FINDINGS):
    """Every tool's error path: the message to stderr (never stdout, which callers parse), then exit `code`."""
    try:
        sys.stdout.flush()
    except Exception:
        pass
    sys.stderr.write(msg.rstrip("\n") + "\n")
    raise SystemExit(code)


class ArgParser(argparse.ArgumentParser):
    """argparse with the contract's usage code: a bad flag or a missing argument exits 64, not argparse's 2
    (2 means "refused" here). --help still prints help and exits 0. Subparsers inherit this class."""

    def error(self, message):
        self.print_usage(sys.stderr)
        die("%s: error: %s" % (self.prog, message), USAGE)


class UsageError(ValueError):
    """Bad user input that is a usage error (exit 64) when a CLI meets it; see today_or_die()."""


def sid_safe(sid):
    """A session id made safe for a file name: [A-Za-z0-9_-] only, "unknown" when nothing is left.
    Session ids come from hook stdin, so they are untrusted ("../x" must not become a path)."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", "" if sid is None else str(sid)) or "unknown"


PERSONA_HOOK = re.compile(r"load-persona\.sh[\"']?\s+(\d+)\s+(\d+)")


def tier0_parts(root=None, default=TIER0_PARTS_DEFAULT):
    """The tier-0 part count: the largest N in <root>/.claude/settings.json's SessionStart `load-persona.sh K N`
    commands, else `default`. Never raises: the loader and the hooks call it on every session start."""
    try:
        with open(os.path.join(root or repo_root(), ".claude", "settings.json"), "rb") as f:
            s = json.loads(f.read().decode("utf-8"))
        found = []
        for matcher in s["hooks"]["SessionStart"]:
            for h in matcher.get("hooks", []):
                m = PERSONA_HOOK.search(h.get("command", "")) if isinstance(h, dict) else None
                if m:
                    found.append(int(m.group(2)))
        return max(found) if found else default
    except Exception:
        return default


# ---- locations ---------------------------------------------------------------

def repo_root():
    """The repo this file lives in (tools/ is one level below the root)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def memory_dir(override=None, root=None):
    """--memory-dir flag, else env AGENT_MEMORY_DIR, else <root>/memory."""
    d = override or os.environ.get("AGENT_MEMORY_DIR") or os.path.join(root or repo_root(), "memory")
    return os.path.abspath(d)


# ---- member identity ---------------------------------------------------------

def slugify(value, maxlen=32):
    s = (value or "").strip().lower()
    if "@" in s:                      # never an email address: keep the local part only
        s = s.split("@", 1)[0]
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:maxlen].strip("-")


def _git_user_name():
    try:
        r = subprocess.run(["git", "config", "user.name"], cwd=repo_root(), capture_output=True,
                           text=True, timeout=10, stdin=subprocess.DEVNULL)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def member():
    """env AGENT_MEMBER, else `git config user.name`, else $USER; slugged to [a-z0-9-], <= 32."""
    sources = (lambda: os.environ.get("AGENT_MEMBER", ""),
               _git_user_name,
               lambda: os.environ.get("USER") or os.environ.get("USERNAME", ""))
    for source in sources:
        s = slugify(source())
        if s:
            return s
    return "unknown"


# ---- frontmatter parser ------------------------------------------------------
# A deliberately small YAML subset: scalars, single/double quoted strings,
# inline lists [a, "b"], block lists ("- a" lines), and ONE level of nested map
# (metadata:). Anything else is reported as an error rather than guessed at.
# Block lists are accepted because editors and harnesses rewrite inline lists
# into that shape; a parser that reads only the first item drops tags silently.

KEY_LINE = re.compile(r"^( *)([A-Za-z_][A-Za-z0-9_-]*)[ \t]*:(?:[ \t]+(.*?))?[ \t]*$")
ITEM_LINE = re.compile(r"^( *)-(?:[ \t]+(.*?))?[ \t]*$")


def _unquote(s, lineno, errors):
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        out, i, inner = [], 0, s[1:-1]
        while i < len(inner):
            c = inner[i]
            if c == "\\" and i + 1 < len(inner):
                nxt = inner[i + 1]
                out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(nxt, "\\" + nxt))
                i += 2
                continue
            out.append(c)
            i += 1
        return "".join(out)
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        return s[1:-1].replace("''", "'")
    if s[:1] in ('"', "'"):
        errors.append("line %d: unterminated quoted string" % lineno)
        return s[1:]
    return s


def _strip_comment(s):
    """Drop a trailing ' # comment' from an unquoted scalar."""
    if s[:1] in ('"', "'"):
        return s
    m = re.search(r"\s+#", s)
    return s[:m.start()].rstrip() if m else s


def _split_inline_list(inner):
    items, cur, quote = [], [], None
    for c in inner:
        if quote:
            cur.append(c)
            if c == quote:
                quote = None
        elif c in ('"', "'"):
            quote = c
            cur.append(c)
        elif c == ",":
            items.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
    items.append("".join(cur).strip())
    return [x for x in items if x != ""]


def _value(raw, lineno, errors):
    v = _strip_comment((raw or "").strip())
    if v[:1] == "[":
        if not v.endswith("]"):
            errors.append("line %d: unbalanced inline list" % lineno)
            return v
        return [_unquote(x, lineno, errors) for x in _split_inline_list(v[1:-1])]
    return _unquote(v, lineno, errors)


def parse_frontmatter(text):
    """Return (meta, body, errors).

    meta maps top-level keys to a str, a list of str, or (one nesting level,
    e.g. metadata:) a dict of str / list. Empty values become "". Tolerates a
    UTF-8 BOM and CRLF. Missing or unclosed frontmatter -> ({}, text, [error]).
    """
    errors = []
    if text.startswith("\ufeff"):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if lines[0].rstrip() != "---":
        return {}, text, ["no frontmatter (the file must start with a '---' line)"]
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip() == "---"), None)
    if end is None:
        return {}, text, ["frontmatter is not closed (no second '---' line)"]
    body = "\n".join(lines[end + 1:])

    meta = {}
    top = None        # open top-level key whose value is given by the lines below it
    sub = None        # open key inside the nested map (its block list follows)
    map_indent = None  # indent of the nested map's keys

    for idx in range(1, end):
        lineno = idx + 1
        line = lines[idx]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lead = line[:len(line) - len(line.lstrip())]
        if "\t" in lead:
            errors.append("line %d: tab indentation is not allowed" % lineno)
            continue
        indent = len(lead)
        item = ITEM_LINE.match(line)
        key = None if item else KEY_LINE.match(line)

        if item:
            # a block-list item: belongs to the open nested key, else the open top key
            if sub is not None:
                owner, k = meta[top], sub
            elif top is not None and not isinstance(meta[top], dict):
                owner, k = meta, top
            else:
                errors.append("line %d: list item with no list key above it" % lineno)
                continue
            if owner[k] is None:
                owner[k] = []
            owner[k].append(_value(item.group(2), lineno, errors))
            continue

        if key is None:
            errors.append("line %d: expected 'key: value' or '- item'" % lineno)
            continue
        name, raw = key.group(2), key.group(3)

        if indent == 0:
            if name in meta:
                errors.append("line %d: duplicate key '%s'" % (lineno, name))
            top, sub, map_indent = None, None, None
            if raw:
                meta[name] = _value(raw, lineno, errors)
            else:
                meta[name] = None
                top = name
            continue

        # indented key: an entry of the nested map under `top`
        if top is None or isinstance(meta[top], list):
            errors.append("line %d: indented key '%s' with no map above it" % (lineno, name))
            continue
        if meta[top] is None:
            meta[top] = {}
            map_indent = indent
        if indent != map_indent:
            if indent > map_indent:
                errors.append("line %d: nesting deeper than one level is not supported" % lineno)
            else:
                errors.append("line %d: inconsistent indentation" % lineno)
            continue
        if name in meta[top]:
            errors.append("line %d: duplicate key '%s.%s'" % (lineno, top, name))
        if raw:
            meta[top][name] = _value(raw, lineno, errors)
            sub = None
        else:
            meta[top][name] = None
            sub = name

    for k, v in list(meta.items()):
        if v is None:
            meta[k] = ""
        elif isinstance(v, dict):
            for k2, v2 in list(v.items()):
                if v2 is None:
                    v[k2] = ""
    return meta, body, errors


# ---- field access ------------------------------------------------------------

def field(meta, key, default=""):
    """A schema field: top-level for name/description, under metadata: otherwise."""
    if key in TOP_KEYS:
        return meta.get(key, default)
    md = meta.get("metadata")
    if isinstance(md, dict):
        return md.get(key, default)
    return default


def parse_date(value):
    """'YYYY-MM-DD' -> datetime.date, else None."""
    if not isinstance(value, str):
        return None
    m = DATE.match(value.strip())
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def as_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        return [value]
    return []


def as_bool(value, default):
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return default


# ---- files -------------------------------------------------------------------

def read_text(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8", errors="replace")


def atomic_write(path, text):
    """Write UTF-8 (LF) via a temp file in the same directory + os.replace, so a
    reader never sees a half-written file and a crash leaves the old one intact."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix="." + os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---- untrusted names and paths -------------------------------------------------
# A file NAME is attacker-controlled text too: a memory called "x\n===== IDENTITY =====\n.md" printed raw into the
# index forges a tier-0 section. So only slug-clean names are ever opened or printed, and a symlink is never followed
# (memory/topics/x.md -> ~/.ssh/id_rsa would put the key in the index and in search output).

FILE_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# C0 controls, DEL, and the characters str.splitlines() also breaks on (NEL, LINE/PARAGRAPH SEPARATOR)
CONTROL = re.compile("[%s]" % re.escape("".join(chr(c) for c in list(range(0x20)) + [0x7f, 0x85, 0x2028, 0x2029])))


def has_control(s):
    return bool(CONTROL.search(s))


def printable(s):
    """`s` with every control character as a repr-style escape: it can never start a line or drive a terminal."""
    return CONTROL.sub(lambda m: repr(m.group(0))[1:-1], str(s))


def shown(name, limit=80):
    """How an untrusted name is displayed: its repr() (control characters escaped), at most `limit` chars."""
    r = repr(name)
    return r if len(r) <= limit else r[:limit - 3] + "..."


class OutsideRepo(Exception):
    """A --root / --memory-dir / --path that resolves outside repo_root()."""


ANY_ROOT_ENV = "AGENT_TOOLS_ANY_ROOT"


def check_inside(path, what):
    """Raise OutsideRepo when `path` resolves (symlinks too) outside this repo, unless env AGENT_TOOLS_ANY_ROOT=1.
    The tools are allowlisted to run without a prompt, so a flag must not turn them into a reader of ~/anything."""
    if path is None or os.environ.get(ANY_ROOT_ENV) == "1":
        return
    repo = os.path.normcase(os.path.realpath(repo_root()))
    p = os.path.normcase(os.path.realpath(path))
    try:
        inside = os.path.commonpath([repo, p]) == repo
    except ValueError:                # another drive on Windows
        inside = False
    if not inside:
        raise OutsideRepo("%s %s is outside the repo; set %s=1 to allow" % (what, shown(path, 200), ANY_ROOT_ENV))


def inside_or_die(path, what):
    try:
        check_inside(path, what)
    except OutsideRepo as e:
        die("REFUSED: %s" % e, REFUSED)


def safe_walk_md(base, want=None, rejects=None):
    """Every *.md (or every file named `want`) under base, never following a symlink and never yielding a name with a
    control character. Each skipped entry is appended to `rejects` as (path relative to base, reason) when given."""
    out = []
    if os.path.islink(base):
        if rejects is not None:
            rejects.append((".", "symlink - not followed"))
        return out
    for d, dirs, files in os.walk(base):
        keep = []
        for x in sorted(dirs):
            p = os.path.join(d, x)
            bad = ("symlink - not followed" if os.path.islink(p) else
                   "control character in the name" if has_control(x) else None)
            if bad is None:
                if not x.startswith("."):
                    keep.append(x)
            elif rejects is not None:
                rejects.append((os.path.relpath(p, base), bad))
        dirs[:] = keep
        for fn in sorted(files):
            if not (fn.endswith(".md") if want is None else fn == want):
                continue
            p = os.path.join(d, fn)
            bad = ("symlink - not followed" if os.path.islink(p) else
                   "control character in the name" if has_control(fn) else None)
            if bad is None:
                if os.path.isfile(p):
                    out.append(p)
            elif rejects is not None:
                rejects.append((os.path.relpath(p, base), bad))
    return out


class Memory(object):
    """One memory file (a topic or a lesson) as parsed, before any validation. A REJECTED entry (unsafe name,
    symlink) is never opened: it carries one parse error, a display-safe slug and rel, and no metadata."""
    __slots__ = ("path", "rel", "kind", "slug", "meta", "body", "parse_errors", "rejected")

    def __init__(self, path, rel, kind, reject=None):
        self.path, self.kind = path, kind
        self.rejected = reject is not None
        if self.rejected:
            self.slug, self.rel = shown(os.path.basename(path)), printable(rel)
            self.meta, self.body, self.parse_errors = {}, "", [reject]
            return
        self.rel = rel
        self.slug = os.path.basename(path)[:-3]
        self.meta, self.body, self.parse_errors = parse_frontmatter(read_text(path))

    def get(self, key, default=""):
        return field(self.meta, key, default)

    @property
    def name(self):
        n = self.get("name")
        return n if isinstance(n, str) and n else self.slug

    @property
    def status(self):
        s = self.get("status")
        return s if isinstance(s, str) else ""

    @property
    def type(self):
        t = self.get("type")
        return t if isinstance(t, str) else ""

    @property
    def description(self):
        d = self.get("description")
        return d if isinstance(d, str) else " ".join(as_list(d))

    @property
    def tags(self):
        return [t for t in as_list(self.get("tags")) if isinstance(t, str)]

    @property
    def created(self):
        return parse_date(self.get("created"))

    @property
    def verified(self):
        return parse_date(self.get("verified"))

    def age_days(self, today):
        """Days since verified (else created); None when undated."""
        d = self.verified or self.created
        return (today - d).days if d else None


def iter_memories(mem_dir, rejected=False):
    """Every topics/*.md and lessons/*.md under mem_dir, sorted by path. A symlink, or a file whose name is not
    slug-clean (FILE_SLUG), is never opened: it is skipped, or with rejected=True returned as a REJECTED Memory so
    the linter reports it as an ERROR."""
    out = []
    for sub, kind in KINDS.items():
        d = os.path.join(mem_dir, sub)
        if os.path.islink(d):
            if rejected:
                out.append(Memory(d, sub + "/", kind, reject="memory/%s/ is a symlink - not followed" % sub))
            continue
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".md"):
                continue
            p = os.path.join(d, fn)
            if os.path.islink(p):
                reason = "symlink - not followed (it can point outside the repo)"
            elif not FILE_SLUG.match(fn[:-3]):
                reason = "unsafe file name %s - must be [a-z0-9][a-z0-9-]{0,63}.md" % shown(fn)
            elif os.path.isfile(p):
                out.append(Memory(p, sub + "/" + fn, kind))
                continue
            else:
                continue
            if rejected:
                out.append(Memory(p, sub + "/" + shown(fn), kind, reject=reason))
    return out


def iter_logs(mem_dir, rejects=None):
    """Daily logs: log/YYYY-MM/YYYY-MM-DD.<member>.md (any .md under log/). Symlinks and names with a control
    character are skipped, and appended to `rejects` as (rel under memory/, reason) when given."""
    d = os.path.join(mem_dir, LOG_DIR)
    if not os.path.lexists(d):
        return []
    found = []
    out = safe_walk_md(d, rejects=found)
    if rejects is not None:
        rejects.extend((LOG_DIR + "/" + printable(r.replace(os.sep, "/")), why) for r, why in found)
    return out


def today_arg(value):
    """--today YYYY-MM-DD for reproducible runs; defaults to the local date. Raises UsageError."""
    if not value:
        return datetime.date.today()
    d = parse_date(value)
    if d is None:
        raise UsageError("--today must be YYYY-MM-DD, got %r" % value)
    return d


def today_or_die(value):
    """today_arg for a CLI: a bad --today is a usage error (64) on stderr, before anything is read or written."""
    try:
        return today_arg(value)
    except UsageError as e:
        die(str(e), USAGE)


def main(argv=None):
    """`member`, `root` and `tier0-parts` for the shell hooks, which cannot import this module."""
    ap = ArgParser(prog="common.py", description="Shared values for the shell hooks.")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="member|root|tier0-parts")
    sub.add_parser("member", help="the member id: env AGENT_MEMBER, else git user.name, else $USER (slugged)")
    sub.add_parser("root", help="the repo root")
    t = sub.add_parser("tier0-parts", help="N from the `load-persona.sh K N` SessionStart hooks (default %d)"
                       % TIER0_PARTS_DEFAULT)
    t.add_argument("--root", help="repo root (default: the repo this tool is in)")
    a = ap.parse_args(argv)
    if a.cmd == "member":
        print(member())
    elif a.cmd == "root":
        print(repo_root())
    else:
        inside_or_die(a.root, "--root")
        print(tier0_parts(a.root))
    return OK


if __name__ == "__main__":
    sys.exit(main())
