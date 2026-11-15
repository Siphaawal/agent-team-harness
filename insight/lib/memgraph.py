#!/usr/bin/env python3
"""memgraph.py - the MEMORY GRAPH panel's data: the repo's memories as nodes and their relations as edges.

    python3 insight/lib/memgraph.py [--root REPO] [--max-nodes N]

Prints ONE JSON object on stdout and exits 0 (like `memory_index.py --print`: the dashboard must get an answer,
never a traceback); only a usage error (a bad flag, --max-nodes outside 1-2000) exits 64:

    {"nodes": [{id, kind, type, status, tags, description, created}],
     "edges": [{a, b, kind}],                 kind: link (a holds [[b]]) | supersedes (a is superseded by b)
                                              | tag (a and b share tags; each node keeps its 3 strongest)
     "total": N, "truncated": N, "dangling": N, "rejected": N,
     "obsidian": {"available": bool, "vault": "<folder name>"}}
    {"nodes": [], "edges": [], "error": "<ErrorClass>"}       on failure

Frontmatter is parsed by tools/common.py, the one parser every tool uses (a second parser drifts). Everything in a
memory is UNTRUSTED (it arrives through other members' pull requests), so:
  - a file whose name is not slug-clean, or a symlink, is never opened (common.iter_memories) - it is only counted;
  - a node id is the FILE slug (`[a-z0-9][a-z0-9-]{0,63}`), never the `name:` value;
  - an edge exists only between two node ids; a [[link]] to anything else is counted as dangling, never echoed;
  - text fields are control-character-escaped and length-capped, tags must be kebab-case, at most 12;
  - at most --max-nodes nodes (active first, newest first); the cut is reported in "truncated".
The page renders every string with textContent; this file only bounds what it can be handed.

--root must resolve inside the repo this file lives in (the tools rule), unless AGENT_TOOLS_ANY_ROOT=1. The
dashboard sets that for this child process only, because its --repo is the operator's own choice at launch.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(os.path.dirname(os.path.dirname(HERE)), "tools")
sys.path.insert(0, TOOLS)

import common  # noqa: E402  (tools/common.py: the one frontmatter parser)

DEFAULT_MAX_NODES = 400
MAX_TAGS = 12
TAG = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}\Z")   # \Z, not $: `$` also matches before a trailing newline
LINK = re.compile(r"\[\[([^\[\]\n|#]{1,80})(?:[#|][^\]\n]{0,200})?\]\]")
TAG_NEIGHBOURS = 3        # shared-tag edges kept per node (the strongest), so a popular tag cannot draw n^2 lines
TAG_GROUP_MAX = 80        # a tag on more memories than this says nothing about any pair of them


def _text(value, limit):
    s = value if isinstance(value, str) else " ".join(str(v) for v in common.as_list(value))
    s = common.printable(s.strip())
    return s if len(s) <= limit else s[:limit - 3] + "..."


def _node(m):
    created = m.created
    return {
        "id": m.slug,
        "kind": m.kind,
        "type": m.type if m.type in common.TYPES else "",
        "status": m.status if m.status in common.STATUSES else "",
        "tags": [t for t in m.tags if TAG.match(t)][:MAX_TAGS],
        "description": _text(m.description, 300),
        "created": created.isoformat() if created else None,
    }


def _vault(root):
    """Is there an Obsidian vault (tools/obsidian_vault.py) linked to THIS repo's memory? Never raises."""
    try:
        import obsidian_vault
        d = obsidian_vault.vault_dir()
        ok = obsidian_vault.links_to_repo(d, root)
        return {"available": bool(ok), "vault": os.path.basename(d) if ok else ""}
    except Exception:
        return {"available": False, "vault": ""}


def build(root, max_nodes=DEFAULT_MAX_NODES):
    mem_dir = os.path.join(root, "memory")
    if not os.path.isdir(mem_dir) or os.path.islink(mem_dir):
        raise FileNotFoundError("memory dir")
    every = common.iter_memories(mem_dir, rejected=True)
    good = [m for m in every if not m.rejected]
    rejected = len(every) - len(good)

    # active before ephemeral before superseded, then newest first, then by id: a stable cut
    rank = {"active": 0, "ephemeral": 1, "superseded": 2}
    good.sort(key=lambda m: m.slug)
    good.sort(key=lambda m: (m.created.isoformat() if m.created else ""), reverse=True)
    good.sort(key=lambda m: rank.get(m.status, 3))
    kept = good[:max_nodes]
    ids = {m.slug for m in kept}

    edges, seen, dangling = [], set(), 0

    def add(a, b, kind):
        key = (min(a, b), max(a, b), kind)
        if a != b and key not in seen:
            seen.add(key)
            edges.append({"a": a, "b": b, "kind": kind})

    for m in kept:
        for raw in LINK.findall(m.description + "\n" + m.body):
            target = raw.strip().lower()
            if target in ids:
                add(m.slug, target, "link")
            else:
                dangling += 1
        sup = m.get("superseded_by")
        if isinstance(sup, str) and sup.strip() in ids:
            add(m.slug, sup.strip(), "supersedes")

    groups = {}
    for m in kept:
        for t in set(t for t in m.tags if TAG.match(t)):
            groups.setdefault(t, []).append(m.slug)
    weight = {}
    for members in groups.values():
        if len(members) > TAG_GROUP_MAX:
            continue
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                k = (min(a, b), max(a, b))
                weight[k] = weight.get(k, 0) + 1
    best = {}
    for (a, b), w in weight.items():
        best.setdefault(a, []).append((w, b))
        best.setdefault(b, []).append((w, a))
    for a in sorted(best):
        for w, b in sorted(best[a], key=lambda x: (-x[0], x[1]))[:TAG_NEIGHBOURS]:
            add(a, b, "tag")

    return {
        "nodes": [_node(m) for m in kept],
        "edges": edges,
        "total": len(good),
        "truncated": len(good) - len(kept),
        "dangling": dangling,
        "rejected": rejected,
        "obsidian": _vault(root),
    }


def main(argv=None):
    common.utf8_stdout()
    ap = common.ArgParser(prog="memgraph.py", description="The memory graph as JSON, for the Insight dashboard. "
                          "Always exits 0; a failure is {\"error\": \"<Class>\"}.")
    ap.add_argument("--root", help="repo root (default: the repo this file is in)")
    ap.add_argument("--max-nodes", type=int, default=DEFAULT_MAX_NODES, help="node cap, 1-2000 (default %(default)s)")
    a = ap.parse_args(argv)
    if not 1 <= a.max_nodes <= 2000:
        ap.error("--max-nodes must be 1-2000")
    try:
        common.check_inside(a.root, "--root")
        out = build(os.path.abspath(a.root or common.repo_root()), a.max_nodes)
    except Exception as e:  # the placeholder, never a traceback: the page shows "graph unavailable: <Class>"
        out = {"nodes": [], "edges": [], "error": type(e).__name__}
    sys.stdout.write(json.dumps(out, ensure_ascii=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
