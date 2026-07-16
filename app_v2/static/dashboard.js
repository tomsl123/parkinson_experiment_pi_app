'use strict';

// ── Shared fetch helper ───────────────────────────────────────────────────────

async function apiFetch(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${text}`);
  }
  const ct = res.headers.get('content-type') || '';
  return ct.includes('application/json') ? res.json() : res;
}

// ── Toast ─────────────────────────────────────────────────────────────────────

function toast(msg, type = 'info') {
  const el = document.createElement('div');
  el.textContent = msg;
  el.style.cssText = `
    position:fixed; bottom:24px; right:24px; z-index:9999;
    padding:10px 18px; border-radius:8px; font-size:13px; font-weight:500;
    background:${type === 'error' ? '#ef4444' : '#22c55e'}; color:#fff;
    box-shadow:0 4px 12px rgba(0,0,0,.4); transition:opacity .3s;
  `;
  document.body.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }, 3000);
}

// ── Formatting ────────────────────────────────────────────────────────────────

function fmtDate(ts) {
  return ts ? new Date(ts * 1000).toLocaleString() : '—';
}

function fmtDuration(started, stopped) {
  if (!stopped) return '—';
  const s = Math.round(stopped - started);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function fmtSensors(sensors) {
  if (!sensors) return '—';
  const map = { imu: 'IMU', audio: 'SPL', nhm: 'MAR' };
  return sensors.split(',').map(s => map[s] || s).join(' · ');
}

// ── Recording state machine (used on experiment detail page) ─────────────────

let _recState  = 'IDLE';   // 'IDLE' | 'RECORDING'
let _recExId   = null;
let _recStart  = null;
let _recTimer  = null;
let _piConn    = false;

function initRecording({ expId, btnEl, timerEl, timerTextEl, onStop }) {
  // Listen to live dashboard WebSocket for Pi status + rec_state updates
  const wsUrl = `ws://${location.host}/ws`;
  let ws = null;

  function connectWs() {
    ws = new WebSocket(wsUrl);
    ws.onmessage = (ev) => {
      if (typeof ev.data !== 'string') return;
      const msg = JSON.parse(ev.data);
      if (msg.t === 'status') {
        _piConn = msg.connected;
        updateBtn();
      }
      if (msg.t === 'rec_state') {
        if (!msg.recording && _recState === 'RECORDING') {
          // stopped externally
          _stopTimer();
          _recState = 'IDLE';
          updateBtn();
          if (onStop) onStop();
        }
      }
    };
    ws.onclose = () => setTimeout(connectWs, 2000);
  }
  connectWs();

  function updateBtn() {
    if (_recState === 'RECORDING') {
      btnEl.textContent = 'Stop Recording';
      btnEl.classList.add('recording');
      btnEl.disabled = false;
    } else {
      btnEl.textContent = 'Start Recording';
      btnEl.classList.remove('recording');
      btnEl.disabled = false; // allow even without Pi (will 503 gracefully)
    }
  }

  function _startTimer() {
    _recStart = Date.now();
    timerEl.style.display = 'inline';
    _recTimer = setInterval(() => {
      const s = Math.floor((Date.now() - _recStart) / 1000);
      timerTextEl.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
    }, 500);
  }

  function _stopTimer() {
    clearInterval(_recTimer);
    timerEl.style.display = 'none';
  }

  btnEl.addEventListener('click', async () => {
    if (_recState === 'IDLE') {
      try {
        btnEl.disabled = true;
        // Spec flow: create exercise first, then start recording
        const ex = await apiFetch(`/experiments/${expId}/exercises`, { method: 'POST', body: { properties: {} } });
        _recExId = ex.id;
        await apiFetch(`/exercises/${ex.id}/recording/start`, { method: 'POST' });
        _recState = 'RECORDING';
        _startTimer();
        updateBtn();
      } catch (e) {
        toast(`Start failed: ${e.message}`, 'error');
        btnEl.disabled = false;
      }
    } else {
      try {
        btnEl.disabled = true;
        await apiFetch(`/exercises/${_recExId}/recording/stop`, { method: 'POST' });
        _stopTimer();
        _recState = 'IDLE';
        updateBtn();
        if (onStop) onStop();
      } catch (e) {
        toast(`Stop failed: ${e.message}`, 'error');
        btnEl.disabled = false;
      }
    }
  });

  updateBtn();
}
