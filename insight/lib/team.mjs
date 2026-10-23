// team.mjs - the TEAM panel: who is working in this session besides the lead, and what each costs.
//
// Two sources, both files Claude Code already writes, nothing invented:
//   ~/.claude/teams/<team>/config.json        the roster (members[]: name, agentType, joinedAt ...),
//                                             matched to the live session by `leadSessionId`
//   <session>/subagents/agent-*.jsonl         one transcript per agent, forked subagents and
//                                             in-process teammates alike, with agent-*.meta.json
//                                             beside it (name, agentType, description)
//
// Each agent transcript is folded with the SAME `foldLine` the main session uses, into its own
// state object, so per-agent tokens, cost, tool counts and context size are computed exactly the way
// the ledger computes them (message.id dedupe included). Nothing here is sampled from the OS: an
// in-process teammate shares the lead's node process, so CPU and memory per agent do not exist.
//
// STATUS is read off the transcript, not guessed from a clock alone:
//   running           the transcript moved within `activeMs` (beats everything: it is working now)
//   pending-followup  a lead->agent SendMessage is NEWER than the agent's last tool call: the agent
//                     has not acted on the follow-up yet
//   handed-in         the agent's latest SendMessage to the lead is newer than its last other tool
//                     call. In-process teammates never call SubagentHandback - they hand in by message
//   done              the agent called `SubagentHandback` and the tool_result came back (forked
//                     subagents). A teammate that merely ends a turn is NOT done
//   idle              none of the above and quiet longer than `activeMs` (waiting on a tool, a
//                     message or a model call - the transcript cannot tell which)
// Precedence: running > pending-followup > handed-in > done > idle. STALE stays a separate flag
// (quiet past staleMs) layered over whichever status won. Only timestamps and recipients are used;
// message text is never read.

import fs from 'node:fs';
import fsp from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { emptyState, foldLine } from './parse.mjs';
import { costOf } from './cost.mjs';

/** An agent whose context window passes this many tokens gets the warning style (row.ctxWarn). */
export const CTX_WARN_TOKENS = 300000;

const TEAMS_DIR = path.join(os.homedir(), '.claude', 'teams');
const USER_AGENTS_DIR = path.join(os.homedir(), '.claude', 'agents');
let REPO_AGENTS_DIR = null;
/** Role definitions are looked up in <repo>/.claude/agents first, then ~/.claude/agents. */
export function setRepo(p) { REPO_AGENTS_DIR = path.join(p, '.claude', 'agents'); defCache.clear(); }

/* ROLE (per-agent memory): which definition an agent was spawned as. A teammate's meta.json
   carries only its NAME as agentType, so the role comes from the lead's own Agent call (parse.mjs
   s.spawns). A forked subagent's meta agentType IS its type. `defined` = <repo>/.claude/agents/<role>.md
   (or ~/.claude/agents/<role>.md) exists (a project role with its own memory), as opposed to a built-in type. It says the role was
   REQUESTED: whether the harness had registered it yet is not in any file (see the team-lead skill). */
const defCache = new Map();
export function roleDefined(role) {
  if (!role) return false;
  const hit = defCache.get(role);
  if (hit && Date.now() - hit.at < 30000) return hit.v;
  const file = role.replace(/[^A-Za-z0-9_.-]/g, '') + '.md';
  const v = [REPO_AGENTS_DIR, USER_AGENTS_DIR].some((d) => d && fs.existsSync(path.join(d, file)));
  defCache.set(role, { v, at: Date.now() });
  return v;
}
function roleOf(a, spawns) {
  const nm = a.meta.name || '';
  const sp = spawns && Object.prototype.hasOwnProperty.call(spawns, nm) ? spawns[nm] : null;
  const role = sp ? (sp.role || 'general-purpose') : (!a.teammate && a.type) ? a.type : null;
  return { role, defined: roleDefined(role) };
}
/** The shared-core delivery for one agent transcript: null = no SubagentStart attachment seen. */
function coreOf(state) {
  const c = state.memory.core;
  return c ? { ok: c.ok, version: c.version, count: c.count, failed: c.failed } : null;
}

// ------------------------------------------------------------------------------------ the roster

const cfg = { sessionId: null, dir: null, mtime: 0, config: null, scannedAt: 0 };

/** The member list without the spawn prompt (kilobytes of private instructions - not telemetry). */
function trimConfig(raw, dir) {
  const members = (raw.members || []).map((m) => ({
    name: m.name || null, agentId: m.agentId || null, agentType: m.agentType || null,
    joinedAt: m.joinedAt || null, backendType: m.backendType || null,
    color: m.color || null, model: m.model || null, cwd: m.cwd || null,
  }));
  return {
    name: raw.name || path.basename(dir), dir,
    createdAt: raw.createdAt || null, leadAgentId: raw.leadAgentId || null,
    leadSessionId: raw.leadSessionId || null, members,
  };
}

/**
 * The team config whose leadSessionId is this session, or null. The teams dir is rescanned every
 * `rescanMs` (one readdir plus a small JSON per team), the matched file re-read when its mtime moves.
 */
export async function findConfig(sessionId, rescanMs = 10000) {
  if (!sessionId) return null;
  const now = Date.now();
  if (cfg.sessionId !== sessionId) { cfg.sessionId = sessionId; cfg.dir = null; cfg.config = null; cfg.scannedAt = 0; }
  if (cfg.dir) {
    try {
      const st = await fsp.stat(path.join(cfg.dir, 'config.json'));
      if (st.mtimeMs !== cfg.mtime) {
        const raw = JSON.parse(await fsp.readFile(path.join(cfg.dir, 'config.json'), 'utf8'));
        if (raw.leadSessionId === sessionId) { cfg.config = trimConfig(raw, cfg.dir); cfg.mtime = st.mtimeMs; }
        else { cfg.dir = null; cfg.config = null; }
      }
      return cfg.config;
    } catch { cfg.dir = null; cfg.config = null; }
  }
  if (now - cfg.scannedAt < rescanMs) return cfg.config;
  cfg.scannedAt = now;
  let dirs = [];
  try { dirs = await fsp.readdir(TEAMS_DIR, { withFileTypes: true }); } catch { return null; }
  for (const d of dirs) {
    if (!d.isDirectory()) continue;
    const dir = path.join(TEAMS_DIR, d.name);
    try {
      const file = path.join(dir, 'config.json');
      const raw = JSON.parse(await fsp.readFile(file, 'utf8'));
      if (raw.leadSessionId !== sessionId) continue;
      cfg.dir = dir; cfg.mtime = (await fsp.stat(file)).mtimeMs; cfg.config = trimConfig(raw, dir);
      return cfg.config;
    } catch { /* not a team dir */ }
  }
  return null;
}

// ------------------------------------------------------------------------------- per-agent folds

/** The meta.json beside a transcript, or {} when the harness wrote none. */
function readMeta(file) {
  try { return JSON.parse(fs.readFileSync(file.replace(/\.jsonl$/, '.meta.json'), 'utf8')); } catch { return {}; }
}

/** A fresh fold for one agent transcript. Fold lines in with `foldAgentLine`. */
export function newAgent(file) {
  const meta = readMeta(file);
  const id = path.basename(file, '.jsonl').replace(/^agent-/, '');
  return {
    file, id, meta,
    // an UNNAMED agent has no name of its own: label it by type plus a short id tail so four
    // general-purpose agents stay four distinct rows. meta.description is the spawn summary
    // (private), so it is deliberately NOT used as a label.
    name: meta.name || `${meta.agentType || meta.subagent_type || 'agent'} ${id.slice(-4)}`,
    named: !!meta.name,
    type: meta.agentType || meta.subagent_type || (meta.taskKind === 'in_process_teammate' ? 'teammate' : null),
    task: null,               // never meta.description - the spawn summary is private
    teammate: meta.taskKind === 'in_process_teammate',
    state: Object.assign(emptyState(), { redactPrompts: true }),
    handback: new Set(),     // tool_use ids of SubagentHandback calls awaiting their result
    doneAt: null,
    mtime: 0,
  };
}

/** Fold one transcript line into the agent's own state, and watch for the handback. */
export function foldAgentLine(agent, d) {
  foldLine(agent.state, d);
  if (d.type === 'assistant') {
    for (const c of d.message?.content || []) {
      if (c && c.type === 'tool_use' && c.name === 'SubagentHandback') agent.handback.add(c.id);
    }
  } else if (d.type === 'user' && agent.handback.size) {
    for (const c of (Array.isArray(d.message?.content) ? d.message.content : [])) {
      if (c && c.type === 'tool_result' && agent.handback.has(c.tool_use_id) && !c.is_error) {
        agent.doneAt = d.timestamp ? Date.parse(d.timestamp) : Date.now();
      }
    }
  }
}

// ------------------------------------------------------------------------------------ the snapshot

const tokTotal = (t) => t.input + t.output + t.cacheRead + t.cacheWrite;

const sumVals = (o) => Object.values(o || {}).reduce((a, b) => a + b, 0);

/**
 * Memory / skill / file breakdown of one state. For an AGENT the state is its own transcript, so
 * every hit is its own. For the LEAD the session state also holds the agents' memory and skill
 * hits (foldSubagentLine tags them `by`), so `ownOnly` subtracts the per-agent tallies to leave
 * what the lead did itself; files and Skill invocations are never folded across, so those are
 * split by the `by` tag directly.
 */
function activityOf(s, ownOnly) {
  const mem = s.memory;
  const tierCount = (n) => { const t = mem.tiers[n]; return ownOnly ? t.total - sumVals(t.byAgent) : t.total; };
  const t0 = tierCount(0), t1 = tierCount(1), t2 = tierCount(2), t3 = tierCount(3);
  const recent = ownOnly ? mem.recent.filter((r) => !r.by) : mem.recent;
  const last = recent.length ? recent[recent.length - 1] : null;
  const touched = Object.values(mem.skillTouches.bySkill).map((r) => {
    const own = ownOnly ? r.read + r.write + r.search - sumVals(r.byAgent) : r.read + r.write + r.search;
    return { skill: r.skill, scope: r.scope, touches: own, lastAt: r.lastAt };
  }).filter((r) => r.touches > 0).sort((a, b) => b.touches - a.touches);
  const invoked = (ownOnly ? s.skills.filter((k) => !k.by) : s.skills).map((k) => ({ at: k.at, skill: k.skill, scope: k.scope }));
  return {
    memory: {
      t0, t1, t2, t3, count: t0 + t1 + t2 + t3,
      last: last ? { at: last.at, tier: last.tier, op: last.op, file: last.file } : null,
    },
    skills: { invoked, touched, touches: touched.reduce((a, r) => a + r.touches, 0) },
    files: { edited: s.fileOps.write, read: s.fileOps.read, searched: s.fileOps.search },
  };
}

function rowOf(s, pricing, now, activeMs, ownOnly = false) {
  const usd = Object.entries(s.byModel).reduce((sum, [m, t]) => sum + costOf(pricing, m, t), 0);
  const tools = Object.entries(s.toolCounts).sort((a, b) => b[1] - a[1]).map(([name, count]) => ({ name, count }));
  const lastTool = s.recentTools[s.recentTools.length - 1] || null;
  const idleMs = s.lastActivityAt ? Math.max(0, now - s.lastActivityAt) : null;
  return {
    ...activityOf(s, ownOnly),
    model: s.model, apiCalls: s.apiCalls,
    startedAt: s.startedAt, lastActivityAt: s.lastActivityAt, idleMs,
    active: idleMs != null && idleMs <= activeMs,
    toolCalls: tools.reduce((a, b) => a + b.count, 0), tools: tools.slice(0, 8),
    lastTool: lastTool ? { at: lastTool.at, name: lastTool.name, detail: lastTool.detail, ok: lastTool.ok } : null,
    tokens: { ...s.tokens, total: tokTotal(s.tokens) }, usd,
    contextTokens: s.contextTokens, errors: s.errorCount,
  };
}

/**
 * The `team` object for /api/state. `lead` is the live session itself; `agents` are the subagent
 * transcripts folded so far, lead-first ordering already applied: running, idle, done.
 */
export function snapshot({ lead, agents, config, pricing, now = Date.now(), activeMs = 60000, staleMs = 1800000 }) {
  const members = new Map((config?.members || []).map((m) => [m.name, m]));
  const leadMember = (config?.members || []).find((m) => m.agentId === config.leadAgentId) || null;
  // `task` is always null: an agent's meta description is its spawn summary,
  // which is private, so it never leaves the server. The field stays so old pages do not break.
  const leadRow = {
    id: lead.sessionId, name: leadMember?.name || 'lead', type: leadMember?.agentType || 'session',
    task: null, lead: true, kind: 'lead', member: leadMember, stale: false, role: 'lead', defined: false, core: null,
    ...rowOf(lead, pricing, now, activeMs, true), doneAt: null,
  };
  leadRow.status = leadRow.active ? 'running' : 'idle';
  leadRow.elapsedMs = lead.startedAt ? now - lead.startedAt : null;

  const leadAliases = new Set(['main', 'lead', 'team-lead', leadRow.name]);
  const norm = (n) => (leadAliases.has(n) ? leadRow.name : n);

  // Row names. A roster name may repeat (a re-spawned teammate: the graph keeps the newest). Any
  // OTHER repeated name is two different agents and gets an id tail, so rows and nodes stay distinct.
  const nameCount = new Map();
  for (const a of agents) if (!members.has(a.name)) nameCount.set(a.name, (nameCount.get(a.name) || 0) + 1);
  const nameOf = new Map(agents.map((a) => [a, !members.has(a.name) && nameCount.get(a.name) > 1 && a.named ? `${a.name} ${a.id.slice(-4)}` : a.name]));

  // lead->agent messages by recipient, newest time (SendMessage `to` is a name)
  const leadSent = new Map();
  for (const m of lead.messages || []) leadSent.set(m.to, Math.max(leadSent.get(m.to) || 0, m.at || 0));

  const rows = agents.map((a) => {
    const nm = nameOf.get(a);
    const r = {
      id: a.id, name: nm, type: a.type, task: null, lead: false, teammate: a.teammate, named: a.named,
      key: members.has(a.name) ? a.name : a.id,     // graph node key: roster names collapse re-spawns, the rest are one node per id
      // the panel's TYPE column: an in-process teammate (meta taskKind) or a forked subagent
      kind: a.teammate ? 'teammate' : 'subagent',
      member: members.get(a.name) || null,
      ...roleOf(a, lead.spawns), core: coreOf(a.state),
      ...rowOf(a.state, pricing, now, activeMs), doneAt: a.doneAt,
    };
    // the file's mtime catches a transcript that is being written but whose last full line is older
    const moved = Math.max(r.lastActivityAt || 0, a.mtime || 0);
    r.idleMs = moved ? Math.max(0, now - moved) : null;
    r.active = r.idleMs != null && r.idleMs <= activeMs;
    // newest tool call of any kind, newest tool call that is not a SendMessage, newest hand-in message
    const calls = a.state.recentTools || [];
    let lastAny = 0, lastOther = 0;
    for (const t of calls) { lastAny = Math.max(lastAny, t.at || 0); if (t.name !== 'SendMessage') lastOther = Math.max(lastOther, t.at || 0); }
    let handedAt = 0;
    for (const m of a.state.messages || []) if (leadAliases.has(m.to)) handedAt = Math.max(handedAt, m.at || 0);
    const followAt = leadSent.get(a.name) || 0;
    r.pendingFollowup = followAt > 0 && followAt > lastAny;
    r.handedIn = handedAt > 0 && handedAt > lastOther;
    r.status = r.active ? 'running' : r.pendingFollowup ? 'pending-followup' : r.handedIn ? 'handed-in' : a.doneAt ? 'done' : 'idle';
    r.ctxWarn = (r.contextTokens || 0) > CTX_WARN_TOKENS;
    r.elapsedMs = r.startedAt ? (a.doneAt || now) - r.startedAt : null;
    // STALE: quiet for longer than team.staleMs - an agent from an earlier run.
    // The graph shrinks it to a dim outer-ring dot and the table folds it into "+N earlier".
    r.stale = r.status !== 'running' && r.idleMs != null && r.idleMs > staleMs;
    return r;
  });
  const rank = { running: 0, 'pending-followup': 1, 'handed-in': 2, idle: 3, done: 4 };
  rows.sort((x, y) => ((x.stale ? 1 : 0) - (y.stale ? 1 : 0)) || (rank[x.status] - rank[y.status]) || ((x.startedAt || 0) - (y.startedAt || 0)));

  const counts = rows.reduce((acc, r) => { acc[r.status] = (acc[r.status] || 0) + 1; return acc; },
    { running: 0, 'pending-followup': 0, 'handed-in': 0, idle: 0, done: 0 });
  leadRow.ctxWarn = (leadRow.contextTokens || 0) > CTX_WARN_TOKENS;

  /* TEAM GRAPH edges: every SendMessage the lead or an agent made, as {from, to, at}.
     parse.mjs keeps only the recipient and the time - message text, summaries and spawn prompts
     never reach this object (the content is private). 'main' and the lead's roster name
     both mean the lead. Newest last, capped at EDGE_CAP. */
  const EDGE_CAP = 60;
  const edges = [];
  for (const m of lead.messages || []) edges.push({ from: leadRow.name, to: norm(m.to), at: m.at });
  for (const a of agents) for (const m of a.state.messages || []) edges.push({ from: nameOf.get(a), to: norm(m.to), at: m.at });
  edges.sort((x, y) => (x.at || 0) - (y.at || 0));

  return {
    config,
    lead: leadRow,
    agents: rows,
    counts,
    edges: edges.slice(-EDGE_CAP),
    staleMs,
    stale: rows.filter((r) => r.stale).length,
    usd: rows.reduce((a, r) => a + r.usd, 0),
    tokens: rows.reduce((a, r) => a + r.tokens.total, 0),
    activeMs,
    note: 'Status is read off each agent transcript: running = the file moved within the window; pending-followup = a lead message newer than the agent\'s last tool call; handed-in = its last SendMessage to the lead is newer than its last other tool call; done = SubagentHandback answered; idle = quiet. In-process teammates share the lead process, so nothing here is sampled from CPU.',
  };
}

// ---------------------------------------------------------------------------- agent detail (modal)

/**
 * GET /api/agent/<id>.
 * One agent's detail, built FIELD BY FIELD - never by spreading a row or a state - so nothing
 * private can ride along: no message body, summary, spawn prompt, task description, user-prompt
 * text or command text. Labels are `actLabel`'s (Bash description, file basename, skill name,
 * pattern, recipient); errors carry the tool and time, never the error output.
 * `id` is the agent id (transcript file id) or the lead's session id. Returns null when unknown.
 */
export function detail({ lead, agents, config, pricing, now = Date.now(), activeMs, staleMs, id, contextLimit }) {
  const snap = snapshot({ lead, agents, config, pricing, now, activeMs, staleMs });
  const isLead = id === snap.lead.id;
  const row = isLead ? snap.lead : snap.agents.find((r) => r.id === id);
  if (!row) return null;
  const state = isLead ? lead : agents.find((a) => a.id === id)?.state;
  if (!state) return null;

  const base = (p) => path.basename(String(p || ''));
  const acts = (state.acts || []).slice(-15).reverse().map((a) => ({ at: a.at, tool: a.name, label: a.label, ok: a.ok }));
  const lastAct = (state.acts || [])[(state.acts || []).length - 1] || null;

  // memory: own hits only (the lead's state also holds the agents' tagged hits)
  const memRecent = (state.memory.recent || []).filter((r) => (isLead ? !r.by : true) && r.file).slice(-6).reverse()
    .map((r) => ({ at: r.at, tier: r.tier, op: r.op, file: base(r.file) }));   // rows with no path are dropped above

  // files: newest first, one entry per path
  const fileList = (op, n) => {
    const seen = new Set(), out = [];
    for (let i = state.files.length - 1; i >= 0 && out.length < n; i--) {
      const f = state.files[i];
      if (f.op !== op || seen.has(f.path)) continue;
      seen.add(f.path);
      out.push({ at: f.at, name: base(f.path), dir: path.dirname(String(f.path)) });
    }
    return out;
  };

  const lastErr = state.errors.length ? state.errors[state.errors.length - 1] : null;
  const mine = (e) => e.from === row.name || e.to === row.name;
  const msgs = snap.edges.filter(mine);

  return {
    id: row.id, name: row.name, lead: !!row.lead, kind: row.kind,
    color: row.member ? row.member.color : null,
    model: (row.member && row.member.model) || row.model || null,
    status: row.status, stale: !!row.stale, active: row.active,
    idleMs: row.idleMs, startedAt: row.startedAt, lastActivityAt: row.lastActivityAt,
    elapsedMs: row.elapsedMs, doneAt: row.doneAt || null,
    now: lastAct ? { at: lastAct.at, tool: lastAct.name, label: lastAct.label, ok: lastAct.ok } : null,
    acts,
    context: { tokens: row.contextTokens || 0, limit: contextLimit || 1000000, warn: !!row.ctxWarn, warnAt: CTX_WARN_TOKENS },
    toolCalls: row.toolCalls, tools: row.tools.slice(0, 6),
    errors: { count: row.errors, last: lastErr ? { at: lastErr.at, tool: lastErr.tool } : null },
    role: row.role || null, defined: !!row.defined, core: row.core || null,
    memory: { t0: row.memory.t0, t1: row.memory.t1, t2: row.memory.t2, t3: row.memory.t3, count: row.memory.count, recent: memRecent },
    skills: {
      invoked: row.skills.invoked.map((k) => ({ at: k.at, skill: k.skill, scope: k.scope })),
      touched: row.skills.touched.map((k) => ({ skill: k.skill, scope: k.scope, touches: k.touches })),
    },
    files: { counts: row.files, edited: fileList('write', 5), read: fileList('read', 5) },
    messages: {
      sent: snap.edges.filter((e) => e.from === row.name).length,
      received: snap.edges.filter((e) => e.to === row.name).length,
      recent: msgs.slice(-6).reverse().map((e) => ({ from: e.from, to: e.to, at: e.at })),
      window: snap.edges.length,
    },
    staleMs: snap.staleMs, activeMs: snap.activeMs, at: now,
  };
}
