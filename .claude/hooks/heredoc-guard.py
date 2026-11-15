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
     runs the rest as shell. File content goes through Write/Edit. `# heredoc-long-ok` overrides.
  5. A Python heredoc that WRITES a file (open(..., 'w'/'a'), .write_text(, .write_bytes() -> block: a find/replace-
     and-write is an edit, and the Edit tool does it with no escaping layer. `# heredoc-write-ok` overrides.
  6. `python -` with NOTHING on stdin (an empty heredoc, or no heredoc or pipe at all) -> block: the interpreter waits
     on the tool's stdin or opens its interactive prompt, and the shell hangs until it is killed. No override.
     Judged per shell segment where python is the COMMAND WORD, so text that only mentions `python -` (an echo, a
     commit message, a grep pattern) passes; a pipe, `<` file, `< <(...)`, here-string or non-empty heredoc on that
     same segment counts as stdin.

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
HEREDOC = re.compile(r"(?P<head>[^\n]*?)<<-?\s*(?P<q>['\"]?)(?P<tag>[A-Za-z_][A-Za-z0-9_]*)(?P=q)[^\n]*\n(?P<body>.*?)\n\s*(?P=tag)\s*(?:\n|$)", re.S)
PYTHON = re.compile(r"(^|[\s;&|(])(python3?|py)(\.exe)?(\s|$)")
MAX_LINES = 60
PY_DASH_C_DQ = re.compile(r"(^|[\s;&|(])(python3?|py)(\.exe)?(\s+-[A-Za-z]+)*\s+-c\s+\"")   # python [-I] -c "
PY_STDIN = re.compile(r"(^|[\s;&|(])(python3?|py)(\.exe)?(\s+-[A-Za-z]+)*\s+-(\s|$)")        # python [-I] -
EMPTY_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1[^\n]*\n\s*\2\s*(\n|$)")
PIPED_PY = re.compile(r"\|\s*(python3?|py)(\.exe)?\b")
STDIN_REDIRECT = re.compile(r"(^|[^<])<\s*[^<\s(]")                                          # python - < file.py
# One heredoc (header, body, terminator); `<<<` is a here-string, not a heredoc. The body may be empty.
HD_ANY = re.compile(r"(?<!<)<<(?!<)-?[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1(?P<tail>[^\n]*)\n(?P<body>(?:.*?\n)??)"
                    r"[ \t]*\2[ \t]*(?:\n|$)", re.S)
PY_NAMES = re.compile(r"^(python(\d+(\.\d+)?)?|py)(\.exe)?$", re.I)
SEG_ENDS = {";", "&", "&&", "||", "|", "\n", "(", ")", "|&", ";;"}
SEG_WRAPPERS = {"env", "exec", "command", "time", "nice", "nohup", "sudo", "then", "do", "else", "if", "while", "!",
                "{", "}"}
SHELL_NAMES = {"bash", "sh", "zsh", "dash", "bash.exe", "sh.exe"}


def starved_python(cmd, depth=0):
    """True when a segment whose COMMAND WORD is python runs `python -` with nothing on stdin: no pipe into it, no
    `<` redirect (a file or a process substitution), no here-string, and no heredoc - or only an EMPTY one - on that
    same segment. Text that merely mentions `python -` (an echo, a commit message, a grep pattern) is a quoted
    argument of another command and does not count. None when the command cannot be parsed."""
    import shlex
    skel = HD_ANY.sub(lambda m: " << %s %s\n" % ("HD_BODY" if m.group("body").strip() else "HD_EMPTY",
                                                  m.group("tail")), cmd)
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
        while w and (w[0] in SEG_WRAPPERS or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w[0])):
            w = w[1:]
        if not w:
            continue
        head = os.path.basename(w[0].replace("\\", "/"))
        if head.lower() in SHELL_NAMES and depth < 3 and "-c" in w[1:-1]:
            if starved_python(w[w.index("-c") + 1], depth + 1):
                return True
            continue
        if not PY_NAMES.match(head):
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
        fed = False
        for i, a in enumerate(w):
            if a in ("<<<", "<(") or (a == "<" and i + 1 < len(w)):
                fed = True
            elif a in ("<<", "<<-") and i + 1 < len(w) and w[i + 1] != "HD_EMPTY":
                fed = True                               # a body, or an unterminated heredoc (bash feeds it to EOF)
        if not fed:
            return True
    return False
PY_WRITES = re.compile(r"open\([^)\n]*,\s*(mode\s*=\s*)?['\"][wax]b?\+?['\"]|\.write_(text|bytes)\(")


def scan(cmd):
    """-> (python heredocs with a backslash, those with a doubled backslash, heredocs over MAX_LINES)"""
    slash, doubled, long_ = [], [], []
    for m in HEREDOC.finditer(cmd):
        body, tag = m.group("body"), m.group("tag")
        if PYTHON.search(m.group("head")) and "\\" in body:
            slash.append(tag)
            if "\\\\" in body:
                doubled.append(tag)
        if body.count("\n") + 1 > MAX_LINES:
            long_.append(tag)
    return slash, doubled, long_


MAX_COMMAND = 32 * 1024     # bytes. Bigger is file content - and the heredoc regex is quadratic on huge input
                            # (140 KB took 57 s), which would run into the hook timeout and fail open.


def verdict(cmd):
    """-> None (allow) or the message to block with"""
    if len(cmd) > MAX_COMMAND and "# heredoc-long-ok" not in cmd[:200] + cmd[-200:]:
        return ("BLOCKED by heredoc-guard: this Bash command is %d KB. A command that large is file content: write it "
                "with the Write tool and run the file. '# heredoc-long-ok' at the start or end overrides."
                % (len(cmd) // 1024))
    slash, doubled, long_ = scan(cmd)
    if doubled:
        return ("BLOCKED by heredoc-guard: this Bash command feeds a heredoc (" + ", ".join(doubled) + ") to Python and the "
                "script contains a DOUBLED backslash. The Bash tool collapses it to one even in a quoted heredoc, which "
                "changes what the script writes. "
                "'# heredoc-ok' does not override this case. Write the script with the Write tool and run the file.")
    if slash and "# heredoc-ok" not in cmd:
        return ("BLOCKED by heredoc-guard: this Bash command feeds a heredoc (" + ", ".join(slash) + ") to Python and the "
                "script contains backslashes. The Bash tool unescapes one level even in a quoted heredoc, so '\\\\' becomes "
                "'\\' and regexes/Windows paths/string escapes break silently. Write the script with the Write tool (e.g. to the scratchpad) "
                "and run the file, or use the Edit tool for file edits. If you have checked the backslashes are safe, add "
                "'# heredoc-ok' to the command.")
    writers = [h.group("tag") for h in HEREDOC.finditer(cmd)
               if PYTHON.search(h.group("head")) and PY_WRITES.search(h.group("body"))]
    if writers and "# heredoc-write-ok" not in cmd:
        return ("BLOCKED by heredoc-guard: this Python heredoc (" + ", ".join(writers) + ") WRITES a file. A find/replace-"
                "and-write is an edit: use the Edit tool (exact old and new text, no escaping layer), or Write the script "
                "to a file and run it. '# heredoc-write-ok' overrides when the heredoc is genuinely the right tool.")
    starved = starved_python(cmd)
    if starved is None:                                  # unparseable (an unbalanced quote): the old whole-text test
        starved = bool(PY_STDIN.search(cmd) and (EMPTY_HEREDOC.search(cmd) or (
            "<<" not in cmd and not PIPED_PY.search(cmd) and not STDIN_REDIRECT.search(cmd))))
    if starved:
        return ("BLOCKED by heredoc-guard: `python -` with nothing on stdin (an EMPTY heredoc, or no heredoc, pipe or "
                "redirect). Python then waits on the tool's stdin or opens its interactive prompt, and the shell hangs "
                "until it is killed. Write the script with the Write tool and run the file.")
    m = PY_DASH_C_DQ.search(cmd)
    if m and "`" in cmd[m.end():] and "# backtick-ok" not in cmd:
        return ("BLOCKED by heredoc-guard: `python -c \"...\"` with a BACKTICK after it. Inside double quotes bash runs "
                "`...` as a command, so the text vanishes and the words inside run as commands. Write the script with the Write tool and run the "
                "file. '# backtick-ok' overrides if the backtick is outside the quotes.")
    if long_ and "# heredoc-long-ok" not in cmd:
        return ("BLOCKED by heredoc-guard: heredoc (" + ", ".join(long_) + ") is longer than " + str(MAX_LINES) + " lines. "
                "That is file content: one quote or apostrophe ends it early and the rest runs as shell. "
                "Write it with the Write tool and run or read the file. '# heredoc-long-ok' overrides.")
    return None


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        long_body = "\n".join("line %d" % i for i in range(MAX_LINES + 5))
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
        }
        bad = 0
        for c, want in cases.items():
            got = verdict(c) is not None
            bad += got != want
            print(("ok  " if got == want else "BAD ") + repr(c[:50]), got)
        bad += banner_selftest()
        print("failures:", bad)
        sys.exit(1 if bad else 0)
    data = read_payload("heredoc-guard")                     # bad stdin is logged, then allowed
    cmd = (data.get("tool_input") or {}).get("command") or ""
    msg = verdict(cmd)
    if msg is None:
        slash, _, long_ = scan(cmd)
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
