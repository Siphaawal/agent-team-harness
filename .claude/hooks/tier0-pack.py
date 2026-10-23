#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tier0-pack.py - split the tier-0 payload across several SessionStart hooks.

    load-persona.sh | python3 tier0-pack.py PART TOTAL   emit one part (what each hook runs)
    load-persona.sh | python3 tier0-pack.py check TOTAL  size table; exit 1 if it does not fit

Why: Claude Code injects a hook's output inline only up to 10,000 characters; anything larger is saved to a file and
only a 2,000-character preview reaches the model (hooks docs). The cap is PER HOOK, so tier 0 is packed into parts of
at most PART_CAP bytes (bytes >= characters, so a byte cap is the safe side) and settings.json runs one hook per part.

Hooks run IN PARALLEL and land in any order, so every part is self-labelled, and part 1 carries a
manifest of what every other part holds - a missing part is then something the agent can notice.
Packing is deterministic: every hook process computes the same split from the same files.
"""
import re
import sys

PART_CAP = 9000        # bytes per rendered part; Claude Code persists anything over ~10,000
CHUNK = 7600           # a single section larger than this is split at paragraph boundaries
RESERVE_FIRST = 1500   # part 1 also carries the header, manifest and status lines
RESERVE_OTHER = 250

MARK = re.compile(r"^===== (.+?) =====\s*$", re.M)


def blocks_of(text):
    """Sections in payload order: [(title, body_including_marker_line)]."""
    starts = [m.start() for m in MARK.finditer(text)]
    out = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(text)
        chunk = text[s:e].rstrip("\n") + "\n"
        title = MARK.match(chunk).group(1)
        out.append((title, chunk))
    return out


def nbytes(s):
    return len(s.encode("utf-8"))


def hard_split(line):
    """A line longer than CHUNK bytes in CHUNK-byte pieces, cut on a UTF-8 character boundary. Without this, one
    long line (a pasted table, a minified blob) made a part bigger than the harness cap, silently."""
    if nbytes(line) <= CHUNK:
        return [line]
    out, raw = [], line.encode("utf-8")
    while raw:
        cut = min(CHUNK, len(raw))
        while cut < len(raw) and (raw[cut] & 0xC0) == 0x80:   # never split inside a multi-byte character
            cut -= 1
        out.append(raw[:cut].decode("utf-8") + ("" if cut == len(raw) else "\n"))
        raw = raw[cut:]
    return out


def split_big(title, body):
    """Split an oversized section at blank lines (then at lines) into continuation blocks."""
    if nbytes(body) <= CHUNK:
        return [(title, body)]
    head, rest = body.split("\n", 1)
    units = re.split(r"(?<=\n)\n", rest)          # paragraphs, keeping their trailing newline
    pieces, cur = [], ""
    for u in units:
        if nbytes(u) > CHUNK:                       # one enormous paragraph: fall back to lines
            for line in u.splitlines(True):
                for seg in hard_split(line):        # ...and a single enormous line to CHUNK-byte pieces
                    if cur and nbytes(cur) + nbytes(seg) > CHUNK:
                        pieces.append(cur); cur = ""
                    cur += seg
            continue
        if cur and nbytes(cur) + nbytes(u) + 1 > CHUNK:
            pieces.append(cur); cur = ""
        cur += u + ("\n" if not u.endswith("\n\n") else "")
    if cur.strip():
        pieces.append(cur)
    n = len(pieces)
    return [("%s (%d/%d)" % (title, i + 1, n),
             "===== %s (%d/%d) =====\n%s" % (title, i + 1, n, p.lstrip("\n")))
            for i, p in enumerate(pieces)]


def pack_fit(text, total):
    """Pack, then grow part 1's reserve until part 1 - header and manifest included - fits PART_CAP. A fixed
    reserve under-counts a long manifest (many parts, long titles) and part 1 would cross the cap."""
    reserve = RESERVE_FIRST
    for _ in range(8):
        parts = pack(text, reserve)
        over = nbytes(render(parts, 1, total)) - PART_CAP
        if over <= 0:
            return parts
        reserve += over + 64
    return parts                                    # still too big: render() puts the OVER CAP banner in part 1


def pack(text, reserve_first=RESERVE_FIRST):
    blocks = []
    for t, b in blocks_of(text):
        blocks.extend(split_big(t, b))
    parts, cur, size = [], [], 0
    for t, b in blocks:
        cap = PART_CAP - (reserve_first if not parts else RESERVE_OTHER)
        if cur and size + nbytes(b) > cap:
            parts.append(cur); cur, size = [], 0
        cur.append((t, b)); size += nbytes(b)
    if cur:
        parts.append(cur)
    return parts


def render(parts, k, total):
    """Rendered text of part k (1-based) when settings.json runs `total` hooks."""
    needed = len(parts)
    lines = ["# AGENT TIER 0 - part %d/%d (tier 0, loaded at session start)" % (k, total), ""]
    if k == 1:
        lines.append("Tier 0 is split across %d SessionStart hooks: Claude Code persists any single hook"
                     " output over ~10,000 bytes and injects only a 2 KB preview. Parts arrive in any order."
                     % total)
        lines.append("")
        lines.append("Manifest (every part below must be in context):")
        for i, p in enumerate(parts[:total]):
            lines.append("  part %d: %s" % (i + 1, " | ".join(t for t, _ in p)))
        if needed > total:
            lost = [t for p in parts[total:] for t, _ in p]
            lines += ["", "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
                      "!! OVERFLOW: tier 0 needs %d parts but settings.json runs %d hooks." % (needed, total),
                      "!! NOT LOADED: %s" % ", ".join(lost),
                      "!! Say so out loud; add a hook entry or trim tier 0 before any work.",
                      "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"]
        big = [j for j in range(2, min(total, needed) + 1) if nbytes(render(parts, j, total)) > PART_CAP]
        if big:
            lines += ["", "!! OVER CAP: part(s) %s exceed %d bytes - the harness will show only a preview of them."
                      % (", ".join(map(str, big)), PART_CAP),
                      "!! Say so out loud and Read the files those parts list before any work."]
        lines += ["",
                  # never write the literal persisted-output tag here: detectors grep for it
                  "If any listed part is missing from context, or arrived only as a 2 KB preview of a",
                  "saved file, identity did NOT fully load: say so out loud and Read that file before any work.",
                  ""]
    if k > needed:
        lines.append("(empty - tier 0 currently fits in %d parts)" % needed)
        return "\n".join(lines) + "\n"
    body = "\n".join(b for _, b in parts[k - 1])
    return "\n".join(lines) + "\n" + body


def main():
    usage = "usage: tier0-pack.py PART|check TOTAL < payload   (PART and TOTAL are integers >= 1)\n"
    if len(sys.argv) != 3 or not sys.argv[2].isdigit() or not (sys.argv[1] == "check" or sys.argv[1].isdigit()):
        sys.stderr.write(usage)
        return 64
    total = int(sys.argv[2])
    if sys.argv[1] != "check" and not 1 <= int(sys.argv[1]):
        sys.stderr.write(usage)
        return 64
    text = sys.stdin.buffer.read().decode("utf-8", "replace").replace("\r\n", "\n")
    parts = pack_fit(text, total)

    if sys.argv[1] == "check":
        ok = len(parts) <= total
        print("tier 0: %d bytes of sections -> %d parts needed, %d hooks configured, cap %d bytes/part"
              % (nbytes(text), len(parts), total, PART_CAP))
        for k in range(1, max(total, len(parts)) + 1):
            r = render(parts, k, total)
            n = nbytes(r)
            flag = "OK" if n <= PART_CAP and k <= total else ("NOT LOADED" if k > total else "OVER CAP")
            ok = ok and flag == "OK"
            titles = " | ".join(t for t, _ in parts[k - 1]) if k <= len(parts) else "(empty)"
            print("  part %d: %5d bytes  %-10s %s" % (k, n, flag, titles))
        return 0 if ok else 1

    k = int(sys.argv[1])
    sys.stdout.buffer.write(render(parts, k, total).encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
