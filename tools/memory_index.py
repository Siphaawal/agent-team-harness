#!/usr/bin/env python3
"""memory_index.py - regenerate memory/_gen/INDEX.md and memory/_gen/TRIGGERS.md.

Both files are GENERATED from the memories' frontmatter (never hand-edit them;
they are gitignored and rebuilt at every session start). A hand-kept index
drifts, and drift is invisible: it keeps serving a reversed decision as truth.

INDEX.md groups, in this order (the order is what survives the size cap):
  INVALID (lint errors, loud and first) / superseded / ephemeral-and-stale /
  rules / decisions / lessons / everything else
TRIGGERS.md: the trigger question of every active lesson with tier0 true,
newest first.

Writes are atomic. An EMPTY input never overwrites a real index: when no topic
or lesson is found but _gen/ holds anything other than the empty placeholders,
nothing is written and the exit code is 2 (a wiped or mis-pointed checkout must
not erase the index everyone loads). --force overrides that guard. A missing
memory dir is always refused.

Exit: 0 written, 1 written but lint errors present, 2 refused, 64 usage error
      (bad flag, bad --today; nothing is read or written).
      --check: 0 the files on disk match a fresh render, 1 they do not.
      --print index|triggers: stdout gets exactly the bytes the writer would write
        ("(no memories yet)" / "(no lessons yet)" when empty); writes nothing;
        always exit 0, "(index unavailable: <ErrorClass>)" on any failure.
      --print lessons-json: {"lessons": [{name, description, trigger, created,
        status, tier0, tags, errors, file}]} for every memory/lessons/*.md, newest
        created first, through the same parser and linter as everything else
        (tier0 is a real bool; errors is [] when the lesson is valid). For Insight,
        so it never needs a frontmatter parser of its own. UTF-8 bytes, exit 0,
        {"lessons": [], "error": "<ErrorClass>"} on any failure.
      --quiet: no stdout on success; errors still go to stderr; same exit codes.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common       # noqa: E402
import memory_lint  # noqa: E402

INDEX_CAP = 24000      # bytes; the harness truncates large always-loaded files
TRIGGERS_CAP = 12000   # bytes; TRIGGERS.md is part of tier 0
DESC_LEN = 120
# what the files (and --print) hold when there is nothing to list
EMPTY_INDEX = "(no memories yet)\n"
EMPTY_TRIGGERS = "(no lessons yet)\n"

GROUPS = (
    ("invalid", "INVALID FRONTMATTER - fix these (run tools/memory_lint.py)"),
    ("superseded", "Superseded - do NOT act on these"),
    ("stale", "Ephemeral and stale - re-verify before acting"),
    ("rule", "Rules"),
    ("decision", "Decisions"),
    ("lesson", "Lessons"),
    ("other", "Everything else"),
)
PROTECTED = ("invalid", "superseded", "rule")   # never cut by the size cap


def _clip(s, n):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n].rstrip() + "..."


def _sort_key(m):
    # newest created first, then name; undated last
    c = m.created
    return (-(c.toordinal() if c else 0), m.slug)


def _group_of(m, errors, today):
    if errors:
        return "invalid"
    if m.status == "superseded":
        return "superseded"
    if m.status == "ephemeral":
        age = m.age_days(today)
        if age is None or age > common.EPHEMERAL_DAYS:
            return "stale"
    if m.type in ("rule", "decision", "lesson"):
        return m.type
    return "other"


def _line(m, group, errors, today):
    """One index line. Every piece is untrusted text, so the whole line goes through common.printable: a control
    character in a name, a tag or an error message can never start a new line (a forged tier-0 section)."""
    return common.printable(_raw_line(m, group, errors, today))


def _raw_line(m, group, errors, today):
    tags = ", ".join(m.tags)
    if m.rejected or not common.FILE_SLUG.match(m.slug):
        # never a link and never the raw name: its repr, truncated (common.shown)
        return "- %s - %s" % (m.slug if m.rejected else common.shown(m.slug), _clip("; ".join(errors), 200))
    head = "- [%s](../%s)" % (m.slug, m.rel)
    if group == "invalid":
        return "%s - %s" % (head, _clip("; ".join(errors), 200))
    marks = ""
    if group == "superseded":
        sup = m.get("superseded_by")
        if isinstance(sup, str) and sup:
            marks = " -> [[%s]]" % sup
    elif m.status == "ephemeral":
        age = m.age_days(today)
        marks = (" **[UNDATED ephemeral - re-verify]**" if age is None else
                 " **[STALE %dd - re-verify]**" % age if group == "stale" else
                 " [ephemeral %dd]" % age)
    return "%s `%s`%s - %s" % (head, tags, marks, _clip(m.description, DESC_LEN))


def _size(lines):
    return len(("\n".join(lines) + "\n").encode("utf-8"))


def _assemble(head, groups, cut_note):
    out = list(head)
    for key, title in GROUPS:
        lines = groups.get(key) or []
        if lines:
            out += ["## " + title, ""] + lines + [""]
    if cut_note:
        out += [cut_note, ""]
    return out


def render_index(results, today):
    if not results:
        return EMPTY_INDEX, 0
    groups = {}
    for m, errors, _ in sorted(results, key=lambda r: _sort_key(r[0])):
        g = _group_of(m, errors, today)
        groups.setdefault(g, []).append(_line(m, g, errors, today))
    n_lessons = sum(1 for m, e, _ in results if m.kind == "lesson")
    n_topics = sum(1 for m, e, _ in results if m.kind == "topic")
    head = ["# Memory index", "",
            "<!-- GENERATED by tools/memory_index.py - do not hand-edit; it is rebuilt every session. -->",
            "",
            "%d memories (%d topics, %d lessons). Search: `python3 tools/memory_tools.py search \"<words>\"`."
            % (n_topics + n_lessons, n_topics, n_lessons),
            ""]
    out = _assemble(head, groups, None)
    cut = 0
    if _size(out) > INDEX_CAP:
        # cut from the tail of the unprotected groups until it fits
        order = [k for k, _ in reversed(GROUPS) if k not in PROTECTED]
        while _size(out) > INDEX_CAP:
            victim = next((k for k in order if groups.get(k)), None)
            if victim is None:
                break      # only protected groups remain: they are kept whole
            groups[victim].pop()
            cut += 1
            note = ("- ... %d more memories not listed (index capped at %d bytes). "
                    "Use tools/memory_tools.py search." % (cut, INDEX_CAP))
            out = _assemble(head, groups, note)
    return "\n".join(out) + "\n", cut


def render_triggers(results):
    lessons = [m for m, errors, _ in results
               if m.kind == "lesson" and not errors and m.status == "active"
               and common.as_bool(m.get("tier0"), True)]
    lessons.sort(key=_sort_key)
    if not lessons:
        return EMPTY_TRIGGERS, 0, 0
    head = ["# Triggers", "",
            "<!-- GENERATED by tools/memory_index.py from memory/lessons/*.md - do not hand-edit. -->",
            "",
            "Questions that must fire. Before acting, ask the one that matches; the linked lesson says why.",
            ""]
    lines = [common.printable('- **"%s"** ([[%s]])' % (" ".join(m.get("trigger").split()), m.slug))
             for m in lessons]
    out = head + lines
    cut = 0
    while lines and _size(out + [""]) > TRIGGERS_CAP:
        lines.pop()                     # oldest last, so the oldest go first
        cut += 1
        out = head + lines + ["", "- ... %d older triggers not listed (capped at %d bytes). "
                              "See memory/lessons/." % (cut, TRIGGERS_CAP)]
    return "\n".join(out) + "\n", cut, len(lessons)


def render(mem_dir, today):
    """Pure: return {'index', 'triggers', 'results', 'index_cut', 'triggers_cut', 'n_triggers'}.
    memory_verify uses this to check that _gen/ is fresh without writing."""
    results = memory_lint.lint_dir(mem_dir, today)
    index, icut = render_index(results, today)
    triggers, tcut, ntrig = render_triggers(results)
    return {"index": index, "triggers": triggers, "results": results,
            "index_cut": icut, "triggers_cut": tcut, "n_triggers": ntrig}


def lessons_rows(results):
    """--print lessons-json rows: every lesson file, valid or not, newest created first."""
    rows = []
    for m, errors, _ in sorted(results, key=lambda r: _sort_key(r[0])):
        if m.kind != "lesson":
            continue
        trig, created = m.get("trigger"), m.get("created")
        rows.append({"name": m.name, "description": m.description,
                     "trigger": trig if isinstance(trig, str) else " ".join(common.as_list(trig)),
                     "created": created if isinstance(created, str) else "",
                     "status": m.status, "tier0": common.as_bool(m.get("tier0"), True),
                     "tags": m.tags, "errors": [str(e) for e in errors], "file": m.rel})
    return rows


def print_mode(a, which):
    """--print index|triggers|lessons-json: emit exactly the bytes the writer would write (or the
    lessons as JSON), write nothing, exit 0 always. The tier-0 loader calls this on every part
    (SessionStart hooks run in parallel, so it cannot wait for the writer), so it must never raise."""
    try:
        common.check_inside(a.root, "--root")             # raises OutsideRepo -> the placeholder below
        common.check_inside(a.memory_dir, "--memory-dir")
        mem_dir = common.memory_dir(a.memory_dir, a.root)
        today = common.today_arg(a.today)
        if which == "lessons-json":
            text = json.dumps({"lessons": lessons_rows(memory_lint.lint_dir(mem_dir, today))},
                              ensure_ascii=False, indent=1) + "\n"
        else:
            r = render(mem_dir, today)
            text = r["index"] if which == "index" else r["triggers"]
    except BaseException as e:  # noqa: B902 - a crash here would blank tier 0
        text = ('{"lessons": [], "error": "%s"}\n' if which == "lessons-json" else
                "(index unavailable: %s)\n") % type(e).__name__
    try:
        out = getattr(sys.stdout, "buffer", None)
        if out is not None:      # raw bytes: no newline translation, no console codec
            sys.stdout.flush()
            out.write(text.encode("utf-8"))
            out.flush()
        else:
            sys.stdout.write(text)
    except Exception:
        pass
    return 0


def main(argv=None):
    common.utf8_stdout()
    ap = common.ArgParser(description="Regenerate memory/_gen/INDEX.md and TRIGGERS.md.")
    ap.add_argument("--root", help="repo root (default: the repo this tool is in)")
    ap.add_argument("--memory-dir", help="memory dir (default: env AGENT_MEMORY_DIR, else <root>/memory)")
    ap.add_argument("--today", help="YYYY-MM-DD, for reproducible runs")
    ap.add_argument("--force", action="store_true", help="write even when no memory was found")
    ap.add_argument("--check", action="store_true", help="compare _gen/ with a fresh render; write nothing")
    ap.add_argument("--print", dest="print_", choices=("index", "triggers", "lessons-json"),
                    help="print what INDEX.md or TRIGGERS.md would hold, or every lesson as JSON; "
                         "write nothing; always exit 0")
    ap.add_argument("-q", "--quiet", action="store_true", help="no stdout on success; errors still go to stderr")
    a = ap.parse_args(argv)
    if a.print_:
        return print_mode(a, a.print_)

    def say(msg):
        if not a.quiet:
            print(msg)

    def err(msg):
        sys.stderr.write(msg + "\n")

    today = common.today_or_die(a.today)
    common.inside_or_die(a.root, "--root")
    common.inside_or_die(a.memory_dir, "--memory-dir")
    mem_dir = common.memory_dir(a.memory_dir, a.root)
    gen = os.path.join(mem_dir, common.GEN_DIR)
    index_path = os.path.join(gen, "INDEX.md")
    triggers_path = os.path.join(gen, "TRIGGERS.md")
    if not os.path.isdir(mem_dir):
        common.die("memory_index: REFUSED - memory dir not found: %s" % common.printable(mem_dir), common.REFUSED)

    r = render(mem_dir, today)
    results = r["results"]

    if a.check:
        stale = []
        for path, want in ((index_path, r["index"]), (triggers_path, r["triggers"])):
            have = common.read_text(path) if os.path.isfile(path) else None
            if have != want:
                stale.append(os.path.basename(path) + (" missing" if have is None else " differs"))
        if stale:
            err("memory_index --check: STALE (" + ", ".join(stale) + ")")
            return 1
        say("memory_index --check: fresh")
        return 0

    if not results and not a.force:
        # only a REAL earlier index is worth protecting; placeholder over placeholder loses nothing,
        # and refusing it would make every SessionStart on a new, empty repo exit 2
        populated = [os.path.basename(p) for p, empty in ((index_path, EMPTY_INDEX), (triggers_path, EMPTY_TRIGGERS))
                     if os.path.isfile(p) and common.read_text(p) != empty]
        if populated:
            common.die("memory_index: REFUSED - found 0 topics/lessons in %s but _gen/%s lists memories; not "
                       "overwriting. Check the path, or pass --force if the memories really are gone."
                       % (mem_dir, " and ".join(populated)), common.REFUSED)

    common.atomic_write(index_path, r["index"])
    common.atomic_write(triggers_path, r["triggers"])
    invalid = sum(1 for _, e, _ in results if e)
    note = ""
    if r["index_cut"]:
        note += "  index capped (%d cut)" % r["index_cut"]
    if r["triggers_cut"]:
        note += "  triggers capped (%d cut)" % r["triggers_cut"]
    if invalid:
        note += "  !! %d INVALID - run tools/memory_lint.py" % invalid
    line = ("memory_index: %d memories, %d triggers, %d bytes index, %d bytes triggers%s"
            % (len(results), r["n_triggers"], len(r["index"].encode("utf-8")),
               len(r["triggers"].encode("utf-8")), note))
    if invalid:
        err(line)
        return 1
    say(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
