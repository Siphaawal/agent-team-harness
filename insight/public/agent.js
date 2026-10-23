/* AGENT DETAIL modal: click a node on the graph (or a TEAM row) to see exactly what that agent is doing now.

   Opens from a graph node (click, or Enter/Space when focused) or a TEAM table row. While open it
   polls GET /api/agent/<id> every 2 s, so /api/state stays lean and the modal is live. Closes on
   Esc, the X, or a click on the backdrop; focus returns to whatever opened it.

   PRIVACY: everything is written with textContent from fields the server builds one by one (lib/team.mjs
   detail()). The server never sends message bodies, summaries, spawn prompts, task descriptions,
   user-prompt text or command text, and this file never asks for them.

   Loaded after app.js (borrows $, el, hhmm, ago, kfmt, ROSTER_HEX). */

const AG = { id: null, timer: 0, opener: null, box: null, seq: 0 };

function agBuildShell() {
  if (AG.box) return AG.box;
  const back = el('div', 'agback');
  back.id = 'agentModal';
  back.hidden = true;
  const box = el('div', 'agmodal');
  box.setAttribute('role', 'dialog');
  box.setAttribute('aria-modal', 'true');
  box.setAttribute('aria-labelledby', 'agName');
  box.tabIndex = -1;
  const x = el('button', 'chip btn agx', 'X');
  x.title = 'Close (Esc)';
  x.setAttribute('aria-label', 'Close');
  x.addEventListener('click', agClose);
  box.appendChild(x);
  box.appendChild(el('div', 'agbody'));
  back.appendChild(box);
  back.addEventListener('mousedown', (e) => { if (e.target === back) agClose(); });
  document.body.appendChild(back);
  AG.box = back;
  return back;
}

function openAgent(id, opener) {
  if (!id) return;
  const back = agBuildShell();
  AG.id = id; AG.opener = opener || document.activeElement;
  back.hidden = false;
  const body = back.querySelector('.agbody');
  body.textContent = '';
  body.appendChild(el('div', 'empty', 'reading the agent…'));
  back.querySelector('.agmodal').focus();
  agFetch();
  clearInterval(AG.timer);
  AG.timer = setInterval(agFetch, 2000);
}

function agClose() {
  if (!AG.box || AG.box.hidden) return;
  AG.box.hidden = true;
  clearInterval(AG.timer); AG.timer = 0; AG.id = null;
  const o = AG.opener; AG.opener = null;
  if (o && typeof o.focus === 'function' && document.contains(o)) o.focus();
}

async function agFetch() {
  const id = AG.id, seq = ++AG.seq;
  if (!id) return;
  let d = null, err = null;
  try {
    const r = await fetch('/api/agent/' + encodeURIComponent(id), { cache: 'no-store' });
    d = await r.json();
    if (!r.ok) err = d && d.error ? d.error : 'HTTP ' + r.status;
  } catch (e) { err = 'link down - retrying'; }
  if (id !== AG.id || seq !== AG.seq) return;          // closed, or a newer poll already answered
  agRender(err ? null : d, err);
}

/* ── render ───────────────────────────────────────────────────────────────────────────────── */
const agColor = (d) => (d.lead ? '#ffe066' : (d.color && ROSTER_HEX[d.color]) || d.color || 'var(--cyan-hot)');
const agModel = (m) => (m ? String(m).replace(/^claude-/, '').replace(/\[1m\]$/i, ' 1M').toUpperCase() : '—');

function agSection(title, into) {
  const s = el('section', 'agsec');
  s.appendChild(el('h3', null, title));
  into.appendChild(s);
  return s;
}
function agKV(into, k, v, cls) {
  const r = el('div', 'agkv');
  r.appendChild(el('span', null, k));
  r.appendChild(el('b', cls || null, v));
  into.appendChild(r);
  return r;
}

function agStatusLine(d) {
  const now = d.at || Date.now();
  if (d.status === 'done') return `DONE${d.doneAt ? ' · ' + ago(now - d.doneAt) + ' ago' : ''}`;
  if (d.status === 'pending-followup' && !d.stale) return `FOLLOW-UP PENDING · a lead message is newer than its last tool call (quiet ${ago(d.idleMs)})`;
  if (d.status === 'handed-in' && !d.stale) return `HANDED IN · its last message to the lead is newer than its last other tool call (quiet ${ago(d.idleMs)})`;
  if (d.stale) return `STALE · quiet ${ago(d.idleMs)} (earlier run)`;
  if (d.status === 'running') return `RUNNING · last move ${ago(d.idleMs)} ago`;
  return `IDLE · for ${ago(d.idleMs)}`;
}

function agRender(d, err) {
  const body = AG.box.querySelector('.agbody');
  body.textContent = '';
  if (!d) { body.appendChild(el('div', 'empty warn', err || 'no data')); return; }
  const col = agColor(d);
  AG.box.querySelector('.agmodal').style.setProperty('--c', col);

  // header: who, what kind, what state and for how long
  const head = el('header', 'aghead');
  const dot = el('i', 'dot ' + (d.stale ? 'stale' : statusBase(d.status)));
  head.appendChild(dot);
  const nm = el('h2', null, d.lead ? AGENT_LABEL : d.name);
  nm.id = 'agName';
  nm.style.color = col;
  head.appendChild(nm);
  head.appendChild(el('span', 'agtag', (d.lead ? 'LEAD · ' + d.name : d.kind.toUpperCase()) + ' · ' + agModel(d.model)));
  body.appendChild(head);
  const st = el('div', 'agstate ' + (d.stale ? 'stale' : statusBase(d.status)), agStatusLine(d));
  body.appendChild(st);
  body.appendChild(el('div', 'agsub', `started ${hhmm(d.startedAt)} · elapsed ${ago(d.elapsedMs)} · live, refreshed ${hhmm(d.at)}`));

  // NOW: the current or last tool
  const nowBox = el('div', 'agnow');
  nowBox.appendChild(el('span', 'k', 'NOW'));
  if (d.now) {
    nowBox.appendChild(el('b', null, d.now.tool.replace(/^mcp__/, '')));
    if (d.now.label) nowBox.appendChild(el('span', 'lbl', d.now.label));
    nowBox.appendChild(el('span', 'ago', ago((d.at || Date.now()) - d.now.at) + ' ago'
      + (d.now.ok === false ? ' · FAILED' : d.now.ok == null ? ' · in flight' : '')));
  } else nowBox.appendChild(el('span', 'lbl', 'no tool call yet'));
  body.appendChild(nowBox);

  const grid = el('div', 'aggrid');
  body.appendChild(grid);
  const left = el('div', 'agcol'), right = el('div', 'agcol');
  grid.appendChild(left); grid.appendChild(right);

  // recent activity
  const ra = agSection('RECENT ACTIVITY · LAST ' + d.acts.length, left);
  if (!d.acts.length) ra.appendChild(el('div', 'empty', 'no tool calls'));
  for (const a of d.acts) {
    const r = el('div', 'agact' + (a.ok === false ? ' bad' : ''));
    r.appendChild(el('span', 't', hhmm(a.at)));
    r.appendChild(el('b', null, a.tool.replace(/^mcp__/, '')));
    const l = el('span', 'l', a.label || '');
    if (a.label) l.dataset.tip = a.label;
    r.appendChild(l);
    ra.appendChild(r);
  }

  // context, tools, errors
  const cx = agSection('CONTEXT & TOOLS', right);
  const pct = d.context.limit ? (d.context.tokens / d.context.limit) * 100 : 0;
  const cw = d.context.warn;      // over d.context.warnAt (300k) tokens: amber
  const cxRow = agKV(cx, 'CONTEXT WINDOW', `${kfmt(d.context.tokens)} of ${kfmt(d.context.limit)} · ${pct.toFixed(1)}%${cw ? ' · OVER ' + kfmt(d.context.warnAt) : ''}`);
  if (cw) cxRow.querySelector('b').style.color = 'var(--amber)';
  const m = el('div', 'meter'); const mi = el('i', cw || pct > 65 ? (pct > 85 ? 'crit' : 'warn') : ''); mi.style.width = Math.min(100, pct) + '%';
  m.appendChild(mi); cx.appendChild(m);
  agKV(cx, 'TOOL CALLS', String(d.toolCalls) + (d.tools.length ? '  ·  ' + d.tools.slice(0, 4).map((t) => `${t.name.replace(/^mcp__/, '')} ${t.count}`).join(', ') : ''));
  agKV(cx, 'ERRORS', d.errors.count ? `${d.errors.count} · last ${d.errors.last ? d.errors.last.tool + ' ' + hhmm(d.errors.last.at) : '—'}` : 'none', d.errors.count ? 'bad' : null);

  // memory and skills
  const ms = agSection('MEMORY & SKILLS', right);
  agKV(ms, 'TIER 0 / 1 / 2 / AGENT', `${d.memory.t0} / ${d.memory.t1} / ${d.memory.t2} / ${d.memory.t3 || 0}`);
  if (!d.lead) {
    agKV(ms, 'ROLE', d.role ? d.role + (d.defined ? ' (own definition + memory)' : ' (built-in)') : '— none recorded');
    agKV(ms, 'CORE', !d.core ? 'NOT delivered - no SubagentStart attachment'
      : d.core.ok ? `agent core v${d.core.version} · fired ${d.core.count}×` : `hook FAILED ${d.core.failed}×`);
  }
  for (const r of d.memory.recent) {
    const row = el('div', 'agline');
    row.appendChild(el('span', 't', hhmm(r.at).slice(0, 5)));
    row.appendChild(el('span', 'op', `T${r.tier} ${r.op}`));
    row.appendChild(el('span', 'l', r.file));
    ms.appendChild(row);
  }
  const sk = [...d.skills.invoked.map((k) => `${k.skill} (invoked)`), ...d.skills.touched.map((k) => `${k.skill} ×${k.touches}`)];
  agKV(ms, 'SKILLS', sk.length ? sk.slice(0, 6).join(' · ') : 'none');

  // files
  const fs = agSection(`FILES · ${d.files.counts.edited} edited · ${d.files.counts.read} read`, right);
  const fileRows = (label, list) => {
    if (!list.length) return;
    fs.appendChild(el('div', 'agmini', label));
    for (const f of list) {
      const row = el('div', 'agline');
      row.appendChild(el('span', 't', hhmm(f.at).slice(0, 5)));
      const n = el('span', 'l', f.name);
      n.dataset.tip = f.dir;                 // the folder on hover, in the dashboard's own tip
      row.appendChild(n);
      fs.appendChild(row);
    }
  };
  fileRows('EDITED', d.files.edited);
  fileRows('READ', d.files.read);
  if (!d.files.edited.length && !d.files.read.length) fs.appendChild(el('div', 'empty', 'no files'));

  // messages: who and when, never what
  const mg = agSection(`MESSAGES · ${d.messages.sent} sent · ${d.messages.received} received`, right);
  for (const e of d.messages.recent) {
    const row = el('div', 'agline');
    row.appendChild(el('span', 't', hhmm(e.at).slice(0, 5)));
    row.appendChild(el('span', 'l', `${e.from} -> ${e.to}`));
    mg.appendChild(row);
  }
  if (!d.messages.recent.length) mg.appendChild(el('div', 'empty', 'none in the last ' + d.messages.window + ' team messages'));
  mg.appendChild(el('div', 'agmini', `counted over the team's last ${d.messages.window} messages · content is never shown`));
}

/* ── wiring: graph nodes, TEAM rows, keyboard ─────────────────────────────────────────────── */
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && AG.box && !AG.box.hidden) { e.preventDefault(); agClose(); return; }
  const t = e.target && e.target.closest && e.target.closest('[data-agent]');
  if (t && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); openAgent(t.dataset.agent, t); }
});
document.addEventListener('click', (e) => {
  const t = e.target && e.target.closest && e.target.closest('[data-agent]');
  if (t && !(AG.box && AG.box.contains(t))) openAgent(t.dataset.agent, t);
});
// keep Tab inside the dialog while it is open
document.addEventListener('focusin', (e) => {
  if (AG.box && !AG.box.hidden && !AG.box.contains(e.target)) AG.box.querySelector('.agmodal').focus();
});
// ?agent=<id> opens the modal on load (for looking at it, and for headless screenshots)
(() => {
  const q = new URLSearchParams(location.search).get('agent');
  if (!q) return;
  let tries = 0;
  const go = () => {
    if (q === 'lead' && !(last && last.team)) { if (++tries < 40) setTimeout(go, 150); return; }
    openAgent(q === 'lead' ? last.team.lead.id : q);
  };
  setTimeout(go, 300);
})();
