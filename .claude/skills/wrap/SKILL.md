---
name: wrap
description: Close out a work session by writing what was learned back into the team's memory - enriches the member's daily log, creates or updates memory files, records lessons with their trigger question, supersedes anything the session disproved, regenerates the index and role agents, runs the health check, and prepares the memory change for a PR. Use when the member says wrap up, we're done, end of session, save what we learned, or /wrap. Also use before a long break or when context is about to run out.
---

# /wrap - session write-back

Loading memory is solved by the SessionStart hooks and the `memory-librarian`. **This skill is the other half: getting
what the session learned back onto disk, where the next member's session will find it.** Work through the steps in
order. Do not skip step 1 - a log written from recollection alone is confident fiction.

## 1. Establish what actually happened

- **Run `date` first** and stamp everything with that local date and time. A time not read from the clock is invented
  (`stamp-guard` refuses future stamps; it cannot catch a past guess).
- `git status`, `git diff --stat`, `git log --oneline -5` in each repo touched. **Never commit** unless the member asks.
- Which verifications actually RAN, and which were inferred. Keep them separate.
- If the session ran teammates or subagents, collect every hand-in's `LEARNED:` bullets - they are lost with the
  teammate otherwise.

If the session produced nothing durable, say so and stop. An empty log beats an invented one.

## 2. Score memory and skill use (evidence, not impression)

- Was the `memory-librarian` dispatched, and did its brief change what was done?
- For each tool family used heavily: was the matching skill invoked BEFORE the first call?
- **Hazards:** list every trap hit this session (error-retry loops, rollbacks, wrong writes, corrected stamps). For
  each, run `python3 tools/memory_tools.py search "<the trap>"`. Recorded before = a **RE-HIT**: fix the record in
  this wrap (sharpen the trigger question, or - on a repeat re-hit - make it a hook). Not recorded = a new lesson.
- Write ONE line into the log: `Memory/skills: <n> recalled+used / <n> re-hit (fixed: ...) / <n> new; skills
  <invoked> of <needed>. Verdict: effective | improve: <the one mechanism change made>.`

## 3. Enrich the daily log

Path: `memory/log/YYYY-MM/YYYY-MM-DD.<member>.md` (the member id is in the SESSION section of tier 0; or run
`python3 tools/common.py member`). One file per member per day, so members never conflict.

The SessionEnd / PreCompact hook may already have appended an **auto skeleton** for this session. Replace its
"(auto skeleton - /wrap enriches it)" line with the sections below instead of adding a second heading. Append, never
overwrite earlier entries. Absolute dates only.

```markdown
## <HH:MM zone> - <what the session was about>

**Did:** <what shipped, with paths>
**Learned:** <the non-obvious part - the seed for a memory or lesson>
**Verified:** <what was confirmed, and how>
**Inferred (not verified):** <be honest>
**Open:** <unfinished work, decisions waiting on a member>
```

## 4. Decide what becomes a memory

A log is narrative; a memory is a durable fact. Promote only what will still matter in a month. **Before creating
anything, search** - `python3 tools/memory_tools.py search "<what the fact is about>"` - because one updated file beats
a near-duplicate, and grep only finds the words you already guessed.

- **New durable fact** -> `memory/topics/<slug>.md` (type rule | decision | project | reference | runbook).
- **How-to-work lesson** (true beyond this one codebase task: a verification habit, a toolchain trap) ->
  `memory/lessons/<slug>.md`, `type: lesson`, with a `trigger:` - the QUESTION to ask before acting, ending in `?`.
  A lesson without a trigger does not fire; the trigger is what reaches every member's tier 0. Tag it for the roles it
  serves (`review`, `verification`, `red-team`) so they receive it.
- **Refines an existing memory** -> edit it; set `verified:` to today.
- **Contradicts an existing memory** -> set the old one `status: superseded` + `superseded_by: <new-slug>`, keep a
  one-line stub of why it reversed. Never silently delete: other members' sessions may cite it.
- **Turned out wrong** -> delete it, and say so in the PR description.
- **Time-sensitive** (handoff, open-issue list) -> `status: ephemeral` with `verified:` today; it is flagged stale
  after 14 days.

Don't save what the repo already records (code structure, git history, CLAUDE.md). Never write secrets, credentials,
customer data or anything a member marked private - memory is committed and every member reads it. A member's
private working preference belongs in their own `~/.config/agent-team-harness/me.md`, not here.

Frontmatter (the schema `tools/memory_lint.py` enforces):

```markdown
---
name: <slug = file name>
description: <one line - the recall signal; specific and searchable>
metadata:
  type: rule | decision | project | reference | lesson | runbook
  tags: [<reuse existing tags>]
  status: active | superseded | ephemeral
  author: <member>
  created: <YYYY-MM-DD from `date`>
  verified: <YYYY-MM-DD, optional>
  trigger: "<question?>"        # lessons only
---

<the fact. For rules and decisions add **Why:** and **How to apply:** lines. Link others with [[slug]].>
```

## 5. Regenerate and check

```bash
python3 tools/memory_index.py            # memory/_gen/ (gitignored; every session rebuilds it anyway)
python3 tools/agent_gen.py --all         # only if a lesson tagged for a role changed
python3 tools/memory_verify.py           # every check must PASS; fix what fails before reporting
```

Tier 0 has a budget: `bash .claude/hooks/load-persona.sh check 8`. If a new lesson pushes it over, set `tier0: false`
on a weaker lesson (it stays searchable) rather than adding a part.

## 6. Prepare the memory change for review

Memory reaches the rest of the team only when merged. Offer the member a branch + commit containing ONLY memory/,
agent/ and .claude/agent-memory/ changes - never commit unasked - and a PR description listing each memory created /
updated / superseded / deleted and why. Changes to `agent/`, `.claude/` or `tools/` need a CODEOWNERS review.

## 7. Report

Tell the member, concisely: the log path; memories and lessons created / updated / superseded / deleted, by slug;
whether role agents were regenerated; `memory_verify` result; anything deliberately NOT saved, and why. Then stop.
