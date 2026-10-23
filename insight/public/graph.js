/* TEAM GRAPH: the agent team as a live tree - the lead agent at the root, every
   teammate a child in its roster colour, plain subagents (the librarian, forks) as small leaves.

   Loaded AFTER app.js and called from its render(); it borrows app.js's top-level helpers ($, el,
   hhmm, ago, shortModel, kfmt, agentColor). Two layers inside #tgraph:
     svg     nodes, edges, labels - rebuilt only when the cast or the panel size changes, otherwise
             patched in place so the CSS animations (pulse, settle) are not restarted every push
     canvas  everything that moves: a drifting starfield, a slow sweep around the lead, particles
             along the edges and the orbit/ring flashes. Throttled to ~20 fps when nothing travels,
             full rate only while a particle is in flight; nothing at all under reduced motion.

   What makes things move - all of it is numbers the server already sends, nothing is invented:
     team.edges            a SendMessage between two agents -> a bright particle from sender to
                           recipient (via the lead when neither end is the lead). Only from, to and
                           the time exist on the wire: message text never reaches this page.
     agent.toolCalls rose  -> a small spark from the agent up its edge to the lead
     memory.count rose     -> gold dots orbit the node and a ring flashes
     skills rose           -> the same in white-green
   ?replay=N on the URL replays the last N message edges on load (for looking at the effect). */

const TG = {
  nodes: new Map(),          // name -> node {name, lead, kind, x, y, r, color, g, path:{p0,c,p1}}
  sig: '', W: 0, H: 0, dpr: 1,
  prev: new Map(),           // name -> {tools, mem, skl}
  seenAt: 0, first: true,
  particles: [], flashes: [], stars: [],
  raf: 0, lastDraw: 0, t0: performance.now(),
  reduce: matchMedia('(prefers-reduced-motion: reduce)').matches,
  lastState: null,
};
const SVGNS = 'http://www.w3.org/2000/svg';
const sv = (tag, attrs, parent) => {
  const n = document.createElementNS(SVGNS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(n);
  return n;
};
const clip = (s, n) => { s = String(s || ''); return s.length <= n ? s : s.slice(0, n - 1) + '…'; };
const modelTag = (m) => {
  if (!m) return '';
  const s = String(m).replace(/^claude-/, '').replace(/\[1m\]$/i, '').replace(/-(\d)-(\d).*$/, ' $1.$2');
  return s.toUpperCase();
};
const ctxk = (n) => (n >= 1e6 ? (n / 1e6).toFixed(2) + 'M' : Math.round((n || 0) / 1e3) + 'k');
const LINE_MAX = 22;          // label lines 2 and 3 are clipped to this, so a label's box is stable

/* ── label placement ──────────────────────────────────────────────────────────────────────────
   Each label tries four spots - outward above/below, the other vertical side, then beside the node
   (outward side first) - and takes the first whose box hits no node and no label already placed.
   If every spot collides it takes the one with the least overlap. Box sizes are estimated from the
   mono font (fixed width), with lines clipped to LINE_MAX, so placement does not jitter per push. */
function tgPlaceLabels(list, W, H) {
  // label text scales with the window (style.css font sizes are px * --fs, set by app.js applyScale), so the
  // line heights, glyph widths and offsets below scale with it, or big screens stack lines on top of each other
  const FS = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--fs')) || 1;
  const boxes = [];
  const hit = (a, b) => Math.max(0, Math.min(a.x2, b.x2) - Math.max(a.x1, b.x1)) * Math.max(0, Math.min(a.y2, b.y2) - Math.max(a.y1, b.y1));
  for (const n of list) boxes.push({ x1: n.x - n.r - 3, x2: n.x + n.r + 3, y1: n.y - n.r - 3, y2: n.y + n.r + 3, node: true, self: n });
  const order = [...list].sort((a, b) => (b.lead ? 1 : 0) - (a.lead ? 1 : 0) || (a.kind === 'subagent') - (b.kind === 'subagent'));
  // compact: too little panel per node (a small screen, a big team) - teammates drop the third line
  // (tool + ctx); both stay in the hover tip and the context ring
  // and when it is tighter still, every child shows its name only (status still reads from the
  // node's pulse / dim / check mark, everything else from the tip)
  // density counts the LIVE nodes only - stale ones show no label until hovered
  const liveN = list.filter((n) => !n.lead && !n.stale).length;
  const density = (W * H) / Math.max(1, liveN) / (FS * FS);
  const compact = density < 26000, tight = density < 20000;
  for (const n of order) {
    const sub = n.kind === 'subagent';
    if (n.stale) {
      // hover-only name, outward of the node; it blocks nothing because it is normally invisible
      const right = Math.cos(n.ang) >= 0, below = Math.sin(n.ang) > 0.6, above = Math.sin(n.ang) < -0.6;
      n.label = above ? { anchor: 'middle', x: 0, y0: -(n.r + 5 * FS), lh: 10 * FS, lines: 1 }
        : below ? { anchor: 'middle', x: 0, y0: n.r + 11 * FS, lh: 10 * FS, lines: 1 }
          : { anchor: right ? 'end' : 'start', x: right ? -(n.r + 5) : n.r + 5, y0: 3 * FS, lh: 10 * FS, lines: 1 };
      continue;
    }
    const lines = n.lead ? 3 : tight ? 1 : sub || compact ? 2 : 3, lh = (sub ? 10 : 11.5) * FS;
    const nameW = (n.lead ? 11 : Math.min(18, n.name.length)) * (sub ? 6.4 : n.lead ? 9.6 : 7.4) * FS;
    const w = lines === 1 ? nameW : Math.max(nameW, (sub ? 16 : LINE_MAX) * (sub ? 5.6 : 5.9) * FS);
    const h = lines * lh;
    const up = !n.lead && Math.sin(n.ang) < -0.35;
    const right = Math.cos(n.ang) >= 0;
    const vert = (above, shift = 0) => ({ anchor: 'middle', x: shift, y0: above ? -(n.r + 6) - (lines - 1) * lh : n.r + 13 * FS,
      box: { x1: n.x + shift - w / 2, x2: n.x + shift + w / 2, y1: above ? n.y - n.r - 5 - h : n.y + n.r + 3, y2: above ? n.y - n.r - 5 : n.y + n.r + 3 + h } });
    const side = (r) => ({ anchor: r ? 'start' : 'end', x: r ? n.r + 7 : -(n.r + 7), y0: -h / 2 + lh * 0.8,
      box: { x1: r ? n.x + n.r + 5 : n.x - n.r - 5 - w, x2: r ? n.x + n.r + 5 + w : n.x - n.r - 5, y1: n.y - h / 2, y2: n.y + h / 2 } });
    const sh = w * 0.35;
    const cands = n.lead ? [vert(false)] : [vert(up), vert(!up), side(right), side(!right),
      vert(up, sh), vert(up, -sh), vert(!up, sh), vert(!up, -sh)];
    let best = null, bestCost = Infinity;
    for (const c of cands) {
      const b = c.box;
      let cost = 0;
      // covering a node is worse than grazing a label
      for (const o of boxes) { if (o.node && o.self === n) continue; cost += hit(b, o) * (o.node ? 3 : 1); }
      // leaving the panel costs as much as overlapping a label of the same size
      cost += (Math.max(0, -b.x1) + Math.max(0, b.x2 - W)) * h + (Math.max(0, -b.y1) + Math.max(0, b.y2 - H)) * w;
      if (cost < bestCost) { best = c; bestCost = cost; }
      if (cost === 0) break;
    }
    n.label = { anchor: best.anchor, x: best.x, y0: best.y0, lh, lines };
    boxes.push(best.box);
  }
}

/* ── layout ───────────────────────────────────────────────────────────────────────────────────
   Root in the middle, teammates on an ellipse that fills the panel. Past 7 teammates every other
   one drops to an inner ellipse, so neighbours on the same ring are two steps apart and the labels
   (which sit on the OUTWARD side of each node, away from its edge) cannot collide. Subagents are
   small leaves on a tight arc above the root, clear of the root's own label below it. */
/* STALE agents (quiet past team.staleMs, i.e. earlier runs) are small dim dots on the
   OUTERMOST ellipse with their name on hover only; the live team's ellipse pulls in to make room,
   so tonight's team owns the space. */
const tgRings = (W, H, hasStale) => {
  const rx = Math.max(60, W / 2 - 70), ry = Math.max(60, H / 2 - 52);
  return { rx, ry, live: hasStale ? 0.78 : 1, srx: W / 2 - 14, sry: H / 2 - 14 };
};
function tgLayout(list, W, H) {
  const cx = W / 2, cy = H / 2;
  const tm = list.filter((n) => !n.lead && !n.stale && n.kind !== 'subagent');
  const sub = list.filter((n) => !n.lead && !n.stale && n.kind === 'subagent');
  const old = list.filter((n) => !n.lead && n.stale);
  const R = tgRings(W, H, old.length > 0);
  const rx = R.rx * R.live, ry = R.ry * R.live;
  const stagger = tm.length > 7;
  const lead = list.find((n) => n.lead);
  if (lead) { lead.x = cx; lead.y = cy; lead.r = Math.max(24, Math.min(40, Math.min(W, H) * 0.065)); lead.ang = Math.PI / 2; }
  tm.forEach((n, i) => {
    const a = -Math.PI / 2 + (i / Math.max(1, tm.length)) * Math.PI * 2;
    const f = stagger && i % 2 ? 0.6 : 1;
    n.x = cx + Math.cos(a) * rx * f; n.y = cy + Math.sin(a) * ry * f; n.r = 12; n.ang = a;
  });
  // subagents: an arc over the root, widening as more arrive
  const span = Math.min(Math.PI * 1.1, 0.5 + sub.length * 0.32);
  sub.forEach((n, i) => {
    const a = -Math.PI / 2 + (sub.length === 1 ? 0 : (i / (sub.length - 1) - 0.5) * span);
    const f = (0.34 + (i % 2) * 0.07) / Math.max(0.85, R.live);
    n.x = cx + Math.cos(a) * rx * f; n.y = cy + Math.sin(a) * ry * f; n.r = 6; n.ang = a;
  });
  // stale: evenly round the outer ring, half a step off the top so none sits on the live top node
  old.forEach((n, i) => {
    const a = -Math.PI / 2 + ((i + 0.5) / old.length) * Math.PI * 2;
    n.x = cx + Math.cos(a) * R.srx; n.y = cy + Math.sin(a) * R.sry; n.r = 4.5; n.ang = a;
  });
  // each child's edge: a quadratic curve bowed clockwise, so the tree reads as branches not spokes
  for (const n of list) {
    if (n.lead || !lead) continue;
    const dx = n.x - lead.x, dy = n.y - lead.y, len = Math.hypot(dx, dy) || 1;
    const ux = dx / len, uy = dy / len;
    const p0 = { x: lead.x + ux * (lead.r + 4), y: lead.y + uy * (lead.r + 4) };
    const p1 = { x: n.x - ux * (n.r + 3), y: n.y - uy * (n.r + 3) };
    const bow = len * 0.14;
    const c = { x: (p0.x + p1.x) / 2 - uy * bow, y: (p0.y + p1.y) / 2 + ux * bow };
    n.path = { p0, c, p1 };
  }
}

const bez = (p, t) => {
  const u = 1 - t;
  return { x: u * u * p.p0.x + 2 * u * t * p.c.x + t * t * p.p1.x, y: u * u * p.p0.y + 2 * u * t * p.c.y + t * t * p.p1.y };
};
const rev = (p) => ({ p0: p.p1, c: p.c, p1: p.p0 });

/* ── the cast ─────────────────────────────────────────────────────────────────────────────── */
function tgCast(s) {
  const t = s.team;
  if (!t || !t.lead) return [];
  const roster = (t.config && t.config.members) || [];
  const order = new Map(roster.map((m, i) => [m.name, i]));
  // roster order is stable; the server's running/idle/done sort would make nodes jump around
  const agents = [...(t.agents || [])].sort((a, b) => {
    const ia = order.has(a.name) ? order.get(a.name) : 1e6, ib = order.has(b.name) ? order.get(b.name) : 1e6;
    return ia - ib || (a.startedAt || 0) - (b.startedAt || 0);
  });
  const list = [{ ...t.lead, lead: true, kind: 'lead', label: AGENT_LABEL }];
  // One node per AGENT ID. Only a roster teammate's re-spawn collapses (same roster name = same
  // seat: the newest transcript wins); unnamed or off-roster agents are never merged by name.
  const seen = new Set([t.lead.name]);
  for (const a of agents) {
    let node = a;
    if (order.has(a.name)) {
      if (seen.has(a.name)) continue;
      seen.add(a.name);
      node = agents.filter((x) => x.name === a.name).sort((x, y) => (y.startedAt || 0) - (x.startedAt || 0))[0];
    } else if (seen.has(a.name)) continue;      // the server already made off-roster names unique
    else seen.add(a.name);
    list.push({ ...node, kind: node.kind === 'subagent' ? 'subagent' : 'teammate', label: node.name, stale: !!node.stale });
  }
  return list;
}

/* ── svg build + patch ────────────────────────────────────────────────────────────────────── */
function tgBuild(list) {
  const svg = $('tgsvg');
  svg.textContent = '';
  svg.setAttribute('viewBox', `0 0 ${TG.W} ${TG.H}`);
  const defs = sv('defs', {}, svg);
  const lead = list.find((n) => n.lead);

  // faint orbit guides, so the rings read as structure even when the team is quiet
  const guides = sv('g', { class: 'tgguides' }, svg);
  if (lead) {
    const hasStale = list.some((n) => n.stale);
    const R = tgRings(TG.W, TG.H, hasStale);
    const rx = R.rx * R.live, ry = R.ry * R.live;
    sv('ellipse', { cx: lead.x, cy: lead.y, rx, ry }, guides);
    if (list.filter((n) => n.kind === 'teammate' && !n.stale).length > 7) sv('ellipse', { cx: lead.x, cy: lead.y, rx: rx * 0.6, ry: ry * 0.6 }, guides);
    if (hasStale) sv('ellipse', { class: 'old', cx: lead.x, cy: lead.y, rx: R.srx, ry: R.sry }, guides);
  }
  const edges = sv('g', { class: 'tgedges' }, svg);
  const nodes = sv('g', { class: 'tgnodes' }, svg);
  TG.nodes = new Map();
  for (const n of list) {
    const node = { name: n.name, lead: n.lead, kind: n.kind, x: n.x, y: n.y, r: n.r, path: n.path, ang: n.ang };
    if (!n.lead && n.path) {
      const { p0, c, p1 } = n.path;
      node.edge = sv('path', { class: 'tgedge ' + n.kind, d: `M${p0.x},${p0.y} Q${c.x},${c.y} ${p1.x},${p1.y}` }, edges);
    }
    // every node opens the AGENT DETAIL modal (public/agent.js): click, or Enter/Space when focused
    const g = sv('g', { class: 'tgn ' + n.kind, transform: `translate(${n.x},${n.y})`, tabindex: 0, role: 'button',
      'data-agent': n.id || '', 'aria-label': `${n.lead ? AGENT_LABEL.toLowerCase() + ', lead' : n.name}: open agent detail` }, nodes);
    node.g = g;
    if (n.lead) {
      // the gate's own Blender-baked ring (already cached by the page) as the root's emblem
      const er = n.r * 1.9;
      sv('image', { class: 'emb', href: 'assets/ring_inner.png', x: -er, y: -er, width: er * 2, height: er * 2 }, g);
      sv('circle', { class: 'halo', r: n.r + 7 }, g);
      sv('circle', { class: 'ctx', r: n.r + 3, transform: 'rotate(-90)' }, g);
      sv('circle', { class: 'av', r: n.r }, g);
      sv('circle', { class: 'rim', r: n.r }, g);
    } else if (n.stale) {
      sv('circle', { class: 'hit', r: 11 }, g);          // a bigger hover target than the dot
      sv('circle', { class: 'core', r: n.r }, g);
    } else {
      sv('circle', { class: 'halo', r: n.r + 6 }, g);
      if (n.kind === 'teammate') sv('circle', { class: 'ctx', r: n.r + 3, transform: 'rotate(-90)' }, g);
      sv('circle', { class: 'core', r: n.r }, g);
      sv('path', { class: 'check', d: n.kind === 'subagent' ? 'M-3,0 L-1,2.5 L3.2,-2.6' : 'M-5,0 L-1.5,4 L5.5,-4.5' }, g);
    }
    // the spot tgPlaceLabels picked: outward above/below, or beside the node
    const L = n.label;
    const lab = sv('text', { class: 'lab', y: L.y0, 'text-anchor': L.anchor }, g);
    node.t1 = sv('tspan', { class: 'l1', x: L.x, dy: 0 }, lab);
    if (L.lines > 1) node.t2 = sv('tspan', { class: 'l2', x: L.x, dy: L.lh }, lab);
    if (L.lines > 2) node.t3 = sv('tspan', { class: 'l3', x: L.x, dy: L.lh }, lab);
    TG.nodes.set(n.name, node);
  }
}

function tgPatch(s, list) {
  const limit = (s.session && s.session.contextLimit) || 1000000;
  const recentMsg = new Map();
  const now = s.now || Date.now();
  for (const e of (s.team.edges || [])) {
    if (now - (e.at || 0) > 90000) continue;
    recentMsg.set(e.from, true); recentMsg.set(e.to, true);
  }
  for (const n of list) {
    const node = TG.nodes.get(n.name);
    if (!node) continue;
    const col = n.lead ? '#ffe066' : agentColor(s, n.name);
    node.color = col;
    const st = n.stale ? ' stale' : '';
    const base = statusBase(n.status);           // handed-in draws as done, pending-followup as idle (amber below)
    node.g.setAttribute('class', `tgn ${n.kind} ${base} ${n.status}${st}${n.errors ? ' err' : ''}`);
    const core = node.g.querySelector('.core');
    if (core) core.style.stroke = n.status === 'pending-followup' ? '#ffb000' : '';   // amber: a follow-up the agent has not acted on
    node.g.style.setProperty('--c', col);
    if (node.edge) {
      node.edge.setAttribute('class', `tgedge ${n.kind} ${base}${st}${recentMsg.has(n.name) ? ' hot' : ''}`);
      node.edge.style.setProperty('--c', col);
    }
    // context ring: the agent's live window against the session's limit
    const ring = node.g.querySelector('.ctx');
    if (ring) {
      const rr = Number(ring.getAttribute('r')), circ = 2 * Math.PI * rr;
      const frac = Math.min(1, (n.contextTokens || 0) / limit);
      ring.setAttribute('stroke-dasharray', `${(circ * frac).toFixed(1)} ${circ.toFixed(1)}`);
      // past 300k tokens the ring turns amber and thick: a long context is a cost and quality risk
      const warn = n.ctxWarn || (n.contextTokens || 0) > CTX_WARN;
      ring.style.stroke = warn ? '#ffb000' : ''; ring.style.strokeWidth = warn ? '3' : ''; ring.style.opacity = warn ? '.95' : '';
    }
    const status = (n.status || '').toUpperCase().replace('-', ' ');
    // an unnamed agent shows 'type tail' (the tail of its id keeps four look-alikes apart); the type was the old label
    const title = n.lead ? AGENT_LABEL : n.named === false ? clip(n.type || 'agent', 12) + ' ' + n.name.slice(-4) : clip(n.name, 18);
    const tool = n.lastTool ? n.lastTool.name.replace(/^mcp__/, '').replace(/__/g, ' ') : '—';
    if (n.lead) {
      node.t1.textContent = AGENT_LABEL;
      node.t2.textContent = `LEAD · ${modelTag(n.model) || '—'} · ${status}`;
      node.t3.textContent = `${clip(tool, 14)} · ctx ${ctxk(n.contextTokens)}`;
    } else if (n.kind === 'subagent') {
      node.t1.textContent = title;
      if (node.t2) node.t2.textContent = clip(status === 'DONE' || status === 'HANDED IN' ? status : `${status} · ${tool}`, 16);
    } else {
      node.t1.textContent = title;
      if (node.t2) node.t2.textContent = clip(`${modelTag((n.member && n.member.model) || n.model) || '—'} · ${status}`, LINE_MAX);
      if (node.t3) node.t3.textContent = clip(`${clip(tool, 12)} · ctx ${ctxk(n.contextTokens)}`, LINE_MAX);
    }
    // full detail in the shared hover tip - attribution of memory, skills and files lives here too
    const mem = n.memory || { t0: 0, t1: 0, t2: 0, count: 0 };
    const skl = n.skills || { invoked: [], touched: [], touches: 0 };
    const fl = n.files || { edited: 0, read: 0, searched: 0 };
    node.g.dataset.tip = [
      `${n.lead ? AGENT_LABEL + ' (' + n.name + ')' : n.name} · ${n.kind.toUpperCase()} · ${status}${n.stale ? " · EARLIER RUN (quiet " + ago(n.idleMs) + ")" : ""}`,
      `model ${(n.member && n.member.model) || n.model || '—'} · up ${ago(n.elapsedMs)} · quiet ${ago(n.idleMs)}`,
      `context ${kfmt(n.contextTokens)} of ${kfmt(limit)} (${((n.contextTokens || 0) / limit * 100).toFixed(1)}%)`,
      `tools ${n.toolCalls}${n.errors ? ' · ' + n.errors + ' failed' : ''}` + (n.tools && n.tools.length ? ' - ' + n.tools.slice(0, 4).map((x) => `${x.name.replace(/^mcp__/, '')} ${x.count}`).join(', ') : ''),
      `memory t0/t1/t2 ${mem.t0}/${mem.t1}/${mem.t2}` + (mem.last ? ` · last ${mem.last.op} ${mem.last.file}` : ''),
      `skills ${skl.invoked.length} invoked, ${skl.touches} file touches` + (skl.touched.length ? ' - ' + skl.touched.slice(0, 3).map((k) => k.skill).join(', ') : ''),
      `files ${fl.edited} edited · ${fl.read} read · ${fl.searched} searched`,
      n.lastTool ? `last ${hhmm(n.lastTool.at)} ${n.lastTool.name} ${n.lastTool.detail && n.lastTool.detail !== n.lastTool.name ? n.lastTool.detail : ''}` : 'no tool yet',
    ].join('\n');
  }
}

/* ── events: what changed since the last push ─────────────────────────────────────────────── */
function tgRoute(from, to) {
  const a = TG.nodes.get(from), b = TG.nodes.get(to);
  if (!a || !b || a === b) return null;
  if (a.lead) return b.path ? [b.path] : null;
  if (b.lead) return a.path ? [rev(a.path)] : null;
  return a.path && b.path ? [rev(a.path), b.path] : null;   // teammate -> teammate goes through the root
}
function tgSpark(segs, color, kind) {
  if (!segs || TG.reduce) return;
  if (TG.particles.length > 140) TG.particles.shift();
  const per = kind === 'msg' ? 1300 : 950;
  TG.particles.push({ segs, color, kind, t0: performance.now() + (kind === 'msg' ? 0 : Math.random() * 400), dur: per * segs.length });
}
function tgFlash(node, color, kind) {
  if (!node || TG.reduce) return;
  TG.flashes.push({ node, color, kind, t0: performance.now(), dur: kind === 'ring' ? 1100 : 5200 });
  if (TG.flashes.length > 60) TG.flashes.shift();
}

function tgEvents(s, list) {
  const edges = s.team.edges || [];
  const newest = edges.length ? edges[edges.length - 1].at || 0 : 0;
  if (TG.first) {
    // on load: replay what passed in the last two minutes (or ?replay=N), staggered, so the graph
    // opens alive instead of waiting for the next message
    const q = Number(new URLSearchParams(location.search).get('replay')) || 0;
    const now = s.now || Date.now();
    const back = q ? edges.slice(-q) : edges.filter((e) => now - (e.at || 0) < 120000).slice(-8);
    back.forEach((e, i) => setTimeout(() => tgSpark(tgRoute(e.from, e.to), TG.nodes.get(e.from)?.color || '#39ff14', 'msg'), 250 + i * 450));
    TG.seenAt = newest;
  } else {
    for (const e of edges) {
      if ((e.at || 0) <= TG.seenAt) continue;
      tgSpark(tgRoute(e.from, e.to), TG.nodes.get(e.from)?.color || '#39ff14', 'msg');
    }
    TG.seenAt = Math.max(TG.seenAt, newest);
  }
  for (const n of list) {
    const node = TG.nodes.get(n.name);
    const cur = { tools: n.toolCalls || 0, mem: (n.memory && n.memory.count) || 0,
      skl: n.skills ? n.skills.invoked.length + n.skills.touches : 0 };
    const was = TG.prev.get(n.name);
    TG.prev.set(n.name, cur);
    if (!was || TG.first || !node) continue;
    if (cur.tools > was.tools && !n.lead && node.path) {
      const k = Math.min(3, cur.tools - was.tools);
      for (let i = 0; i < k; i++) tgSpark([rev(node.path)], node.color, 'tool');
    }
    if (cur.tools > was.tools && n.lead) tgFlash(node, '#ffe066', 'ring');
    if (cur.mem > was.mem) { tgFlash(node, '#ffe066', 'orbit'); tgFlash(node, '#ffe066', 'ring'); }
    if (cur.skl > was.skl) { tgFlash(node, '#dfffd6', 'orbit'); tgFlash(node, '#9dff8a', 'ring'); }
  }
  TG.first = false;
}

/* ── canvas: starfield, sweep, particles, flashes ─────────────────────────────────────────── */
function tgSeedStars() {
  const n = Math.round(Math.min(160, (TG.W * TG.H) / 2600));
  TG.stars = Array.from({ length: n }, () => ({
    x: Math.random() * TG.W, y: Math.random() * TG.H, z: 0.25 + Math.random() * 0.75, p: Math.random() * Math.PI * 2,
  }));
}
function tgDraw(now) {
  const c = $('tgfx');
  if (!c || !TG.W) return;
  const ctx = c.getContext('2d');
  ctx.setTransform(TG.dpr, 0, 0, TG.dpr, 0, 0);
  ctx.clearRect(0, 0, TG.W, TG.H);
  const t = (now - TG.t0) / 1000;

  // drifting stars: slow, parallax by depth, a faint twinkle
  for (const st of TG.stars) {
    const x = TG.reduce ? st.x : (st.x - t * 4 * st.z + TG.W * 10) % TG.W;
    const y = TG.reduce ? st.y : (st.y + Math.sin(t * 0.1 + st.p) * 3 * st.z);
    const a = 0.18 + 0.4 * st.z * (TG.reduce ? 1 : 0.6 + 0.4 * Math.sin(t * 1.3 + st.p));
    ctx.fillStyle = `rgba(157,255,138,${a.toFixed(3)})`;
    ctx.fillRect(x, y, st.z > 0.8 ? 1.6 : 1, st.z > 0.8 ? 1.6 : 1);
  }
  const lead = [...TG.nodes.values()].find((n) => n.lead);
  // the radar sweep: one turn every 16 s around the root
  if (lead && !TG.reduce && ctx.createConicGradient) {
    const ang = (t / 16) * Math.PI * 2;
    const R = Math.hypot(TG.W, TG.H) / 2;
    const g = ctx.createConicGradient(ang, lead.x, lead.y);
    g.addColorStop(0, 'rgba(57,255,20,0.055)');
    g.addColorStop(0.07, 'rgba(57,255,20,0.0)');
    g.addColorStop(1, 'rgba(57,255,20,0.0)');
    ctx.fillStyle = g;
    ctx.beginPath(); ctx.arc(lead.x, lead.y, R, 0, Math.PI * 2); ctx.fill();
  }
  if (TG.reduce) return;

  // particles along the edges, each with a short fading trail
  TG.particles = TG.particles.filter((p) => now - p.t0 < p.dur);
  for (const p of TG.particles) {
    const f = (now - p.t0) / p.dur;
    if (f < 0) continue;
    const msg = p.kind === 'msg';
    const trail = msg ? 9 : 4;
    for (let k = trail; k >= 0; k--) {
      const ff = Math.max(0, f - k * (msg ? 0.012 : 0.02));
      const segF = ff * p.segs.length, si = Math.min(p.segs.length - 1, Math.floor(segF));
      const pt = bez(p.segs[si], segF - si);
      const a = (1 - k / (trail + 1)) * (msg ? 1 : 0.7);
      ctx.globalAlpha = a;
      ctx.fillStyle = p.color;
      const r = (msg ? 3.2 : 1.7) * (1 - k / (trail + 2));
      ctx.beginPath(); ctx.arc(pt.x, pt.y, r, 0, Math.PI * 2); ctx.fill();
      if (k === 0 && msg) {
        ctx.globalAlpha = 0.25; ctx.beginPath(); ctx.arc(pt.x, pt.y, 8, 0, Math.PI * 2); ctx.fill();
      }
    }
  }
  ctx.globalAlpha = 1;

  // memory / skill: dots orbiting the node, and a ring that flashes outward
  TG.flashes = TG.flashes.filter((fl) => now - fl.t0 < fl.dur);
  for (const fl of TG.flashes) {
    const f = (now - fl.t0) / fl.dur, n = fl.node;
    ctx.strokeStyle = fl.color; ctx.fillStyle = fl.color;
    if (fl.kind === 'ring') {
      ctx.globalAlpha = 1 - f; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(n.x, n.y, n.r + 4 + f * 22, 0, Math.PI * 2); ctx.stroke();
    } else {
      const fade = f < 0.1 ? f / 0.1 : 1 - (f - 0.1) / 0.9;
      for (let i = 0; i < 3; i++) {
        const a = t * 3.2 + (i * Math.PI * 2) / 3;
        ctx.globalAlpha = Math.max(0, fade);
        ctx.beginPath(); ctx.arc(n.x + Math.cos(a) * (n.r + 9), n.y + Math.sin(a) * (n.r + 9), 2, 0, Math.PI * 2); ctx.fill();
      }
    }
  }
  ctx.globalAlpha = 1;
}
function tgLoop(now) {
  TG.raf = requestAnimationFrame(tgLoop);
  if (document.hidden) return;
  const busy = TG.particles.length || TG.flashes.length;
  if (!busy && now - TG.lastDraw < 50) return;      // ~20 fps while only the stars move
  TG.lastDraw = now;
  tgDraw(now);
}

/* ── sizing ───────────────────────────────────────────────────────────────────────────────── */
function tgResize() {
  const box = $('tgraph'), c = $('tgfx');
  if (!box || !c) return false;
  const r = box.getBoundingClientRect();
  const W = Math.round(r.width), H = Math.round(r.height);
  if (!W || !H || (W === TG.W && H === TG.H)) return false;
  TG.W = W; TG.H = H; TG.dpr = Math.min(2, window.devicePixelRatio || 1);
  c.width = W * TG.dpr; c.height = H * TG.dpr;
  tgSeedStars();
  return true;
}

function renderGraph(s) {
  TG.lastState = s;
  tgResize();
  const svg = $('tgsvg');
  if (!svg || !TG.W) return;
  const t = s.team;
  const list = tgCast(s);
  if (!list.length) {
    svg.textContent = ''; TG.sig = '';
    svg.setAttribute('viewBox', `0 0 ${TG.W} ${TG.H}`);
    sv('text', { class: 'tgempty', x: TG.W / 2, y: TG.H / 2 }, svg).textContent = 'SOLO - no team for this session';
    $('graphStamp').textContent = 'solo';
    return;
  }
  const sig = list.map((n) => n.name + ':' + n.kind + (n.stale ? ':s' : '')).join('|') + `@${TG.W}x${TG.H}`;
  tgLayout(list, TG.W, TG.H);
  if (sig !== TG.sig) { TG.sig = sig; tgPlaceLabels(list, TG.W, TG.H); tgBuild(list); }
  tgPatch(s, list);
  tgEvents(s, list);
  const c = t.counts || {};
  const msgs = (t.edges || []).length;
  $('graphStamp').textContent = `${list.length - 1} agents · ${c.running || 0} running · ${c.idle || 0} idle · ${c['handed-in'] || 0} handed in${c['pending-followup'] ? ' · ' + c['pending-followup'] + ' follow-up pending' : ''} · ${c.done || 0} done${t.stale ? " · " + t.stale + " earlier" : ""} · ${msgs} msgs`;
  if (!TG.raf) { if (TG.reduce) tgDraw(performance.now()); else TG.raf = requestAnimationFrame(tgLoop); }
}

if (window.ResizeObserver && $('tgraph')) {
  new ResizeObserver(() => { if (tgResize() && TG.lastState) { TG.sig = ''; renderGraph(TG.lastState); if (TG.reduce) tgDraw(performance.now()); } }).observe($('tgraph'));
}
matchMedia('(prefers-reduced-motion: reduce)').addEventListener?.('change', (e) => {
  TG.reduce = e.matches; TG.particles = []; TG.flashes = [];
  if (TG.reduce) { cancelAnimationFrame(TG.raf); TG.raf = 0; tgDraw(performance.now()); } else if (!TG.raf) TG.raf = requestAnimationFrame(tgLoop);
});
