// alerts.mjs - the rules that decide when Insight should raise its voice on screen.
//
// Every rule is evaluated server-side so the logic is testable and the browser is only a renderer.
// Each alert carries a STABLE `id`: a condition that stays true keeps the same id, and an escalating
// severity changes it (`ctx.warn` -> `ctx.crit`), so the page can tell a new alarm from a standing one.
// Alerts are text on the page only; nothing is spoken and nothing leaves the machine.

const pct = (n) => Math.round(n);

export function alertsFor(state, cfg) {
  const a = [];
  const t = cfg.alerts;

  // ---- context window
  const ctx = state.session.contextLimit > 0
    ? (state.session.contextTokens / state.session.contextLimit) * 100 : 0;
  if (ctx >= t.contextCritical) {
    a.push({
      id: 'ctx.crit', level: 'critical', title: `CONTEXT ${pct(ctx)}%`,
      detail: `${(state.session.contextTokens / 1e3).toFixed(0)}k of ${(state.session.contextLimit / 1e3).toFixed(0)}k in the live window`,
    });
  } else if (ctx >= t.contextWarn) {
    a.push({
      id: 'ctx.warn', level: 'warn', title: `CONTEXT ${pct(ctx)}%`,
      detail: 'approaching the compaction threshold',
    });
  }

  // ---- struggling
  const f = state.friction;
  if (f.level >= t.frictionCritical) {
    a.push({ id: 'friction.crit', level: 'critical', title: 'STUCK', detail: f.reasons[0] || 'repeated failures' });
  } else if (f.level >= t.frictionWarn) {
    a.push({ id: 'friction.warn', level: 'warn', title: 'STRUGGLING', detail: f.reasons[0] || 'friction rising' });
  }

  // ---- an error burst, independent of the friction score
  const recent = state.tools.recent.slice(0, t.errorWindow);
  const failed = recent.filter((x) => x.ok === false).length;
  if (failed >= t.errorBurst) {
    a.push({
      id: 'errors.burst', level: 'warn', title: `${failed} FAILURES`,
      detail: `in the last ${recent.length} tool calls`,
    });
  }

  // ---- the local budget gauge (API-list-equivalent dollars against config.budget)
  if (state.usage.pct >= 100) {
    a.push({
      id: 'budget.crit', level: 'critical', title: 'BUDGET SPENT',
      detail: `${state.usage.window} budget exhausted (API-equivalent)`,
    });
  } else if (state.usage.pct >= t.budgetWarn) {
    a.push({
      id: 'budget.warn', level: 'warn', title: `BUDGET ${pct(state.usage.pct)}%`,
      detail: `of the ${state.usage.window} budget`,
    });
  }

  // ---- an unusually expensive single prompt
  const last = state.cost.prompts[0];
  if (last && last.usd >= t.promptUsd) {
    a.push({
      id: 'prompt.expensive.' + last.n, level: 'info', title: `PROMPT #${last.n} $${last.usd.toFixed(0)}`,
      detail: `${(last.total / 1e6).toFixed(1)}M tokens, ${last.toolCalls} tool calls`,
    });
  }

  // ---- persona did not actually reach context (loaded != in context - see memtier.mjs)
  const idStatus = state.memory?.identity?.status;
  if (idStatus && idStatus.level === 'critical') {
    a.push({ id: 'identity.missing', level: 'critical', title: 'PERSONA NOT IN CONTEXT', detail: idStatus.text });
  }

  // ---- skills exist and are not being used
  const sk = state.skills || {};
  if ((sk.toolCalls || 0) >= (t.skillsIdleAfter || 150) && (sk.invokeCount || 0) === 0) {
    a.push({
      id: 'skills.unused', level: 'warn', title: 'NO SKILLS INVOKED',
      detail: `${sk.toolCalls} tool calls, 0 skills` + (sk.fileTouches ? ` (skill files touched ${sk.fileTouches}x)` : ''),
    });
  }

  // ---- gone quiet mid-task (a hung tool call looks exactly like this)
  const idle = state.session.idleMs;
  if (idle != null && idle > t.idleMinutes * 60000 && idle < t.idleMinutes * 60000 * 6) {
    a.push({
      id: 'idle', level: 'info', title: 'QUIET',
      detail: `no transcript activity for ${Math.round(idle / 60000)} minutes`,
    });
  }

  const rank = { critical: 0, warn: 1, info: 2 };
  return a.sort((x, y) => rank[x.level] - rank[y.level]);
}
