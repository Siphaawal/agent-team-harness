#!/usr/bin/env python3
"""leak_scan.py - fail when anything that should never be committed is in the repo.

    python3 tools/leak_scan.py [--root DIR] [--path SUBDIR] [--list]     (--path limits the scan to one subtree)

Scans every file git would commit (tracked + untracked, minus .gitignore) - or, outside a git repo, every file under
the root except .git/ and node_modules/. Binary files are skipped. Two pattern sources:

  1. BUILT-IN generic patterns: personal home paths (/home/<name>/, /Users/<name>/, X:\\Users\\<name>\\), email
     addresses, private keys, and common token shapes (GitHub, Slack, AWS, OpenAI/Anthropic-style keys).
  2. tools/leak_patterns.txt - one Python regex per line, case-insensitive, # comments. GITIGNORED and local: each
     member lists the names that must never appear (their own name, a client, an internal project). It never leaves
     the machine, so the scan can check for words the repo must not even mention.

A line containing `leak-ok` is skipped (use it for documentation of a pattern, never for a real value).
A line over 4096 characters is not regex-scanned (a backtracking pattern on a 200 KB line took 31 s): it is reported
as one "long line - unscanned" finding, like a file over 512 KB. --root and --path must resolve inside this repo
(exit 2 otherwise; env AGENT_TOOLS_ANY_ROOT=1 allows it): findings print fragments of the files they hit.
Exit 0 clean, 1 findings (printed as path:line: pattern), 2 refused, 64 usage error. A bad regex in the local list is
reported once on stderr and skipped.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
from common import repo_root  # noqa: E402

# A placeholder is the WHOLE name (<user>, $USER, example, ...), so /home/user-x/ and /home/me.x/ are still caught.
END = r"(?=[/\\\s\"'`),;:]|$)"
PLACEHOLDER_USERS = r"(?!(?:<|\{|\$)|(?:example|user|username|me|you|runner|ubuntu|name)" + END + ")"
NAME = r"[A-Za-z_][A-Za-z0-9_.-]*" + END
BUILTIN = [
    ("home path", re.compile(r"/home/" + PLACEHOLDER_USERS + NAME)),
    ("home path", re.compile(r"/Users/" + PLACEHOLDER_USERS + NAME)),
    ("home path", re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+" + PLACEHOLDER_USERS + NAME, re.I)),
    # the local part is bounded and must START a run (the lookbehind): an unbounded [..]+@ retried from every
    # position of a long run without '@' was quadratic
    ("email", re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@(?!example\.(?:com|org)\b)"
                         r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("github token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("gitlab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("aws key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("stripe key", re.compile(r"\b[rs]k_live_[0-9A-Za-z]{20,}")),
    ("npm token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("api key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{24,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{24,}=*")),
    # a secret-named key with a LITERAL value: quoted, or unquoted and not code. `secret = os.path.join(...)`,
    # `password = getpass()`, `token = cfg["token"]` and `api_key: ${API_KEY}` are references, not secrets; so is a
    # quoted fragment that an operator continues (`TOKEN = "test-token-" + os.urandom(12).hex()`).
    ("password", re.compile(r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key)\b\s*[:=]\s*"
                            r"(?:(?:\"[^\"\s]{8,}\"|'[^'\s]{8,}')(?!\s*[-+%*.\[(])"
                            r"|(?![\"'])(?![^\s]*[(\[$])(?![^\s]*\.[A-Za-z])[^\s\"'<>{}]{8,})")),
    ("private ip", re.compile(r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")),
]
SKIP_DIRS = {".git", "node_modules", "__pycache__"}
MAX_BYTES = 512 * 1024                   # text files
MAX_BINARY_BYTES = 32 * 1024 * 1024      # images, PDFs: only their printable runs are scanned
# Inside a binary, an address must look real: 2+ char local part, lowercase domain labels. Compressed image data is
# random bytes, and in a 12 MB PDF one run spelled "a@2r.lQ" - the text-file rule would flag that every scan.
BINARY_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])[a-z0-9._%+-]{2,64}@(?!example\.(?:com|org)\b)[a-z0-9-]{2,}"
                          r"(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b")
MAX_LINE = 4096
PRINTABLE = re.compile(rb"[\x20-\x7e]{6,}")       # text runs inside a binary (PNG tEXt chunks, EXIF, embedded paths)


def local_patterns(root):
    p = os.path.join(root, "tools", "leak_patterns.txt")
    out = []
    if not os.path.isfile(p):
        return out
    with open(p, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                out.append(("local pattern %d" % n, re.compile(line, re.I)))
            except re.error as e:
                sys.stderr.write("leak_patterns.txt:%d: bad regex (%s) - skipped\n" % (n, e))
    return out


def files(root):
    # core.excludesFile off: the REPO's .gitignore decides, not this machine's global excludes. A path this member
    # ignores globally is still committed by every other member, so it must be scanned here too.
    try:
        r = subprocess.run(["git", "-c", "core.excludesFile=/dev/null", "-C", root, "ls-files", "-z", "-co",
                            "--exclude-standard"], capture_output=True, timeout=20, stdin=subprocess.DEVNULL)
        if r.returncode == 0:
            return [os.path.join(root, p) for p in r.stdout.decode("utf-8", "replace").split("\0") if p]
    except Exception:
        pass
    out = []
    for d, dirs, names in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        out += [os.path.join(d, n) for n in names]
    return out


def scan(root, only=None, local=None):
    """local: the already-loaded local patterns (main reads the list once); None reads it here."""
    pats = BUILTIN + (local_patterns(root) if local is None else local)
    me = os.path.abspath(__file__)
    hits = []
    prefix = os.path.normcase(os.path.join(os.path.abspath(root), only.strip("/\\"))) if only else None
    for path in files(root):
        if prefix and not (os.path.normcase(os.path.abspath(path)) + os.sep).startswith(prefix + os.sep):
            continue
        rel = os.path.relpath(path, root).replace("\\", "/")
        if os.path.islink(path):
            # never follow: a link to ~/.ssh/id_rsa would make the scanner itself print the key's first bytes
            hits.append((rel, 0, "symlink", "not followed - a committed symlink can point outside the repo"))
            continue
        if not os.path.isfile(path) or os.path.abspath(path) == me or os.path.basename(path) == "leak_patterns.txt":
            continue
        size = os.lstat(path).st_size
        with open(path, "rb") as f:
            binary = b"\0" in f.read(8192)
        # Text is regex-scanned line by line, so it has a tight cap. A binary (an image, a PDF) is scanned only for
        # its short printable runs - metadata, embedded paths - which stays fast even for a large file.
        cap = MAX_BINARY_BYTES if binary else MAX_BYTES
        if size > cap:
            hits.append((rel, 0, "unscanned", "over %d KB - too large to scan; keep it out of the repo" % (cap >> 10)))
            continue
        with open(path, "rb") as f:
            data = f.read()
        if binary:                                     # binary: scan only the text runs inside it
            text = "\n".join(m.group(0).decode("ascii") for m in PRINTABLE.finditer(data))
        else:
            text = data.decode("utf-8", "replace")
        for n, line in enumerate(text.splitlines(), 1):
            if "leak-ok" in line:
                continue
            if len(line) > MAX_LINE:
                hits.append((rel, n, "long line", "%d chars - unscanned (limit %d); wrap it or keep it out of the repo"
                             % (len(line), MAX_LINE)))
                continue
            for label, rx in pats:
                if binary and label == "email":
                    rx = BINARY_EMAIL           # random compressed bytes spell "a@2r.lQ"; real metadata is lowercase
                m = rx.search(line)
                if m:
                    hits.append((rel, n, label, m.group(0)[:60]))
    return hits


def main(argv=None):
    common.utf8_stdout()
    ap = common.ArgParser(prog="leak_scan.py", description="Fail on anything personal or secret that git would commit.")
    ap.add_argument("--root", help="repo root to scan (default: the repo this tool is in)")
    ap.add_argument("--path", help="limit the scan to this subtree of the root")
    ap.add_argument("--list", action="store_true", help="print the patterns and exit")
    a = ap.parse_args(argv)
    common.inside_or_die(a.root, "--root")
    root = os.path.abspath(a.root) if a.root else repo_root()
    if not os.path.isdir(root):
        ap.error("--root is not a directory: %s" % common.printable(root))
    if a.path:
        common.inside_or_die(os.path.join(root, a.path.strip("/\\")), "--path")
    local = local_patterns(root)        # read once: a bad regex is reported once
    if a.list:
        for label, rx in BUILTIN + local:
            print("%-18s %s" % (label, rx.pattern))
        return common.OK
    hits = scan(root, a.path, local)
    for rel, n, label, frag in hits:
        print(common.printable("%s:%d: %s: %s" % (rel, n, label, frag)))
    print("leak_scan: %d finding(s); %d built-in + %d local patterns%s" % (
        len(hits), len(BUILTIN), len(local), "" if local else " (no tools/leak_patterns.txt - add your own names)"))
    return common.FINDINGS if hits else common.OK


if __name__ == "__main__":
    sys.exit(main())
