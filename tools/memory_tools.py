#!/usr/bin/env python3
"""memory_tools.py - retrieval and hygiene over the shared memory.

  search <query>   ranked BM25 over memory/** (topics, lessons, logs), agent/**
                   and .claude/skills/**/SKILL.md; superseded results are labelled
  dupes            near-duplicate memory pairs (TF-IDF cosine)
  stale            ephemerals past their shelf life, and old memories that assert
                   measured state
  tier0            tier-0 budget: runs .claude/hooks/load-persona.sh check N, with N
                   from settings.json (common.tier0_parts); advisory, always exit 0

Markdown stays the source of truth: nothing is cached, every run re-reads the
files (well under a second at a few hundred memories). stdlib only.

Exit 0 for every subcommand; 2 when search refuses a query with no searchable
words; 64 on a usage error (no subcommand, bad flag, bad --today). Results go to
stdout, errors and advisories to stderr.
"""

import math
import os
import re
import shutil
import subprocess
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

DEMOTE = 0.3             # superseded / ephemeral score factor (unless asked for by name)
LOADER = os.path.join(".claude", "hooks", "load-persona.sh")

STOP = set("""a an the and or but if then than that this these those is are was were be been being
of in on at to for from by with as it its not no so such can could may might will would should
do does did done have has had i me my we our you your he she they them their there here what which
who whom when where why how all any both each few more most other some only own same very just
now one two also into out up down over under again further once""".split())
WORD = re.compile(r"[A-Za-z0-9_]+")
NOT_DUP = "Not a duplicate of [[%s]]"


def stem(w):
    """Cheap suffix stripping so 'timeouts' ~ 'timeout' and 'deploying' ~ 'deploy'."""
    for suf in ("ability", "ables", "able", "ingly", "edly", "ing", "ies", "ed", "es", "ly", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[:-len(suf)]
    return w


def tokens(text):
    return [stem(w.lower()) for w in WORD.findall(text or "")
            if w.lower() not in STOP and len(w) > 1]


class Doc(object):
    __slots__ = ("path", "rel", "kind", "name", "desc", "status", "body", "toks", "tf", "mem")

    def __init__(self, path, rel, kind, name=None, mem=None):
        self.path, self.rel, self.kind, self.mem = path, rel, kind, mem
        if mem is not None:
            meta, body = mem.meta, mem.body
        else:
            meta, body, _ = common.parse_frontmatter(common.read_text(path))
        self.body = body
        self.name = name or (meta.get("name") if isinstance(meta.get("name"), str) and meta.get("name")
                             else os.path.basename(path)[:-3])
        d = meta.get("description", "")
        self.desc = d if isinstance(d, str) else " ".join(common.as_list(d))
        st = common.field(meta, "status")
        self.status = st if isinstance(st, str) else ""
        trig = common.field(meta, "trigger")
        extra = trig if isinstance(trig, str) else ""
        # name, description and trigger carry the recall signal: weight them
        self.toks = (tokens(body) + tokens(self.name.replace("-", " ")) * 3
                     + tokens(self.desc) * 3 + tokens(extra) * 3)
        self.tf = Counter(self.toks)


def load_docs(root, mem_dir, memories_only=False):
    """Every searchable file. Symlinks and names with a control character are never opened (common.iter_memories,
    iter_logs, safe_walk_md): a link could point at a secret outside the repo and search prints body lines."""
    docs = []
    for m in common.iter_memories(mem_dir):
        docs.append(Doc(m.path, "memory/" + m.rel, m.kind, mem=m))
    if memories_only:
        return docs
    for p in common.iter_logs(mem_dir):
        docs.append(Doc(p, os.path.relpath(p, os.path.dirname(mem_dir)).replace(os.sep, "/"), "log",
                        name=os.path.basename(p)[:-3]))
    for p in common.safe_walk_md(os.path.join(root, "agent")):
        docs.append(Doc(p, os.path.relpath(p, root).replace(os.sep, "/"), "agent",
                        name="agent/" + os.path.basename(p)))
    for p in common.safe_walk_md(os.path.join(root, ".claude", "skills"), want="SKILL.md"):
        docs.append(Doc(p, os.path.relpath(p, root).replace(os.sep, "/"), "skill",
                        name="skill " + os.path.basename(os.path.dirname(p))))
    return docs


def bm25(docs, query_toks, k1=1.5, b=0.75):
    n = len(docs)
    df = Counter()
    for d in docs:
        df.update(set(d.tf))
    avgdl = (sum(len(d.toks) for d in docs) / float(n)) or 1.0
    qset = set(query_toks)
    scored = []
    for d in docs:
        dl = len(d.toks) or 1
        s = 0.0
        for t in qset:
            f = d.tf.get(t, 0)
            if f:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avgdl))
        if s > 0:
            named = set(tokens(d.name.replace("-", " "))) <= qset
            if d.status in ("superseded", "ephemeral") and not named:
                s *= DEMOTE
            scored.append((s, d))
    scored.sort(key=lambda x: (-x[0], x[1].rel))
    return scored


TAGS = {"topic": "MEM", "lesson": "LES", "log": "LOG", "agent": "AGT", "skill": "SKL"}


def cmd_search(a, root, mem_dir):
    q = tokens(a.query)
    if not q:
        common.die("search: REFUSED - query has no searchable words: %r" % a.query, common.REFUSED)
    docs = load_docs(root, mem_dir, a.memories_only)
    if not docs:
        sys.stderr.write("search: no documents found under %s\n" % root)
        return 0
    scored = bm25(docs, q)
    if not scored:
        print("no matches for: %s" % a.query)
        return 0
    qset = set(q)
    for s, d in scored[:a.limit]:
        label = ""
        if d.status == "superseded":
            sup = d.mem.get("superseded_by") if d.mem else ""
            label = "  [SUPERSEDED - do not act on it%s]" % (", see " + sup if sup else "")
        elif d.status == "ephemeral":
            label = "  [EPHEMERAL]"
        # every printed field is file content or a file name: escape control characters
        out = common.printable
        print("")
        print(out("%s  %-40s  score %.2f%s" % (TAGS.get(d.kind, "DOC"), d.name, s, label)))
        if d.desc:
            print(out("     %s" % d.desc[:150]))
        best, bestn = "", 0
        for line in d.body.split("\n"):
            hits = len(qset & set(tokens(line)))
            if hits > bestn:
                best, bestn = line.strip(), hits
        if best:
            print(out("     > %s" % best[:160]))
        print(out("     %s" % d.rel))
    print("")
    print("%d match(es), showing %d" % (len(scored), min(len(scored), a.limit)))
    return 0


def cosine(a, b):
    common_t = set(a) & set(b)
    if not common_t:
        return 0.0
    num = sum(a[t] * b[t] for t in common_t)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return num / (na * nb) if na and nb else 0.0


def dupe_pairs(docs, threshold):
    n = len(docs)
    df = Counter()
    for d in docs:
        df.update(set(d.tf))
    vecs = [dict((t, (1 + math.log(f)) * math.log((n + 1) / float(df[t])))
                 for t, f in d.tf.items()) for d in docs]
    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            # a superseded memory restates its replacement on purpose
            if "superseded" in (docs[i].status, docs[j].status):
                continue
            # a pair reviewed and found distinct says so in a body, once, forever
            if (NOT_DUP % docs[j].name) in docs[i].body or (NOT_DUP % docs[i].name) in docs[j].body:
                continue
            c = cosine(vecs[i], vecs[j])
            if c >= threshold:
                pairs.append((c, docs[i], docs[j]))
    pairs.sort(key=lambda x: (-x[0], x[1].name, x[2].name))
    return pairs


def cmd_dupes(a, root, mem_dir):
    docs = load_docs(root, mem_dir, memories_only=True)
    pairs = dupe_pairs(docs, a.threshold)
    if not pairs:
        print("no memory pairs at or above similarity %.2f (%d memories compared)" % (a.threshold, len(docs)))
        return 0
    print("near-duplicate candidates (similarity >= %.2f):" % a.threshold)
    print("")
    for c, x, y in pairs[:a.limit]:
        print(common.printable("  %.3f  %s" % (c, x.name)))
        print(common.printable("         %s" % y.name))
    print("")
    print("%d pair(s). Review before merging: overlap can be two different facts about one subsystem." % len(pairs))
    print("If a pair is distinct, write '%s' in one body and it is skipped from then on." % (NOT_DUP % "<other>"))
    return 0


MEASURED = re.compile(r"\b(verified|measured|currently|confirmed|as of|reads)\b", re.I)


def stale_report(mems, today, days, ephemeral_days):
    ephemeral, aging = [], []
    for m in mems:
        age = m.age_days(today)
        if m.status == "ephemeral":
            if age is None or age > ephemeral_days:
                ephemeral.append((age, m))
        elif m.status != "superseded" and age is not None and age > days and MEASURED.search(m.body):
            aging.append((age, m))
    key = lambda x: (-(x[0] if x[0] is not None else 10 ** 6), x[1].slug)  # noqa: E731
    return sorted(ephemeral, key=key), sorted(aging, key=key)


def cmd_stale(a, root, mem_dir):
    ephemeral, aging = stale_report(common.iter_memories(mem_dir), common.today_or_die(a.today),
                                    a.days, a.ephemeral_days)
    if ephemeral:
        print("EPHEMERAL past its %d-day shelf life (%d) - re-verify, then set verified: or supersede:"
              % (a.ephemeral_days, len(ephemeral)))
        for age, m in ephemeral:
            print("  %5s  %s" % ("undated" if age is None else "%dd" % age, m.slug))
        print("")
    if aging:
        print("Asserts measured state, not verified for %dd+ (%d) - re-verify before acting on:"
              % (a.days, len(aging)))
        for age, m in aging[:a.limit]:
            print(common.printable("  %4dd  %-40s %s" % (age, m.slug, m.description[:64])))
    if not ephemeral and not aging:
        print("nothing stale (no ephemeral past %dd; nothing asserting measured state older than %dd)"
              % (a.ephemeral_days, a.days))
    return 0


def cmd_tier0(a, root, mem_dir):
    # advisory: every path exits 0 (memory_verify check 3 is the gate); problems go to stderr
    loader = os.path.join(root, LOADER)
    if not os.path.isfile(loader):
        sys.stderr.write("tier0: loader not found (%s)\n" % LOADER.replace(os.sep, "/"))
        return 0
    bash = shutil.which("bash")
    if not bash:
        sys.stderr.write("tier0: bash not found on PATH\n")
        return 0
    parts = common.tier0_parts(root)
    try:
        # relative POSIX path + cwd: an absolute Windows path loses its backslashes in a WSL bash
        r = subprocess.run([bash, LOADER.replace(os.sep, "/"), "check", str(parts)], cwd=root, capture_output=True,
                           timeout=60, stdin=subprocess.DEVNULL,
                           env=dict(os.environ, CLAUDE_PROJECT_DIR=root))
    except Exception as e:
        sys.stderr.write("tier0: could not run the loader: %s\n" % e)
        return 0
    sys.stdout.write(r.stdout.decode("utf-8", "replace"))
    err = r.stderr.decode("utf-8", "replace").strip()
    if err:
        sys.stderr.write(err + "\n")
    print("tier0: loader check %d parts: %s (exit %d)"
          % (parts, "FITS" if r.returncode == 0 else "DOES NOT FIT", r.returncode))
    return 0


def main(argv=None):
    common.utf8_stdout()
    ap = common.ArgParser(description="Search and hygiene over the shared memory.")
    ap.add_argument("--root", help="repo root (default: the repo this tool is in)")
    ap.add_argument("--memory-dir", help="memory dir (default: env AGENT_MEMORY_DIR, else <root>/memory)")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="search|dupes|stale|tier0")

    s = sub.add_parser("search", help="ranked BM25 search")
    s.add_argument("query", nargs="+")
    s.add_argument("--limit", type=int, default=8)
    s.add_argument("--memories-only", action="store_true", help="topics + lessons only")
    s.set_defaults(func=cmd_search)

    d = sub.add_parser("dupes", help="near-duplicate pairs")
    d.add_argument("--threshold", type=float, default=0.40)
    d.add_argument("--limit", type=int, default=15)
    d.set_defaults(func=cmd_dupes)

    st = sub.add_parser("stale", help="memories to re-verify")
    st.add_argument("--days", type=int, default=45)
    st.add_argument("--ephemeral-days", type=int, default=common.EPHEMERAL_DAYS)
    st.add_argument("--limit", type=int, default=12)
    st.add_argument("--today", help="YYYY-MM-DD, for reproducible runs")
    st.set_defaults(func=cmd_stale)

    t = sub.add_parser("tier0", help="tier-0 budget via the persona loader")
    t.set_defaults(func=cmd_tier0)

    a = ap.parse_args(argv)
    if a.cmd == "search":
        a.query = " ".join(a.query)
    common.inside_or_die(a.root, "--root")
    common.inside_or_die(a.memory_dir, "--memory-dir")
    root = os.path.abspath(a.root) if a.root else common.repo_root()
    return a.func(a, root, common.memory_dir(a.memory_dir, root))


if __name__ == "__main__":
    sys.exit(main())
