# ARCHITECTURE - how the team's agent remembers

One persona and one memory, shared by every member through git. Each member runs Claude Code in their own clone; the
hooks give every session the same identity, the same lessons and an index of everything the team has written down.

## The two problems this solves

1. **Context is finite; memory grows.** Loading "everything important" at startup crowds out the work and, past a
   per-hook size cap, silently fails: Claude Code replaces any single hook output over 10,000 characters with a
   2,000-character preview of a saved file. A memory system that degrades without saying so is worse than a small
   one, because everyone keeps trusting it.
2. **Saving depends on remembering to save.** Knowledge that is never written back dies with the session - and on a
   team, knowledge one member's session learned is invisible to every other member unless it reaches the repo.

## Three tiers, by how often the content is needed

| Tier | Content | Loaded |
|---|---|---|
| **0** | `agent/IDENTITY.md`, `SOUL.md`, `TEAM.md`, `PROJECTS.md`, `ROUTING.md`, the generated TRIGGERS (every lesson's question) and INDEX (every memory's one-liner) | every session, by 8 SessionStart hooks |
| **1** | `docs/ARCHITECTURE.md`, `docs/SPEC.md` | on demand |
| **2** | `memory/topics/`, `memory/lessons/`, `memory/log/` | `tools/memory_tools.py search`, or the `memory-librarian` subagent |

- **Tier 0 is expensive; everything in it earns its place, and it carries pointers, never detail.**
- **Packed, not truncated.** `tier0-pack.py` splits tier 0 into parts of at most 9,000 bytes, one hook per part; part 1
  carries a manifest and an OVERFLOW banner, so a missing part is noticed and said out loud. The identity block is
  emitted first, so if anything is ever cut, what survives is what matters.
- **Hooks run in parallel**, so each part computes the index and triggers in-process (`memory_index.py --print`) rather
  than waiting for another hook.

## Retrieval: the librarian is the whole trick

A subagent has its own context window. The session states its goal, dispatches `memory-librarian`, and gets back a
brief of about 2 KB - the main session pays for the brief, not the corpus. `librarian-gate.py` makes this happen: it
nags on every prompt until the dispatch is observed and blocks the end of a turn once (trivial sessions under 5 tool
calls are waived automatically; any other waiver is logged with its reason).

Most questions are one search: `python3 tools/memory_tools.py search "<terms>"` ranks memories, lessons and skills
(BM25, stdlib) and labels superseded results.

## Writing: frontmatter is the schema

Every memory is one file with the frontmatter in `docs/SPEC.md`, validated by `tools/memory_lint.py`. The fields doing
real work:

- **`status`** - `superseded` means *do not act on this* (kept, listed first in the index, with `superseded_by`, so
  other members' citations still resolve and the reversal leaves a scar). `ephemeral` describes a moment and is
  flagged stale 14 days after `verified`. **Expire by truth, not by date.**
- **`type: lesson` + `trigger:`** - a lesson is stored with the QUESTION to ask before acting. Knowing a fact is not
  the same as having a question that fires at the right moment; only the triggers are in tier 0.
- **`tags`** - lessons tagged `build`, `review`, `verification` or `red-team` are compiled into the builder,
  reviewer, verifier and adversary role agents by `tools/agent_gen.py`. The repo ships no lessons, so a new team's
  role agents carry an empty lesson list until /wrap records the first tagged lesson.

## Why it is shaped this way for a team

| Single-user habit | Team version | Why |
|---|---|---|
| one LESSONS.md, numbered | one file per lesson, keyed by slug | two members appending at once would conflict and collide on numbers |
| hand-kept or committed indexes | `memory/_gen/` generated and gitignored | every merge would conflict on a generated file |
| one daily log | `memory/log/YYYY-MM/YYYY-MM-DD.<member>.md` | one file per member per day never conflicts |
| memory written straight to disk | memory merged by PR | a merged memory is truth for every member's agent; one bad note misleads the whole team |
| personal profile in tier 0 | `TEAM.md` shared; each member's `~/.config/agent-team-harness/me.md` local | a shared profile of one person is wrong for everyone else |
| hooks in a user's `~/.claude` | hooks in `.claude/settings.json` via `$CLAUDE_PROJECT_DIR` | every clone gets them; CODEOWNERS guards them because they run on every machine |

Auto memory (`~/.claude/projects/<slug>/memory/`) stays ON: subagent `memory:` fields have no effect without it, and
it is a fine place for one member's private working notes. Team knowledge does not go there - no other member can
see it.

## The write-back half: `/wrap`

A procedure, so it does not depend on remembering: gather evidence from git (not recollection), score memory and skill
use, enrich the member's daily log (the SessionEnd hook already left a skeleton), search before creating, write
memories and lessons with full frontmatter, supersede what was disproved, regenerate, run `memory_verify.py`, and offer
a memory-only branch for review.

## Agents

- `agents-src/CORE-AGENTS.md` - the ~3 KB core every spawned agent receives (SubagentStart hook).
- `agents-src/<role>.src.md` -> `.claude/agents/<role>.md` (generated) - builder, reviewer, verifier, adversary.
  Builders spawn as the `builder` role so the build contract and the `build` lessons reach every builder without the
  lead pasting them into each brief.
- `.claude/agent-memory/<role>/MEMORY.md` - each role's own memory, shared by the team (`memory: project`).
- `agent-model-guard.py` - every spawn states its job class and an allowed model.
- Agent teams run inside one member's lead session; there is no channel between members' sessions. Members coordinate
  through git.

## Team gates: parallel work gets named, watched teammates

Running a team fails in quiet ways: the lead does parallel work alone, a teammate finishes and sits idle with no
report, and nothing wakes a lead whose turn has already ended. Three hooks turn those habits into mechanisms. All of
them keep project-scoped state under `.claude/state/`, fail open, and log their own errors.

| Hook | Event | What it does |
|---|---|---|
| `team-gate.py` | tool use, Stop | nags after a run of lead tool calls with no named (non-helper) teammate; blocks the Stop once at a higher count; an audited `waive` silences it, and the waiver EXPIRES once the lead writes three distinct project files (scratch, memory and state do not count) - a spawn replaces a waiver |
| `team-stall-gate.py` | TeammateIdle, Stop | refuses idle (exit 2, "check your blocker directly now") to a teammate that still owns an in_progress task, up to 3 times, then records the stall and blocks the lead's Stop once with the list; in Stop mode it also blocks while teammates are active and no live watchdog is running |
| `team-watchdog.py` | started by the lead, in the background | exits `STALE` (a teammate quiet for `--minutes`, naming who and its last action), `QUIET`, `MAX`, `OVERSIZE` (a teammate's context over the limit, once per teammate) or `ERROR` (the watchdog itself failed; logged, exit 0); skips teammates whose tasks are all completed and teammates that were stopped |

The exit of the watchdog is what re-invokes a lead that would otherwise be idle, which is why the stall gate insists a
watchdog is running before the lead may end a turn. Both gates key on the tasks a teammate OWNS, so the team-lead skill
requires one task per teammate. The shipped guards were tightened in the same release: `git-guard.py` counts commit
size from the parsed commit (a pathspec commit counts only its paths) and asks for a reviewer on a large commit, and
`heredoc-guard.py` keeps a block log so a repeated habit is reported with its count.

## The Obsidian vault and the memory graph

Memory is plain markdown with `[[links]]`, so it can be read as a graph.

- **`tools/obsidian_vault.py`** sets up an Obsidian vault OUTSIDE the repo, holding links (symlinks, or directory
  junctions on Windows) to `memory/` and `agent/`, with a locked-down `.obsidian/` written before the first launch:
  automatic updates off, Sync, Publish and the web viewer off, community plugins off, no themes or CSS snippets
  (CSS can load remote content; `check` flags any that appear). A note can still embed a remote image, which Obsidian
  fetches when the note is opened. `setup`
  refuses to overwrite an existing vault, `check` exits 1 if any lockdown value has drifted, `path` prints the vault
  directory, `lock-app` turns automatic updates off in Obsidian's own app config. It never installs Obsidian and never touches the network. The vault sits outside the repo because
  `.obsidian/` holds per-user workspace state that must not be committed (it is also in `.gitignore`).
- **Honest limit:** the links point at the LIVE working tree, not a copy. An edit or rename made inside Obsidian is an
  edit to team memory (`memory_lint.py` catches a broken file; a rename desyncs `name:`). Treat the vault as a reader.
- **Why updates are switched off:** one measurement on Obsidian 1.15.0 saw 0 outbound connections with automatic
  updates off and 2 (the update endpoints) with them on. That is a data point, not a guarantee - re-measure with a
  logging proxy and a positive control (updates on) rather than trusting it.
- **Insight memory graph** is a panel in the Insight dashboard: nodes are memories and lessons, edges are `[[links]]`
  and shared tags, built from `memory/` by the server at request time (nothing generated is committed). It keeps every
  Insight rule - token on `/api/*`, Host allow-list, CSP, no outbound calls, no write endpoints - and shows an OBSIDIAN
  button (`obsidian://open?vault=<name>`) only when the vault exists.

## Failure modes and their defences

| Failure | Defence |
|---|---|
| Tier 0 silently truncated | packed into parts under the cap; manifest + OVERFLOW banner; `memory_verify` check 3 |
| Project hooks disabled by org policy | CLAUDE.md tells the agent to say so when no tier-0 part arrived |
| Index drifts from reality | generated every session from frontmatter; hand edits are overwritten |
| Reversed decision read as current | `status: superseded` + `superseded_by`, listed first as "do NOT act on" |
| Stale moment-in-time note trusted | `status: ephemeral` + staleness flag after 14 days |
| Lesson known but not applied | the trigger question is in tier 0; a repeat re-hit becomes a hook |
| Personal or secret data committed | `leak_scan.py` (built-in patterns + each member's local list), `memory_verify` check 5 |
| A merge runs code on every machine | CODEOWNERS on `.claude/`, `tools/`, `agent/`, `insight/` |
| Uncommitted work discarded by an agent | `git-guard.py` |
| A gate dies quietly | every hook logs its own errors to `.claude/state/` and fails open |
| Parallel work done alone, or an idle teammate nobody sees | `team-gate.py`, `team-stall-gate.py`, `team-watchdog.py` |
| Obsidian phones home or edits team memory unnoticed | `obsidian_vault.py check` (lockdown drift); the vault is documented as read-only by convention |
