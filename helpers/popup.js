// Takeout Downloader Helper - Popup Logic v3
// One button: "Copy ALL exports". Always produces a multi-export JSON
// payload by fetching the full export list from Takeout's API.

const els = {
    statusBox: null,
    errorBox: null,
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

function setError(text) {
    // Sticky error display: doesn't auto-clear, only cleared by a new
    // action or by the user dismissing it.
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

    els.clearBtn.disabled = false;

    if (!capture.cookie) {
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
        // Part index 0-based. Lets the CLI reconstruct the i= param
        // without parsing the URL, and lets it match sizes to parts
        // even if URL ordering changes.
        if (i > 0 || urls.length > 1) entry.partIndex = i;
        // Attach size from button metadata if available. The CLI uses
        // these to skip Range probes and to show "155.3 MB" up front.
        if (meta && meta.sizes && meta.sizes[i]) {
            entry.size = meta.sizes[i];
        } else if (meta && meta.buttonData && meta.buttonData[i]) {
            entry.size = meta.buttonData[i].size;
        }
        // Attach download count from page text if available
        if (meta && meta.dlCounts && meta.dlCounts[entry.filename] !== undefined) {
            entry.dlCount = meta.dlCounts[entry.filename];
        }
        return entry;
    });

    // Bump schema to 2 when we have full part metadata (archiveId +
    // expectedParts). Schema 1 multi-payloads are still accepted by the
    // CLI as a legacy fallback but skip the new auto-download features.
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

    // v2 fields: archiveId + expectedParts let the CLI auto-detect the
    // part count from the payload instead of asking the user.
    if (hasPartMetadata) {
        payload.archiveId = meta.archiveId;
        payload.expectedParts = meta.expectedParts;
    }

    // Legacy _meta blob for back-compat with older CLIs (they read it
    // but ignore the new top-level fields).
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
        return await chrome.tabs.sendMessage(tab.id, { action: 'contentFetchExports', url: capture.url });
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
    els.errorBox = $('errorBox');
    els.copyAllBtn = $('copyAllBtn');
    els.copyCaptureBtn = $('copyCaptureBtn');
    els.clearBtn = $('clearBtn');
    els.preview = $('preview');
    els.autoCopy = $('autoCopy');
    els.countPill = $('countPill');

    // Dismiss button on the error box
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

    // "Copy this capture" button — copies the single captured URL as a
    // single-export payload. Useful when Copy ALL fails.
    els.copyCaptureBtn.addEventListener('click', async () => {
        setError(null);
        const response = await chrome.runtime.sendMessage({ action: 'getCapture' });
        if (!response || !response.capture) {
            setStatus('No capture to copy.', 'warn');
            return;
        }
        const payload = buildSinglePayload(response.capture);
        await copyToClipboard(JSON.stringify(payload, null, 2), 'this capture');
    });

    // Clicking the [copy] link next to "Preview capture" does the same.
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
        });
    }

    // The one and only copy button: fetches all exports then builds
    // a multi-payload with them.
    els.copyAllBtn.addEventListener('click', async () => {
        setError(null);  // clear any previous error
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
            // Fallback: the full-export fetch often fails because the manage-page
            // cookie is different from the download-host cookie. The single capture
            // is enough for the CLI to discover the rest, so copy it instead of
            // leaving the clipboard empty.
            setStatus('Full export list unavailable (' + err + '). Falling back to single capture.', 'warn');
            const singlePayload = buildSinglePayload(capture);
            await copyToClipboard(JSON.stringify(singlePayload, null, 2), 'this capture');
            setError('Copied the single captured export. The CLI will discover the rest.');
            return;
        }

        const urls = result.urls || [];
        if (urls.length === 0) {
            setStatus('No URLs returned by Takeout API.', 'warn');
            setError('Takeout returned 0 URLs. ' +
                     'Open the browser console for details. ' +
                     'The cookie may have expired — click a download again.');
            return;
        }

        const payload = buildMultiPayload(capture, urls, result.meta);
        // Status hint shows the detected part count when we got it from
        // the page (so the user sees "Detected 5 parts" before pasting).
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
        await copyToClipboard(
            JSON.stringify(payload, null, 2),
            `ALL exports (${urls.length}${sizeInfo})${partInfo}`
        );
        setError(null);
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

    // Listen for storage changes (auto-refresh after a new capture)
    chrome.storage.onChanged.addListener((changes, area) => {
        if (area === 'local' && (changes.lastCapture || changes.captureCount)) {
            refresh();
        }
    });

    // Refresh every 2s while popup is open so age stays accurate
    setInterval(refresh, 2000);
});
