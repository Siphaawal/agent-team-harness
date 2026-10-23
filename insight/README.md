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
public/           app.js graph.js agent.js lesson.js index.html style.css assets/
test/             security-probe.sh, fixture/ (a tiny repo for the tests)
```
