#!/usr/bin/env python3
"""skills-gate.py - harness-enforced "invoke the skill that covers this" rule.

Why: skills exist so a session does not rediscover what the team already wrote down, but a skill that is never
invoked does nothing - a session can edit a skill file and still never load it. This hook makes the harness say it.

Design choices that matter:
  * THE TRIGGER IS A FACT, NOT A GUESS. Matching free text to a skill is fuzzy and a matcher that cries wolf gets
    tuned out, so the gate fires on something unarguable: "this repo has skills, you have made N tool calls, and you
    have invoked zero." Relevance guesses are included only as labelled hints.
  * THE CATALOGUE IS GENERATED from the live SKILL.md frontmatter (name, description, "Triggers:" phrases).
  * ONE INVOCATION SATISFIES THE SESSION; the gate goes quiet rather than nagging per skill.
  * SPEAKS LATE, BLOCKS LATER, BLOCKS ONCE. Silent for the first NAG_AFTER_CALLS tool calls.

Modes (first argv):
  check    UserPromptSubmit. Remembers the prompt; past NAG_AFTER_CALLS calls with zero skills invoked, prints
           additionalContext listing the repo's skills + ranked hints.
  mark     PostToolUse (no matcher). Counts every tool call of the lead; records Skill invocations.
  stop     Stop. Blocks ONCE past BLOCK_AFTER_CALLS calls with zero skills invoked. Re-entry safe.
  waive    python3 skills-gate.py waive <session_id> <skill> "<reason>"   (audited)
  catalog  Prints the parsed catalogue.
  probe    python3 skills-gate.py probe "<prompt text>"  - shows the scores, for tuning.

State: .claude/state/skills-<session_id>.json, pruned after 14 days. Teammates are never counted or nagged.
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _hooklib import STATE as STATE_DIR, append_log, fail_open, is_agent_session, read_payload, sid_safe  # noqa: E402

HOME = os.path.expanduser("~")
PRUNE_AFTER_SECONDS = 14 * 24 * 3600

SKILL_DIRS = [os.path.join(HOME, ".claude", "skills")]

# Words too common to carry signal. Without this every prompt matches every skill.
STOP = set("""the and for with that this from your you our have has are was were will would can could
should about into out not but they them then than there here what when where which who why how all
any some more most other another each both few many much need needs want make made use used using
get got put set add new old run ran let like just now also only very same such its it's dont don't
please thanks ok okay yes no its also again still even ever never always able way ways thing things
work works working do does did done go goes going come came take took give gave find found look
looks looking see saw seen say said tell told ask asked keep kept let's lets us we i me my mine""".split())

TOKEN = re.compile(r"[a-z0-9_][a-z0-9_\-\.]{2,}")

STRONG = 5.0          # a lexical hint confident enough to name first
WEAK = 1.5            # worth listing as a hint at all
NL = "\n"
NAG_AFTER_CALLS = 25  # tool calls in a session with zero skills invoked before the gate speaks
BLOCK_AFTER_CALLS = 120  # ... and before it blocks the end of a turn, once


# --------------------------------------------------------------------------- catalogue

def parse_front_matter(path):
    """Return (name, description) from a SKILL.md, or (None, None).

    Deliberately NOT tools/common.parse_frontmatter: SKILL.md is Claude Code's format, written by anyone (global
    skills on a member's machine included) and allowed multi-line YAML descriptions, which the memory schema parser
    rejects on purpose. This reader only needs two fields and must never fail on a valid skill."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            head = fh.read(8000)
    except Exception:
        return None, None
    if not head.startswith("---"):
        return None, None
    end = head.find("\n---", 3)
    if end == -1:
        return None, None
    fm = head[3:end]
    name = desc = None
    m = re.search(r"^name:\s*(.+)$", fm, re.M)
    if m:
        name = m.group(1).strip().strip('"').strip("'")
    m = re.search(r"^description:\s*(.+?)(?=\n[a-zA-Z_]+:|\Z)", fm, re.M | re.S)
    if m:
        desc = " ".join(m.group(1).split()).strip().strip('"').strip("'")
    return name, desc


def build_catalog():
    """Every installed skill: name, description, trigger phrases, and weighted keywords."""
    cat = []
    seen = set()
    dirs = list(SKILL_DIRS)
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    dirs.append(os.path.join(cwd, ".claude", "skills"))
    for root in dirs:
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root)):
            sp = os.path.join(root, entry, "SKILL.md")
            if not os.path.isfile(sp):
                continue
            name, desc = parse_front_matter(sp)
            name = name or entry
            if name in seen:
                continue
            seen.add(name)
            desc = desc or ""
            # explicit "Triggers: 'a', 'b'" phrases carry the most signal - they were written
            # precisely to say "invoke me when you see this"
            trig = []
            for tm in re.finditer(r"[Tt]riggers?[^:]{0,12}:(.{0,600})", desc):
                trig += re.findall(r"'([^']{3,60})'", tm.group(1))
            words = set()
            for w in TOKEN.findall(desc.lower()):
                if w not in STOP and len(w) > 2:
                    words.add(w)
            for w in TOKEN.findall(name.lower().replace("-", " ")):
                if w not in STOP:
                    words.add(w)
            cat.append({
                "name": name,
                "desc": desc,
                "triggers": [t.lower() for t in trig],
                "words": words,
                "project": root != SKILL_DIRS[0],
            })
    return cat


def score(prompt, skill):
    """How strongly does this prompt call for this skill, and on what evidence?"""
    p = prompt.lower()
    toks = set(t for t in TOKEN.findall(p) if t not in STOP)
    pts = 0.0
    why = []

    for t in skill["triggers"]:
        if not t:
            continue
        if t in p:
            pts += 4.0
            why.append("trigger '" + t + "'")
            continue
        # a user rarely types a trigger phrase verbatim; treat "most of its words present" as a hit
        tw = [w for w in TOKEN.findall(t) if w not in STOP]
        if len(tw) >= 2 and sum(1 for w in tw if w in p) >= max(2, len(tw) - 1):
            pts += 2.0
            why.append("~trigger '" + t + "'")
    # the skill's own name appearing is near-decisive
    nm = skill["name"].lower()
    if nm in p or nm.replace("-", " ") in p:
        pts += 5.0
        why.append("names the skill")

    overlap = toks & skill["words"]
    # rare words are worth more than common ones; cap so a long description cannot win by size
    if overlap:
        pts += min(5.0, len(overlap) * 0.9)
        why.append("terms: " + ", ".join(sorted(overlap)[:5]))
    if skill["project"]:
        pts += 0.75   # a skill installed in THIS repo is more likely to be the right one
    return pts, why


# --------------------------------------------------------------------------- state

def ensure_state_dir():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
    except Exception:
        pass


def prune_old():
    now = time.time()
    try:
        for f in os.listdir(STATE_DIR):
            if not f.startswith("skills-"):
                continue
            p = os.path.join(STATE_DIR, f)
            if now - os.path.getmtime(p) > PRUNE_AFTER_SECONDS:
                os.remove(p)
    except Exception:
        pass


def state_path(sid):
    return os.path.join(STATE_DIR, "skills-" + sid_safe(sid) + ".json")   # same sid_safe as librarian-gate


def load_state(sid):
    try:
        with open(state_path(sid), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"invoked": [], "blocked": [], "waived": {}, "calls": 0}


def save_state(sid, st):
    # temp file + os.replace: a plain open('w') truncates first, so a concurrent reader sees an empty file
    ensure_state_dir()
    path = state_path(sid)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(st, fh)
        os.replace(tmp, path)
    except Exception as e:
        log_error("save_state", e)
        try:
            os.remove(tmp)
        except Exception:
            pass


def log_error(where, exc):
    """Fail open, but never silently: a dead gate must look different from a quiet one."""
    append_log("hook-errors.log", "%s skills-gate %s: %r" % (time.strftime("%Y-%m-%dT%H:%M:%S"), where, exc))


class state_lock:
    """Exclusive lock around one hook run's read-modify-write of the state file. os.replace makes each WRITE atomic
    but does not serialise them: hooks fired together each loaded the same state, and the later save dropped the
    earlier one's update (a Skill invocation was lost in 6 of 20 concurrent trials). Runs take milliseconds, so one
    lock for the whole state dir costs nothing. If locking itself fails, the run proceeds unlocked (fail open)."""

    def __enter__(self):
        self.fh = None
        try:
            ensure_state_dir()
            self.fh = open(os.path.join(STATE_DIR, "gate.lock"), "a+")
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                for _ in range(500):                       # up to ~5 s, then proceed unlocked
                    try:
                        msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(0.01)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
        except Exception as e:
            log_error("lock", e)
        return self

    def __exit__(self, *exc):
        if self.fh:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            self.fh.close()
        return False


# --------------------------------------------------------------------------- modes

def cmd_mark():
    """PostToolUse on EVERY tool: count the call, and note a Skill invocation when it happens."""
    data = read_payload("skills-gate")
    if is_agent_session(data):
        return 0
    sid = data.get("session_id")
    tool = data.get("tool_name") or ""
    st = load_state(sid)
    st["calls"] = int(st.get("calls", 0)) + 1
    if tool == "Skill":
        name = ((data.get("tool_input") or {}).get("skill") or "").strip()
        if name and name not in st["invoked"]:
            st["invoked"].append(name)
            save_state(sid, st)
            print(json.dumps({"systemMessage": "Skills gate satisfied: '%s' invoked." % name}))
            return 0
    save_state(sid, st)
    return 0


def cmd_stop():
    """Block the end of a turn ONCE when a long session never invoked a single skill."""
    data = read_payload("skills-gate")
    if data.get("stop_hook_active") or is_agent_session(data):
        return 0
    sid = data.get("session_id")
    st = load_state(sid)
    if st.get("invoked") or st.get("stop_blocked"):
        return 0
    calls = int(st.get("calls", 0))
    if calls < BLOCK_AFTER_CALLS:
        return 0
    cat = build_catalog()
    if not cat:
        return 0
    best = st.get("last_hint")           # [skill, reason], computed at the last prompt - the prompt itself is not kept
    st["stop_blocked"] = True            # blocks at most ONCE per session, never a loop
    save_state(sid, st)
    hint = (" Closest match: %s (%s)." % (best[0], best[1])) if best else ""
    print(json.dumps({
        "decision": "block",
        "reason": (
            "SKILLS GATE: %d tool calls this session and not one skill invoked.%s "
            "Invoke the skill that covers this work with Skill(skill=\"<name>\") and apply it, or "
            "state in one clause why none of the installed skills apply. This blocks once per "
            "session; it will not ask again." % (calls, hint)
        ),
    }))
    return 0


def best_hint(prompt, waived):
    """[skill, reason] for the best lexical match >= WEAK, else None."""
    ranked = sorted(((score(prompt, sk) + (sk["name"],)) for sk in build_catalog() if sk["name"] not in waived),
                    key=lambda r: r[0], reverse=True)
    if ranked and ranked[0][0] >= WEAK:
        pts, why, name = ranked[0]
        return [name, "; ".join(why[:1]) or "description match"]
    return None


def cmd_waive(argv):
    if len(argv) < 4:
        sys.stderr.write("usage: skills-gate.py waive <session_id> <skill> \"<reason>\"\n")
        return 64
    sid, name, reason = argv[1], argv[2], " ".join(argv[3:])
    st = load_state(sid)
    st.setdefault("waived", {})[name] = reason
    save_state(sid, st)
    append_log("skill-waivers.log", "%s\t%s\t%s\t%s" % (time.strftime("%Y-%m-%dT%H:%M:%S"), sid, name, reason))
    print("waived '%s' for session %s: %s" % (name, sid, reason))
    return 0


def cmd_catalog():
    for sk in build_catalog():
        print("%-28s %s%s" % (
            sk["name"],
            "[project] " if sk["project"] else "",
            (", ".join(sk["triggers"][:4]) or sk["desc"][:70])))
    return 0


def cmd_probe(argv):
    prompt = " ".join(argv[1:])
    rows = []
    for sk in build_catalog():
        pts, why = score(prompt, sk)
        if pts > 0:
            rows.append((pts, sk["name"], why))
    rows.sort(reverse=True)
    for pts, name, why in rows[:8]:
        flag = "STRONG" if pts >= STRONG else ("weak  " if pts >= WEAK else "      ")
        print("%6.2f %s %-28s %s" % (pts, flag, name, "; ".join(why[:2])))
    if not rows:
        print("(no skill matched)")
    return 0


def main(argv):
    mode = argv[1] if len(argv) > 1 else ""
    if mode in ("check", "mark", "stop", "waive"):           # the modes that read-modify-write state
        with state_lock():
            return run_mode(mode, argv)
    return run_mode(mode, argv)


def cmd_check():
    """UserPromptSubmit: past NAG_AFTER_CALLS calls with zero skills invoked, nag. Keeps only the computed hint for
    the Stop hook - never the prompt text, which can hold anything a member typed (state is local, but still)."""
    data = read_payload("skills-gate")
    if is_agent_session(data):
        return 0
    sid = data.get("session_id")
    prompt = data.get("prompt") or ""
    st = load_state(sid)
    st.pop("last_prompt", None)                                     # state written by an older version
    if not st.get("invoked"):
        st["last_hint"] = best_hint(prompt, set((st.get("waived") or {}).keys()))
    save_state(sid, st)

    if st.get("invoked"):
        return 0                      # habit already broken this session
    calls = int(st.get("calls", 0))
    if calls < NAG_AFTER_CALLS:
        return 0                      # trivial or early session: say nothing
    cat = build_catalog()
    if not cat:
        return 0
    waived = set((st.get("waived") or {}).keys())
    ranked = []
    for sk in cat:
        if sk["name"] in waived:
            continue
        pts, why = score(prompt, sk)
        ranked.append((pts, sk["name"], why, sk["project"]))
    ranked.sort(reverse=True)
    hints = [r for r in ranked if r[0] >= WEAK][:3]
    proj = sorted(sk["name"] for sk in cat if sk["project"] and sk["name"] not in waived)
    names = proj or sorted(sk["name"] for sk in cat if sk["name"] not in waived)[:10]

    lines = []
    if hints:
        lines.append("  most likely: " + ", ".join(
            "%s (%s)" % (n, "; ".join(w[:1]) or "description match") for _, n, w, _ in hints))
    lines.append("  installed in this repo: " + ", ".join(names))
    msg = (
        "SKILLS GATE (harness hook, not a memory note): %d tool calls into this session, "
        "ZERO skills invoked." % calls + NL + NL.join(lines) + NL +
        "Invoke the one that fits - Skill(skill=\"<name>\") - and apply it. Reading a skill "
        "file, or remembering it, is NOT the same thing. "
        "If none apply, say so in one clause and carry on - or waive one: "
        "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/skills-gate.py\" waive " + str(sid) + " <skill> \"<reason>\""
    )
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": msg}}))
    return 0


def run_mode(mode, argv):
    ensure_state_dir()
    prune_old()
    if mode == "check":
        return cmd_check()
    if mode == "mark":
        return cmd_mark()
    if mode == "stop":
        return cmd_stop()
    if mode == "waive":
        return cmd_waive(argv[1:])
    if mode == "catalog":
        return cmd_catalog()
    if mode == "probe":
        return cmd_probe(argv[1:])
    sys.stderr.write(__doc__)
    return 64


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except SystemExit:
        raise
    except Exception as exc:
        fail_open("skills-gate", exc)   # a broken gate must never break a session - but it must leave a trace
