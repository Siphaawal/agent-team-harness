#!/usr/bin/env python3
"""init_agent.py - name the team's agent: fill the {{...}} placeholders in agent/*.md.

    python3 tools/init_agent.py --name "Atlas" --team "Platform" [--vibe "..."] [--emoji "..."] [--dry-run]
    python3 tools/init_agent.py            (asks for each value - only when stdin is a terminal)

Fills {{AGENT_NAME}}, {{TEAM_NAME}}, {{VIBE}}, {{EMOJI}} in agent/IDENTITY.md, SOUL.md, TEAM.md, PROJECTS.md,
ROUTING.md. Refuses to run when no placeholder is left (already initialised - edit the files by hand from then on),
and refuses values containing "{{", newlines or more than 60 characters. Writes each file atomically. Run once per
team, then commit agent/ through a reviewed PR.

Prompts only on a terminal and never with --dry-run, so a script, a pipe or CI never hangs or crashes on a prompt:
without a terminal, --name and --team are required. --vibe and --emoji fall back to defaults.

Exit 0 done, 2 refused (already initialised, no agent/*.md, a symlink in agent/, a bad value, --root outside the repo
without AGENT_TOOLS_ANY_ROOT=1), 64 usage (bad flag, --name/--team missing with no terminal to ask).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
from common import atomic_write, read_text, repo_root  # noqa: E402

FILES = ["IDENTITY.md", "SOUL.md", "TEAM.md", "PROJECTS.md", "ROUTING.md"]
DEFAULTS = {"VIBE": "Concise, builder-minded, opinionated, security-first.", "EMOJI": "*"}


def clean(label, value):
    value = (value or "").strip()
    if not value:
        common.die("REFUSING: %s is empty" % label, common.REFUSED)
    if "{{" in value or "}}" in value or "\n" in value or len(value) > 60:
        common.die("REFUSING: %s must be one line, <= 60 chars, without {{ }}" % label, common.REFUSED)
    return value


def can_prompt():
    """True only for a real terminal on stdin."""
    try:
        if sys.stdin is None or not sys.stdin.isatty():
            return False
        if os.name == "nt":
            # NUL is a character device, so isatty() is True for `< NUL` and for subprocess.DEVNULL; only a console
            # handle has a console mode
            import ctypes
            import msvcrt
            mode = ctypes.c_uint32()
            return bool(ctypes.windll.kernel32.GetConsoleMode(msvcrt.get_osfhandle(sys.stdin.fileno()),
                                                              ctypes.byref(mode)))
        return True
    except Exception:
        return False


def ask(prompt, default=None):
    try:
        return input(prompt).strip() or default
    except EOFError:
        common.die("init_agent: no answer (end of input) - pass --name and --team", common.USAGE)


def main(argv=None):
    common.utf8_stdout()
    ap = common.ArgParser(prog="init_agent.py", description="Fill the persona placeholders in agent/*.md")
    ap.add_argument("--name", help="the agent's name")
    ap.add_argument("--team", help="the team's name")
    ap.add_argument("--vibe", help="one line of personality (default: %s)" % DEFAULTS["VIBE"])
    ap.add_argument("--emoji", help="default: %s" % DEFAULTS["EMOJI"])
    ap.add_argument("--root", default=repo_root(), help="repo root (default: the repo this tool is in)")
    ap.add_argument("--dry-run", action="store_true", help="show what would change; write nothing; never prompt")
    a = ap.parse_args(argv)
    common.inside_or_die(a.root, "--root")
    agent_dir = os.path.join(a.root, "agent")
    links = [f for f in FILES if os.path.islink(os.path.join(agent_dir, f))]
    if os.path.islink(agent_dir) or links:
        common.die("init_agent: REFUSED - symlink in agent/ (%s); never followed" % (", ".join(links) or "agent/"),
                   common.REFUSED)
    texts = {f: read_text(os.path.join(agent_dir, f)) for f in FILES if os.path.isfile(os.path.join(agent_dir, f))}
    if not texts:
        common.die("init_agent: REFUSED - no agent/*.md found under %s" % a.root, common.REFUSED)
    if not any("{{" in t for t in texts.values()):
        common.die("init_agent: already initialised: no {{...}} placeholders left in agent/ - edit the files directly",
                   common.REFUSED)
    interactive = can_prompt() and not a.dry_run
    if not (a.name and a.team) and not interactive:
        common.die("init_agent: --name and --team are required (no terminal to ask)", common.USAGE)
    vals = {
        "AGENT_NAME": clean("--name", a.name or ask("Agent name: ")),
        "TEAM_NAME": clean("--team", a.team or ask("Team name: ")),
        "VIBE": clean("--vibe", a.vibe or (ask("Vibe [%s]: " % DEFAULTS["VIBE"], DEFAULTS["VIBE"])
                                           if interactive and not a.name else DEFAULTS["VIBE"])),
        "EMOJI": clean("--emoji", a.emoji or DEFAULTS["EMOJI"]),
    }
    changed = []
    for f, t in texts.items():
        new = t
        for k, v in vals.items():
            new = new.replace("{{%s}}" % k, v)
        if new != t:
            changed.append(f)
            if not a.dry_run:
                atomic_write(os.path.join(agent_dir, f), new)
    left = [f for f in FILES if f in texts and "{{" in (texts[f] if a.dry_run else read_text(os.path.join(agent_dir, f)))]
    print("%s %s: %s" % ("would update" if a.dry_run else "updated", ", ".join(changed) or "nothing",
                         "; ".join("%s=%s" % kv for kv in vals.items())))
    if left and not a.dry_run:
        print("placeholders still present in: %s" % ", ".join(left))
    print("next: fill TEAM.md and PROJECTS.md by hand, then commit agent/ through a reviewed PR")
    return common.OK


if __name__ == "__main__":
    sys.exit(main())
