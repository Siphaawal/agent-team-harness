/* LESSON DETAIL modal. Opens from a row of the LESSONS box (click, or Enter/Space when focused):
   GET /api/lesson/<slug> once, then the lesson in full - title, date, the trigger question and whether it
   is always loaded (tier 0) or on demand, and the lesson text. Closes on Esc, the X or a click on the
   backdrop; focus returns to the row. Same shell and styles as the agent modal (agent.js: .agback /
   .agmodal). Text only, via textContent. Loaded after app.js (borrows $, el). */

const LS = { box: null, opener: null, seq: 0 };

function lsShell() {
  if (LS.box) return LS.box;
  const back = el('div', 'agback lsback');
  back.hidden = true;
  const box = el('div', 'agmodal lsmodal');
  box.setAttribute('role', 'dialog');
  box.setAttribute('aria-modal', 'true');
  box.tabIndex = -1;
  const x = el('button', 'chip btn agx', 'X');
  x.title = 'Close (Esc)';
  x.setAttribute('aria-label', 'Close');
  x.addEventListener('click', lsClose);
  box.appendChild(x);
  box.appendChild(el('div', 'agbody'));
  back.appendChild(box);
  back.addEventListener('mousedown', (e) => { if (e.target === back) lsClose(); });
  document.body.appendChild(back);
  LS.box = back;
  return back;
}

function lsClose() {
  if (!LS.box || LS.box.hidden) return;
  LS.box.hidden = true;
  const o = LS.opener; LS.opener = null;
  if (o && typeof o.focus === 'function' && document.contains(o)) o.focus();
}

async function openLesson(key, opener) {
  const back = lsShell();
  LS.opener = opener || document.activeElement;
  back.hidden = false;
  const body = back.querySelector('.agbody');
  body.textContent = '';
  body.appendChild(el('div', 'empty', 'reading the lesson…'));
  back.querySelector('.lsmodal').focus();
  const seq = ++LS.seq;
  let d = null;
  try { const r = await fetch('/api/lesson/' + encodeURIComponent(key)); if (r.ok) d = await r.json(); } catch { d = null; }
  if (seq !== LS.seq || back.hidden) return;
  body.textContent = '';
  if (!d) { body.appendChild(el('div', 'empty', 'lesson ' + key + ' not found')); return; }

  const head = el('div', 'aghead');
  head.appendChild(el('h2', null, d.key));
  head.appendChild(el('span', 'agtag', (d.date || 'undated') + ' · ' + (d.status || 'active')));
  body.appendChild(head);
  body.appendChild(el('div', 'lstitle', d.title));

  body.appendChild(el('div', 'lssec', 'TRIGGER - the question that must fire'));
  if (!d.trigger) body.appendChild(el('div', 'lsnone', 'none - this lesson has no trigger question, so nothing makes it fire'));
  else {
    const row = el('div', 'lstrig');
    row.appendChild(el('i', 'lw', d.tier0 ? 'TIER 0' : 'ON DEMAND'));
    const q = el('div', 'q');
    q.appendChild(el('b', null, '“' + d.trigger + '”'));
    row.appendChild(q);
    body.appendChild(row);
  }
  body.appendChild(el('div', 'lssec', 'THE LESSON'));
  body.appendChild(el('div', 'lsbody', d.body || '(no body text)'));
}

document.addEventListener('click', (e) => {
  const row = e.target.closest && e.target.closest('.lsrow[data-lesson]');
  if (row) openLesson(row.dataset.lesson, row);
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && LS.box && !LS.box.hidden) { lsClose(); return; }
  const row = e.target.closest && e.target.closest('.lsrow[data-lesson]');
  if (row && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); openLesson(row.dataset.lesson, row); }
});

// ?lesson=<key>: open on load (headless captures of the modal)
{
  const k = new URLSearchParams(location.search).get('lesson');
  if (k && /^[a-z0-9-]{1,64}$/.test(k)) setTimeout(() => openLesson(k), 1500);
}
