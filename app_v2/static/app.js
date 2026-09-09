'use strict';

const ACCEL_SCALE = 16384.0;
const GYRO_SCALE  = 131.0;
const WINDOW_S    = 10;
const MAX_PTS     = 600; // 60Hz × 10s

// ── Live SPL meter (causal, presentation-only) ────────────────────────────────
const SPL_TAU        = 0.125;  // EMA time constant (s) — sound-meter "Fast"
const SPL_PEAK_DECAY = 12;     // peak-hold fall rate (dB/s)
const SPL_MIN        = 40;     // meter/chart floor (dB SPL)
const SPL_MAX        = 100;    // meter/chart ceiling (dB SPL)

let splEmaPower = null;  // EMA of linear power (energy domain, like a real meter)
let splSmoothDb = null;  // current smoothed level (dB SPL)
let splPeakDb   = null;  // peak-hold marker (dB SPL)
let splLastTs   = null;

// ── Rolling buffer ────────────────────────────────────────────────────────────

function makeBuffer(keys) {
  const b = { ts: [] };
  keys.forEach(k => b[k] = []);
  return b;
}

function bufPush(b, ts, vals) {
  b.ts.push(ts);
  const keys = Object.keys(b).filter(k => k !== 'ts');
  keys.forEach((k, i) => b[k].push(vals[i]));
  if (b.ts.length > MAX_PTS) {
    b.ts.shift();
    keys.forEach(k => b[k].shift());
  }
}

const bufs = {
  nhm:   makeBuffer(['nhm']),
  spl:   makeBuffer(['raw', 'smooth']),
  accel: makeBuffer(['x', 'y', 'z']),
  gyro:  makeBuffer(['x', 'y', 'z']),
};

let dirty = { nhm: false, spl: false, accel: false, gyro: false };

// ── uPlot setup ───────────────────────────────────────────────────────────────

const DARK = '#0f1117';
const GRID = { stroke: '#2d3148', width: 1 };
const TICK = { stroke: '#4b5563' };
const FONT = '11px system-ui, sans-serif';

function chartOpts(target, series, yLabel, height = 140, yRange = null) {
  const width = document.getElementById(target).parentElement.clientWidth - 24;
  return {
    width,
    height,
    cursor: { show: false },
    legend: { show: true },
    scales: yRange ? { y: { range: yRange } } : {},
    axes: [
      {
        stroke: '#6b7280', font: FONT,
        grid: GRID, ticks: TICK,
        values: (u, vals) => vals.map(v => v == null ? '' : `${v.toFixed(1)}s`),
      },
      {
        stroke: '#6b7280', font: FONT, label: yLabel,
        labelFont: FONT, labelGap: 6,
        grid: GRID, ticks: TICK,
      },
    ],
    series: [{ label: 'time' }, ...series],
  };
}

const CHART_SERIES = {
  nhm:   [{ label: 'MAR',    stroke: '#a855f7', width: 1.5 }],
  spl:   [
    { label: 'raw',    stroke: 'rgba(34,197,94,0.28)', width: 1 },
    { label: 'dB SPL', stroke: '#22c55e', width: 2 },
  ],
  accel: [
    { label: 'X', stroke: '#3b82f6', width: 1.5 },
    { label: 'Y', stroke: '#22c55e', width: 1.5 },
    { label: 'Z', stroke: '#f97316', width: 1.5 },
  ],
  gyro: [
    { label: 'X', stroke: '#3b82f6', width: 1.5 },
    { label: 'Y', stroke: '#22c55e', width: 1.5 },
    { label: 'Z', stroke: '#f97316', width: 1.5 },
  ],
};

// Relative timestamps for chart (since session start)
let t0 = null;

function relTs(buf) {
  if (!t0 && buf.ts.length) t0 = buf.ts[0];
  return buf.ts.map(t => t - (t0 || 0));
}

function makePlot(id, seriesKey, yLabel, yRange = null) {
  const el = document.getElementById(id);
  const b = bufs[seriesKey];
  const keys = Object.keys(b).filter(k => k !== 'ts');
  const emptyData = [[], ...keys.map(() => [])];
  const opts = chartOpts(id, CHART_SERIES[seriesKey], yLabel, 140, yRange);
  const u = new uPlot(opts, emptyData, el);
  return u;
}

const plots = {
  nhm:   makePlot('chart-nhm',   'nhm',   'MAR'),
  spl:   makePlot('chart-spl',   'spl',   'dB SPL', [SPL_MIN, SPL_MAX]),
  accel: makePlot('chart-accel', 'accel', 'g'),
  gyro:  makePlot('chart-gyro',  'gyro',  '°/s'),
};

// ── SPL meter DOM ─────────────────────────────────────────────────────────────
const splFill   = document.getElementById('spl-fill');
const splPeakEl = document.getElementById('spl-peak');
const splDbEl   = document.getElementById('spl-db');

function splPct(db) {
  return Math.max(0, Math.min(1, (db - SPL_MIN) / (SPL_MAX - SPL_MIN)));
}

function updateSplMeter() {
  if (splSmoothDb == null || !splFill) return;
  splFill.style.width = (splPct(splSmoothDb) * 100) + '%';
  splFill.style.background =
    splSmoothDb < 52 ? '#3b82f6' :   // quiet / floor
    splSmoothDb < 85 ? '#22c55e' :   // good speech level
    splSmoothDb < 93 ? '#eab308' : '#ef4444';  // loud / near clip
  if (splPeakDb != null) splPeakEl.style.left = (splPct(splPeakDb) * 100) + '%';
  splDbEl.textContent = Math.round(splSmoothDb);
  setVal('val-spl', splSmoothDb, '', 0);
}

// ── Render loop ───────────────────────────────────────────────────────────────

function render() {
  for (const key of ['nhm', 'spl', 'accel', 'gyro']) {
    if (!dirty[key]) continue;
    dirty[key] = false;
    const b = bufs[key];
    const ts = relTs(b);
    const vals = Object.keys(b).filter(k => k !== 'ts').map(k => b[k]);
    plots[key].setData([ts, ...vals]);
  }
  updateSplMeter();
  requestAnimationFrame(render);
}
requestAnimationFrame(render);

// ── Live stat updates ─────────────────────────────────────────────────────────

function setVal(id, val, unit = '', decimals = 3) {
  const el = document.getElementById(id);
  if (el) el.textContent = val == null ? '—' : val.toFixed(decimals) + unit;
}

// ── Camera ────────────────────────────────────────────────────────────────────

let prevCamUrl = null;
const camImg   = document.getElementById('camera');
const noCamera = document.getElementById('no-camera');

function showFrame(jpegBytes) {
  const blob = new Blob([jpegBytes], { type: 'image/jpeg' });
  const url  = URL.createObjectURL(blob);
  camImg.onload = () => { if (prevCamUrl) URL.revokeObjectURL(prevCamUrl); prevCamUrl = url; };
  camImg.src = url;
  camImg.style.display = 'block';
  noCamera.style.display = 'none';
}

// ── WebSocket ─────────────────────────────────────────────────────────────────

const dot   = document.getElementById('status-dot');
const label = document.getElementById('status-label');

function setStatus(connected, mock) {
  dot.className = connected ? (mock ? 'mock' : 'connected') : '';
  label.textContent = connected ? (mock ? 'Mock data' : 'Pi connected') : 'Waiting for Pi…';
}

let isMock = false;

function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.binaryType = 'arraybuffer';

  ws.onopen = () => setStatus(false, false);

  ws.onmessage = (ev) => {
    if (ev.data instanceof ArrayBuffer) {
      const bytes = new Uint8Array(ev.data);
      const header = String.fromCharCode(bytes[0], bytes[1], bytes[2], bytes[3]);
      if (header === 'CAM:') {
        showFrame(bytes.slice(4));
      } else if (header === 'ZIP:') {
        const name = new TextDecoder().decode(bytes.slice(4, 68)).replace(/\0+$/, '').trim();
        const blob = new Blob([bytes.slice(68)], { type: 'application/zip' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = name || 'recording.zip';
        a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 5000);
      }
      return;
    }

    const msg = JSON.parse(ev.data);

    if (msg.t === 'status') {
      isMock = !!msg.mock;
      setStatus(msg.connected, isMock);
    }

    else if (msg.t === 'imu') {
      const ax = msg.ax / ACCEL_SCALE, ay = msg.ay / ACCEL_SCALE, az = msg.az / ACCEL_SCALE;
      const gx = msg.gx / GYRO_SCALE,  gy = msg.gy / GYRO_SCALE,  gz = msg.gz / GYRO_SCALE;
      bufPush(bufs.accel, msg.ts, [ax, ay, az]);
      bufPush(bufs.gyro,  msg.ts, [gx, gy, gz]);
      dirty.accel = dirty.gyro = true;
      setVal('val-az', az, 'g', 3);
      setVal('val-gx', gx, '°/s', 1);
    }

    else if (msg.t === 'audio') {
      // msg.spl is per-frame dB SPL (already high-passed on the Pi). Smooth it
      // causally in the POWER domain (energy) so the meter reads like a real SPL
      // meter, and track a slow-decaying peak-hold.
      const now   = msg.ts;
      const rawDb = msg.spl;  // null on true silence
      const dt = (splLastTs != null && now > splLastTs && now - splLastTs < 1)
                 ? now - splLastTs : 0.02;

      if (rawDb != null) {
        const power = Math.pow(10, (rawDb - 120) / 10);
        if (splEmaPower == null || dt <= 0 || dt >= 1) {
          splEmaPower = power;                       // (re)seed after a gap
        } else {
          const a = 1 - Math.exp(-dt / SPL_TAU);
          splEmaPower += a * (power - splEmaPower);
        }
        splSmoothDb = 10 * Math.log10(splEmaPower) + 120;
      }

      if (splSmoothDb != null) {
        splPeakDb = (splPeakDb == null || splSmoothDb > splPeakDb)
          ? splSmoothDb
          : Math.max(splSmoothDb, splPeakDb - SPL_PEAK_DECAY * dt);
      }

      splLastTs = now;
      bufPush(bufs.spl, now, [rawDb, splSmoothDb]);
      dirty.spl = true;
    }

    else if (msg.t === 'mar') {
      if (msg.vertical != null && msg.horizontal != null && msg.horizontal > 1e-6) {
        const mar = msg.vertical / msg.horizontal;
        bufPush(bufs.nhm, msg.ts, [mar]);
        dirty.nhm = true;
        setVal('val-mar', mar, '', 3);
      }
    }

    else if (msg.t === 'rec_state') {
      updateRecUI(msg.recording);
    }
  };

  ws.onclose = () => {
    setStatus(false, false);
    setTimeout(connect, 2000);
  };

  ws.onerror = () => ws.close();

  return ws;
}

let ws = connect();

// ── Recording UI ──────────────────────────────────────────────────────────────

let recStart = null;
let recTimer = null;
const btnRec   = document.getElementById('btn-rec');
const recTimerEl = document.getElementById('rec-timer');
const recTimeEl  = document.getElementById('rec-time');

function updateRecUI(recording) {
  if (recording) {
    btnRec.textContent = 'Stop Recording';
    btnRec.classList.add('recording');
    recStart = Date.now();
    recTimerEl.classList.add('active');
    recTimer = setInterval(() => {
      const s = Math.floor((Date.now() - recStart) / 1000);
      recTimeEl.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
    }, 500);
  } else {
    btnRec.textContent = 'Start Recording';
    btnRec.classList.remove('recording');
    recTimerEl.classList.remove('active');
    clearInterval(recTimer);
    recStart = null;
  }
}

btnRec.addEventListener('click', () => {
  const recording = btnRec.classList.contains('recording');
  ws.send(JSON.stringify({ cmd: recording ? 'stop_rec' : 'start_rec' }));
});

// ── Resize charts on window resize ───────────────────────────────────────────
window.addEventListener('resize', () => {
  for (const [key, u] of Object.entries(plots)) {
    const el = u.root.parentElement;
    u.setSize({ width: el.clientWidth - 24, height: u.height });
  }
});
