# agent-team-harness

A shared Claude Code agent for a whole team: **one persona, one memory, one set of lessons**, used by every member in
their own clone. What one member's session learns reaches every other member's session once it is merged.

> Diagrams are [Mermaid](https://mermaid.js.org/) and render on GitHub and GitLab. In an editor without Mermaid
> support, each diagram is followed by a short text description.

---

## Contents

1. [What you get](#1-what-you-get)
2. [How it works](#2-how-it-works) - architecture, one session, the knowledge loop, tier 0, agent teams, Insight
3. [Getting started](#3-getting-started) - prerequisites, team setup (once), member setup, first session
4. [Daily use](#4-daily-use)
5. [Writing memory by hand](#5-writing-memory-by-hand)
6. [Security model](#6-security-model)
7. [Troubleshooting](#7-troubleshooting)
8. [Maintenance](#8-maintenance)
9. [Repository layout](#9-repository-layout)
10. [FAQ](#10-faq)

---

## 1. What you get

| Piece | What it does | Where |
|---|---|---|
| **Persona** | The agent's name, principles and how the team works with it, loaded into every session | `agent/` |
| **Team memory** | Durable facts, decisions, runbooks and lessons, one file each, reviewed through PRs | `memory/` |
| **Librarian** | A subagent that reads the memory in its own context and hands the session a 2 KB brief | `.claude/agents/memory-librarian.md` |
| **`/wrap`** | The end-of-session procedure that writes what was learned back to memory | `.claude/skills/wrap/` |
| **`team-lead`** | How to run several agents in parallel without them trampling each other | `.claude/skills/team-lead/` |
| **Role agents** | `reviewer`, `verifier`, `adversary`, each with a shared role memory | `.claude/agents/`, `.claude/agent-memory/` |
| **Guard hooks** | Stop the agent discarding uncommitted work, inventing timestamps, or spawning agents carelessly | `.claude/hooks/` |
| **Insight** | A local dashboard of what Claude Code is doing: tokens, cost, tools, the agent team, lessons | `insight/` |

**Requirements:** Linux, Python 3.10+, git, bash, Claude Code, and Node 18+ for Insight only. **Nothing to install
beyond that:** every tool uses the standard library only, there are no npm or pip dependencies, no vendored binaries,
and nothing makes a network call.

---

## 2. How it works

### 2.1 Architecture

```mermaid
flowchart LR
  subgraph REMOTE["Team git remote"]
    direction TB
    R_AGENT["agent/<br/>persona"]
    R_MEM["memory/<br/>topics, lessons, logs"]
    R_CFG[".claude/<br/>hooks, agents, skills"]
    R_TOOLS["tools/ + insight/"]
  end

  subgraph A["Member A's machine"]
    direction TB
    A_CLONE["clone of the repo"]
    A_CC["Claude Code session"]
    A_PRIV["~/.claude/<br/>transcripts + private auto memory"]
    A_INS["Insight<br/>127.0.0.1 only"]
    A_CLONE -- "hooks + tier 0" --> A_CC
    A_CC -- "writes transcripts" --> A_PRIV
    A_PRIV -- "read locally" --> A_INS
  end

  subgraph B["Member B's machine"]
    direction TB
    B_CLONE["clone of the repo"]
    B_CC["Claude Code session"]
    B_CLONE -- "hooks + tier 0" --> B_CC
  end

  REMOTE -- "git pull" --> A_CLONE
  REMOTE -- "git pull" --> B_CLONE
  A_CLONE -- "PR: memory changes" --> REMOTE
  B_CLONE -- "PR: memory changes" --> REMOTE
```

*In words:* the repo is the single source of the persona, the memory and the hook code. Each member pulls it and runs
Claude Code in their clone; the committed `.claude/settings.json` makes Claude Code run the hooks, which load the
persona and memory index into the session. Anything the session learns goes back as a PR. Transcripts and Claude
Code's own auto memory stay on each member's machine and are never shared.

### 2.2 Inside one session: which hook runs when

**The session, stage by stage:**

```mermaid
flowchart LR
  A["1. SessionStart<br/>load-persona.sh 1..8<br/>tier 0 into context"] --> B["2. Each prompt<br/>UserPromptSubmit:<br/>librarian gate, skills gate"]
  B --> C["3. Work<br/>every tool call goes<br/>through the guards below"]
  C --> D["4. Turn ends<br/>Stop: blocks ONCE if the<br/>librarian was never used"]
  D -->|"next prompt"| B
  D --> E["5. Session ends<br/>SessionEnd / PreCompact:<br/>skeleton in the daily log"]
```

**One tool call:**

```mermaid
flowchart LR
  T["agent wants a tool"] --> G{"PreToolUse guard<br/>for that tool"}
  G -->|"Bash"| G1["git-guard<br/>heredoc-guard"]
  G -->|"Write / Edit"| G2["stamp-guard"]
  G -->|"Agent"| G3["agent-model-guard"]
  G1 & G2 & G3 -->|"allowed"| RUN["tool runs"] --> P["PostToolUse<br/>count the call,<br/>note librarian / skill use"]
  G1 & G2 & G3 -->|"blocked"| WHY["reason shown to the agent,<br/>which retries the safe way"]
  RUN -->|"if it spawned an agent"| S["SubagentStart<br/>inject CORE-AGENTS.md"]
```

*In words:* eight SessionStart hooks deliver tier 0. Every prompt passes two gates that push the agent to fetch a
memory brief and to use the right skill. Every tool call passes a guard for that tool, which blocks known-dangerous
patterns with a reason the agent can act on. Every spawned agent gets the shared core. When the session ends, a hook
leaves a skeleton entry in the member's daily log, so something survives even if nobody runs `/wrap`.

### 2.3 Session lifecycle with the librarian and `/wrap`

```mermaid
sequenceDiagram
  autonumber
  actor M as Member
  participant CC as Claude Code
  participant H as Hooks
  participant L as memory-librarian
  participant FS as memory/ in the clone
  participant G as Git remote

  M->>CC: start claude in the clone
  CC->>H: SessionStart (8 parts)
  H->>FS: read agent/*, compute index + triggers
  H-->>CC: tier 0 (manifest in part 1)
  M->>CC: "add retries to the export job"
  CC->>H: UserPromptSubmit
  H-->>CC: librarian gate: dispatch the librarian first
  CC->>L: goal = add retries to the export job
  L->>FS: search memory, read the top hits only
  L-->>CC: 2 KB brief: hazards, decisions, open threads
  CC->>CC: work, guarded by PreToolUse hooks
  M->>CC: /wrap
  CC->>FS: enrich daily log, write memories + lessons
  CC->>CC: memory_index, agent_gen, memory_verify
  CC-->>M: offers a memory-only branch + PR text
  M->>G: push branch, open PR
  Note over G: a second member reviews and merges
```

### 2.4 The knowledge loop between members

```mermaid
flowchart LR
  A1["Member A's session<br/>hits a trap"] --> W["/wrap writes<br/>memory/lessons/slug.md<br/>with a trigger question"]
  W --> PR["PR, reviewed by a teammate<br/>CODEOWNERS on memory/"]
  PR --> MERGE["merged to main"]
  MERGE --> PULL["Member B: git pull"]
  PULL --> T0["B's next SessionStart:<br/>the trigger is in tier 0,<br/>the memory is in the index"]
  T0 --> B1["B's agent asks the question<br/>before repeating the trap"]
  MERGE --> GEN["tools/agent_gen.py --all<br/>if tagged review / verification / red-team"]
  GEN --> ROLE["reviewer / verifier / adversary<br/>now carry the lesson"]
```

*In words:* a lesson is stored with the **question** that should fire before the mistake. Once merged, that question
is in every member's tier 0, so the next session asks it at the right moment instead of rediscovering the trap. Lessons
tagged for a role are compiled into that role agent.

### 2.5 Tier 0 and why it is split into parts

```mermaid
flowchart LR
  subgraph SRC["Sources"]
    I["agent/IDENTITY.md"]
    S["agent/SOUL.md"]
    T["agent/TEAM.md"]
    TR["TRIGGERS<br/>from memory/lessons"]
    P["agent/PROJECTS.md"]
    RO["agent/ROUTING.md"]
    SE["SESSION<br/>member id, local notes"]
    IX["MEMORY INDEX<br/>from memory/"]
  end
  SRC --> PAY["one payload<br/>IDENTITY first"]
  PAY --> PK["tier0-pack.py<br/>deterministic split"]
  PK --> P1["part 1: manifest + first sections"]
  PK --> P2["part 2"]
  PK --> PN["... part 8<br/>spare parts print 'empty'"]
  P1 & P2 & PN --> CTX[["session context"]]
```

Claude Code injects one hook's output only up to 10,000 characters; anything larger is replaced by a short preview of
a saved file, and the agent would silently run without its identity. So tier 0 is split into up to 8 parts of at most
9,000 bytes, one hook each. Part 1 lists what every part holds, so a missing part is noticed and said out loud.
Measure it any time: `bash .claude/hooks/load-persona.sh check 8`.

### 2.6 Agent teams inside one session

```mermaid
flowchart TD
  LEAD["Lead session<br/>one member's Claude Code"] -->|"Agent + name + model<br/>+ MODEL-BY-JOB line"| GUARD{"agent-model-guard"}
  GUARD -->|"allowed"| B1["builder-a<br/>owns files X"]
  GUARD -->|"allowed"| B2["builder-b<br/>owns files Y"]
  GUARD -->|"allowed"| RV["reviewer<br/>gets spec + diff only"]
  GUARD -->|"allowed"| VF["verifier<br/>runs the real thing"]
  GUARD -->|"missing / wrong"| NO["blocked with the policy table"]
  CORE["agents-src/CORE-AGENTS.md"] -.->|"SubagentStart"| B1 & B2 & RV & VF
  RMEM[".claude/agent-memory/role/MEMORY.md<br/>shared role memory"] -.-> RV & VF
  B1 & B2 & RV & VF -->|"SendMessage + TaskUpdate"| LEAD
```

Teams run **inside one member's session** (Claude Code's experimental agent-teams feature, enabled in
`.claude/settings.json`). There is no live channel between two members' sessions; members share knowledge through
merged memory and coordinate through your usual PRs and task board.

### 2.7 Insight data flow and its trust boundary

```mermaid
flowchart LR
  TX["~/.claude/projects/*.jsonl<br/>your transcripts"] --> SRV["insight/server.mjs<br/>binds 127.0.0.1"]
  REPO["the repo: memory/lessons,<br/>load-persona.sh check"] --> SRV
  SRV --> CHK{"Host is 127.0.0.1 or localhost?<br/>API request same-origin<br/>and carrying this launch's token?"}
  CHK -->|"yes"| BR["your browser tab"]
  CHK -->|"no: 403 / 401"| X["any other web page, DNS rebinding,<br/>CSRF, another local process or user"]
```

Transcripts contain everything typed into Claude Code, including pasted secrets. Insight serves them only to a page it
served itself: requests with any other `Host`, cross-site API requests and path traversal are refused, and it never
makes an outbound call. Loopback is not a boundary on a shared Linux host (any local process or user can connect), so
every `/api/*` request also needs a random per-launch token. The server prints a URL ending in `?token=...` at start;
opening it stores the token in an HttpOnly cookie and redirects, scripts can send `Authorization: Bearer <token>`. The
token is never written to a file, and a restart makes a new one.

---

## 3. Getting started

### 3.1 Prerequisites (every member)

| Need | Check | Notes |
|---|---|---|
| Linux shell with bash | `bash --version` | macOS works for everything except the Linux-specific tests |
| Python 3.10 or newer | `python3 --version` | standard library only |
| git | `git --version` | your `user.name` becomes your member id |
| Claude Code | `claude --version` | install per [the Claude Code docs](https://code.claude.com/docs) or your org's instructions |
| Node 18+ (optional) | `node --version` | only for Insight; its tests are reported as SKIP without it |

If your organisation manages Claude Code centrally, ask the administrator whether **project hooks are allowed**. A
managed setting (`allowManagedHooksOnly` or `disableAllHooks`) switches every hook in this repo off; the agent then has
no persona and no memory, and section 7 shows how to tell.

### 3.2 Team setup (once, by whoever owns the agent)

1. **Create the team repo** from this skeleton (copy it, or use it as a template) and clone it.

2. **Name the agent:**

   ```bash
   python3 tools/init_agent.py --name "Atlas" --team "Platform" --vibe "Concise, careful, opinionated."
   ```

   This fills the `{{...}}` placeholders in `agent/`. Only `IDENTITY.md` and `TEAM.md` carry placeholders, so its
   output lists just those two - the other files are written by hand in step 3. It refuses (exit 2) to run a second
   time; after that, edit the files directly.

3. **Describe the team** - the agent reads these at every session start, so keep them short and factual:

   | File | Write | Budget |
   |---|---|---|
   | `agent/TEAM.md` | members and roles, how the team directs the agent, standing rules | under 4 KB |
   | `agent/PROJECTS.md` | one line per repo or system: where the truth lives | pointers only |
   | `agent/ROUTING.md` | "when the request mentions X, load Y" routes | one line per route |
   | `agent/SOUL.md` | principles - adjust the defaults if your team works differently | |

4. **Set the owners.** Edit `.github/CODEOWNERS`: replace `@your-org/agent-maintainers` and `@your-org/team` with
   real teams. Then protect `main`: require a PR, require review from Code Owners, and disallow force-push.
   **Why:** the hooks run on every member's machine on every tool call, so anyone who can merge to `.claude/`,
   `tools/` or `agent/` can run code on the whole team's machines.

5. **Set your model policy.** Edit `POLICY` in `.claude/hooks/agent-model-guard.py` (and section 1 of the
   `team-lead` skill) to the models your plan provides.

6. **Verify and commit:**

   ```bash
   python3 tests/run_all.py          # every line PASS (Insight lines SKIP without node)
   python3 tools/memory_index.py
   python3 tools/memory_verify.py    # 0 FAIL
   git add -A
   git update-index --chmod=+x .claude/hooks/load-persona.sh insight/start.sh
   git commit -m "Initialise the team agent"
   git push
   ```

### 3.3 Member setup (each person)

1. **Clone and identify yourself:**

   ```bash
   git clone <team repo url> && cd <repo>
   git config user.name "Ada Lovelace"       # member id becomes "ada-lovelace"; or export AGENT_MEMBER=...
   ```

   Your member id is **shared on purpose**: it appears in `author:` fields and in your daily-log file names, so the
   team can see who wrote what. Pick the `user.name` (or `AGENT_MEMBER`) you are happy to have in the repo.
   If the clone comes back empty with "remote HEAD refers to nonexistent ref", the remote's default branch is not
   `main` - ask whoever set it up.

2. **Run the tests on your machine:**

   ```bash
   python3 tests/run_all.py
   ```

   Every line should read PASS. SKIP means a runtime is missing (usually Node, for Insight); FAIL means stop and
   report it.

3. **List the words that must never be committed** - client names, internal project or system names, your real
   e-mail or anything else private. **Not your member id**: that is meant to be in the repo (step 1), and listing it
   would turn every `/wrap` red. This file is gitignored and never leaves your machine; `tools/leak_scan.py` (and
   `memory_verify` check 5) fail if any of these appear in the repo:

   ```bash
   printf '%s\n' 'globex ?corp' 'project ?falcon' > tools/leak_patterns.txt   # one regex per line, case-insensitive
   python3 tools/leak_scan.py                                                 # expect: 0 finding(s)
   ```

   Replace the two examples with your own words - they are placeholders.

4. **Optional - your own working preferences.** `~/.config/agent-team-harness/me.md` (up to 3 KB) is added to *your*
   sessions only - e.g. "I prefer short answers", "I own the billing service". It never enters the repo.

5. **Start Claude Code in the repo:**

   ```bash
   claude
   ```

   If Claude Code asks whether to trust the folder, read `.claude/settings.json` first (it lists every command the
   hooks run), then accept.

6. **Check that the persona loaded.** Ask the agent:

   > Which tier-0 parts did you receive, and what is your name?

   | It says | Meaning |
   |---|---|
   | It lists the manifest with every part present and gives the agent's name | working |
   | "PERSONA NOT INITIALISED" | the team setup step 2 was not done |
   | A part is missing, or it saw only a preview of a saved file | tier 0 is over budget - see section 8 |
   | It has no idea what you mean | hooks did not run - see section 7 |

### 3.4 Your first session, end to end

1. State the goal in one line: *"Add retries to the export job."*
2. The librarian gate asks the agent to dispatch `memory-librarian`; it returns a short brief of what the team already
   knows. (A trivial session - one question, no repo work - is waived automatically.)
3. Work as normal. If a guard blocks something, it says why and what to do instead.
4. Finish with **`/wrap`**. The agent enriches your daily log
   (`memory/log/YYYY-MM/YYYY-MM-DD.<you>.md`), writes or updates memories and lessons, regenerates the index and role
   agents, runs `memory_verify.py`, and offers a branch with only the memory changes.
5. Push the branch and open the PR. A teammate reviews it - **read what the agent wrote before you push**: merged
   memory is loaded into everyone's sessions.

---

## 4. Daily use

| You say or do | What happens |
|---|---|
| start a session | tier 0 loads; the librarian gate asks for a memory brief once the goal is known |
| "where were we?" | the librarian's resume path: newest daily logs, open handoffs, `git status` |
| "review this" | spawns the `reviewer` role (spec + diff, ranked findings) |
| "verify it" / "does it actually work?" | spawns the `verifier` role (runs it, positive and negative controls) |
| "red team this" / "is this conclusion right?" | spawns the `adversary` role |
| "do it in parallel" / "staff it" | the `team-lead` skill: named teammates with owned files + a reviewer |
| `/wrap` | log, memories, lessons, index, health check, memory-only branch |
| `python3 tools/memory_tools.py search "<terms>"` | search the team's memory yourself |

**Insight** (optional): `bash insight/start.sh --open` serves on 127.0.0.1, port 7717 by default - or the next free
port if 7717 is taken - and prints the URL to use, with this launch's token (`http://127.0.0.1:<port>/?token=...`).
`--open` opens that URL for you; otherwise paste the printed URL into your browser.

---

## 5. Writing memory by hand

Memory files are plain markdown with frontmatter; `/wrap` writes them for you, but you can too. Run
`python3 tools/memory_lint.py` after editing.

**A fact, rule or decision** - `memory/topics/export-job-retries.md`:

```markdown
---
name: export-job-retries
description: The export job retries 3 times with exponential backoff (2s, 4s, 8s) and then pages on-call; never retry on HTTP 4xx.
metadata:
  type: decision
  tags: [export-job, reliability]
  status: active
  author: ada-lovelace
  created: 2026-01-15
---

Decided in the 2026-01-15 incident review.

**Why:** retrying 4xx responses duplicated rows in the warehouse.
**How to apply:** any new caller of the export API uses the same retry helper.
```

**A lesson** - `memory/lessons/check-the-step-that-wrote-the-log.md`. The `trigger` is the question that reaches every
member's tier 0:

```markdown
---
name: check-the-step-that-wrote-the-log
description: A failed build chain leaves the previous run's test log in place, so "all tests passed" can be stale.
metadata:
  type: lesson
  tags: [verification, ci]
  status: active
  author: ada-lovelace
  created: 2026-01-20
  trigger: "Did the step that WROTE this log run in this chain?"
---

Chain build steps with `&&` and print each exit code; read the log's timestamp before trusting it.
```

| Field | Values | Meaning |
|---|---|---|
| `type` | rule, decision, project, reference, lesson, runbook | what kind of knowledge |
| `status` | active, superseded, ephemeral | superseded = do not act on it (needs `superseded_by`); ephemeral = flagged stale after 14 days |
| `tags` | lowercase-kebab | `review`, `verification`, `red-team` also feed those role agents |
| `trigger` | a question ending in `?` | lessons only |
| `tier0` | true / false | lessons only; false keeps it searchable but out of tier 0 |

Never write secrets, credentials, customer data or anything private: memory is committed and everyone reads it.

---

## 6. Security model

```mermaid
flowchart TB
  subgraph TRUST["Runs with your user's rights on your machine"]
    HOOKS[".claude/hooks/*<br/>every prompt and tool call"]
    TOOLS["tools/*.py, insight/"]
  end
  subgraph SHARED["Shared, influences every member's agent"]
    MEMF["memory/ + agent/<br/>loaded into every session"]
  end
  subgraph LOCAL["Stays on your machine"]
    TRANS["~/.claude transcripts + auto memory"]
    STATE[".claude/state/ (gitignored)"]
    LP["tools/leak_patterns.txt (gitignored)"]
    ME["~/.config/agent-team-harness/me.md"]
  end
  PR["Pull request + CODEOWNERS review"] --> TRUST
  PR --> SHARED
```

| Risk | Defence |
|---|---|
| A merge to `.claude/`, `tools/` or `insight/` runs code on everyone's machine | CODEOWNERS + required review; treat these paths like a build script |
| A memory file carries instructions aimed at other members' agents (prompt injection) | memory changes go through PR review; read memory diffs as if they were code; the agent treats memory as information, not commands |
| Secrets or personal data committed | `leak_scan.py`: built-in patterns (home paths, emails, private keys, common token shapes) + your local name list; `memory_verify` check 5 |
| The agent discards someone's uncommitted work | `git-guard.py` blocks checkout/restore/stash/reset --hard/clean on a dirty tree |
| Hook state or transcripts leak | `.claude/state/` is gitignored; hooks log shapes and file paths, never prompt text; transcripts never leave `~/.claude` |
| A web page, or another local process or user, reads your transcripts through Insight | Per-launch random token on every `/api/*` request (HttpOnly SameSite=Strict cookie or Bearer header), Host allow-list, cross-site refusal, traversal guard, CSP + `frame-ancestors 'none'`, loopback bind, no outbound calls |
| No dependencies to compromise | stdlib Python, Node built-ins, no package manager, no network calls |

Report a suspected vulnerability to the repo owners privately rather than in a public issue.

---

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| The agent does not know its name or the team | project hooks did not run | run `bash .claude/hooks/load-persona.sh 1 8 </dev/null \| head`; if that works, hooks are disabled for you (managed settings or a declined trust prompt) |
| "PACKER FAILED" banner | `python3` missing or crashed | `python3 --version`; set `AGENT_PYTHON=/path/to/python3` |
| "OVERFLOW" banner, or a part shows as a preview of a saved file | tier 0 is over budget | section 8 - trim `agent/` or set `tier0: false` on weaker lessons |
| "Member in this session: unknown" | no `git config user.name` and no `AGENT_MEMBER` | set one of them |
| The librarian gate nags every prompt | the librarian was never dispatched this session | let the agent dispatch it, or waive with the command it prints |
| `git-guard` blocks a command | it would discard uncommitted work | copy the file aside before editing, restore from the copy; or add `# git-guard-ok` after checking |
| `memory_verify` check 2 WARN "stale" | `memory/_gen/` is older than memory/ | `python3 tools/memory_index.py` (every session also rebuilds it) |
| `memory_verify` check 5 WARN "names were NOT checked" | no `tools/leak_patterns.txt` on this clone | create it (section 3.3 step 3) |
| `memory_verify` check 4 FAIL "DRIFT" | a lesson changed, role agents not rebuilt | `python3 tools/agent_gen.py --all` |
| Insight tests show SKIP | Node is not installed | install Node 18+ if you want Insight |

---

## 8. Maintenance

- **Before every memory PR:** `python3 tools/memory_verify.py` - all PASS (WARN is informational).
- **After a lesson tagged `review` / `verification` / `red-team` changes:** `python3 tools/agent_gen.py --all`, and
  commit the regenerated `.claude/agents/*.md`.
- **Tier-0 budget:** `bash .claude/hooks/load-persona.sh check 8`. When it approaches 8 parts, cut - set `tier0: false`
  on lessons that rarely fire, shorten `agent/` files, supersede stale memories. Adding parts is a last resort: tier 0
  is paid for in every member's every session.
- **Stale memory:** `python3 tools/memory_tools.py stale` lists ephemeral memories past 14 days; `dupes` lists
  near-duplicates to merge.
- **Role agent changes:** edit `agents-src/<role>.src.md`, then `python3 tools/agent_gen.py <role>`. Never edit the
  generated `.claude/agents/<role>.md`.
- **Everything at once:** `python3 tests/run_all.py`.

### Continuous integration

Run these on every PR (any CI system - they need only `python3`, `git`, `bash` and, for Insight, `node` 18+):

```bash
python3 tests/run_all.py --strict        # --strict: a SKIP fails the run
python3 tools/memory_verify.py           # exit 1 on any FAIL
python3 tools/agent_gen.py --check       # exit 1 if a role agent is out of date
python3 tools/leak_scan.py               # exit 1 on any finding
```

`run_all.py` switches to `--strict` by itself when the `CI` environment variable is set, which most CI systems do.
So **a CI image without Node fails** on the Insight suite's SKIP - that is deliberate, since a green run must not
mean "Insight was never tested". Install Node 18+ in the image, or, if your team does not use Insight, replace the
first line with these three and say so in the pipeline:

```bash
python3 tests/run_all.py -k hooks --strict          # the four hook self-tests
python3 tests/run_all.py -k memory_tools --strict   # needs a Linux filesystem: its POSIX cases SKIP on Windows
python3 tests/run_all.py -k test_wiring --strict
```
 CI has no `tools/leak_patterns.txt`, so check 5 there tests only the built-in patterns; members' local
lists catch the names.

---

## 9. Repository layout

```
agent/                 persona - tier 0 (CODEOWNERS)
  IDENTITY.md SOUL.md TEAM.md PROJECTS.md ROUTING.md
memory/                the team's memory - tier 2 (PR review)
  topics/              facts, rules, decisions, runbooks
  lessons/             lessons, each with a trigger question
  log/YYYY-MM/         daily logs, one file per member per day
  _gen/                generated INDEX.md + TRIGGERS.md (gitignored)
agents-src/            role agent sources + CORE-AGENTS.md (the core every spawned agent receives)
.claude/
  settings.json        hooks + env (committed)
  hooks/               load-persona.sh, tier0-pack.py, gates, guards, logs
  agents/              memory-librarian.md + generated roles
  agent-memory/        shared memory per role
  skills/              wrap, team-lead
  state/               per-clone hook state (gitignored)
tools/                 common, memory_index, memory_lint, memory_tools, memory_verify, agent_gen, init_agent, leak_scan
insight/               local dashboard (node, loopback only)
tests/                 run_all.py + suites + fixtures
docs/                  ARCHITECTURE.md (how memory works), SPEC.md (the build contract)
```

---

## 10. FAQ

**Can two members' agents talk to each other live?** No. Agent teams run inside one session, and Claude Code's
cross-session messaging covers only your own sessions. Members share through merged memory.

**Does my private Claude Code memory get shared?** No. Auto memory (`~/.claude/projects/...`) and transcripts stay on
your machine. Only what you commit to `memory/` is shared.

**Why are lessons separate files instead of one list?** Two members appending to one numbered list at the same time
would conflict and collide on numbers. One file per lesson, keyed by name, never conflicts.

**Why is `memory/_gen/` not committed?** It is generated from `memory/`; committing it would make every PR conflict.
Every session rebuilds it.

**Can I use this on Windows?** The tools run under Git Bash with Python, but the team target is Linux and the tests
are written for it.

**How much context does tier 0 use?** Run `bash .claude/hooks/load-persona.sh check 8`; a new team starts at about
7 KB.
