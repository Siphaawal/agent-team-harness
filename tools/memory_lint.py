#!/usr/bin/env python3
"""memory_lint.py - validate every memory's frontmatter against docs/SPEC.md.

Never writes. Checks memory/topics/*.md and memory/lessons/*.md; daily logs
(memory/log/**) are free-form and not linted, except that a symlink or a name
with a control character anywhere under memory/ is an ERROR (never opened).

  ERROR    the memory breaks the schema; tools may misplace or drop it
           (also: a symlink, or a file name that is not [a-z0-9][a-z0-9-]{0,63}.md)
  WARNING  suspicious but usable (unknown key, dangling link, future date, and
           SUSPECT text: a description or trigger shaped like a command or an
           instruction to the agent - a URL, curl/wget, a pipe into a shell,
           backticks, $(, "ignore previous", "system prompt", "instructions",
           "disregard". Tier 0 carries those fields into every session, so a
           reviewer must look; they stay warnings because real memories cite URLs.)

Every printed path and message has its control characters escaped.

Exit 0 when there are no errors (warnings allowed), 1 when any error exists,
2 when the memory dir does not exist, 64 on a usage error. Findings go to
stdout; a tool error goes to stderr.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

LINK = re.compile(r"\[\[([^\]\n]+)\]\]")
MAX_DESC = 300
SUSPECT = re.compile(r"(?i)https?://|\bcurl\b|\bwget\b|\|\s*(?:sh|bash)\b|`|\$\(|\bignore (?:all|previous|prior)\b"
                     r"|\bsystem (?:prompt|note)\b|\binstructions?\b|\bdisregard\b")
SUSPECT_MARK = "SUSPECT"     # memory_verify counts warnings that carry this word


def suspect_warnings(field_name, value):
    m = SUSPECT.search(value) if isinstance(value, str) else None
    if not m:
        return []
    return ["%s %s looks like a command or an instruction to the agent (%r) - a reviewer must check it"
            % (SUSPECT_MARK, field_name, m.group(0))]


def lint_memory(m, slugs, today):
    """Return (errors, warnings) for one Memory. slugs = every memory slug in the set."""
    errors = list(m.parse_errors)
    warnings = []
    if not m.meta:
        return errors, warnings
    meta = m.meta
    md = meta.get("metadata")

    # ---- shape ----
    for k in meta:
        if k in common.META_KEYS:
            errors.append("'%s' must sit under metadata:, not at the top level" % k)
        elif k not in common.TOP_KEYS:
            warnings.append("unknown top-level key '%s'" % k)
    if not isinstance(md, dict):
        errors.append("missing 'metadata:' map")
        md = {}
    for k in md:
        if k not in common.META_KEYS:
            warnings.append("unknown metadata key '%s'" % k)

    # ---- name ----
    name = meta.get("name", "")
    if not isinstance(name, str) or not name:
        errors.append("missing name")
    elif not common.SLUG.match(name):
        errors.append("name '%s' is not a kebab-case slug" % name)
    elif name != m.slug:
        errors.append("name '%s' does not equal the file name '%s'" % (name, m.slug))

    # ---- description ----
    desc = meta.get("description", "")
    if not isinstance(desc, str):
        errors.append("description must be a single string")
    elif not desc.strip():
        errors.append("missing description")
    else:
        if "\n" in desc:
            errors.append("description must be one line")
        if len(desc) > MAX_DESC:
            errors.append("description is %d chars (max %d)" % (len(desc), MAX_DESC))
        warnings += suspect_warnings("description", desc)

    # ---- type / location ----
    typ = md.get("type", "")
    if not typ:
        errors.append("missing metadata.type")
    elif typ not in common.TYPES:
        errors.append("type '%s' is not one of %s" % (typ, "|".join(common.TYPES)))
    if m.kind == "lesson" and typ and typ != "lesson":
        errors.append("files in lessons/ must be type: lesson (got '%s')" % typ)
    if m.kind == "topic" and typ == "lesson":
        errors.append("type: lesson belongs in lessons/, not topics/")

    # ---- tags ----
    tags = md.get("tags", "")
    if isinstance(tags, str):
        errors.append("tags must be a list with at least one tag" if not tags
                      else "tags must be a list, e.g. tags: [%s]" % tags)
    elif not tags:
        errors.append("tags must be a list with at least one tag")
    else:
        for t in tags:
            if not isinstance(t, str) or not common.SLUG.match(t):
                errors.append("tag %r is not lowercase kebab-case" % (t,))

    # ---- status / supersession ----
    status = md.get("status", "")
    sup = md.get("superseded_by", "")
    if not status:
        errors.append("missing metadata.status")
    elif status not in common.STATUSES:
        errors.append("status '%s' is not one of %s" % (status, "|".join(common.STATUSES)))
    if status == "superseded" and not sup:
        errors.append("status: superseded requires superseded_by: <slug>")
    if sup:
        if status != "superseded":
            errors.append("superseded_by is only allowed with status: superseded")
        if not isinstance(sup, str) or not common.SLUG.match(sup):
            errors.append("superseded_by %r is not a slug" % (sup,))
        elif sup == m.slug:
            errors.append("superseded_by points at itself")
        elif sup not in slugs:
            warnings.append("superseded_by '%s' matches no memory" % sup)

    # ---- author ----
    author = md.get("author", "")
    if not author:
        errors.append("missing metadata.author")
    elif not isinstance(author, str) or not common.SLUG.match(author) or len(author) > 32:
        errors.append("author %r is not a member slug ([a-z0-9-], <= 32)" % (author,))

    # ---- dates ----
    created = md.get("created", "")
    cd = common.parse_date(created)
    if not created:
        errors.append("missing metadata.created")
    elif cd is None:
        errors.append("created %r is not a valid YYYY-MM-DD date" % (created,))
    elif cd > today:
        warnings.append("created %s is in the future" % created)
    verified = md.get("verified", "")
    vd = common.parse_date(verified)
    if verified and vd is None:
        errors.append("verified %r is not a valid YYYY-MM-DD date" % (verified,))
    elif vd:
        if vd > today:
            warnings.append("verified %s is in the future" % verified)
        if cd and vd < cd:
            warnings.append("verified %s is before created %s" % (verified, created))

    # ---- lessons: trigger / tier0 ----
    trigger = md.get("trigger", "")
    if typ == "lesson":
        if not trigger:
            errors.append("type: lesson requires trigger: \"<question?>\"")
        elif not isinstance(trigger, str):
            errors.append("trigger must be a single string")
        elif "\n" in trigger:
            errors.append("trigger must be one line")
        elif not trigger.strip().endswith("?"):
            errors.append("trigger must end with '?' (it is a question the agent asks itself)")
        warnings += suspect_warnings("trigger", trigger)
    elif trigger:
        errors.append("trigger is only allowed on type: lesson")
    if "tier0" in md:
        if not isinstance(md["tier0"], str) or md["tier0"].strip().lower() not in ("true", "false"):
            errors.append("tier0 must be true or false")
        if typ and typ != "lesson":
            warnings.append("tier0 has no effect outside lessons")

    # ---- links ----
    for target in sorted(set(LINK.findall(m.body))):
        t = target.strip()
        if t not in slugs:
            warnings.append("link [[%s]] matches no memory" % t)

    return errors, warnings


def lint_dir(mem_dir, today):
    """Return [(Memory, errors, warnings)] for every memory under mem_dir, plus one REJECTED entry (kind topic,
    lesson or log; one error; never opened) for every symlink or unsafe name the tools refuse to read."""
    mems = common.iter_memories(mem_dir, rejected=True)
    log_rejects = []
    common.iter_logs(mem_dir, rejects=log_rejects)
    mems += [common.Memory(os.path.join(mem_dir, rel), rel, "log", reject="%s - never read" % why)
             for rel, why in log_rejects]
    slugs = set(m.slug for m in mems if not m.rejected)
    seen = {}
    out = []
    for m in mems:
        errors, warnings = lint_memory(m, slugs, today)
        if m.slug in seen:
            errors.append("slug '%s' also exists as %s; [[%s]] would be ambiguous"
                          % (m.slug, seen[m.slug], m.slug))
        else:
            seen[m.slug] = m.rel
        out.append((m, errors, warnings))
    return out


def main(argv=None):
    common.utf8_stdout()
    ap = common.ArgParser(description="Validate memory frontmatter (never writes).")
    ap.add_argument("--root", help="repo root (default: the repo this tool is in)")
    ap.add_argument("--memory-dir", help="memory dir (default: env AGENT_MEMORY_DIR, else <root>/memory)")
    ap.add_argument("--today", help="YYYY-MM-DD, for reproducible runs")
    ap.add_argument("-q", "--quiet", action="store_true", help="print errors only, no warnings")
    a = ap.parse_args(argv)
    today = common.today_or_die(a.today)
    common.inside_or_die(a.root, "--root")
    common.inside_or_die(a.memory_dir, "--memory-dir")
    mem_dir = common.memory_dir(a.memory_dir, a.root)
    if not os.path.isdir(mem_dir):
        common.die("lint: REFUSED - memory dir not found: %s" % common.printable(mem_dir), common.REFUSED)
    results = lint_dir(mem_dir, today)
    n_err = n_warn = 0
    for m, errors, warnings in results:
        for e in errors:
            print(common.printable("ERROR  %s: %s" % (m.rel, e)))
        if not a.quiet:
            for w in warnings:
                print(common.printable("WARN   %s: %s" % (m.rel, w)))
        n_err += len(errors)
        n_warn += len(warnings)
    bad = sum(1 for _, e, _ in results if e)
    print("lint: %d memories, %d invalid, %d errors, %d warnings" % (len(results), bad, n_err, n_warn))
    return 1 if n_err else 0


if __name__ == "__main__":
    sys.exit(main())
