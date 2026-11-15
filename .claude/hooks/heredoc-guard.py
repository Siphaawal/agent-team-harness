#!/usr/bin/env python3
"""heredoc-guard.py - PreToolUse hook (matcher: Bash). Blocks shell constructs that silently corrupt file content.

Rules:
  1. A heredoc fed to python/python3 whose body contains a backslash -> block: some shell layers unescape one level
     even inside a QUOTED heredoc, so '\\' becomes '\' and regexes and string escapes break silently. Write the
     script with the Write tool and run the file. `# heredoc-ok` overrides (audited to state/heredoc-overrides.log).
  2. The override is REFUSED when the body holds a DOUBLED backslash - the one case where the collapse changes meaning.
  3. `python -c "..."` with a backtick after it -> block: inside double quotes the shell runs `...` as a command.
     `# backtick-ok` overrides.
  4. ANY heredoc longer than 60 lines -> block: that is file content, and one quote or apostrophe ends it early and
     runs the rest as shell. File content goes through Write/Edit. `# heredoc-long-ok` overrides. The same marker
     lifts the two size caps (a command over 32 KB; too many '<<' x length to parse each heredoc) - but over the
     '<<' cap the other rules still run, judged on the whole text, and the marker does not lift them.
  5. A Python heredoc that WRITES a file (open(..., 'w'/'a'), .write_text(, .write_bytes() -> block: a find/replace-
     and-write is an edit, and the Edit tool does it with no escaping layer. `# heredoc-write-ok` overrides.
  6. `python -` with NOTHING on stdin (an empty heredoc, or no heredoc or pipe at all) -> block: the interpreter waits
     on the tool's stdin or opens its interactive prompt, and the shell hangs until it is killed. No override.
     Judged per shell segment where python is the COMMAND WORD, so text that only mentions `python -` (an echo, a
     commit message, a grep pattern) passes; a pipe, `<` file, `< <(...)`, here-string or non-empty heredoc on that
     same segment counts as stdin. Wrappers are skipped WITH their own flags and arguments (`timeout 60`, `sudo -E`,
     `env -i`, `xargs -n1`, `winpty`, `setsid`, `flock <file>`, `doas`, `chronic`, `strace`, `taskset <mask>`,
     `uv run` / `poetry run` / `pipenv run` / `conda run -n x` / `pdm run` / `hatch run` / `rye run` / `pixi run`,
     `pyenv exec`); a wrapper flag the table does not know falls back to the old whole-text test for that segment.
     A remote command (`ssh host python -`) is not parsed: allowed.

Every block is counted in state/heredoc-blocks.log (rotated) and the message opens with "block #N in the last 7 days":
each block on its own reads as a one-off, the running count shows the habit at the moment it repeats.
"""
import datetime
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _hooklib  # noqa: E402
from _hooklib import append_log, fail_open, read_payload  # noqa: E402
# One heredoc: the line that opens it (head), the tag, the body, the terminator line. Three shapes keep it linear;
# each matches exactly what the plain form `(?P<head>[^\n]*?)<<...(?P<tag>[A-Za-z_][A-Za-z0-9_]*)(?P=q)[^\n]*\n
# (?P<body>.*?)\n\s*(?P=tag)...` matches (the self-test and an equivalence corpus check that):
# - anchored at a line start: a match ends at a newline or the end, so the leftmost match is the same one, but an
#   unanchored lazy head is retried from every position of a long line (quadratic in the line's length);
# - a lookahead after the tag's first letter fails at once when no newline follows on that line, instead of retrying
#   the rest of the line for every shorter tag;
# - the body ends only at the FIRST newline of a whitespace run: every newline of one run reaches the same next word,
#   so they all succeed or all fail - trying each one is quadratic in a long run of blank lines.
HEREDOC = re.compile(r"(?m)^(?P<head>[^\n]*?)<<-?\s*(?P<q>['\"]?)(?P<tag>[A-Za-z_](?=[^\n]*\n)[A-Za-z0-9_]*)(?P=q)"
                     r"[^\n]*\n(?P<body>(?:.*?\S)??[^\S\n]*)\n\s*(?P=tag)\s*(?:\n|$)", re.S)
PYTHON = re.compile(r"(^|[\s;&|(])(python3?|py)(\.exe)?(\s|$)")
MAX_LINES = 60
PY_DASH_C_DQ = re.compile(r"(^|[\s;&|(])(python3?|py)(\.exe)?(\s+-[A-Za-z]+)*\s+-c\s+\"")   # python [-I] -c "
PY_STDIN = re.compile(r"(^|[\s;&|(])(python3?|py)(\.exe)?(\s+-[A-Za-z]+)*\s+-(\s|$)")        # python [-I] -
EMPTY_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_](?=[^\n]*\n)[A-Za-z0-9_]*)\1[^\n]*\n\s*\2\s*(\n|$)")
PIPED_PY = re.compile(r"\|\s*(python3?|py)(\.exe)?\b")
STDIN_REDIRECT = re.compile(r"(^|[^<])<\s*[^<\s(]")                                          # python - < file.py
# One heredoc (header, body, terminator); `<<<` is a here-string, not a heredoc. The body may be empty.
HD_ANY = re.compile(r"(?<!<)<<(?!<)-?[ \t]*(['\"]?)([A-Za-z_](?=[^\n]*\n)[A-Za-z0-9_]*)\1(?P<tail>[^\n]*)\n"
                    r"(?P<body>(?:.*?\n)??)[ \t]*\2[ \t]*(?:\n|$)", re.S)
# A heredoc ends at a line that holds its tag alone, so no tag can be longer than the longest such line. The heredoc
# regexes are run with the tag's length capped at that (same matches: a longer tag never finds its terminator), else
# a tag of thousands of letters is retried at every shorter length, each time scanning the rest of the command.
TERM_LINE = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*")
TAG_REST = "[A-Za-z0-9_]*"


def tag_cap(cmd):
    """-> the longest word that stands alone on a line of cmd (0: no heredoc in cmd can be terminated)"""
    return max((len(m.group(1)) for m in map(TERM_LINE.fullmatch, cmd.split("\n")) if m), default=0)


_CAPPED = {}


def capped(rx, n):
    """rx with its tag limited to n characters (n >= 1)."""
    key = (rx.pattern, n)
    if key not in _CAPPED:
        if len(_CAPPED) > 64:
            _CAPPED.clear()
        assert rx.pattern.count(TAG_REST) == 1
        _CAPPED[key] = re.compile(rx.pattern.replace(TAG_REST, "[A-Za-z0-9_]{0,%d}" % (n - 1)), rx.flags)
    return _CAPPED[key]
PY_NAMES = re.compile(r"^(python(\d+(\.\d+)?)?|py)(\.exe)?$", re.I)
SEG_ENDS = {";", "&", "&&", "||", "|", "\n", "(", ")", "|&", ";;"}
SEG_KEYWORDS = {"then", "do", "else", "elif", "if", "while", "until", "!", "{", "}"}
# Wrappers that run the command after them: {name: (flags without a value, flags that take the next word, flags whose
# value can only be attached (-i{}), positional words after the flags)}. Short flags may be bundled (-En) and a value
# attached (-n1, --user=x). A flag NOT in the table might take a value, so the command word is unknown: that segment
# is judged by the old whole-text rule instead (block-side).
WRAP = {
    "env": ({"-i", "--ignore-environment", "-0", "--null", "-v", "--debug", "-"}, {"-u", "--unset", "-C", "--chdir"},
            set(), 0),
    "sudo": ({"-A", "--askpass", "-b", "--background", "-B", "--bell", "-E", "--preserve-env", "-H", "--set-home",
              "-i", "--login", "-k", "--reset-timestamp", "-K", "-n", "--non-interactive", "-P", "--preserve-groups",
              "-s", "--shell", "-S", "--stdin", "-N", "--no-update"},
             {"-u", "--user", "-g", "--group", "-h", "--host", "-C", "--close-from", "-p", "--prompt", "-r", "--role",
              "-t", "--type", "-U", "--other-user", "-T", "--command-timeout", "-D", "--chdir", "-R", "--chroot"},
             set(), 0),
    "timeout": ({"--preserve-status", "--foreground", "-v", "--verbose"}, {"-k", "--kill-after", "-s", "--signal"},
                set(), 1),
    "nice": (set(), {"-n", "--adjustment"}, set(), 0),
    "ionice": ({"-t", "--ignore"}, {"-c", "--class", "-n", "--classdata", "-p", "--pid", "-P", "--pgid", "-u",
                                    "--uid"}, set(), 0),
    "nohup": (set(), set(), set(), 0),
    "stdbuf": (set(), {"-i", "--input", "-o", "--output", "-e", "--error"}, set(), 0),
    "time": ({"-p", "--portability", "-a", "--append", "-v", "--verbose", "-q", "--quiet"},
             {"-f", "--format", "-o", "--output"}, set(), 0),
    "command": ({"-p"}, set(), set(), 0),
    "exec": ({"-c", "-l"}, {"-a"}, set(), 0),
    "winpty": ({"--mouse", "--showkey", "-Xallow-non-tty", "-Xplain"}, set(), set(), 0),
    "xargs": ({"-0", "--null", "-o", "--open-tty", "-p", "--interactive", "-r", "--no-run-if-empty", "-t", "--verbose",
               "-x", "--exit"},
              {"-I", "--replace", "-n", "--max-args", "-P", "--max-procs", "-L", "--max-lines", "-d", "--delimiter",
               "-E", "--eof", "-s", "--max-chars", "-a", "--arg-file", "--process-slot-var"}, {"-i", "-l", "-e"}, 0),
    "uv run": ({"--no-project", "--isolated", "--frozen", "--locked", "--no-sync", "--all-extras", "--no-dev", "--dev",
                "--offline", "-q", "--quiet", "-v", "--verbose", "--script", "--active", "--no-config"},
               {"--with", "--python", "-p", "--package", "--directory", "--project", "--env-file", "--extra",
                "--group", "-w", "--with-requirements", "--with-editable", "--index", "--index-url",
                "--extra-index-url", "-f", "--find-links"}, set(), 0),
    "poetry run": (set(), set(), set(), 0),
    "pipenv run": (set(), set(), set(), 0),
    "conda run": ({"--no-capture-output", "--live-stream", "-v", "--verbose", "--debug-wrapper-scripts"},
                  {"-n", "--name", "-p", "--prefix", "--cwd"}, set(), 0),
    "setsid": ({"-c", "--ctty", "-f", "--fork", "-w", "--wait"}, set(), set(), 0),
    "flock": ({"-s", "--shared", "-x", "-e", "--exclusive", "-u", "--unlock", "-n", "--nb", "--nonblock", "-o",
               "--close", "-F", "--no-fork", "--verbose"}, {"-w", "--wait", "--timeout", "-E", "--conflict-exit-code"},
              set(), 1),                                                         # flock [flags] <file> command
    "doas": ({"-L", "-n", "-s"}, {"-a", "-C", "-u"}, set(), 0),
    "chronic": ({"-e", "-v"}, set(), set(), 0),
    "strace": ({"-f", "-F", "-c", "-C", "-d", "-D", "-h", "-i", "-q", "-r", "-t", "-T", "-v", "-V", "-w", "-x", "-y",
                "-k", "-z", "-Z", "-n", "--follow-forks", "--summary-only", "--summary"},
               {"-o", "--output", "-p", "--attach", "-e", "-s", "--string-limit", "-u", "--user", "-E", "--env", "-a",
                "--columns", "-P", "--trace-path", "-I", "-b", "-X", "-S", "-O"}, set(), 0),
    "taskset": ({"-a", "--all-tasks", "-c", "--cpu-list", "-p", "--pid"}, set(), set(), 1),  # taskset <mask> command
    "pdm run": ({"-s", "--site-packages", "-g", "--global", "-v", "--verbose", "-q", "--quiet", "--json", "-l",
                 "--list", "--recreate"}, {"-p", "--project", "--venv"}, set(), 0),
    "hatch run": (set(), set(), set(), 0),
    "rye run": ({"-l", "--list", "-v", "--verbose", "-q", "--quiet"}, {"--pyproject"}, set(), 0),
    "pixi run": ({"--frozen", "--locked", "--no-lockfile-update", "--no-install", "--clean-env", "--revalidate", "-v",
                  "--verbose", "-q", "--quiet"}, {"-e", "--environment", "--manifest-path"}, set(), 0),
    "pyenv exec": (set(), set(), set(), 0),
}
TWO_WORD = {"uv": "run", "poetry": "run", "pipenv": "run", "conda": "run", "pdm": "run", "hatch": "run", "rye": "run",
            "pixi": "run", "pyenv": "exec"}                                      # `<tool> run ...`
# An unquoted Windows path loses its backslashes in the shell lexer: `C:\Python311\python.exe` reads as
# `C:Python311python.exe`. Such a word still names python (block-side; a separator-free name never reaches here).
EATEN_PY = re.compile(r"[^/\s]*python(\d+(\.\d+)?)?\.exe|[A-Za-z]:[^/\s]+python(\d+(\.\d+)?)?", re.I)
SHELL_NAMES = {"bash", "sh", "zsh", "dash", "bash.exe", "sh.exe"}


def _skip_flags(name, w, noval, val, attached, positional):
    """-> (index of the command word in w, confident)."""
    i = 0
    while i < len(w):
        a = w[i]
        if a == "--":
            i += 1
            break
        if name in ("env", "sudo") and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", a):
            i += 1                                       # env / sudo VAR=value
            continue
        if not a.startswith("-") or (a == "-" and a not in noval):
            break
        if a in noval or (name == "nice" and re.fullmatch(r"-\d+", a)):
            i += 1
            continue
        if a.startswith("--"):
            flag = a.split("=", 1)[0]
            if flag in val:
                i += 1 if "=" in a else 2
                continue
            if flag in noval:
                i += 1
                continue
            return i, False                              # unknown long flag: may take a value
        step = 1
        for j in range(1, len(a)):                       # bundled short flags: -En, -n1, -oL
            f = "-" + a[j]
            if f in val:
                step = 1 if j + 1 < len(a) else 2        # value attached, or the next word
                break
            if f in attached:
                break                                    # the rest of the word is its value
            if f not in noval:
                return i, False
        i += step
    if i + positional > len(w):
        return i, False
    return i + positional, True


def strip_wrappers(w):
    """Skip shell keywords, VAR=value words and wrapper commands WITH their own flags and arguments (`timeout 60`,
    `sudo -u x`, `xargs -n1`, `uv run --with y`). -> (words from the command word on, confident). Not confident when
    a wrapper carries a flag the table does not know (it may take a value, so the next word may not be the command)."""
    while w:
        a = w[0]
        if a in SEG_KEYWORDS or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", a):
            w = w[1:]
            continue
        head = os.path.basename(a.replace("\\", "/")).lower()
        head = head[:-4] if head.endswith(".exe") else head
        n = 1
        if len(w) > 1 and TWO_WORD.get(head) == w[1]:
            head, n = head + " " + w[1], 2
        if head not in WRAP:
            return w, True
        i, sure = _skip_flags(head, w[n:], *WRAP[head])
        if not sure:
            return w[n:], False
        w = w[n + i:]
    return w, True


def fed_by(w):
    """A pipe aside, does this segment give its command something on stdin? A `<` file or process substitution, a
    here-string, or a heredoc with a body (an unterminated one counts: bash feeds it to EOF)."""
    for i, a in enumerate(w):
        if a in ("<<<", "<(") or (a == "<" and i + 1 < len(w)):
            return True
        if a in ("<<", "<<-") and i + 1 < len(w) and w[i + 1] != "HD_EMPTY":
            return True
    return False


def starved_python(cmd, depth=0):
    """True when a segment whose COMMAND WORD is python runs `python -` with nothing on stdin: no pipe into it, no
    `<` redirect (a file or a process substitution), no here-string, and no heredoc - or only an EMPTY one - on that
    same segment. Text that merely mentions `python -` (an echo, a commit message, a grep pattern) is a quoted
    argument of another command and does not count. None when the command cannot be parsed."""
    import shlex
    cap = tag_cap(cmd) if "<<" in cmd else 0
    skel = cmd if not cap else capped(HD_ANY, cap).sub(
        lambda m: " << %s %s\n" % ("HD_BODY" if m.group("body").strip() else "HD_EMPTY", m.group("tail")), cmd)
    try:
        lex = shlex.shlex(skel, posix=True, punctuation_chars="();<>|&\n")
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        return None
    segs, cur, piped = [], [], False
    for t in toks + [";"]:
        if t in SEG_ENDS:
            if cur:
                segs.append((cur, piped))
            cur, piped = [], t in ("|", "|&")
        else:
            cur.append(t)
    for w, piped in segs:
        w, sure = strip_wrappers(w)
        if not w:
            continue
        if not sure:                                     # the command word is unknown: the old whole-text rule
            if not piped and PY_STDIN.search(" ".join(w)) and not fed_by(w):
                return True
            continue
        head = os.path.basename(w[0].replace("\\", "/"))
        if head.lower() in SHELL_NAMES and depth < 3 and "-c" in w[1:-1]:
            if starved_python(w[w.index("-c") + 1], depth + 1):
                return True
            continue
        if head in ("-c", "--command") and len(w) > 1 and depth < 3:       # `flock <file> -c '<command>'`
            if starved_python(w[1], depth + 1):
                return True
            continue
        if not PY_NAMES.match(head) and not EATEN_PY.fullmatch(w[0]):
            continue
        dash = False
        for a in w[1:]:
            if a == "-":
                dash = True
                break
            if not a.startswith("-") or a in ("-c", "-m") or a.startswith(("<", ">")):
                break                                    # a script, -c code or -m module: stdin is not the program
        if not dash or piped:
            continue
        fed = fed_by(w)
        if not fed:
            return True
    return False
PY_WRITES = re.compile(r"open\([^)\n]*,\s*(mode\s*=\s*)?['\"][wax]b?\+?['\"]|\.write_(text|bytes)\(")


def scan(cmd):
    """-> (python heredocs with a backslash, those with a doubled backslash, heredocs over MAX_LINES, python heredocs
    that write a file). One pass of the heredoc regex per hook call; none at all when the command has no '<<'."""
    slash, doubled, long_, writers = [], [], [], []
    if "<<" not in cmd:
        return slash, doubled, long_, writers
    cap = tag_cap(cmd)
    for m in (capped(HEREDOC, cap).finditer(cmd) if cap else ()):
        body, tag = m.group("body"), m.group("tag")
        py = PYTHON.search(m.group("head"))
        if py and "\\" in body:
            slash.append(tag)
            if "\\\\" in body:
                doubled.append(tag)
        if body.count("\n") + 1 > MAX_LINES:
            long_.append(tag)
        if py and PY_WRITES.search(body):
            writers.append(tag)
    return slash, doubled, long_, writers


MAX_COMMAND = 32 * 1024     # bytes. Bigger is file content: blocked unless `# heredoc-long-ok` is at the start or end.
# Each '<<' whose tag is never closed makes the heredoc regexes scan the rest of the command: the work grows with
# (number of '<<') x (length), and a few thousand C++ `cout <<` lines take seconds. Past this budget the heredocs are
# not parsed: every rule is judged on the whole text (whole_text_rules, block-side, no `# heredoc-long-ok` override),
# then the command is blocked as file content unless `# heredoc-long-ok` lifts the cap.
MAX_HEREDOC_WORK = 400 * 1000


def whole_text_rules(cmd):
    """Over the work cap the heredocs are not parsed one by one: every rule is judged on the whole text instead,
    block-side (a mention counts), and `# heredoc-long-ok` does not lift any of them. -> the reason, or None."""
    py = PYTHON.search(cmd)
    if py and "\\\\" in cmd:
        return "runs Python and holds a DOUBLED backslash (no override)"
    if py and "\\" in cmd and "# heredoc-ok" not in cmd:
        return "runs Python and holds a backslash ('# heredoc-ok' overrides)"
    if py and PY_WRITES.search(cmd) and "# heredoc-write-ok" not in cmd:
        return "runs Python that writes a file ('# heredoc-write-ok' overrides)"
    for m in PY_STDIN.finditer(cmd):                     # `python -` with no pipe before it, no `<` after it
        start, end = cmd.rfind("\n", 0, m.start()) + 1, cmd.find("\n", m.end() - 1)
        if "|" not in cmd[start:m.start()] and "<" not in cmd[m.end() - 1:len(cmd) if end < 0 else end]:
            return "runs `python -` with nothing on its stdin (no override)"
    cap = tag_cap(cmd) if PY_STDIN.search(cmd) else 0
    if cap and capped(EMPTY_HEREDOC, cap).search(cmd):
        return "runs `python -` and holds an EMPTY heredoc (no override)"
    m = PY_DASH_C_DQ.search(cmd)
    if m and "`" in cmd[m.end():] and "# backtick-ok" not in cmd:
        return "has `python -c \"...\"` with a backtick after it ('# backtick-ok' overrides)"
    return None


def verdict(cmd):
    """-> None (allow) or the message to block with"""
    return decide(cmd)[0]


def decide(cmd):
    """-> (None (allow) or the message to block with, the scan() result or None when the scan did not run). The
    heredoc scan runs at most once per hook call: main() reuses its result for the override log."""
    if len(cmd) > MAX_COMMAND and "# heredoc-long-ok" not in cmd[:200] + cmd[-200:]:
        return ("BLOCKED by heredoc-guard: this Bash command is %d KB. A command that large is file content: write it "
                "with the Write tool and run the file. '# heredoc-long-ok' at the start or end overrides."
                % (len(cmd) // 1024)), None
    markers = cmd.count("<<")
    if markers * len(cmd) > MAX_HEREDOC_WORK:
        size = (markers, max(1, len(cmd) // 1024))
        why = whole_text_rules(cmd)                      # before the marker: '# heredoc-long-ok' lifts the cap only
        if why:
            return ("BLOCKED by heredoc-guard: this Bash command holds %d '<<' in %d KB - too many to parse each "
                    "heredoc, so the rules are judged on the whole text - and it %s. Write the script with the Write "
                    "tool and run the file." % (size + (why,))), None
        if "# heredoc-long-ok" in cmd:
            return None, None
        return ("BLOCKED by heredoc-guard: this Bash command holds %d '<<' in %d KB. That is file content (source code "
                "with shift operators, or many heredocs): write it with the Write tool and run the file. "
                "'# heredoc-long-ok' overrides." % size), None
    found = scan(cmd)
    slash, doubled, long_, writers = found
    if doubled:
        return ("BLOCKED by heredoc-guard: this Bash command feeds a heredoc (" + ", ".join(doubled) + ") to Python and the "
                "script contains a DOUBLED backslash. The Bash tool collapses it to one even in a quoted heredoc, which "
                "changes what the script writes. "
                "'# heredoc-ok' does not override this case. Write the script with the Write tool and run the file."), found
    if slash and "# heredoc-ok" not in cmd:
        return ("BLOCKED by heredoc-guard: this Bash command feeds a heredoc (" + ", ".join(slash) + ") to Python and the "
                "script contains backslashes. The Bash tool unescapes one level even in a quoted heredoc, so '\\\\' becomes "
                "'\\' and regexes/Windows paths/string escapes break silently. Write the script with the Write tool (e.g. to the scratchpad) "
                "and run the file, or use the Edit tool for file edits. If you have checked the backslashes are safe, add "
                "'# heredoc-ok' to the command."), found
    if writers and "# heredoc-write-ok" not in cmd:
        return ("BLOCKED by heredoc-guard: this Python heredoc (" + ", ".join(writers) + ") WRITES a file. A find/replace-"
                "and-write is an edit: use the Edit tool (exact old and new text, no escaping layer), or Write the script "
                "to a file and run it. '# heredoc-write-ok' overrides when the heredoc is genuinely the right tool."), found
    starved = starved_python(cmd)
    if starved is None:                                  # unparseable (an unbalanced quote): the old whole-text test
        cap = tag_cap(cmd) if "<<" in cmd else 0
        starved = bool(PY_STDIN.search(cmd) and ((cap and capped(EMPTY_HEREDOC, cap).search(cmd)) or (
            "<<" not in cmd and not PIPED_PY.search(cmd) and not STDIN_REDIRECT.search(cmd))))
    if starved:
        return ("BLOCKED by heredoc-guard: `python -` with nothing on stdin (an EMPTY heredoc, or no heredoc, pipe or "
                "redirect). Python then waits on the tool's stdin or opens its interactive prompt, and the shell hangs "
                "until it is killed. Write the script with the Write tool and run the file."), found
    m = PY_DASH_C_DQ.search(cmd)
    if m and "`" in cmd[m.end():] and "# backtick-ok" not in cmd:
        return ("BLOCKED by heredoc-guard: `python -c \"...\"` with a BACKTICK after it. Inside double quotes bash runs "
                "`...` as a command, so the text vanishes and the words inside run as commands. Write the script with the Write tool and run the "
                "file. '# backtick-ok' overrides if the backtick is outside the quotes."), found
    if long_ and "# heredoc-long-ok" not in cmd:
        return ("BLOCKED by heredoc-guard: heredoc (" + ", ".join(long_) + ") is longer than " + str(MAX_LINES) + " lines. "
                "That is file content: one quote or apostrophe ends it early and the rest runs as shell. "
                "Write it with the Write tool and run or read the file. '# heredoc-long-ok' overrides."), found
    return None, found


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        long_body = "\n".join("line %d" % i for i in range(MAX_LINES + 5))
        shifts = "".join("x%d = (y << %d) | (z << 2)\n" % (i, i % 30) for i in range(150))     # over the '<<' cap
        echoes = "".join("echo $((1<<%d))\n" % (i % 9) for i in range(300))
        cases = {
            "python - <<'EOF'\nprint('a\\\\b')\nEOF\n": True,
            "python - <<EOF\nx = r'C:\\Users'\nEOF": True,
            "cd x; python3 - <<'PYEOF'\nimport re\nre.sub(r'\\d', '', s)\nPYEOF\n": True,
            "python - <<'EOF'\nprint('no backslash here')\nEOF\n": False,
            "cat <<'EOF' > f.txt\nC:\\path\nEOF\n": False,
            "python script.py C:\\\\Users": False,
            "python - <<'EOF'  # heredoc-ok\nprint('a\\nb')\nEOF\n": False,              # single backslash, checked: allowed
            "python - <<'EOF'  # heredoc-ok\nprint('a\\\\b')\nEOF\n": True,              # doubled: the override is refused
            "python - <<'EOF'\ns = 'x\\\\u0001y'\nEOF\necho done # heredoc-ok": True,
            "cat > f.html <<'EOF'\n" + long_body + "\nEOF\n": True,                       # file content
            "cat > f.html <<'EOF'\n" + long_body + "\nEOF\n# heredoc-long-ok": False,
            "git commit -F - <<'EOF'\nsubject\n\nbody line\nEOF\n": False,
            'python -I -c "t+=\'see `tools/x.py` here\'"': True,
            'python -I -c "print(1)"': False,
            "python -I -c 'print(\"`x`\")'": False,                                       # single quotes: bash leaves it
            'python -c "print(1)"; echo `date`  # backtick-ok': False,
            "echo " + "x" * (40 * 1024): True,                                   # 40 KB command: file content
            "echo " + "x" * (40 * 1024) + "  # heredoc-long-ok": False,
            "echo " + "x" * (20 * 1024): False,
            "python -I - <<'EOF'\nEOF\necho skip": True,                                   # empty heredoc: hangs
            "cd x && python - <<'X'\nX\n": True,
            "python -": True,                                                             # nothing on stdin at all
            "python3 -I -": True,
            "python -I - <<'EOF'\nprint(1)\nEOF\n": False,                                # a real body is fine
            "cat a.py | python -": False,                                                 # piped
            "python - < script.py": False,                                                # redirected from a file
            "python -I script.py - x": False,                                             # '-' as a script argument
            'echo "run python - to read stdin"': False,                                   # only MENTIONS python -
            'git commit -m "docs: explain python - usage"': False,
            'grep -rn "use python - here" docs': False,
            "python - <<'EOF'\nprint(1)\nEOF\ncat > empty.txt <<'X'\nX\n": False,      # the empty heredoc is cat's
            "python3 - < <(echo 'print(1)')": False,                                      # process substitution
            "python - <<< 'print(1)'": False,                                             # here-string
            "cat > e.txt <<'X'\nX\npython -": True,                                       # python's own stdin is empty
            "echo hi | grep h; python3 -I -": True,                                       # the pipe is not python's
            "bash -c 'python -'": True,                                                   # inside a shell -c string
            "FOO=1 python -": True,
            "python - <<'EOF'\nprint(1)\n": False,                                     # unterminated: bash feeds it
            "python -I - <<'EOF'\ns=open(p).read()\nopen(p,'w',encoding='utf-8').write(s)\nEOF\n": True,   # an edit
            "python - <<'EOF'\nfrom pathlib import Path\nPath('x').write_text('y')\nEOF\n": True,
            "python - <<'EOF'\nf = open('o', mode='a')\nEOF\n": True,
            "python - <<'EOF'\nprint(open('a.txt').read())\nEOF\n": False,              # reading is fine
            "python - <<'EOF'\nopen('o','w').write('1')\nEOF\n# heredoc-write-ok": False,
            "cat > f.txt <<'EOF'\nopen(p,'w')\nEOF\n": False,                             # not Python
            # `python -` behind a wrapper: the wrapper's own flags and arguments are skipped, not just its name
            "timeout 60 python -": True,
            "timeout 900 python - <<'EOF'\nEOF": True,
            "timeout -k 5 -s KILL 60 python3 -I -": True,
            "winpty python -": True,
            "uv run python -": True,
            "xargs python -": True,
            "env -i python -": True,
            "sudo -E python -": True,
            "sudo -u bob -E python -": True,
            "nice -n 5 python -": True,
            "conda run -n base python -": True,
            "poetry run python -": True,
            "xargs -n1 -I{} python -": True,
            "uv run --strange-flag python -": True,                        # unknown flag: old whole-text rule blocks
            "timeout 60 python script.py": False,
            "uv run python script.py": False,
            "xargs -n1 python script.py": False,
            "timeout 900 python - <<'EOF'\nprint(1)\nEOF\n": False,        # a real body
            "cat a.py | timeout 60 python -": False,                        # piped
            "timeout 60 python - < a.py": False,
            "sudo -u python python script.py": False,                       # -u takes a value: `python` is the user
            "timeout 60 echo 'python -'": False,                            # a mention behind a wrapper
            "uv run --strange-flag python script.py": False,
            # many '<<' that never close (C++ shifts): file content, decided without the quadratic scans
            "cat > a.cpp <<'EOF'\n" + "    std::cout << v << endl;\n" * 300: True,
            "cat > a.cpp <<'EOF'\n" + "    std::cout << v << endl;\n" * 300 + "# heredoc-long-ok": False,
            "x=$((1 << 4)); y=$((x << 2)); python3 -c 'print(1 << 3)'": False,             # a few shifts are fine
            # over the '<<' cap the rules run on the whole text, and '# heredoc-long-ok' lifts none of them
            "python - <<'EOF'\nimport re\nprint(re.sub('\\\\\\\\d', '', s))\n" + shifts + "EOF\n# heredoc-long-ok": True,
            "python - <<'EOF'\nimport re\nprint(re.sub('\\\\\\\\d', '', s))\n" + shifts + "EOF\n": True,
            "python - <<'EOF'\nimport re\nprint(re.sub('\\\\\\\\d', '', s))\n" + shifts + "EOF\n# heredoc-long-ok"
            "  # heredoc-ok": True,                                         # doubled: no override at all
            "python - <<'EOF'\nprint(re.sub('\\d', '', s))\n" + shifts + "EOF\n# heredoc-long-ok": True,  # one backslash
            "python - <<'EOF'\nprint(re.sub('\\d', '', s))\n" + shifts + "EOF\n# heredoc-long-ok # heredoc-ok": False,
            "python - <<'EOF'\nopen('o','w').write('1')\n" + shifts + "EOF\n# heredoc-long-ok": True,
            "python - <<'EOF'\nopen('o','w').write('1')\n" + shifts + "EOF\n": True,
            "timeout 60 python -  # heredoc-long-ok\n" + echoes: True,
            "timeout 60 python -\n" + echoes: True,
            "python - <<'EOF'\n" + echoes + "EOF\npython -  # heredoc-long-ok": True,
            "python -c \"print(`id`)\"  # heredoc-long-ok\n" + echoes: True,
            "python -c \"print(`id`)\"\n" + echoes: True,
            "python - <<'EOF'\n" + shifts + "EOF\n# heredoc-long-ok": False,           # a real body, fed: allowed
            "cat x.py | python -  # heredoc-long-ok\n" + echoes: False,
            # more wrappers with their own argument shapes
            "setsid python -": True,
            "setsid -w python -": True,
            "flock /tmp/l python -": True,
            "flock -w 5 /tmp/l python3 -": True,
            "flock /tmp/l -c 'python -'": True,
            "doas python -": True,
            "doas -u bob python -": True,
            "chronic python -": True,
            "strace -f -o trace.txt python -": True,
            "taskset 1 python -": True,
            "taskset -c 0-3 python -": True,
            "pdm run python -": True,
            "hatch run python -": True,
            "rye run python -": True,
            "pixi run -e dev python -": True,
            "pyenv exec python -": True,
            "flock /tmp/l python script.py": False,
            "taskset 1 python script.py": False,
            "pyenv exec python -m pip list": False,
            "ssh host python -": False,                                     # a remote command is not parsed
            "C:\\Python311\\python.exe -": True,                            # the lexer eats the backslashes
            "C:\\Python311\\python.exe script.py": False,
            ".\\venv\\Scripts\\python.exe -": True,
        }
        bad = 0
        for c, want in cases.items():
            got = verdict(c) is not None
            bad += got != want
            print(("ok  " if got == want else "BAD ") + repr(c[:50]), got)
        bad += scan_once_selftest()
        bad += perf_selftest()
        bad += banner_selftest()
        print("failures:", bad)
        sys.exit(1 if bad else 0)
    data = read_payload("heredoc-guard")                     # bad stdin is logged, then allowed
    cmd = (data.get("tool_input") or {}).get("command") or ""
    msg, found = decide(cmd)
    if msg is None:
        # found is None: allowed by an override before the scan ran (do not scan it now)
        slash, _, long_, _ = ([], [], ["<<"], []) if found is None else found
        used = [m for m in ("heredoc-long-ok", "heredoc-ok", "heredoc-write-ok", "backtick-ok") if "# " + m in cmd]
        if used and (slash or long_ or "# heredoc-write-ok" in cmd or "# backtick-ok" in cmd):
            # which override and how big - never the command text (it can carry secrets). append_log never raises.
            append_log("heredoc-overrides.log", "%s %s override=%s lines=%d" % (
                datetime.datetime.now().isoformat(timespec="seconds"), str(data.get("session_id", "?"))[:36],
                ",".join(used), cmd.count("\n") + 1))
        sys.exit(0)
    sys.stderr.write(week_banner(data) + msg + "\n")
    sys.exit(2)


BLOCK_LOG = "heredoc-blocks.log"


def week_banner(data, now=None):
    """Count this block into state/heredoc-blocks.log and say how many there were in the last 7 days (this log and its
    rotated generation). Never raises: on any error the block goes out without the banner."""
    try:
        now = now or datetime.datetime.now()
        append_log(BLOCK_LOG, "%s %s" % (now.isoformat(timespec="seconds"), str(data.get("session_id", "?"))[:8]))
        cutoff = (now - datetime.timedelta(days=7)).isoformat(timespec="seconds")
        n = 0
        base = os.path.join(_hooklib.STATE, BLOCK_LOG)
        for p in (base + ".1", base):
            try:
                with open(p, encoding="ascii", errors="replace") as f:
                    n += sum(1 for line in f if line[:19] >= cutoff)
            except OSError:
                pass
        return ("[heredoc block #%d in the last 7 days - a habit, not a one-off: for any Python with a backslash or "
                "any file content, Write the file FIRST, then run it.] " % n)
    except Exception:
        return ""


def scan_once_selftest():
    """A command with no '<<' runs no heredoc regex; one with heredocs runs the heredoc scan exactly once."""
    global capped
    real, calls, bad = capped, [], 0

    def counting(rx, n):
        calls.append(rx.pattern == HEREDOC.pattern)
        return real(rx, n)
    capped = counting
    try:
        for cmd, want, what in (("echo hi\nEOF\npython3 x.py\n", 0, "no '<<': no heredoc regex runs"),
                                ("python - <<'EOF'\nprint(1)\nEOF\n", 1, "heredocs: the scan runs once")):
            del calls[:]
            decide(cmd)
            got = calls.count(True) if want else len(calls)
            bad += got != want
            print(("ok  " if got == want else "BAD ") + what, got)
    finally:
        capped = real
    return bad


def perf_selftest(bound=2.0, runs=10):
    """Every command under the size cap is decided fast: each heredoc regex alone on the shape that is quadratic
    without its guard (10 runs), then verdict() on worst shapes. The first slow check stops the rest: a regressed
    shape takes seconds per run."""
    import time
    line = "y" * (30 * 1024)
    word = "cat <<" + "a" * (30 * 1024)                    # a tag letter, no newline after it
    checks = [
        ("HEREDOC: a 30 KB line with no '<<' (line-start anchor)", lambda: list(capped(HEREDOC, 3).finditer(line))),
        ("HEREDOC: a 30 KB tag with no newline (lookahead)", lambda: list(capped(HEREDOC, len(word)).finditer(word))),
        ("HEREDOC: 30,000 blank lines after the header (body ends at a run's first newline)",
         lambda: list(capped(HEREDOC, 3).finditer("cat <<EOF\n" + "\n" * 30000))),
        ("HD_ANY: a 30 KB tag with no newline (lookahead)", lambda: capped(HD_ANY, len(word)).sub("", word)),
        ("EMPTY_HEREDOC: a 30 KB tag with no newline (lookahead)",
         lambda: capped(EMPTY_HEREDOC, len(word)).search(word)),
        ("scan: a 12 KB tag word, then 10,000 lines (tag capped at the longest lone word)",
         lambda: scan("cat <<" + "a" * 12000 + "\n" + "b\n" * 10000)),
        ("verdict: a 32 KB line with no '<<'", lambda: verdict("echo " + "y" * (32 * 1024 - 100))),
        ("verdict: one '<<' at the end of a 30 KB line", lambda: verdict("echo " + line + " <<EOF")),
        ("verdict: 64 '<<' on one 30 KB line", lambda: verdict("echo " + ("y" * 470 + " <<T") * 64)),
        ("verdict: 2,000 short '<<' lines", lambda: verdict("cat > f <<'EOF'\n" + "a << b\n" * 2000)),
    ]
    for i, (what, fn) in enumerate(checks):
        t0 = time.perf_counter()
        for _ in range(runs):
            fn()
            if time.perf_counter() - t0 > bound:
                break
        dt = time.perf_counter() - t0
        if dt > bound:
            print("BAD  %s: over %.1f s - the remaining %d checks are skipped" % (what, bound, len(checks) - i - 1))
            return 1
        print("ok   %s (%d runs)" % (what, runs))
    return 0


def banner_selftest():
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="heredoc-")
    saved, _hooklib.STATE = _hooklib.STATE, tmp
    bad = 0
    try:
        old = datetime.datetime.now() - datetime.timedelta(days=9)
        week_banner({"session_id": "s"}, now=old)                       # outside the window: not counted
        week_banner({"session_id": "s"})
        got = week_banner({"session_id": "s"})
        want = "[heredoc block #2 in the last 7 days"
        bad += not got.startswith(want)
        print(("ok  " if got.startswith(want) else "BAD ") + "block counter counts the last 7 days only", got[:40])
    finally:
        _hooklib.STATE = saved
        shutil.rmtree(tmp, ignore_errors=True)
    return bad


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        fail_open("heredoc-guard", exc)
