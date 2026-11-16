# Changelog

## v1.1.2 - 2026-11-15

One false block that v1.1.1 introduced in `git-guard.py`, and the parsing flaw behind it. Upgrading: pull.

### Fixed

- `git-guard.py` review rule: a small commit whose quoted message holds an apostrophe before a parenthesis
  (`git commit -m "fix: don't crash (again)"`) was blocked in v1.1.1 whenever the tree held enough unrelated unstaged
  changes: the message was cut at the parenthesis, a message word was read as a pathspec, and the whole tree was
  counted. The command splitter now treats `(` and `)` inside quotes as text. `$(...)` and backticks inside double
  quotes still run, and the rules that guard uncommitted work also judge the old reading, so none of them got weaker.
- The same flaw under-counted two commit forms in every earlier version; both are counted now: a quoted message
  holding both braces and parentheses, and a path given after a message with parentheses
  (`git commit -m "fix: handle (x) case" a.txt`).
- A pathspec token with a stray quote character no longer makes the whole tree count; only a magic pathspec
  (`:(glob)...`, `:!x`, also with a leftover opening quote) does.
- A `git add` whose arguments the hook cannot resolve is counted as `git add -A` (logged) instead of not at all: a
  shell or PowerShell variable (`for f in ...; do git add $f; done`, `"$f"`, `${f}`, `$_`, `%VAR%`), the `{}` of
  `find -exec` or `xargs -I{}`, a plain `xargs git add`, `$(...)` or backticks. A `git add` started by `find -exec`,
  `xargs`, `env` or `command` is now seen for the count.

### Changed

- Known limits of the review rule after this release. Not counted: `git commit --amend` beyond its new change; a
  `git mv`, `git rm` or a file created in the same line as the commit; a variable used as a commit pathspec
  (`git commit -m x $f`); a commit pathspec whose file name holds a quote character (new in this release); an add
  fed by `parallel`; a commit that follows a `cd` the shell does not apply (in a pipeline, a subshell or the
  background, or a `cd` that fails), which is then counted in the wrong directory; a commit followed by a `#`
  comment that holds braces; git aliases and merge / cherry-pick / am / rebase. Over-counted: a new symlink;
  `git rm --cached f && git add f`; an add in a directory outside any repository; an unresolved add in a tree with
  unrelated changes (it is counted as `git add -A`), which includes a literal `$` in a single-quoted file name and
  an add whose output is redirected to a `"$VAR"` target (new in this release).
- Known limits of the rules that guard uncommitted work, present in every version so far: a discard after a `cd`
  that fails (`cd missing-dir; git reset --hard` - the shell stays in the repository, the hook follows the `cd`),
  and a discard inside a quoted command string that holds a separator (`bash -c "git status && git reset --hard"`,
  `powershell -Command "...; git stash"`) are not blocked.
- Self-test case labels in `git-guard.py` now say what they check; more cases and mutation entries.
- `tests/test_team_gates.py`: each mutation run gets 15 minutes instead of 5 (on a heavily loaded machine a run could
  time out and be reported as an error).

## v1.1.1 - 2026-11-15

Fixes to the guard hooks, found by reviewing v1.1 again. Upgrading from v1.1: pull; nothing to configure.

### Fixed

**`git-guard.py` - the review rule no longer touches your index, and no longer slows every commit**

- The `git add --dry-run` that tells what an add in the same line stages now runs on a private copy of the index
  (`GIT_INDEX_FILE`, mtime kept), and every git call the hook makes sets `GIT_OPTIONAL_LOCKS=0`. In v1.1 the dry run
  took the index lock: a concurrent git command could fail with "index.lock exists", and while another git held the
  lock a new file was counted as 0 lines. Copies left behind by a killed hook are removed at a later hook start.
- A commit runs only the diffs its shape needs (a plain commit: `git diff --cached` alone; an add and commit: the
  added files; a pathspec commit: those paths; `-a`: the whole tree). v1.1 ran a whole-tree diff on every commit,
  which took seconds on Windows on a tree with ~100 dirty files and could run past the hook timeout.
- Counting has one time budget per hook run (3 s, fitted with the dirty-tree check under 9 s), shared by every commit
  in the line. Reading new files stops once the review threshold is reached. A count that fails or times out is
  logged and treated as over the threshold, never as 0.
- Counts that were wrong in v1.1:
  - a `git add` in a subdirectory of the same repository (`cd sub && git add f && cd .. && git commit`,
    `git -C sub add f && git commit`) was not counted; an add in another repository still is not;
  - shell braces around a commit (`{ git add f && git commit -m x; }`, `if ($?) { git commit ... }`) turned `}` into
    a pathspec and the count into 0; a trailing `fi` / `done` / `)` is never a pathspec now;
  - a file removed from the index, written again and re-added counted only its deletion;
  - a new file whose name holds a newline or non-UTF-8 bytes counted 0; output of the dry run that cannot be parsed
    is now logged and counted as `git add -A`;
  - a magic pathspec (`:(glob)...`, `:!x`) failed the count; it now counts the whole tree (logged);
  - a pure `git mv` counted twice the file's length (rename detection is on again).
- Known limits, unchanged from v1.1. Not counted (the commit is allowed without a review): `git commit --amend`
  beyond its new change; a `git mv`, `git rm` or a file created in the same line as the commit; an add whose
  argument is a shell variable (`for f in ...; do git add $f; done`); an add run by `find -exec` or `xargs`; a
  commit whose quoted message holds both braces and parentheses; git aliases and merge / cherry-pick / am / rebase.
  Over-counted (a review is asked for needlessly): a new symlink; `git rm --cached f && git add f`; an add in a
  directory outside any repository.

**`heredoc-guard.py`**

- Commands with long lines or long runs of blank lines are decided in milliseconds. In v1.1 (and v1.0) one 30 KB
  line could take about 10 seconds, past the hook timeout, with or without a `<<` in it. Very long heredoc tags are
  several times faster than in v1.1 but not bounded: a command holding two different words of thousands of letters,
  each alone on a line, can still take seconds. The
  heredoc patterns are anchored and bounded and match exactly what they matched before (checked against several
  thousand generated commands); commands without `<<` skip the scan, and the scan runs once per call.
- A command over the `<<` work cap is treated as file content and decided at once. `# heredoc-long-ok` lifts that
  cap and nothing else: a doubled backslash in Python, a `python -` with nothing on stdin, a Python heredoc that
  writes a file and a backtick in `python -c "..."` are still judged on the whole text, and so is a single
  backslash (`# heredoc-ok` overrides that one). Over the cap these tests see the whole command, so a large C or
  C++ heredoc with backslashes next to a Python call is blocked: write such files with the Write tool.
- `python -` with nothing on stdin is blocked again behind a wrapper: `timeout`, `sudo`, `env`, `nice`, `xargs`,
  `winpty`, `setsid`, `flock`, `doas`, `chronic`, `strace`, `taskset`, `uv` / `poetry` / `pipenv` / `conda` / `pdm` /
  `hatch` / `rye` / `pixi run` and `pyenv exec` (the wrapper's own flags and arguments are skipped, not just its
  name), and as an unquoted Windows path to `python.exe`. A wrapper flag the guard does not know falls back to the
  whole-text test. `ssh host python -` is not a local stdin problem and stays allowed.
- Known limit, unchanged from v1.1: when one line opens two heredocs (`cat <<'A'; python3 - <<'B'`), the body of
  the second one is not checked.

**Other hooks**

- `team-gate.py`: a lead-call counter that went backwards (its state was reset) no longer silences the nag until
  the count passes its old value; the nag follows the normal thresholds from the new count.
- `session-log.py`: its `git diff --stat` sets `GIT_OPTIONAL_LOCKS=0` too.

### Changed

- Self-tests and `tests/test_team_gates.py`: many more cases, and a mutation entry for each guard above (the list
  went from 27 to about 80 entries). `tests/run_all.py` gives each suite 30 minutes, because the mutation suite now
  takes several minutes. The `heredoc-guard.py` self-test now has timing bounds (10 runs of each check within 2 s;
  they take well under half a second on an idle machine): on a machine oversubscribed many times over they can fail
  without anything being wrong - re-run it.
- `git-guard.py`: the review-rule block message says "at least N changed lines", because counting stops at the
  threshold.

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
