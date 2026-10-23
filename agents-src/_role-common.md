## Common to every role (role-common v1 - from agents-src/_role-common.md)

**Your brief outranks this definition.** The lead gives you the task, the owned files and the facts; the shared agent
core (injected at start) gives the non-negotiables. If something you need is missing, SendMessage the lead - do not
guess.

**Hand-in** (SendMessage the lead, and TaskUpdate your task when you have one): what you did (files, commands); the
evidence (exact commands and their output, file:line); what you did NOT check and why; `LEARNED:` up to 5 bullets.
Label every claim CONFIRMED (observed this session) or PLAUSIBLE (reasoned). Never report "done" for something you did
not run.

**Your role memory** (`.claude/agent-memory/<your role>/MEMORY.md`, committed and shared by the whole team, already in
your context when you run as this role) is for what a FUTURE you in this role needs on this team's work - a recurring
defect class, a check that caught something real, a false alarm and why, a tool trap. One dated line each with its
evidence (read the clock first); merge or delete stale lines; stay under 150 lines. Never write secrets, customer data
or anything sensitive there - it is committed. A lesson true for EVERY role goes to the lead in your hand-in instead
(it belongs in memory/lessons/, where all agents get it).
