'use strict';
/* Shared helpers for the dashboard and the HTML reports. */

const LB = (() => {
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  // Colour follows the stream, never its position in a list.
  const STREAM_SLOT = { imu_left: 1, imu_right: 2, audio_level: 3, camera: 4 };
  function color(name, idx = 0) {
    const slot = STREAM_SLOT[name] || ((idx % 4) + 5);
    return css(`--s${slot}`);
  }

  const fmt = (v, nd = 2) => (v === null || v === undefined || Number.isNaN(v)) ? '–'
    : (typeof v === 'number' ? v.toFixed(nd) : String(v));

  function badge(passed) {
    if (passed === null || passed === undefined) return '<span class="badge na">n/a</span>';
    return passed ? '<span class="badge pass">✓ pass</span>' : '<span class="badge fail">✗ fail</span>';
  }

  function throttledText(t) {
    if (!t) return '–';
    const v = parseInt(t, 16);
    if (!v) return 'none';
    const flags = [];
    if (v & 0x1) flags.push('under-voltage');
    if (v & 0x2) flags.push('freq capped');
    if (v & 0x4) flags.push('throttled');
    if (v & 0x8) flags.push('temp limit');
    if (!flags.length && v & 0x70000) flags.push('earlier event');
    return flags.join(', ') || t;
  }

  // Stream table. `agentStats` adds sender-side overruns and send-wait time.
  function streamTable(el, streams, sources = {}, agentStats = {}) {
    if (!streams || !streams.length) { el.innerHTML = '<p class="hint">No data streams in this run.</p>'; return; }
    const rows = streams.map((s, i) => {
      const a = agentStats[s.name] || {};
      const c = s.checks || {};
      const bad = (k) => c[k] === false ? ' class="bad"' : '';
      const integ = (s.crc_errors || 0) + (s.pattern_errors || 0);
      return `<tr>
        <td><span class="dot" style="background:${color(s.name, i)}"></span>${s.name}
            <div class="hint">${sources[s.name] || ''}</div></td>
        <td>${fmt(s.nominal_hz, 1)}</td><td>${fmt(s.sender_hz, 1)}</td>
        <td${bad('rate')}>${fmt(s.recv_hz, 1)}</td>
        <td${bad('loss')}>${fmt(s.loss_pct, 2)}</td><td>${s.lost ?? '–'}</td>
        <td>${s.duplicates ?? '–'}</td><td>${s.reordered ?? '–'}</td>
        <td${bad('integrity')}>${s.received ? integ : '–'}</td>
        <td>${fmt(s.latency_ms_p50)}</td><td${bad('latency')}>${fmt(s.latency_ms_p95)}</td>
        <td>${fmt(s.latency_ms_max)}</td>
        <td>${fmt(s.interarrival_ms_std)}</td><td>${s.stalls ?? '–'}</td>
        <td>${fmt(s.throughput_kBps, 1)}</td>
        <td>${a.overruns ?? '–'}</td><td>${fmt(a.send_wait_ms, 0)}</td>
        <td>${badge(s.passed)}</td></tr>`;
    }).join('');
    el.innerHTML = `<table class="data"><thead><tr>
      <th>stream</th><th>nominal Hz</th><th>sender Hz</th><th>received Hz</th>
      <th>loss %</th><th>lost</th><th>dup</th><th>reorder</th><th>integrity err</th>
      <th>latency p50 ms</th><th>p95 ms</th><th>max ms</th><th>jitter ms</th><th>stalls</th>
      <th>kB/s</th><th>sender overruns</th><th>send wait ms</th><th>status</th>
      </tr></thead><tbody>${rows}</tbody></table>`;
  }

  // Inter-arrival histograms as small multiples: each stream has its own period.
  function histograms(el, hist, nominal = {}) {
    const names = Object.keys(hist || {});
    if (!names.length) { el.innerHTML = '<p class="hint">No data yet.</p>'; return; }
    el.innerHTML = names.map((name, i) => {
      const h = hist[name];
      const max = Math.max(1, ...h.counts);
      const total = h.counts.reduce((a, b) => a + b, 0);
      const step = h.edges[1] - h.edges[0];
      const bars = h.counts.map((c, j) =>
        `<div style="height:${(100 * c / max).toFixed(1)}%;background:${color(name, i)}"
              title="${h.edges[j].toFixed(1)}–${(h.edges[j] + step).toFixed(1)} ms: ${c} frames"></div>`).join('');
      const period = nominal[name] ? (1000 / nominal[name]).toFixed(1) : null;
      return `<div class="hist"><h3><span class="dot" style="background:${color(name, i)}"></span>${name}
          <span class="hint">${total} gaps${period ? ` · nominal ${period} ms` : ''}</span></h3>
        <div class="bars">${bars}</div>
        <div class="axis"><span>0 ms</span><span>${h.edges[h.edges.length - 1].toFixed(0)} ms</span></div></div>`;
    }).join('');
  }

  // uPlot time-series chart with one y-axis. series: [{label, color, points?:bool}]
  function chart(el, yLabel, series, height = 200, scales = {}) {
    const axisStyle = { stroke: css('--muted'), grid: { stroke: css('--grid'), width: 1 },
                        ticks: { stroke: css('--axis'), width: 1 } };
    const opts = {
      width: Math.max(300, el.clientWidth), height,
      cursor: { drag: { x: true, y: false } },
      legend: { show: true },
      scales: { x: { time: false }, ...scales },
      axes: [{ ...axisStyle, values: (u, v) => v.map(x => x == null ? '' : `${x}s`) },
             { ...axisStyle, label: yLabel, size: 56 }],
      series: [{ label: 't (s)' }, ...series.map(s => ({
        label: s.label, stroke: s.color, width: s.points ? 0 : 2,
        paths: s.points ? () => null : undefined,
        points: s.points ? { show: true, size: 4, fill: s.color, stroke: s.color } : { show: false },
        value: (u, v) => v == null ? '–' : v.toFixed(2),
      }))],
    };
    const u = new uPlot(opts, [[], ...series.map(() => [])], el);
    new ResizeObserver(() => u.setSize({ width: Math.max(300, el.clientWidth), height })).observe(el);
    return u;
  }

  // Merge per-series [t, v] point lists into uPlot's aligned columns.
  function align(lists) {
    const ts = [...new Set(lists.flatMap(l => l.map(p => p[0])))].sort((a, b) => a - b);
    const idx = new Map(ts.map((t, i) => [t, i]));
    const cols = lists.map(l => { const c = new Array(ts.length).fill(null); l.forEach(p => { c[idx.get(p[0])] = p[1]; }); return c; });
    return [ts, ...cols];
  }

  return { css, color, fmt, badge, throttledText, streamTable, histograms, chart, align };
})();
