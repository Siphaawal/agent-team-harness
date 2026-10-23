#!/usr/bin/env python3
"""stamp-guard.py - PreToolUse hook (matcher: Write|Edit). Blocks clock stamps that lie in the FUTURE.

Why: an agent that read the clock once and then extrapolated from how long the work FELT writes times that never
happened into logs and memories. A stamp READ from the clock is never ahead of it, so any datetime written into a file
that is later than the wall clock (plus a 1-minute grace) was invented, and the write is refused with the clock
printed, so the fix is one edit.

Patterns caught:
  - ISO datetimes            2026-01-06T01:05:00  2026-01-06 01:05  (+ optional Z / +hh:mm / zone word)
  - date + clock in prose    2026-01-06 01:45 UTC   2026-01-05 ~22:00 CET
  - MM-DD + clock + zone     01-06 01:48 PST   or   01:48 PST 01-06
  - epoch milliseconds under a time-ish key ("at": 1767225600000)
Dates alone (no clock) are never blocked. A past stamp is never blocked. `# stamp-ok` anywhere in the content
overrides - in any comment syntax: `# stamp-ok`, `<!-- stamp-ok -->` - (audited to .claude/state/stamp-overrides.log) for a
deliberately future time (a schedule, a deadline). An MM-DD stamp more than 180 days ahead is read as last year.
A clock with no zone is read as the machine's local time.
"""
import datetime
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _hooklib import append_log, fail_open, read_payload  # noqa: E402

GRACE = datetime.timedelta(minutes=1)
ZONES = {"UTC": 0, "GMT": 0, "Z": 0, "EST": -5, "EDT": -4, "CST": -6, "CDT": -5, "MST": -7, "MDT": -6,
         "PST": -8, "PDT": -7, "AKST": -9, "AKDT": -8, "HST": -10, "WET": 0, "WEST": 1, "BST": 1,
         "CET": 1, "CEST": 2, "EET": 2, "EEST": 3, "JST": 9, "KST": 9, "AEST": 10, "AEDT": 11}
ZW = "|".join(sorted((z for z in ZONES if z != "Z"), key=len, reverse=True))
ISO = re.compile(r"(?P<y>20\d\d)-(?P<m>\d\d)-(?P<d>\d\d)[T ](?P<H>\d\d):(?P<M>\d\d)(?::\d\d(?:\.\d+)?)?(?P<tz>Z|[+-]\d\d:?\d\d)?")
PROSE = re.compile(r"(?P<y>20\d\d)-(?P<m>\d\d)-(?P<d>\d\d)[^\n]{0,24}?~?(?P<H>\d\d):(?P<M>\d\d)\s*(?P<tz>%s)\b" % ZW)
SHORT = re.compile(r"(?<![\d-])(?P<m>\d\d)-(?P<d>\d\d)\)?\s*[^\n]{0,8}?~?(?P<H>\d\d):(?P<M>\d\d)\s*(?P<tz>%s)\b"
                   r"|~?(?P<H2>\d\d):(?P<M2>\d\d)\s*(?P<tz2>%s)\s*(?P<m2>\d\d)-(?P<d2>\d\d)(?![\d-])" % (ZW, ZW))
EPOCH_MS = re.compile(r"[\"']?(?:at|ts|time|timestamp|updatedAt|createdAt)[\"']?\s*[:=]\s*(?P<ms>1[6-9]\d{11})(?!\d)")


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def to_utc(y, m, d, H, M, tz):
    try:
        t = datetime.datetime(int(y), int(m), int(d), int(H), int(M))
    except ValueError:
        return None
    if tz in ZONES:
        return t.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=ZONES[tz])))
    if tz and re.match(r"[+-]\d\d:?\d\d$", tz):
        sign = 1 if tz[0] == "+" else -1
        return t.replace(tzinfo=datetime.timezone(sign * datetime.timedelta(hours=int(tz[1:3]), minutes=int(tz[-2:]))))
    return t.replace(tzinfo=datetime.datetime.now().astimezone().tzinfo)


def future_stamps(text, now=None):
    """-> list of (matched text, parsed utc) that lie beyond now + GRACE"""
    now = now or now_utc()
    year = now.astimezone().year
    out, seen = [], set()

    def add(s, t):
        if t and t > now + GRACE and s not in seen:
            seen.add(s)
            out.append((s, t))

    for m in ISO.finditer(text):
        tz = m.group("tz")
        if not tz:
            tail = re.match(r"\s*(%s)\b" % ZW, text[m.end():m.end() + 6])
            tz = tail.group(1) if tail else None
        add(m.group(0), to_utc(m.group("y"), m.group("m"), m.group("d"), m.group("H"), m.group("M"), tz))
    for m in PROSE.finditer(text):
        add(m.group(0), to_utc(m.group("y"), m.group("m"), m.group("d"), m.group("H"), m.group("M"), m.group("tz")))
    for m in SHORT.finditer(text):
        if m.group("H"):
            args = (m.group("m"), m.group("d"), m.group("H"), m.group("M"), m.group("tz"))
        else:
            args = (m.group("m2"), m.group("d2"), m.group("H2"), m.group("M2"), m.group("tz2"))
        t = to_utc(year, *args)
        if t and t - now > datetime.timedelta(days=180):     # "12-31 23:00" written on Jan 1 means LAST year
            t = to_utc(year - 1, *args)
        add(m.group(0), t)
    for m in EPOCH_MS.finditer(text):
        add(m.group(0), datetime.datetime.fromtimestamp(int(m.group("ms")) / 1000, datetime.timezone.utc))
    return out


def content_of(data):
    ti = data.get("tool_input") or {}
    return "\n".join([ti.get("content") or "", ti.get("new_string") or ""])


def selftest():
    now = datetime.datetime(2026, 1, 6, 2, 25, tzinfo=datetime.timezone.utc)
    cases = {
        "modified: 2026-01-06T01:05:00.000+03:00": False,          # 22:05Z on the 5th: past
        "modified: 2026-01-06T04:05:00.000-04:00": True,           # 08:05Z: ahead
        "Last synced: 2026-01-06 03:48 UTC (x)": True,
        "Last synced: 2026-01-06 02:00 UTC (x)": False,
        "(~05:10 CET 01-06)": True,                                # 04:10Z
        "(~02:10 CET 01-06)": False,                               # 01:10Z
        "re-read 01-05 19:30 PST": True,                           # 19:30 PST 01-05 = 03:30Z 01-06, an hour ahead
        "re-read 01-05 17:30 PST": False,                          # = 01:30Z 01-06, past
        "2026-01-06 02:26 UTC": False,                             # inside the grace
        "2026-01-06 02:30 UTC": True,
        "exportDate 2026-01-11, run": False,                       # date only
        "2026-01-06T01:50:28.007Z": False,
        "2026-01-06T03:00:00Z": True,
        '{"d": "done", "at": 1799999000000}': True,                # far ahead
        '{"d": "done", "at": 1700000000000}': False,               # past
        '{"count": 1799999000000}': False,                         # not a time key
        "version 2026-01-06 build 1": False,
        "reviewed 12-31 23:00 UTC": False,                         # MM-DD near New Year = last year  # stamp-ok
    }
    bad = 0
    for c, want in cases.items():
        got = bool(future_stamps(c, now))
        bad += got != want
        print(("ok  " if got == want else "BAD ") + repr(c), got)
    print("failures:", bad)
    return 1 if bad else 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("test", "--self-test"):
        sys.exit(selftest())
    data = read_payload("stamp-guard")
    text = content_of(data)
    if not text.strip():
        sys.exit(0)
    hits = future_stamps(text)
    if not hits:
        sys.exit(0)
    now = now_utc()
    local = now.astimezone()
    if "stamp-ok" in text:          # the marker may sit in any comment syntax (# , <!-- -->, //), so match the word
        append_log("stamp-overrides.log", "%s %s %r" % (local.isoformat(timespec="seconds"),
                                                       data.get("session_id", "?"), [h[0] for h in hits]))
        sys.exit(0)
    shown = ", ".join("%r (= %s)" % (h[0], h[1].astimezone().strftime("%Y-%m-%d %H:%M %Z")) for h in hits[:4])
    sys.stderr.write(
        "BLOCKED by stamp-guard: this write carries a clock stamp in the FUTURE - " + shown + ". "
        "The wall clock is %s (%s). A time not read from the clock in THIS turn is invented. Run `date; date -u`, "
        "write the time you read, and label the zone. For a deliberately future time add '# stamp-ok'.\n"
        % (local.strftime("%Y-%m-%d %H:%M %Z"), now.strftime("%H:%M UTC")))
    sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        fail_open("stamp-guard", exc)
