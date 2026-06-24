// Takeout Manager UI. Vanilla JS, no build step.
// Polls /api/jobs for the list; opens an SSE stream for the selected job.

const $ = (sel, root = document) => root.querySelector(sel);
const fmtBytes = (n) => {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
};
const fmtSpeed = (bps) => bps ? `${fmtBytes(bps)}/s` : "—";
const pct = (done, total) => total > 0 ? Math.min(100, (done / total) * 100) : 0;

let selectedJob = null;
let evtSource = null;

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.headers.get("content-type")?.includes("json") ? r.json() : r.text();
}

async function refreshHealth() {
  const el = $("#health");
  try {
    const h = await api("/api/control/health");
    el.textContent = `● ${h.jobs_active} active / ${h.jobs_total} jobs`;
    el.className = "health ok";
  } catch {
    el.textContent = "● manager unreachable";
    el.className = "health bad";
  }
}

async function refreshJobs() {
  let data;
  try { data = await api("/api/jobs"); } catch { return; }
  const list = $("#jobs-list");
  const jobs = data.jobs || [];
  if (!jobs.length) { list.innerHTML = '<p class="dim">No jobs yet. Capture a Takeout download to begin.</p>'; return; }
  list.innerHTML = "";
  for (const j of jobs) {
    const t = j.totals || {};
    const card = document.createElement("div");
    card.className = "job-card" + (j.job_id === selectedJob ? " active" : "");
    card.onclick = () => selectJob(j.job_id);
    card.innerHTML = `
      <div class="jc-top">
        <span class="jc-name">${j.workflow}</span>
        <span class="badge ${j.status}">${j.status.replace("_", " ")}</span>
      </div>
      <div class="jc-sub">${t.parts_done || 0}/${t.parts_total || 0} parts · ${fmtBytes(t.bytes_done)} / ${fmtBytes(t.bytes_total)}</div>`;
    list.appendChild(card);
  }
  // Auto-select the first job if none chosen.
  if (!selectedJob && jobs.length) selectJob(jobs[0].job_id);
}

function selectJob(jobId) {
  selectedJob = jobId;
  if (evtSource) { evtSource.close(); evtSource = null; }
  document.querySelectorAll(".job-card").forEach(c => c.classList.remove("active"));
  evtSource = new EventSource(`/api/jobs/${jobId}/events`);
  const handler = (e) => { try { renderDetail(JSON.parse(e.data)); } catch {} };
  for (const ev of ["snapshot", "started", "progress", "needs_cookie", "complete", "error", "resumed"]) {
    evtSource.addEventListener(ev, handler);
  }
  refreshJobs();
}

function renderDetail(job) {
  const panel = $("#detail-panel");
  if (!job || !job.job_id) { panel.innerHTML = '<p class="dim">Select a job.</p>'; return; }
  const t = job.totals || {};
  const parts = job.parts || [];
  const needsCookie = job.status === "needs_cookie";
  panel.innerHTML = `
    <div class="detail-head">
      <h2>${job.workflow}</h2>
      <span class="badge ${job.status}">${job.status.replace("_", " ")}</span>
      <span class="spacer"></span>
      <button class="btn" data-act="pause" ${job.status !== "downloading" ? "disabled" : ""}>Pause</button>
      <button class="btn" data-act="recapture">Re-capture cookie</button>
      <button class="btn danger" data-act="cancel" ${job.status === "downloading" || job.status === "queued" ? "" : "disabled"}>Cancel</button>
      <button class="btn danger" data-act="delete" ${job.status === "downloading" || job.status === "queued" ? "disabled" : ""}>Remove</button>
    </div>
    ${needsCookie ? '<div class="banner warn">Cookie expired. Re-capture in the browser (the extension auto-re-captures if enabled). The job resumes from partials automatically.</div>' : ""}
    ${job.last_error ? `<div class="banner warn">${job.last_error}</div>` : ""}
    <div class="totals">
      <span><b>${t.parts_done || 0}</b>/${t.parts_total || 0} parts</span>
      <span><b>${fmtBytes(t.bytes_done)}</b> / ${fmtBytes(t.bytes_total)}</span>
      <span>${fmtSpeed(t.speed_bps)}</span>
      <span class="dim">${job.output_dir || ""}</span>
    </div>
    <div class="progress-big"><div class="bar" style="width:${pct(t.bytes_done, t.bytes_total)}%"></div></div>
    <table class="parts">
      <thead><tr><th>#</th><th>File</th><th>Size</th><th>Progress</th><th>Status</th></tr></thead>
      <tbody>${parts.map(rowFor).join("")}</tbody>
    </table>
    <details class="logbox"><summary>Engine log</summary><div class="log" id="logpane">loading…</div></details>`;

  panel.querySelectorAll("button[data-act]").forEach(b => {
    b.onclick = () => doControl(b.dataset.act, job.job_id);
  });
  const log = $("#logpane");
  if (log) api(`/api/jobs/${job.job_id}/log?lines=120`).then(t => log.textContent = t).catch(() => {});
}

function rowFor(p) {
  const name = (p.filename || "").split("/").pop() || `part ${p.index}`;
  return `<tr>
    <td>${p.index}</td>
    <td title="${name}">${name.length > 42 ? "…" + name.slice(-39) : name}</td>
    <td>${fmtBytes(p.size)}</td>
    <td><div class="minibar"><div style="width:${pct(p.done, p.size)}%"></div></div></td>
    <td><span class="st-${p.status}">${p.status}</span></td>
  </tr>`;
}

function apiToken() {
  const m = document.querySelector('meta[name="api-token"]');
  return m ? m.getAttribute("content") : "";
}

async function doControl(act, jobId) {
  const headers = { "Content-Type": "application/json" };
  const tok = apiToken();
  if (tok) headers["X-Api-Token"] = tok;
  if (act === "delete") {
    if (!confirm("Remove this job from the list? Downloaded files are kept on disk; only the job record is removed.")) return;
    try {
      await api(`/api/jobs/${jobId}`, { method: "DELETE", headers });
      if (selectedJob === jobId) {
        selectedJob = null;
        if (evtSource) { evtSource.close(); evtSource = null; }
        $("#detail-panel").innerHTML = '<p class="dim">Select a job.</p>';
      }
      refreshJobs();
    } catch (e) {
      alert(`remove failed: ${e.message}`);
    }
    return;
  }
  const map = {
    pause: ["/api/control/pause", "POST"],
    cancel: ["/api/control/cancel", "POST"],
    recapture: ["/api/control/recapture", "POST"],
  };
  const [path, method] = map[act] || [];
  if (!path) return;
  if (act === "cancel" && !confirm("Cancel this job? Partials are kept on disk.")) return;
  try {
    await api(path, {
      method,
      headers,
      body: JSON.stringify({ job_id: jobId }),
    });
    refreshJobs();
  } catch (e) {
    alert(`${act} failed: ${e.message}`);
  }
}

refreshHealth();
refreshJobs();
setInterval(refreshHealth, 5000);
setInterval(refreshJobs, 4000);


// --- Paste-payload box ------------------------------------------------------
function captureToken() {
  const m = document.querySelector('meta[name="capture-token"]');
  return m ? m.getAttribute("content") : "";
}

async function submitPaste() {
  const btn = $("#paste-submit");
  const statusEl = $("#paste-status");
  const raw = $("#paste-json").value.trim();
  const label = $("#paste-label").value.trim();
  if (!raw) { statusEl.textContent = "Nothing to submit."; return; }
  let path = "/api/payload";
  if (label) path += "?label=" + encodeURIComponent(label);
  const headers = { "Content-Type": "application/json" };
  const tok = captureToken();
  if (tok) headers["X-Capture-Token"] = tok;
  btn.disabled = true;
  statusEl.textContent = "Submitting\u2026";
  try {
    const r = await fetch(path, { method: "POST", headers, body: raw });
    const txt = await r.text();
    if (!r.ok) throw new Error(r.status + " " + txt);
    let job;
    try { job = JSON.parse(txt); } catch { job = null; }
    statusEl.textContent = "Started" + (job && job.job_id ? " job " + job.job_id : "") + ".";
    $("#paste-json").value = "";
    $("#paste-label").value = "";
    refreshJobs();
    if (job && job.job_id) selectJob(job.job_id);
  } catch (e) {
    statusEl.textContent = "Failed: " + e.message;
  } finally {
    btn.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const b = document.getElementById("paste-submit");
  if (b) b.addEventListener("click", submitPaste);
});
