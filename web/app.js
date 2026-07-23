(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);

  const state = {
    jobId: null,
    status: 'idle',
    eventSource: null,
    reconnectTimer: null,
    parts: new Map(),
    logLines: [],
    completed: [],
    failed: [],
  };

  const els = {
    payload: $('payload'),
    outputDir: $('output-dir'),
    parallel: $('parallel'),
    startBtn: $('start-btn'),
    pauseBtn: $('pause-btn'),
    cancelBtn: $('cancel-btn'),
    resumeBtn: $('resume-btn'),
    statusBadge: $('status-badge'),
    message: $('message'),
    overallBar: $('overall-bar'),
    overallStats: $('overall-stats'),
    overallSpeed: $('overall-speed'),
    overallEta: $('overall-eta'),
    log: $('log'),
    parts: $('parts'),
  };

  function showMessage(text, type = 'info') {
    els.message.textContent = text;
    els.message.className = `message show ${type}`;
  }

  function clearMessage() {
    els.message.textContent = '';
    els.message.className = 'message';
  }

  function setBadge(status) {
    state.status = status;
    const badge = els.statusBadge;
    badge.className = `badge ${status}`;
    badge.textContent = status;
  }

  function formatBytes(n) {
    if (n == null || n <= 0) return '?';
    let f = Number(n);
    for (const unit of ['B', 'KB', 'MB', 'GB', 'TB']) {
      if (f < 1024 || unit === 'TB') {
        return unit === 'B' ? `${Math.round(f)} B` : `${f.toFixed(1)} ${unit}`;
      }
      f /= 1024;
    }
    return `${f.toFixed(1)} TB`;
  }

  function formatSpeed(bps) {
    if (!bps || bps <= 0) return '0 B/s';
    return `${formatBytes(bps)}/s`;
  }

  function formatDuration(seconds) {
    if (seconds == null || seconds < 0) return '';
    seconds = Math.round(seconds);
    if (seconds < 60) return `${seconds}s`;
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    if (m < 60) return `${m}m ${s}s`;
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m ${s}s`;
  }

  function addLog(text, level = 'info') {
    const entry = document.createElement('div');
    entry.className = `entry ${level}`;
    entry.textContent = text;
    els.log.appendChild(entry);
    els.log.scrollTop = els.log.scrollHeight;
    state.logLines.push({ text, level });
    if (state.logLines.length > 200) {
      els.log.removeChild(els.log.firstChild);
      state.logLines.shift();
    }
  }

  function clearLog() {
    els.log.innerHTML = '';
    state.logLines = [];
  }

  function renderParts() {
    if (state.parts.size === 0) {
      els.parts.innerHTML = '<p class="empty">Start a download to see live part progress here.</p>';
      return;
    }

    // Ensure rows exist and update in place to avoid full re-render churn.
    const rows = Array.from(state.parts.values()).sort((a, b) => a.num - b.num);
    const frag = document.createDocumentFragment();

    for (const part of rows) {
      let row = document.getElementById(`part-${part.num}`);
      if (!row) {
        row = document.createElement('div');
        row.id = `part-${part.num}`;
        row.className = 'part';
        row.innerHTML = `
          <div class="num">#${part.num.toString().padStart(3, '0')}</div>
          <div class="info">
            <div class="filename"></div>
            <div class="bar-bg"><div class="bar"></div></div>
          </div>
          <div class="stats"></div>
        `;
        frag.appendChild(row);
      }
      row.className = `part ${part.status}`;
      row.querySelector('.filename').textContent = part.filename || `part-${part.num}.zip`;
      const bar = row.querySelector('.bar');
      bar.style.width = `${part.pct || 0}%`;
      const stats = row.querySelector('.stats');
      const speed = part.speed ? formatSpeed(part.speed) : '';
      stats.textContent = `${part.pct || 0}%${speed ? '\n' + speed : ''}`;
      stats.title = `${formatBytes(part.done)} / ${formatBytes(part.total)}`;
    }

    if (frag.childNodes.length) {
      els.parts.appendChild(frag);
    }
  }

  function updateOverall(event) {
    const rows = Array.from(state.parts.values());
    const total = rows.reduce((acc, p) => acc + (p.total || 0), 0);
    const done = rows.reduce((acc, p) => acc + (p.done || 0), 0);
    const pct = total > 0 ? Math.min(100, Math.round((done * 100) / total)) : 0;
    els.overallBar.style.width = `${pct}%`;
    els.overallStats.textContent = `${rows.filter((p) => p.status === 'done').length} / ${rows.length} parts`;

    let speed = 0;
    for (const p of rows) {
      speed += Number(p.speed || 0);
    }
    els.overallSpeed.textContent = formatSpeed(speed);

    // Prefer server-provided ETA; fall back to local rough estimate.
    let etaText = '';
    if (event && event.eta_seconds != null && event.eta_seconds >= 0) {
      etaText = `${formatDuration(event.eta_seconds)} left`;
    } else if (speed > 0 && total > done) {
      etaText = `${formatDuration((total - done) / speed)} left`;
    }
    els.overallEta.textContent = etaText;
  }

  function applyProgress(parts, event) {
    for (const p of parts || []) {
      state.parts.set(p.num, p);
    }
    renderParts();
    updateOverall(event);
  }

  function closeEventSource() {
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
  }

  function scheduleReconnect() {
    if (state.status === 'running' && state.jobId && !state.reconnectTimer) {
      state.reconnectTimer = setTimeout(() => {
        state.reconnectTimer = null;
        if (state.status === 'running' && state.jobId) {
          connectStream(state.jobId);
        }
      }, 3000);
    }
  }

  function clearReconnect() {
    if (state.reconnectTimer) {
      clearTimeout(state.reconnectTimer);
      state.reconnectTimer = null;
    }
  }

  function updateActions(status) {
    const running = status === 'running' || status === 'discovering';
    els.startBtn.disabled = running;
    els.startBtn.textContent = running ? 'Running…' : 'Start Download';
    els.pauseBtn.disabled = !running;
    els.cancelBtn.disabled = !(running || status === 'paused');
    els.resumeBtn.disabled = !(status === 'paused' || status === 'failed' || status === 'cancelled');
  }

  function handleEvent(event) {
    switch (event.type) {
      case 'status':
        setBadge(event.status || 'running');
        if (event.message) {
          addLog(event.message);
          showMessage(event.message, event.status === 'failed' ? 'error' : 'info');
        }
        if (event.parts) {
          for (const p of event.parts) {
            state.parts.set(p.num, { ...p, status: 'queued' });
          }
          renderParts();
          updateOverall();
        }
        updateActions(event.status || 'running');
        break;
      case 'discovered':
        addLog(`Discovered #${String(event.num).padStart(3, '0')} ${event.filename} (${event.size_human})`);
        break;
      case 'progress':
        applyProgress(event.parts || [], event);
        break;
      case 'log':
        addLog(event.message, event.level || 'info');
        break;
      case 'auth_required':
        setBadge('paused');
        showMessage(event.message, 'warning');
        updateActions('paused');
        state.completed = event.completed || [];
        state.failed = event.failed || [];
        break;
      default:
        break;
    }
  }

  async function startDownload() {
    if (state.eventSource) {
      closeEventSource();
    }

    clearMessage();
    clearReconnect();
    state.parts.clear();
    state.completed = [];
    state.failed = [];
    clearLog();
    renderParts();
    updateOverall();

    const payload = els.payload.value.trim();
    if (!payload) {
      showMessage('Paste a JSON payload first.', 'error');
      return;
    }

    els.startBtn.disabled = true;
    setBadge('running');
    updateActions('running');

    try {
      const resp = await fetch('/api/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          payload,
          output_dir: els.outputDir.value || undefined,
          parallel: Math.max(1, Math.min(2, parseInt(els.parallel.value, 10) || 1)),
        }),
      });

      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        throw new Error(data.detail || `Start failed: ${resp.status}`);
      }

      state.jobId = data.job_id;
      addLog(`Job ${state.jobId} started.`);
      connectStream(state.jobId);
    } catch (err) {
      setBadge('failed');
      showMessage(err.message, 'error');
      updateActions('failed');
    }
  }

  function connectStream(jobId) {
    closeEventSource();
    const es = new EventSource(`/api/stream/${jobId}`);
    state.eventSource = es;

    es.addEventListener('message', (ev) => {
      try {
        const data = JSON.parse(ev.data);
        handleEvent(data);
      } catch (err) {
        addLog(`Parse error: ${err.message}`, 'error');
      }
    });

    es.addEventListener('error', () => {
      if (state.status !== 'paused') {
        setBadge('failed');
        showMessage('Progress stream disconnected. Reconnecting…', 'error');
      }
      closeEventSource();
      scheduleReconnect();
    });
  }

  async function callJobAction(endpoint, method = 'POST') {
    if (!state.jobId) {
      showMessage('No active job.', 'error');
      return;
    }
    try {
      const resp = await fetch(`/api/${endpoint}/${state.jobId}`, { method });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        throw new Error(data.detail || `${endpoint} failed: ${resp.status}`);
      }
      return data;
    } catch (err) {
      showMessage(err.message, 'error');
      throw err;
    }
  }

  async function pauseDownload() {
    const data = await callJobAction('pause');
    if (data) {
      addLog(`Paused job ${data.job_id}`);
      showMessage('Paused. Click Resume to continue.', 'info');
      setBadge('paused');
      updateActions('paused');
    }
  }

  async function cancelDownload() {
    const data = await callJobAction('cancel');
    if (data) {
      addLog(`Cancelled job ${data.job_id}`);
      showMessage('Cancelled.', 'info');
      setBadge('cancelled');
      updateActions('cancelled');
      closeEventSource();
      clearReconnect();
    }
  }

  async function resumeDownload() {
    if (!state.jobId) {
      showMessage('No paused job to resume.', 'error');
      return;
    }

    const payload = els.payload.value.trim();
    const body = payload ? { payload } : {};

    els.resumeBtn.disabled = true;
    showMessage('Resuming…', 'info');

    try {
      const resp = await fetch(`/api/resume/${state.jobId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        throw new Error(data.detail || `Resume failed: ${resp.status}`);
      }
      setBadge('running');
      showMessage('Resumed.', 'info');
      updateActions('running');
      connectStream(state.jobId);
    } catch (err) {
      setBadge('paused');
      showMessage(err.message, 'error');
      updateActions('paused');
    }
  }

  function onEnter(handler) {
    return (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        handler();
      }
    };
  }

  // Reconnect to an existing running/paused job on page refresh.
  async function tryReconnectToExisting() {
    try {
      const resp = await fetch('/api/jobs');
      const data = await resp.json();
      if (!data.jobs || data.jobs.length === 0) return;
      // Prefer the most recent job that isn't completed/cancelled.
      const job = data.jobs.reverse().find((j) => ['running', 'paused', 'discovering', 'pending', 'failed'].includes(j.status));
      if (job) {
        state.jobId = job.job_id;
        setBadge(job.status);
        updateActions(job.status);
        addLog(`Reconnected to existing job ${job.job_id} (${job.status})`);
        connectStream(job.job_id);
      }
    } catch (err) {
      // Ignore; user can start a new job manually.
    }
  }

  els.startBtn.addEventListener('click', startDownload);
  els.pauseBtn.addEventListener('click', pauseDownload);
  els.cancelBtn.addEventListener('click', cancelDownload);
  els.resumeBtn.addEventListener('click', resumeDownload);
  els.payload.addEventListener('keydown', onEnter(startDownload));

  // Expose state for debugging.
  window.takeoutState = state;

  // On first load, see if there is an existing job to reconnect to.
  tryReconnectToExisting();
})();
