'use strict';
(() => {
  const { el, fmtDuration } = window.HD;
  const $ = (id) => document.getElementById(id);
  // The list never refreshes on a timer: only on opening the tab, after a start or
  // stop, and when Refresh is pressed — so nothing moves while you type.
  const S = { data: null, form: null, busy: false };

  const fmtWhen = (t) => {
    if (!t) return 'never';
    const d = new Date(t * 1000);
    const today = new Date().toDateString() === d.toDateString();
    return today ? `today ${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
                 : d.toLocaleDateString([], { month: 'short', day: 'numeric' });
  };

  async function refresh() {
    const btn = $('claude-refresh');
    btn.disabled = true;
    try {
      const r = await fetch('/api/claude', { cache: 'no-store' });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      S.data = await r.json();
      $('claude-updated').textContent = `updated ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`;
      render();
    } catch (e) {
      note(`Couldn't load projects (${e.message}).`);
    } finally {
      btn.disabled = false;
    }
  }

  function render() {
    const { projects, sessions, terminal_available: canOpen } = S.data;
    const byPath = new Map();
    for (const s of sessions) byPath.set(s.cwd, [...(byPath.get(s.cwd) || []), s]);
    const known = new Set(projects.map((p) => p.path));
    const strays = sessions.filter((s) => !known.has(s.cwd));

    const cards = projects.map((p) => projectCard(p, byPath.get(p.path) || [], canOpen));
    if (strays.length) {
      cards.push(el('article', { class: 'cproj' },
        el('div', { class: 'cproj-h' }, el('div', { class: 'cproj-id' },
          el('div', { class: 'cproj-name', text: 'Other folders' }),
          el('div', { class: 'cproj-path', text: 'Claude running outside your project list' }))),
        el('div', { class: 'cruns' }, ...strays.map((s) => sessionRow(s, s.cwd)))));
    }
    $('claude-count').textContent = `${projects.length} folders · ${sessions.length} running`;
    // append()/replaceChildren() would render a null child as the text "null"
    $('claude-body').replaceChildren(...[
      canOpen ? null : el('p', { class: 'note', text: 'gnome-terminal is not installed, so sessions can’t be started from here.' }),
      el('div', { class: 'cprojs' }, ...cards),
    ].filter(Boolean));

    if (S.form) {  // a start form was open: put it back, with what you typed
      const card = $('claude-body').querySelector(`.cproj[data-project="${CSS.escape(S.form.project)}"]`);
      const project = projects.find((p) => p.id === S.form.project);
      if (card && project) openForm(card, project, false);
      else S.form = null;
    }
  }

  function projectCard(p, runs, canOpen) {
    const card = el('article', { class: 'cproj', 'data-project': p.id });
    const start = el('button', { class: 'btn primary', type: 'button', text: 'Start' });
    start.disabled = !p.exists || !canOpen;
    if (!p.exists) start.title = 'That folder no longer exists';
    start.addEventListener('click', () => {
      if (S.form && S.form.project === p.id) { closeForm(); return; }
      S.form = { project: p.id, name: suggestName(p), cont: true };
      openForm(card, p, true);
    });

    card.append(...[
      el('div', { class: 'cproj-h' },
        el('div', { class: 'cproj-id' },
          el('div', { class: 'cproj-name', text: p.name }),
          el('div', { class: 'cproj-path', title: p.path || p.id, text: p.path || `${p.id} (path unknown)` })),
        start),
      el('div', { class: 'cproj-meta' },
        el('span', { text: `${p.sessions} ${p.sessions === 1 ? 'conversation' : 'conversations'}` }),
        el('span', { text: `last used ${fmtWhen(p.last_used)}` }),
        p.exists ? null : el('span', { class: 'gone', text: 'folder missing' })),
      runs.length ? el('div', { class: 'cruns' }, ...runs.map((s) => sessionRow(s, null))) : null,
    ].filter(Boolean));
    return card;
  }

  function sessionRow(s, pathLabel) {
    const close = el('button', { class: 'btn ghost', type: 'button', text: 'Close' });
    close.addEventListener('click', () => stopSession(s));
    const bits = [s.tty || 'no tty', fmtDuration(Date.now() / 1000 - s.started), `pid ${s.pid}`];
    if (s.continued) bits.push('continued');
    if (pathLabel) bits.push(pathLabel);
    return el('div', { class: `crun${s.own_project ? ' own' : ''}` }, ...[
      el('span', { class: 'chip good' }, el('span', { class: 'icon', 'aria-hidden': 'true', text: '●' }), 'running'),
      el('span', { class: 'crun-name', text: s.remote_control || 'claude (no remote control)' }),
      // This one works on the dashboard itself: it may be the session you're chatting with.
      s.own_project ? el('span', { class: 'chip warn', title: 'Works on this dashboard — closing it may end the conversation you are having' },
        el('span', { class: 'icon', 'aria-hidden': 'true', text: '▲' }), 'builds this dashboard') : null,
      el('span', { class: 'crun-meta', text: bits.join(' · ') }),
      close,
    ].filter(Boolean));
  }

  function suggestName(p) {
    const used = new Set((S.data?.sessions || []).map((s) => s.remote_control).filter(Boolean));
    let name = p.name;
    for (let i = 2; used.has(name); i++) name = `${p.name}-${i}`;
    return name;
  }

  const closeForm = () => {
    S.form = null;
    $('claude-body').querySelector('.cstart')?.remove();
  };

  // You asked to be prompted each time: session name + continue toggle.
  // Values live in S.form, so a refresh restores whatever you typed.
  function openForm(card, p, focus) {
    $('claude-body').querySelector('.cstart')?.remove();

    const nameInput = el('input', { class: 'input', type: 'text', maxlength: '40', 'aria-label': 'Remote Control session name' });
    nameInput.value = S.form.name;
    nameInput.addEventListener('input', () => { S.form.name = nameInput.value; });
    const cont = el('input', { type: 'checkbox' });
    const canContinue = p.can_continue ?? p.sessions > 0;
    cont.checked = canContinue && S.form.cont;
    cont.disabled = !canContinue;
    cont.addEventListener('change', () => { S.form.cont = cont.checked; });
    const msg = el('p', { class: 'msg' });
    const submit = el('button', { class: 'btn primary', type: 'submit', text: 'Start session' });
    const cancel = el('button', { class: 'btn ghost', type: 'button', text: 'Cancel' });

    const form = el('form', { class: 'cstart' },
      el('label', { class: 'stat-label' }, 'Remote Control session name', nameInput),
      el('label', { class: 'check' }, cont, ' Continue the most recent conversation'),
      canContinue ? null : el('p', { class: 'note', text: 'No conversation here that Claude can continue, so this starts a new one.' }),
      el('div', { class: 'cstart-actions' }, submit, cancel),
      msg);
    cancel.addEventListener('click', closeForm);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (S.busy) return;
      S.busy = true;
      submit.disabled = true;
      submit.textContent = 'Opening terminal…';
      msg.textContent = '';
      try {
        const r = await fetch('/api/claude/start', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ project: p.id, name: nameInput.value.trim(), continue: cont.checked }),
        });
        const j = await r.json().catch(() => ({}));
        if (r.ok && j.ok) {
          S.form = null;
          note(j.warning || '');
          await refresh();
        } else {
          msg.textContent = j.error || `Couldn't start it (HTTP ${r.status}).`;
        }
      } catch (err) {
        msg.textContent = `Couldn't reach the dashboard (${err.message}).`;
      } finally {
        S.busy = false;
        submit.disabled = false;
        submit.textContent = 'Start session';
      }
    });
    card.append(form);
    if (focus) {
      nameInput.focus();
      nameInput.select();
    }
  }

  async function stopSession(s) {
    const label = s.remote_control || `pid ${s.pid}`;
    const warning = s.own_project
      ? '\n\nCAREFUL: this session works on the dashboard itself. If it is the conversation you are having right now, closing it ends that conversation.'
      : '';
    if (!confirm(`Close the terminal running "${label}"?\n\nClaude and anything else in that window will stop.${warning}`)) return;
    try {
      const r = await fetch('/api/claude/stop', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pid: s.pid }),
      });
      const j = await r.json().catch(() => ({}));
      note(r.ok && j.ok ? '' : j.error || `Couldn't close it (HTTP ${r.status}).`);
    } catch (e) {
      note(`Couldn't reach the dashboard (${e.message}).`);
    }
    await refresh();
  }

  function note(text) {
    const n = $('claude-note');
    n.textContent = text;
    n.hidden = !text;
  }

  $('claude-refresh').addEventListener('click', refresh);
  window.addEventListener('hd-tab', (e) => { if (e.detail === 'claude') refresh(); });
})();
