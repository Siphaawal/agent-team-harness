/* Insight — front end. Pure DOM, no build step, no dependencies.
   All numbers arrive from /api/stream (SSE). Nothing is computed here that the server did not send,
   except formatting and the sparkline. */

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };

/* The agent's display name: read from agent/IDENTITY.md by the server, "Agent" when there is none. */
const agentName = (s) => String((s && s.agent && s.agent.name) || 'Agent');
// upper-case form for the headline and the graph root; render() refreshes it from every push
let AGENT_LABEL = 'AGENT';

const nf = new Intl.NumberFormat('en-US');
const kfmt = (n) => {
  n = n || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return nf.format(n);
};
const usd = (v) => (v == null ? '—' : '$' + (v < 0.01 && v > 0 ? v.toFixed(4) : v.toFixed(2)));
const hhmm = (ts) => (ts ? new Date(ts).toTimeString().slice(0, 8) : '--:--:--');
const ago = (ms) => {
  if (ms == null) return '—';
  const s = Math.round(ms / 1000);
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm ' + (s % 60) + 's';
  return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
};
const shortModel = (m) => (m || '—').replace(/^claude-/, '').replace(/-\d{8}$/, '').toUpperCase();
const tail = (p, n = 44) => { if (!p) return '—'; return p.length <= n ? p : '…' + p.slice(-n); };

let last = null;
const history = [];              // sparkline: tokens per push
let lastTotal = 0;

/* Burn rate - tokens per minute, an EMA over the pushes the page already receives. A high burn spins the
   ring fast and smokes it. It costs no tokens: it is computed HERE from numbers already on the wire;
   nothing is asked of the server or of any model.
   ?burn=<tokens per minute> on the URL forces a rate (for looking at the effect). */
const burn = { rate: 0, at: 0, total: 0, forced: Number(new URLSearchParams(location.search).get('burn')) || 0 };
function updateBurn(s) {
  const now = s.now || Date.now();
  if (burn.at && s.tokens.total >= burn.total) {
    const dtMin = (now - burn.at) / 60000;
    if (dtMin > 0.02) {                                   // ignore sub-second double pushes
      const inst = (s.tokens.total - burn.total) / dtMin;
      const a = 1 - Math.exp(-dtMin / 0.35);              // ~20 s time constant
      burn.rate = burn.rate + a * (inst - burn.rate);
      burn.at = now; burn.total = s.tokens.total;
    }
  } else { burn.at = now; burn.total = s.tokens.total; }
  return burn.forced || burn.rate;
}
const burnLevel = (s, rate) => {
  const th = s.burn || { hotTokPerMin: 1500000, blazeTokPerMin: 4000000 };
  return rate >= th.blazeTokPerMin ? 'blaze' : rate >= th.hotTokPerMin ? 'hot' : '';
};

/* ── panels ────────────────────────────────────────────────────────────── */

/* CONTEXT & BUDGET: the context meter stays here and the local budget bar is in renderUsage. */
function renderLedger(s) {
  $('ledgerStamp').textContent = 'updated ' + hhmm(s.now);

  const pct = Math.min(100, (s.session.contextTokens / s.session.contextLimit) * 100);
  $('ctxPct').textContent = pct.toFixed(1) + '%';
  const bar = $('ctxBar');
  bar.style.width = pct + '%';
  bar.className = pct > 85 ? 'crit' : pct > 65 ? 'warn' : '';
  $('ctxSub').textContent = s.session.contextSource === 'compact'
    ? `compacted ${hhmm(s.session.compactedAt)} · ~${kfmt(s.session.contextTokens)} estimated from the summary until the next call`
    : `${kfmt(s.session.contextTokens)} of ${kfmt(s.session.contextLimit)} in the live window`
      + (s.session.compactions ? ` · compacted ×${s.session.compactions}` : '');
}

function renderGate(s) {
  const gate = document.querySelector('.gate');
  const busy = s.session.idleMs != null && s.session.idleMs < 20000;
  const f = s.friction;
  gate.classList.toggle('busy', busy && f.level < 70);
  gate.classList.toggle('stuck', f.level >= 70);
  const rate = burn.forced || burn.rate;
  const lvl = burnLevel(s, rate);
  gate.classList.toggle('hot', lvl === 'hot');
  gate.classList.toggle('blaze', lvl === 'blaze');
  gate.classList.toggle('smoketest', new URLSearchParams(location.search).has('smoke'));

  $('gModel').textContent = shortModel(s.session.model) + (s.session.effort ? ' · ' + s.session.effort.toUpperCase() : '');
  $('gState').textContent = f.level >= 70 ? 'STUCK' : busy ? 'WORKING' : s.session.idleMs < 120000 ? 'HOLDING' : 'STANDBY';

  const lastTool = s.tools.recent[0];
  $('gSub').textContent = busy && lastTool ? lastTool.detail : 'last activity ' + ago(s.session.idleMs);
  $('gRingLabel').textContent = f.label + (f.level ? ' // ' + f.level : '')
    + (rate >= 100000 ? '   ·   BURN ' + kfmt(Math.round(rate)) + '/MIN' + (lvl === 'blaze' ? ' · BLAZING' : lvl === 'hot' ? ' · HOT' : '') : '');
  $('gRingLabel').style.color = f.level >= 70 ? 'var(--red)' : f.level >= 40 ? 'var(--amber)' : 'var(--cyan-dim)';
}

function renderActivity(s) {
  const box = $('statline');
  box.textContent = '';
  const cell = (label, value) => { const d = el('div'); d.appendChild(el('span', null, label)); d.appendChild(el('b', null, value)); box.appendChild(d); };
  cell('TURNS', String(s.session.turns));
  cell('TOOL CALLS', String(s.tools.recent.length ? s.tools.counts.reduce((a, b) => a + b.count, 0) : 0));
  cell('FAULTS', String(s.errors.count));
  cell('UPTIME', ago(s.session.startedAt ? s.now - s.session.startedAt : null));

  // the sparkline is TOOL CALLS per push
  const total = s.tools.counts.reduce((a, b) => a + b.count, 0);
  if (lastTotal && total > lastTotal) history.push(total - lastTotal);
  else if (history.length === 0) history.push(0);
  lastTotal = total;
  while (history.length > 180) history.shift();

  const c = $('spark'), ctx = c.getContext('2d');
  const w = c.width, h = c.height;
  ctx.clearRect(0, 0, w, h);
  const max = Math.max(1, ...history);
  ctx.strokeStyle = 'rgba(29,122,46,.55)'; ctx.lineWidth = 1;
  for (let i = 1; i < 4; i++) { const y = (h / 4) * i; ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
  const bw = w / 180;
  history.forEach((v, i) => {
    const bh = Math.max(1, (v / max) * (h - 6));
    const g = ctx.createLinearGradient(0, h - bh, 0, h);
    g.addColorStop(0, '#39ff14'); g.addColorStop(1, 'rgba(31,154,58,.25)');
    ctx.fillStyle = g;
    ctx.fillRect(i * bw, h - bh, Math.max(1, bw - 1), bh);
  });
}

/* Roster colours: the team config gives each teammate a colour name (blue, green,
   yellow, purple, orange, pink...). The same colour marks the agent in the TEAM row and in every
   "· by <agent>" tag on the MEMORY CORE and SKILLS panels, so a glance tells lead from teammate.
   An agent with no roster row (a forked subagent such as the librarian) gets the dim green. */
const ROSTER_HEX = { blue: '#5aa9ff', green: '#39ff14', yellow: '#ffe066', purple: '#c78bff', orange: '#ff9f43',
  pink: '#ff7ac8', red: '#ff6e5a', cyan: '#33e0e0', white: '#dfffd6', magenta: '#ff5ad6', teal: '#2fd4b6' };
function agentColor(s, name) {
  const m = s.team && s.team.config && s.team.config.members.find((x) => x.name === name);
  return (m && m.color && ROSTER_HEX[m.color]) || (m && m.color) || 'var(--cyan-dim)';
}
/** "· by <agent>" in the agent's roster colour; null when the hit was the lead's own. */
function byTag(s, name) {
  if (!name) return null;
  const t = el('em', 'by', '· ' + name);
  t.style.color = agentColor(s, name);
  return t;
}
/** "name n · name n" for a per-agent tally, each name in its colour. */
function byList(s, tally, into) {
  const entries = Object.entries(tally || {}).sort((a, b) => b[1] - a[1]);
  entries.forEach(([name, n], i) => {
    const t = el('em', 'by', (i ? ' ' : '') + name + ' ' + n);
    t.style.color = agentColor(s, name);
    into.appendChild(t);
  });
  return entries.length;
}

/* TEAM: the lead and one row per agent transcript, lead first, then running, idle, done.
   Every number is the agent's own transcript folded the way the ledger folds the session; the
   server already sorted the rows. Without a team config and without subagents/ it says so.
   MEM / SKILLS / FILES: the agent's own memory-tier hits (t0/t1/t2), skill invocations plus
   skill-file touches (names in the tooltip), and files edited. The lead's MEM and SKILLS are the
   session totals minus what the agents did - the server subtracts, the page only formats. */
/* Two statuses the stylesheet has no rules for borrow an existing look: handed-in reads as done
   (dim, check mark), pending-followup as idle (amber). The words stay their own (STATUS column,
   tooltips, counts). Shared with graph.js and agent.js, which load after this file. */
const STATUS_BASE = { 'handed-in': 'done', 'pending-followup': 'idle' };
const statusBase = (st) => STATUS_BASE[st] || st;
const CTX_WARN = 300000;     // mirrors lib/team.mjs CTX_WARN_TOKENS; the server also sends row.ctxWarn
function renderTeam(s) {
  const box = $('team'), stamp = $('teamStamp');
  const t = s.team;
  box.textContent = '';
  if (!t || (!t.config && !(t.agents || []).length)) {
    stamp.textContent = 'solo';
    box.appendChild(el('div', 'empty', 'no team - no teams/ config for this session and no subagents/ transcripts'));
    return;
  }
  const agents = t.agents || [];
  const c = t.counts || {};
  stamp.textContent = t.config ? t.config.name : 'subagents only - no team config';

  const head = el('div', 'teamhead');
  const stat = (n, label, cls) => { head.appendChild(el('b', cls || null, String(n))); head.appendChild(el('span', null, label)); };
  stat(agents.length, agents.length === 1 ? 'agent' : 'agents');
  stat(c.running || 0, 'running', 'dim');
  if (c['pending-followup']) stat(c['pending-followup'], 'follow-up pending', 'dim');
  stat(c.idle || 0, 'idle', 'dim');
  stat(c['handed-in'] || 0, 'handed in', 'dim');
  stat(c.done || 0, 'done', 'dim');
  if (t.stale) stat(t.stale, 'earlier', 'dim');
  stat((t.edges || []).length, 'msgs', 'dim');
  if (t.config) head.appendChild(el('span', 'tn', `${t.config.members.length} in roster · ${t.config.members.filter((m) => m.backendType === 'in-process').length} in-process`));
  box.appendChild(head);

  const hdr = el('div', 'teamrow head');
  // short headers, right-aligned over the number columns: they ran together otherwise
  for (const [h, cls] of [['', ''], ['AGENT', ''], ['TYPE', ''], ['ROLE', ''], ['CORE', ''], ['STATUS', ''], ['UP', 'r'], ['TOOLS', 'r'], ['MEM', 'r'],
    ['SKILL', 'r'], ['FILE', 'r'], ['CTX', 'r'], ['LAST TOOL', '']]) hdr.appendChild(el('span', cls || null, h));
  box.appendChild(hdr);

  const row = (r) => {
    const d = el('div', 'teamrow ' + statusBase(r.status) + ' ' + r.status + (r.lead ? ' lead' : '') + (r.stale ? ' stale' : ''));
    // the row opens the AGENT DETAIL modal, like the graph node (public/agent.js)
    if (r.id) { d.dataset.agent = r.id; d.tabIndex = 0; d.setAttribute('role', 'button'); }
    d.appendChild(el('i', 'dot ' + statusBase(r.status)));
    // the name only - never the agent's task/description (private)
    const nm = el('span', 'nm', r.name || '?'); nm.title = r.name || '';
    if (!r.lead) nm.style.color = agentColor(s, r.name);
    d.appendChild(nm);
    // TYPE is the kind (lead / teammate / subagent); agentType, model and backend go to the tooltip
    const ty = el('span', 'ty', (r.kind || (r.lead ? 'lead' : 'subagent')).toUpperCase());
    ty.title = [r.type && r.type !== r.name ? r.type : null, r.member && r.member.model, r.member && r.member.backendType].filter(Boolean).join(' · ');
    d.appendChild(ty);
    // ROLE: the definition it was spawned as. Bright = a project role with its own
    // definition + memory; dim = a built-in type; '—' = no role recorded. It is the REQUESTED role -
    // a brand-new role spawned before the harness registered it runs generic and reads the same here.
    const ro = el('span', 'ro' + (r.defined ? ' def' : ' dim'), r.lead ? '—' : r.role === 'general-purpose' ? 'generic' : (r.role || '—'));
    ro.title = r.lead ? 'the lead session' : r.role
      ? (r.defined ? `role ${r.role}: ~/.claude/agents/${r.role}.md + its own memory` : `built-in type ${r.role} (no project definition)`)
      : 'no spawn call recorded for this name';
    d.appendChild(ro);
    // CORE: the shared agent core from the SubagentStart hook. ✓vN = delivered; ✗ = hook ran but failed;
    // '—' on an agent = never fired (broken hook, or a transcript older than the hook). ×N = resumed N-1 times.
    const co = r.core;
    const coTxt = r.lead ? '—' : !co ? '—' : co.ok ? `✓v${co.version}${co.count > 1 ? '×' + co.count : ''}` : '✗';
    const ce = el('span', 'co' + (r.lead ? ' dim' : !co ? ' bad' : co.ok ? '' : ' bad'), coTxt);
    ce.title = r.lead ? 'the lead gets tier 0 instead (SessionStart)' : !co ? 'no SubagentStart attachment in this transcript - the core never arrived'
      : `agent core v${co.version ?? '?'} · fired ${co.count}× (a resume fires again)` + (co.failed ? ` · ${co.failed} failed` : '');
    d.appendChild(ce);
    d.appendChild(el('span', 'st ' + statusBase(r.status), r.status.toUpperCase().replace('-', ' ')));
    d.appendChild(el('span', 'num', ago(r.elapsedMs)));
    d.appendChild(el('span', 'num' + (r.errors ? ' bad' : ''), String(r.toolCalls) + (r.errors ? '/' + r.errors + '!' : '')));
    // MEM: tier hits as t0/t1/t2; SKILLS: invocations + file touches with the names in the tip; FILES: edits
    const mem = r.memory || { t0: 0, t1: 0, t2: 0, t3: 0, count: 0 }, skl = r.skills || { invoked: [], touched: [], touches: 0 }, fl = r.files || { edited: 0, read: 0, searched: 0 };
    const mm = el('span', 'num' + (mem.count ? '' : ' dim'), mem.count ? `${mem.t0}/${mem.t1}/${mem.t2}/${mem.t3 || 0}` : '—');
    mm.title = mem.count
      ? `memory tier 0 / 1 / 2 / agent hits: ${mem.t0} / ${mem.t1} / ${mem.t2} / ${mem.t3 || 0} (agent = role memory, definitions, core loads)` + (mem.last ? ` · last ${hhmm(mem.last.at).slice(0, 5)} ${mem.last.op} ${mem.last.file} (tier ${mem.last.tier})` : '')
      : 'no memory access';
    d.appendChild(mm);
    const skN = skl.invoked.length + skl.touches;
    const sk = el('span', 'num' + (skN ? '' : ' dim'), skN ? `${skl.invoked.length}+${skl.touches}` : '—');
    sk.title = skN
      ? (skl.invoked.length ? 'invoked: ' + skl.invoked.map((k) => k.skill).join(', ') + '  ' : '')
        + (skl.touches ? 'files touched: ' + skl.touched.map((k) => `${k.skill} (${k.scope}) ×${k.touches}`).join(', ') : '')
      : 'no Skill invocation, no skill file touched';
    d.appendChild(sk);
    const fe = el('span', 'num' + (fl.edited ? '' : ' dim'), fl.edited ? String(fl.edited) : '—');
    fe.title = `${fl.edited} edited · ${fl.read} read · ${fl.searched} searched`;
    d.appendChild(fe);
    // CTX: the agent's live context window
    const lim = s.session.contextLimit || 1000000;
    const warn = r.ctxWarn || (r.contextTokens || 0) > CTX_WARN;
    const cx = el('span', 'num' + (warn ? ' ctxwarn' : ''), kfmt(r.contextTokens) + (warn ? '!' : ''));
    if (warn) cx.style.color = 'var(--amber)';
    cx.title = `context ${nf.format(r.contextTokens || 0)} of ${kfmt(lim)} (${((r.contextTokens || 0) / lim * 100).toFixed(1)}%) · ${shortModel(r.model)}` + (warn ? ` · over ${kfmt(CTX_WARN)} tokens` : '');
    d.appendChild(cx);
    // describeTool falls back to the bare name for a tool with no path or description - do not print it twice
    const ltDetail = r.lastTool && r.lastTool.detail && r.lastTool.detail !== r.lastTool.name ? r.lastTool.detail : '';
    const lt = el('span', 'lt', r.lastTool ? `${hhmm(r.lastTool.at).slice(0, 5)} ${r.lastTool.name.replace(/^mcp__/, '')} ${ltDetail}`.trim() : '—');
    if (r.lastTool) lt.title = `${hhmm(r.lastTool.at)} ${r.lastTool.name} ${r.lastTool.detail || ''}${r.lastTool.ok === false ? ' (failed)' : ''}`;
    d.appendChild(lt);
    box.appendChild(d);
  };
  if (t.lead) row(t.lead);
  // live agents first; agents quiet for longer than team.staleMs (earlier runs) fold into one
  // "+N earlier" row that opens on click and stays open across pushes
  const live = agents.filter((r) => !r.stale), stale = agents.filter((r) => r.stale);
  for (const r of live) row(r);
  if (stale.length) {
    const more = el('div', 'teamrow more');
    more.appendChild(el('i', 'dot done'));
    const lab = el('span', 'mo', `${teamStaleOpen ? '−' : '+'}${stale.length} earlier · quiet over ${Math.round((t.staleMs || 1800000) / 60000)} min · `);
    stale.forEach((r, i) => { const n = el('em', null, (i ? ' ' : '') + r.name); n.style.color = agentColor(s, r.name); lab.appendChild(n); });
    more.appendChild(lab);
    more.addEventListener('click', () => { teamStaleOpen = !teamStaleOpen; if (last) renderTeam(last); });
    box.appendChild(more);
    if (teamStaleOpen) for (const r of stale) row(r);
  }
}
let teamStaleOpen = new URLSearchParams(location.search).has('teamopen');   // ?teamopen: start unfolded (headless captures)

/* One labelled bar: the label, the percent used, and optionally a sub-label. */
function planRow(label, pct, severity) {
  const d = el('div', 'planrow');
  const lab = el('div', 'ctxlabel');
  lab.appendChild(el('span', null, label));
  const cls = pct >= 100 || severity === 'critical' ? 'crit' : pct >= 85 || severity === 'warning' ? 'warn' : '';
  lab.appendChild(el('b', cls, `${Math.round(pct)}% used`));
  d.appendChild(lab);
  const m = el('div', 'meter big'); const i = el('i', cls); i.style.width = Math.min(100, pct) + '%';
  m.appendChild(i); d.appendChild(m);
  return d;
}

/* The local budget gauge: API-list-equivalent spend over the window in config.json against its usd cap. */
function renderUsage(s) {
  const u = s.usage;
  const box = $('plan'), stamp = $('planStamp');
  box.textContent = '';
  $('planLabel').textContent = `API-EQUIV · ${u.window.toUpperCase()} BUDGET of ${usd(u.budgetUSD)}`;
  stamp.textContent = 'local budget'; stamp.className = '';
  box.appendChild(planRow('LOCAL BUDGET', u.pct, null));
}

/* LESSONS: the newest memory/lessons/*.md files, one row each. Hover = the trigger question and whether it is
   always loaded (tier 0) or on demand; click (or Enter) = the full lesson in a modal (lesson.js,
   GET /api/lesson/<slug>). A lesson with no trigger is flagged: it cannot fire. Rebuilt only when the list
   changes, so a hover tip survives the 1 s pushes. */
let lessonsSig = '';
function renderLessons(s) {
  const box = $('lessons'), stamp = $('lessonsStamp');
  const L = s.lessons;
  if (!box || !L) return;
  const sig = JSON.stringify([L.error || null, !!L.loading, L.total, L.triggers,
    (L.recent || []).map((r) => [r.key, r.status, r.trigger && r.trigger.where])]);
  if (sig === lessonsSig) return;
  lessonsSig = sig;
  box.textContent = '';
  if (L.error) { stamp.textContent = ''; box.appendChild(el('div', 'empty warn', L.error)); return; }
  if (L.loading) { stamp.textContent = ''; box.appendChild(el('div', 'empty', 'reading lessons…')); return; }
  if (!L.total) { stamp.textContent = ''; box.appendChild(el('div', 'empty', 'no lessons yet - memory/lessons/ is empty')); return; }
  stamp.textContent = `${L.total} · ${L.triggers} triggers · ${L.withoutTrigger} unfired`;
  stamp.title = `${L.total} live lessons; ${L.tier0} always-loaded triggers; `
    + `${L.withoutTrigger} lessons have no trigger, so nothing makes them fire`;
  for (const r of L.recent || []) {
    const row = el('div', 'lsrow' + (r.trigger ? '' : ' unfired'));
    row.tabIndex = 0; row.setAttribute('role', 'button'); row.dataset.lesson = r.key;
    row.appendChild(el('b', null, r.key));
    row.appendChild(el('span', 'lt', r.title));
    row.appendChild(el('i', 'lw', r.trigger ? (r.trigger.where === 'tier 0' ? 'T0' : 'T2') : '—'));
    row.appendChild(el('s', null, r.date ? r.date.slice(5) : ''));
    row.title = `${r.key}${r.date ? ' (' + r.date + ')' : ''}\n${r.title}\n\n`
      + (r.trigger ? `TRIGGER (${r.trigger.where}):\n“${r.trigger.question}”` : 'NO TRIGGER - nothing makes this lesson fire')
      + '\n\nclick for the full lesson';
    box.appendChild(row);
  }
}

function renderSkills(s) {
  const box = $('skills');
  const sk = s.skills || {};
  const invoked = sk.invoked || [];
  box.textContent = '';

  // the headline is the RATIO, not the list - an empty list reads as "nothing to report",
  // which is exactly the wrong impression when it means "a capability went unused"
  const bar = el('div', 'skhead' + (sk.invokeCount === 0 && sk.toolCalls > 150 ? ' bad' : ''));
  bar.appendChild(el('b', null, String(sk.invokeCount ?? 0)));
  bar.appendChild(el('span', null, 'invoked'));
  bar.appendChild(el('b', 'dim', String(sk.fileTouches ?? 0)));
  bar.appendChild(el('span', null, 'file touches'));
  bar.appendChild(el('b', 'dim', kfmt(sk.toolCalls || 0)));
  bar.appendChild(el('span', null, 'tool calls'));
  box.appendChild(bar);

  // where the skills live: invocations and file touches split by global / repo / backup / plugin
  const scopes = ['repo', 'global', 'repo+global', 'backup', 'plugin', 'built-in'];
  const split = (m) => scopes.filter((k) => m && m[k]).map((k) => `${k} ${m[k]}`).join(' · ');
  const inv = split(sk.invokeByScope), tch = split(sk.touchByScope);
  if (inv || tch) {
    const sc = el('div', 'fold');
    sc.appendChild(el('span', null, (inv ? 'invoked: ' + inv : '') + (inv && tch ? '   ' : '') + (tch ? 'files: ' + tch : '')));
    box.appendChild(sc);
  }

  if (!invoked.length) {
    const w = el('div', 'empty' + (sk.toolCalls > 150 ? ' warn' : ''));
    w.textContent = sk.toolCalls > 150
      ? `no skill invoked in ${kfmt(sk.toolCalls)} calls` + (sk.fileTouches ? ` — yet skill files were touched ${sk.fileTouches}x` : '')
      : 'no Skill invocations yet';
    box.appendChild(w);
  } else {
    for (const k of invoked) {
      const r = el('div', 'srow');
      r.appendChild(el('b', null, k.skill || '?'));
      const line = el('span', null, hhmm(k.at) + ` · ${k.scope || '?'}` + (k.args ? ' · ' + String(k.args).slice(0, 40) : ''));
      const tag = byTag(s, k.by);               // the teammate that invoked it, in its roster colour
      if (tag) line.appendChild(tag);
      r.appendChild(line);
      box.appendChild(r);
    }
  }

  if ((sk.touched || []).length) {
    box.appendChild(el('div', 'subhead', 'SKILL FILES TOUCHED'));
    for (const t of sk.touched) {
      const r = el('div', 'fold');
      const ops = ['read', 'search', 'write'].filter((o) => t[o]).map((o) => `${t[o]}${o[0]}`).join(' ');
      const line = el('span', null, `${t.skill} · ${t.scope}`);
      // every agent that touched this skill's files, each with its count, in its roster colour;
      // the remainder (total minus the agents) is the lead's own
      const agentTouches = Object.values(t.byAgent || {}).reduce((a, b) => a + b, 0);
      const own = (t.read || 0) + (t.write || 0) + (t.search || 0) - agentTouches;
      if (agentTouches) { line.appendChild(document.createTextNode(' · ')); byList(s, t.byAgent, line); }
      if (own > 0 && agentTouches) line.appendChild(el('em', 'by lead', ' lead ' + own));
      r.appendChild(line);
      r.appendChild(el('b', null, ops));
      box.appendChild(r);
    }
  }

  box.appendChild(el('div', 'subhead', 'TOOLS BY VOLUME'));
  for (const t of s.tools.counts.slice(0, 8)) {
    const r = el('div', 'fold');
    r.appendChild(el('span', null, t.name.replace(/^mcp__/, '').slice(0, 26)));
    r.appendChild(el('b', null, String(t.count)));
    box.appendChild(r);
  }
}

function renderTools(s) {
  const box = $('tools');
  box.textContent = '';
  for (const t of s.tools.recent) {
    const r = el('div', 'trow');
    r.appendChild(el('i', 'dot' + (t.ok === true ? ' ok' : t.ok === false ? ' bad' : '')));
    r.appendChild(el('span', 'tt', hhmm(t.at)));
    r.appendChild(el('span', 'tn', t.name.replace(/^mcp__/, '')));
    const d = el('span', 'td', t.detail);
    d.title = t.detail;
    r.appendChild(d);
    box.appendChild(r);
  }
  if (!s.tools.recent.length) box.appendChild(el('div', 'empty', 'idle'));
}

function renderFiles(s) {
  const box = $('files');
  box.textContent = '';
  box.appendChild(el('div', 'subhead', 'FOLDERS TOUCHED'));
  for (const f of s.files.folders) {
    const r = el('div', 'fold');
    const n = el('span', null, tail(f.dir, 40)); n.title = f.dir;
    r.appendChild(n);
    r.appendChild(el('b', null, '×' + f.hits));
    box.appendChild(r);
  }
  box.appendChild(el('div', 'subhead', 'RECENT FILES'));
  for (const f of s.files.recent) {
    const r = el('div', 'frow');
    r.appendChild(el('span', 'op', f.op.toUpperCase()));
    const p = el('span', 'fp', tail(f.path, 48)); p.title = f.path;
    r.appendChild(p);
    box.appendChild(r);
  }
  if (!s.files.recent.length && !s.files.folders.length) box.appendChild(el('div', 'empty', 'no filesystem activity'));
}

function renderErrors(s) {
  const box = $('errors');
  box.textContent = '';
  if (s.friction.reasons.length) {
    box.appendChild(el('div', 'subhead', 'FRICTION · ' + s.friction.label));
    for (const r of s.friction.reasons) box.appendChild(el('div', 'fold', '· ' + r));
  }
  box.appendChild(el('div', 'subhead', 'FAULTS · ' + s.errors.count + ' TOTAL'));
  if (!s.errors.recent.length) { box.appendChild(el('div', 'empty', 'clean')); return; }
  for (const e of s.errors.recent) {
    const r = el('div', 'erow');
    r.appendChild(el('div', 'eh', hhmm(e.at) + '  ' + e.tool + (e.detail ? ' · ' + e.detail : '')));
    r.appendChild(el('div', 'em', (e.message || '').replace(/\s+/g, ' ').slice(0, 600)));
    box.appendChild(r);
  }
}

function renderSessions(s) {
  const parts = s.sessions.slice(0, 5).map((x) =>
    `${x.active ? '▸' : '·'} ${(x.projectPath || x.project || '').split(/[\\/]/).pop()} ${hhmm(x.at).slice(0, 5)}`);
  // project and last-activity time only
  $('sessions').textContent = 'RECENT SESSIONS  ' + (parts.join('   ') || '—');
}



/* ── memory tiers, drawn on the gate ────────────────────────────────────
   The three engraved grooves of ring_core ARE the three tiers.
   viewBox radii derived from the Blender geometry - see the note in style.css. */
// The AGENT tier (3) has no groove of its own: its pip shares the outer groove with tier 2,
// higher up the arc, and its card tops the column - role memory, role definitions, the shared agent core.
const TIER_R = { 0: 14.55, 1: 20.00, 2: 24.55, 3: 24.55 };
const TIER_META = {
  0: { name: 'TIER 0', hue: '#ffe066', what: 'resident - identity, soul, team, triggers' },
  1: { name: 'TIER 1', hue: '#39ff14', what: 'on demand - CLAUDE.md, docs' },
  2: { name: 'TIER 2', hue: '#19c96a', what: 'archive - memory/** + auto memory' },
  3: { name: 'AGENT', hue: '#5ad1ff', what: 'per role - role memory, definitions, shared core' },
};
// Pips sit on their own grooves; the CARDS live outside the rings entirely, in their own grid
// column. Card Y is FIXED and evenly spaced - deriving it from the pip's Y bunched all three
// within 18 percent of each other, because sin() at three similar radii lands them together.
const TIER_ANGLE = { 0: 214, 1: 180, 2: 146, 3: 118 };   // pip angle on its groove, degrees

function renderMemory(s) {
  const m = s.memory;
  const svg = $('leaders'), box = $('tierCallouts');
  if (!m || !svg || !box) return;
  svg.textContent = ''; box.textContent = '';
  const NS = 'http://www.w3.org/2000/svg';
  const mk = (t, attrs) => { const e = document.createElementNS(NS, t); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };

  const pips = [];
  const busy = m.tiers.reduce((a, t) => a + t.total, 0);
  document.querySelector('.gatebox')?.classList.toggle('memhot', busy > 20);

  const ordered = [...m.tiers].reverse();          // T2 top, T0 bottom
  for (const t of ordered) {
    const meta = TIER_META[t.tier];
    const r = TIER_R[t.tier];
    const a = (TIER_ANGLE[t.tier] * Math.PI) / 180;
    const cx = 50 + r * Math.cos(a);
    const cy = 50 - r * Math.sin(a);
    const cold = t.total === 0;
    // tier 0 can be "loaded" and still not be in context (persisted hook output) - that is red, not gold
    const idBad = t.tier === 0 && m.identity?.status?.level === 'critical';
    const col = idBad ? '#ff6e5a' : cold ? '#1a4a24' : meta.hue;

    pips.push({ tier: t.tier, cx, cy, col, cold });

    const c = el('div', 'tc t' + t.tier + (cold ? ' cold' : '') + (idBad ? ' bad' : ''));
    c.dataset.tier = String(t.tier);

    const head = el('div', 'tcn');
    head.appendChild(el('b', null, meta.name));
    head.appendChild(el('span', 'split', cold ? 'untouched'
      : `${t.load ? t.load + 'L ' : ''}${t.read}r ${t.write}w ${t.search}s`));
    head.appendChild(el('em', null, cold ? '—' : String(t.total)));
    c.appendChild(head);
    c.appendChild(el('div', 'what', meta.what));
    // who did it: each agent's share of this tier in its roster colour, the rest is the lead's own
    if (t.byAgent && Object.keys(t.byAgent).length) {
      const who = el('div', 'who');
      byList(s, t.byAgent, who);
      const own = t.total - Object.values(t.byAgent).reduce((a, b) => a + b, 0);
      if (own > 0) who.appendChild(el('em', 'by lead', ' lead ' + own));
      c.appendChild(who);
    }
    if (t.tier === 0 && m.identity?.status) {
      c.appendChild(el('div', 'idline' + (idBad ? ' warn' : ''), m.identity.status.text));
    }
    if (t.tier === 0 && s.tier0) {
      // what `load-persona.sh check` reports for the repo checkout (not what this session received)
      const z = s.tier0;
      const line = z.available
        ? `loader: ${z.parts.length} part${z.parts.length === 1 ? '' : 's'}, ${nf.format(z.bytes)} B${z.ok ? '' : ' - DOES NOT FIT'}`
        : `loader: ${z.note || 'not found'}`;
      const ln = el('div', 'idline' + (z.available && !z.ok ? ' warn' : ''), line);
      if (z.available) ln.title = z.parts.map((p) => `part ${p.k}: ${p.bytes} bytes ${p.flag} ${p.titles}`).join('\n');
      c.appendChild(ln);
    }

    const hits = t.recent || [];
    const hitsBox = el('div', 'hits');
    c.appendChild(hitsBox);
    if (!hits.length) {
      hitsBox.appendChild(el('div', 'why', 'no access this session'));
    } else {
      for (const h of hits) {
        const hit = el('div', 'hit');
        const op = el('div', 'op');
        op.appendChild(el('i', h.op, h.op));
        const u = el('u', null, h.file);
        u.title = `${h.file} (${h.where})`;
        op.appendChild(u);
        const tag = byTag(s, h.by);             // a teammate's or the librarian's read, in its colour
        if (tag) op.appendChild(tag);
        op.appendChild(el('s', null, hhmm(h.at).slice(0, 5)));
        hit.appendChild(op);
        hit.appendChild(el('div', 'why', h.why ? '“' + h.why + '”' : 'prompt #' + (h.promptN || '?')));
        hitsBox.appendChild(hit);
      }
    }
    box.appendChild(c);
  }

  if (m.notes.length) box.appendChild(el('div', 'memnote', '! ' + m.notes[0]));

  // the cards are in the DOM, so measuring forces layout: fit them NOW, not in a frame callback - a frame that
  // never comes (a background tab, a headless capture) would leave the unfitted, sliced text on screen
  fitTierCards(box);
  // then draw each leader to its card's measured centre
  requestAnimationFrame(() => {
    const gb = document.querySelector('.gatebox');
    if (!gb) return;
    const br = gb.getBoundingClientRect();
    if (!br.height) return;
    for (const p of pips) {
      const card = box.querySelector(`.tc[data-tier="${p.tier}"]`);
      if (!card) continue;
      const cr = card.getBoundingClientRect();
      const y = ((cr.top + cr.height / 2) - br.top) / br.height * 100;
      // the card's right edge in the box's own 0-100 space (negative = left of the box). The SVG
      // overflows, so the line genuinely reaches the card whatever the column gap is.
      const xEdge = (cr.right - br.left) / br.width * 100;
      const knee = Math.max(xEdge + 3, 3);
      svg.appendChild(mk('line', { x1: p.cx, y1: p.cy, x2: knee, y2: y, stroke: p.col, class: 'lead' }));
      svg.appendChild(mk('line', { x1: knee, y1: y, x2: xEdge, y2: y, stroke: p.col }));
      svg.appendChild(mk('circle', { cx: xEdge, cy: y, r: 1.1, fill: p.col }));
      if (!p.cold) svg.appendChild(mk('circle', { cx: p.cx, cy: p.cy, fill: 'none', stroke: p.col, class: 'halo' }));
      svg.appendChild(mk('circle', { cx: p.cx, cy: p.cy, fill: p.col, class: 'pip' }));
    }
  });
}

/* ── alarms ─────────────────────────────────────────────────────────────
   Text only: nothing is spoken and nothing is sent anywhere. */
function renderAlarms(s) {
  const box = $('alarms');
  box.textContent = '';
  const alerts = s.alerts || [];

  for (const al of alerts) {
    const d = el('div', 'alarm ' + al.level);
    d.appendChild(el('i'));
    d.appendChild(el('b', null, al.title));
    d.appendChild(el('span', null, al.detail || ''));
    box.appendChild(d);
  }

  const name = agentName(s);
  document.title = alerts.some((a) => a.level === 'critical')
    ? `● ALARM // ${name.toUpperCase()} INSIGHT`
    : `${name.toUpperCase()} // INSIGHT`;
}

/* ── wiring ────────────────────────────────────────────────────────────── */

/* ── "..." + tooltip on anything that got cut off ──────────────────────────────────────────────
   CSS does the ellipsis (text-overflow / line-clamp); this marks the element with the full text so
   the hover tip can show it. Runs after every render, on the classes that can truncate. Elements
   that already carry a title (paths) hand it over so there is one tooltip style, not two. */
const CLAMPABLE = '.why,.gsub,.ttl,.alarm span,.em,.eh,.srow span,.fold span,.td,.tn,.fp,.what,.idline,.tc u,.memnote,.usagegrid span,.teamrow .nm,.teamrow .ty,.teamrow .lt,.teamrow .num,.teamhead .tn';
function markOverflow() {
  for (const n of document.querySelectorAll(CLAMPABLE)) {
    const t = n.getAttribute('title');
    if (t) { n.dataset.tip = t; n.removeAttribute('title'); continue; }
    const cut = n.scrollWidth > n.clientWidth + 1 || n.scrollHeight > n.clientHeight + 1;
    if (cut) n.dataset.tip = n.textContent; else delete n.dataset.tip;
  }
}
(() => {
  const tip = $('tip');
  let cur = null;
  const place = (e) => {
    const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
    let x = e.clientX + pad, y = e.clientY + pad;
    if (x + w > innerWidth - 8) x = Math.max(8, e.clientX - w - pad);
    if (y + h > innerHeight - 8) y = Math.max(8, e.clientY - h - pad);
    tip.style.left = x + 'px'; tip.style.top = y + 'px';
  };
  document.addEventListener('mouseover', (e) => {
    const n = e.target.closest('[data-tip]');
    if (!n || n === cur) return;
    cur = n; tip.textContent = n.dataset.tip; tip.hidden = false; place(e);
  });
  document.addEventListener('mousemove', (e) => { if (cur) place(e); });
  document.addEventListener('mouseout', (e) => {
    if (cur && !cur.contains(e.relatedTarget)) { cur = null; tip.hidden = true; }
  });
})();

/* ── text that follows the window ───────────────────────────────────────────────────────────────────
   Every font size in style.css is `base px * var(--fs)`. --fs is 1 at 1280x720 (the size the layout was
   tuned at) and grows GENTLY with the smaller of the two window ratios: a third of the window's growth,
   capped at 1.4x. A bigger window should mostly buy MORE lines, not bigger ones. SCALE_GAIN and SCALE_MAX
   are the two knobs. Never below 1: smaller than 9 px is unreadable.
   Tier cards also get --tcs: each card is a fixed third of the ring's height, so its text shrinks (to 0.76x
   at most) until the header plus one whole hit fits, and any hit that would be sliced by the card's edge is
   hidden whole rather than shown cut in half. */
const SCALE_GAIN = 0.33, SCALE_MAX = 1.4;
function applyScale() {
  const s = Math.min(innerWidth / 1280, innerHeight / 720);
  const fs = Math.max(1, Math.min(SCALE_MAX, 1 + (s - 1) * SCALE_GAIN));
  document.documentElement.style.setProperty('--fs', fs.toFixed(3));
}

function fitTierCards(box) {
  for (const c of (box || document).querySelectorAll('.tc')) {
    let k = 1;
    c.style.setProperty('--tcs', '1');
    const hits = [...c.querySelectorAll('.hit')];
    for (const h of hits) h.style.display = '';
    const hb = c.querySelector('.hits');
    if (!hb) continue;
    const what = c.querySelector('.what'), who = c.querySelector('.who');
    for (const x of [what, who]) if (x) x.style.display = '';
    c.classList.remove('tight');                 // start every fit from the full layout
    // ONE limit for both the fit and the per-hit check: the hits box's bottom (it tracks the card padding,
    // including .tight), or the card edge when the header has pushed the hits box below it
    const bottom = () => Math.min(hb.getBoundingClientRect().bottom, c.getBoundingClientRect().bottom - 2);
    const need = () => {               // the header plus the newest hit's FILE line (or the empty line) must fit
      const first = hits[0] ? hits[0].querySelector('.op') : hb.firstElementChild;
      const end = first ? first.getBoundingClientRect().bottom : hb.getBoundingClientRect().top;
      return end > bottom() + 0.5;
    };
    // give up the least live text first: the static description, then a little size, then the per-agent
    // split (also in the TEAM panel), then more size. A hit's own "why" is dropped per hit below.
    if (need() && what) what.style.display = 'none';
    while (need() && k > 0.88) { k -= 0.04; c.style.setProperty('--tcs', k.toFixed(2)); }
    if (need() && who) who.style.display = 'none';
    if (need()) c.classList.add('tight');      // small window: drop padding and hit spacing before shrinking more
    while (need() && k > 0.76) { k -= 0.04; c.style.setProperty('--tcs', k.toFixed(2)); }
    const lim = bottom() + 0.5;
    let cut = false;
    for (const h of hits) {            // newest first: once one does not fit, the older ones go too
      const why = h.querySelector('.why');
      if (why) why.style.display = '';
      if (!cut && h.getBoundingClientRect().bottom > lim && why) why.style.display = 'none';   // keep the file line
      if (cut || h.getBoundingClientRect().bottom > lim) { h.style.display = 'none'; cut = true; }
    }
  }
}

// ?snap=1&fitdebug: after the page settles, write each tier card's final geometry into <pre id="fitdbg"> so a
// headless --dump-dom can check that no visible hit crosses its card's edge (the fit's own test harness)
if (new URLSearchParams(location.search).has('fitdebug')) {
  setTimeout(() => {
    // headless --dump-dom fires no animation frames, so run the fit directly before measuring
    try { fitTierCards(document.getElementById('tierCallouts')); } catch (e) { console.error('[insight] fit', e); }
    const out = [];
    for (const c of document.querySelectorAll('.tc')) {
      const cb = c.getBoundingClientRect();
      out.push(`T${c.dataset.tier} card ${cb.top.toFixed(1)}-${cb.bottom.toFixed(1)} tcs=${c.style.getPropertyValue('--tcs')} what=${c.querySelector('.what')?.style.display || 'show'}`);
      for (const h of c.querySelectorAll('.hit')) {
        if (h.style.display === 'none') { out.push('   hit hidden'); continue; }
        const r = h.getBoundingClientRect(), w = h.querySelector('.why');
        const vis = w && w.style.display !== 'none';
        out.push(`   hit ${r.top.toFixed(1)}-${r.bottom.toFixed(1)} why=${vis ? 'show' : 'hidden'} ${r.bottom > cb.bottom - 2 ? 'CROSSES-EDGE' : 'inside'}`);
      }
    }
    const pre = document.createElement('pre'); pre.id = 'fitdbg'; pre.textContent = out.join('\n'); document.body.appendChild(pre);
  }, 1500);
}

// the cards' height also changes when the ring art finishes loading or the layout reflows - refit then too
if (window.ResizeObserver) {
  let roQueued = false;
  new ResizeObserver(() => {
    if (roQueued || !last) return;
    roQueued = true;
    requestAnimationFrame(() => { roQueued = false; renderMemory(last); });
  }).observe(document.getElementById('tierCallouts'));
}

let resizeQueued = false;
addEventListener('resize', () => {
  if (resizeQueued) return;
  resizeQueued = true;
  requestAnimationFrame(() => {
    resizeQueued = false;
    applyScale();
    if (last) { renderMemory(last); requestAnimationFrame(markOverflow); }   // re-fits cards + redraws leaders
  });
});
applyScale();

function render(s) {
  last = s;
  updateBurn(s);
  AGENT_LABEL = agentName(s).toUpperCase();
  $('brand').textContent = AGENT_LABEL + '//INSIGHT';
  $('gAvatar').textContent = AGENT_LABEL.slice(0, 1);
  const tt = $('sessionTitle');
  tt.textContent = s.session.title || s.session.projectPath || '—';
  renderLedger(s); renderGate(s); renderActivity(s);
  renderUsage(s); renderLessons(s); renderSkills(s); renderTeam(s);
  if (typeof renderGraph === 'function') { try { renderGraph(s); } catch (e) { console.error('[insight] graph', e); } }
  renderTools(s); renderFiles(s); renderErrors(s); renderSessions(s); renderMemory(s); renderAlarms(s);
  requestAnimationFrame(markOverflow);
}

function setLink(state, bad) {
  const c = $('linkState');
  c.querySelector('em').textContent = state;
  c.classList.toggle('bad', !!bad);
}

/* ?snap=1 renders one frame and opens no stream. A live EventSource never lets a page reach
   "load complete", which hangs headless screenshotters (and any embed that waits for onload). */
const SNAP = new URLSearchParams(location.search).has('snap');

let es;
function connect() {
  if (SNAP) {
    fetch('/api/state').then((r) => r.json()).then((s) => { render(s); setLink('SNAP', false); });
    return;
  }
  es = new EventSource('/api/stream');
  es.onopen = () => setLink('ONLINE', false);
  es.onmessage = (ev) => { try { render(JSON.parse(ev.data)); } catch (e) { console.error(e); } };
  es.onerror = () => {
    setLink('RETRY', true);
    es.close();
    setTimeout(connect, 2000);
  };
}
connect();

setInterval(() => {
  $('clock').textContent = new Date().toTimeString().slice(0, 8);
  if (last) { renderGate(last); $('gSub').textContent = $('gSub').textContent; }
}, 1000);
