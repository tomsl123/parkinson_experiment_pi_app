'use strict';
/* Compare tab: recorded runs side by side. */

(() => {
  const $ = (id) => document.getElementById(id);
  let runs = [];
  const selected = new Set();

  // Worst value over the streams that were judged (ramp camera is excluded).
  const judged = (r) => (r.streams || []).filter(s => s.passed !== null && s.received);
  const worst = (r, key, abs = false) => {
    const v = judged(r).map(s => s[key]).filter(x => x !== null && x !== undefined);
    if (!v.length) return null;
    return Math.max(...v.map(x => abs ? Math.abs(x) : x));
  };

  const METRICS = [
    { label: 'Worst p95 latency (ms) – lower is better', get: r => worst(r, 'latency_ms_p95') },
    { label: 'Worst loss (%) – lower is better', get: r => worst(r, 'loss_pct') },
    { label: 'Worst rate error (%) – lower is better', get: r => worst(r, 'rate_error_pct', true) },
    { label: 'Worst inter-arrival jitter (ms) – lower is better', get: r => worst(r, 'interarrival_ms_std') },
    { label: 'Median RTT (ms) – lower is better', get: r => r.clock && r.clock.rtt_ms_p50 },
    { label: 'Max sustainable throughput (kB/s) – higher is better', get: r => r.max_sustainable_kBps },
    { label: 'Bulk transfer (MB/s) – higher is better', get: r => r.bulk && r.bulk.MBps },
  ];

  const short = (r) => `${r.config.link} · ${r.config.transport} · ${r.config.profile}` +
    (r.config.note ? ` · ${r.config.note}` : '');

  function renderTable() {
    if (!runs.length) { $('runs-table').innerHTML = '<p class="hint">No runs recorded yet.</p>'; return; }
    $('runs-table').innerHTML = `<table class="data"><thead><tr><th></th><th class="l">run</th>
      <th class="l">connection</th><th>status</th><th>worst loss %</th><th>worst p95 ms</th>
      <th>RTT p50 ms</th><th>max kB/s</th><th>bulk MB/s</th><th></th></tr></thead><tbody>${runs.map(r => `
      <tr><td><input type="checkbox" data-id="${r.run_id}" ${selected.has(r.run_id) ? 'checked' : ''}></td>
      <td class="l">${r.started_at.replace('T', ' ')}</td><td class="l">${short(r)}</td>
      <td>${LB.badge(r.passed)}</td><td>${LB.fmt(worst(r, 'loss_pct'))}</td>
      <td>${LB.fmt(worst(r, 'latency_ms_p95'))}</td><td>${LB.fmt(r.clock && r.clock.rtt_ms_p50)}</td>
      <td>${LB.fmt(r.max_sustainable_kBps, 1)}</td><td>${LB.fmt(r.bulk && r.bulk.MBps)}</td>
      <td><a href="/api/runs/${r.run_id}/report" target="_blank">report</a></td></tr>`).join('')}
      </tbody></table>`;
    $('runs-table').querySelectorAll('input[type=checkbox]').forEach(cb => cb.addEventListener('change', () => {
      cb.checked ? selected.add(cb.dataset.id) : selected.delete(cb.dataset.id);
      renderBars();
    }));
  }

  function renderBars() {
    const sel = runs.filter(r => selected.has(r.run_id));
    if (sel.length < 1) { $('compare-bars').innerHTML = '<p class="hint">Select runs above.</p>'; return; }
    $('compare-bars').innerHTML = METRICS.map(m => {
      const vals = sel.map(r => ({ r, v: m.get(r) })).filter(x => x.v !== null && x.v !== undefined);
      if (!vals.length) return '';
      const max = Math.max(...vals.map(x => x.v), 1e-9);
      return `<div class="panel bars-h" style="background:var(--surface-2)"><h2>${m.label}</h2>${vals.map(x => `
        <div class="row" title="${x.r.run_id}"><span>${short(x.r)}</span>
        <div><div class="bar" style="width:${(100 * x.v / max).toFixed(1)}%"></div></div>
        <span class="val">${LB.fmt(x.v)}</span></div>`).join('')}</div>`;
    }).join('');
  }

  window.loadRuns = () => fetch('/api/runs').then(r => r.json()).then(list => {
    runs = list;
    renderTable();
    renderBars();
  });
})();
