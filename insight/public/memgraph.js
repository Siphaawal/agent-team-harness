/* MEMORY GRAPH panel. GET /api/memgraph every 30 s (not part of the SSE state, which stays lean); a 2D canvas,
   zero dependencies. Nodes are memories (topics) and lessons; edges are [[links]] (solid), supersedes (dashed red)
   and shared tags (faint). Layout: a few hundred steps of a plain force simulation, seeded from each id so the
   picture is stable between refreshes, run once per new graph (no animation loop - nothing to stop for reduced
   motion). Hover shows a memory's id, type, tags and description in the line under the graph; clicking a lesson
   opens the lesson modal (lesson.js); clicking a topic pins it. The OBSIDIAN button appears only when the server
   found a vault linked to this repo (tools/obsidian_vault.py) and opens the vault, or the pinned memory in it.

   SECURITY: every memory string is untrusted (it arrives through other members' pull requests). Text goes to the
   page with textContent only, and to the canvas with fillText; the Obsidian link is built with encodeURIComponent
   from a slug the server validated. Loaded after app.js (borrows $, el). */

const MG = { data: null, sig: '', pos: new Map(), hover: null, pinned: null, W: 0, H: 0, dpr: 1, xf: null };
const MG_COL = {
  topic: '#33e05a', lesson: '#e0b23a', superseded: '#ff5a3c', dim: '#1f9a3a',
  link: 'rgba(57,255,20,.55)', tag: 'rgba(57,255,20,.13)', supersedes: 'rgba(255,90,60,.6)',
};

/** A stable pseudo-random number in [0, 1) from a string: same id, same starting place. */
function mgHash(s, salt) {
  let h = 2166136261 ^ salt;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  return ((h >>> 0) % 100000) / 100000;
}

/** Positions in a unit box for every node. O(n^2) per step; n is capped at 400 by the server. */
function mgLayout(nodes, edges) {
  const n = nodes.length;
  const idx = new Map(nodes.map((d, i) => [d.id, i]));
  const x = new Float64Array(n), y = new Float64Array(n);
  nodes.forEach((d, i) => { x[i] = mgHash(d.id, 1) - 0.5; y[i] = mgHash(d.id, 2) - 0.5; });
  const springs = edges.map((e) => [idx.get(e.a), idx.get(e.b), e.kind === 'tag' ? 0.4 : 1]);
  const k = 0.6 / Math.sqrt(Math.max(n, 1));
  const steps = n > 250 ? 160 : 260;
  for (let s = 0; s < steps; s++) {
    const t = 0.08 * (1 - s / steps) + 0.004;
    const fx = new Float64Array(n), fy = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        let dx = x[i] - x[j], dy = y[i] - y[j];
        let d2 = dx * dx + dy * dy;
        if (d2 < 1e-6) { dx = 1e-3 * (i - j); dy = 1e-3; d2 = 1e-6; }
        const f = (k * k) / d2;
        fx[i] += dx * f; fy[i] += dy * f; fx[j] -= dx * f; fy[j] -= dy * f;
      }
    }
    for (const [a, b, w] of springs) {
      const dx = x[a] - x[b], dy = y[a] - y[b];
      const d = Math.sqrt(dx * dx + dy * dy) || 1e-3;
      const f = (w * d) / k;
      fx[a] -= (dx / d) * f * d; fy[a] -= (dy / d) * f * d;
      fx[b] += (dx / d) * f * d; fy[b] += (dy / d) * f * d;
    }
    for (let i = 0; i < n; i++) {
      fx[i] -= x[i] * 0.6; fy[i] -= y[i] * 0.6;               // gravity keeps loose nodes on screen
      const m = Math.sqrt(fx[i] * fx[i] + fy[i] * fy[i]) || 1;
      x[i] += (fx[i] / m) * Math.min(m, t); y[i] += (fy[i] / m) * Math.min(m, t);
    }
  }
  const pos = new Map();
  nodes.forEach((d, i) => pos.set(d.id, { x: x[i], y: y[i] }));
  return pos;
}

function mgColour(d) {
  if (d.status === 'superseded') return MG_COL.superseded;
  return d.kind === 'lesson' ? MG_COL.lesson : MG_COL.topic;
}

function mgResize() {
  const box = $('mgraph'), c = $('mgfx');
  if (!box || !c) return false;
  const W = box.clientWidth, H = box.clientHeight;
  if (W === MG.W && H === MG.H) return false;
  MG.W = W; MG.H = H; MG.dpr = Math.min(2, window.devicePixelRatio || 1);
  c.width = Math.max(1, Math.round(W * MG.dpr)); c.height = Math.max(1, Math.round(H * MG.dpr));
  return true;
}

/** Unit-box layout -> canvas pixels, fitted with a margin. */
function mgTransform() {
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const p of MG.pos.values()) { x0 = Math.min(x0, p.x); x1 = Math.max(x1, p.x); y0 = Math.min(y0, p.y); y1 = Math.max(y1, p.y); }
  if (!isFinite(x0)) return (p) => ({ x: MG.W / 2, y: MG.H / 2 });
  const pad = 16, sx = (MG.W - 2 * pad) / Math.max(x1 - x0, 1e-3), sy = (MG.H - 2 * pad) / Math.max(y1 - y0, 1e-3);
  const s = Math.min(sx, sy), ox = (MG.W - s * (x1 - x0)) / 2, oy = (MG.H - s * (y1 - y0)) / 2;
  return (p) => ({ x: ox + (p.x - x0) * s, y: oy + (p.y - y0) * s });
}

function mgDraw() {
  const c = $('mgfx');
  if (!c || !MG.data) return;
  const g = c.getContext('2d');
  g.setTransform(MG.dpr, 0, 0, MG.dpr, 0, 0);
  g.clearRect(0, 0, MG.W, MG.H);
  const xf = MG.xf = mgTransform();
  const focus = MG.pinned || MG.hover;
  const near = new Set();
  if (focus) for (const e of MG.data.edges) { if (e.a === focus) near.add(e.b); if (e.b === focus) near.add(e.a); }
  for (const kind of ['tag', 'link', 'supersedes']) {
    g.strokeStyle = MG_COL[kind];
    g.lineWidth = kind === 'tag' ? 1 : 1.3;
    g.setLineDash(kind === 'supersedes' ? [4, 4] : []);
    g.beginPath();
    for (const e of MG.data.edges) {
      if (e.kind !== kind) continue;
      const a = MG.pos.get(e.a), b = MG.pos.get(e.b);
      if (!a || !b) continue;
      const pa = xf(a), pb = xf(b);
      g.moveTo(pa.x, pa.y); g.lineTo(pb.x, pb.y);
    }
    g.stroke();
  }
  g.setLineDash([]);
  g.font = '10px Consolas, "DejaVu Sans Mono", monospace';
  g.textBaseline = 'middle';
  for (const d of MG.data.nodes) {
    const p = xf(MG.pos.get(d.id));
    const on = d.id === focus || near.has(d.id);
    g.globalAlpha = focus && !on ? 0.35 : (d.status === 'ephemeral' ? 0.6 : 1);
    g.fillStyle = mgColour(d);
    g.beginPath();
    g.arc(p.x, p.y, d.id === focus ? 5.5 : d.kind === 'lesson' ? 3.6 : 3, 0, Math.PI * 2);
    g.fill();
    // labels on the focused neighbourhood, or on every node of a small graph; fillText: the canvas never parses it
    if (on || (!focus && MG.data.nodes.length <= 30)) { g.fillStyle = on ? '#9dff8a' : '#4fbf5a'; g.fillText(d.id, p.x + 7, p.y); }
  }
  g.globalAlpha = 1;
}

function mgNodeAt(ev) {
  if (!MG.data || !MG.xf) return null;
  const r = $('mgfx').getBoundingClientRect();
  const mx = ev.clientX - r.left, my = ev.clientY - r.top;
  let best = null, bd = 10 * 10;
  for (const d of MG.data.nodes) {
    const p = MG.xf(MG.pos.get(d.id));
    const dd = (p.x - mx) ** 2 + (p.y - my) ** 2;
    if (dd < bd) { bd = dd; best = d; }
  }
  return best;
}

function mgInfo(d) {
  const box = $('mginfo');
  if (!box) return;
  box.textContent = '';
  if (!d) {
    const s = MG.data;
    box.appendChild(el('span', 'empty', s && s.nodes.length
      ? 'hover a node - lessons gold, topics green, superseded red; click a lesson to read it'
      : ''));
    return;
  }
  box.appendChild(el('b', null, d.id));
  box.appendChild(el('span', 'mgmeta', ' ' + [d.kind, d.type, d.status, d.created].filter(Boolean).join(' · ')));
  if (d.tags.length) box.appendChild(el('span', 'mgtags', ' [' + d.tags.join(', ') + ']'));
  box.appendChild(el('div', 'mgdesc', d.description || '(no description)'));
}

function mgObsidian() {
  const a = $('mgObsidian');
  if (!a) return;
  const ob = MG.data && MG.data.obsidian;
  if (!ob || !ob.available || !ob.vault) { a.hidden = true; a.removeAttribute('href'); return; }
  let url = 'obsidian://open?vault=' + encodeURIComponent(ob.vault);
  const pin = MG.pinned && MG.data.nodes.find((d) => d.id === MG.pinned);
  if (pin) url += '&file=' + encodeURIComponent('memory/' + (pin.kind === 'lesson' ? 'lessons' : 'topics') + '/' + pin.id + '.md');
  a.href = url;
  a.title = pin ? 'Open ' + pin.id + ' in Obsidian (read it there; edits change team memory)' : 'Open the memory vault in Obsidian';
  a.hidden = false;
}

function mgRender(d) {
  MG.data = d;
  const stamp = $('mgStamp');
  if (d.error) {
    if (stamp) stamp.textContent = d.error;
    MG.pos = new Map(); mgDraw(); mgInfo(null); mgObsidian();
    return;
  }
  const sig = d.nodes.map((n) => n.id + ':' + n.status).join('|') + '#' + d.edges.length;
  if (sig !== MG.sig) { MG.sig = sig; MG.pos = mgLayout(d.nodes, d.edges); }
  if (MG.pinned && !d.nodes.some((n) => n.id === MG.pinned)) MG.pinned = null;
  if (stamp) {
    const bits = [d.nodes.length + ' memories', d.edges.length + ' edges'];
    if (d.truncated) bits.push(d.truncated + ' not shown');
    if (d.rejected) bits.push(d.rejected + ' unsafe names skipped');
    if (d.dangling) bits.push(d.dangling + ' dangling links');
    stamp.textContent = bits.join(' · ');
  }
  mgResize(); mgDraw(); mgInfo(null); mgObsidian();
}

async function mgFetch() {
  let d = null;
  try { const r = await fetch('/api/memgraph', { cache: 'no-store' }); if (r.ok) d = await r.json(); } catch { d = null; }
  if (d && Array.isArray(d.nodes) && Array.isArray(d.edges)) mgRender(d);
  else { const s = $('mgStamp'); if (s) s.textContent = 'graph unavailable'; }
}

if ($('mgfx')) {
  const c = $('mgfx');
  c.addEventListener('mousemove', (ev) => {
    const d = mgNodeAt(ev);
    const id = d ? d.id : null;
    if (id === MG.hover) return;
    MG.hover = id; c.classList.toggle('hot', !!d);
    mgDraw(); mgInfo(d || (MG.pinned && MG.data.nodes.find((n) => n.id === MG.pinned)) || null);
  });
  c.addEventListener('mouseleave', () => { MG.hover = null; mgDraw(); });
  c.addEventListener('click', (ev) => {
    const d = mgNodeAt(ev);
    MG.pinned = d && MG.pinned !== d.id ? d.id : null;
    mgDraw(); mgInfo(d); mgObsidian();
    if (d && d.kind === 'lesson' && d.status !== 'superseded' && typeof openLesson === 'function') openLesson(d.id, c);
  });
  if (window.ResizeObserver) new ResizeObserver(() => { if (mgResize()) mgDraw(); }).observe($('mgraph'));
  mgFetch();
  setInterval(mgFetch, 30000);
}
