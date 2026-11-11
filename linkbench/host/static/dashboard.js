'use strict';
/* Live tab: start/stop runs and render the 5 Hz updates from the host. */

const $ = (id) => document.getElementById(id);
const MAX_PTS = 3000;

const LINK_DEFAULTS = {
  usb:   { transport: 'ws', addr: '10.55.0.1' },
  ap:    { transport: 'ws', addr: '10.42.0.1' },
  lan:   { transport: 'ws', addr: '' },
  bt:    { transport: 'rfcomm', addr: 'AA:BB:CC:DD:EE:FF' },
  ble:   { transport: 'ble', addr: 'linkbench' },
  local: { transport: 'ws', addr: '127.0.0.1' },
};

let meta = null;
let ws = null;
let charts = null;      // {latency, throughput, rtt, loss, names}
let buf = null;         // accumulated points per chart/series
let lastUpdate = null;

// ── tabs ───────────────────────────────────────────────────────────────────────
document.querySelectorAll('nav.tabs button').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('nav.tabs button').forEach(x => x.classList.toggle('active', x === b));
  $('tab-live').classList.toggle('hidden', b.dataset.tab !== 'live');
  $('tab-compare').classList.toggle('hidden', b.dataset.tab !== 'compare');
  if (b.dataset.tab === 'compare') window.loadRuns && window.loadRuns();
}));

// ── form ───────────────────────────────────────────────────────────────────────
$('f-link').addEventListener('change', () => {
  const d = LINK_DEFAULTS[$('f-link').value];
  $('f-transport').value = d.transport;
  $('f-addr').value = d.addr;
});
$('f-profile').addEventListener('change', describeProfile);

function describeProfile() {
  const p = meta && meta.presets[$('f-profile').value];
  if (!p) return;
  const streams = p.streams.map(s => `${s.name} ${s.rate_hz} Hz × ${s.sample_bytes * s.batch} B`).join(', ');
  const extra = p.bulk_bytes ? `bulk ${(p.bulk_bytes / 1048576).toFixed(1)} MB` :
    p.ramp ? `camera ${p.ramp.fps_steps.join('/')} fps × ${p.ramp.frame_bytes} B, ${p.ramp.step_s} s per step` :
    `${p.duration_s} s`;
  $('profile-desc').textContent = `${p.label}. ${streams ? streams + '; ' : ''}${extra}.`;
}

function config() {
  const num = (id) => $(id).value === '' ? null : Number($(id).value);
  return {
    link: $('f-link').value, transport: $('f-transport').value, addr: $('f-addr').value.trim(),
    port: num('f-port'), profile: $('f-profile').value, duration_s: num('f-duration'),
    source: $('f-source').value, note: $('f-note').value,
    faults: { drop_pct: num('f-drop') || 0, delay_ms: num('f-delay') || 0,
              jitter_ms: num('f-jitter') || 0, corrupt_pct: num('f-corrupt') || 0 },
  };
}

$('btn-start').addEventListener('click', () => {
  $('run-error').textContent = '';
  resetView();
  ws.send(JSON.stringify({ cmd: 'start', config: config() }));
});
$('btn-stop').addEventListener('click', () => ws.send(JSON.stringify({ cmd: 'stop' })));

function setRunning(r) {
  $('btn-start').disabled = r;
  $('btn-stop').disabled = !r;
}

// ── rendering ──────────────────────────────────────────────────────────────────
function resetView() {
  ['c-latency', 'c-throughput', 'c-rtt', 'c-loss'].forEach(id => { $(id).innerHTML = ''; });
  charts = null; buf = null;
  $('done-panel').classList.add('hidden');
  $('bulk-panel').classList.add('hidden');
  $('ramp-panel').classList.add('hidden');
  $('hists').innerHTML = '';
  $('table-scope').textContent = 'last 5 s';
}

function ensureCharts(names) {
  if (charts && charts.names.join() === names.join()) return;
  resetView();
  const s = names.map((n, i) => ({ label: n, color: LB.color(n, i) }));
  charts = {
    names,
    latency: LB.chart($('c-latency'), 'ms', s.map(x => ({ ...x, points: true }))),
    throughput: LB.chart($('c-throughput'), 'kB/s', s),
    loss: LB.chart($('c-loss'), '% lost', s, 200, { y: { range: (u, lo, hi) => [0, Math.max(1, (hi || 0) * 1.2)] } }),
    rtt: LB.chart($('c-rtt'), 'ms', [{ label: 'RTT', color: LB.css('--s1'), points: true }]),
  };
  buf = { latency: names.map(() => []), throughput: names.map(() => []),
          loss: names.map(() => []), rtt: [[]] };
}

function push(list, pts) {
  list.push(...pts);
  if (list.length > MAX_PTS) list.splice(0, list.length - MAX_PTS);
}

function stat(k, v, sub = '') {
  return `<div class="stat"><div class="k">${k}</div><div class="v">${v}${sub ? ` <small>${sub}</small>` : ''}</div></div>`;
}

function renderStrip(m) {
  const c = m.clock || {}, s = m.sysinfo || {}, cfg = m.config || {}, a = m.agent || {};
  const tr = a.transport || {};
  const wifi = s.wifi;
  const wifiText = wifi ? `${wifi.signal_dbm ?? '–'} dBm` : '–';
  const wifiSub = wifi ? `${wifi.tx_bitrate_mbps ?? '–'} Mbit/s${wifi.freq_mhz ? ' · ' + wifi.freq_mhz + ' MHz' : ''}` : '';
  const stateBadge = m.state === 'done'
    ? '<span class="badge na">done</span>' : `<span class="badge run">${m.state}</span>`;
  $('strip').innerHTML = [
    stat('State', stateBadge, `${LB.fmt(m.elapsed_s, 1)} s`),
    stat('Connection', `${cfg.link} / ${cfg.transport}`, tr.peer || ''),
    stat('Pi interface', a.iface || '–', tr.local || ''),
    stat('Board', (s.board || a.board || '–').replace('Raspberry Pi', 'Pi')),
    stat('Pi CPU', s.cpu_pct != null ? `${s.cpu_pct} %` : '–', s.temp_c != null ? `${s.temp_c} °C` : ''),
    stat('Throttling', LB.throttledText(s.throttled)),
    stat('Wi-Fi', wifiText, wifiSub),
    stat('RTT', `${LB.fmt(c.rtt_ms_p50)} ms`, `p95 ${LB.fmt(c.rtt_ms_p95)} · max ${LB.fmt(c.rtt_ms_max)}`),
    stat('Clock offset', `${LB.fmt(c.offset_ms, 3)} ms`, c.drift_ppm != null ? `drift ${c.drift_ppm} ppm` : 'drift: collecting'),
    stat('Ping loss', `${LB.fmt(c.ping_loss_pct, 1)} %`, `${c.pings_answered ?? 0} answered`),
    stat('Malformed frames', m.malformed ?? 0),
  ].join('');
}

function onUpdate(m) {
  lastUpdate = m;
  $('run-id').textContent = m.run_id || '';
  if (m.error) $('run-error').textContent = m.error;
  renderStrip(m);
  const streams = m.streams_window || [];
  const names = streams.map(s => s.name);
  if (names.length) {
    ensureCharts(names);
    LB.streamTable($('stream-table'), streams, m.sources, m.agent_stats);
    const t = m.elapsed_s;
    names.forEach((n, i) => {
      push(buf.latency[i], (m.series.latency[n] || []));
      push(buf.throughput[i], [[t, m.series.throughput[n] ?? 0]]);
      push(buf.loss[i], [[t, streams[i].loss_pct ?? null]]);
    });
    charts.latency.setData(LB.align(buf.latency));
    charts.throughput.setData(LB.align(buf.throughput));
    charts.loss.setData(LB.align(buf.loss));
    const nominal = Object.fromEntries(streams.map(s => [s.name, s.nominal_fps]));
    LB.histograms($('hists'), m.hist, nominal);
  } else if (!charts) {
    ensureCharts([]);
  }
  push(buf.rtt[0], m.series.rtt || []);
  charts.rtt.setData(LB.align(buf.rtt));

  if (m.bulk) {
    $('bulk-panel').classList.remove('hidden');
    $('bulk-bar').style.width = `${m.bulk.progress_pct || 0}%`;
    $('bulk-text').textContent = `${(m.bulk.received_bytes / 1048576).toFixed(2)} of ` +
      `${(m.bulk.expected_bytes / 1048576).toFixed(2)} MB · ${LB.fmt(m.bulk.MBps, 2)} MB/s · ` +
      `${m.bulk.chunks} chunks · ${m.bulk.integrity_errors} integrity errors`;
  }
  if (m.ramp && m.ramp.length) renderRamp(m.ramp);
}

function renderRamp(steps) {
  $('ramp-panel').classList.remove('hidden');
  $('ramp-table').innerHTML = `<table class="data"><thead><tr><th>step</th><th>camera fps</th>
    <th>frame B</th><th>offered kB/s</th><th>throughput kB/s</th><th>camera received Hz</th>
    <th>loss %</th><th>p95 ms</th><th>status</th></tr></thead><tbody>${steps.map(r => `<tr>
    <td>${r.step}</td><td>${r.camera_fps}</td><td>${r.frame_bytes}</td><td>${LB.fmt(r.offered_kBps, 1)}</td>
    <td>${LB.fmt(r.throughput_kBps, 1)}</td><td>${LB.fmt(r.camera_recv_hz, 1)}</td>
    <td>${LB.fmt(r.loss_pct)}</td><td>${LB.fmt(r.latency_ms_p95)}</td><td>${LB.badge(r.passed)}</td></tr>`).join('')}
    </tbody></table>`;
}

function onDone(s) {
  setRunning(false);
  if (lastUpdate) renderStrip({ ...lastUpdate, state: 'done', elapsed_s: s.duration_s, error: s.error });
  $('table-scope').textContent = 'whole run';
  LB.streamTable($('stream-table'), s.streams, s.sources, s.agent_stats);
  if (s.ramp && s.ramp.length) renderRamp(s.ramp);
  if (s.error) $('run-error').textContent = s.error;
  const b = s.bulk;
  const lines = [
    `<p>${LB.badge(s.passed)} &nbsp; <b>${s.run_id}</b> · ${s.profile_label} · ${LB.fmt(s.duration_s, 1)} s</p>`,
    b ? `<p>Bulk: ${(b.received_bytes / 1048576).toFixed(2)} MB in ${LB.fmt(b.seconds, 2)} s = <b>${LB.fmt(b.MBps, 2)} MB/s</b>,
         SHA-256 ${b.sha256_ok ? '<span class="badge pass">✓ match</span>' : '<span class="badge fail">✗ mismatch</span>'}
         ${b.missing_chunks ? `, ${b.missing_chunks} chunks missing` : ''}</p>` : '',
    s.max_sustainable_kBps ? `<p>Max sustainable throughput: <b>${s.max_sustainable_kBps} kB/s</b>
         ${s.ramp_ceiling_reached ? '(ramp ceiling reached – the link may carry more)' : ''}</p>` : '',
    `<p><a href="/api/runs/${s.run_id}/report" target="_blank">Open the full report</a>
       <span class="hint">· saved in linkbench/runs/${s.run_id}/</span></p>`,
  ];
  $('done-body').innerHTML = lines.join('');
  $('done-panel').classList.remove('hidden');
}

// ── socket ─────────────────────────────────────────────────────────────────────
function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.t === 'state') setRunning(m.running);
    else if (m.t === 'update') onUpdate(m);
    else if (m.t === 'done') onDone(m.summary);
    else if (m.t === 'error') $('run-error').textContent = m.message;
  };
  ws.onclose = () => setTimeout(connect, 1500);
}

fetch('/api/meta').then(r => r.json()).then(m => {
  meta = m;
  $('f-profile').innerHTML = Object.entries(m.presets)
    .map(([k, p]) => `<option value="${k}" ${k === 'full' ? 'selected' : ''}>${k} – ${p.label}</option>`).join('');
  describeProfile();
  setRunning(m.running);
  connect();
});
