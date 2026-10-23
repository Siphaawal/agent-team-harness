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
"""
import datetime
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _hooklib import STATE, fail_open, read_payload  # noqa: E402
HEREDOC = re.compile(r"(?P<head>[^\n]*?)<<-?\s*(?P<q>['\"]?)(?P<tag>[A-Za-z_][A-Za-z0-9_]*)(?P=q)[^\n]*\n(?P<body>.*?)\n\s*(?P=tag)\s*(?:\n|$)", re.S)
PYTHON = re.compile(r"(^|[\s;&|(])(python3?|py)(\.exe)?(\s|$)")
MAX_LINES = 60
PY_DASH_C_DQ = re.compile(r"(^|[\s;&|(])(python3?|py)(\.exe)?(\s+-[A-Za-z]+)*\s+-c\s+\"")   # python [-I] -c "


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
        }
        bad = 0
        for c, want in cases.items():
            got = verdict(c) is not None
            bad += got != want
            print(("ok  " if got == want else "BAD ") + repr(c[:50]), got)
        print("failures:", bad)
        sys.exit(1 if bad else 0)
    data = read_payload("heredoc-guard")                     # bad stdin is logged, then allowed
    cmd = (data.get("tool_input") or {}).get("command") or ""
    msg = verdict(cmd)
    if msg is None:
        slash, _, long_ = scan(cmd)
        if slash or long_:                                    # an override was used: keep the audit trail
            try:                                              # an unwritable log must not turn an allow into exit 1
                os.makedirs(STATE, exist_ok=True)
                with open(os.path.join(STATE, "heredoc-overrides.log"), "a", encoding="ascii", errors="replace") as f:
                    # which override and how big - never the command text (it can carry secrets)
                    f.write("%s %s override=%s lines=%d\n" % (
                        datetime.datetime.now().isoformat(timespec="seconds"), data.get("session_id", "?"),
                        "heredoc-long-ok" if long_ else "heredoc-ok", cmd.count("\n") + 1))
            except Exception:
                pass
        sys.exit(0)
    sys.stderr.write(msg + "\n")
    sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        fail_open("heredoc-guard", exc)
