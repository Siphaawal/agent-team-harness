// memgraph.mjs - the MEMORY GRAPH panel. The memories are NOT parsed here.
//
// memory/**/*.md frontmatter has exactly one parser, the Python one in tools/common.py (see lessons.mjs for why a
// second one in JavaScript is a bug farm). This module runs the graph builder that ships beside this dashboard:
//
//   python3 insight/lib/memgraph.py --root <repo>
//   -> {"nodes":[...], "edges":[...], "total", "truncated", "dangling", "rejected", "obsidian":{available, vault}}
//   -> {"nodes":[], "edges":[], "error":"<ErrorClass>"} when it failed
//
// execFile (no shell), stdin closed, a 10 s timeout, the answer cached for 30 s; graph() never throws and never
// waits once a first answer exists. The server re-checks the shape and the caps before anything leaves it: the
// memory arrives through other members' pull requests, so its text is untrusted all the way to the page, which
// renders it with textContent only.
//
// The child gets AGENT_TOOLS_ANY_ROOT=1: the tools refuse a --root outside their own repo so that a prompt-free tool
// call cannot be aimed at ~/anything, but the dashboard's --repo is the operator's own choice at launch.

import path from 'node:path';
import { execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { pythonCandidates } from './lessons.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const BUILDER = path.join(HERE, 'memgraph.py');
const TTL_MS = 30000;
const TIMEOUT_MS = 10000;
export const MAX_NODES = 400;
export const MAX_EDGES = 2400;
const ID = /^[a-z0-9][a-z0-9-]{0,63}$/;
const TAG = /^[a-z0-9][a-z0-9-]{0,39}$/;
const KINDS = new Set(['link', 'supersedes', 'tag']);
const VAULT = /^[^\u0000-\u001f\u007f/\\]{1,80}$/;

let REPO = process.cwd();
const cache = { at: 0, busy: null, value: null };
export function setRepo(p) { REPO = p; cache.at = 0; cache.value = null; cache.busy = null; }

function run(cmd, argv) {
  return new Promise((resolve) => {
    let child;
    try {
      child = execFile(cmd, argv, {
        cwd: REPO, timeout: TIMEOUT_MS, windowsHide: true, maxBuffer: 8 * 1024 * 1024,
        env: { ...process.env, AGENT_TOOLS_ANY_ROOT: '1' },
      }, (err, stdout, stderr) => resolve({ err, stdout: String(stdout || ''), stderr: String(stderr || '') }));
    } catch (err) {     // spawn can throw synchronously (e.g. EINVAL for a .bat/.cmd on Windows): a failed run, not a crash
      resolve({ err, stdout: '', stderr: '' });
      return;
    }
    if (child.stdin) child.stdin.end();
  });
}

// strings only: String() would turn a missing value into "undefined" and an array into its joined items
const str = (v, n) => (typeof v === 'string' ? v : '').replace(/[\u0000-\u001f\u007f]/g, ' ').slice(0, n);
const isStr = (v) => typeof v === 'string';

/** The builder's answer cut down to the documented shape and caps. Anything else in it is dropped. */
export function sanitise(data) {
  if (!data || typeof data !== 'object') return { nodes: [], edges: [], error: 'bad output from the graph builder' };
  if (data.error) return { nodes: [], edges: [], error: 'graph unavailable: ' + (str(data.error, 80) || 'bad output from the graph builder') };
  const nodes = [];
  const ids = new Set();
  for (const n of Array.isArray(data.nodes) ? data.nodes : []) {
    if (nodes.length >= MAX_NODES) break;
    if (!n || !isStr(n.id) || !ID.test(n.id) || ids.has(n.id)) continue;
    ids.add(n.id);
    nodes.push({
      id: n.id,
      kind: n.kind === 'lesson' ? 'lesson' : 'topic',
      type: str(n.type, 16), status: str(n.status, 16),
      tags: (Array.isArray(n.tags) ? n.tags : []).filter((t) => isStr(t) && TAG.test(t)).slice(0, 12),
      description: str(n.description, 300),
      created: isStr(n.created) && /^\d{4}-\d{2}-\d{2}$/.test(n.created) ? n.created : null,
    });
  }
  const edges = [];
  for (const e of Array.isArray(data.edges) ? data.edges : []) {
    if (edges.length >= MAX_EDGES) break;
    if (e && ids.has(e.a) && ids.has(e.b) && e.a !== e.b && KINDS.has(e.kind)) edges.push({ a: e.a, b: e.b, kind: e.kind });
  }
  const num = (v) => (Number.isFinite(v) && v >= 0 ? Math.floor(v) : 0);
  const ob = data.obsidian || {};
  const vault = ob.available === true && isStr(ob.vault) && VAULT.test(ob.vault) ? ob.vault : '';
  return {
    nodes, edges,
    total: num(data.total), truncated: num(data.truncated) + Math.max(0, (Array.isArray(data.nodes) ? data.nodes.length : 0) - nodes.length),
    dangling: num(data.dangling), rejected: num(data.rejected),
    obsidian: { available: !!vault, vault },
  };
}

export async function fetchGraph(repo = REPO) {
  let last = 'python not found';
  for (const py of pythonCandidates()) {
    const r = await run(py, [BUILDER, '--root', repo, '--max-nodes', String(MAX_NODES)]);
    if (r.err) {
      last = r.err.code === 'ENOENT' ? `${py} not found`
        : r.err.killed ? `${py} timed out after ${TIMEOUT_MS / 1000}s`
        : `${py} failed`;
      continue;
    }
    try { return sanitise(JSON.parse(r.stdout)); }
    catch { return { nodes: [], edges: [], error: 'graph unavailable: bad output from the graph builder' }; }
  }
  return { nodes: [], edges: [], error: `graph unavailable: ${last}` };
}

function refresh() {
  if (cache.busy) return cache.busy;
  const p = fetchGraph(REPO)
    .catch(() => ({ nodes: [], edges: [], error: 'graph unavailable: the graph builder could not be run' }))
    .then((v) => { cache.value = v; cache.at = Date.now(); return v; })
    .finally(() => { if (cache.busy === p) cache.busy = null; });    // never left pending or rejected
  cache.busy = p;
  return p;
}

/** The graph: the cached answer (refreshed in the background when older than TTL_MS), or the first one awaited. */
export async function graph() {
  if (!cache.value) return refresh();
  if (Date.now() - cache.at > TTL_MS) refresh();
  return cache.value;
}
