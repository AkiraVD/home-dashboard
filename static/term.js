'use strict';
(() => {
  const { css, withAlpha, fmtDuration, isLightTheme } = window.HD;
  const $ = (id) => document.getElementById(id);
  const enc = new TextEncoder();
  const FONT_KEY = 'hd-term-font';

  const S = { term: null, fit: null, ws: null, open: false, ended: false, ctrl: false, alt: false, info: null };

  function show(which) {
    for (const id of ['term-loading', 'term-setup', 'term-lock', 'term-main']) $(id).hidden = id !== which;
  }

  function setStatus(state, text) {
    $('term-status').dataset.state = state;
    $('term-status-text').textContent = text;
  }

  // ---------------------------------------------------------------- access
  async function refresh() {
    try {
      const r = await fetch('/api/session', { cache: 'no-store' });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      S.info = await r.json();
    } catch (e) {
      show('term-loading');
      $('term-loading').textContent = `Couldn't check access (${e.message}).`;
      return;
    }
    const who = `${S.info.login} on ${S.info.device}`;
    if (!S.info.totp_configured) {
      $('term-setup-cmd').textContent = S.info.setup_command;
      show('term-setup');
    } else if (!S.info.unlocked) {
      $('lock-who').textContent = `Signed in to Tailscale as ${who}.`;
      $('lock-msg').textContent = S.info.lockout_seconds ? lockoutText(S.info.lockout_seconds) : '';
      show('term-lock');
      $('otp').focus();
    } else {
      $('term-who').textContent = who;
      show('term-main');
      ensureTerminal();
      if (!S.open && !S.ws && !S.ended) startSession();
      else S.term.focus();
    }
  }

  const lockoutText = (s) => `Too many wrong codes. Try again in ${fmtDuration(s)}.`;

  $('term-setup-retry').addEventListener('click', refresh);

  $('term-lock').addEventListener('submit', async (e) => {
    e.preventDefault();
    const code = $('otp').value.replace(/\s/g, '');
    const btn = $('otp-submit');
    btn.disabled = true;
    try {
      const r = await fetch('/api/unlock', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code }),
      });
      const j = await r.json().catch(() => ({}));
      if (r.ok && j.ok) {
        $('otp').value = '';
        $('lock-msg').textContent = '';
        S.ended = false;
        await refresh();
        return;
      }
      $('lock-msg').textContent =
        j.error === 'locked' ? lockoutText(j.retry_after) :
        j.error === 'invalid' ? 'Wrong code. Check the app and try the current code.' :
        `Couldn't unlock (${j.error || `HTTP ${r.status}`}).`;
      $('otp').select();
    } catch (err) {
      $('lock-msg').textContent = `Couldn't reach the dashboard (${err.message}).`;
    } finally {
      btn.disabled = false;
    }
  });
  $('otp').addEventListener('input', (e) => {
    if (e.target.value.replace(/\s/g, '').length === 6 && !$('otp-submit').disabled) $('term-lock').requestSubmit();
  });

  // ---------------------------------------------------------------- terminal
  function termTheme() {
    return {
      background: css('--term-bg'),
      foreground: css('--ink'),
      cursor: css('--ink'),
      cursorAccent: css('--term-bg'),
      selectionBackground: withAlpha(css('--s1'), 0.35),
    };
  }
  function fontSize() {
    let v = NaN;
    try { v = parseInt(localStorage.getItem(FONT_KEY), 10); } catch { /* default below */ }
    return Number.isFinite(v) ? v : matchMedia('(max-width: 640px)').matches ? 12 : 14;
  }

  let fitQueued = false;
  function fitSoon() {
    if (fitQueued) return;
    fitQueued = true;
    requestAnimationFrame(() => {
      fitQueued = false;
      if (!S.fit || $('term-main').hidden || $('view-terminal').hidden) return;
      try { S.fit.fit(); } catch { /* not laid out yet */ }
    });
  }

  function ensureTerminal() {
    if (S.term) { fitSoon(); return; }
    const term = new Terminal({
      cursorBlink: true,
      fontFamily: css('--mono'),
      fontSize: fontSize(),
      scrollback: 5000,
      theme: termTheme(),
      minimumContrastRatio: isLightTheme() ? 4.5 : 1,
    });
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open($('term-screen'));
    term.onData(sendText);
    term.onBinary((d) => sendBytes(Uint8Array.from(d, (c) => c.charCodeAt(0) & 255)));
    term.onResize(({ cols, rows }) => sendControl({ type: 'resize', cols, rows }));
    new ResizeObserver(fitSoon).observe($('term-screen'));
    window.visualViewport?.addEventListener('resize', fitSoon);
    S.term = term;
    S.fit = fit;
    fitSoon();
  }

  function sendBytes(bytes) {
    if (S.ws && S.ws.readyState === WebSocket.OPEN) S.ws.send(bytes);
  }
  function sendControl(msg) {
    if (S.ws && S.ws.readyState === WebSocket.OPEN) S.ws.send(JSON.stringify(msg));
  }
  // Applies the on-screen Ctrl/Alt modifiers (for phone keyboards) to the next key.
  function sendText(data) {
    if (S.ctrl && data.length === 1) {
      const c = data.toUpperCase().charCodeAt(0);
      if (c >= 64 && c <= 95) data = String.fromCharCode(c - 64);
      else if (data === ' ') data = '\x00';
      else if (data === '?') data = '\x7f';
      setModifier('ctrl', false);
    }
    if (S.alt) {
      data = `\x1b${data}`;
      setModifier('alt', false);
    }
    sendBytes(enc.encode(data));
  }

  function startSession() {
    ensureTerminal();
    try { S.fit.fit(); } catch { /* not laid out yet; onopen re-sends the size */ }
    S.ended = false;
    $('term-ended').hidden = true;
    S.term.reset();
    setStatus('connecting', 'Starting shell…');

    const { cols, rows } = S.term;
    const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/term?cols=${cols}&rows=${rows}`);
    ws.binaryType = 'arraybuffer';
    S.ws = ws;
    let endReason = null;

    ws.onopen = () => {
      S.open = true;
      // Resizes that happened while connecting were dropped; sync the pty now.
      sendControl({ type: 'resize', cols: S.term.cols, rows: S.term.rows });
      setStatus('live', 'Connected');
      S.term.focus();
    };
    ws.onmessage = (e) => {
      if (typeof e.data !== 'string') { S.term.write(new Uint8Array(e.data)); return; }
      let m;
      try { m = JSON.parse(e.data); } catch { return; }
      if (m.type === 'exit') endReason = `The shell exited (code ${m.code}).`;
      else if (m.type === 'closed' && m.reason === 'idle') endReason = `Closed after ${m.minutes} minutes without activity.`;
      else if (m.type === 'error') endReason = m.message;
    };
    ws.onclose = (e) => {
      S.open = false;
      S.ws = null;
      setModifier('ctrl', false);
      setModifier('alt', false);
      if (e.code === 4401) {  // unlock expired or revoked
        setStatus('offline', 'Locked');
        refresh();
        return;
      }
      ended(endReason || (e.code === 1000 ? 'Session ended.' : 'Connection lost, so the shell was closed.'));
    };
  }

  function ended(message) {
    S.ended = true;
    setStatus('offline', 'Not connected');
    $('term-ended-msg').textContent = message;
    $('term-ended').hidden = false;
  }

  $('term-restart').addEventListener('click', startSession);
  $('term-end').addEventListener('click', () => { if (S.ws) S.ws.close(1000); });
  $('term-lock-btn').addEventListener('click', async () => {
    if (S.ws) S.ws.close(1000);
    try { await fetch('/api/lock', { method: 'POST' }); } catch { /* the cookie expires anyway */ }
    S.ended = false;
    refresh();
  });

  function changeFont(delta) {
    if (!S.term) return;
    const size = Math.max(9, Math.min(24, S.term.options.fontSize + delta));
    S.term.options.fontSize = size;
    try { localStorage.setItem(FONT_KEY, String(size)); } catch { /* per-viewer nicety only */ }
    fitSoon();
  }
  $('term-font-dec').addEventListener('click', () => changeFont(-1));
  $('term-font-inc').addEventListener('click', () => changeFont(1));

  // ---------------------------------------------------------------- phone key bar
  const KEYS = { esc: '\x1b', tab: '\t', pipe: '|', tilde: '~', slash: '/', dash: '-' };
  const ARROWS = { up: 'A', down: 'B', right: 'C', left: 'D' };

  function setModifier(name, on) {
    S[name] = on;
    document.querySelector(`#keybar [data-key="${name}"]`).setAttribute('aria-pressed', String(on));
  }
  // Keep focus (and the phone keyboard) in the terminal while tapping keys.
  $('keybar').addEventListener('pointerdown', (e) => { if (e.target.closest('button')) e.preventDefault(); });
  $('keybar').addEventListener('click', (e) => {
    const key = e.target.closest('button')?.dataset.key;
    if (!key || !S.term) return;
    if (key === 'ctrl' || key === 'alt') setModifier(key, !S[key]);
    else if (ARROWS[key]) sendText((S.term.modes.applicationCursorKeysMode ? '\x1bO' : '\x1b[') + ARROWS[key]);
    else sendText(KEYS[key]);
    S.term.focus();
  });

  // ---------------------------------------------------------------- page wiring
  window.addEventListener('hd-tab', (e) => { if (e.detail === 'terminal') refresh(); });
  window.addEventListener('hd-themechange', () => {
    if (!S.term) return;
    S.term.options.theme = termTheme();
    S.term.options.minimumContrastRatio = isLightTheme() ? 4.5 : 1;
  });
  window.addEventListener('beforeunload', (e) => {
    if (S.open) { e.preventDefault(); e.returnValue = ''; }
  });
})();
