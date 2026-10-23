// parse.mjs - turn a Claude Code session transcript (.jsonl) into the numbers Insight draws.
//
// The transcript is the ONLY data source. Nothing here talks to the network and nothing is
// invented: every field below is read off a line that Claude Code wrote. Where a number cannot be
// read (an account's plan limit, for instance) it is NOT faked - it comes from config.json and is
// labelled a local budget in the UI.
//
// One line per JSON object. The shapes that matter:
//   {type:'assistant', message:{model, usage:{...}, content:[{type:'tool_use',name,input}]}, effort, timestamp, cwd, gitBranch}
//   {type:'user', message:{content:[{type:'tool_result', is_error, content}]}, toolUseResult, timestamp}
//   {type:'user', message:{content:'...'}}                      <- a real human prompt
//   {type:'system', level, subtype, content, toolUseID}
//   {type:'ai-title', aiTitle} / {type:'mode', mode} / {type:'permission-mode', permissionMode}

//   {type:'attachment', attachment:{type:'hook_success', hookEvent:'SessionStart', content}}  <- tier 0 arrives HERE
//
// Subagents (the memory librarian among them) write their own transcripts under
// <session>/subagents/agent-*.jsonl. `foldSubagentLine` folds only their memory and skill activity
// into the parent session, tagged with the agent, so reads done "in another context" stay visible.

import fs, { createReadStream } from 'node:fs';
import { createInterface } from 'node:readline';
import os from 'node:os';
import path from 'node:path';
import * as memtier from './memtier.mjs';

const PROJECTS_DIR = path.join(os.homedir(), '.claude', 'projects');

/** Read a persisted hook payload - only ever from Claude Code's own tool-results dirs. */
function readPersisted(p) {
  const full = path.resolve(p);
  if (!full.startsWith(PROJECTS_DIR + path.sep) || !/[\\/]tool-results[\\/]hook-[^\\/]*-stdout\.txt$/.test(full)) return null;
  try { return fs.readFileSync(full, 'utf8'); } catch { return null; }
}

/** Tool names that mean "the agent touched the filesystem here". */
const FILE_TOOLS = new Set(['Read', 'Edit', 'Write', 'NotebookEdit', 'Glob', 'Grep']);

/** Pull the first plausible path out of a tool input. */
function pathOf(name, input) {
  if (!input || typeof input !== 'object') return null;
  return input.file_path || input.notebook_path || input.path || null;
}

/**
 * AGENT DETAIL label. Stricter than describeTool, because the agent modal shows these
 * one by one: never command text, prompt, message or description of a spawn. Allowed sources only:
 * the Bash/PowerShell `description` (empty when missing - the tool name stands alone), a file's
 * basename, a skill name, a Grep/Glob pattern, and a recipient.
 */
export function actLabel(name, input) {
  if (!input || typeof input !== 'object') return '';
  const s = (v, n) => String(v || '').replace(/\s+/g, ' ').slice(0, n);
  if (name === 'Bash' || name === 'PowerShell') return s(input.description, 80);
  if (name === 'Read' || name === 'Edit' || name === 'Write' || name === 'NotebookEdit') {
    const p = input.file_path || input.notebook_path;
    return p ? path.basename(String(p)) : '';
  }
  if (name === 'Skill') return s(input.skill, 60);
  if (name === 'Grep' || name === 'Glob') return s(input.pattern, 60);
  if (name === 'SendMessage') return typeof input.to === 'string' ? '-> ' + s(input.to, 64) : '';
  if (name === 'Agent') return '-> ' + s(input.name || input.subagent_type || 'agent', 64);
  return '';
}

/** A short human-readable "what is it doing" line for a tool call. */
export function describeTool(name, input) {
  if (!input || typeof input !== 'object') return name;
  const p = pathOf(name, input);
  if (p) return `${name} ${path.basename(p)}`;
  if (name === 'Bash' || name === 'PowerShell') return input.description || String(input.command || '').slice(0, 80);
  if (name === 'Skill') return `${input.skill}${input.args ? ' ' + String(input.args).slice(0, 40) : ''}`;
  // never the spawn description or prompt (team task text is private) - who, not what
  if (name === 'Agent') return `Agent ${String(input.name || input.subagent_type || 'agent').slice(0, 40)}`;
  if (name === 'Grep') return `Grep ${String(input.pattern || '').slice(0, 40)}`;
  if (name?.startsWith('mcp__')) {
    const t = input.tool_name || input.toolset_name || '';
    return t ? `${name.split('__').pop()} ${t}` : name;
  }
  return name;
}

function textOf(content) {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content.filter((c) => c && c.type === 'text').map((c) => c.text).join('\n');
}

/* PRIVACY: what agents say to each other is not telemetry. A message from a
   teammate or another session arrives in a transcript as a user line and so becomes a "prompt",
   whose text then shows up as the memory panel's "why". And EVERY prompt in an agent's own
   transcript is the lead's spawn prompt or a message. So: in an agent transcript (s.redactPrompts,
   set by lib/team.mjs) no prompt text is kept at all, and in the session any message envelope is
   reduced to who sent it. */
const MSG_ENVELOPE = /<teammate-message|<cross-session-message|Another Claude session sent a message/;
function promptTextOf(s, d) {
  const t = textOf(d.message.content);
  if (s.redactPrompts) return '[message to this agent]';
  if (MSG_ENVELOPE.test(t)) {
    const who = /(?:teammate_id|from)="([^"]{1,64})"/.exec(t);
    return `[message from ${who ? who[1] : 'another agent'}]`;
  }
  return t.slice(0, 400);
}

/** Is this user line a REAL human prompt, or machinery wearing a user costume? */
function isHumanPrompt(d) {
  if (d.type !== 'user') return false;
  if (d.isMeta || d.isCompactSummary || d.isSidechain) return false;
  if (d.toolUseResult !== undefined) return false;
  const c = d.message?.content;
  if (Array.isArray(c) && c.some((x) => x && x.type === 'tool_result')) return false;
  const t = textOf(c).trim();
  if (!t) return false;
  // Hook output, command wrappers and system reminders arrive as user text.
  if (t.startsWith('<') && (t.includes('system-reminder') || t.includes('command-name'))) return false;
  return true;
}

export function emptyState() {
  return {
    file: null,
    sessionId: null,
    project: null,
    cwd: null,
    gitBranch: null,
    version: null,
    title: null,
    model: null,
    effort: null,
    permissionMode: null,
    startedAt: null,
    lastActivityAt: null,
    turns: 0,
    apiCalls: 0,
    tokens: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, thinking: 0 },
    byModel: {},              // model -> {calls, input, output, cacheRead, cacheWrite}
    contextTokens: 0,         // the live context size: last call's read+write+input
    contextSource: 'call',    // 'call' = read off a usage block; 'compact' = estimated from the summary
    compactedAt: null,        // when the window was last rebuilt (isCompactSummary line)
    compactions: 0,
    lastMsg: null,            // {id, t, model} of the last assistant line, for the per-block dedupe
    prompts: [],              // {n, at, text, tokens:{...}, toolCalls, durationMs}
    toolCounts: {},
    recentTools: [],          // {at, name, detail, ok}
    skills: [],               // {at, skill, args, why}
    messages: [],             // SendMessage calls: {at, to} ONLY - the text is never kept. Capped at 80.
    spawns: {},               // named Agent calls: name -> {role (subagent_type or null), at}. Newest spawn wins.
    acts: [],                 // agent-detail ring buffer: {at, name, label (actLabel), id, ok}. Capped at 40.
    files: [],                // {at, op, path} - capped at 200, so counts come from fileOps
    fileOps: { read: 0, write: 0, search: 0 },
    folders: {},              // dir -> hits
    errors: [],               // {at, tool, message}
    errorCount: 0,
    memory: memtier.empty(),
  };
}

/**
 * Fold one transcript line into `s`. Exported so the tailer can reuse it on appended bytes.
 */
export function foldLine(s, d) {
  const ts = d.timestamp ? Date.parse(d.timestamp) : null;
  if (ts) {
    if (!s.startedAt || ts < s.startedAt) s.startedAt = ts;
    if (!s.lastActivityAt || ts > s.lastActivityAt) s.lastActivityAt = ts;
  }
  if (d.sessionId && !s.sessionId) s.sessionId = d.sessionId;
  if (d.cwd) s.cwd = d.cwd;
  if (d.gitBranch) s.gitBranch = d.gitBranch;
  if (d.version) s.version = d.version;

  if (d.attachment?.hookEvent === 'SessionStart' && !d.isSidechain) {
    memtier.recordHookLoad(s.memory, { at: ts, content: d.attachment.content, readPersisted });
    return;
  }
  // the shared agent core, injected into every spawned agent - lands only in AGENT transcripts.
  // One firing writes TWO lines: the hook's result (hook_success / an error type, with exitCode) and a
  // `hook_additional_context` copy of the injected text. Count the result only, or every agent reads x2.
  if (d.attachment?.hookEvent === 'SubagentStart') {
    if (d.attachment.type === 'hook_additional_context') return;
    memtier.recordCoreLoad(s.memory, { at: ts, stdout: d.attachment.stdout, exitCode: d.attachment.exitCode });
    return;
  }

  switch (d.type) {
    case 'ai-title': if (d.aiTitle) s.title = d.aiTitle; return;
    case 'permission-mode': if (d.permissionMode) s.permissionMode = d.permissionMode; return;

    case 'assistant': {
      const m = d.message || {};
      const u = m.usage || {};
      if (m.model) s.model = m.model;
      if (d.effort) s.effort = d.effort;

      /* ⚠️ Claude Code writes one line PER CONTENT BLOCK of a message (text, thinking, every
         tool_use), and each line repeats the whole message's `usage`. Summing per line over-counted
         a session about 3x (one transcript line per content block, not per message) - the ledger, the
         cost per prompt and the budget gauge were all a third real. Key on message.id: the first
         line of a message ADDS its usage, a repeat REPLACES it (deltas), so the last line stands. */
      const now = {
        input: u.input_tokens || 0, output: u.output_tokens || 0,
        cacheRead: u.cache_read_input_tokens || 0, cacheWrite: u.cache_creation_input_tokens || 0,
        thinking: u.output_tokens_details?.thinking_tokens || 0,
      };
      const repeat = !!(m.id && s.lastMsg && s.lastMsg.id === m.id);
      const prev = repeat ? s.lastMsg.t : { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, thinking: 0 };
      const key = m.model || (repeat ? s.lastMsg.model : 'unknown');
      s.lastMsg = { id: m.id || null, t: now, model: key };
      if (!repeat) s.apiCalls++;

      const bm = (s.byModel[key] ||= { calls: 0, input: 0, output: 0, cacheRead: 0, cacheWrite: 0 });
      if (!repeat) bm.calls++;
      const cur = s.prompts[s.prompts.length - 1];
      for (const k of ['input', 'output', 'cacheRead', 'cacheWrite']) {
        const delta = now[k] - prev[k];
        s.tokens[k] += delta; bm[k] += delta;
        if (cur) cur.tokens[k] += delta;
      }
      s.tokens.thinking += now.thinking - prev.thinking;
      s.contextTokens = now.input + now.cacheRead + now.cacheWrite;
      s.contextSource = 'call';

      if (cur) {
        cur.model = m.model || cur.model;
        if (ts) cur.endedAt = ts;
      }

      for (const c of m.content || []) {
        if (!c || c.type !== 'tool_use') continue;
        const name = c.name || '?';
        s.toolCounts[name] = (s.toolCounts[name] || 0) + 1;
        if (cur) cur.toolCalls++;
        const detail = describeTool(name, c.input);
        memtier.record(s.memory, {
          at: ts, tool: name, input: c.input,
          promptN: cur ? cur.n : 0,
          promptText: cur ? cur.text.replace(/\s+/g, ' ').slice(0, 90) : null,
        });
        s.recentTools.push({ at: ts, name, detail, id: c.id, ok: null });
        if (s.recentTools.length > 300) s.recentTools.shift();
        s.acts.push({ at: ts, name, label: actLabel(name, c.input), id: c.id, ok: null });
        if (s.acts.length > 40) s.acts.shift();

        // the ROLE a named agent was spawned as. A teammate's own meta.json records only its NAME as
        // agentType, so the spawn call is the only place the role exists. Name, role and time only -
        // never the prompt or description.
        if (name === 'Agent' && c.input && typeof c.input.name === 'string') {
          s.spawns[c.input.name.slice(0, 64)] = {
            role: typeof c.input.subagent_type === 'string' ? c.input.subagent_type.slice(0, 64) : null, at: ts,
          };
        }

        // the TEAM GRAPH's message edges: recipient and time, nothing else from the input
        if (name === 'SendMessage' && c.input && typeof c.input.to === 'string') {
          s.messages.push({ at: ts, to: c.input.to.slice(0, 64) });
          if (s.messages.length > 80) s.messages.shift();
        }

        if (name === 'Skill') {
          s.skills.push({ at: ts, skill: c.input?.skill, args: c.input?.args || null, why: detail,
            scope: memtier.scopeOfInvocation(c.input?.skill, s.cwd), by: null });
          if (s.skills.length > 60) s.skills.shift();
        }
        const p = pathOf(name, c.input);
        if (p && FILE_TOOLS.has(name)) {
          const op = name === 'Read' ? 'read' : name === 'Glob' || name === 'Grep' ? 'search' : 'write';
          s.files.push({ at: ts, op, path: p });
          if (s.files.length > 200) s.files.shift();
          s.fileOps[op]++;
          const dir = path.dirname(p);
          s.folders[dir] = (s.folders[dir] || 0) + 1;
        }
      }
      return;
    }

    case 'user': {
      if (d.isCompactSummary) {
        /* The window was just rebuilt around this summary. Until the next call answers with a real
           usage block the honest number is the summary's own size - the meter used to sit on the
           pre-compaction figure until the first post-compaction assistant line (seen when a
           session was compacted and the dashboard did not move). */
        const chars = textOf(d.message?.content).length;
        s.contextTokens = Math.round(chars / 4);
        s.contextSource = 'compact';
        s.compactedAt = ts;
        s.compactions++;
        return;
      }
      if (isHumanPrompt(d)) {
        s.turns++;
        s.prompts.push({
          n: s.turns, at: ts, endedAt: ts,
          text: promptTextOf(s, d),
          model: s.model,
          tokens: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
          toolCalls: 0,
        });
        if (s.prompts.length > 200) s.prompts.shift();
        return;
      }
      // tool results: mark the matching call ok/failed, and collect real errors
      const content = d.message?.content;
      if (Array.isArray(content)) {
        for (const c of content) {
          if (!c || c.type !== 'tool_result') continue;
          const hit = s.recentTools.find((t) => t.id === c.tool_use_id);
          const bad = !!c.is_error;
          if (hit) hit.ok = !bad;
          const act = s.acts.find((t) => t.id === c.tool_use_id);
          if (act) act.ok = !bad;
          if (bad) {
            s.errorCount++;
            s.errors.push({
              at: ts,
              tool: hit?.name || 'tool',
              detail: hit?.detail || null,
              message: textOf(c.content).slice(0, 300) || String(c.content).slice(0, 300),
            });
            if (s.errors.length > 60) s.errors.shift();
          }
        }
      }
      return;
    }

    case 'system': {
      if (d.level === 'error' && d.content) {
        s.errorCount++;
        s.errors.push({ at: ts, tool: 'system', detail: d.subtype || null, message: String(d.content).slice(0, 300) });
        if (s.errors.length > 60) s.errors.shift();
      }
      return;
    }
  }
}

/**
 * Fold one line of a SUBAGENT transcript into the parent state: memory and skill activity only.
 * Its tokens and tool counts are deliberately left out here - they are the subagent's, and mixing
 * them into the parent's per-prompt numbers would misattribute them.
 */
export function foldSubagentLine(s, d, label) {
  if (d.type !== 'assistant') return;
  const ts = d.timestamp ? Date.parse(d.timestamp) : null;
  const cur = s.prompts[s.prompts.length - 1];
  for (const c of d.message?.content || []) {
    if (!c || c.type !== 'tool_use') continue;
    memtier.record(s.memory, {
      at: ts, tool: c.name, input: c.input, by: label,
      promptN: cur ? cur.n : 0,
      promptText: cur ? cur.text.replace(/\s+/g, ' ').slice(0, 90) : null,
    });
    if (c.name === 'Skill') {
      s.skills.push({ at: ts, skill: c.input?.skill, args: c.input?.args || null, why: label,
        scope: memtier.scopeOfInvocation(c.input?.skill, d.cwd || s.cwd), by: label });
      if (s.skills.length > 60) s.skills.shift();
    }
  }
}

/** Human label for a subagent transcript: its meta.json agentType/description, else the file id. */
export function subagentLabel(file) {
  try {
    const meta = JSON.parse(fs.readFileSync(file.replace(/\.jsonl$/, '.meta.json'), 'utf8'));
    // never meta.description: that is the spawn summary (private)
    return meta.name || meta.agentType || meta.subagent_type || path.basename(file, '.jsonl');
  } catch { return path.basename(file, '.jsonl'); }
}

/** Read a whole transcript. Returns {state, bytes} so the caller can tail from `bytes`. */
export async function parseFile(file, state = emptyState()) {
  state.file = file;
  state.project = path.basename(path.dirname(file));
  let bytes = 0;
  const stream = createReadStream(file, { encoding: 'utf8' });
  const rl = createInterface({ input: stream, crlfDelay: Infinity });
  for await (const line of rl) {
    bytes += Buffer.byteLength(line, 'utf8') + 1;
    if (!line.trim()) continue;
    try { foldLine(state, JSON.parse(line)); } catch { /* a half-written last line is normal */ }
  }
  return { state, bytes };
}

/** Turn a project directory name back into a readable path. */
export function projectLabel(dirName) {
  if (!dirName) return null;
  const s = dirName.replace(/^([a-zA-Z])--/, '$1:/').replace(/-/g, '/');
  return s.replace(/\/\//g, '-');
}
