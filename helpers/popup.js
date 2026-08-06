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
