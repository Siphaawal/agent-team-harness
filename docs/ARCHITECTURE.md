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
- **`tags`** - lessons tagged `review`, `verification` or `red-team` are compiled into those role agents by
  `tools/agent_gen.py`.

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
- `agents-src/<role>.src.md` -> `.claude/agents/<role>.md` (generated) - reviewer, verifier, adversary.
- `.claude/agent-memory/<role>/MEMORY.md` - each role's own memory, shared by the team (`memory: project`).
- `agent-model-guard.py` - every spawn states its job class and an allowed model.
- Agent teams run inside one member's lead session; there is no channel between members' sessions. Members coordinate
  through git.

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
