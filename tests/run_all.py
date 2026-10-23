#!/usr/bin/env python3
"""run_all.py - run every tests/test_*.py and every hook self-test; exit non-zero if any fails.

    python3 tests/run_all.py [-k substring] [--strict]

Each test runs in its own process with stdin closed (a child that waits on an inherited pipe would hang the run) and a
timeout. Prints one PASS / SKIP / FAIL line per test and, for a failure, the last 25 lines of its output.
A SKIP (a runtime such as node missing) is NOT a pass: with --strict, or when the CI environment variable is set, any
skip fails the run, so a CI box without node cannot go green on Insight it never tested.
"""
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable or "python3"
HOOK_SELFTESTS = [
    [".claude/hooks/git-guard.py", "test"],
    [".claude/hooks/heredoc-guard.py", "test"],
    [".claude/hooks/stamp-guard.py", "test"],
    [".claude/hooks/session-log.py", "test"],
]


def main(argv):
    k = argv[argv.index("-k") + 1] if "-k" in argv and argv.index("-k") + 1 < len(argv) else ""
    jobs = [[PY, "-I"] + h for h in HOOK_SELFTESTS]
    tdir = os.path.join(ROOT, "tests")
    jobs += [[PY, "-I", os.path.join("tests", n)] for n in sorted(os.listdir(tdir))
             if n.startswith("test_") and n.endswith(".py")]
    jobs = [j for j in jobs if k in " ".join(j)]
    if not jobs:
        print("no tests matched")
        return 1
    failed = skipped = 0
    for cmd in jobs:
        name = " ".join(cmd[2:])
        t0 = time.time()
        try:
            r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL,
                               encoding="utf-8", errors="replace")
            rc, out = r.returncode, r.stdout + r.stderr
        except subprocess.TimeoutExpired:
            rc, out = 124, "TIMEOUT after 300 s"
        ok = rc == 0
        failed += not ok
        # a skipped test is not a passed one: say SKIP and why, or a missing runtime reads as green
        sk = re.search(r"OK \(skipped=(\d+)\)", out)
        label = "FAIL" if not ok else ("SKIP" if sk else "PASS")
        skipped += 1 if (ok and sk) else 0
        why = ""
        if ok and sk:
            reasons = sorted(set(re.findall(r"skipped '([^']+)'", out)))
            why = "  (%s skipped: %s)" % (sk.group(1), "; ".join(reasons) or "see output")
        print("%s %-48s %5.1fs%s" % (label, name, time.time() - t0, why))
        if not ok:
            print("\n".join("    " + l for l in out.strip().splitlines()[-25:]))
    print("run_all: %d of %d passed%s" % (len(jobs) - failed - skipped, len(jobs),
                                         ", %d SKIPPED (not verified on this machine)" % skipped if skipped else ""))
    strict = "--strict" in argv or bool(os.environ.get("CI"))
    if skipped and strict:
        print("run_all: FAIL - skips are not allowed in --strict / CI mode")
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
