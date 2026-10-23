#!/usr/bin/env python3
"""agent_gen.py - build .claude/agents/<role>.md from agents-src/<role>.src.md.

    python3 tools/agent_gen.py reviewer          build one role
    python3 tools/agent_gen.py --all             build every agents-src/*.src.md
    python3 tools/agent_gen.py --check [role]    build in memory, report drift against the committed file

Placeholders in a source:
  {{INCLUDE agents-src/_role-common.md}}   shared role text, one source for every role
  {{LESSONS tag:review}}                   every ACTIVE, VALID lesson in memory/lessons tagged `review`, newest first
  {{LESSONS slug-a,slug-b}}                exactly these lessons; refuses to build if one is missing, invalid or not
                                           active

Lessons are read through memory_lint.lint_dir - the same parser and rules as the index and TRIGGERS.md - from the
memory dir (env AGENT_MEMORY_DIR, else <root>/memory), keyed by FILE slug (never by `name:`, so zzz.md claiming
`name: check-exit-code` cannot replace that lesson). Only error-free, active lessons render. A lesson carrying the
role's tag that lints with errors REFUSES the role (exit 2, naming the file) rather than silently dropping out; so does
any lesson file the tools refuse to read (a symlink, an unsafe name), because its tags are unknown. A missing status
is an error, not "active".

A lesson renders as `- **<slug>** <description> *Ask:* <trigger>`, description and trigger flattened to one line with
control characters escaped (a trigger must never become a heading in the agent file). A tag with no lessons yet
renders one line saying so - a new team starts with none, and the role picks them up as /wrap records them. Generated files are committed (the
harness registers agent types at session start); re-run --all after a lesson changes, and memory_verify flags drift.
Writes atomically. Refuses an output under 500 bytes or with an unexpanded {{...}} left.

Every role is processed even when one is refused. Exit 0 ok, 1 drift (--check), 2 any role refused, 64 usage.
"""
import datetime
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common       # noqa: E402
import memory_lint  # noqa: E402
from common import atomic_write, read_text, repo_root  # noqa: E402

ROOT = repo_root()
SRC = os.path.join(ROOT, "agents-src")
OUT = os.path.join(ROOT, ".claude", "agents")


class Refuse(Exception):
    pass


def lessons():
    """slug -> (Memory, errors) for every memory/lessons/*.md, linted with the shared rules."""
    mem_dir = common.memory_dir(None, ROOT)
    if not os.path.isdir(mem_dir):
        return {}
    return dict((m.slug, (m, errors)) for m, errors, _ in memory_lint.lint_dir(mem_dir, datetime.date.today())
                if m.kind == "lesson")


def usable(entry):
    m, errors = entry
    return not errors and m.status == "active"


def flat(text):
    """One line, control characters escaped: a trigger holding "\\n## Heading" must not become a heading in the
    generated agent file."""
    return common.printable(" ".join(str(text).split()))


def render(m):
    row = "- **%s** %s" % (m.slug, flat(m.description))
    trig = m.get("trigger")
    if isinstance(trig, str) and trig:
        row += " *Ask:* %s" % flat(trig)
    return row


def expand_lessons(spec, L, count):
    spec = spec.strip()
    if spec.startswith("tag:"):
        tag = spec[4:].strip()
        # an invalid lesson must not silently drop out of a role: refuse, naming the file. A rejected file (symlink,
        # unsafe name) was never read, so its tags are unknown - it blocks every tag.
        broken = sorted(m.rel for s, (m, e) in L.items() if e and (m.rejected or tag in m.tags))
        if broken:
            raise Refuse("lesson(s) for tag `%s` are invalid - fix them (python3 tools/memory_lint.py): %s"
                         % (tag, ", ".join("memory/" + r for r in broken)))
        hits = sorted(((m.get("created"), s) for s, (m, e) in L.items() if usable((m, e)) and tag in m.tags),
                      reverse=True)
        if not hits:
            return "- (no team lessons tagged `%s` yet - they appear here when /wrap records one)" % tag
        count[0] += len(hits)
        return "\n".join(render(L[s][0]) for _, s in hits)
    slugs = [s.strip() for s in spec.split(",") if s.strip()]
    bad = []
    for s in slugs:
        if s not in L:
            bad.append("%s (missing)" % s)
        elif L[s][1]:
            bad.append("%s (invalid, memory/%s: %s)" % (s, L[s][0].rel, common.printable("; ".join(L[s][1]))[:120]))
        elif L[s][0].status != "active":
            bad.append("%s (status %s)" % (s, L[s][0].status or "?"))
    if bad:
        raise Refuse("lesson(s) not usable from memory/lessons: %s" % ", ".join(bad))
    count[0] += len(slugs)
    return "\n".join(render(L[s][0]) for s in slugs)


def build(role, L):
    """Return (text, number of lessons rendered)."""
    path = os.path.join(SRC, role + ".src.md")
    if not os.path.isfile(path):
        raise Refuse("no source: agents-src/%s.src.md" % role)
    src = read_text(path).replace("\r\n", "\n")

    def include(m):
        p = os.path.join(ROOT, m.group(1).strip())
        if not os.path.isfile(p):
            raise Refuse("include not found: %s" % m.group(1).strip())
        return read_text(p).strip()

    count = [0]
    src = re.sub(r"\{\{INCLUDE ([^}]+)\}\}", include, src)
    out = re.sub(r"\{\{LESSONS ([^}]+)\}\}", lambda m: expand_lessons(m.group(1), L, count), src)
    if "{{" in out:
        i = out.index("{{")
        raise Refuse("unexpanded placeholder left: %s" % out[i:i + 60])
    size = len(out.encode("utf-8"))
    if size < 500:
        raise Refuse("output suspiciously small (%d bytes)" % size)
    return out, count[0]


def roles():
    if not os.path.isdir(SRC):
        return []
    return sorted(n[:-7] for n in os.listdir(SRC) if n.endswith(".src.md"))


def main(argv=None):
    common.utf8_stdout()
    ap = common.ArgParser(prog="agent_gen.py", description="Build .claude/agents/<role>.md from agents-src/.")
    ap.add_argument("role", nargs="*", help="role(s) to build or check")
    ap.add_argument("--all", action="store_true", help="every agents-src/*.src.md")
    ap.add_argument("--check", action="store_true", help="write nothing; exit 1 when a generated file drifted")
    a = ap.parse_args(argv)
    targets = a.role or (roles() if (a.all or a.check) else [])
    if not targets:
        ap.error("give a role, --all or --check" if not (a.all or a.check) else "no agents-src/*.src.md found")
    L = lessons()
    drift = refused = 0
    for role in targets:
        try:
            out, n = build(role, L)
        except Refuse as e:
            sys.stderr.write("REFUSING %s: %s\n" % (role, e))
            refused += 1
            continue
        dst = os.path.join(OUT, role + ".md")
        if a.check:
            cur = read_text(dst).replace("\r\n", "\n") if os.path.isfile(dst) else ""
            ok = cur == out
            drift += not ok
            print("%-12s %s" % (role, "in sync" if ok else "DRIFT - run: python3 tools/agent_gen.py " + role))
            continue
        atomic_write(dst, out)
        print("wrote .claude/agents/%s.md (%d bytes, %d lessons)" % (role, len(out.encode("utf-8")), n))
    return common.REFUSED if refused else (common.FINDINGS if drift else common.OK)


if __name__ == "__main__":
    sys.exit(main())
