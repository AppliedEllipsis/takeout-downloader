// Takeout Downloader Helper - Popup Logic v3
// One button: "Copy ALL exports". Always produces a multi-export JSON
// payload by fetching the full export list from Takeout's API.

const els = {
    statusBox: null,
    copyAllBtn: null,
    clearBtn: null,
    preview: null,
    autoCopy: null,
    countPill: null
};

function $(id) { return document.getElementById(id); }

function setStatus(text, cls) {
    els.statusBox.textContent = text;
    els.statusBox.className = 'status ' + (cls || 'dim');
}

function shortFilename(url) {
    if (!url) return 'unknown';
    const tail = url.split('?')[0].split('/').pop() || url;
    return tail.length > 30 ? '…' + tail.slice(-27) : tail;
}

function formatAge(timestamp) {
    if (!timestamp) return '';
    const seconds = Math.max(0, Math.round((Date.now() - timestamp) / 1000));
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
    return `${Math.round(seconds / 3600)}h ago`;
}

function renderCapture(data) {
    const capture = data.capture;
    const count = data.count || 0;
    els.countPill.textContent = String(count);
    els.countPill.style.display = count > 0 ? 'inline-block' : 'none';

    // Enable "Copy ALL" if we have a capture with a cookie.
    els.copyAllBtn.disabled = !capture || !capture.cookie;

    if (!capture) {
        setStatus('No capture yet. Start a download on takeout.google.com to capture.', 'dim');
        els.clearBtn.disabled = true;
        els.preview.textContent = 'No capture yet.';
        return;
    }

    const filename = shortFilename(capture.url);
    const age = formatAge(capture.captured_at ? Date.parse(capture.captured_at) : Date.now());
    const cookieChars = (capture.cookie || '').length;

    els.clearBtn.disabled = false;

    if (capture.pre_redirect) {
        setStatus(
            `⚠ Pre-redirect capture: ${filename} (${age}). ` +
            `Re-capture from a request to takeout-download.usercontent.google.com.`,
            'warn'
        );
    } else if (!capture.cookie) {
        setStatus('✗ No cookie captured. Try clicking the download again.', 'err');
    } else if (cookieChars < 100) {
        setStatus(
            `⚠ Cookie only ${cookieChars} chars (${age}). ` +
            `Likely missing __Secure-* markers. Re-capture from final host.`,
            'warn'
        );
    } else {
        setStatus(
            `✓ Captured: ${filename} (${age}, cookie ${cookieChars} chars, #${count}). ` +
            `Click Copy ALL exports to fetch the full archive list.`,
            'ok'
        );
    }

    // Show a preview of just the captured URL (no cookie text)
    els.preview.textContent = JSON.stringify({
        url: capture.url,
        cookie: `[${(capture.cookie || '').length} chars]`,
        captured_at: capture.captured_at
    }, null, 2);
}

async function copyToClipboard(text, kind) {
    try {
        await navigator.clipboard.writeText(text);
        setStatus(`✓ ${kind} copied! Paste into the CLI.`, 'ok');
    } catch (e) {
        // Fallback for browsers that block the async clipboard API in popups
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        try {
            document.execCommand('copy');
            setStatus(`✓ ${kind} copied! Paste into the CLI.`, 'ok');
        } catch (e2) {
            setStatus(`✗ Could not copy: ${e2.message}. Select text manually.`, 'err');
        } finally {
            document.body.removeChild(ta);
        }
    }
}

async function fetchAllExportsFromContentScript(capture) {
    // Delegate to the content script on the active Takeout tab. It runs
    // in the page context where cookies attach automatically and there's
    // no CORS preflight issue. Service workers can't reliably do
    // same-origin fetches in MV3, and the popup itself can hit preflight
    // failures with custom headers.
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.id) {
        return { ok: false, error: 'no active tab' };
    }
    if (!tab.url || !tab.url.startsWith('https://takeout.google.com/')) {
        return { ok: false, error: 'open the Takeout manage page first' };
    }
    try {
        return await chrome.tabs.sendMessage(tab.id, { action: 'contentFetchExports' });
    } catch (e) {
        return { ok: false, error: 'content script unreachable: ' + e.message };
    }
}

function refresh() {
    chrome.runtime.sendMessage({ action: 'getCapture' }, (response) => {
        if (chrome.runtime.lastError) {
            setStatus('Extension error: ' + chrome.runtime.lastError.message, 'err');
            return;
        }
        renderCapture(response || {});
    });
}

document.addEventListener('DOMContentLoaded', () => {
    els.statusBox = $('statusBox');
    els.copyAllBtn = $('copyAllBtn');
    els.clearBtn = $('clearBtn');
    els.preview = $('preview');
    els.autoCopy = $('autoCopy');
    els.countPill = $('countPill');

    refresh();

    // Load preferences
    chrome.runtime.sendMessage({ action: 'getPreferences' }, (prefs) => {
        if (prefs && typeof prefs.autoCopy === 'boolean') {
            els.autoCopy.checked = prefs.autoCopy;
        }
    });

    // The one and only copy button: fetches all exports then builds
    // a multi-payload with them.
    els.copyAllBtn.addEventListener('click', async () => {
        setStatus('Reading capture...', 'dim');
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

        setStatus('Fetching all exports from Takeout...', 'dim');
        const result = await fetchAllExportsFromContentScript(capture);

        if (!result || !result.ok) {
            const err = (result && result.error) || 'unknown';
            console.log('Takeout Downloader debug:', result && result.debug);
            setStatus("✗ Could not fetch exports: " + err, 'err');
            return;
        }

        const urls = result.urls || [];
        if (urls.length === 0) {
            setStatus('No URLs returned by Takeout API. See console for details.', 'warn');
            return;
        }

        const exports = urls.map(url => ({
            url: url,
            filename: shortFilename(url)
        }));

        const payload = {
            schema: capture.schema || 1,
            captured_at: new Date().toISOString(),
            source: 'extension',
            multi: true,
            exports: exports,
            headers: capture.headers || {},
            cookie: capture.cookie || ''
        };
        await copyToClipboard(
            JSON.stringify(payload, null, 2),
            `ALL exports (${exports.length})`
        );
    });

    els.clearBtn.addEventListener('click', () => {
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

    // Listen for storage changes (auto-refresh after a new capture)
    chrome.storage.onChanged.addListener((changes, area) => {
        if (area === 'local' && (changes.lastCapture || changes.captureCount)) {
            refresh();
        }
    });

    // Refresh every 2s while popup is open so age stays accurate
    setInterval(refresh, 2000);
});
