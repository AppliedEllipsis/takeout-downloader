// Takeout Downloader Helper - Popup Logic v4
// Rich feedback: activity log, strategy progress during Copy ALL,
// per-part download status in the live panel.

const els = {
    statusBox: null,
    errorBox: null,
    copyAllBtn: null,
    copyCaptureBtn: null,
    clearBtn: null,
    preview: null,
    autoCopy: null,
    countPill: null
};

function $(id) { return document.getElementById(id); }

// ---------------------------------------------------------------------------
// Activity log
// ---------------------------------------------------------------------------
const MAX_ACTIVITY = 100;
const activityLog = [];

function addActivity(icon, msg, detail, cls) {
    const entry = {
        time: new Date().toLocaleTimeString(),
        icon: icon || '·',
        msg: msg || '',
        detail: detail || '',
        cls: cls || 'act-dim'
    };
    activityLog.push(entry);
    if (activityLog.length > MAX_ACTIVITY) activityLog.shift();
    renderActivity();
}

function renderActivity() {
    const container = document.getElementById('activityLog');
    const badge = document.getElementById('activityBadge');
    if (!container) return;
    if (activityLog.length === 0) {
        container.innerHTML = '<div class="act-entry act-dim">Waiting for activity…</div>';
        if (badge) badge.style.display = 'none';
        return;
    }
    if (badge) {
        badge.textContent = activityLog.length;
        badge.style.display = 'inline-block';
    }
    // Newest first
    const items = activityLog.slice().reverse();
    container.innerHTML = items.map(e => {
        const detailHtml = e.detail
            ? `<span class="act-detail">${e.detail}</span>`
            : '';
        return `<div class="act-entry ${e.cls}">` +
            `<span class="act-time">${e.time}</span>` +
            `<span class="act-icon">${e.icon}</span>` +
            `<span class="act-msg">${e.msg}</span>` +
            detailHtml +
            `</div>`;
    }).join('');
    container.scrollTop = 0;
}

// Auto-open activity details on first entry
let activityAutoOpened = false;
function ensureActivityVisible() {
    if (!activityAutoOpened && activityLog.length >= 1) {
        activityAutoOpened = true;
        const det = document.getElementById('activityDetails');
        if (det && !det.open) det.open = true;
    }
}

// ---------------------------------------------------------------------------
// Status helpers
// ---------------------------------------------------------------------------
function setStatus(text, cls) {
    els.statusBox.textContent = text;
    els.statusBox.className = 'status ' + (cls || 'dim');
}

function setError(text) {
    if (!text) {
        els.errorBox.style.display = 'none';
        document.getElementById('errorText').textContent = '';
    } else {
        document.getElementById('errorText').textContent = text;
        els.errorBox.style.display = 'block';
    }
}

function shortFilename(url) {
    if (!url) return 'unknown';
    const tail = url.split('?')[0].split('/').pop() || url;
    return tail.length > 30 ? '\u2026' + tail.slice(-27) : tail;
}

function formatAge(timestamp) {
    if (!timestamp) return '';
    const seconds = Math.max(0, Math.round((Date.now() - timestamp) / 1000));
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
    return `${Math.round(seconds / 3600)}h ago`;
}

function fmtBytes(n) {
    n = n || 0;
    if (n >= 1e12) return (n/1e12).toFixed(2)+' TB';
    if (n >= 1e9) return (n/1e9).toFixed(1)+' GB';
    if (n >= 1e6) return (n/1e6).toFixed(0)+' MB';
    if (n >= 1e3) return (n/1e3).toFixed(0)+' KB';
    return n+' B';
}

function fmtMs(ms) {
    if (ms < 1000) return Math.round(ms) + 'ms';
    return (ms / 1000).toFixed(1) + 's';
}

function parseArchiveId(url) {
    try {
        return new URL(url).searchParams.get('j') || null;
    } catch (e) {
        return null;
    }
}

// ---------------------------------------------------------------------------
// Capture rendering
// ---------------------------------------------------------------------------
function renderCapture(data) {
    const capture = data.capture;
    const count = data.count || 0;
    els.countPill.textContent = String(count);
    els.countPill.style.display = count > 0 ? 'inline-block' : 'none';

    els.copyAllBtn.disabled = !capture || !capture.cookie;
    els.copyCaptureBtn.disabled = !capture || !capture.cookie;

    if (!capture) {
        setStatus('No capture yet. Start a download on takeout.google.com to capture.', 'dim');
        els.clearBtn.disabled = true;
        els.preview.textContent = 'No capture yet.';
        return;
    }

    const filename = shortFilename(capture.url);
    const age = formatAge(capture.captured_at ? Date.parse(capture.captured_at) : Date.now());
    const cookieChars = (capture.cookie || '').length;
    const archiveId = parseArchiveId(capture.url);

    els.clearBtn.disabled = false;

    if (!capture.cookie) {
        setStatus('\u2717 No cookie captured. Try clicking the download again.', 'err');
    } else if (cookieChars < 100) {
        setStatus(
            `\u26a0 Cookie only ${cookieChars} chars (${age}). ` +
            `Likely missing __Secure-* markers. Re-capture from final host.`,
            'warn'
        );
    } else {
        const idHint = archiveId ? ` archive ${archiveId.slice(0,8)}\u2026` : '';
        setStatus(
            `\u2713 Captured: ${filename} (${age}, cookie ${cookieChars} chars, #${count})${idHint}. ` +
            `Click Copy ALL exports to fetch the full archive list.`,
            'ok'
        );
    }

    els.preview.textContent = JSON.stringify({
        url: capture.url,
        cookie: `[${(capture.cookie || '').length} chars]`,
        captured_at: capture.captured_at
    }, null, 2);
}

// ---------------------------------------------------------------------------
// Payload builders
// ---------------------------------------------------------------------------
function buildSinglePayload(capture) {
    return {
        schema: capture.schema || 1,
        captured_at: new Date().toISOString(),
        source: 'extension',
        url: capture.url,
        method: capture.method || 'GET',
        headers: capture.headers || {},
        cookie: capture.cookie || ''
    };
}

function buildMultiPayload(capture, urls, meta) {
    const exports = (urls || []).map((url, i) => {
        const entry = {
            url: url,
            filename: shortFilename(url)
        };
        if (i > 0 || urls.length > 1) entry.partIndex = i;
        if (meta && meta.sizes && meta.sizes[i]) {
            entry.size = meta.sizes[i];
        } else if (meta && meta.buttonData && meta.buttonData[i]) {
            entry.size = meta.buttonData[i].size;
        }
        if (meta && meta.dlCounts && meta.dlCounts[entry.filename] !== undefined) {
            entry.dlCount = meta.dlCounts[entry.filename];
        }
        return entry;
    });

    const hasPartMetadata = meta && meta.archiveId && meta.expectedParts;
    const schema = hasPartMetadata ? 2 : (capture.schema || 1);

    const payload = {
        schema: schema,
        captured_at: new Date().toISOString(),
        source: 'extension',
        multi: true,
        exports: exports,
        headers: capture.headers || {},
        cookie: capture.cookie || ''
    };

    if (hasPartMetadata) {
        payload.archiveId = meta.archiveId;
        payload.expectedParts = meta.expectedParts;
    }

    if (meta) {
        payload._meta = {
            archiveId: meta.archiveId,
            user: meta.user,
            authuser: meta.authuser,
            filenames: meta.filenames,
            buttonCount: meta.buttonData ? meta.buttonData.length : 0,
            expectedParts: meta.expectedParts,
            dlCounts: meta.dlCounts || {}
        };
    }

    return payload;
}

// ---------------------------------------------------------------------------
// Clipboard
// ---------------------------------------------------------------------------
async function copyToClipboard(text, kind) {
    try {
        await navigator.clipboard.writeText(text);
        setStatus(`\u2713 ${kind} copied! Paste into the CLI.`, 'ok');
    } catch (e) {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        try {
            document.execCommand('copy');
            setStatus(`\u2713 ${kind} copied! Paste into the CLI.`, 'ok');
        } catch (e2) {
            setStatus(`\u2717 Could not copy: ${e2.message}. Select text manually.`, 'err');
        } finally {
            document.body.removeChild(ta);
        }
    }
}

// ---------------------------------------------------------------------------
// Fetch all exports via content script (with strategy feedback)
// ---------------------------------------------------------------------------
async function fetchAllExportsFromContentScript(capture) {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.id) {
        return { ok: false, error: 'no active tab' };
    }
    if (!tab.url || !tab.url.startsWith('https://takeout.google.com/')) {
        return { ok: false, error: 'open the Takeout manage page first' };
    }
    try {
        addActivity('\u{1F50D}', 'Delegating to content script on active tab\u2026',
            'tab ' + tab.id, 'act-info');
        const t0 = performance.now();
        const result = await chrome.tabs.sendMessage(tab.id, {
            action: 'contentFetchExports',
            url: capture.url
        });
        const elapsed = performance.now() - t0;

        // Surface strategy info from the content script's response.
        if (result && result.strategies) {
            for (const s of result.strategies) {
                const icon = s.ok ? '\u2713' : '\u2717';
                const cls = s.ok ? 'act-ok' : 'act-warn';
                const detail = s.elapsedMs ? ` (${fmtMs(s.elapsedMs)})` : '';
                addActivity(icon, `Strategy: ${s.name}${detail}`,
                    s.detail || '', cls);
            }
        }
        if (result && result.source) {
            addActivity('\u2713', `Source used: ${result.source}`,
                `total ${fmtMs(elapsed)}`, 'act-ok');
        } else if (result && result.ok) {
            addActivity('\u2713', `Got ${(result.urls||[]).length} URLs`,
                `in ${fmtMs(elapsed)}`, 'act-ok');
        } else {
            addActivity('\u2717', 'Content script returned failure',
                (result && result.error) || 'unknown', 'act-err');
        }
        // Surface debug lines
        if (result && result.debug) {
            for (const d of result.debug) {
                addActivity('\u{1F4DD}', d, '', 'act-dim');
            }
        }
        return result;
    } catch (e) {
        addActivity('\u2717', 'Content script unreachable',
            e.message || String(e), 'act-err');
        return { ok: false, error: 'content script unreachable: ' + e.message };
    }
}

// ---------------------------------------------------------------------------
// Refresh
// ---------------------------------------------------------------------------
function refresh() {
    chrome.runtime.sendMessage({ action: 'getCapture' }, (response) => {
        if (chrome.runtime.lastError) {
            setStatus('Extension error: ' + chrome.runtime.lastError.message, 'err');
            return;
        }
        renderCapture(response || {});
    });
}

// ---------------------------------------------------------------------------
// DOM ready
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
    els.statusBox = $('statusBox');
    els.errorBox = $('errorBox');
    els.copyAllBtn = $('copyAllBtn');
    els.copyCaptureBtn = $('copyCaptureBtn');
    els.clearBtn = $('clearBtn');
    els.preview = $('preview');
    els.autoCopy = $('autoCopy');
    els.countPill = $('countPill');

    const errorDismiss = document.getElementById('errorDismiss');
    if (errorDismiss) {
        errorDismiss.addEventListener('click', () => setError(null));
    }

    refresh();

    // Load preferences
    chrome.runtime.sendMessage({ action: 'getPreferences' }, (prefs) => {
        if (prefs && typeof prefs.autoCopy === 'boolean') {
            els.autoCopy.checked = prefs.autoCopy;
        }
    });

    // "Copy this capture"
    els.copyCaptureBtn.addEventListener('click', async () => {
        setError(null);
        const t0 = performance.now();
        const response = await chrome.runtime.sendMessage({ action: 'getCapture' });
        if (!response || !response.capture) {
            setStatus('No capture to copy.', 'warn');
            return;
        }
        const payload = buildSinglePayload(response.capture);
        await copyToClipboard(JSON.stringify(payload, null, 2), 'this capture');
        addActivity('\u{1F4CB}', 'Copied single capture to clipboard',
            `cookie ${(response.capture.cookie||'').length} chars, ${fmtMs(performance.now() - t0)}`, 'act-ok');
        ensureActivityVisible();
    });

    // [copy] link next to "Preview capture"
    const copyPreview = document.getElementById('copyPreview');
    if (copyPreview) {
        copyPreview.addEventListener('click', async () => {
            setError(null);
            const response = await chrome.runtime.sendMessage({ action: 'getCapture' });
            if (!response || !response.capture) {
                setStatus('No capture to copy.', 'warn');
                return;
            }
            const payload = buildSinglePayload(response.capture);
            await copyToClipboard(JSON.stringify(payload, null, 2), 'this capture');
            addActivity('\u{1F4CB}', 'Copied single capture to clipboard',
                'from preview [copy] link', 'act-ok');
            ensureActivityVisible();
        });
    }

    // "Copy ALL exports" — the main button
    els.copyAllBtn.addEventListener('click', async () => {
        setError(null);
        setStatus('Reading capture\u2026', 'dim');
        const response = await chrome.runtime.sendMessage({ action: 'getCapture' });
        if (!response || !response.capture) {
            setStatus('No capture. Click a download on the Takeout page first.', 'warn');
            return;
        }
        const capture = response.capture;
        if (!capture.cookie) {
            setStatus('No cookie in capture. Re-click a download first.', 'warn');
            return;
        }

        const archiveId = parseArchiveId(capture.url);
        addActivity('\u{1F4E4}', 'Copy ALL exports started',
            `archive ${archiveId ? archiveId.slice(0,8) + '\u2026' : '?'}, ` +
            `cookie ${(capture.cookie||'').length} chars`, 'act-info');

        setStatus('Fetching all exports from Takeout\u2026', 'dim');
        addActivity('\u{1F50D}', 'Querying page for export list\u2026',
            'The extension tries multiple strategies: buttons \u2192 spy cache \u2192 API endpoints \u2192 DOM scrape', 'act-info');

        const result = await fetchAllExportsFromContentScript(capture);

        if (!result || !result.ok) {
            const err = (result && result.error) || 'unknown';
            addActivity('\u26a0', 'Full export fetch failed, falling back to single capture',
                err, 'act-warn');
            // Fallback: single capture
            setStatus('Full export list unavailable (' + err + '). Falling back to single capture.', 'warn');
            const singlePayload = buildSinglePayload(capture);
            await copyToClipboard(JSON.stringify(singlePayload, null, 2), 'this capture');
            setError('Copied the single captured export. The CLI will discover the rest.');
            ensureActivityVisible();
            return;
        }

        const urls = result.urls || [];
        if (urls.length === 0) {
            addActivity('\u2717', '0 URLs returned', 'cookie may be expired', 'act-err');
            setStatus('No URLs returned by Takeout API.', 'warn');
            setError('Takeout returned 0 URLs. ' +
                     'Open the browser console for details. ' +
                     'The cookie may have expired \u2014 click a download again.');
            ensureActivityVisible();
            return;
        }

        const payload = buildMultiPayload(capture, urls, result.meta);

        const sizesArr = result.meta && result.meta.sizes;
        const knownSizes = sizesArr ? sizesArr.filter(s => s > 0).length : 0;
        const sizeInfo = knownSizes > 0
            ? ` (${knownSizes} with sizes)`
            : (result.meta && result.meta.buttonData
                ? ` (${result.meta.buttonData.filter(b => b.size > 0).length} with sizes)`
                : '');
        const partInfo = result.meta && result.meta.expectedParts
            ? ` [detected ${result.meta.expectedParts} part(s)]`
            : '';

        const totalSize = sizesArr
            ? sizesArr.reduce((a, b) => a + (b || 0), 0)
            : 0;

        addActivity('\u2713', `Payload built: ${urls.length} export(s)${partInfo}`,
            totalSize > 0 ? `total ${fmtBytes(totalSize)}` : 'sizes unknown', 'act-ok');

        await copyToClipboard(
            JSON.stringify(payload, null, 2),
            `ALL exports (${urls.length}${sizeInfo})${partInfo}`
        );
        setError(null);
        ensureActivityVisible();
    });

    els.clearBtn.addEventListener('click', () => {
        setError(null);
        chrome.runtime.sendMessage({ action: 'clearCapture' }, () => {
            refresh();
        });
    });

    els.autoCopy.addEventListener('change', () => {
        chrome.runtime.sendMessage({
            action: 'setPreference',
            key: 'autoCopy',
            value: els.autoCopy.checked
        });
    });

    // -----------------------------------------------------------------------
    // v4 manager wiring (with activity log entries)
    // -----------------------------------------------------------------------
    const sendNowBtn = $('sendNowBtn');
    const managerStatus = $('managerStatus');
    const autoPost = $('autoPost');

    // -------------------------------------------------------------------
    // v4.2 budget + identity display (docs/v2/03-UX §5)
    // -------------------------------------------------------------------
    function partIdx(name) {
        const m = /takeout-\d{8}T\d{6}Z-\d+-(\d+)\.zip/.exec(name || '');
        return m ? parseInt(m[1], 10) : 0;
    }
    function escHtml(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }
    // Renders Google's own attempt counter (dl_counts) per part, the account
    // identity (label + export_ts), and a prominent warning when any part has
    // used >= 4 of the 5 attempts. Hidden when no v2 capture exists yet.
    function renderV2Budget(st) {
        const panel = document.getElementById('budgetPanel');
        if (!panel) return;
        const payload = (st && st.lastV2Capture) || null;
        const post = (st && st.lastV2PostStatus) || null;
        if (!payload) {
            panel.style.display = 'none';
            return;
        }
        panel.style.display = 'block';

        const warnBox = document.getElementById('budgetWarn');
        const identity = document.getElementById('budgetIdentity');
        const list = document.getElementById('budgetList');
        const summary = document.getElementById('budgetSummary');

        const acct = payload.account || {};
        const label = acct.label || acct.email || '?';
        const ts = payload.export_ts_raw || 'unknown';
        const src = acct.label_source || '';
        if (identity) {
            identity.textContent = 'account: ' + label +
                (src ? ' [' + src + ']' : '') +
                ' \u00b7 export: ' + ts +
                (post ? (post.ok ? ' \u00b7 v2 POST ok' : ' \u00b7 v2 POST failed') : '');
        }

        const counts = payload.dl_counts || {};
        const names = Object.keys(counts);
        if (summary) {
            summary.textContent = names.length
                ? names.length + ' part(s) \u00b7 Google counter'
                : ((payload.filenames || []).length + ' part(s) \u00b7 no counter visible');
        }
        if (list) {
            list.innerHTML = names.length ? names.slice().sort(function (a, b) {
                return partIdx(a) - partIdx(b);
            }).map(function (f) {
                const n = counts[f];
                const left = Math.max(0, 5 - n);
                const cls = n >= 5 ? 'act-err' : (n >= 4 ? 'act-warn' : 'act-ok');
                return '<div class="act-entry ' + cls + '">' + escHtml(f) +
                    ' \u00b7 ' + n + '/5 used \u00b7 ' + left + ' left</div>';
            }).join('') : '<div class="act-entry act-dim">Google counter not visible on this page.</div>';
        }

        // Prominent budget warning: any part at dl_count >= 4 (<= 1 left).
        if (warnBox) {
            const atRisk = names.filter(function (f) { return counts[f] >= 4; })
                .sort(function (a, b) { return counts[b] - counts[a]; });
            if (atRisk.length > 0) {
                const worst = atRisk[0];
                const n = counts[worst];
                const left = Math.max(0, 5 - n);
                warnBox.style.display = 'block';
                warnBox.className = 'status err';
                warnBox.innerHTML = (n >= 5
                    ? '\u26a0\ufe0f <b>BUDGET EXHAUSTED</b> \u2014 part ' + escHtml(worst) +
                      ': ' + n + '/5 downloads used, 0 left. Re-request the export before downloading more.'
                    : '\u26a0\ufe0f <b>LAST ATTEMPT</b> \u2014 part ' + escHtml(worst) +
                      ': ' + n + '/5 downloads used, ' + left + ' left. Download deliberately.');
            } else {
                warnBox.style.display = 'none';
            }
        }
    }

    function refreshManager() {
        chrome.runtime.sendMessage({ action: 'getState' }, (st) => {
            if (!st) return;
            if (autoPost) autoPost.checked = st.autoPost !== false;
            if (sendNowBtn) sendNowBtn.disabled = !st.hasCapture;
            renderV2Budget(st);
            if (!managerStatus) return;
            const p = st.lastPostStatus;
            if (p && p.ok) {
                managerStatus.textContent = 'Manager: job ' + (p.jobId || '?') +
                    ' (' + (p.status || '?') + ')';
                managerStatus.className = 'status ok';
            } else if (p && !p.ok) {
                managerStatus.textContent = 'Manager: ' + (p.error || 'unreachable');
                managerStatus.className = 'status err';
            } else {
                managerStatus.textContent = 'Manager: idle (no send yet)';
                managerStatus.className = 'status dim';
            }
        });
    }

    if (sendNowBtn) {
        sendNowBtn.addEventListener('click', () => {
            setError(null);
            managerStatus.textContent = 'Manager: sending\u2026';
            managerStatus.className = 'status dim';
            addActivity('\u{1F4E4}', 'Sending capture to manager\u2026', '', 'act-info');
            const t0 = performance.now();
            chrome.runtime.sendMessage({ action: 'forceCapture' }, (r) => {
                const elapsed = performance.now() - t0;
                if (r && r.ok) {
                    const jid = (r.result && r.result.job_id) || '?';
                    managerStatus.textContent = 'Manager: job ' + jid + ' sent';
                    managerStatus.className = 'status ok';
                    addActivity('\u2713', 'Manager accepted capture',
                        `job ${jid}, ${fmtMs(elapsed)}`, 'act-ok');
                } else {
                    managerStatus.textContent = 'Manager: ' +
                        ((r && r.error) || 'failed');
                    managerStatus.className = 'status err';
                    addActivity('\u2717', 'Manager rejected capture',
                        (r && r.error) || 'unknown error', 'act-err');
                }
                ensureActivityVisible();
            });
        });
    }

    if (autoPost) {
        autoPost.addEventListener('change', () => {
            chrome.runtime.sendMessage({
                action: 'setManagerConfig',
                config: { autoPost: autoPost.checked }
            });
        });
    }

    refreshManager();
    setInterval(refreshManager, 2000);

    // Listen for storage changes
    chrome.storage.onChanged.addListener((changes, area) => {
        if (area === 'local') {
            if (changes.lastCapture) {
                const cap = changes.lastCapture.newValue;
                if (cap && cap.cookie) {
                    addActivity('\u{1F4E5}', 'New capture detected',
                        `cookie ${(cap.cookie||'').length} chars, ` +
                        shortFilename(cap.url || ''), 'act-ok');
                    ensureActivityVisible();
                }
                refresh();
            }
            if (changes.captureCount) {
                refresh();
            }
            if (changes.lastPostStatus) {
                const ps = changes.lastPostStatus.newValue;
                if (ps) {
                    if (ps.ok) {
                        addActivity('\u2713', 'Auto-POST to manager succeeded',
                            `job ${ps.jobId || '?'}`, 'act-ok');
                    } else {
                        addActivity('\u26a0', 'Auto-POST to manager failed',
                            ps.error || 'unknown', 'act-warn');
                    }
                    ensureActivityVisible();
                }
            }
            if (changes.lastV2PostStatus) {
                const ps = changes.lastV2PostStatus.newValue;
                if (ps) {
                    if (ps.ok) {
                        addActivity('\u2713', 'v2 capture POSTed to manager',
                            `archive ${(ps.archive_id || '?').slice(0, 8)}, parts ${ps.parts === null || ps.parts === undefined ? '?' : ps.parts}`,
                            'act-ok');
                    } else {
                        addActivity('\u26a0', 'v2 capture POST failed',
                            ps.error || 'unknown', 'act-warn');
                    }
                    ensureActivityVisible();
                }
            }
        }
    });

    // Refresh every 2s while popup is open
    setInterval(refresh, 2000);

    // -----------------------------------------------------------------------
    // v4.1 live status panel (with activity log entries for status changes)
    // -----------------------------------------------------------------------
    const sp = {
        panel: document.getElementById("statusPanel"),
        head: document.getElementById("jobHead"),
        barFill: document.getElementById("jobBarFill"),
        meta: document.getElementById("jobMeta"),
        heartbeat: document.getElementById("jobHeartbeat"),
        err: document.getElementById("jobErr"),
        partsSummary: document.getElementById("partsSummary"),
        partsList: document.getElementById("partsList"),
        log: document.getElementById("jobLog")
    };

    let _prevStatus = null;
    let _prevActiveCount = -1;
    let _prevDoneCount = -1;

    function statusColor(st) {
        if (st === 'complete') return 'ok';
        if (st === 'error') return 'err';
        if (st === 'needs_cookie' || st === 'paused') return 'warn';
        return 'dim';
    }

    function renderStatusPanel(resp) {
        if (!resp || !resp.ok || !resp.jobs || resp.jobs.length === 0) {
            if (sp.panel) sp.panel.style.display = 'none';
            return;
        }
        sp.panel.style.display = 'block';
        const job = resp.jobs[0];
        const d = resp.detail || job;
        const t = d.totals || job.totals || {};
        const st = d.status || job.status || '?';
        const pct = t.bytes_total ? Math.min(100, (t.bytes_done / t.bytes_total) * 100) : 0;

        // Status change => activity log entry
        if (_prevStatus !== null && _prevStatus !== st) {
            const icon = st === 'complete' ? '\u2713' :
                         st === 'error' ? '\u2717' :
                         st === 'needs_cookie' ? '\u{1F511}' :
                         st === 'downloading' ? '\u25BC' :
                         st === 'discovering' ? '\u{1F50D}' :
                         '\u2022';
            const cls = st === 'complete' ? 'act-ok' :
                        st === 'error' ? 'act-err' :
                        st === 'needs_cookie' ? 'act-warn' :
                        'act-info';
            addActivity(icon, `Job status: ${_prevStatus} \u2192 ${st}`,
                `job ${d.job_id || job.job_id || '?'}`, cls);
            ensureActivityVisible();
        }
        _prevStatus = st;

        // Part progress change => activity log (only on significant changes)
        const parts = d.parts || [];
        const activeCount = parts.filter(p => p.status === 'active').length;
        const doneCount = parts.filter(p => p.status === 'done').length;
        if (_prevDoneCount !== -1 && doneCount > _prevDoneCount) {
            const newDone = doneCount - _prevDoneCount;
            if (newDone > 0 && parts.length > 0) {
                // Find which parts just completed
                const justDone = parts.filter(p => p.status === 'done' && p.size > 0);
                if (justDone.length <= 3) {  // only log if few to avoid spam
                    for (const p of justDone.slice(-newDone)) {
                        const label = p.filename
                            ? (p.filename.length > 28 ? p.filename.slice(-28) : p.filename)
                            : `part ${p.index}`;
                        addActivity('\u2713', `Download complete: ${label}`,
                            `${fmtBytes(p.size || p.done)}`, 'act-ok');
                    }
                } else {
                    addActivity('\u2713', `${newDone} part(s) completed`,
                        `${doneCount}/${parts.length} total done`, 'act-ok');
                }
                ensureActivityVisible();
            }
        }
        _prevDoneCount = doneCount;
        if (activeCount !== _prevActiveCount && _prevActiveCount >= 0) {
            if (activeCount > _prevActiveCount) {
                addActivity('\u25BC', `${activeCount - _prevActiveCount} download(s) started`,
                    `${activeCount} active now`, 'act-info');
                ensureActivityVisible();
            }
        }
        _prevActiveCount = activeCount;

        // Render header
        sp.head.textContent = (d.workflow || job.workflow || 'job') + ' \u2014 ' + st;
        sp.head.className = 'status ' + statusColor(st);
        sp.barFill.style.width = pct.toFixed(1) + '%';
        sp.barFill.style.background = st === 'error' ? '#ef4444' :
            (st === 'complete' ? '#22c55e' : '#a855f7');

        const spd = t.speed_bps ? (' \u2022 ' + fmtBytes(t.speed_bps) + '/s') : '';
        sp.meta.textContent = (t.parts_done || 0) + '/' + (t.parts_total || 0) +
            ' parts \u2022 ' + fmtBytes(t.bytes_done) + ' / ' + fmtBytes(t.bytes_total) +
            ' (' + pct.toFixed(1) + '%)' + spd;

        // Heartbeat
        const upd = d.updated_at || job.updated_at;
        if (upd) {
            const age = Math.max(0, Math.round((Date.now() - Date.parse(upd)) / 1000));
            const stale = age > 30;
            sp.heartbeat.textContent = '\u2665 last update ' +
                (age < 60 ? age + 's' : Math.round(age / 60) + 'm') + ' ago' +
                (stale && (st === 'downloading') ? ' \u26a0 stalled?' : '');
            sp.heartbeat.style.color = (stale && st === 'downloading') ? '#f59e0b' : '#94a3b8';
        } else {
            sp.heartbeat.textContent = '';
        }

        // Error
        const errMsg = d.last_error || job.last_error;
        if (errMsg) {
            sp.err.style.display = 'block';
            sp.err.textContent = '\u26a0 ' + errMsg;
        } else {
            sp.err.style.display = 'none';
        }

        // Per-part list
        if (parts.length) {
            const done = parts.filter(p => p.status === 'done').length;
            const active = parts.filter(p => p.status === 'active').length;
            const auth = parts.filter(p => p.status === 'auth').length;
            const queued = parts.filter(p => p.status === 'queued').length;
            const errs = parts.filter(p => p.status === 'error').length;
            let sumText = '(' + done + ' done';
            if (active) sumText += ', ' + active + ' active';
            if (queued) sumText += ', ' + queued + ' queued';
            if (errs) sumText += ', ' + errs + ' failed';
            if (auth) sumText += ', ' + auth + ' auth';
            sumText += ')';
            sp.partsSummary.textContent = sumText;

            sp.partsList.innerHTML = parts.map(p => {
                const ppct = p.size ? Math.min(100, (p.done / p.size) * 100) : 0;
                const icon = p.status === 'done' ? '\u2713' :
                    (p.status === 'active' ? '\u25BC' :
                    (p.status === 'auth' ? '\u{1F511}' :
                    (p.status === 'error' ? '\u2717' : '\u00B7')));
                const col = p.status === 'done' ? '#22c55e' :
                    (p.status === 'active' ? '#a855f7' :
                    (p.status === 'error' || p.status === 'auth' ? '#f59e0b' : '#64748b'));
                const sp2 = p.speed ? (' ' + fmtBytes(p.speed) + '/s') : '';
                const err = p.error ? (' \u26a0 ' + p.error) : '';
                return '<div style="color:' + col + '">' + icon +
                    ' i=' + String(p.index).padStart(2, '0') + ' ' +
                    ppct.toFixed(0).padStart(3) + '% ' +
                    fmtBytes(p.done) + '/' + fmtBytes(p.size) + sp2 + err +
                    '</div>';
            }).join('');
        }

        // Log tail
        if (resp.log) {
            sp.log.textContent = resp.log;
            sp.log.scrollTop = sp.log.scrollHeight;
        }
    }

    function pollStatus() {
        chrome.runtime.sendMessage({ action: 'getManagerJobs' }, (resp) => {
            if (chrome.runtime.lastError) return;
            renderStatusPanel(resp);
        });
    }

    pollStatus();
    setInterval(pollStatus, 2000);

    // Reset tracker when popup reopens (storage change signals a new session)
    chrome.storage.onChanged.addListener((changes, area) => {
        if (area === 'local' && changes.lastCapture) {
            _prevStatus = null;
            _prevDoneCount = -1;
            _prevActiveCount = -1;
        }
    });
});

// ---------------------------------------------------------------------------
// v4.3 — v2 LIVE PER-PART MONITOR  (docs/v2/06-LIVE-MONITORING.md §4 & §6)
// Consumes the v2 control plane at {managerUrl}/api/v2 (mounted by
// manager/v2integration.py):
//   GET  /jobs                         -> job list / auto-select / <select>
//   GET  /jobs/{id}?parts=1            -> seed rows (sizes, statuses, verify)
//   GET  /jobs/{id}/events?since=<seq> -> SSE: part_progress / part_done /
//                                          part_error / job_status /
//                                          attempt_spent / heartbeat
//   GET  /jobs/{id}/budget             -> parts_at_risk (header "budget ⚠ N")
// Additive only: the v1 status panel, the budget panel and the activity log
// stay untouched. Nothing here touches background.js / content.js / manifest.
// ---------------------------------------------------------------------------
const v2LiveMonitor = (function () {
    'use strict';

    const TERMINAL = new Set(['DONE', 'FAILED', 'SKIPPED', 'BUDGET_EXHAUSTED']);
    const MAX_ROWS = 50;                 // cap rendered rows for huge jobs
    const PART_STALL_MS = 90000;         // per-part: no part_progress in 90 s
    const GLOBAL_STALL_MS = 60000;       // global: no event of any kind in 60 s
    const SSE_MAX_FAILS = 2;             // then fall back to 2 s polling
    const CONN_COLORS = { ok: '#22c55e', warn: '#f59e0b', err: '#ef4444', dim: '#94a3b8' };

    let cfg = { mgrUrl: null, token: '' };
    let jobs = [];
    let archiveId = null;
    let rows = new Map();                // idx -> row
    let totals = {};
    let headJob = null;                  // last job snapshot
    let lastSeq = 0;                     // SSE cursor (the `id:` line)
    let lastEventAt = 0;                 // any-event clock (global stall)
    let sse = null;
    let sseFails = 0;
    let mode = 'idle';                   // idle | sse | polling
    let pollTimer = null;
    let stallTimer = null;
    let listTimer = null;
    let budgetTimer = null;
    let retryTimer = null;
    let retries = 0;
    let partsAtRisk = 0;
    let connText = '\u2026';
    let connCls = 'dim';
    let started = false;

    // -- tiny helpers --------------------------------------------------------
    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }
    function v2Url(path) {
        const base = cfg.mgrUrl || 'http://127.0.0.1:8080';
        return base + '/api/v2' + path;
    }
    function fetchV2Json(path, opts) {
        opts = opts || {};
        const headers = Object.assign({}, opts.headers || {});
        if (cfg.token) headers['X-Capture-Token'] = cfg.token;
        return fetch(v2Url(path), { headers: headers }).then(function (r) {
            if (!r.ok) throw new Error('HTTP ' + r.status + ' ' + path);
            return r.json();
        });
    }
    function fmtSpeed(bps) {
        if (!bps || bps <= 0) return '\u2014';
        return fmtBytes(bps) + '/s';
    }
    function fmtEta(sec) {
        if (sec == null || !isFinite(sec) || sec < 0) return '';
        if (sec < 60) return '~' + Math.round(sec) + 's';
        if (sec < 3600) return '~' + Math.round(sec / 60) + 'm';
        return '~' + Math.floor(sec / 3600) + 'h ' + Math.round((sec % 3600) / 60) + 'm';
    }
    function isTerminal(row) {
        return !!(row.done || (row.status && TERMINAL.has(row.status)));
    }
    function rowState(row) {
        return row.lastError ? 'ERROR' : (row.status || '?');
    }
    function rowPct(row) {
        if (row.done) return 100;
        if (!row.sizeExpected || row.sizeExpected <= 0) return null;
        return Math.min(100, ((row.sizeOnDisk || 0) / row.sizeExpected) * 100);
    }
    function scheduleRetry(fn, ms) {
        if (retryTimer) clearTimeout(retryTimer);
        retryTimer = setTimeout(fn, ms);
    }
    function setConn(text, cls) {
        connText = text;
        connCls = cls || 'dim';
        renderV2Conn();
    }
    function showLiveSection(show) {
        const el = $('v2LiveSection');
        if (el) el.style.display = show ? 'block' : 'none';
    }
    function v2StatusClass(st) {
        if (st === 'COMPLETE') return 'ok';
        if (st === 'FAILED' || st === 'BUDGET_EXHAUSTED') return 'err';
        if (st === 'NEEDS_COOKIE' || st === 'PAUSED') return 'warn';
        return 'dim';
    }

    // -- rows ----------------------------------------------------------------
    function ensureRow(idx) {
        const row = {
            idx: idx,
            filename: 'part ' + idx,
            status: 'PENDING',
            verify: 'UNVERIFIED',
            sizeExpected: null,
            sizeOnDisk: null,
            attemptsUsed: 0,
            attemptsLeft: null,
            speedBps: null,
            lastError: null,
            lastProgressAt: null,
            isStalled: false,
            stallSecs: 0,
            done: false
        };
        rows.set(idx, row);
        return row;
    }
    function mergeSnapshot(snap) {
        // Reconcile rows from a poll snapshot; keeps speedBps when available
        // and computes a crude delta speed between 2 s polls when it grew.
        const now = Date.now();
        (snap.parts || []).forEach(function (p) {
            const prev = rows.get(p.idx);
            const done = p.status === 'DONE';
            const grew = prev && prev.sizeOnDisk != null &&
                         (p.size_on_disk || 0) > prev.sizeOnDisk;
            const inFlight = p.status === 'ACTIVE' || p.status === 'PARTIAL' ||
                             p.status === 'VERIFYING';
            rows.set(p.idx, {
                idx: p.idx,
                filename: p.filename || (prev ? prev.filename : 'part ' + p.idx),
                status: p.status || (prev ? prev.status : 'PENDING'),
                verify: p.verify_state || (prev ? prev.verify : 'UNVERIFIED'),
                sizeExpected: (p.size_expected != null && p.size_expected > 0)
                    ? p.size_expected : (prev ? prev.sizeExpected : null),
                sizeOnDisk: p.size_on_disk != null
                    ? p.size_on_disk : (prev ? prev.sizeOnDisk : null),
                attemptsUsed: p.attempts_used != null
                    ? p.attempts_used : (prev ? prev.attemptsUsed : 0),
                attemptsLeft: prev ? prev.attemptsLeft : null,
                speedBps: grew
                    ? Math.round((p.size_on_disk - prev.sizeOnDisk) / 2)
                    : (prev ? prev.speedBps : null),
                lastError: p.last_error || (prev ? prev.lastError : null),
                lastProgressAt: (grew || done)
                    ? now : (prev ? prev.lastProgressAt : (inFlight ? now : null)),
                // Preserve the stall state when the part didn't grow this poll,
                // so the "⏳ stalled Ns" marker doesn't flicker on each refresh.
                isStalled: (grew || done) ? false : (prev ? prev.isStalled : false),
                stallSecs: prev ? prev.stallSecs : 0,
                done: done
            });
        });
    }

    // -- SSE event handlers ---------------------------------------------------
    function handleV2Event(e, kind) {
        lastEventAt = Date.now();            // any event resets the global stall clock
        const id = parseInt(e.lastEventId, 10);
        if (!isNaN(id) && id > lastSeq) lastSeq = id;   // lossless cursor
        let ev = null;
        try { ev = JSON.parse(e.data); } catch (err) { /* non-JSON: ignore */ }
        if (ev && typeof ev.seq === 'number' && ev.seq > lastSeq) lastSeq = ev.seq;
        const k = kind || (ev && ev.kind) || 'message';
        const d = (ev && ev.data) || {};
        switch (k) {
            case 'part_progress': applyPartProgress(d); break;
            case 'part_done':     applyPartDone(d); break;
            case 'part_error':    applyPartError(d); break;
            case 'part_update':   applyPartUpdate(d); break;   // current engine emits this
            case 'part_state':    applyPartState(d); break;    // current engine emits this
            case 'job_status':    applyJobStatus(d); break;
            case 'attempt_spent': applyAttemptSpent(d); break;
            default: break;   // heartbeat + unknown kinds: only timers bumped
        }
        renderV2Conn();
    }
    function applyPartProgress(d) {
        const row = rows.get(d.idx) || ensureRow(d.idx);
        if (typeof d.size_on_disk === 'number' && d.size_on_disk >= 0) row.sizeOnDisk = d.size_on_disk;
        if (typeof d.size_expected === 'number' && d.size_expected > 0) row.sizeExpected = d.size_expected;
        if (typeof d.speed_bps === 'number') row.speedBps = d.speed_bps;
        if (d.state) row.status = String(d.state).toUpperCase();
        if (d.verify) row.verify = d.verify;
        if (d.error != null) row.lastError = d.error;
        row.done = row.status === 'DONE';
        row.isStalled = false;
        row.stallSecs = 0;
        row.lastProgressAt = Date.now();     // the per-part stall clock (90 s)
        if (row.done) { row.speedBps = null; row.attemptsLeft = null; }
        patchV2Row(d.idx);
    }
    function applyPartDone(d) {
        const row = rows.get(d.idx) || ensureRow(d.idx);
        row.status = d.state || 'DONE';
        row.done = true;
        if (typeof d.size_expected === 'number' && d.size_expected > 0) row.sizeExpected = d.size_expected;
        if (typeof d.size_on_disk === 'number' && d.size_on_disk >= 0) row.sizeOnDisk = d.size_on_disk;
        else if (row.sizeExpected != null) row.sizeOnDisk = row.sizeExpected;
        if (d.verify) row.verify = d.verify;
        row.speedBps = null;
        row.lastError = null;
        row.attemptsLeft = null;
        row.isStalled = false;
        row.lastProgressAt = Date.now();
        patchV2Row(d.idx);
        fetchBudget();                       // attempts freed — refresh the money view
    }
    function applyPartError(d) {
        const row = rows.get(d.idx) || ensureRow(d.idx);
        row.status = d.state ? String(d.state).toUpperCase() : 'FAILED';
        if (typeof d.attempts_left === 'number') row.attemptsLeft = d.attempts_left;
        if (row.attemptsLeft === 0) row.status = 'BUDGET_EXHAUSTED';
        row.lastError = d.error || (d.outcome || 'ERROR');
        row.speedBps = null;
        row.done = false;
        row.lastProgressAt = Date.now();
        patchV2Row(d.idx);
        fetchBudget();
        addActivity('\u2717', 'Part ' + d.idx + ' failed',
            (d.error || d.outcome || '') +
            (row.attemptsLeft != null ? ' \u00b7 ' + row.attemptsLeft + ' attempts left' : ''),
            'act-err');
        ensureActivityVisible();
    }
    function applyPartUpdate(d) {
        // Defensive: the CURRENT engine emits part_update (status/size/verify
        // changes) instead of the normative part_done; treat DONE as terminal.
        const row = rows.get(d.idx) || ensureRow(d.idx);
        let changed = false;
        if (d.status) { row.status = String(d.status).toUpperCase(); changed = true; }
        if (d.verify_state) { row.verify = d.verify_state; changed = true; }
        if (typeof d.size_on_disk === 'number') { row.sizeOnDisk = d.size_on_disk; changed = true; }
        if (d.error != null) { row.lastError = d.error; changed = true; }
        if (changed) {
            row.done = row.status === 'DONE';
            if (row.done) { row.speedBps = null; row.attemptsLeft = null; }
            row.isStalled = false;
            row.lastProgressAt = Date.now();
            patchV2Row(d.idx);
            if (row.done) fetchBudget();
        }
    }
    function applyPartState(d) {
        if (d.idx == null || !d.state) return;
        const row = rows.get(d.idx) || ensureRow(d.idx);
        row.status = String(d.state).toUpperCase();
        row.done = row.status === 'DONE';
        patchV2Row(d.idx);
    }
    function applyJobStatus(d) {
        if (headJob) {
            if (d.status) headJob.status = d.status;
            if (d.error != null) headJob.last_error = d.error;
        }
        renderV2Header();
        if (d.status === 'COMPLETE' || d.status === 'FAILED' || d.status === 'BUDGET_EXHAUSTED') {
            addActivity(d.status === 'COMPLETE' ? '\u2713' : '\u26a0',
                'v2 job ' + d.status.toLowerCase(),
                d.error || '', d.status === 'COMPLETE' ? 'act-ok' : 'act-warn');
            ensureActivityVisible();
        }
    }
    function applyAttemptSpent() {
        if (budgetTimer) clearTimeout(budgetTimer);
        budgetTimer = setTimeout(fetchBudget, 500);
    }

    // -- rendering ------------------------------------------------------------
    function v2RowClass(row) {
        if (row.done) return 'v2-done';
        if (row.lastError || row.status === 'FAILED' || row.status === 'BUDGET_EXHAUSTED') return 'v2-err';
        if (row.isStalled) return 'v2-stall';
        if (row.status === 'ACTIVE' || row.status === 'PARTIAL' || row.status === 'VERIFYING') return 'v2-active';
        return 'v2-pending';
    }
    function v2RowHtml(row) {
        const pct = rowPct(row);
        const bar = pct == null
            ? '<span class="v2-bar-none">\u2014</span>'
            : '<span class="v2-bar-track"><span class="v2-bar-fill" style="width:' +
              pct.toFixed(1) + '%"></span></span>';
        const disk = row.sizeExpected != null
            ? fmtBytes(row.sizeOnDisk || 0) + '/' + fmtBytes(row.sizeExpected)
            : (row.sizeOnDisk != null ? fmtBytes(row.sizeOnDisk) + '/?' : '\u2014');
        const speed = row.done
            ? '\u2014'
            : (row.isStalled
                ? '\u23F3 stalled ' + (row.stallSecs || 0) + 's'
                : fmtSpeed(row.speedBps));
        let err = '';
        if (row.done) err = '\u2713';
        else if (row.lastError) {
            err = '\u2716 ' + esc(row.lastError);
            if (row.attemptsLeft != null) err += ' \u00b7 ' + row.attemptsLeft + ' attempts left';
        }
        return '<div class="v2-row ' + v2RowClass(row) + '" data-idx="' + row.idx +
            '" title="' + esc(row.filename || 'part ' + row.idx) + '">' +
            '<span class="v2c-idx">' + String(row.idx).padStart(2, '0') + '</span>' +
            '<span class="v2c-state">' + esc(rowState(row)) + '</span>' +
            '<span class="v2c-bar">' + bar + '</span>' +
            '<span class="v2c-pct">' + (pct == null ? '\u2014' : pct.toFixed(0) + '%') + '</span>' +
            '<span class="v2c-disk">' + disk + '</span>' +
            '<span class="v2c-speed">' + speed + '</span>' +
            '<span class="v2c-err">' + err + '</span>' +
            '</div>';
    }
    function renderV2Summary() {
        const sum = $('v2PartsSummary');
        if (!sum) return;
        if (rows.size === 0) { sum.textContent = ''; return; }
        let done = 0, errs = 0;
        rows.forEach(function (r) { if (r.done) done++; if (r.lastError) errs++; });
        sum.textContent = '(' + done + '/' + rows.size + ' done' +
            (errs ? ', ' + errs + ' error' + (errs > 1 ? 's' : '') : '') + ')';
    }
    function renderV2Table() {
        const wrap = $('v2PartsTable');
        const body = $('v2PartsBody');
        if (!wrap || !body) return;
        const st = wrap.scrollTop;
        const sorted = Array.from(rows.values()).sort(function (a, b) { return a.idx - b.idx; });
        if (sorted.length === 0) {
            body.innerHTML = '<div class="v2-more">no parts yet (discovering)\u2026</div>';
        } else {
            const shown = sorted.slice(0, MAX_ROWS);
            body.innerHTML = shown.map(v2RowHtml).join('') +
                (sorted.length > MAX_ROWS
                    ? '<div class="v2-more">\u2026 (' + (sorted.length - MAX_ROWS) +
                      ' more parts hidden)</div>'
                    : '');
        }
        wrap.scrollTop = st;
        renderV2Summary();
    }
    function patchV2Row(idx) {
        const body = $('v2PartsBody');
        const row = rows.get(idx);
        if (!body || !row) return;
        const el = body.querySelector('.v2-row[data-idx="' + idx + '"]');
        if (el) {
            el.outerHTML = v2RowHtml(row);
        } else {
            renderV2Table();   // new part appeared (or beyond cap) — rebuild
        }
        renderV2Summary();
        renderV2Header();
    }
    function renderV2Header() {
        const el = $('v2LiveHead');
        const meta = $('v2LiveMeta');
        const barFill = $('v2LiveBarFill');
        if (!el || !meta) return;
        if (!headJob) { el.textContent = ''; meta.textContent = ''; return; }
        const label = headJob.label || headJob.archive_id || '?';
        const ts = headJob.export_ts || 'unknown';
        let doneCount = 0, bytesDone = 0, bytesTotal = 0, agg = 0;
        rows.forEach(function (r) {
            if (r.done) doneCount++;
            if (r.sizeOnDisk != null) bytesDone += r.sizeOnDisk;
            if (r.sizeExpected != null) bytesTotal += r.sizeExpected;
            if (r.speedBps && r.speedBps > 0) agg += r.speedBps;
        });
        if (rows.size === 0 && totals.parts_total) {
            doneCount = totals.parts_done || 0;
            bytesDone = totals.bytes_done || 0;
            bytesTotal = totals.bytes_total || 0;
        }
        const totalParts = rows.size || totals.parts_total || 0;
        const pct = bytesTotal > 0 ? (bytesDone / bytesTotal) * 100 : 0;
        const remaining = Math.max(0, bytesTotal - bytesDone);
        const eta = agg > 0 && remaining > 0 ? fmtEta(remaining / agg) : '';
        const st = headJob.status || '?';
        const bits = [
            doneCount + '/' + totalParts + ' done',
            (bytesTotal > 0 ? pct.toFixed(1) : '?') + '%',
            agg > 0 ? fmtSpeed(agg) + ' agg' : '0 B/s agg'
        ];
        if (eta) bits.push(eta);
        if (partsAtRisk > 0) bits.push('budget \u26a0 ' + partsAtRisk);
        el.textContent = 'takeout2 \u00b7 ' + label + ' / ' + ts;
        meta.innerHTML = '<span class="pill ' + v2StatusClass(st) + '">' + esc(st) +
            '</span> ' + bits.join(' \u00b7 ');
        if (barFill) {
            barFill.style.width = (bytesTotal > 0 ? Math.min(100, pct) : 0).toFixed(1) + '%';
            barFill.style.background =
                st === 'COMPLETE' ? '#22c55e' : (st === 'FAILED' ? '#ef4444' : '#a855f7');
        }
    }
    function renderV2Conn() {
        const el = $('v2LiveConn');
        if (!el) return;
        const age = lastEventAt
            ? Math.max(0, Math.round((Date.now() - lastEventAt) / 1000)) : null;
        el.textContent = connText +
            (age != null ? ' \u00b7 last event ' + age + 's ago' : '');
        el.style.color = CONN_COLORS[connCls] || '#94a3b8';
    }
    function renderSelector() {
        const sel = $('v2JobSelect');
        const wrap = $('v2JobSelector');
        if (!sel || !wrap) return;
        if (jobs.length <= 1) {
            wrap.style.display = 'none';
            return;
        }
        wrap.style.display = 'block';
        sel.innerHTML = jobs.map(function (j) {
            const label = j.label || j.archive_id || '?';
            return '<option value="' + esc(j.archive_id) + '"' +
                (j.archive_id === archiveId ? ' selected' : '') + '>' +
                esc(label + ' \u00b7 ' + (j.status || '?') + ' \u00b7 ' +
                    (j.parts_done || 0) + '/' + (j.parts_total || 0)) + '</option>';
        }).join('');
    }

    // -- lifecycle ------------------------------------------------------------
    function init() {
        if (started) return;
        started = true;
        const sel = $('v2JobSelect');
        if (sel) sel.addEventListener('change', function () { selectJob(sel.value); });
        startListRefresh();
        // Resolve manager config the SAME way refreshManager() does: ask the
        // background for getState, read managerUrl + captureToken from it.
        chrome.runtime.sendMessage({ action: 'getState' }, function (st) {
            if (chrome.runtime.lastError || !st) {
                showLiveSection(true);
                setConn('extension state unavailable', 'err');
                return;
            }
            cfg.mgrUrl = String(st.managerUrl || 'http://127.0.0.1:8080').replace(/\/+$/, '');
            cfg.token = st.captureToken || '';
            // Wire the live-monitor link to the same resolved manager URL.
            var mon = document.getElementById('openMonitorBtn');
            if (mon) {
                mon.addEventListener('click', function (ev) {
                    ev.preventDefault();
                    chrome.tabs.create({ url: cfg.mgrUrl + '/ui/monitor.html' });
                });
            }
            loadJobList();
        });
    }
    function loadJobList() {
        fetchV2Json('/jobs?limit=50').then(function (data) {
            retries = 0;
            jobs = (data && data.jobs) || [];
            if (jobs.length === 0) {
                showLiveSection(true);
                setConn('No v2 jobs yet \u2014 start a download.', 'dim');
                renderSelector();
                return;
            }
            if (archiveId && jobs.some(function (j) { return j.archive_id === archiveId; })) {
                renderSelector();          // keep the current selection
                return;
            }
            if (jobs.length === 1) {
                selectJob(jobs[0].archive_id);            // auto-select the single job
            } else {
                // Multiple jobs: the <select> is shown; auto-select the first so
                // the monitor is live immediately (the operator can switch).
                renderSelector();
                selectJob(jobs[0].archive_id);
            }
        }).catch(function (err) {
            retries++;
            showLiveSection(true);
            setConn('v2 manager unreachable (' + err.message + ')' +
                (retries < 12 ? ' \u2014 retrying\u2026' : ''), 'err');
            if (retries < 12) scheduleRetry(loadJobList, 5000);
            else stopStreaming();
        });
    }
    function refreshList() {
        fetchV2Json('/jobs?limit=50').then(function (data) {
            jobs = (data && data.jobs) || [];
            if (archiveId == null) {
                if (jobs.length > 0) selectJob(jobs[0].archive_id);
                else setConn('No v2 jobs yet \u2014 start a download.', 'dim');
                return;
            }
            renderSelector();
        }).catch(function () { /* manager down; keep current state */ });
    }
    function startListRefresh() {
        if (listTimer) clearInterval(listTimer);
        listTimer = setInterval(refreshList, 30000);
    }
    function selectJob(id) {
        if (archiveId === id && mode !== 'idle') return;   // already watching it
        stopStreaming();
        archiveId = id;
        lastSeq = 0;
        lastEventAt = 0;
        sseFails = 0;
        partsAtRisk = 0;
        rows = new Map();
        totals = {};
        headJob = null;
        showLiveSection(true);
        renderSelector();
        setConn('loading\u2026', 'dim');
        seedJob();
        fetchBudget();
    }
    function stopStreaming() {
        if (sse) { try { sse.close(); } catch (e) {} sse = null; }
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        if (stallTimer) { clearInterval(stallTimer); stallTimer = null; }
        if (budgetTimer) { clearTimeout(budgetTimer); budgetTimer = null; }
        if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
        mode = 'idle';
    }
    function seedJob() {
        fetchV2Json('/jobs/' + encodeURIComponent(archiveId) + '?parts=1')
            .then(function (snap) {
                if (!snap || snap.archive_id !== archiveId) return;
                headJob = snap;
                totals = snap;
                rows = new Map();
                const now = Date.now();
                (snap.parts || []).forEach(function (p) {
                    const done = p.status === 'DONE';
                    const inFlight = p.status === 'ACTIVE' || p.status === 'PARTIAL' ||
                                     p.status === 'VERIFYING';
                    rows.set(p.idx, {
                        idx: p.idx,
                        filename: p.filename || ('part ' + p.idx),
                        status: p.status || 'PENDING',
                        verify: p.verify_state || 'UNVERIFIED',
                        sizeExpected: (p.size_expected != null && p.size_expected > 0)
                            ? p.size_expected : null,
                        sizeOnDisk: p.size_on_disk != null ? p.size_on_disk : null,
                        attemptsUsed: p.attempts_used || 0,
                        attemptsLeft: null,
                        speedBps: null,
                        lastError: p.last_error || null,
                        lastProgressAt: (done || !inFlight) ? null : now,
                        isStalled: false,
                        stallSecs: 0,
                        done: done
                    });
                });
                renderV2Header();
                renderV2Table();
                connectV2SSE();
            })
            .catch(function (err) {
                setConn('load failed: ' + err.message + ' \u2014 retrying\u2026', 'err');
                scheduleRetry(seedJob, 3000);
            });
    }

    // -- SSE + fallback --------------------------------------------------------
    function connectV2SSE() {
        if (!archiveId) return;
        if (sse) { try { sse.close(); } catch (e) {} sse = null; }
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        mode = 'sse';
        setConn('connecting\u2026', 'dim');
        const url = v2Url('/jobs/' + encodeURIComponent(archiveId) +
                          '/events?since=' + lastSeq);
        let es;
        try {
            es = new EventSource(url);
        } catch (e) {
            sseFailed('EventSource error: ' + e.message);
            return;
        }
        sse = es;
        es.onopen = function () {
            sseFails = 0;              // a live connection resets the failure count
            lastEventAt = Date.now();
            setConn('\u25CF live', 'ok');
            startV2StallWatch();
        };
        ['part_progress', 'part_done', 'part_error', 'job_status',
         'attempt_spent', 'part_update', 'part_state', 'heartbeat']
            .forEach(function (kind) {
                es.addEventListener(kind, function (e) { handleV2Event(e, kind); });
            });
        es.onmessage = function (e) { handleV2Event(e, null); };   // safety net
        es.onerror = function () {
            // EventSource would auto-reconnect to the STALE URL; close it and
            // reconnect ourselves with a fresh ?since=<lastSeq> (lossless).
            try { es.close(); } catch (e) {}
            if (sse === es) sse = null;
            sseFailed('stream dropped');
        };
    }
    function sseFailed(reason) {
        if (mode === 'polling') return;
        mode = 'idle';
        sseFails++;
        lastEventAt = 0;
        setConn('stream error (' + reason + ') \u2014 reconnect ' + sseFails + '/2', 'warn');
        if (sseFails >= SSE_MAX_FAILS) {
            startPollFallback();
            return;
        }
        scheduleRetry(function () { connectV2SSE(); }, 2000);
    }
    function startPollFallback() {
        mode = 'polling';
        sseFails = 0;
        setConn('\u25CD polling every 2s', 'warn');
        pollOnce();
        pollTimer = setInterval(pollOnce, 2000);
        startV2StallWatch();
    }
    function pollOnce() {
        if (!archiveId || mode !== 'polling') return;
        fetchV2Json('/jobs/' + encodeURIComponent(archiveId) + '?parts=1')
            .then(function (snap) {
                if (!snap || snap.archive_id !== archiveId) return;
                lastEventAt = Date.now();
                totals = snap;
                headJob = snap;
                mergeSnapshot(snap);
                renderV2Header();
                renderV2Table();
                renderV2Conn();
            })
            .catch(function (err) {
                setConn('poll failed: ' + err.message, 'err');
            });
    }
    function fetchBudget() {
        if (!archiveId) return;
        fetchV2Json('/jobs/' + encodeURIComponent(archiveId) + '/budget')
            .then(function (b) {
                if (b && typeof b.parts_at_risk === 'number') {
                    partsAtRisk = b.parts_at_risk;
                    renderV2Header();
                }
            })
            .catch(function () { /* budget is best-effort */ });
    }

    // -- stall detection -------------------------------------------------------
    function startV2StallWatch() {
        if (stallTimer) clearInterval(stallTimer);
        stallTimer = setInterval(v2StallTick, 2000);
    }
    function v2StallTick() {
        const now = Date.now();
        // Global: SSE open but no event of any kind for 60 s -> stale, reconnect.
        if (mode === 'sse' && sse && lastEventAt && (now - lastEventAt) > GLOBAL_STALL_MS) {
            setConn('\u26a0 stale \u2014 reconnect', 'warn');
            try { sse.close(); } catch (e) {}
            sse = null;
            sseFailed('stale (no event for 60s)');
            return;
        }
        const jobPaused = headJob && ['PAUSED', 'NEEDS_COOKIE', 'COMPLETE', 'FAILED',
                                      'BUDGET_EXHAUSTED'].indexOf(headJob.status) >= 0;
        const body = $('v2PartsBody');
        rows.forEach(function (row) {
            const was = row.isStalled;
            if (jobPaused || !row.lastProgressAt || isTerminal(row)) {
                row.isStalled = false;
                if (was && body) {
                    const el = body.querySelector('.v2-row[data-idx="' + row.idx + '"]');
                    if (el) { el.classList.remove('v2-stall'); patchV2Row(row.idx); }
                }
                return;
            }
            const age = now - row.lastProgressAt;
            row.isStalled = age > PART_STALL_MS;
            row.stallSecs = Math.floor(age / 1000);
            if (!body) return;
            const el = body.querySelector('.v2-row[data-idx="' + row.idx + '"]');
            if (!el) return;
            if (row.isStalled) {
                const sp = el.querySelector('.v2c-speed');
                if (sp) sp.textContent = '\u23F3 stalled ' + row.stallSecs + 's';
                if (!was) el.classList.add('v2-stall');
            } else if (was) {
                el.classList.remove('v2-stall');
                patchV2Row(row.idx);
            }
        });
        renderV2Conn();   // keep the "last event Ns ago" ticking
    }

    return { init: init };
})();

document.addEventListener('DOMContentLoaded', function () {
    v2LiveMonitor.init();
});
