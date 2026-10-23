// memtier.mjs - which tier of the agent's memory is being touched, how often, and why.
//
// The architecture (docs/ARCHITECTURE.md) sorts memory by HOW OFTEN it is needed, not by how important it
// feels:
//
//   TIER 0  agent/*.md (identity, soul, team, projects, routing) + the generated TRIGGERS list.
//           Loaded into EVERY session by the SessionStart hook, so a tool-level "read" of these is rare
//           and means something: someone is CHANGING one.
//   TIER 1  CLAUDE.md and docs/*.md. Zero cost until wanted. Read on demand.
//   TIER 2  memory/** (topics, lessons, log, the generated index) and the per-user auto-memory dirs.
//           Reached by grep or by the memory-librarian subagent, never bulk-loaded.
//   TIER 3  the AGENT tier: .claude/agent-memory/<role>/, the role definitions in .claude/agents/, their
//           sources in agents-src/, and the shared agent core that the SubagentStart hook injects into
//           every spawned agent (recordCoreLoad).
//
// LOADED is not ACCESSED. Tier 0 arrives as a SessionStart hook attachment, never as a tool call, so a
// counter that only folds tool calls shows the most important tier as "untouched" every single session.
// `recordHookLoad` counts it as op 'load' - and records whether the payload actually reached context
// inline, or was persisted to disk by Claude Code with only a 2 KB preview left in context.
//
// SKILLS live in three places and are counted by SCOPE, not by tier:
//   global  ~/.claude/skills/<name>/          what every project sees
//   repo    <project>/.claude/skills/<name>/  scoped to one repo
//   plugin  ~/.claude/plugins/**/skills/<name>/

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFile } from 'node:child_process';

const T0 = ['identity.md', 'soul.md', 'team.md', 'projects.md', 'routing.md'];
const HOME = os.homedir().replace(/\\/g, '/').toLowerCase();

const norm = (p) => {
  const s = String(p).replace(/\\/g, '/').replace(/^~(?=\/)/, HOME).toLowerCase();
  // a relative path (from a cd'd bash command) is matched by its suffix like an absolute one
  return /^([a-z]:)?\//.test(s) ? s : '/' + s.replace(/^\.\//, '');
};

let REPO = process.cwd();
export function setRepo(p) { REPO = p; tier0Cache.at = 0; }

/** Classify a path. Returns {tier, file} or null when it is not memory at all. */
export function classify(p) {
  if (!p) return null;
  const s = norm(p);
  const base = s.split('/').pop();

  // the persona hook's own payload, saved to disk when it is too large to inject inline
  if (/\/\.claude\/projects\/[^/]+\/[^/]+\/tool-results\/hook-[^/]*-stdout\.txt$/.test(s)) {
    return { tier: 0, file: 'hook payload', where: 'persisted hook output' };
  }

  // tier 3, the AGENT tier: each role's own memory, its generated definition, and the sources the
  // generator builds them from. The role name is kept in the label, since every role memory file is
  // called MEMORY.md.
  const am = s.match(/\/\.claude\/agent-memory\/([^/]+)\/(.+)$/);
  if (am) return { tier: 3, file: `${am[1]}/${am[2].split('/').pop()}`, where: 'role memory' };
  if (/\/\.claude\/agents\/[^/]+\.md$/.test(s)) return { tier: 3, file: base, where: 'role definition' };
  if (/\/agents-src\/[^/]+$/.test(s)) return { tier: 3, file: base, where: 'agent sources' };

  // tier 0: the hand-written persona files and the generated trigger list
  if (/\/agent\/[^/]+\.md$/.test(s) && T0.includes(base)) return { tier: 0, file: base, where: 'agent/' };
  if (/\/memory\/_gen\/triggers\.md$/.test(s)) return { tier: 0, file: base, where: 'generated triggers' };

  // tier 2: anything under a memory directory, including the per-user auto-memory ones
  if (/\/\.claude\/projects\/[^/]+\/memory\//.test(s)) return { tier: 2, file: base, where: 'auto memory' };
  if (/\/memory\/[^/]+(?:\/|$)/.test(s) && !/\/\.claude\/projects\//.test(s)) return { tier: 2, file: base, where: 'memory/' };

  // tier 1: project instructions and docs
  if (base === 'claude.md') return { tier: 1, file: 'CLAUDE.md', where: 'repo root' };
  if (/\/docs\/[^/]+\.md$/.test(s)) return { tier: 1, file: base, where: 'docs/' };
  return null;
}

/** Is this path inside a skill? Returns {scope, skill} or null. */
export function classifySkill(p) {
  if (!p) return null;
  const s = norm(p);
  // a real skill dir name - not a shell variable or glob lifted out of a command ($s, *, {a,b})
  const ok = (name) => /^[a-z0-9][\w.-]*$/.test(name) && !/\.md$/.test(name);
  let m = s.match(/\/\.claude\/plugins\/.*\/skills\/([^/]+)/);
  if (m) return ok(m[1]) ? { scope: 'plugin', skill: m[1] } : null;
  m = s.match(/^(.*)\/\.claude\/skills\/([^/]+)/);
  if (m) return ok(m[2]) ? { scope: m[1] === HOME ? 'global' : 'repo', skill: m[2] } : null;
  return null;
}

// Where does an INVOKED skill live? The Skill tool input carries only a name, so look on disk.
// Cached per cwd+name: the answer only changes when a skill is installed or deleted.
const scopeCache = new Map();
export function scopeOfInvocation(name, cwd) {
  if (!name) return 'unknown';
  if (name.includes(':')) return 'plugin';          // plugin:skill, or a dir-prefixed project skill
  const key = `${cwd || ''}|${name}`;
  if (scopeCache.has(key)) return scopeCache.get(key);
  const has = (dir) => { try { return fs.existsSync(path.join(dir, name, 'SKILL.md')); } catch { return false; } };
  const repo = cwd ? has(path.join(cwd, '.claude', 'skills')) : false;
  const global = has(path.join(os.homedir(), '.claude', 'skills'));
  // a skill in both places: Claude Code prefers the project copy inside the repo
  const scope = repo && global ? 'repo+global' : repo ? 'repo' : global ? 'global' : 'built-in';
  scopeCache.set(key, scope);
  return scope;
}

/** Paths hiding inside a shell command - most memory reads are cat/grep, not the Read tool.
 *  Variables assigned in the command are expanded, and a cd becomes the base for relative paths. */
const BARE_TIER = new RegExp('(?<![\\w./\\\\])(' + [...T0, 'claude.md'].map((f) => f.replace('.', '\\.')).join('|') + ')(?![\\w-])', 'gi');
const REL_DIR = /(?<![\w./\\$:-])((?:memory|agent|agents-src|docs|\.claude)[\\/][\w.\-\\/]+)/g;
const joinBase = (base, rel) => base.replace(/[\\/]+$/, '') + '/' + rel.replace(/^\.?[\\/]/, '');
const isAbs = (v) => /^([A-Za-z]:|~|\/)/.test(v);

export function pathsInCommand(cmd) {
  if (!cmd) return [];
  const s = String(cmd);
  const out = [];
  const re = /[A-Za-z]:[\\/][^\s"'|;&)>]+|~[\\/][^\s"'|;&)>]+|(?<![\w.:])\/[\w.\-@/]+|(?:\.{0,2}[\\/])?[\w.\-]+(?:[\\/][\w.\-]+)*\.md/g;
  const m = s.match(re);
  if (m) out.push(...m);

  // variables assigned a path in this same command (bash W=..., PowerShell $f='...')
  const vars = {};
  const asg = /(?:^|[\s;(])\$?([A-Za-z_]\w*)=\s*["']?([^\s"';&|)]+)["']?/g;
  for (let a; (a = asg.exec(s));) vars[a[1]] = a[2];

  // the base directory: an explicit cd
  let base = null;
  const cdm = s.match(/(?:^|[\s;&|(])cd\s+["']?([^\s"';&|)]+)/);
  if (cdm) base = cdm[1];

  for (const [name, v] of Object.entries(vars)) {
    if (isAbs(v)) out.push(v); else if (base && /[\\/]/.test(v)) out.push(joinBase(base, v));
    const use = new RegExp('\\$\\{?' + name + '\\}?([\\\\/][^\\s"\'|;&)>]+)', 'g');
    for (let u; (u = use.exec(s));) out.push(isAbs(v) || !base ? v + u[1] : joinBase(base, v + u[1]));
  }
  if (base) {
    for (let b; (b = BARE_TIER.exec(s));) out.push(joinBase(base, b[1]));
    BARE_TIER.lastIndex = 0;
    for (let r; (r = REL_DIR.exec(s));) out.push(joinBase(base, r[1]));
    REL_DIR.lastIndex = 0;
  }
  return out;
}

const WRITE_TOOLS = new Set(['Write', 'Edit', 'NotebookEdit']);
// the last alternative: a Python open(p, 'w') / 'a' inside a heredoc script is a write too
const WRITE_CMD = /\b(>|>>|tee|Set-Content|Out-File|WriteAllText|cp |copy |mv |Copy-Item|memory_index)\b|open\([^)]*,\s*['"][wa]/i;

function hitTier(mem, h, meta) {
  const t = mem.tiers[h.tier];
  t.total++;
  t[h.op] = (t[h.op] || 0) + 1;
  t.files[h.file] = (t.files[h.file] || 0) + 1;
  t.lastAt = meta.at;
  if (meta.by) t.byAgent[meta.by] = (t.byAgent[meta.by] || 0) + 1;   // per-agent share of this tier
  mem.recent.push({ at: meta.at, tier: h.tier, file: h.file, op: h.op, where: h.where,
    by: meta.by || null, promptN: meta.promptN, why: meta.promptText });
  if (mem.recent.length > 120) mem.recent.shift();
}

function hitSkill(mem, sk, op, meta) {
  const bucket = mem.skillTouches;
  bucket.total++;
  bucket.byScope[sk.scope] = (bucket.byScope[sk.scope] || 0) + 1;
  const k = `${sk.scope}:${sk.skill}`;
  const row = (bucket.bySkill[k] ||= { skill: sk.skill, scope: sk.scope, read: 0, write: 0, search: 0, lastAt: null, by: null, byAgent: {} });
  row[op] = (row[op] || 0) + 1;
  row.lastAt = meta.at;
  // `by` is the LAST toucher; `byAgent` keeps every agent's count, so four teammates reading the
  // same SKILL.md are all credited, not only the one that read it most recently
  if (meta.by) { row.by = meta.by; row.byAgent[meta.by] = (row.byAgent[meta.by] || 0) + 1; }
  if (meta.by) bucket.byAgent[meta.by] = (bucket.byAgent[meta.by] || 0) + 1;
}

/**
 * Fold one tool call into the memory tracker.
 * `promptN` attributes the access to the prompt that caused it - that is the "why".
 * `by` names a subagent when the call came from one (the librarian reads in its own transcript).
 */
export function record(mem, { at, tool, input, promptN, promptText, by }) {
  const meta = { at, promptN, promptText, by };
  const hits = [];
  const skills = [];
  const add = (p, op) => {
    const c = classify(p);
    if (c) hits.push({ ...c, op, where: by ? `${c.where} · ${by}` : c.where });
    const sk = classifySkill(p);
    if (sk) skills.push({ sk, op });
  };

  if (WRITE_TOOLS.has(tool) || tool === 'Read' || tool === 'Glob' || tool === 'Grep') {
    const p = input?.file_path || input?.path || input?.notebook_path;
    const op = WRITE_TOOLS.has(tool) ? 'write' : tool === 'Read' ? 'read' : 'search';
    if (p) add(p, op);
  }
  if (tool === 'Bash' || tool === 'PowerShell') {
    const cmd = input?.command || '';
    const op = WRITE_CMD.test(cmd) ? 'write' : 'read';
    const seen = new Set();
    for (const p of pathsInCommand(cmd)) {
      const c = classify(p), sk = classifySkill(p);
      const k = (c ? c.tier + c.file : '') + '|' + (sk ? sk.scope + sk.skill : '');
      if (seen.has(k)) continue;
      seen.add(k);
      if (c) hits.push({ ...c, op, where: by ? `${c.where} · ${by}` : c.where });
      if (sk) skills.push({ sk, op });
    }
  }
  if (tool === 'Agent' && /librarian/i.test(JSON.stringify(input || {}))) {
    hits.push({ tier: 2, file: 'memory-librarian', where: 'subagent', op: 'search' });
  }

  for (const h of hits) {
    hitTier(mem, h, meta);
    // reading the persisted identity payload is what actually puts tier 0 into context
    if (h.file === 'hook payload' && h.op === 'read' && mem.identity && !by) mem.identity.recoveredAt = at;
  }
  for (const { sk, op } of skills) hitSkill(mem, sk, op, meta);
  return hits.length + skills.length;
}

/**
 * Fold a SessionStart hook attachment. Only the persona loader (.claude/hooks/load-persona.sh) counts as a
 * tier-0 load: every part starts with "# AGENT TIER 0 - part k/N" and carries a "===== SECTION - ..." banner per section.
 * `readPersisted(path)` returns the saved payload text (or null) - the transcript itself keeps only
 * the 2 KB preview, so section checks against the transcript would always say "missing".
 */
const PERSONA_MARK = /AGENT TIER 0\b[^\n]{0,20}?part (\d+)\/(\d+)/i;
const hasSection = (text, name) => new RegExp('(?:^|\\n)\\s*(?:=+|#+)\\s*' + name + '\\b', 'i').test(text);

export function recordHookLoad(mem, { at, content, readPersisted }) {
  const text = String(content || '');
  const persisted = text.startsWith('<persisted-output>');
  let full = text;
  if (persisted && readPersisted) {
    const m = text.match(/Full output saved to: (.+?stdout\.txt)/);
    if (m) full = readPersisted(m[1].trim()) || text;
  }
  if (!PERSONA_MARK.test(full)) return false;

  const sizeKB = persisted
    ? parseFloat((text.match(/Output too large \(([\d.]+)KB\)/) || [])[1]) || null
    : +(Buffer.byteLength(text, 'utf8') / 1024).toFixed(1);

  // Tier 0 arrives as N parts ("part k/N"), in any order. A single-part payload is one
  // part of one. Sections are judged across the union of parts once every attachment has landed.
  const pm = full.match(PERSONA_MARK);
  const k = pm ? +pm[1] : 1, n = pm ? +pm[2] : 1;
  const id = (mem.identity ||= { at, expected: n, parts: {}, inContext: '', produced: '', recoveredAt: null });
  id.expected = Math.max(id.expected, n);
  id.parts[k] = { persisted, sizeKB };
  if (!persisted) id.inContext += text;
  id.produced += full;
  const sections = ['SOUL', 'TEAM', 'TRIGGERS', 'PROJECTS'];
  const seen = Object.values(id.parts);
  id.persisted = seen.some((p) => p.persisted);
  id.sizeKB = +seen.reduce((a, p) => a + (p.sizeKB || 0), 0).toFixed(1);
  id.partsSeen = seen.length;
  // what the MODEL received: inline parts only (a persisted part contributes a 2 KB preview)
  id.missingInContext = sections.filter((s) => !hasSection(id.inContext, s));
  // what the hook PRODUCED: tells a hook-side truncation apart from a Claude Code-side one
  id.missingInPayload = sections.filter((s) => !hasSection(id.produced, s));
  id.overflow = /!! OVERFLOW/.test(full) || id.overflow || false;

  const t = mem.tiers[0];
  t.total++; t.load = (t.load || 0) + 1; t.lastAt = at;
  t.files['hook payload'] = (t.files['hook payload'] || 0) + 1;
  mem.recent.push({ at, tier: 0, file: n > 1 ? `hook part ${k}/${n}` : 'hook payload', op: 'load',
    where: persisted ? `persisted - ${sizeKB} KB, preview only` : `inline - ${sizeKB} KB`,
    promptN: 0, why: 'SessionStart hook' });
  return true;
}

/**
 * Fold a SubagentStart hook attachment: the shared agent core (agents-src/CORE-AGENTS.md) injected by
 * .claude/hooks/subagent-core.py. It is the agent's equivalent of tier 0 - it arrives as a hook,
 * never as a tool call - so it is counted as a tier-3 'load'. `ok` = exit 0 AND the core's version
 * stamp in the hook's stdout; a resumed agent fires the hook again, so `count` can exceed 1.
 */
export function recordCoreLoad(mem, { at, stdout, exitCode }) {
  const out = String(stdout || '');
  const m = out.match(/agent-core v(\d+)/);
  const ok = String(exitCode) === '0' && !!m;
  const core = (mem.core ||= { at, count: 0, ok: false, version: null, failed: 0 });
  core.count++;
  core.lastAt = at;
  if (ok) { core.ok = true; core.version = +m[1]; } else core.failed++;
  const t = mem.tiers[3];
  t.total++; t.load = (t.load || 0) + 1; t.lastAt = at;
  t.files['agent core'] = (t.files['agent core'] || 0) + 1;
  mem.recent.push({ at, tier: 3, file: m ? `agent core v${m[1]}` : 'agent core', op: 'load',
    where: ok ? 'SubagentStart hook' : `SubagentStart hook FAILED (exit ${exitCode})`, promptN: 0, why: 'SubagentStart hook' });
  if (mem.recent.length > 120) mem.recent.shift();
  return ok;
}

export function empty() {
  const tier = () => ({ total: 0, load: 0, read: 0, write: 0, search: 0, files: {}, lastAt: null, byAgent: {} });
  return {
    tiers: { 0: tier(), 1: tier(), 2: tier(), 3: tier() },
    recent: [],
    core: null,         // agent transcripts only: {count, ok, version, failed, at, lastAt} - see recordCoreLoad
    identity: null,     // {expected, parts:{k:{persisted,sizeKB}}, partsSeen, persisted, sizeKB, missingInContext, missingInPayload, overflow, recoveredAt}
    skillTouches: { total: 0, byScope: {}, bySkill: {}, byAgent: {} },
  };
}

/** Is identity actually in context right now? */
export function identityStatus(mem) {
  const id = mem.identity;
  if (!id) return { level: 'critical', text: 'persona hook did not load this session' };
  if (id.overflow) return { level: 'critical', text: 'tier 0 OVERFLOW - more parts needed than hooks configured' };
  if (id.partsSeen < id.expected) {
    return { level: 'critical', text: `only ${id.partsSeen} of ${id.expected} identity parts arrived` };
  }
  if (id.missingInPayload.length) {
    return { level: 'critical', text: `hook payload itself is missing ${id.missingInPayload.join('/')}` };
  }
  if (id.persisted && !id.recoveredAt) {
    return { level: 'critical', text: `identity ${id.sizeKB} KB was persisted to disk - only a 2 KB preview is in context (${id.missingInContext.join('/')} absent)` };
  }
  if (id.persisted) return { level: 'warn', text: `identity ${id.sizeKB} KB persisted, recovered by reading the saved payload` };
  return { level: 'ok', text: `identity inline, ${id.expected > 1 ? id.expected + ' parts, ' : ''}${id.sizeKB} KB` };
}

/** The headline reading: tier 0 loads every session but should also be WRITTEN sometimes. */
export function health(mem) {
  const t0 = mem.tiers[0], t1 = mem.tiers[1], t2 = mem.tiers[2];
  const notes = [];
  const id = identityStatus(mem);
  if (id.level !== 'ok') notes.push(id.text);
  if (t0.write === 0 && (t1.write > 0 || t2.write > 0)) {
    notes.push('tier 0 not written this session - the persona and team files were not updated');
  }
  if (t2.total === 0 && t1.total + t0.total > 0) {
    notes.push('tier 2 never consulted - the memory-librarian was not used');
  }
  if (t0.total + t1.total + t2.total === 0) notes.push('memory untouched this session');
  return notes;
}

// ------------------------------------------------------------------------ tier 0 as the loader reports it

/* N, the number of persona hooks, is the largest N in the `load-persona.sh K N` commands of
   <repo>/.claude/settings.json (8 when that file is absent or has none).
   `bash .claude/hooks/load-persona.sh check N` prints one line per part, e.g.
     part 1: 8123 bytes ok  CRITICAL CORE, IDENTITY, SOUL
   and exits 1 when the payload does not fit. Run it at most every 30 s, asynchronously, and serve the last
   answer - /api/state is built on every push and must never wait on a subprocess. A missing script or
   bash is a normal state ("loader not found"), never an error. */
const tier0Cache = { at: 0, busy: false, value: { available: false, note: 'loader not found', parts: [], bytes: 0 } };
const PART_LINE = /^\s*part\s+(\d+)\s*:\s*(\d+)\s*bytes\s+(OK|NOT LOADED|OVER CAP|\S+)\s*(.*)$/i;

export function parseTier0(out) {
  const parts = [];
  for (const line of String(out || '').split(/\r?\n/)) {
    const m = PART_LINE.exec(line);
    if (m) parts.push({ k: +m[1], bytes: +m[2], flag: m[3] || '', titles: (m[4] || '').trim() });
  }
  // spare hooks print "(empty)"; they are configured-but-unused, not parts of the payload
  return parts.filter((p) => p.titles !== '(empty)');
}

/** Largest N among the `load-persona.sh K N` hook commands in a settings.json text, or null. */
export function personaPartsFromSettings(text) {
  let json;
  try { json = JSON.parse(text); } catch { return null; }
  let best = null;
  const walk = (v) => {
    if (typeof v === 'string') {
      const m = /load-persona\.sh\W+(\d+)\s+(\d+)/.exec(v);
      if (m && +m[2] > 0 && (best === null || +m[2] > best)) best = +m[2];
    } else if (v && typeof v === 'object') {
      for (const x of Object.values(v)) walk(x);
    }
  };
  walk(json);
  return best;
}

function personaParts() {
  try { return personaPartsFromSettings(fs.readFileSync(path.join(REPO, '.claude', 'settings.json'), 'utf8')) || 8; } catch { return 8; }
}

function refreshTier0() {
  if (tier0Cache.busy) return;
  const script = path.join(REPO, '.claude', 'hooks', 'load-persona.sh');
  if (!fs.existsSync(script)) {
    tier0Cache.value = { available: false, note: 'loader not found', parts: [], bytes: 0 };
    tier0Cache.at = Date.now();
    return;
  }
  tier0Cache.busy = true;
  const child = execFile('bash', [script.split(path.sep).join('/'), 'check', String(personaParts())], { cwd: REPO, timeout: 8000, windowsHide: true },
    (err, stdout) => {
      tier0Cache.busy = false;
      tier0Cache.at = Date.now();
      const parts = parseTier0(stdout);
      if (!parts.length) {
        tier0Cache.value = { available: false, note: err && err.code === 'ENOENT' ? 'bash not found' : 'loader gave no part table', parts: [], bytes: 0 };
        return;
      }
      tier0Cache.value = {
        available: true, ok: !err, parts, bytes: parts.reduce((a, p) => a + p.bytes, 0),
        note: err ? 'tier 0 does not fit (loader exited non-zero)' : '',
      };
    });
  child.stdin && child.stdin.end();
}

/** The last tier-0 reading for the page; kicks a refresh when it is older than 30 s. */
export function tierZero() {
  if (Date.now() - tier0Cache.at > 30000) refreshTier0();
  return tier0Cache.value;
}
