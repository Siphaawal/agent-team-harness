// server.mjs - Insight. A zero-dependency local telemetry server for Claude Code.
//
//   node server.mjs [--port 7717] [--repo PATH] [--projects PATH] [--open]
//
// It reads the session transcripts Claude Code already writes under
// ~/.claude/projects/<slug>/<session>.jsonl, folds them into live numbers, and serves:
//   GET /              the dashboard
//   GET /api/state     one snapshot (JSON)
//   GET /api/stream    Server-Sent Events, pushed whenever the active transcript grows
//
// --repo is the agent repo whose lessons and tier-0 loader the panels read (default: two levels up from this
// file). --projects overrides the transcript root (default ~/.claude/projects).
//
// SECURITY: binds 127.0.0.1 only. The transcripts contain everything anyone has typed at Claude Code -
// never expose this port off loopback, and never put it behind a tunnel. No outbound network calls.
// Every /api/* request needs this launch's random token (cookie or Bearer header): see the SECURITY block
// above the server. INSIGHT_TOKEN pins the token and exists for TESTS only.

import http from 'node:http';
import crypto from 'node:crypto';
import fsp from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { parseFile, emptyState, foldLine, foldSubagentLine, subagentLabel, projectLabel } from './lib/parse.mjs';
import { costOf, frictionOf } from './lib/cost.mjs';
import { alertsFor } from './lib/alerts.mjs';
import * as memtier from './lib/memtier.mjs';
import * as team from './lib/team.mjs';
import * as lessons from './lib/lessons.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC = path.join(HERE, 'public');

const args = process.argv.slice(2);
const argOf = (name) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : undefined; };
const USAGE = 'usage: node server.mjs [--port 1024-65535] [--repo PATH] [--projects PATH] [--open]';
const DEFAULT_PORT = 7717;
const PORT_RETRIES = 12;      // when the port is busy, try the next PORT_RETRIES ports before giving up
function portArg() {
  if (!args.includes('--port')) return DEFAULT_PORT;
  const raw = argOf('--port');
  const n = /^\d{1,5}$/.test(String(raw)) ? Number(raw) : NaN;
  if (!(n >= 1024 && n <= 65535)) {
    console.error(`[insight] --port needs a whole number from 1024 to 65535, got ${JSON.stringify(raw)}\n[insight] ${USAGE}`);
    process.exit(64);
  }
  return n;
}
const PORT = portArg();
const OPEN = args.includes('--open');
const REPO = path.resolve(argOf('--repo') || path.join(HERE, '..'));
const PROJECTS = path.resolve(argOf('--projects') || path.join(os.homedir(), '.claude', 'projects'));

lessons.setRepo(REPO);
lessons.summary(0);           // starts the first tool call now, so the first page load is not "loading"
memtier.setRepo(REPO);
team.setRepo(REPO);

const pricing = JSON.parse(await fsp.readFile(path.join(HERE, 'pricing.json'), 'utf8'));
const config = JSON.parse(await fsp.readFile(path.join(HERE, 'config.json'), 'utf8'));

/** The agent's display name: the first `- **Name:** X` line of agent/IDENTITY.md, else "Agent". */
async function agentName() {
  try {
    const text = await fsp.readFile(path.join(REPO, 'agent', 'IDENTITY.md'), 'utf8');
    const m = /^\s*-\s*\*\*Name:\*\*\s*(.+?)\s*$/m.exec(text);
    const name = m && m[1].replace(/[*`]/g, '').trim();
    if (name && !name.includes('{{')) return name.slice(0, 40);   // an unfilled template placeholder is not a name
  } catch { /* no identity file yet */ }
  return 'Agent';
}
let AGENT = await agentName();

// ---------------------------------------------------------------- small shared helpers

const tokenSum = (t) => t.input + t.output + t.cacheRead + t.cacheWrite;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const teamTimings = () => ({
  activeMs: (config.team && config.team.activeMs) || 60000,
  staleMs: (config.team && config.team.staleMs) || 1800000,
});

/* Catch-alls keep one bad transcript line or file from stopping the dashboard, but a bare `catch {}` also
   hides a real bug. Log each site at most once per 10 s so a persistent fault is visible without flooding. */
const lastLogged = new Map();
function logFault(site, err) {
  const now = Date.now();
  if (now - (lastLogged.get(site) || 0) < 10000) return;
  lastLogged.set(site, now);
  console.error(`[insight] ${site}: ${String((err && err.message) || err).slice(0, 200)}`);
}

/** Read `len` bytes of `file` from `start`; the descriptor is closed even when the read throws. */
async function readRange(file, start, len) {
  const fh = await fsp.open(file, 'r');
  try {
    const buf = Buffer.alloc(len);
    const { bytesRead } = await fh.read(buf, 0, len, start);     // the file may have shrunk since stat()
    return buf.subarray(0, bytesRead).toString('utf8');
  } finally { await fh.close(); }
}

// ---------------------------------------------------------------- transcript discovery + tailing

/* Claude Code names a project folder after its cwd with every non-alphanumeric turned into '-'.
   Anything run from the temp folder is not a session to watch - the newest transcript on disk would
   otherwise be a scratch run. */
const slugOf = (p) => String(p || '').replace(/[^A-Za-z0-9]/g, '-');
const IGNORED_SLUGS = new Set([slugOf(os.tmpdir()), ...(config.ignoreProjects || [])].filter(Boolean));

/** Every transcript on disk, newest first. */
async function listTranscripts() {
  const out = [];
  let dirs = [];
  try { dirs = await fsp.readdir(PROJECTS, { withFileTypes: true }); } catch { return out; }
  for (const d of dirs) {
    if (!d.isDirectory()) continue;
    if (IGNORED_SLUGS.has(d.name)) continue;
    const dir = path.join(PROJECTS, d.name);
    let files = [];
    try { files = await fsp.readdir(dir); } catch { continue; }
    for (const f of files) {
      if (!f.endsWith('.jsonl')) continue;
      const full = path.join(dir, f);
      try {
        const st = await fsp.stat(full);
        out.push({ file: full, project: d.name, mtime: st.mtimeMs, size: st.size });
      } catch { /* vanished mid-scan */ }
    }
  }
  return out.sort((a, b) => b.mtime - a.mtime);
}

const live = { state: emptyState(), bytes: 0, file: null, subs: new Map() };
const history = new Map();  // file -> {mtime, size, totals}
let lastPush = 0;

/** Re-read the active transcript from scratch (startup, or when the active session changes). */
async function loadActive(file) {
  const { state, bytes } = await parseFile(file);
  live.state = state; live.bytes = bytes; live.file = file; live.subs = new Map();
  await syncSubagents();
}

/**
 * Fold new lines from the active session's subagent transcripts (<session>/subagents/*.jsonl).
 * The memory librarian does its reading there; without this its tier-1/2 reads never appear.
 * Each line is folded twice: memory/skill activity into the parent (unchanged), and the whole line
 * into the agent's OWN state (lib/team.mjs) for the TEAM panel - tokens, cost, tools, status.
 * The team roster (~/.claude/teams/<team>/config.json) is refreshed on the same pass.
 */
let teamConfig = null;
async function syncSubagents() {
  if (!live.file) return false;
  try { teamConfig = await team.findConfig(live.state.sessionId); } catch { teamConfig = null; }
  const dir = path.join(live.file.replace(/\.jsonl$/, ''), 'subagents');
  let names = [];
  try { names = (await fsp.readdir(dir)).filter((f) => f.endsWith('.jsonl')); } catch { return false; }
  let grew = false;
  for (const name of names) {
    const file = path.join(dir, name);
    let st;
    try { st = await fsp.stat(file); } catch { continue; }
    const sub = live.subs.get(file) || { bytes: 0, label: subagentLabel(file), agent: team.newAgent(file) };
    sub.agent.mtime = st.mtimeMs;
    if (st.size <= sub.bytes) { live.subs.set(file, sub); continue; }
    let text;
    try { text = await readRange(file, sub.bytes, st.size - sub.bytes); }
    catch (e) { logFault('subagent read', e); continue; }     // one unreadable agent file must not hide the others
    const lastNl = text.lastIndexOf('\n');
    if (lastNl === -1) { live.subs.set(file, sub); continue; }
    for (const line of text.slice(0, lastNl).split('\n')) {
      if (!line.trim()) continue;
      let d;
      try { d = JSON.parse(line); } catch { continue; }
      try { foldSubagentLine(live.state, d, sub.label); } catch (e) { logFault('fold subagent line', e); }
      try { team.foldAgentLine(sub.agent, d); } catch (e) { logFault('fold agent line', e); }
    }
    sub.bytes += Buffer.byteLength(text.slice(0, lastNl + 1), 'utf8');
    live.subs.set(file, sub);
    grew = true;
  }
  return grew;
}

/** Read only the bytes appended since last time. */
async function tailActive() {
  if (!live.file) return false;
  let st;
  try { st = await fsp.stat(live.file); } catch { return false; }
  if (st.size === live.bytes) return false;
  if (st.size < live.bytes) { await loadActive(live.file); return true; }   // truncated/rotated
  const text = await readRange(live.file, live.bytes, st.size - live.bytes);
  const lastNl = text.lastIndexOf('\n');
  if (lastNl === -1) return false;                 // a partial line; wait for the rest
  for (const line of text.slice(0, lastNl).split('\n')) {
    if (!line.trim()) continue;
    let d;
    try { d = JSON.parse(line); } catch { continue; }          // a junk or half-written line
    try { foldLine(live.state, d); } catch (e) { logFault('fold line', e); }
  }
  live.bytes += Buffer.byteLength(text.slice(0, lastNl + 1), 'utf8');
  return true;
}

/** Totals for older sessions, so "today" and "this week" are real numbers. Cached by size+mtime. */
async function summarise(entry) {
  const cached = history.get(entry.file);
  if (cached && cached.mtime === entry.mtime && cached.size === entry.size) return cached.totals;
  const { state } = await parseFile(entry.file);
  const totals = {
    file: entry.file, project: entry.project, mtime: entry.mtime,
    startedAt: state.startedAt, lastActivityAt: state.lastActivityAt,
    title: state.title, model: state.model, turns: state.turns,
    firstPrompt: (state.prompts[0] && state.prompts[0].text) || '',   // `title` is the ai-title record, absent on a one-shot
    tokens: state.tokens, byModel: state.byModel, errors: state.errorCount,
    usd: Object.entries(state.byModel).reduce(
      (sum, [m, t]) => sum + costOf(pricing, m, { input: t.input, output: t.output, cacheRead: t.cacheRead, cacheWrite: t.cacheWrite }), 0),
  };
  history.set(entry.file, { mtime: entry.mtime, size: entry.size, totals });
  return totals;
}

// ------------------------------------------------------------------------------- the state object


function buildState(all) {
  const out = buildStateInner(all);
  out.alerts = alertsFor(out, config);
  return out;
}

function buildStateInner(all) {
  const s = live.state;
  const byModel = Object.entries(s.byModel).map(([model, t]) => ({
    model, calls: t.calls,
    tokens: tokenSum(t),
    usd: costOf(pricing, model, t),
  })).sort((a, b) => b.usd - a.usd);

  const sessionUSD = byModel.reduce((a, b) => a + b.usd, 0);

  const prompts = s.prompts.slice(-config.promptsShown).map((p) => ({
    n: p.n, at: p.at, text: p.text, model: p.model, toolCalls: p.toolCalls,
    tokens: p.tokens,
    total: tokenSum(p.tokens),
    usd: costOf(pricing, p.model, p.tokens),
    durationMs: p.endedAt && p.at ? p.endedAt - p.at : null,
  })).reverse();

  const folders = Object.entries(s.folders).sort((a, b) => b[1] - a[1]).slice(0, 8)
    .map(([dir, hits]) => ({ dir, hits }));

  const tools = Object.entries(s.toolCounts).sort((a, b) => b[1] - a[1])
    .map(([name, count]) => ({ name, count }));

  const dayStart = new Date(); dayStart.setHours(0, 0, 0, 0);
  const weekStart = Date.now() - 7 * 864e5;
  const inDay = all.filter((t) => t.lastActivityAt >= dayStart.getTime());
  const inWeek = all.filter((t) => t.lastActivityAt >= weekStart);
  const sum = (xs, f) => xs.reduce((a, b) => a + f(b), 0);
  const tokTotal = (t) => tokenSum(t.tokens);

  const budget = config.budget;
  const spent = budget.window === 'day' ? sum(inDay, (t) => t.usd) : sum(inWeek, (t) => t.usd);

  return {
    now: Date.now(),
    agent: { name: AGENT },
    source: { file: s.file, projects: PROJECTS },
    session: {
      id: s.sessionId, project: s.project, projectPath: projectLabel(s.project),
      cwd: s.cwd, gitBranch: s.gitBranch, version: s.version, title: s.title,
      model: s.model, effort: s.effort, permissionMode: s.permissionMode,
      startedAt: s.startedAt, lastActivityAt: s.lastActivityAt,
      turns: s.turns, apiCalls: s.apiCalls,
      idleMs: s.lastActivityAt ? Date.now() - s.lastActivityAt : null,
      contextTokens: s.contextTokens,
      contextLimit: config.contextLimit,
      contextSource: s.contextSource, compactedAt: s.compactedAt, compactions: s.compactions,
    },
    tokens: { ...s.tokens, total: tokTotal(s) },
    cost: { sessionUSD, byModel, prompts },
    tools: { counts: tools, recent: s.recentTools.slice(-config.toolsShown).reverse() },
    skills: {
      invoked: [...s.skills].sort((a, b) => (a.at || 0) - (b.at || 0)).slice(-12).reverse(),
      invokeCount: s.skills.length,
      invokeByScope: s.skills.reduce((acc, k) => { acc[k.scope] = (acc[k.scope] || 0) + 1; return acc; }, {}),
      fileTouches: s.memory.skillTouches.total,
      touchByScope: s.memory.skillTouches.byScope,
      touchByAgent: s.memory.skillTouches.byAgent || {},
      touched: Object.values(s.memory.skillTouches.bySkill)
        .sort((a, b) => (b.lastAt || 0) - (a.lastAt || 0)).slice(0, 8),
      toolCalls: Object.values(s.toolCounts).reduce((a, b) => a + b, 0),
    },
    files: { recent: s.files.slice(-config.filesShown).reverse(), folders },
    errors: { count: s.errorCount, recent: s.errors.slice(-8).reverse() },
    friction: frictionOf(s),
    // the LESSONS box: newest lessons + their trigger, no bodies; the modal GETs /api/lesson/<slug>
    lessons: (() => { try { return lessons.summary(12); } catch (e) { return { error: String(e.message || e).slice(0, 120), recent: [] }; } })(),
    memory: (() => {
      // subagent transcripts fold in after the parent's lines, so order by time before slicing
      const recent = [...s.memory.recent].sort((a, b) => (a.at || 0) - (b.at || 0));
      return {
        tiers: [0, 1, 2, 3].map((n) => {
          const t = s.memory.tiers[n];
          return {
            tier: n, total: t.total, load: t.load || 0, read: t.read || 0, write: t.write || 0, search: t.search || 0,
            lastAt: t.lastAt,
            byAgent: t.byAgent || {},   // agent name -> hits of this tier (the rest is the lead's own)
            top: Object.entries(t.files).sort((a, b) => b[1] - a[1]).slice(0, 5).map(([file, n2]) => ({ file, n: n2 })),
            // the last touches of THIS tier, each with the prompt that caused it; the page's fitTierCards()
            // shows as many WHOLE hits as the card has room for
            recent: recent.filter((r) => r.tier === n).slice(-8).reverse(),
          };
        }),
        recent: recent.slice(-14).reverse(),
        identity: { ...(s.memory.identity || {}), status: memtier.identityStatus(s.memory) },
        notes: memtier.health(s.memory),
      };
    })(),
    team: team.snapshot({
      lead: s, agents: [...live.subs.values()].map((sub) => sub.agent), config: teamConfig,
      pricing, ...teamTimings(),
    }),
    burn: config.burn || null,
    usage: {
      window: budget.window,
      budgetUSD: budget.usd,
      spentUSD: spent,
      pct: budget.usd > 0 ? Math.min(100, (spent / budget.usd) * 100) : 0,
      session: { usd: sessionUSD, tokens: tokTotal(s) },
      today: { usd: sum(inDay, (t) => t.usd), tokens: sum(inDay, tokTotal), sessions: inDay.length },
      week: { usd: sum(inWeek, (t) => t.usd), tokens: sum(inWeek, tokTotal), sessions: inWeek.length },
      note: 'Local budget from config.json - not a reading of the account plan limit.',
      costModel: config._costModel,
    },
    sessions: all.slice(0, 8).map((t) => ({
      project: t.project, projectPath: projectLabel(t.project), title: t.title,
      at: t.lastActivityAt, turns: t.turns, usd: t.usd, tokens: tokTotal(t),
      active: t.file === live.file,
    })),
    pricingNote: pricing.note,
    tier0: memtier.tierZero(),
  };
}

// ------------------------------------------------------------------------------------ the polling

let cachedAll = [];
let refreshing = false;
let ticking = false;        // the fast loop is mid-run
let swapping = false;       // refreshAll is replacing live.state

/** Replace the live session. Waits for an in-flight tick first: a tick that read bytes of the OLD file would
 *  otherwise fold them into the NEW state and advance its offset (lines folded twice or from the wrong file). */
async function swapActive(file) {
  swapping = true;                      // new ticks skip from here on
  try {
    while (ticking) await sleep(25);
    await loadActive(file);
  } finally { swapping = false; }
}
let dirty = false;          // the live transcript grew since the last push

async function refreshAll() {
  if (refreshing) return;
  refreshing = true;
  try {
    AGENT = await agentName();   // picks up tools/init_agent.py filling the template without a restart
    const list = await listTranscripts();
    const recent = list.filter((e) => e.mtime >= Date.now() - config.historyDays * 864e5)
      .slice(0, config.maxSessionsParsed);
    const out = [];
    for (const e of recent) {
      try {
        const t = await summarise(e);
        out.push(t);
      } catch (err) { logFault('summarise ' + path.basename(e.file), err); }
    }
    const newest = out.sort((a, b) => b.mtime - a.mtime)[0];
    if (newest && newest.file !== live.file) await swapActive(newest.file);
    cachedAll = out.sort((a, b) => b.lastActivityAt - a.lastActivityAt);
  } finally { refreshing = false; }
}

const clients = new Set();
function push() {
  if (!clients.size) return;
  const payload = `data: ${JSON.stringify(buildState(cachedAll))}\n\n`;
  for (const res of clients) { try { res.write(payload); } catch { clients.delete(res); } }
  lastPush = Date.now();
}

await refreshAll();

// Fast loop: tail the live file. Slow loop: re-scan every project for a new session.
setInterval(async () => {
  if (ticking || swapping) return;      // never overlap with itself or with a session swap
  ticking = true;
  try {
    const grew = (await tailActive()) | (await syncSubagents());
    if (grew) {
      const i = cachedAll.findIndex((t) => t.file === live.file);
      if (i >= 0) {
        // the 20 s rescan re-parses the file; between rescans the live fold is the fresher number,
        // so TODAY and 7 DAYS move with the ledger instead of stepping every rescan
        const s = live.state;
        cachedAll[i] = {
          ...cachedAll[i], lastActivityAt: s.lastActivityAt, tokens: s.tokens, turns: s.turns,
          usd: Object.entries(s.byModel).reduce((sum, [m, t]) => sum + costOf(pricing, m, t), 0),
        };
      }
      dirty = true;
    }
    // CPU / bandwidth: a push is ~160 KB of JSON and a full re-render in the tab, and a busy tool loop can
    // grow the transcript more than once a second. Coalesce to one push per 1.5 s.
    if (dirty && Date.now() - lastPush >= (config.minPushMs || 1500)) { dirty = false; push(); }
    else if (!dirty && Date.now() - lastPush > 5000) push();   // keep clocks and idle timers moving
  } catch (e) { logFault('poll', e); }
  finally { ticking = false; }
}, config.pollMs);

setInterval(() => { refreshAll().then(push).catch((e) => logFault('rescan', e)); }, config.rescanMs);

// ------------------------------------------------------------------------------------- the server

const MIME = {
  '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8', '.png': 'image/png', '.jpg': 'image/jpeg',
  '.webp': 'image/webp', '.svg': 'image/svg+xml', '.woff2': 'font/woff2',
};

/* SECURITY: binding to 127.0.0.1 is NOT enough. With no Host check, any web page could DNS-rebind its own
   name to 127.0.0.1 and read /api/state - prompt text included. And with no Origin check, any page could
   drive the API from the victim's browser. The port is read per request because the EADDRINUSE retry
   below can move it. */
function hostAllowed(host) {
  const port = server.address() && server.address().port;
  return host === `127.0.0.1:${port}` || host === `localhost:${port}`;
}
// Browsers stamp Sec-Fetch-Site on every request and Origin on POST; anything a foreign page triggers
// (img, audio, form POST, fetch) arrives cross-site. No headers = a non-browser client - which is exactly
// what any other local process or user on a shared host looks like, so cross-site alone is NOT access
// control; the token below is.
function crossSite(req) {
  const sfs = req.headers['sec-fetch-site'];
  if (sfs && sfs !== 'same-origin' && sfs !== 'none') return true;
  const origin = req.headers.origin;
  return !!origin && !hostAllowed(origin.replace(/^https?:\/\//, ''));
}

/* AUTH: a random token per launch (never written to a file; printed once on the console as a URL). Opening
   /?token=T sets it as an HttpOnly SameSite=Strict cookie and redirects to / so it leaves the address bar and
   history. /api/* then needs that cookie, or `Authorization: Bearer T` for scripts and curl. Compared with
   timingSafeEqual after a length check (it throws on unequal lengths). INSIGHT_TOKEN pins the value and is
   meant for TESTS ONLY: a fixed token is a password on a shared host. Static files stay public: they hold
   no transcript data. */
const TOKEN = process.env.INSIGHT_TOKEN || crypto.randomBytes(32).toString('hex');
if (process.env.INSIGHT_TOKEN && TOKEN.length < 16) { console.error('[insight] INSIGHT_TOKEN must be at least 16 characters'); process.exit(1); }
const TOKEN_BUF = Buffer.from(TOKEN);
function tokenOk(candidate) {
  if (typeof candidate !== 'string' || !candidate) return false;
  const b = Buffer.from(candidate);
  return b.length === TOKEN_BUF.length && crypto.timingSafeEqual(b, TOKEN_BUF);
}
function cookieToken(req) {
  for (const part of String(req.headers.cookie || '').split(';')) {
    const i = part.indexOf('=');
    if (i > 0 && part.slice(0, i).trim() === 'insight') return part.slice(i + 1).trim();
  }
  return null;
}
function authed(req) {
  const m = /^Bearer (.+)$/.exec(String(req.headers.authorization || ''));
  return tokenOk(cookieToken(req)) || (!!m && tokenOk(m[1].trim()));
}

// On every response, including errors and redirects.
const SEC_HEADERS = {
  'content-security-policy': "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
  'x-frame-options': 'DENY',
  'x-content-type-options': 'nosniff',
  'referrer-policy': 'no-referrer',
};

const server = http.createServer(async (req, res) => {
  for (const [k, v] of Object.entries(SEC_HEADERS)) res.setHeader(k, v);
  let url;
  try { url = new URL(req.url, 'http://localhost'); }
  catch { res.writeHead(400, { 'content-type': 'text/plain' }); return res.end('bad url'); }

  if (!hostAllowed(req.headers.host)) { res.writeHead(403, { 'content-type': 'text/plain' }); return res.end('bad host'); }
  // static files may be linked from anywhere; the API answers only the page itself
  if (url.pathname.startsWith('/api/') && crossSite(req)) {
    res.writeHead(403, { 'content-type': 'text/plain' }); return res.end('cross-site');
  }
  if (url.pathname.startsWith('/api/') && !authed(req)) {
    res.writeHead(401, { 'content-type': 'text/plain', 'cache-control': 'no-store' }); return res.end('token required');
  }
  // the URL printed at start: trade the token for the cookie, then drop it from the address bar
  if (url.pathname === '/' && url.searchParams.has('token')) {
    if (tokenOk(url.searchParams.get('token'))) {
      res.writeHead(302, { 'set-cookie': `insight=${TOKEN}; HttpOnly; SameSite=Strict; Path=/`, location: '/', 'cache-control': 'no-store' });
      return res.end();
    }
    // a wrong token is not an error page: serve the plain page, whose API calls will 401
  }

  if (url.pathname === '/api/state') {
    res.writeHead(200, { 'content-type': MIME['.json'], 'cache-control': 'no-store' });
    return res.end(JSON.stringify(buildState(cachedAll)));
  }

  /* AGENT DETAIL: one agent's live detail for the modal, polled by the page while
     it is open, so /api/state stays lean. Loopback only like everything else. The id is the agent's
     transcript id or the lead's session id - validated to a plain token before it is looked up. */
  const ag = /^\/api\/agent\/([A-Za-z0-9_-]{1,96})$/.exec(url.pathname);
  if (ag) {
    let d = null;
    try {
      d = team.detail({
        lead: live.state, agents: [...live.subs.values()].map((sub) => sub.agent), config: teamConfig, pricing,
        ...teamTimings(),
        contextLimit: config.contextLimit, id: ag[1],
      });
    } catch { d = null; }
    res.writeHead(d ? 200 : 404, { 'content-type': MIME['.json'], 'cache-control': 'no-store' });
    return res.end(JSON.stringify(d || { error: 'no such agent in the live session' }));
  }

  /* LESSON DETAIL: one memory/lessons/<slug>.md in full, for the LESSONS box's modal. The slug is
     validated to a plain token before it is looked up, so it can never name a path. */
  const ls = /^\/api\/lesson\/([a-z0-9-]{1,64})$/.exec(url.pathname);
  if (ls) {
    let d = null;
    try { d = await lessons.one(ls[1]); } catch (e) { logFault('lesson detail', e); d = null; }
    res.writeHead(d ? 200 : 404, { 'content-type': MIME['.json'], 'cache-control': 'no-store' });
    return res.end(JSON.stringify(d || { error: 'no such lesson' }));
  }

  if (url.pathname === '/api/stream') {
    res.writeHead(200, {
      'content-type': 'text/event-stream', 'cache-control': 'no-store',
      connection: 'keep-alive', 'x-accel-buffering': 'no',
    });
    res.write(`data: ${JSON.stringify(buildState(cachedAll))}\n\n`);
    clients.add(res);
    req.on('close', () => clients.delete(res));
    return;
  }

  // static, confined to public/
  let rel;
  try { rel = url.pathname === '/' ? 'index.html' : decodeURIComponent(url.pathname.slice(1)); }
  catch { res.writeHead(400, { 'content-type': 'text/plain' }); return res.end('bad path'); }   // malformed %-escape
  if (rel.includes('\0')) { res.writeHead(400, { 'content-type': 'text/plain' }); return res.end('bad path'); }
  let file = path.join(PUBLIC, rel);
  // PUBLIC + sep, not PUBLIC: a bare prefix also admits a sibling such as public-old/
  if (!file.startsWith(PUBLIC + path.sep)) { res.writeHead(403); return res.end('no'); }
  try {
    const data = await fsp.readFile(file);
    res.writeHead(200, {
      'content-type': MIME[path.extname(file).toLowerCase()] || 'application/octet-stream',
      // no-store, or an edit to app.js/style.css silently does nothing until a hard refresh
      'cache-control': 'no-store, must-revalidate',
    });
    res.end(data);
  } catch {
    res.writeHead(404, { 'content-type': 'text/plain' });
    res.end('not found');
  }
});

// execFile with an argument list - no shell string to quote or inject into
function openBrowser(url) {
  const [exe, argv] = process.platform === 'win32' ? ['cmd', ['/c', 'start', '', url]]
    : process.platform === 'darwin' ? ['open', [url]] : ['xdg-open', [url]];
  import('node:child_process').then((cp) => cp.execFile(exe, argv, () => {}));
}

// ⚠️ Register 'listening' ONCE, outside the retry, and read the port back from server.address().
// A failed listen() does NOT remove the callback it attached, so a per-attempt callback survives and
// fires on the attempt that finally succeeds - printing (and opening) the port that was already busy.
server.on('listening', () => {
  const url = `http://127.0.0.1:${server.address().port}/?token=${TOKEN}`;
  console.log(`[insight] watching ${PROJECTS}`);
  console.log(`[insight] agent repo: ${REPO} (${AGENT})`);
  console.log(`[insight] active transcript: ${live.file ? path.basename(live.file) : '(none yet)'}`);
  console.log(`[insight] open this URL (it carries this launch's token): ${url}`);
  console.log(`[insight] loopback only, Ctrl+C to stop`);
  if (OPEN) openBrowser(url);
});

let triesLeft = PORT_RETRIES;
server.on('error', (err) => {
  if (err.code !== 'EADDRINUSE') { console.error('[insight]', err.message); process.exit(1); }
  const busy = PORT + (PORT_RETRIES - triesLeft);
  if (busy === PORT) {
    console.log(`[insight] port ${PORT} is already in use - probably Insight is already running.`);
    console.log(`[insight] use the token URL that instance printed, or stop the old one with:`);
    console.log(`[insight]   find the old process with:  lsof -i :${PORT}   (or: ss -ltnp | grep ${PORT})`);
  }
  if (--triesLeft <= 0) { console.error(`[insight] no free port near ${PORT}; giving up.`); process.exit(1); }
  const next = busy + 1;
  console.log(`[insight] trying ${next} instead...`);
  setTimeout(() => server.listen(next, '127.0.0.1'), 50);
});

server.listen(PORT, '127.0.0.1');
