# CLAUDE.md

This repo is the team's shared agent: one persona, one memory, used by every member in their own clone. The harness
loads this file for every member and every spawned teammate, so it stays short; the persona itself arrives through
the SessionStart hooks (tier 0).

## Every session

1. **Tier 0 loads automatically** (8 SessionStart hooks run `.claude/hooks/load-persona.sh K 8`). Part 1 carries a
   manifest. If a listed part is missing, shows OVERFLOW, or arrived only as a saved-file preview, identity did NOT
   load - say so and Read `agent/IDENTITY.md`, `agent/SOUL.md`, `agent/TEAM.md` before any work. If NO tier-0 part
   arrived at all, project hooks are probably disabled (org managed settings can block them) - say so.
2. **Dispatch the `memory-librarian`** as soon as the goal is known (`librarian-gate.py` nags until you do; waive a
   genuinely trivial session with a reason).
3. **Invoke the skill that covers the work** before its first tool call (`skills-gate.py` speaks after 25 calls).
4. **Write back with `/wrap`** at the end. Team knowledge goes to `memory/`, merged by PR - never only into one
   member's private auto memory (`~/.claude/projects/...`), which no other member can see.

## Rules the hooks enforce (and why they are hooks)

| Hook | Blocks / does |
|---|---|
| `git-guard.py` | discarding uncommitted work (checkout/restore/stash/reset --hard/clean), killing processes by name |
| `heredoc-guard.py` | heredocs that corrupt file content (Python with backslashes, > 60 lines) - use Write/Edit |
| `stamp-guard.py` | writing a clock time later than the wall clock - read `date` first |
| `agent-model-guard.py` | spawning an agent without `name`, explicit `model`, and a `MODEL-BY-JOB:` line |
| `subagent-core.py` | injects `agents-src/CORE-AGENTS.md` into every spawned agent |
| `session-log.py` | appends an auto skeleton to `memory/log/YYYY-MM/YYYY-MM-DD.<member>.md` at session end |

## Where things live

- `agent/` - persona (tier 0). `memory/topics`, `memory/lessons`, `memory/log` - team memory (tier 2).
- `memory/_gen/` - generated index + triggers (gitignored; rebuilt every session). Never hand-edit.
- `agents-src/` -> `tools/agent_gen.py` -> `.claude/agents/<role>.md` (generated; edit the source).
- `.claude/agent-memory/<role>/MEMORY.md` - each role's shared memory (committed).
- `docs/ARCHITECTURE.md` - how memory works; `docs/SPEC.md` - the build contract.

## Commands

```bash
python3 tools/memory_tools.py search "<terms>"   # find a memory, lesson or skill
python3 tools/memory_verify.py                   # health check - run before every memory PR
python3 tests/run_all.py                         # every test + hook self-test
bash .claude/hooks/load-persona.sh check 8       # tier-0 size per part
```

Never commit or push unless the member in this session asks. Changes to `agent/`, `.claude/`, `tools/` and `insight/`
run on every member's machine - they need a CODEOWNERS review.
