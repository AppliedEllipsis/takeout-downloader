// Takeout Downloader Helper - Background Service Worker
// v2.0.0 — capture-only. The user copies JSON from the popup and pastes
// it into the TUI. No network calls leaving the browser.
//
// Why this design?
//   v1 sent captures to a localhost Flask server automatically. The TUI
//   has no server now (the web UI was removed). The user is the transport
//   layer: extension produces JSON, user copies to clipboard, user pastes
//   into the TUI. Simpler, fewer attack surface, works on any machine.

const SCHEMA_VERSION = 1;

// Patterns we capture from. We want the FINAL host in the redirect chain
// (takeout-download.usercontent.google.com) because that's where the
// session cookies are valid. We also accept takeout.google.com for the
// case where the user pauses the download before redirect completes,
// but flag those as "pre-redirect" so the popup can warn.
//
// See https://blog.omgmog.net/post/downloading-google-takeout-to-a-nas/
// for the full write-up of why pre-redirect captures fail.
const FINAL_HOST_PATTERNS = [
    'takeout-download.usercontent.google.com',
    'storage.cloud.google.com'
];

const ALL_URL_PATTERNS = FINAL_HOST_PATTERNS.map(h => `https://${h}/*`);

const DEFAULTS = {
    hasCapture: false,
    captureCount: 0,
    lastCapture: null,
    lastError: null,
    captureTimestamps: [],   // rolling buffer of recent capture times (for badge)
    // --- v4 manager settings (see docs/webgui/03-extension-v4.md) ---
    managerUrl: 'http://127.0.0.1:8080',
    captureToken: '',        // X-Capture-Token; empty => dev/open manager
    autoPost: true,          // POST captures to the manager automatically
    autoRecapture: true,     // re-capture when the manager asks (needs_cookie)
    accountEmail: null,      // best-effort, scraped from the Takeout page DOM
    lastPostStatus: null     // { ok, code, jobId, error, at } of the last POST
};

// v4: keys the manager client + popup may read/write via setManagerConfig.
const MANAGER_CONFIG_KEYS = ['managerUrl', 'captureToken', 'autoPost', 'autoRecapture'];

// Boot: merge defaults into storage
chrome.runtime.onInstalled.addListener(() => {
    chrome.storage.local.get(DEFAULTS, (existing) => {
        const merged = { ...DEFAULTS };
        for (const k of Object.keys(existing || {})) {
            if (k in DEFAULTS) merged[k] = existing[k];
        }
        chrome.storage.local.set(merged);
    });
});

// ---------------------------------------------------------------------------
// Capture: webRequest onBeforeSendHeaders
// ---------------------------------------------------------------------------
// Runs in the service worker for every matching request. We pull the Cookie
// header out of the request headers and persist the capture. We do NOT
// send it anywhere — that's the user's job (copy from popup, paste to TUI).
chrome.webRequest.onBeforeSendHeaders.addListener(
    (details) => {
        try {
            const capture = buildCapture(details);
            if (!capture) return;

            chrome.storage.local.get(['captureCount', 'captureTimestamps'], (data) => {
                const count = (data.captureCount || 0) + 1;
                const stamps = (data.captureTimestamps || []).concat([Date.now()]).slice(-50);
                chrome.storage.local.set({
                    lastCapture: capture,
                    hasCapture: true,
                    captureCount: count,
                    captureTimestamps: stamps,
                    lastError: null
                });
                updateBadgeAndIcon();
                const filename = extractFilename(capture.url);
                notifyCapture(filename);
                // v4: also POST to the manager (additive; clipboard path is
                // untouched). Failures fall back silently to clipboard use.
                maybeAutoPost(capture);
            });
        } catch (err) {
            chrome.storage.local.set({ lastError: err.message || String(err) });
        }
    },
    { urls: ALL_URL_PATTERNS },
    // 'extraHeaders' is REQUIRED in MV3 (Chrome 72+) to observe the Cookie
    // header. Without it Chrome strips Cookie/Referer/Authorization from
    // what onBeforeSendHeaders sees, so `cookie` comes back empty and the
    // popup reports "No cookie captured" even on a valid request.
    ['requestHeaders', 'extraHeaders']
);

function buildCapture(details) {
    const url = details.url || '';
    if (!url) return null;

    // Pick out the headers we care about
    const out = {};
    let cookie = '';
    for (const h of (details.requestHeaders || [])) {
        const name = (h.name || '').toLowerCase();
        const value = h.value || '';
        if (name === 'cookie') {
            cookie = value;
            continue;
        }
        if (name === 'user-agent') out['User-Agent'] = value;
        else if (name === 'accept') out['Accept'] = value;
        else if (name === 'accept-language') out['Accept-Language'] = value;
        else if (name === 'referer') out['Referer'] = value;
        else if (name === 'origin') out['Origin'] = value;
    }

    // Validate it's a takeout download URL on the final redirect host.
    // We intentionally do NOT capture takeout.google.com page requests here:
    // the cookie/header set sent to the page before the 302 redirect is not
    // the same as the one sent to takeout-download.usercontent.google.com,
    // and capturing it causes "pre-redirect" failures.
    const lower = url.toLowerCase();
    const isFinal = FINAL_HOST_PATTERNS.some(p => lower.includes(p));
    if (!isFinal) return null;

    // Only GET requests carry takeout downloads; skip anything weird
    if (details.method && details.method.toUpperCase() !== 'GET') return null;

    return {
        schema: SCHEMA_VERSION,
        captured_at: new Date().toISOString(),
        source: 'extension',
        url: url,
        method: 'GET',
        headers: out,
        cookie: cookie,
        pre_redirect: false
    };
}

// ---------------------------------------------------------------------------
// Badge + icon: shows capture freshness at a glance
// ---------------------------------------------------------------------------
const FRESH_CUTOFF = 300;   // 5 minutes — green badge
const STALE_CUTOFF = 600;   // 10 minutes — orange badge (Google cookies ~10 min)

function updateBadgeAndIcon() {
    chrome.storage.local.get(['hasCapture', 'lastCapture'], (data) => {
        if (!data.hasCapture || !data.lastCapture || !data.lastCapture.captured_at) {
            chrome.action.setBadgeText({ text: '' });
            return;
        }
        const age = (Date.now() - Date.parse(data.lastCapture.captured_at)) / 1000;
        if (age < FRESH_CUTOFF) {
            chrome.action.setBadgeText({ text: '✓' });
            chrome.action.setBadgeBackgroundColor({ color: '#22c55e' });
        } else if (age < STALE_CUTOFF) {
            chrome.action.setBadgeText({ text: '!' });
            chrome.action.setBadgeBackgroundColor({ color: '#f59e0b' });
        } else {
            chrome.action.setBadgeText({ text: '✗' });
            chrome.action.setBadgeBackgroundColor({ color: '#ef4444' });
        }
    });
}

// Notification ding when a fresh cookie is captured
function notifyCapture(filename) {
    try {
        chrome.notifications.create('takeout-' + Date.now(), {
            type: 'basic',
            iconUrl: 'icon48.png',
            title: 'Cookie captured!',
            message: filename
                ? `Click the extension → Copy ALL exports to save ${filename}`
                : 'Click the extension → Copy ALL exports',
            priority: 1,
            silent: false
        });
    } catch (e) {
        // notifications may fail in some contexts; non-critical
    }
}

// Staleness timer: update badge every 30s so the indicator reflects age
let staleTimer = null;
function startStaleTimer() {
    if (staleTimer) return;
    staleTimer = setInterval(updateBadgeAndIcon, 30000);
}
startStaleTimer();

function extractFilename(url) {
    try {
        const tail = url.split('?')[0].split('/').pop() || '';
        return tail.replace(/\.\w+$/, '').slice(-8);
    } catch (e) {
        return '';
    }
}

// ---------------------------------------------------------------------------
// v4 manager client (additive — never touches the capture listener above)
// ---------------------------------------------------------------------------
// On a successful capture we ALSO POST the payload to the local manager so it
// can drive the download with no human paste. The clipboard path is untouched;
// if the manager is unreachable the user can still Copy as JSON. See
// docs/webgui/03-extension-v4.md.

function getManagerSettings() {
    return new Promise((resolve) => {
        chrome.storage.local.get(
            ['managerUrl', 'captureToken', 'autoPost', 'autoRecapture'],
            (d) => resolve({
                managerUrl: d.managerUrl || DEFAULTS.managerUrl,
                captureToken: d.captureToken || '',
                autoPost: d.autoPost !== false,
                autoRecapture: d.autoRecapture !== false
            })
        );
    });
}

// Build the same multi-export payload the popup builds, but headless. We POST
// the single capture; the manager's engine discovers the rest of the parts.
// (The popup's "Copy ALL" still produces the richer multi payload for manual
// use; auto-POST favors reliability over completeness and lets the engine
// sweep i=0,1,2,... which it already does well.)
function buildManagerPayload(capture, meta) {
    const payload = {
        schema: capture.schema || 1,
        captured_at: capture.captured_at || new Date().toISOString(),
        source: 'extension',
        url: capture.url,
        method: capture.method || 'GET',
        headers: capture.headers || {},
        cookie: capture.cookie || ''
    };
    // Attach identity meta so the manager can derive <account>/<export-ts>/.
    // CRITICAL: user/authuser/j live on the DOWNLOAD url (capture.url), NOT on
    // the takeout.google.com page url. Parse them from capture.url; the scraped
    // email (meta.email) is the best human-readable label and supplements them.
    let urlUser = null, urlAuthuser = null, urlArchive = null;
    try {
        const qs = new URL(capture.url).searchParams;
        urlUser = qs.get('user');
        urlAuthuser = qs.get('authuser');
        urlArchive = qs.get('j');
    } catch (e) { /* malformed url; fall back to scraped meta */ }
    const m = meta || {};
    const email = m.email || null;
    const user = urlUser || m.user || null;
    const authuser = urlAuthuser || m.authuser || null;
    const archiveId = urlArchive || m.archiveId || null;
    if (email || user || authuser || archiveId) {
        payload._meta = { email, user, authuser, archiveId };
    }
    return payload;
}

async function postToManager(payload) {
    const s = await getManagerSettings();
    const headers = { 'Content-Type': 'application/json' };
    if (s.captureToken) headers['X-Capture-Token'] = s.captureToken;
    const resp = await fetch(s.managerUrl.replace(/\/$/, '') + '/api/payload', {
        method: 'POST',
        headers,
        body: JSON.stringify(payload)
    });
    const text = await resp.text();
    let body;
    try { body = JSON.parse(text); } catch (e) { body = { detail: text }; }
    if (!resp.ok) {
        const err = new Error(body.detail || ('HTTP ' + resp.status));
        err.status = resp.status;
        throw err;
    }
    return body;
}

async function maybeAutoPost(capture) {
    let s;
    try { s = await getManagerSettings(); } catch (e) { return; }
    if (!s.autoPost) return;
    // Pull identity meta the content script may have stashed.
    chrome.storage.local.get(['accountMeta'], async (d) => {
        const meta = d.accountMeta || {};
        try {
            const payload = buildManagerPayload(capture, meta);
            const result = await postToManager(payload);
            chrome.storage.local.set({
                lastPostStatus: { ok: true, jobId: result.job_id,
                                  status: result.status, at: Date.now() }
            });
            notifyManager('Sent to manager', 'Job ' + (result.job_id || '?')
                + ' (' + (result.status || '?') + ')');
        } catch (e) {
            chrome.storage.local.set({
                lastPostStatus: { ok: false, error: e.message || String(e),
                                  at: Date.now() }
            });
            // Non-fatal: the clipboard copy still works as a fallback.
            notifyManager('Manager unreachable',
                'Capture kept; use Copy as JSON. (' + (e.message || 'error') + ')');
        }
    });
}

function notifyManager(title, message) {
    try {
        chrome.notifications.create('mgr-' + Date.now(), {
            type: 'basic', iconUrl: 'icon48.png',
            title, message, priority: 0, silent: true
        });
    } catch (e) { /* non-critical */ }
}

// ---------------------------------------------------------------------------
// Auto-re-capture heartbeat (v4): poll the manager for needs_cookie jobs and,
// if found, trigger a fresh capture by re-clicking the most recent export's
// download in an existing takeout tab. The extension NEVER types credentials;
// it only re-clicks a download in an already-authenticated session. If that
// yields no valid cookie, the manager escalates to a human via Telegram/sound.
// ---------------------------------------------------------------------------
const RECAPTURE_ALARM = 'takeout-recapture-poll';

chrome.alarms.create(RECAPTURE_ALARM, { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === RECAPTURE_ALARM) pollRecapturePending();
});

async function pollRecapturePending() {
    let s;
    try { s = await getManagerSettings(); } catch (e) { return; }
    if (!s.autoRecapture) return;
    try {
        const headers = {};
        if (s.captureToken) headers['X-Capture-Token'] = s.captureToken;
        const resp = await fetch(
            s.managerUrl.replace(/\/$/, '') + '/api/control/recapture-pending',
            { headers });
        if (!resp.ok) return;
        const body = await resp.json();
        if (body && body.pending) {
            await triggerRecapture();
        }
    } catch (e) {
        // manager unreachable; try again next alarm tick
    }
}

async function triggerRecapture() {
    // Find an existing takeout tab (or open one) and ask the content script to
    // re-click the most recent Download button. A fresh request to the final
    // host fires the capture listener, which then auto-POSTs the new cookie.
    try {
        const tabs = await chrome.tabs.query({ url: 'https://takeout.google.com/*' });
        let tab = tabs[0];
        if (!tab) {
            tab = await chrome.tabs.create({
                url: 'https://takeout.google.com/manage', active: false });
            // give the page a moment to load before messaging
            await new Promise(r => setTimeout(r, 4000));
        }
        chrome.tabs.sendMessage(tab.id, { action: 'recaptureDownload' }, () => {
            void chrome.runtime.lastError;  // swallow if content script not ready
        });
    } catch (e) {
        // non-fatal; manager will escalate to a human if no cookie arrives
    }
}

// ---------------------------------------------------------------------------
// Clipboard helper for the popup
// ---------------------------------------------------------------------------
// The popup (which has a DOM) does the clipboard write directly. The
// background just hands back the captured data on request.

// ---------------------------------------------------------------------------
// Message handlers from popup / options
// ---------------------------------------------------------------------------
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.action === 'getCapture') {
        chrome.storage.local.get(['lastCapture', 'hasCapture', 'captureCount', 'lastError', 'pageScrape'], (data) => {
            sendResponse({
                capture: data.lastCapture || null,
                hasCapture: !!data.hasCapture,
                count: data.captureCount || 0,
                error: data.lastError || null,
                pageScrape: data.pageScrape || null
            });
        });
        return true;  // async
    }

    if (msg.action === 'pageScrape') {
        // Content script sent us the list of exports from the DOM.
        chrome.storage.local.set({
            pageScrape: {
                exports: msg.exports || [],
                sizes: msg.sizes || {},
                url: msg.url,
                timestamp: msg.timestamp
            }
        }, () => {
            sendResponse({ ok: true, count: (msg.exports || []).length });
        });
        return true;
    }

    if (msg.action === 'clickIntercept') {
        // A download button was clicked. Store the URL so the popup can
        // correlate it with the page-scraped exports.
        chrome.storage.local.get(['clickIntercepts'], (data) => {
            const intercepts = (data.clickIntercepts || []).concat([{
                url: msg.url,
                text: msg.text,
                timestamp: msg.timestamp
            }]).slice(-20);
            chrome.storage.local.set({ clickIntercepts: intercepts });
        });
        sendResponse({ ok: true });
        return false;
    }

    if (msg.action === 'clearCapture') {
        chrome.storage.local.set({
            lastCapture: null,
            hasCapture: false,
            lastError: null,
            pageScrape: null
        }, () => {
            updateBadgeAndIcon();
            sendResponse({ ok: true });
        });
        return true;
    }

    if (msg.action === 'setPreference') {
        const allowed = ['autoCopy', 'badgeFilename'];
        if (!allowed.includes(msg.key)) {
            sendResponse({ error: 'unknown preference' });
            return false;
        }
        chrome.storage.local.set({ [msg.key]: msg.value }, () => {
            sendResponse({ ok: true });
        });
        return true;
    }

    if (msg.action === 'getPreferences') {
        chrome.storage.local.get(['autoCopy', 'badgeFilename'], (data) => {
            sendResponse({
                autoCopy: !!data.autoCopy,
                badgeFilename: data.badgeFilename !== false  // default true
            });
        });
        return true;
    }

    // --- v4 manager handlers (additive) -------------------------------------
    if (msg.action === 'getState') {
        chrome.storage.local.get(
            ['hasCapture', 'captureCount', 'lastPostStatus', 'lastError',
             'managerUrl', 'captureToken', 'autoPost', 'autoRecapture'],
            (d) => sendResponse({
                hasCapture: !!d.hasCapture,
                captureCount: d.captureCount || 0,
                lastPostStatus: d.lastPostStatus || null,
                lastError: d.lastError || null,
                managerUrl: d.managerUrl || DEFAULTS.managerUrl,
                captureToken: d.captureToken || '',
                autoPost: d.autoPost !== false,
                autoRecapture: d.autoRecapture !== false
            })
        );
        return true;
    }

    if (msg.action === 'setManagerConfig') {
        const patch = {};
        for (const k of MANAGER_CONFIG_KEYS) {
            if (k in (msg.config || {})) patch[k] = msg.config[k];
        }
        chrome.storage.local.set(patch, () => sendResponse({ ok: true, saved: patch }));
        return true;
    }

    if (msg.action === 'forceCapture') {
        // Re-POST the most recent capture to the manager on demand (used by the
        // popup "Send now" button and by the manager-driven recapture flow).
        chrome.storage.local.get(['lastCapture'], async (d) => {
            if (!d.lastCapture) { sendResponse({ ok: false, error: 'no capture' }); return; }
            try {
                const result = await postToManager(
                    buildManagerPayload(d.lastCapture, (await chrome.storage.local.get(['accountMeta'])).accountMeta || {}));
                chrome.storage.local.set({
                    lastPostStatus: { ok: true, jobId: result.job_id,
                                      status: result.status, at: Date.now() }
                });
                sendResponse({ ok: true, result });
            } catch (e) {
                chrome.storage.local.set({
                    lastPostStatus: { ok: false, error: e.message || String(e),
                                      at: Date.now() }
                });
                sendResponse({ ok: false, error: e.message || String(e) });
            }
        });
        return true;
    }

    if (msg.action === 'setAccountMeta') {
        // Content script reports scraped identity (email/user/authuser).
        chrome.storage.local.set({ accountMeta: msg.meta || {} },
            () => sendResponse({ ok: true }));
        return true;
    }
});
