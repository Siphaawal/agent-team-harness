# Changelog

## v1.1 - 2026-11-15

Team gates, an optional Obsidian vault, a memory graph in Insight, and fixes to two guard hooks.

**Upgrading from v1.0:** pull. The new hooks are wired by the committed `.claude/settings.json`. Then run
`python3 tests/run_all.py` and `python3 tools/memory_verify.py`. The vault and the memory graph are optional.

### Added

- **Team gates** (`.claude/hooks/`), lead-session only, fail open, state under `.claude/state/`:
  - `team-gate.py` nags after 20 lead tool calls with no named, non-helper teammate and blocks the Stop once at 40. An
    audited `waive` silences it; a waiver expires once the lead writes 3 distinct project files; a spawn replaces it.
  - `team-stall-gate.py` refuses idle (exit 2, "check your blocker directly now") to a teammate that still owns an
    in_progress task, up to 3 times, then records the stall and blocks the lead's Stop once with the list. It also
    blocks the Stop while teammates are active and no watchdog is running.
  - `team-watchdog.py` is started in the background by the lead and exits with `STALE`, `OVERSIZE`, `QUIET`, `MAX` or `ERROR`,
    which wakes the lead. It skips teammates whose tasks are all completed and teammates stopped with `TaskStop`.
  - `_teamlib.py` (shared code), settings.json wiring and allowlist, `tests/test_team_gates.py` (with mutation cases
    that fail when a guard is compiled out) and three hook self-tests in `tests/run_all.py`.
- **`tools/obsidian_vault.py`** (`setup`, `check`, `lock-app`, `path`): a locked-down Obsidian vault outside the repo,
  linking the live `memory/` and `agent/` (symlinks; directory junctions on Windows), with Sync, Publish, the web
  viewer and community plugins off and automatic updates off in Obsidian's app config; `check` also flags themes and
  CSS snippets (CSS can load remote content). Never installs Obsidian, no
  network. `.obsidian/` is also in `.gitignore`. Tests: `tests/test_obsidian.py`.
- **Insight memory graph**: a panel (`GET /api/memgraph`, built per request from `memory/`, 2D canvas, nothing
  vendored) with `[[link]]`, `superseded_by` and shared-tag edges, caps of 400 nodes and 2,400 edges, hostile names
  and symlinks counted and never opened, and an OBSIDIAN button when the vault exists. A re-runnable "does it phone
  home?" check procedure is in `insight/README.md`.
- `git-guard.py`: a commit of 150 or more changed lines is blocked when no reviewer, verifier or adversary was
  dispatched since the last commit (`# no-review-ok: <reason>` overrides, logged).
- `heredoc-guard.py`: blocks `python -` with nothing on stdin and a Python heredoc that writes a file
  (`# heredoc-write-ok`), and reports "heredoc block #N in the last 7 days" from a rotated block log.
- **`builder` role agent**: `agents-src/builder.src.md` -> `.claude/agents/builder.md` (the build contract plus
  lessons tagged `build`) and `.claude/agent-memory/builder/MEMORY.md`; the team-lead skill spawns builders with
  `subagent_type: "builder"`. Still subject to `agent-model-guard.py` like every other role.

### Changed

- `team-lead` skill (rules v2) and `wrap` skill revised; the `reviewer` role agent source and its generated file
  updated; `docs/ARCHITECTURE.md` gains sections on the team gates and on the vault and memory graph.
- README, `docs/SPEC.md` and `CLAUDE.md` describe the above.

### Fixed

- `git-guard.py` takes a commit's line count from the parsed commit: a word in the message can no longer make it
  count the whole tree, and a pathspec commit is counted for those paths.
- `tools/common.py`: the slug, file-name and date patterns end in `\Z`, not `$` (which also matched before a final
  newline, so a file named `x<LF>.md` passed as a clean slug on Linux and macOS).
- Insight: a Python interpreter that cannot be spawned at all (for example `AGENT_PYTHON` set to a `.bat` on Windows)
  no longer crashes the server at startup; the lessons box reports it as unavailable.

## v1.0

First release: one shared persona and memory per team, loaded into every member's Claude Code session through
SessionStart hooks; one-file-per-memory storage with lessons and trigger questions, generated indexes, and a tier-0
packer that keeps every hook output under Claude Code's size cap; the memory librarian, the `/wrap` and `team-lead`
skills, the `reviewer`, `verifier` and `adversary` role agents; guard hooks for uncommitted work, timestamps and agent
spawns; the Insight local dashboard; and Python and Node tools with no dependencies, plus a test runner and a
memory health check.
