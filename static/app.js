'use strict';
(() => {
  const HOUR = 3600;
  const CHART_H = 132;
  const $ = (id) => document.getElementById(id);

  // ---------------------------------------------------------------- helpers
  // All data from the server goes into the DOM as text, never as HTML.
  function el(tag, props, ...children) {
    const n = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (v == null || v === false) continue;
        if (k === 'class') n.className = v;
        else if (k === 'text') n.textContent = v;
        else n.setAttribute(k, v === true ? '' : v);
      }
    }
    for (const c of children) if (c != null) n.append(c);
    return n;
  }
  const setText = (id, v) => { const n = $(id); if (n && n.textContent !== v) n.textContent = v; };
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  function withAlpha(hex, a) {
    const m = /^#([0-9a-f]{6})$/i.exec(hex);
    if (!m) return hex;
    const n = parseInt(m[1], 16);
    return `rgba(${n >> 16}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
  }
  function isLightTheme() {
    const t = document.documentElement.getAttribute('data-theme');
    return t ? t === 'light' : !matchMedia('(prefers-color-scheme: dark)').matches;
  }

  const IEC = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  function fmtBytes(b) {
    if (b == null) return '–';
    let i = 0;
    while (Math.abs(b) >= 1024 && i < IEC.length - 1) { b /= 1024; i++; }
    return `${b.toFixed(i === 0 || b >= 10 ? 0 : 1)} ${IEC[i]}`;
  }
  const fmtRate = (b) => (b == null ? '–' : `${fmtBytes(b)}/s`);
  const fmtPct = (v) => (v == null ? '–' : `${v > 0 && v < 10 ? v.toFixed(1) : Math.round(v)}%`);
  const fmtTemp = (v) => (v == null ? '–' : `${Math.round(v)}°C`);
  function fmtDuration(s) {
    if (s == null) return '–';
    s = Math.max(0, Math.round(s));
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    if (d) return `${d}d ${h}h`;
    if (h) return `${h}h ${m}m`;
    if (m) return `${m}m`;
    return `${s}s`;
  }
  const fmtClock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const fmtClockSec = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

  window.HD = { el, css, withAlpha, fmtDuration, isLightTheme };

  function meter(pct, sev) {
    const v = Math.max(0, Math.min(100, pct ?? 0));
    const fill = el('i');
    fill.style.width = `${v}%`;
    return el('div', { class: `meter ${sev}`, role: 'meter', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(Math.round(v)) }, fill);
  }
  const severity = (pct, warn, crit) => (pct == null ? '' : pct >= crit ? 'crit' : pct >= warn ? 'warn' : '');
  function chip(kind, icon, label) {
    return el('span', { class: `chip ${kind}` }, el('span', { class: 'icon', 'aria-hidden': 'true', text: icon }), label);
  }

  // ---------------------------------------------------------------- theme
  const THEMES = ['dark', 'light', 'system'];
  const THEME_LABEL = { dark: 'Dark', light: 'Light', system: 'Auto' };
  function themePref() { try { return localStorage.getItem('hd-theme') || 'dark'; } catch { return 'dark'; } }
  function applyTheme(pref) {
    if (pref === 'system') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', pref);
    try { localStorage.setItem('hd-theme', pref); } catch { /* per-viewer nicety only */ }
    $('theme-btn').textContent = THEME_LABEL[pref];
    window.dispatchEvent(new Event('hd-themechange'));
  }
  $('theme-btn').textContent = THEME_LABEL[themePref()];
  $('theme-btn').addEventListener('click', () => applyTheme(THEMES[(THEMES.indexOf(themePref()) + 1) % THEMES.length]));
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => window.dispatchEvent(new Event('hd-themechange')));

  // ---------------------------------------------------------------- history & charts
  const HIST_KEYS = ['cpu', 'mem', 'swap', 'gpu_util', 'gpu_mem', 'cpu_temp', 'gpu_temp', 'disk_r', 'disk_w', 'net_rx', 'net_tx', 'bat'];
  let hist = null;

  function pushPoint(p) {
    hist.t.push(p.t);
    for (const k of HIST_KEYS) hist[k].push(p[k] ?? null);
    const cutoff = p.t - HOUR;
    let n = 0;
    while (n < hist.t.length && hist.t[n] < cutoff) n++;
    if (n) {
      hist.t.splice(0, n);
      for (const k of HIST_KEYS) hist[k].splice(0, n);
    }
  }

  function niceBytes(max) {
    if (!(max > 0)) return 1024;
    let unit = 1;
    while (max >= unit * 1024) unit *= 1024;
    for (const m of [1, 2, 5, 10, 20, 50, 100, 200, 500]) if (m * unit >= max) return m * unit;
    return 1024 * unit;
  }
  const pctAxis = { size: 44, range: () => [0, 100], splits: () => [0, 50, 100], label: (v) => `${v}%` };
  const tempAxis = {
    size: 44,
    range: (u, min, max) => [
      Math.min(30, Math.floor((Number.isFinite(min) ? min : 30) / 10) * 10),
      Math.max(100, Math.ceil((Number.isFinite(max) ? max : 100) / 10) * 10),
    ],
    splits: (u, i, min, max) => { const out = []; for (let v = Math.ceil(min / 20) * 20; v <= max; v += 20) out.push(v); return out; },
    label: (v) => `${v}°`,
  };
  const bytesAxis = {
    size: 64,
    range: (u, min, max) => [0, niceBytes(Number.isFinite(max) ? max : 0)],
    splits: (u, i, min, max) => [0, max / 2, max],
    label: (v) => (v === 0 ? '0' : fmtBytes(v)),
  };

  // Two series at most per chart: slots 1–2 of the validated categorical palette.
  const CHARTS = [
    { id: 'cpu', axis: pctAxis, fill: true, series: [{ key: 'cpu', label: 'CPU', fmt: fmtPct }] },
    { id: 'mem', axis: pctAxis, series: [{ key: 'mem', label: 'RAM', fmt: fmtPct }, { key: 'swap', label: 'Swap', fmt: fmtPct }] },
    { id: 'gpu', axis: pctAxis, series: [{ key: 'gpu_util', label: 'Utilization', fmt: fmtPct }, { key: 'gpu_mem', label: 'VRAM', fmt: fmtPct }] },
    { id: 'temp', axis: tempAxis, series: [{ key: 'cpu_temp', label: 'CPU package', fmt: fmtTemp }, { key: 'gpu_temp', label: 'GPU', fmt: fmtTemp }] },
    { id: 'disk', axis: bytesAxis, series: [{ key: 'disk_r', label: 'Read', fmt: fmtRate }, { key: 'disk_w', label: 'Write', fmt: fmtRate }] },
    { id: 'net', axis: bytesAxis, series: [{ key: 'net_rx', label: 'Download', fmt: fmtRate }, { key: 'net_tx', label: 'Upload', fmt: fmtRate }] },
    { id: 'bat', axis: pctAxis, fill: true, series: [{ key: 'bat', label: 'Charge', fmt: fmtPct }] },
  ];
  const seriesColors = () => [css('--s1'), css('--s2')];
  const chartData = (spec) => [hist.t, ...spec.series.map((s) => hist[s.key])];

  function buildChart(spec) {
    const host = $(`chart-${spec.id}`);
    if (spec.u) spec.u.destroy();
    host.replaceChildren();
    const colors = seriesColors();
    const muted = css('--muted');
    const font = '11px system-ui, -apple-system, "Segoe UI", sans-serif';
    const tip = el('div', { class: 'tip', hidden: true });

    const opts = {
      width: Math.max(host.clientWidth, 120),
      height: CHART_H,
      padding: [8, 24, 0, 0],  // room for the last time label at the right edge
      legend: { show: false },
      cursor: { y: false, points: { show: false }, drag: { x: false, y: false, setScale: false } },
      scales: {
        x: { time: true, range: () => { const end = hist.t.length ? hist.t[hist.t.length - 1] : Date.now() / 1000; return [end - HOUR, end]; } },
        y: { range: spec.axis.range },
      },
      axes: [
        { stroke: muted, font, size: 24, gap: 4, space: 70, grid: { show: false },
          ticks: { stroke: css('--axis'), width: 1, size: 4 }, values: (u, splits) => splits.map(fmtClock) },
        { stroke: muted, font, size: spec.axis.size, gap: 4, grid: { stroke: css('--grid'), width: 1 },
          ticks: { show: false }, splits: spec.axis.splits, values: (u, splits) => splits.map(spec.axis.label) },
      ],
      series: [{}, ...spec.series.map((s, i) => ({
        label: s.label, stroke: colors[i], width: 2, points: { show: false },
        fill: spec.fill ? withAlpha(colors[i], 0.1) : undefined,
      }))],
      hooks: { setCursor: [(u) => moveTip(u, spec, host, tip, colors)] },
    };
    spec.u = new uPlot(opts, chartData(spec), host);
    host.append(tip);
  }

  function moveTip(u, spec, host, tip, colors) {
    const i = u.cursor.idx;
    // Only show a readout when a sample is actually near the pointer (history can have gaps).
    if (i == null || u.cursor.left == null || u.cursor.left < 0 || u.data[0][i] == null
        || Math.abs(u.valToPos(u.data[0][i], 'x') - u.cursor.left) > 12) { tip.hidden = true; return; }
    tip.replaceChildren(el('div', { class: 'tip-time', text: fmtClockSec(u.data[0][i]) }));
    spec.series.forEach((s, k) => {
      const key = el('span', { class: 'key' });
      key.style.background = colors[k];
      tip.append(el('div', { class: 'tip-row' }, key, el('span', { class: 'v', text: s.fmt(u.data[k + 1][i]) }), el('span', { class: 'n', text: s.label })));
    });
    tip.hidden = false;
    const over = u.over.getBoundingClientRect(), box = host.getBoundingClientRect();
    const x = over.left - box.left + u.cursor.left;
    const w = tip.offsetWidth;
    tip.style.left = `${x + 12 + w > box.width ? Math.max(0, x - 12 - w) : x + 12}px`;
  }

  function summarize(arr) {
    let last = null, sum = 0, n = 0, max = null;
    for (const v of arr) {
      if (v == null) continue;
      last = v; sum += v; n++;
      if (max == null || v > max) max = v;
    }
    return { last, avg: n ? sum / n : null, max };
  }

  // Legends double as the text view of each chart: now, 1h average and 1h peak.
  function renderLegend(spec) {
    const host = $(`legend-${spec.id}`);
    const colors = seriesColors();
    if (spec.series.length === 1) {
      const s = spec.series[0], st = summarize(hist[s.key]);
      host.replaceChildren(
        el('span', { class: 'item' }, 'Last hour: avg ', el('span', { class: 'v', text: s.fmt(st.avg) })),
        el('span', { class: 'item' }, 'peak ', el('span', { class: 'v', text: s.fmt(st.max) })),
      );
      return;
    }
    host.replaceChildren(...spec.series.map((s, k) => {
      const st = summarize(hist[s.key]);
      const key = el('span', { class: 'key' });
      key.style.background = colors[k];
      return el('span', { class: 'item' }, key, s.label, el('span', { class: 'v', text: s.fmt(st.last) }), `peak ${s.fmt(st.max)}`);
    }));
  }

  const overviewVisible = () => !$('view-overview').hidden && !document.hidden;
  function refreshCharts() {
    if (!hist || !overviewVisible()) return;
    for (const spec of CHARTS) {
      if (!spec.u) buildChart(spec);
      else spec.u.setData(chartData(spec));
      renderLegend(spec);
    }
  }
  function rebuildCharts() {
    if (!hist) return;
    for (const spec of CHARTS) { buildChart(spec); renderLegend(spec); }
  }
  window.addEventListener('hd-themechange', rebuildCharts);
  document.addEventListener('visibilitychange', refreshCharts);

  const ro = new ResizeObserver((entries) => {
    for (const e of entries) {
      const spec = CHARTS.find((c) => `chart-${c.id}` === e.target.id);
      const w = Math.floor(e.contentRect.width);
      if (spec?.u && w > 0 && w !== spec.u.width) spec.u.setSize({ width: w, height: CHART_H });
    }
  });
  for (const spec of CHARTS) ro.observe($(`chart-${spec.id}`));

  // ---------------------------------------------------------------- live cards
  let host = null;

  function renderSample(s) {
    if (!s) return;
    setText('host-sub', `${host?.os ?? ''} · up ${fmtDuration(s.uptime)}`);

    // CPU
    setText('cpu-total', fmtPct(s.cpu.total));
    setText('cpu-load', s.cpu.load.map((v) => v.toFixed(2)).join(' / '));
    setText('cpu-freq', s.cpu.freq_mhz ? `${(s.cpu.freq_mhz / 1000).toFixed(2)} GHz` : '–');
    $('cpu-cores').replaceChildren(...s.cpu.cores.map((v, i) =>
      el('div', { class: 'bar-line' }, el('span', { text: `Core ${i}` }), meter(v, ''), el('span', { class: 'v', text: fmtPct(v) }))));

    // Memory
    const m = s.mem;
    setText('mem-total', `${fmtBytes(m.total)} total`);
    setText('mem-pct', fmtPct(m.percent));
    setText('mem-used', fmtBytes(m.used));
    setText('mem-cache', fmtBytes(m.cached));
    setText('swap-used', m.swap_total ? `${fmtBytes(m.swap_used)} of ${fmtBytes(m.swap_total)}` : 'None');
    $('mem-meter').replaceChildren(meter(m.percent, severity(m.percent, 85, 95)));

    renderGpu(s.gpu);

    // Temperature
    const gpuTemp = ['active', 'idle'].includes(s.gpu.state) ? s.gpu.temp : null;
    setText('temp-cpu', fmtTemp(s.cpu.temp));
    setText('temp-gpu', gpuTemp == null ? '–' : fmtTemp(gpuTemp));
    setText('temp-cores', s.cpu.core_temps.length ? s.cpu.core_temps.map((v) => Math.round(v)).join(' · ') + '°C' : '–');
    const hottest = Math.max(s.cpu.temp ?? 0, gpuTemp ?? 0);
    $('temp-chip').replaceChildren(
      hottest >= 90 ? chip('crit', '▲', 'Very hot') : hottest >= 80 ? chip('serious', '▲', 'Hot') : chip('good', '●', 'Normal'));

    // Disks
    setText('disk-read', fmtRate(s.disk_io.read));
    setText('disk-write', fmtRate(s.disk_io.write));
    $('disk-mounts').replaceChildren(...s.disks.map((d) => el('div', {},
      el('div', { class: 'mount-top' }, el('span', { class: 'path', title: d.device, text: d.mount }),
        el('span', { class: 'v', text: `${fmtBytes(d.used)} of ${fmtBytes(d.total)} · ${fmtPct(d.percent)}` })),
      meter(d.percent, severity(d.percent, 85, 95)))));

    // Network
    setText('net-rx', fmtRate(s.net.rx));
    setText('net-tx', fmtRate(s.net.tx));
    const shown = s.net.ifaces.filter((i) => i.up && (i.physical || i.name.startsWith('tailscale')));
    $('net-ifaces').replaceChildren(...shown.map((i) =>
      el('li', {}, el('span', { class: 'name', text: i.name }), el('span', { class: 'v', text: `↓ ${fmtRate(i.rx)}  ↑ ${fmtRate(i.tx)}` }))));

    renderBattery(s.battery);
  }

  function renderGpu(g) {
    const note = $('gpu-note');
    setText('gpu-name', g.name || '');
    $('card-gpu').hidden = g.state === 'none';
    const live = g.state === 'active' || g.state === 'idle';
    setText('gpu-util', live ? fmtPct(g.util) : g.state === 'asleep' ? 'Asleep' : '–');
    setText('gpu-vram', live && g.mem_total ? `${fmtBytes(g.mem_used)} of ${fmtBytes(g.mem_total)}` : '–');
    setText('gpu-power', live && g.power_w != null ? `${g.power_w} W` : live ? 'Not reported' : '–');
    const notes = {
      asleep: 'Powered down. Not polled while asleep, so the dashboard never wakes it.',
      idle: 'Idle. Checked every 30 s so it can power down.',
      error: `Can't read the GPU: ${g.error || 'unknown error'}`,
    };
    note.hidden = !notes[g.state];
    note.textContent = notes[g.state] || '';
    const procs = live ? [...(g.procs || [])].sort((a, b) => (b.mem ?? 0) - (a.mem ?? 0)) : [];
    $('gpu-procs').replaceChildren(...procs.map((p) =>
      el('li', {}, el('span', { class: 'name', text: `${p.name} (${p.pid})` }), el('span', { class: 'v', text: p.mem != null ? fmtBytes(p.mem) : '' }))));
    gpuPids = new Set(procs.map((p) => p.pid));
  }

  function renderBattery(b) {
    $('card-bat').hidden = !b;
    if (!b) return;
    setText('bat-pct', fmtPct(b.percent));
    setText('bat-state', b.status);
    setText('bat-plug', b.plugged ? '⚡ On AC power' : 'On battery');
    setText('bat-power', b.watts != null ? `${b.watts} W` : '–');
    setText('bat-time', b.secs ? (b.status === 'Charging' ? `${fmtDuration(b.secs)} to full` : `${fmtDuration(b.secs)} left`) : '–');
  }

  // ---------------------------------------------------------------- processes
  let gpuPids = new Set();
  const proc = { rows: [], sort: 'cpu', desc: true, q: '', group: false, all: false };
  const LIMIT = 60;
  const PROC_COLS = [
    { key: 'pid', label: 'PID', num: true, get: (r) => r[0] },
    { key: 'name', label: 'Name', get: (r) => r[1].toLowerCase() },
    { key: 'user', label: 'User', cls: 'hide-sm', get: (r) => r[2] },
    { key: 'cpu', label: 'CPU %', num: true, get: (r) => r[3] },
    { key: 'mem', label: 'Memory', num: true, get: (r) => r[4] },
    { key: 'status', label: 'State', cls: 'hide-sm', get: (r) => r[5] },
    { key: 'cmd', label: 'Command', cls: 'hide-sm', get: (r) => r[6] },
  ];
  const GROUP_COLS = [
    { key: 'name', label: 'App', get: (g) => g.name.toLowerCase() },
    { key: 'count', label: 'Processes', num: true, get: (g) => g.count },
    { key: 'user', label: 'Users', cls: 'hide-sm', get: (g) => g.users },
    { key: 'cpu', label: 'CPU %', num: true, get: (g) => g.cpu },
    { key: 'mem', label: 'Memory', num: true, get: (g) => g.mem },
  ];

  function renderProcHead() {
    const cols = proc.group ? GROUP_COLS : PROC_COLS;
    if (!cols.some((c) => c.key === proc.sort)) { proc.sort = 'cpu'; proc.desc = true; }
    $('proc-head').replaceChildren(...cols.map((c) => el('th', {
      class: ['sortable', c.num && 'num', c.cls].filter(Boolean).join(' '),
      scope: 'col', 'data-key': c.key, tabindex: '0',
      'aria-sort': proc.sort === c.key ? (proc.desc ? 'descending' : 'ascending') : null,
      text: c.label,
    })));
  }

  function renderProcs() {
    const q = proc.q.trim().toLowerCase();
    let rows = proc.rows;
    if (q) rows = rows.filter((r) => String(r[0]) === q || r[1].toLowerCase().includes(q) || r[2].toLowerCase().includes(q) || r[6].toLowerCase().includes(q));

    let items = rows, cols = PROC_COLS;
    if (proc.group) {
      const groups = new Map();
      for (const r of rows) {
        const g = groups.get(r[1]) || { name: r[1], count: 0, cpu: 0, mem: 0, userSet: new Set(), gpu: false };
        g.count++; g.cpu += r[3]; g.mem += r[4]; g.userSet.add(r[2]); g.gpu ||= gpuPids.has(r[0]);
        groups.set(r[1], g);
      }
      items = [...groups.values()].map((g) => ({ ...g, cpu: Math.round(g.cpu * 10) / 10, users: [...g.userSet].join(', ') }));
      cols = GROUP_COLS;
    }
    const col = cols.find((c) => c.key === proc.sort);
    const dir = proc.desc ? -1 : 1;
    items = [...items].sort((a, b) => {
      const x = col.get(a), y = col.get(b);
      return (x < y ? -1 : x > y ? 1 : 0) * dir;
    });

    const total = items.length;
    const visible = proc.all ? items : items.slice(0, LIMIT);
    const frag = document.createDocumentFragment();
    for (const it of visible) {
      if (proc.group) {
        frag.append(el('tr', {},
          el('td', { class: 'name', title: it.name }, it.name, it.gpu ? el('span', { class: 'badge', text: 'GPU' }) : null),
          el('td', { class: 'num', text: String(it.count) }),
          el('td', { class: 'dim hide-sm', text: it.users }),
          el('td', { class: 'num', text: it.cpu.toFixed(1) }),
          el('td', { class: 'num', text: fmtBytes(it.mem) })));
      } else {
        frag.append(el('tr', {},
          el('td', { class: 'num dim', text: String(it[0]) }),
          el('td', { class: 'name', title: it[1] }, it[1], gpuPids.has(it[0]) ? el('span', { class: 'badge', text: 'GPU' }) : null),
          el('td', { class: 'dim hide-sm', text: it[2] }),
          el('td', { class: 'num', text: it[3].toFixed(1) }),
          el('td', { class: 'num', text: fmtBytes(it[4]) }),
          el('td', { class: 'dim hide-sm', text: it[5] }),
          el('td', { class: 'cmd hide-sm', title: it[6], text: it[6] || `[${it[1]}]` })));
      }
    }
    $('proc-body').replaceChildren(frag);
    const noun = proc.group ? 'apps' : 'processes';
    setText('proc-count', total > visible.length ? `Top ${visible.length} of ${total} ${noun} · CPU % where 100 = one core` : `${total} ${noun} · CPU % where 100 = one core`);
    const more = $('proc-more');
    more.hidden = total <= LIMIT;
    more.textContent = proc.all ? `Show top ${LIMIT}` : `Show all ${total}`;
  }

  function sortBy(key) {
    if (proc.sort === key) proc.desc = !proc.desc;
    else { proc.sort = key; proc.desc = !['name', 'user', 'status', 'cmd'].includes(key); }
    renderProcHead();
    renderProcs();
  }
  $('proc-head').addEventListener('click', (e) => { const th = e.target.closest('th[data-key]'); if (th) sortBy(th.dataset.key); });
  $('proc-head').addEventListener('keydown', (e) => {
    const th = e.target.closest('th[data-key]');
    if (th && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); sortBy(th.dataset.key); }
  });
  $('proc-search').addEventListener('input', (e) => { proc.q = e.target.value; renderProcs(); });
  $('proc-group').addEventListener('change', (e) => { proc.group = e.target.checked; renderProcHead(); renderProcs(); });
  $('proc-more').addEventListener('click', () => { proc.all = !proc.all; renderProcs(); });
  renderProcHead();

  // ---------------------------------------------------------------- docker, services, ports
  function renderDocker(d) {
    const body = $('docker-body');
    if (!d.available) { body.replaceChildren(el('p', { class: 'note', text: d.error })); setText('docker-count', ''); return; }
    const list = [...d.containers].sort((a, b) => (a.state === 'running' ? 0 : 1) - (b.state === 'running' ? 0 : 1) || a.name.localeCompare(b.name));
    const running = list.filter((c) => c.state === 'running').length;
    setText('docker-count', `${running} running · ${list.length} total`);
    if (!list.length) { body.replaceChildren(el('p', { class: 'note', text: 'No containers.' })); return; }
    const stateChip = (c) => {
      if (/unhealthy/.test(c.status)) return chip('crit', '▲', 'Unhealthy');
      if (c.state === 'running') return chip('good', '●', /healthy/.test(c.status) ? 'Healthy' : 'Running');
      if (c.state === 'restarting') return chip('serious', '▲', 'Restarting');
      if (c.state === 'paused') return chip('warn', '■', 'Paused');
      return chip('idle', '○', c.state.charAt(0).toUpperCase() + c.state.slice(1));
    };
    body.replaceChildren(el('div', { class: 'table-wrap' }, el('table', { class: 'table' },
      el('thead', {}, el('tr', {},
        el('th', { scope: 'col', text: 'Container' }), el('th', { scope: 'col', text: 'State' }),
        el('th', { scope: 'col', class: 'num', text: 'CPU %' }), el('th', { scope: 'col', class: 'num', text: 'Memory' }),
        el('th', { scope: 'col', class: 'hide-sm', text: 'Status' }))),
      el('tbody', {}, ...list.map((c) => el('tr', {},
        el('td', {}, c.name, el('span', { class: 'sub', title: c.image, text: c.image })),
        el('td', {}, stateChip(c)),
        el('td', { class: 'num', text: c.cpu == null ? '–' : c.cpu.toFixed(1) }),
        el('td', { class: 'num', text: c.mem == null ? '–' : fmtBytes(c.mem) }),
        el('td', { class: 'dim hide-sm', text: c.status })))))));
  }

  function renderServices(sv) {
    const failed = sv.failed;
    const nSys = sv.running.system.length, nUser = sv.running.user.length;
    setText('svc-count', `${nSys} system · ${nUser} user running`);
    const box = $('svc-failed');
    const parts = [];
    if (!failed.length) parts.push(el('p', { class: 'note' }, chip('good', '✓', 'No failed units')));
    else {
      parts.push(el('ul', { class: 'svc-list' }, ...failed.map((u) => el('li', {},
        chip('crit', '▲', 'Failed'),
        el('span', { class: 'unit', text: `${u.unit}${u.scope === 'user' ? ' (user)' : ''}` }),
        el('span', { class: 'desc', text: u.description })))));
    }
    for (const err of sv.errors) parts.push(el('p', { class: 'note small', text: err }));
    box.replaceChildren(...parts);
    const names = (rows) => el('ul', { class: 'svc-names' }, ...rows.map((r) => el('li', { title: r.description, text: r.unit.replace(/\.service$/, '') })));
    $('svc-running').replaceChildren(
      el('h3', { text: `User (${nUser})` }), names(sv.running.user),
      el('h3', { text: `System (${nSys})` }), names(sv.running.system));
  }

  function addrLabel(ip) {
    if (ip === '0.0.0.0' || ip === '::') return 'all interfaces';
    if (ip === '127.0.0.1' || ip === '::1') return 'localhost';
    if (ip.startsWith('127.')) return `${ip} (local)`;
    if (/^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\./.test(ip) || ip.startsWith('fd7a:115c:a1e0')) return `${ip} (tailscale)`;
    return ip;
  }
  function renderPorts(ports) {
    const tcp = ports.filter((p) => p.proto.startsWith('tcp'));
    setText('ports-count', `${tcp.length} TCP · ${ports.length - tcp.length} UDP`);
    $('ports-body').replaceChildren(el('table', { class: 'table' },
      el('thead', {}, el('tr', {},
        el('th', { scope: 'col', class: 'num', text: 'Port' }), el('th', { scope: 'col', text: 'Proto' }),
        el('th', { scope: 'col', text: 'Address' }), el('th', { scope: 'col', text: 'Process' }))),
      el('tbody', {}, ...ports.map((p) => el('tr', {},
        el('td', { class: 'num', text: String(p.port) }),
        el('td', { class: 'dim', text: p.proto }),
        el('td', { class: 'dim', text: addrLabel(p.ip) }),
        el('td', { text: p.process ? `${p.process} (${p.pid})` : p.pid ? `pid ${p.pid}` : '–' }))))));
  }

  function siteTile(s) {
    const label = s.name || (s.kind === 'tcp' ? `TCP port ${s.port}` : (s.url || '').replace(/^https?:\/\//, ''));
    const link = s.url && /^https?:\/\//.test(s.url)
      ? el('a', { class: 'site-name', href: s.url, target: '_blank', rel: 'noopener noreferrer', title: `Open ${s.url}`, text: `${label} ↗` })
      : el('span', { class: 'site-name', text: label });
    let status;
    if (s.self) status = chip('good', '●', 'This dashboard');
    else if (!s.probe) status = chip('idle', '○', { files: 'Files', text: 'Text', tcp: 'TCP forward' }[s.kind] || 'Not checked');
    else if (s.probe.up) status = chip('good', '●', `Up · ${s.probe.ms} ms`);
    else status = chip('crit', '▲', 'Down');
    const problem = s.probe && (!s.probe.up ? s.probe.error || `HTTP ${s.probe.status}` : s.probe.status >= 400 ? `HTTP ${s.probe.status}` : null);
    return el('div', { class: 'site' },
      el('div', { class: 'site-top' }, link, status),
      el('div', { class: 'site-url', text: s.url || `port ${s.port}` }),
      el('div', { class: 'site-meta' },
        el('span', { text: `→ ${s.target}` }),
        s.owner && s.owner !== s.name ? el('span', { text: s.owner }) : null,
        s.serve === false
          ? chip('idle', '○', 'Not via serve')
          : s.public ? chip('serious', '▲', 'Public (Funnel)') : chip('idle', '●', 'Tailnet only')),
      problem ? el('div', { class: 'site-err', text: problem }) : null);
  }

  function renderSites(d) {
    const sites = d.sites || [];
    const checked = sites.filter((s) => s.probe);
    const up = checked.filter((s) => s.probe.up).length;
    setText('sites-count', sites.length ? `${sites.length} sites · ${up} of ${checked.length} up` : '');
    const body = $('sites-body');
    if (d.error) body.replaceChildren(el('p', { class: 'note', text: d.error }));
    else if (!sites.length) body.replaceChildren(el('p', { class: 'note', text: 'Nothing published with tailscale serve, and no links in sites.json.' }));
    else body.replaceChildren(
      ...(d.config_error ? [el('p', { class: 'note', text: d.config_error })] : []),
      el('div', { class: 'sites' }, ...sites.map(siteTile)));
  }

  function applyViews(m) {
    if (m.sites) renderSites(m.sites);
    if (m.procs) { proc.rows = m.procs.rows; renderProcs(); }
    if (m.docker) renderDocker(m.docker);
    if (m.system) { renderServices(m.system.services); renderPorts(m.system.ports); }
  }

  // ---------------------------------------------------------------- connection
  let ws = null, retry = 0, lastMsg = 0;

  function setConn(state, text) {
    $('conn').dataset.state = state;
    setText('conn-text', text);
  }

  function connect() {
    setConn('connecting', 'Connecting…');
    ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/stats`);
    ws.onmessage = (e) => {
      const m = JSON.parse(e.data);
      lastMsg = Date.now();
      if (m.type === 'init') {
        retry = 0;
        host = m.host;
        hist = m.history;
        setText('hostname', host.hostname);
        setText('cpu-model', host.cpu_model);
        document.title = `${host.hostname} · Home dashboard`;
        renderSample(m.sample);
        applyViews(m);
        rebuildCharts();
        setConn('live', 'Live');
      } else if (m.type === 'tick') {
        pushPoint(m.p);
        renderSample(m.s);
        applyViews(m);
        refreshCharts();
        setConn('live', 'Live');
      }
    };
    ws.onclose = () => {
      ws = null;
      setConn('offline', 'Offline, retrying…');
      setTimeout(connect, Math.min(1000 * 2 ** retry++, 15000));
    };
  }
  setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN && lastMsg && Date.now() - lastMsg > 8000) setConn('connecting', 'No updates…');
  }, 2000);

  // ---------------------------------------------------------------- tabs
  const TABS = ['overview', 'terminal', 'claude'];
  function showTab(name) {
    for (const t of TABS) {
      $(`tab-${t}`).setAttribute('aria-selected', String(t === name));
      $(`view-${t}`).hidden = t !== name;
    }
    document.body.classList.toggle('term-mode', name === 'terminal');
    history.replaceState(null, '', name === 'overview' ? location.pathname : `#${name}`);
    if (name === 'overview') {
      for (const spec of CHARTS) if (spec.u) spec.u.setSize({ width: $(`chart-${spec.id}`).clientWidth, height: CHART_H });
      refreshCharts();
    }
    window.dispatchEvent(new CustomEvent('hd-tab', { detail: name }));
  }
  for (const t of TABS) $(`tab-${t}`).addEventListener('click', () => showTab(t));

  document.addEventListener('DOMContentLoaded', () => {
    showTab(TABS.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'overview');
    connect();
  });
})();
