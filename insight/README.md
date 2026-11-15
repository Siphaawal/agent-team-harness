# Insight

A local, live telemetry dashboard for what Claude Code is doing in the background: context window, tool
stream, files touched, failures, skills engaged, the agent team (table and graph), which tier of the agent's
memory is being read or written, and the newest lessons. Zero dependencies (Node 18+ built-ins only), no build
step, no network calls.

```
./insight/start.sh                 # or: node insight/server.mjs
# it prints  http://127.0.0.1:7717/?token=<random>  - open THAT url (--open launches it for you)
```

The token is new at every launch and is never written to a file. Opening the URL stores it in an HttpOnly,
SameSite=Strict cookie and redirects to `/`; after that the page works normally. For scripts, send
`Authorization: Bearer <token>`. `INSIGHT_TOKEN=<16+ chars>` pins the token and exists for the tests only: a fixed
token is a password that other users of a shared host can guess or read from your environment.

Options: `--port N` (default 7717; if busy it tries the next 12), `--repo PATH` (the agent repo whose lessons and
tier-0 loader it reads; default is the repo this folder lives in), `--projects PATH` (transcript root; default
`~/.claude/projects`), `--open`.

## What it reads

| Source | Used for |
|---|---|
| `~/.claude/projects/<slug>/<session>.jsonl` | the live session: tokens, tools, files, errors, context, skills, memory-tier hits |
| `<session>/subagents/agent-*.jsonl`, `~/.claude/teams/<team>/config.json` | the TEAM table and graph (role definitions: `<repo>/.claude/agents/<role>.md`, then `~/.claude/agents`) |
| `<repo>/memory/lessons/*.md` | the LESSONS box (frontmatter: `name`, `description`, `metadata.trigger`, `metadata.created`, `metadata.status`, `metadata.tier0`) |
| `bash <repo>/.claude/hooks/load-persona.sh check N` (N = the largest `K N` in `.claude/settings.json`, else 8) | the tier-0 size line (`part K: N bytes FLAG titles`); shows "loader not found" when absent |
| `<repo>/agent/IDENTITY.md` | the dashboard's agent name (first `- **Name:** X` line; fallback "Agent") |
| `<repo>/memory/{topics,lessons}/*.md` via `python3 lib/memgraph.py` | the MEMORY GRAPH (frontmatter + `[[links]]`) |
| the vault dir of `tools/obsidian_vault.py` (`$AGENT_VAULT_DIR` or the default) | whether to show the OBSIDIAN button |

Dollar figures are API-list-equivalent, from `pricing.json` (hand-edited estimates). On a subscription plan they
measure work done, not an invoice.

## Panels

- **Context and budget**: the live context window against `contextLimit`, and a local budget gauge (`config.json`).
- **Alarms** (text only): context high, stuck or struggling, failure burst, budget, persona not in context, no
  skills used, quiet session.
- **Gate**: the three engraved grooves are memory tiers 0 / 1 / 2 (plus the agent tier). Every read, write or
  search of a memory file lights its tier; the SessionStart persona load is counted as a tier-0 `load`.
- **Team**: lead and every agent transcript, with a graph of who messaged whom. Message bodies, spawn prompts and
  task descriptions are never read.
- **Lessons**: newest `memory/lessons/*.md`. Hover for the trigger question, click for the full lesson.
- **Memory graph**: every memory and lesson under `memory/` as a node (lessons gold, topics green, superseded red),
  with three kinds of edge: a `[[slug]]` link (solid), `superseded_by` (dashed red) and shared tags (faint; each
  node keeps its three strongest so a popular tag cannot draw every pair). Hover a node for its type, tags and
  description; click a lesson to read it; click a topic to pin it. The stamp counts what was NOT drawn: memories
  over the 400-node cap, files skipped for an unsafe name, and `[[links]]` to nothing. See "Memory graph" below.

## Memory graph

Built at request time from the repo's `memory/` (nothing generated is committed): `GET /api/memgraph` runs
`python3 lib/memgraph.py --root <repo>`, which parses frontmatter with `tools/common.py` (the one parser every
tool uses), caches the answer for 30 s, and the server re-checks its shape before sending it. A 2D canvas draws it
(`public/memgraph.js`, zero dependencies; a 3D view was considered and left out to keep "no vendored code" true).

Memory arrives through other members' pull requests, so everything in it is treated as untrusted:

- a file whose name is not `[a-z0-9][a-z0-9-]{0,63}.md`, or a symlink, is never opened, only counted;
- a node's id is its FILE slug, never its `name:` value; an edge is kept only between two node ids, so a
  `[[../../etc/passwd]]` link is counted as dangling and never echoed;
- descriptions are capped at 300 characters with control characters escaped, tags must be kebab-case (12 max),
  `type` and `status` must be schema values; at most 400 nodes and 2,400 edges;
- the page renders every memory string with `textContent` (and `fillText` on the canvas), never as HTML.

The endpoint sits behind the same token, Host allow-list and cross-site refusal as every other `/api/*` route.

### OBSIDIAN button

Shown only when `tools/obsidian_vault.py` has set up a vault whose `memory` link resolves to THIS repo's
`memory/`. It is a plain `obsidian://open?vault=<name>` link (with `&file=memory/...` when a node is pinned);
the dashboard never launches anything itself. To read the memory in Obsidian:

```
python3 tools/obsidian_vault.py setup        # the vault, outside the repo, locked down before first launch
python3 tools/obsidian_vault.py check        # exit 1 if any lockdown value drifted, or a theme / CSS snippet appeared
```

`setup` makes the vault in your user data dir (`--vault DIR` to choose), with `memory/` and `agent/` LINKED, not
copied, and writes `.obsidian/` before Obsidian ever opens it: the Sync, Publish and Web viewer core plugins off, no
community plugins, links never rewritten on rename, delete asks first. If Obsidian has no app config yet, it is
created with automatic updates off; an existing one is yours and is left alone (`lock-app` turns updates off in it,
for every vault, when you ask). See `python3 tools/obsidian_vault.py --help`.

**The links are live.** An edit in Obsidian is an edit to the team memory in your working tree; a rename leaves the
file's `name:` frontmatter wrong (`tools/memory_lint.py` reports it) and a delete deletes. Use the vault to read and
to browse; change memory through the normal flow (`/wrap`, a pull request).

### Does it phone home? (a check you can re-run)

Settings can change between Obsidian releases, so do not take the lockdown on trust; measure it on your version.

1. Run a logging proxy on loopback (for example `mitmdump -p 8080`, or any proxy that prints each CONNECT host).
2. Start Obsidian with its traffic sent through it: `obsidian --proxy-server=http://127.0.0.1:8080` (a Chromium
   switch that Electron apps generally honour; if the proxy stays empty, use a system-wide proxy instead), open the
   vault, browse for a few minutes, quit. Record every host it connected to.
3. **Positive control:** set `updateDisabled` to `false` in the app config, repeat, and confirm the proxy now sees
   the update check. A run that records nothing in BOTH cases proves only that the proxy was not in the path.
4. Put `updateDisabled` back (`tools/obsidian_vault.py lock-app`) and run `check`.

Measured once on Obsidian 1.15.0: with updates off, 0 connections; with updates on, 2 (the update endpoints). That is
why `updateDisabled` is the setting `check` insists on. It is one measurement on one version, not a guarantee for
yours. Note that a note's body can embed a remote image, which Obsidian fetches when the note is shown; the lint's
SUSPECT check covers descriptions and triggers, not bodies.

## Security

- Binds `127.0.0.1` only. The transcripts hold everything anyone has typed at Claude Code: never expose the port,
  never put it behind a tunnel.
- Host allow-list (`127.0.0.1:<port>` / `localhost:<port>`) on every request, which defeats DNS rebinding.
- `/api/*` (including `/api/stream`) needs the per-launch token as the cookie or a Bearer header, else 401. A request
  with no browser headers used to count as a trusted local client, which on a shared host is any other process or
  user. The token is compared with `crypto.timingSafeEqual` after a length check. Static files stay public (they hold
  no transcript data).
- `/api/*` also refuses cross-site requests (`Sec-Fetch-Site`, `Origin`); static files stay linkable.
- Every response carries `Content-Security-Policy` (`default-src 'self'`, no inline script or style, `frame-ancestors
  'none'`, `base-uri 'none'`, `form-action 'none'`), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` and
  `Referrer-Policy: no-referrer`. The page has no inline script, style attribute or `on*` handler; keep it that way.
- Static files are confined to `public/` with a `public + path.sep` prefix check; malformed escapes get 400.
- The browser is opened with `execFile` and an argument list, never a shell string.
- `/api/lesson/:slug` accepts only `^[a-z0-9-]{1,64}$`; `/api/agent/:id` only a plain token.
- Probe a running instance with `INSIGHT_TOKEN=<token> bash insight/test/security-probe.sh <port>`; the automated version is
  `tests/test_insight.py`.

## Art

The ring, corner and rivet-bar PNGs in `public/assets/` are rendered by `blender-insight-ui.py`
(`blender -b -P insight/blender-insight-ui.py`). Blender is optional; the PNGs are committed. Blender writes text chunks
(file path, date, camera) into every render and the files are large: strip the metadata and recompress before
committing (`tools/leak_scan.py` flags embedded paths and any file over 512 KB). The committed PNGs were re-saved that way,
pixel-identical.

## Files

```
server.mjs        http server, SSE stream, polling, state assembly
lib/parse.mjs     transcript folding; cost.mjs prices + friction; alerts.mjs alarm rules
lib/team.mjs      team roster, agent detail; memtier.mjs memory tiers + tier-0 loader; lessons.mjs lessons
lib/memgraph.py   memory graph builder (python3, tools/common.py parser); memgraph.mjs runs it, caches, re-checks
public/           app.js graph.js agent.js lesson.js memgraph.js index.html style.css assets/
test/             security-probe.sh, fixture/ (a tiny repo for the tests)
```
