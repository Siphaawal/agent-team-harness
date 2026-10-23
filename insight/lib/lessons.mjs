// lessons.mjs - the LESSONS box. The lessons themselves are NOT parsed here.
//
// memory/lessons/*.md frontmatter has exactly one parser, the Python one every other tool uses (tools/common.py).
// A second parser in JavaScript drifted from it (inline comments, block lists, trailing spaces), so this module
// asks the tool instead:
//
//   python3 tools/memory_index.py --root <repo> --print lessons-json
//   -> {"lessons":[{"name","description","trigger","created","status","tier0","tags","errors","file"}]}
//   -> {"lessons":[],"error":"<ErrorClass>"} when the tool itself failed
//
// `file` is relative to <repo>/memory (e.g. "lessons/x.md") and is the only way a lesson's body is located.
//
// The call is made with execFile (no shell), stdin ignored, a 10 s timeout, and its answer is cached for 20 s.
// summary() and one() never wait for it and never throw: they return the last answer, or a "lessons unavailable:
// <reason>" error when python is missing or the tool failed. Lessons with errors[] or status superseded are
// hidden. Only the display BODY is read here, straight from the file, with the frontmatter block cut off.

import fs from 'node:fs';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OWN_TOOL = path.join(HERE, '..', '..', 'tools', 'memory_index.py');   // the tool next to this dashboard
const SLUG = /^[a-z0-9-]{1,64}$/;
const TTL_MS = 20000;
const TIMEOUT_MS = 10000;

let REPO = process.cwd();
const cache = { at: 0, busy: null, value: null };     // value: {lessons} | {error}
export function setRepo(p) { REPO = p; cache.at = 0; cache.value = null; cache.busy = null; }

/** Python interpreters to try, in order: $AGENT_PYTHON alone when set (as load-persona.sh does), else python3, python. */
export function pythonCandidates(env = process.env) {
  return env.AGENT_PYTHON ? [env.AGENT_PYTHON] : ['python3', 'python'];
}

/** The tool of the target repo if it has one, else the one shipped beside this dashboard. */
function toolPath() {
  const mine = path.join(REPO, 'tools', 'memory_index.py');
  return fs.existsSync(mine) ? mine : OWN_TOOL;
}

function run(cmd, argv) {
  return new Promise((resolve) => {
    const child = execFile(cmd, argv, { cwd: REPO, timeout: TIMEOUT_MS, windowsHide: true, maxBuffer: 8 * 1024 * 1024 },
      (err, stdout, stderr) => resolve({ err, stdout: String(stdout || ''), stderr: String(stderr || '') }));
    if (child.stdin) child.stdin.end();          // stdin is never used: close it so the tool cannot wait on it
  });
}

/** Ask the tool. Resolves to {lessons: [...]} or {error: "lessons unavailable: ..."}; never rejects. */
export async function fetchLessons(repo = REPO) {
  const tool = toolPath();
  if (!fs.existsSync(tool)) return { error: 'lessons unavailable: tools/memory_index.py not found' };
  let last = 'python not found';
  for (const py of pythonCandidates()) {
    const r = await run(py, [tool, '--root', repo, '--print', 'lessons-json']);
    if (r.err) {
      last = r.err.code === 'ENOENT' ? `${py} not found`
        : r.err.killed ? `${py} timed out after ${TIMEOUT_MS / 1000}s`
        : `${py} failed: ${(r.stderr.trim().split('\n').pop() || r.err.message).slice(0, 160)}`;
      continue;                      // e.g. the Windows python3 stub exits non-zero: try the next name
    }
    try {
      const data = JSON.parse(r.stdout);
      if (!data || !Array.isArray(data.lessons)) throw new Error('no "lessons" array');
      if (data.error) return { error: `lessons unavailable: ${String(data.error).slice(0, 80)}` };    // the tool's own failure class
      return { lessons: data.lessons };
    } catch (e) {
      return { error: `lessons unavailable: bad output from the tool (${String(e.message).slice(0, 80)})` };
    }
  }
  return { error: `lessons unavailable: ${last}` };
}

function refresh() {
  if (cache.busy) return cache.busy;
  cache.busy = fetchLessons(REPO).then((v) => {
    cache.value = v; cache.at = Date.now(); cache.busy = null; return v;
  });
  return cache.busy;
}

/** Last answer; starts a refresh when it is older than TTL_MS. */
function current() {
  if (Date.now() - cache.at > TTL_MS) refresh();
  return cache.value;
}

const visible = (l) => l && SLUG.test(String(l.name)) && !(Array.isArray(l.errors) && l.errors.length) && l.status !== 'superseded';

/** The newest `n` visible lessons for the box (no bodies). Synchronous: serves the cached answer. */
export function summary(n = 12) {
  const v = current();
  if (!v) return { loading: true, total: 0, triggers: 0, tier0: 0, withoutTrigger: 0, hidden: 0, recent: [] };
  if (v.error) return { error: v.error, total: 0, triggers: 0, tier0: 0, withoutTrigger: 0, hidden: 0, recent: [] };
  const live = v.lessons.filter(visible)
    .sort((a, b) => String(b.created || '').localeCompare(String(a.created || '')) || String(a.name).localeCompare(String(b.name)));
  return {
    total: live.length,
    hidden: v.lessons.length - live.length,          // superseded, or the tool reported errors for them
    triggers: live.filter((l) => l.trigger).length,
    tier0: live.filter((l) => l.trigger && l.tier0 !== false).length,
    withoutTrigger: live.filter((l) => !l.trigger).length,
    recent: live.slice(0, n).map((l) => ({
      key: l.name, title: l.description || l.name, date: l.created || null, status: l.status || 'active',
      trigger: l.trigger ? { question: l.trigger, where: l.tier0 !== false ? 'tier 0' : 'on demand' } : null,
    })),
  };
}

/** The file text after its frontmatter block (closing `---` may carry trailing spaces). Display only. */
export function bodyOf(text) {
  const lines = String(text).replace(/\r\n?/g, '\n').split('\n');
  if (lines[0].trim() !== '---') return lines.join('\n').trim();
  const end = lines.findIndex((l, i) => i > 0 && l.trim() === '---');
  return (end < 0 ? lines : lines.slice(end + 1)).join('\n').trim();
}

/** <repo>/memory/<file> when `file` (as reported by the tool) stays inside memory/lessons, else null. */
export function lessonPath(repo, file) {
  if (typeof file !== 'string' || !file) return null;
  const root = path.resolve(repo, 'memory', 'lessons');
  const full = path.resolve(repo, 'memory', file);
  return full.startsWith(root + path.sep) && full.endsWith('.md') ? full : null;
}

/** One visible lesson in full by slug, or null. The slug is validated here whatever the caller did. */
export async function one(key) {
  if (!SLUG.test(String(key))) return null;
  const v = cache.value || await refresh();
  if (!v || v.error) return null;
  const l = v.lessons.find((x) => x.name === key && visible(x));
  if (!l) return null;
  let body = '';
  const file = lessonPath(REPO, l.file);
  try { if (file) body = bodyOf(fs.readFileSync(file, 'utf8')); } catch { /* file vanished */ }
  return {
    key: l.name, title: l.description || l.name, date: l.created || null, status: l.status || 'active',
    trigger: l.trigger || null, tier0: l.tier0 !== false, tags: l.tags || [], body,
  };
}
