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
    captureTimestamps: []   // rolling buffer of recent capture times (for badge)
};

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
                updateBadge();
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
// Badge: pulse when a new capture arrives
// ---------------------------------------------------------------------------
function updateBadge() {
    chrome.storage.local.get(['hasCapture', 'lastCapture'], (data) => {
        if (data.hasCapture && data.lastCapture) {
            const filename = extractFilename(data.lastCapture.url);
            const short = filename.length > 8 ? filename.slice(-6) : filename;
            chrome.action.setBadgeText({ text: short });
            chrome.action.setBadgeBackgroundColor({ color: '#7c3aed' });
        } else {
            chrome.action.setBadgeText({ text: '' });
        }
    });
}

function extractFilename(url) {
    try {
        const tail = url.split('?')[0].split('/').pop() || '';
        return tail.replace(/\.\w+$/, '').slice(-8);
    } catch (e) {
        return '';
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
            updateBadge();
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
});
