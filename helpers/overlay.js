// Takeout Downloader Helper - On-page overlay (docs/v2/08-SELF-DRIVING-UX.md §3)
//
// The third rendering surface for the v2 control plane. Same events, same
// classifyGuard() rules, same routes as manager/web/monitor.html and
// helpers/popup.js — three implementations, ONE behavior (parity-tested).
//
// Hard rules this file obeys (08 §3.2):
//   * Shadow DOM: Google's CSS cannot reach in, ours cannot leak out.
//   * Opens NO new backend routes: only the read routes monitor.html uses plus
//     the documented control routes in 08 §2.
//   * NEVER throws into Google's page context. Every entry point is wrapped.
//   * Never covers Google's own download buttons (bottom-right, narrow, and
//     collapsible; pointer-events confined to the card).
//
// Public API (window.__tkOverlay):
//   init()    - idempotent; builds the host, resolves config, starts streams.
//   destroy() - tears down DOM, SSE, timers, listeners. Safe to call twice.
//   update()  - force an immediate refresh (re-pick job + reseed snapshot).

(function () {
    'use strict';

    if (window.__tkOverlay) return;            // already injected

    /* ---------- constants (mirrors monitor.html) -------------------------- */
    const STALL_S = 90;              // TK2_STALL_S — no progress => stalled
    const EWMA_ALPHA = 0.3;          // per-part speed EWMA
    const POLL_INTERVAL = 2000;      // fallback snapshot poll (06 §6)
    const JOBLIST_INTERVAL = 15000;  // re-pick the job for this page
    const OFFLINE_RETRY_MS = 5000;   // manager unreachable retry (08 §3.2)
    const HISTORY_MAX = 10;          // captureHistory entries rendered
    const Z_INDEX = 2147483600;      // below Chrome UI, above Google's

    /* ---------- formatting helpers (mirrors monitor.html) ----------------- */
    const fmtBytes = (n) => {
        if (!n) return '0 B';
        const u = ['B', 'KB', 'MB', 'GB', 'TB'];
        let i = 0, v = n;
        while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
        return v.toFixed(v < 10 && i > 0 ? 1 : 0) + ' ' + u[i];
    };
    const fmtSpeed = (bps) => (bps ? fmtBytes(bps) + '/s' : '\u2014');
    const pct = (done, total) => (total > 0 ? Math.min(100, (done / total) * 100) : 0);
    const fmtDur = (sec) => {
        if (!isFinite(sec) || sec <= 0) return '\u2014';
        if (sec < 60) return '~' + Math.round(sec) + 's';
        const m = Math.floor(sec / 60);
        if (m < 60) return '~' + m + ' min';
        const h = Math.floor(m / 60), mm = m % 60;
        if (h >= 48) return '~' + (h / 24).toFixed(1) + ' d';
        return '~' + h + 'h ' + mm + 'm';
    };
    const esc = (s) => String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    const parseTs = (ts) => {
        if (ts == null) return Date.now();
        let ms = Date.parse(ts);
        if (!isFinite(ms)) {
            const f = parseFloat(ts);          // heartbeat ts is epoch seconds
            ms = isFinite(f) ? f * 1000 : NaN;
        }
        return isFinite(ms) ? ms : Date.now();
    };

    /* ---------- reliability guards ---------------------------------------
       VERBATIM copy of manager/web/monitor.html's classifier. The engine has
       four guards that deliberately stop or slow a transfer; unreported, each
       is indistinguishable from a hang. There is no dedicated event kind for
       them, so all three surfaces pattern-match the text the engine writes
       into part.last_error / job.last_error. Change all three or none — a
       parity test enforces identical behavior. */
    const GUARD_SEVERITY = { storage: 4, rate: 3, cache: 2, stall: 1 };
    const GUARD_NOTICE_TTL_MS = 120000;   // an event-only sighting ages out

    function guardWaitSecs(t) {
        // "waiting 42s", "waiting 42.5s", "retry-after 60" -> 42 / 60
        const m = /(?:wait|waiting|retry[- ]after|backoff)\D{0,12}(\d+(?:\.\d+)?)\s*s?/i.exec(t);
        return m ? Math.round(parseFloat(m[1])) : null;
    }
    function guardCachePct(t) {
        const m = /cache\D{0,24}?(\d+(?:\.\d+)?)\s*%/i.exec(t) ||
                  /(\d+(?:\.\d+)?)\s*%\s*full/i.exec(t);
        return m ? Math.round(parseFloat(m[1])) : null;
    }

    /* Pure: (errorText, partStatus) -> {kind, severity, message, why} | null. */
    function classifyGuard(errText, status) {
        const raw = errText == null ? "" : String(errText);
        const t = raw.toLowerCase();
        const st = String(status == null ? "" : status).toUpperCase();

        // 1. STORAGE — checked FIRST and short-circuits, mirroring preflight_write:
        //    on a detached mount a free-space probe measures the ROOT disk and would
        //    wrongly pass, green-lighting exactly the write we are preventing.
        if (t.indexOf("storage preflight:") >= 0 ||
            t.indexOf("is not a mount point") >= 0 || t.indexOf("volume is detached") >= 0 ||
            t.indexOf("insufficient space") >= 0 || t.indexOf("headroom") >= 0 ||
            t.indexOf("sentinel") >= 0 || t.indexOf("disk_error") >= 0 ||
            t.indexOf("disk full") >= 0) {
            return {
                kind: "storage", severity: GUARD_SEVERITY.storage,
                message: "\u26D4 storage blocked \u2014 refusing to write: " +
                         raw.replace(/^storage preflight:\s*/i, ""),
                why: "The archive volume is detached or out of headroom. Every part will " +
                     "fail the same way, and an unguarded write would land 10 GB parts on " +
                     "the root disk. Fix the mount before resuming."
            };
        }

        // 2. RATE LIMITED — a 429 backoff sleep.
        if (t.indexOf("rate_limited") >= 0 || t.indexOf("rate limit") >= 0 ||
            t.indexOf("rate-limit") >= 0 || t.indexOf("429") >= 0 ||
            t.indexOf("throttl") >= 0 || t.indexOf("too many requests") >= 0) {
            const w = guardWaitSecs(raw);
            return {
                kind: "rate", severity: GUARD_SEVERITY.rate,
                message: "\u23F3 rate limited by Google \u2014 waiting " +
                         (w != null ? w + "s" : "out the backoff") +
                         " (attempts are limited to 5 per part)",
                why: "Re-requesting now would spend one of the 5 irreplaceable attempts " +
                     "on a request Google has already refused, so the engine sleeps instead."
            };
        }

        // 3. CACHE PAUSED — rclone upload backlog; a deliberate sleep, not a hang.
        if (t.indexOf("upload cache") >= 0 || t.indexOf("vfs cache") >= 0 ||
            t.indexOf("cache full") >= 0 || t.indexOf("rclone") >= 0 ||
            (t.indexOf("cache") >= 0 && (t.indexOf("pause") >= 0 || t.indexOf("drain") >= 0 ||
                                         t.indexOf("% full") >= 0))) {
            const p = guardCachePct(raw);
            return {
                kind: "cache", severity: GUARD_SEVERITY.cache,
                message: "\u23F8 paused \u2014 upload cache " + (p != null ? p + "%" : "nearly") +
                         " full, waiting for rclone to drain",
                why: "Writing into a full VFS cache blocks forever instead of failing. " +
                     "The engine holds no attempt and no cookie while it waits \u2014 this " +
                     "is a deliberate pause, not a stall."
            };
        }

        // 4. STALL ABORT / resume. Distinguish the three phases so the operator can
        //    see that the ONE permitted Range resume has been used up.
        if (t.indexOf("stall") >= 0 || t.indexOf("no bytes for") >= 0) {
            // "stall abort on resume" / "stalled again" means the ONE permitted resume
            // was itself killed by the watchdog -> the engine stopped for good.
            const gaveUp = t.indexOf("on resume") >= 0 || t.indexOf("again") >= 0 ||
                           t.indexOf("gave up") >= 0 || t.indexOf("giving up") >= 0 ||
                           t.indexOf("twice") >= 0;
            const resumed = !gaveUp && (t.indexOf("resum") >= 0 || st === "ACTIVE");
            return {
                kind: "stall", severity: GUARD_SEVERITY.stall,
                message: gaveUp
                    ? "\u23F1 stalled twice \u2014 gave up"
                    : (resumed ? "\u23F1 stalled \u2192 resumed (1 of 1)"
                               : "\u23F1 stalled \u2014 stream aborted by the watchdog"),
                why: gaveUp
                    ? "A second resume would spend a third attempt on a link that is not " +
                      "moving, so the engine stopped. Attempts left are shown per part."
                    : "A stream that moves zero bytes never trips a socket timeout, so a " +
                      "watchdog kills it. Exactly ONE Range resume is allowed \u2014 a resume " +
                      "can cost one of the 5 attempts per part."
            };
        }
        return null;
    }

    /* ---------- state ---------------------------------------------------- */
    const TERMINAL_JOBS = new Set(['COMPLETE', 'FAILED', 'BUDGET_EXHAUSTED']);
    const SSE_KINDS = ['part_progress', 'part_done', 'part_error', 'part_update',
                       'job_status', 'heartbeat', 'attempt_spent', 'needs_cookie',
                       'budget_warning', 'self_heal', 'error'];

    const cfg = { mgrUrl: '', token: '', loaded: false };

    let host = null, root = null, els = null;   // DOM
    let started = false;
    let selected = null;                        // archive_id being rendered
    let jobSnap = null;                         // last snapshot
    let rows = new Map();                       // idx -> part row
    let partSpeed = new Map();                  // idx -> EWMA bps
    let prevPollBytes = new Map();               // idx -> bytes at last poll
    let lastPollAt = 0;
    let lastSeq = 0;                            // SSE resume cursor
    let lastEventAt = 0;
    let sseFails = 0, inPollMode = false, offline = false;
    let es = null, pollTimer = null, jobsTimer = null, offlineTimer = null;
    let tickTimer = null, retryTimer = null, navTimer = null;
    const listeners = [];                       // [event, handler] on window
    let activity = [];                          // newest-first log lines
    let captureHistory = [];
    let collapsed = true;                       // 08 §3.2: remembered, starts collapsed
    let posPersist = null;                      // {right, bottom}
    let openSections = { parts: false, captures: false, activity: false };
    let guardNotices = new Map();               // kind@source -> {guard, at}
    let guardStorageNote = null, guardStorageDismissed = false;
    let guardActive = new Set();
    let dirty = false, rafPending = false;
    let busy = '';                              // in-flight control action

    /* ---------- tiny utils ----------------------------------------------- */
    function log(msg) {
        activity.unshift({ ts: Date.now(), msg: String(msg) });
        if (activity.length > 40) activity.length = 40;
    }
    function safe(fn) {                         // never throw into the page
        return function () {
            try { return fn.apply(null, arguments); }
            catch (e) { try { console.debug('[tkOverlay]', e && e.message); } catch (_) {} }
        };
    }
    function hhmm(ts) {
        try { return new Date(ts).toLocaleTimeString(); } catch (_) { return ''; }
    }

    /* ---------- config + storage ------------------------------------------
       Manager URL + capture token are resolved exactly as popup.js's
       v2LiveMonitor does: ask the background service worker for its state. */
    function loadConfig(cb) {
        let done = false;
        const finish = function (ok) { if (!done) { done = true; cfg.loaded = ok; cb(ok); } };
        try {
            if (!chrome || !chrome.runtime || !chrome.runtime.sendMessage) { finish(false); return; }
            chrome.runtime.sendMessage({ action: 'getState' }, function (st) {
                try {
                    if (chrome.runtime.lastError || !st) { finish(false); return; }
                    cfg.mgrUrl = String(st.managerUrl || 'http://127.0.0.1:8080').replace(/\/+$/, '');
                    cfg.token = st.captureToken || '';
                    finish(true);
                } catch (_) { finish(false); }
            });
        } catch (_) { finish(false); }
        setTimeout(function () { finish(cfg.loaded); }, 4000);   // never hang
    }

    function loadPrefs(cb) {
        try {
            chrome.storage.local.get(
                ['overlayCollapsed', 'overlayPos', 'captureHistory'],
                function (d) {
                    try {
                        d = d || {};
                        collapsed = d.overlayCollapsed !== false;   // default collapsed
                        if (d.overlayPos && typeof d.overlayPos === 'object') {
                            posPersist = {
                                right: Number(d.overlayPos.right) || 16,
                                bottom: Number(d.overlayPos.bottom) || 16
                            };
                        }
                        captureHistory = Array.isArray(d.captureHistory) ? d.captureHistory : [];
                    } catch (_) {}
                    cb();
                });
        } catch (_) { cb(); }
    }
    function savePref(patch) {
        try { chrome.storage.local.set(patch, function () { void chrome.runtime.lastError; }); }
        catch (_) {}
    }
    // Read-only consumer of captureHistory: the background writer owns the key,
    // the overlay only renders the last HISTORY_MAX entries.
    function refreshCaptureHistory() {
        try {
            chrome.storage.local.get(['captureHistory'], function (d) {
                try {
                    if (d && Array.isArray(d.captureHistory)) {
                        captureHistory = d.captureHistory;
                        scheduleRender();
                    }
                } catch (_) {}
            });
        } catch (_) {}
    }

    /* ---------- network ---------------------------------------------------- */
    function v2Url(path) {
        return (cfg.mgrUrl || 'http://127.0.0.1:8080') + '/api/v2' + path;
    }
    function api(path, opts) {
        opts = opts || {};
        const headers = Object.assign({}, opts.headers || {});
        if (cfg.token) headers['X-Capture-Token'] = cfg.token;
        return fetch(v2Url(path), {
            method: opts.method || 'GET',
            headers: headers,
            body: opts.body || undefined,
            cache: 'no-store'
        }).then(function (r) {
            if (!r.ok) throw new Error('HTTP ' + r.status + ' ' + path);
            return r.json();
        });
    }

    function setOffline(isOff, why) {
        if (offline === isOff) return;
        offline = isOff;
        if (isOff) {
            log('manager offline' + (why ? ' (' + why + ')' : ''));
            stopStreams();
            // 08 §3.2: unreachable manager => "manager offline" + retry every 5 s.
            if (!offlineTimer) offlineTimer = setInterval(safe(pickJob), OFFLINE_RETRY_MS);
        } else {
            log('manager online');
            if (offlineTimer) { clearInterval(offlineTimer); offlineTimer = null; }
        }
        scheduleRender();
    }

    /* ---------- job selection ---------------------------------------------
       Preference order:
         1. the archive_id in THIS page's URL (?j=<uuid>) — the same field
            content.js scrapes as 'url-j-param' / archiveIdFromUri.
         2. otherwise the most recently active non-terminal job.
         3. otherwise the newest job (GET /jobs is created_at DESC). */
    function pageArchiveId() {
        try {
            const u = new URL(location.href);
            const j = u.searchParams.get('j');
            if (j) return j;
            const m = location.href.match(/[?&]j=([a-f0-9-]+)/i);
            return m ? m[1] : null;
        } catch (_) { return null; }
    }

    function chooseJob(jobs) {
        if (!jobs || !jobs.length) return null;
        const want = pageArchiveId();
        if (want) {
            for (let i = 0; i < jobs.length; i++) {
                if (jobs[i].archive_id === want) return jobs[i].archive_id;
            }
        }
        const live = jobs.filter(function (j) { return !TERMINAL_JOBS.has(j.status); });
        if (live.length) {
            const moving = live.filter(function (j) {
                return (j.parts_active || 0) > 0 || j.status === 'DOWNLOADING';
            })[0];
            return (moving || live[0]).archive_id;
        }
        return jobs[0].archive_id;
    }

    function pickJob() {
        if (!started) return;
        api('/jobs?limit=50').then(function (data) {
            setOffline(false);
            const jobs = (data && data.jobs) || [];
            const id = chooseJob(jobs);
            if (!id) { selected = null; jobSnap = null; scheduleRender(); return; }
            if (id !== selected) { selectJob(id); return; }
            // Same job: fold the fresh list row into the snapshot so the header,
            // totals and the Pause/Resume label stay correct even when the SSE
            // stream is quiet (a PAUSED runner emits nothing).
            for (let i = 0; i < jobs.length; i++) {
                if (jobs[i].archive_id !== id) continue;
                if (jobSnap) {
                    const j = jobs[i];
                    const keep = ['status', 'label', 'export_ts', 'parts_total', 'parts_done',
                                  'parts_active', 'parts_partial', 'parts_failed',
                                  'parts_exhausted', 'bytes_done', 'bytes_total'];
                    for (let k = 0; k < keep.length; k++) {
                        if (j[keep[k]] != null) jobSnap[keep[k]] = j[keep[k]];
                    }
                } else {
                    jobSnap = jobs[i];
                }
                break;
            }
            scheduleRender();
        }).catch(function (e) { setOffline(true, e && e.message); });
    }

    function selectJob(id) {
        selected = id;
        stopStreams();
        resetJobView();
        log('watching ' + id);
        api('/jobs/' + encodeURIComponent(id) + '?parts=1').then(function (snap) {
            setOffline(false);
            jobSnap = snap;
            seedRows(snap);
            lastSeq = 0;
            sseFails = 0;
            inPollMode = false;
            connectSSE();
            scheduleRender();
        }).catch(function (e) { setOffline(true, e && e.message); });
    }

    /* ---------- SSE (06 §6) ----------------------------------------------- */
    function stopStreams() {
        if (es) { try { es.close(); } catch (_) {} es = null; }
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
        inPollMode = false;
    }

    function connectSSE() {
        if (!started || !selected) return;
        stopStreams();
        inPollMode = false;
        let src;
        try {
            src = new EventSource(v2Url('/jobs/' + encodeURIComponent(selected) +
                                        '/events?since=' + lastSeq));
        } catch (_) { startPolling(); return; }
        es = src;
        src.onopen = safe(function () {
            sseFails = 0; setOffline(false); markEvent(); scheduleRender();
        });
        src.onerror = safe(function () {
            if (es === src) { try { src.close(); } catch (_) {} es = null; }
            sseFails++;
            // 06 §6.4: two consecutive failures -> polling fallback.
            if (sseFails >= 2) { startPolling(); return; }
            retryTimer = setTimeout(safe(connectSSE), 1200);   // resumes at lastSeq
            scheduleRender();
        });
        for (let i = 0; i < SSE_KINDS.length; i++) src.addEventListener(SSE_KINDS[i], onSseEvent);
        src.onmessage = onSseEvent;                            // unnamed frames
    }

    const onSseEvent = safe(function (e) {
        if (!selected) return;
        let env;
        try { env = JSON.parse(e.data); } catch (_) { return; }
        if (env.archive_id && env.archive_id !== selected) return;
        // Cursor comes from the `id:` field (EventSource surfaces it as
        // lastEventId); state.py writes seq there, so this is the same number
        // as env.seq and the reconnect is lossless either way.
        const idSeq = Number(e.lastEventId);
        const seq = isFinite(idSeq) && idSeq > 0 ? idSeq : Number(env.seq);
        if (isFinite(seq) && seq > lastSeq) lastSeq = seq;
        applyEvent(env.kind, env.data || {}, env.ts);
        markEvent();
        setOffline(false);
        scheduleRender();
    });

    function applyEvent(kind, d, ts) {
        if (kind === 'part_progress') {
            const r = row(d.idx);
            if (typeof d.size_on_disk === 'number') r.size_on_disk = d.size_on_disk;
            if (d.size_expected != null) r.size_expected = d.size_expected;
            if (d.state) r.state = d.state;
            if (d.verify) r.verify = d.verify;
            r.error = null; r.outcome = null;
            if (typeof d.speed_bps === 'number') {
                const prev = partSpeed.get(d.idx) || 0;
                partSpeed.set(d.idx, prev === 0 ? d.speed_bps
                    : EWMA_ALPHA * d.speed_bps + (1 - EWMA_ALPHA) * prev);
            }
            // ts lives on the SSE envelope, not the payload. It drives the
            // per-part stall clock, so replayed events keep stall math correct
            // while the stream catches up on connect.
            if (ts != null) r.lastProgressAtMs = parseTs(ts);
        } else if (kind === 'part_done') {
            const r = row(d.idx);
            r.state = 'DONE';
            if (d.size_on_disk != null) r.size_on_disk = d.size_on_disk;
            if (d.size_expected != null) r.size_expected = d.size_expected;
            if (d.verify) r.verify = d.verify;
            r.error = null; r.outcome = null;
            partSpeed.delete(d.idx);
            log('part ' + d.idx + ' done');
        } else if (kind === 'part_error') {
            const r = row(d.idx);
            r.state = d.state || 'PARTIAL';
            r.error = d.error || d.outcome || 'error';
            r.outcome = d.outcome || null;
            r.attempts_left = typeof d.attempts_left === 'number' ? d.attempts_left : null;
            r.lastProgressAtMs = null;               // don't ALSO flag it stalled
            partSpeed.delete(d.idx);
            noteGuard(r.error, r.state, 'part ' + d.idx);
            log('part ' + d.idx + ' error: ' + r.error);
        } else if (kind === 'part_update') {
            // The engine emits part_update (state.update_part) for quiet
            // transitions; guard text such as "storage preflight: …" arrives on
            // THIS kind, so it must be inspected too.
            const r = row(d.idx);
            if (d.status) r.state = String(d.status).toUpperCase();
            if (d.verify_state) r.verify = d.verify_state;
            if (typeof d.size_on_disk === 'number') r.size_on_disk = d.size_on_disk;
            if (d.error != null) { r.error = d.error; noteGuard(d.error, r.state, 'part ' + d.idx); }
            if (r.state === 'DONE') { r.error = null; partSpeed.delete(d.idx); }
        } else if (kind === 'attempt_spent') {
            const r = row(d.idx);
            if (typeof d.attempts === 'number') r.attempts_used = d.attempts;
            if (d.outcome) r.last_error = d.outcome;
            // The ledger note is the ONLY place a successful stall resume is
            // visible, so mine it for stall state.
            if (d.note) noteGuard(d.note, r.state, 'part ' + d.idx + ' ledger');
        } else if (kind === 'job_status') {
            if (jobSnap && d.status) jobSnap.status = d.status;
            if (jobSnap && d.error) jobSnap.last_error = d.error;
            if (d.error) noteGuard(d.error, d.status, 'job');
            if (d.status) log('job ' + String(d.status).toLowerCase());
        } else if (kind === 'needs_cookie') {
            log('\uD83D\uDD11 cookie expired \u2014 waiting for a fresh capture');
        } else if (kind === 'budget_warning') {
            log('\u26A0 part ' + (d.idx != null ? d.idx : '?') + ' on its last attempt');
        } else if (kind === 'self_heal') {
            log('\uD83D\uDD04 self-heal: re-captured automatically');
        } else if (kind === 'error') {
            const m = d.message || d.error || 'engine error';
            log('engine error: ' + m);
            noteGuard(m, null, 'engine');
        }
        // heartbeat / unknown kinds: nothing to apply. heartbeat resets the
        // stall clock via markEvent() and still advances lastSeq.
    }

    /* ---------- polling fallback ------------------------------------------ */
    function startPolling() {
        if (!selected || inPollMode) return;
        inPollMode = true;
        if (es) { try { es.close(); } catch (_) {} es = null; }
        log('SSE unavailable \u2014 polling every 2s');
        pollTimer = setInterval(safe(pollJob), POLL_INTERVAL);
        pollJob();
        scheduleRender();
    }

    function pollJob() {
        if (!selected) return;
        api('/jobs/' + encodeURIComponent(selected) + '?parts=1').then(function (snap) {
            setOffline(false);
            jobSnap = snap;
            const now = Date.now();
            const dt = lastPollAt ? (now - lastPollAt) / 1000 : 0;
            lastPollAt = now;
            const parts = (snap && snap.parts) || [];
            for (let i = 0; i < parts.length; i++) {
                const p = parts[i];
                const r = row(p.idx);
                r.state = p.status || r.state;
                if (p.filename) r.filename = p.filename;
                if (p.verify_state) r.verify = p.verify_state;
                if (p.size_expected != null) r.size_expected = p.size_expected;
                if (p.attempts_used != null) r.attempts_used = p.attempts_used;
                if (p.last_error) { r.error = p.last_error; r.lastProgressAtMs = null; }
                if (typeof p.size_on_disk === 'number') {
                    const prev = prevPollBytes.get(p.idx);
                    if (dt > 0 && prev != null && p.size_on_disk > prev && dt < 30) {
                        const inst = (p.size_on_disk - prev) / dt;
                        const old = partSpeed.get(p.idx) || 0;
                        partSpeed.set(p.idx, old === 0 ? inst
                            : EWMA_ALPHA * inst + (1 - EWMA_ALPHA) * old);
                        r.lastProgressAtMs = now;
                    }
                    r.size_on_disk = p.size_on_disk;
                }
                prevPollBytes.set(p.idx, p.size_on_disk || 0);
            }
            markEvent();
            scheduleRender();
        }).catch(function (e) { setOffline(true, e && e.message); });
    }

    /* ---------- row model ------------------------------------------------- */
    function row(idx) {
        let r = rows.get(idx);
        if (!r) {
            r = { idx: idx, filename: 'part ' + idx, state: 'PENDING', verify: null,
                  size_on_disk: 0, size_expected: null, error: null, outcome: null,
                  attempts_used: 0, attempts_left: null, lastProgressAtMs: null };
            rows.set(idx, r);
        }
        return r;
    }

    function seedRows(snap) {
        rows.clear(); partSpeed.clear(); prevPollBytes.clear();
        const parts = (snap && snap.parts) || [];
        for (let i = 0; i < parts.length; i++) {
            const p = parts[i];
            const r = row(p.idx);
            r.filename = p.filename || 'part ' + p.idx;
            r.state = p.status || 'PENDING';
            r.verify = p.verify_state || null;
            r.size_on_disk = p.size_on_disk || 0;
            r.size_expected = p.size_expected != null ? p.size_expected : null;
            r.attempts_used = p.attempts_used || 0;
            if (p.last_error) r.error = p.last_error;
            // Already moving: seed the stall clock so we never falsely flag
            // them as stalled right after load.
            if (p.status === 'ACTIVE' || p.status === 'PARTIAL') r.lastProgressAtMs = Date.now();
        }
        const total = (snap && snap.parts_total) || parts.length;
        if (total > parts.length) for (let i = parts.length; i < total; i++) row(i);
    }

    function resetJobView() {
        rows.clear(); partSpeed.clear(); prevPollBytes.clear();
        jobSnap = null; lastSeq = 0; lastPollAt = 0;
        guardNotices.clear(); guardStorageNote = null; guardStorageDismissed = false;
    }

    function markEvent() { lastEventAt = Date.now(); }

    function aggregateSpeed() {
        let sum = 0;
        partSpeed.forEach(function (spd, idx) {
            const r = rows.get(idx);
            if (!r || r.state === 'DONE' || r.state === 'FAILED' ||
                r.state === 'BUDGET_EXHAUSTED') return;
            sum += spd;
        });
        return sum;
    }

    function stalledFor(r, now) {
        if (!r.lastProgressAtMs) return null;
        if (r.state !== 'ACTIVE' && r.state !== 'PARTIAL') return null;
        const sec = (now - r.lastProgressAtMs) / 1000;
        return sec >= STALL_S ? Math.floor(sec) : null;
    }

    /* ---------- guard surfacing (same no-latch rules as monitor.html) -----
       noteGuard() records an EPHEMERAL sighting from an event (events are
       one-shot: the wording never appears again). Guards still present in the
       snapshot are recomputed every render, so both paths converge and nothing
       latches except the catastrophic storage note. */
    function noteGuard(text, status, source) {
        const g = classifyGuard(text, status);
        if (!g) return;
        if (g.kind === 'storage') {
            if (!guardStorageDismissed) guardStorageNote = g;
            return;
        }
        guardNotices.set(g.kind + '@' + (source || ''), { guard: g, at: Date.now() });
    }

    function collectGuards(now) {
        const best = new Map();
        const add = function (g) {
            if (!g) return;
            if (g.kind === 'storage') {
                if (!guardStorageDismissed) guardStorageNote = g;
                return;
            }
            if (!best.has(g.kind)) best.set(g.kind, g);
        };
        if (jobSnap && jobSnap.last_error) add(classifyGuard(jobSnap.last_error, jobSnap.status));
        rows.forEach(function (r) {
            if (r.error) add(classifyGuard(r.error, r.state));
            else if (r.last_error) add(classifyGuard(r.last_error, r.state));
        });
        guardNotices.forEach(function (v, k) {
            if (now - v.at > GUARD_NOTICE_TTL_MS) guardNotices.delete(k);
            else add(v.guard);
        });
        const out = [];
        best.forEach(function (g) { out.push(g); });
        out.sort(function (a, b) { return b.severity - a.severity; });   // storage>rate>cache>stall
        return out;
    }

    function worstGuard(now) {
        if (guardStorageNote && !guardStorageDismissed) return guardStorageNote;
        const list = collectGuards(now);
        return list.length ? list[0] : null;
    }

    /* ---------- shadow DOM ------------------------------------------------
       All CSS lives inside the shadow root: Google's stylesheets cannot reach
       in and nothing we declare leaks out (08 §3). The host is a single fixed
       element in the bottom-right corner with pointer events confined to the
       card, so Google's own Download buttons are never covered or blocked. */
    const CSS = [
        ':host{all:initial}',
        '*{box-sizing:border-box;margin:0;padding:0}',
        '.card{position:fixed;font:12px/1.45 -apple-system,BlinkMacSystemFont,',
        '"Segoe UI",Roboto,Arial,sans-serif;width:330px;max-height:70vh;',
        'display:flex;flex-direction:column;background:#12151c;color:#e6e9ef;',
        'border:1px solid #2b3240;border-radius:10px;',
        'box-shadow:0 8px 28px rgba(0,0,0,.45);overflow:hidden}',
        '.card.collapsed{width:230px}',
        '.hd{display:flex;align-items:center;gap:8px;padding:8px 10px;',
        'background:#181c25;border-bottom:1px solid #2b3240;cursor:grab;',
        'user-select:none;flex:0 0 auto}',
        '.hd:active{cursor:grabbing}',
        '.chev{width:12px;text-align:center;color:#8b94a7;font-size:10px}',
        '.title{flex:1;font-weight:600;letter-spacing:.2px;white-space:nowrap;',
        'overflow:hidden;text-overflow:ellipsis}',
        '.health{display:flex;align-items:center;gap:5px;font-size:10.5px;color:#8b94a7;',
        'white-space:nowrap}',
        '.dot{width:8px;height:8px;border-radius:50%;background:#4b5364;flex:0 0 auto}',
        '.dot.ok{background:#3fb950}.dot.warn{background:#d29922}',
        '.dot.err{background:#f85149}.dot.off{background:#4b5364}',
        '.body{padding:9px 10px;overflow-y:auto;flex:1 1 auto}',
        '.body::-webkit-scrollbar{width:7px}',
        '.body::-webkit-scrollbar-thumb{background:#2b3240;border-radius:4px}',
        '.job{display:flex;gap:6px;align-items:baseline;margin-bottom:6px}',
        '.job b{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
        '.job .sep{color:#4b5364}.job .ts{color:#8b94a7;font-size:11px}',
        '.bar{height:7px;background:#232936;border-radius:4px;overflow:hidden;margin:5px 0}',
        '.bar>i{display:block;height:100%;background:linear-gradient(90deg,#2f81f7,#3fb950);',
        'width:0;transition:width .4s ease}',
        '.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:11px}',
        '.line{color:#c2c9d6;margin:2px 0}',
        '.line .dim{color:#8b94a7}',
        '.budget{margin:5px 0 0;font-size:11px;color:#8b94a7}',
        '.budget.warn{color:#d29922}.budget.bad{color:#f85149}',
        '.guard{margin:7px 0 0;padding:6px 8px;border-radius:6px;font-size:11px;',
        'border:1px solid transparent;line-height:1.4}',
        '.guard.g-storage{background:#3d1416;border-color:#7d2b2f;color:#ffb4b0}',
        '.guard.g-amber{background:#3a2c0c;border-color:#7a5c11;color:#f2cc60}',
        '.guard.g-quiet{background:#1c2230;border-color:#2b3240;color:#9aa4b6}',
        '.guard .why{display:block;margin-top:3px;color:#8b94a7;font-size:10.5px}',
        '.btns{display:flex;gap:6px;margin:9px 0 2px;padding-top:8px;',
        'border-top:1px solid #232936}',
        'button{flex:1;font:inherit;font-size:11px;padding:5px 6px;cursor:pointer;',
        'background:#232936;color:#e6e9ef;border:1px solid #2b3240;border-radius:6px;',
        'white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
        'button:hover:not(:disabled){background:#2b3240;border-color:#3a4354}',
        'button:disabled{opacity:.45;cursor:default}',
        'section{border-top:1px solid #232936;margin-top:8px}',
        '.sh{display:flex;align-items:center;gap:6px;padding:6px 0;cursor:pointer;',
        'user-select:none;color:#c2c9d6}',
        '.sh:hover{color:#fff}',
        '.sh .tw{width:10px;color:#8b94a7;font-size:9px}',
        '.sh .n{color:#8b94a7;font-size:10.5px;margin-left:auto}',
        '.sc{padding:0 0 7px}',
        '.plist{max-height:190px;overflow-y:auto}',
        '.p{display:flex;align-items:center;gap:6px;padding:2px 0;font-size:11px}',
        '.p .i{width:26px;color:#8b94a7;text-align:right;flex:0 0 auto}',
        '.p .pb{flex:1;height:5px;background:#232936;border-radius:3px;overflow:hidden}',
        '.p .pb>i{display:block;height:100%;background:#2f81f7;width:0}',
        '.p .st{width:74px;flex:0 0 auto;overflow:hidden;text-overflow:ellipsis;',
        'white-space:nowrap;font-size:10.5px}',
        '.p .sp{width:62px;flex:0 0 auto;text-align:right;color:#8b94a7;font-size:10.5px}',
        '.p.done .pb>i{background:#3fb950}.p.done .st{color:#3fb950}',
        '.p.err .pb>i{background:#f85149}.p.err .st{color:#f85149}',
        '.p.risk .st{color:#d29922}',
        '.p.pend .st{color:#6b7385}',
        '.cap{display:flex;gap:6px;font-size:11px;padding:2px 0}',
        '.cap .t{color:#8b94a7;width:58px;flex:0 0 auto}',
        '.cap .f{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
        '.cap.ok .m{color:#3fb950}.cap.bad .m{color:#f85149}',
        '.act{font-size:10.5px;color:#9aa4b6;padding:1px 0;',
        'display:flex;gap:6px;align-items:baseline}',
        '.act .t{color:#6b7385;flex:0 0 auto}',
        '.act .m{flex:1;word-break:break-word}',
        '.none{color:#6b7385;font-size:11px;padding:2px 0}'
    ].join('');

    function build() {
        host = document.createElement('div');
        host.setAttribute('data-tk-overlay', '1');
        // The host itself must not intercept anything outside the card.
        host.style.cssText = 'all:initial;position:static;';
        root = host.attachShadow({ mode: 'open' });

        const style = document.createElement('style');
        style.textContent = CSS;
        root.appendChild(style);

        const card = document.createElement('div');
        card.className = 'card';
        card.style.zIndex = String(Z_INDEX);
        card.innerHTML = [
            '<div class="hd" part="hd">',
            '  <span class="chev" data-chev>\u25BC</span>',
            '  <span class="title">Takeout Downloader</span>',
            '  <span class="health"><span class="dot" data-dot></span>',
            '    <span data-health>starting\u2026</span></span>',
            '</div>',
            '<div class="body" data-body>',
            '  <div class="job"><b data-label>\u2014</b><span class="sep">\u00B7</span>',
            '    <span class="ts" data-export></span></div>',
            '  <div class="bar"><i data-bar></i></div>',
            '  <div class="line mono" data-parts>\u2014</div>',
            '  <div class="line mono" data-bytes>\u2014</div>',
            '  <div class="budget" data-budget></div>',
            '  <div data-guard></div>',
            '  <div class="btns">',
            '    <button data-act="pause" type="button">\u23F8 Pause</button>',
            '    <button data-act="recapture" type="button">\u21BB Recapture</button>',
            '    <button data-act="monitor" type="button">\uD83D\uDCC8 Monitor</button>',
            '  </div>',
            '  <section><div class="sh" data-sec="parts"><span class="tw">\u25B8</span>',
            '    <span>Parts</span><span class="n" data-parts-n></span></div>',
            '    <div class="sc" data-sc="parts" hidden><div class="plist" data-plist></div></div>',
            '  </section>',
            '  <section><div class="sh" data-sec="captures"><span class="tw">\u25B8</span>',
            '    <span>Capture history</span><span class="n" data-cap-n></span></div>',
            '    <div class="sc" data-sc="captures" hidden><div data-caplist></div></div>',
            '  </section>',
            '  <section><div class="sh" data-sec="activity"><span class="tw">\u25B8</span>',
            '    <span>Activity</span><span class="n" data-act-n></span></div>',
            '    <div class="sc" data-sc="activity" hidden><div data-actlist></div></div>',
            '  </section>',
            '</div>'
        ].join('');
        root.appendChild(card);

        const q = function (sel) { return root.querySelector(sel); };
        els = {
            card: card, hd: q('.hd'), body: q('[data-body]'),
            chev: q('[data-chev]'), dot: q('[data-dot]'), health: q('[data-health]'),
            label: q('[data-label]'), exportTs: q('[data-export]'),
            bar: q('[data-bar]'), parts: q('[data-parts]'), bytes: q('[data-bytes]'),
            budget: q('[data-budget]'), guard: q('[data-guard]'),
            partsN: q('[data-parts-n]'), plist: q('[data-plist]'),
            capN: q('[data-cap-n]'), caplist: q('[data-caplist]'),
            actN: q('[data-act-n]'), actlist: q('[data-actlist]'),
            scParts: q('[data-sc="parts"]'), scCaptures: q('[data-sc="captures"]'),
            scActivity: q('[data-sc="activity"]')
        };

        applyPos();
        applyCollapsed();
        wireEvents(card);
        (document.body || document.documentElement).appendChild(host);
    }

    function applyPos() {
        const p = posPersist || { right: 16, bottom: 16 };
        els.card.style.right = Math.max(0, p.right) + 'px';
        els.card.style.bottom = Math.max(0, p.bottom) + 'px';
        els.card.style.left = 'auto';
        els.card.style.top = 'auto';
    }

    function applyCollapsed() {
        els.card.classList.toggle('collapsed', collapsed);
        els.body.hidden = collapsed;
        els.chev.textContent = collapsed ? '\u25B6' : '\u25BC';
    }

    /* ---------- events: collapse, drag, sections, controls ----------------- */
    function wireEvents(card) {
        let drag = null;

        els.hd.addEventListener('mousedown', safe(function (ev) {
            if (ev.button !== 0) return;
            const r = card.getBoundingClientRect();
            drag = {
                x: ev.clientX, y: ev.clientY, moved: false,
                right: window.innerWidth - r.right, bottom: window.innerHeight - r.bottom
            };
            ev.preventDefault();
        }));

        const onMove = safe(function (ev) {
            if (!drag) return;
            const dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
            if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 4) return;
            drag.moved = true;
            const r = card.getBoundingClientRect();
            const right = Math.min(Math.max(0, drag.right - dx),
                                   Math.max(0, window.innerWidth - r.width));
            const bottom = Math.min(Math.max(0, drag.bottom - dy),
                                    Math.max(0, window.innerHeight - r.height));
            card.style.right = right + 'px';
            card.style.bottom = bottom + 'px';
        });

        const onUp = safe(function () {
            if (!drag) return;
            const wasDrag = drag.moved;
            drag = null;
            if (wasDrag) {
                const r = card.getBoundingClientRect();
                posPersist = {
                    right: Math.round(window.innerWidth - r.right),
                    bottom: Math.round(window.innerHeight - r.bottom)
                };
                savePref({ overlayPos: posPersist });
            } else {
                // A click (not a drag) on the header toggles collapse (08 §3.1).
                collapsed = !collapsed;
                applyCollapsed();
                savePref({ overlayCollapsed: collapsed });
                if (!collapsed) { refreshCaptureHistory(); scheduleRender(); }
            }
        });

        window.addEventListener('mousemove', onMove, true);
        window.addEventListener('mouseup', onUp, true);
        listeners.push(['mousemove', onMove], ['mouseup', onUp]);

        root.addEventListener('click', safe(function (ev) {
            const path = ev.composedPath ? ev.composedPath() : [ev.target];
            for (let i = 0; i < path.length; i++) {
                const el = path[i];
                if (!el || !el.getAttribute) continue;
                const sec = el.getAttribute('data-sec');
                if (sec) { toggleSection(sec, el); return; }
                const act = el.getAttribute('data-act');
                if (act) { ev.stopPropagation(); onAction(act); return; }
                if (el === els.hd) return;                     // handled on mouseup
            }
        }));
    }

    function toggleSection(name, headerEl) {
        openSections[name] = !openSections[name];
        const sc = name === 'parts' ? els.scParts
                 : name === 'captures' ? els.scCaptures : els.scActivity;
        if (sc) sc.hidden = !openSections[name];
        const tw = headerEl.querySelector('.tw');
        if (tw) tw.textContent = openSections[name] ? '\u25BE' : '\u25B8';
        if (name === 'captures') refreshCaptureHistory();
        scheduleRender();
    }

    /* ---------- controls (08 §2 routes) -----------------------------------
       POST /api/v2/jobs/{id}/pause | /resume | /start, token in
       X-Capture-Token. Recapture goes through the background worker's existing
       forceCapture action; Monitor opens {mgr}/ui/monitor.html. */
    function onAction(act) {
        if (busy) return;
        if (act === 'monitor') { openMonitor(); return; }
        if (act === 'recapture') { doRecapture(); return; }
        if (act === 'pause') { doPauseResume(); return; }
    }

    function control(route, label) {
        if (!selected) { log('no job selected'); scheduleRender(); return; }
        busy = label;
        scheduleRender();
        api('/jobs/' + encodeURIComponent(selected) + route, { method: 'POST' })
            .then(function () {
                busy = '';
                log(label + ' ok');
                // Re-seed so the header state is authoritative immediately.
                api('/jobs/' + encodeURIComponent(selected) + '?parts=1')
                    .then(function (snap) { jobSnap = snap; scheduleRender(); })
                    .catch(function () { scheduleRender(); });
            })
            .catch(function (e) {
                busy = '';
                log(label + ' failed: ' + (e && e.message));
                scheduleRender();
            });
    }

    function doPauseResume() {
        const st = jobSnap && jobSnap.status;
        if (st === 'PAUSED') { control('/resume', 'resume'); return; }
        if (st === 'READY' || st === 'DISCOVERING') { control('/start', 'start'); return; }
        control('/pause', 'pause');
    }

    function doRecapture() {
        busy = 'recapture';
        scheduleRender();
        log('re-capturing\u2026');
        try {
            chrome.runtime.sendMessage({ action: 'forceCapture' }, function (res) {
                busy = '';
                try {
                    if (chrome.runtime.lastError) {
                        log('recapture failed: ' + chrome.runtime.lastError.message);
                    } else if (res && res.ok) {
                        log('recapture sent to manager');
                        refreshCaptureHistory();
                        pickJob();
                    } else {
                        log('recapture failed: ' + ((res && res.error) || 'unknown'));
                    }
                } catch (_) {}
                scheduleRender();
            });
        } catch (e) {
            busy = '';
            log('recapture failed: ' + (e && e.message));
            scheduleRender();
        }
    }

    function openMonitor() {
        const url = (cfg.mgrUrl || 'http://127.0.0.1:8080') + '/ui/monitor.html';
        let sent = false;
        try {
            chrome.runtime.sendMessage({ action: 'openMonitor', url: url }, function (res) {
                try {
                    // background.js has no 'openMonitor' handler today: it
                    // returns undefined / sets lastError. Fall back to a plain
                    // window.open so the button always works.
                    if (!sent && (chrome.runtime.lastError || !res || !res.ok)) {
                        sent = true;
                        window.open(url, '_blank');
                    }
                } catch (_) {}
            });
        } catch (_) {
            if (!sent) { sent = true; try { window.open(url, '_blank'); } catch (__) {} }
        }
        // Some Chrome builds never invoke the callback for an unknown action.
        setTimeout(safe(function () {
            if (!sent) { sent = true; window.open(url, '_blank'); }
        }), 400);
    }

    /* ---------- render ----------------------------------------------------- */
    function scheduleRender() {
        dirty = true;
        if (rafPending) return;
        rafPending = true;
        const run = safe(function () {
            rafPending = false;
            if (!dirty) return;
            dirty = false;
            render();
        });
        try { requestAnimationFrame(run); } catch (_) { setTimeout(run, 60); }
    }

    function healthState(now, guard) {
        if (offline) return ['off', '\u25CB manager offline'];
        if (!selected) return ['off', 'no job yet'];
        const st = (jobSnap && jobSnap.status) || '?';
        if (st === 'BUDGET_EXHAUSTED') return ['err', '\u25CF budget exhausted'];
        if (st === 'FAILED') return ['err', '\u25CF failed'];
        if (st === 'NEEDS_COOKIE') return ['warn', '\u25CF needs cookie'];
        if (st === 'PAUSED') return ['warn', '\u25CF paused'];
        if (st === 'COMPLETE') return ['ok', '\u25CF complete'];
        // A deliberate engine pause must never read as "stalled?": while a cache
        // or rate guard is active the stream legitimately goes quiet for minutes.
        if (guard && guard.kind === 'cache') return ['warn', '\u23F8 paused (cache)'];
        if (guard && guard.kind === 'rate') return ['warn', '\u23F3 backing off'];
        if (guard && guard.kind === 'storage') return ['err', '\u25CF storage blocked'];
        const age = lastEventAt ? (now - lastEventAt) / 1000 : 999;
        if (age > 45) return ['warn', '\u25CF quiet ' + Math.floor(age) + 's'];
        return ['ok', '\u25CF healthy' + (inPollMode ? ' (poll)' : '')];
    }

    function render() {
        if (!els) return;
        const now = Date.now();
        const guard = worstGuard(now);
        guardActive = new Set(collectGuards(now).map(function (g) { return g.kind; }));
        if (guardStorageNote && !guardStorageDismissed) guardActive.add('storage');

        const hs = healthState(now, guard);
        els.dot.className = 'dot ' + hs[0];
        els.health.textContent = hs[1];

        if (collapsed) {
            // Collapsed still carries the one number that matters.
            const t = jobSnap || {};
            const d = t.parts_done || 0, m = t.parts_total || 0;
            els.chev.textContent = '\u25B6';
            const title = root.querySelector('.title');
            if (title) {
                title.textContent = m > 0
                    ? 'Takeout \u00B7 ' + d + '/' + m + ' \u00B7 ' +
                      Math.round(pct(t.bytes_done, t.bytes_total)) + '%'
                    : 'Takeout Downloader';
            }
            return;                       // body is hidden; skip its work
        }
        const title = root.querySelector('.title');
        if (title) title.textContent = 'Takeout Downloader';

        const t = jobSnap || {};
        els.label.textContent = t.label || t.archive_id || (offline ? '\u2014' : 'no job');
        els.exportTs.textContent = t.export_ts || '';

        const done = t.parts_done || 0, total = t.parts_total || 0;
        const p = pct(t.bytes_done, t.bytes_total);
        els.bar.style.width = p.toFixed(1) + '%';
        els.parts.innerHTML = '<b>' + done + '</b>/' + total + ' parts \u00B7 ' +
            p.toFixed(0) + '%' + (total ? '' : ' <span class="dim">(discovering)</span>');

        const agg = aggregateSpeed();
        const remaining = Math.max(0, (t.bytes_total || 0) - (t.bytes_done || 0));
        const eta = agg > 0 ? remaining / agg : NaN;
        els.bytes.textContent = fmtBytes(t.bytes_done) + ' / ' + fmtBytes(t.bytes_total) +
            ' \u00B7 ' + fmtSpeed(agg) + ' \u00B7 ETA ' + fmtDur(eta);

        renderBudget(t);
        renderGuard(guard);
        renderButtons();
        renderParts(now, total, done);
        renderCaptures();
        renderActivity();
    }

    function renderBudget(t) {
        // parts_exhausted comes from job_totals; at-risk parts are the ones
        // showing a live error while still retryable.
        const exhausted = typeof t.parts_exhausted === 'number' ? t.parts_exhausted : 0;
        let lastAttempt = 0, failing = 0;
        rows.forEach(function (r) {
            if (r.attempts_left === 1) lastAttempt++;
            if (r.state === 'FAILED' || (r.error && r.state !== 'DONE')) failing++;
        });
        const bits = [];
        if (exhausted > 0) bits.push(exhausted + ' exhausted');
        if (lastAttempt > 0) bits.push('\u26A0 ' + lastAttempt + ' part' +
            (lastAttempt === 1 ? '' : 's') + ' at 1 attempt left');
        else if (failing > 0) bits.push(failing + ' part' + (failing === 1 ? '' : 's') +
            ' with errors');
        if (!bits.length) {
            els.budget.className = 'budget';
            els.budget.textContent = 'budget \u2713 ok';
        } else {
            els.budget.className = 'budget ' + (exhausted > 0 ? 'bad' : 'warn');
            els.budget.textContent = bits.join(' \u00B7 ');
        }
    }

    function renderGuard(g) {
        if (!g) { if (els.guard.innerHTML) els.guard.innerHTML = ''; return; }
        const cls = g.kind === 'storage' ? 'g-storage'
                  : (g.kind === 'stall' ? 'g-quiet' : 'g-amber');
        const html = '<div class="guard ' + cls + '">' + esc(g.message) +
                     '<span class="why">' + esc(g.why) + '</span></div>';
        if (els.guard.innerHTML !== html) els.guard.innerHTML = html;   // avoid clobber
    }

    function renderButtons() {
        const st = (jobSnap && jobSnap.status) || '';
        const pauseBtn = root.querySelector('[data-act="pause"]');
        const recapBtn = root.querySelector('[data-act="recapture"]');
        if (pauseBtn) {
            let label = '\u23F8 Pause';
            if (st === 'PAUSED') label = '\u25B6 Resume';
            else if (st === 'READY' || st === 'DISCOVERING') label = '\u25B6 Start';
            pauseBtn.textContent = busy && busy !== 'recapture' ? '\u2026' : label;
            pauseBtn.disabled = !!busy || !selected || offline ||
                st === 'COMPLETE' || st === 'FAILED';
        }
        if (recapBtn) {
            recapBtn.textContent = busy === 'recapture' ? '\u2026' : '\u21BB Recapture';
            recapBtn.disabled = !!busy;
        }
    }

    function renderParts(now, total, done) {
        let active = 0, pending = 0;
        rows.forEach(function (r) {
            if (r.state === 'ACTIVE' || r.state === 'PARTIAL') active++;
            else if (r.state === 'PENDING') pending++;
        });
        els.partsN.textContent = '(' + done + ' done, ' + active + ' active, ' +
            pending + ' pending)';
        if (!openSections.parts) return;

        const list = [];
        rows.forEach(function (r) { list.push(r); });
        list.sort(function (a, b) {
            // Interesting parts first (active/errored), then by index.
            const ia = (a.state === 'ACTIVE' || a.state === 'PARTIAL' || a.error) ? 0 : 1;
            const ib = (b.state === 'ACTIVE' || b.state === 'PARTIAL' || b.error) ? 0 : 1;
            return ia !== ib ? ia - ib : a.idx - b.idx;
        });
        const html = [];
        const cap = Math.min(list.length, 60);
        for (let i = 0; i < cap; i++) {
            const r = list[i];
            const stalled = stalledFor(r, now);
            // Same classifier as the banners, so a row explains itself
            // ("stalled -> resumed (1 of 1)") instead of showing a raw blob.
            const g = r.state === 'DONE' ? null : classifyGuard(r.error || r.last_error, r.state);
            let cls = 'p', st;
            if (r.state === 'DONE') { cls += ' done'; st = '\u2713 DONE'; }
            else if (r.state === 'BUDGET_EXHAUSTED') { cls += ' err'; st = '\u2716 EXHAUSTED'; }
            else if (g && g.kind === 'storage') { cls += ' err'; st = '\u26D4 storage'; }
            else if (g) { cls += ' risk'; st = g.message.split(' \u2014 ')[0]; }
            else if (r.state === 'FAILED' || r.error) { cls += ' err'; st = '\u2716 ' + r.state; }
            else if (stalled) { cls += ' risk'; st = '\u23F1 ' + stalled + 's'; }
            else if (r.state === 'ACTIVE') { st = 'ACTIVE'; }
            else if (r.state === 'PARTIAL') { cls += ' risk'; st = 'PARTIAL'; }
            else { cls += ' pend'; st = r.state; }
            const rp = r.state === 'DONE' ? 100 : pct(r.size_on_disk, r.size_expected);
            html.push('<div class="' + cls + '"><span class="i">' + r.idx + '</span>' +
                '<span class="pb"><i style="width:' + rp.toFixed(0) + '%"></i></span>' +
                '<span class="st">' + esc(st) + '</span>' +
                '<span class="sp">' + (r.state === 'DONE' ? '\u2014'
                    : fmtSpeed(partSpeed.get(r.idx) || 0)) + '</span></div>');
        }
        if (list.length > cap) {
            html.push('<div class="none">\u2026 ' + (list.length - cap) + ' more</div>');
        }
        if (!html.length) html.push('<div class="none">no parts discovered yet</div>');
        const next = html.join('');
        if (els.plist.dataset.sig !== next) {
            els.plist.dataset.sig = next;
            els.plist.innerHTML = next;
        }
    }

    function renderCaptures() {
        const hist = (captureHistory || []).slice(-HISTORY_MAX).reverse();
        els.capN.textContent = '(' + hist.length + ')';
        if (!openSections.captures) return;
        const html = [];
        for (let i = 0; i < hist.length; i++) {
            const h = hist[i] || {};
            const ok = h.ok !== false;
            html.push('<div class="cap ' + (ok ? 'ok' : 'bad') + '">' +
                '<span class="t">' + esc(hhmm(h.ts)) + '</span>' +
                '<span class="f">' + esc(h.filename || h.archive_id || '\u2014') + '</span>' +
                '<span class="m">' + (ok ? '\u2713' : '\u2716') + '</span></div>');
        }
        if (!html.length) html.push('<div class="none">none yet</div>');
        const next = html.join('');
        if (els.caplist.dataset.sig !== next) {
            els.caplist.dataset.sig = next;
            els.caplist.innerHTML = next;
        }
    }

    function renderActivity() {
        els.actN.textContent = '(' + activity.length + ')';
        if (!openSections.activity) return;
        const html = [];
        for (let i = 0; i < Math.min(activity.length, 20); i++) {
            const a = activity[i];
            html.push('<div class="act"><span class="t">' + esc(hhmm(a.ts)) +
                '</span><span class="m">' + esc(a.msg) + '</span></div>');
        }
        if (!html.length) html.push('<div class="none">nothing yet</div>');
        const next = html.join('');
        if (els.actlist.dataset.sig !== next) {
            els.actlist.dataset.sig = next;
            els.actlist.innerHTML = next;
        }
    }

    /* ---------- SPA navigation -------------------------------------------- */
    function onNavigate() {
        // Takeout is an SPA: the ?j= archive can change without a reload. Only
        // re-pick the job; never rebuild the DOM (that would drop the stream).
        const want = pageArchiveId();
        if (want && want !== selected) pickJob();
    }

    function watchNavigation() {
        let last = location.href;
        const check = safe(function () {
            if (location.href === last) return;
            last = location.href;
            if (!isTakeoutPage()) { destroy(); return; }
            onNavigate();
        });
        navTimer = setInterval(check, 1500);
        const onPop = safe(check);
        window.addEventListener('popstate', onPop, true);
        window.addEventListener('hashchange', onPop, true);
        listeners.push(['popstate', onPop], ['hashchange', onPop]);
    }

    function isTakeoutPage() {
        try { return location.hostname === 'takeout.google.com'; }
        catch (_) { return false; }
    }

    /* ---------- lifecycle -------------------------------------------------- */
    function init() {
        try {
            if (started) return;
            if (!isTakeoutPage()) return;             // 08: takeout.google.com only
            if (!document.documentElement) return;
            started = true;
            loadPrefs(safe(function () {
                if (!started) return;
                build();
                log('overlay ready');
                scheduleRender();
                loadConfig(safe(function (ok) {
                    if (!started) return;
                    if (!ok) { log('cannot reach the extension background'); }
                    pickJob();
                    jobsTimer = setInterval(safe(pickJob), JOBLIST_INTERVAL);
                    // A 1 s tick keeps stall counters, ETA and the health dot
                    // honest even when no event arrives.
                    tickTimer = setInterval(safe(scheduleRender), 1000);
                    watchNavigation();
                }));
            }));
        } catch (e) {
            try { console.debug('[tkOverlay] init failed', e && e.message); } catch (_) {}
        }
    }

    function destroy() {
        try {
            started = false;
            stopStreams();
            if (jobsTimer) { clearInterval(jobsTimer); jobsTimer = null; }
            if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
            if (offlineTimer) { clearInterval(offlineTimer); offlineTimer = null; }
            if (navTimer) { clearInterval(navTimer); navTimer = null; }
            for (let i = 0; i < listeners.length; i++) {
                try { window.removeEventListener(listeners[i][0], listeners[i][1], true); }
                catch (_) {}
            }
            listeners.length = 0;
            if (host && host.parentNode) host.parentNode.removeChild(host);
            host = null; root = null; els = null;
            selected = null; jobSnap = null; offline = false;
            rows.clear(); partSpeed.clear(); prevPollBytes.clear();
        } catch (e) {
            try { console.debug('[tkOverlay] destroy failed', e && e.message); } catch (_) {}
        }
    }

    function update() {
        try {
            if (!started) { init(); return; }
            refreshCaptureHistory();
            pickJob();
            scheduleRender();
        } catch (_) {}
    }

    window.__tkOverlay = {
        init: init,
        destroy: destroy,
        update: update,
        // Exposed for the cross-surface parity test (08 §3.2): these must
        // behave identically to the copies in monitor.html and popup.js.
        _classifyGuard: classifyGuard,
        _guardWaitSecs: guardWaitSecs,
        _guardCachePct: guardCachePct,
        _GUARD_SEVERITY: GUARD_SEVERITY
    };
})();
