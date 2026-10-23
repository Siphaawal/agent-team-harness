// cost.mjs - turn token counts into dollars, and friction counts into a "struggling" reading.
//
// ⚠️ PRICES ARE A LOCAL TABLE, NOT A READING OF THE ACCOUNT. `pricing.json` holds $/million-token
// rates you can edit; nothing here queries Anthropic. Every cost in the UI is therefore an ESTIMATE
// and is labelled as one. Same for the usage limit: an account's real plan allowance is not written
// to the transcript, so `config.json` holds a budget YOU set and the gauge reads against that.

export function priceFor(pricing, model) {
  if (!model) return pricing.default;
  if (pricing.models[model]) return pricing.models[model];
  // fall back by family so a new dated model id still prices sensibly
  const m = model.toLowerCase();
  for (const [frag, p] of Object.entries(pricing.families || {})) {
    if (m.includes(frag)) return p;
  }
  return pricing.default;
}

/** Cost of one usage bundle, in USD. */
export function costOf(pricing, model, t) {
  const p = priceFor(pricing, model);
  const M = 1_000_000;
  return (
    ((t.input || 0) * p.input +
      (t.output || 0) * p.output +
      (t.cacheWrite || 0) * (p.cacheWrite ?? p.input * 1.25) +
      (t.cacheRead || 0) * (p.cacheRead ?? p.input * 0.1)) / M
  );
}

/**
 * How much friction is in the last stretch of work? 0 = clean, 100 = stuck.
 * Built only from things the transcript actually records:
 *   - failed tool calls in the recent window
 *   - the SAME tool failing repeatedly (a retry loop)
 *   - the same file written over and over (patching instead of restarting)
 */
export function frictionOf(state, windowSize = 40) {
  const recent = state.recentTools.slice(-windowSize);
  if (!recent.length) return { level: 0, label: 'IDLE', reasons: [] };

  const failed = recent.filter((t) => t.ok === false);
  const reasons = [];
  let score = 0;

  const failRate = failed.length / recent.length;
  score += failRate * 120;
  if (failed.length >= 2) reasons.push(`${failed.length} failed calls in the last ${recent.length}`);

  // a retry loop: one tool accounting for most of the failures
  const byTool = {};
  for (const f of failed) byTool[f.name] = (byTool[f.name] || 0) + 1;
  const worst = Object.entries(byTool).sort((a, b) => b[1] - a[1])[0];
  if (worst && worst[1] >= 3) { score += 20; reasons.push(`${worst[0]} failing repeatedly (${worst[1]}x)`); }

  // the same file rewritten again and again
  const writes = state.files.filter((f) => f.op === 'write').slice(-20);
  const byFile = {};
  for (const w of writes) byFile[w.path] = (byFile[w.path] || 0) + 1;
  const hot = Object.entries(byFile).sort((a, b) => b[1] - a[1])[0];
  if (hot && hot[1] >= 4) {
    score += 18;
    reasons.push(`${hot[0].split(/[\\/]/).pop()} rewritten ${hot[1]}x - consider a clean restart`);
  }

  // consecutive failures right now hurt most
  let tail = 0;
  for (let i = recent.length - 1; i >= 0 && recent[i].ok === false; i--) tail++;
  if (tail >= 2) { score += tail * 10; reasons.push(`${tail} failures back to back`); }

  const level = Math.max(0, Math.min(100, Math.round(score)));
  const label = level >= 70 ? 'STUCK' : level >= 40 ? 'STRUGGLING' : level >= 15 ? 'FRICTION' : 'NOMINAL';
  return { level, label, reasons };
}
