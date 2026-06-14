// Takeout Downloader Helper - Popup Logic v2
// Copy-as-JSON / Copy-as-cURL. No network calls outside the browser.

const els = {
    statusBox: null,
    copyJsonBtn: null,
    copyAllBtn: null,
    copyCurlBtn: null,
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

function renderScrape(scrape) {
    if (!scrape || !scrape.exports || scrape.exports.length === 0) return;

    // Build a small DOM list of detected exports
    let container = document.getElementById('scrapeBox');
    if (!container) {
        container = document.createElement('div');
        container.id = 'scrapeBox';
        container.className = 'scrape-box';
        container.innerHTML = '<h4>Detected exports on this page</h4>';
        els.preview.parentElement.insertBefore(container, els.preview);
    }

    const list = document.createElement('ul');
    for (const exp of scrape.exports.slice(0, 20)) {
        const li = document.createElement('li');
        li.textContent = exp.filename || shortFilename(exp.url);
        list.appendChild(li);
    }
    if (scrape.exports.length > 20) {
        const li = document.createElement('li');
        li.textContent = `… and ${scrape.exports.length - 20} more`;
        list.appendChild(li);
    }

    // Replace previous list
    const oldList = container.querySelector('ul');
    if (oldList) oldList.remove();
    container.appendChild(list);
}

function renderCapture(data) {
    const capture = data.capture;
    const count = data.count || 0;
    els.countPill.textContent = String(count);
    els.countPill.style.display = count > 0 ? 'inline-block' : 'none';

    // Always show scraped exports if available
    if (data.pageScrape) {
        renderScrape(data.pageScrape);
    }

    // Enable "Copy ALL" if we have a capture (the cookie lets us fetch
    // the full export list from the Takeout API server-side).
    els.copyAllBtn.disabled = !capture || !capture.cookie;

    if (!capture) {
        setStatus('No capture yet. Start a download on takeout.google.com to capture.', 'dim');
        els.copyJsonBtn.disabled = true;
        els.copyCurlBtn.disabled = true;
        els.clearBtn.disabled = true;
        els.preview.textContent = 'No capture yet.';
        return;
    }

    const filename = shortFilename(capture.url);
    const age = formatAge(capture.captured_at ? Date.parse(capture.captured_at) : Date.now());
    const cookieChars = (capture.cookie || '').length;

    els.copyJsonBtn.disabled = false;
    els.copyCurlBtn.disabled = false;
    els.clearBtn.disabled = false;

    // Pre-redirect warning — captured from takeout.google.com (not the final host).
    // Those cookies are tied to the wrong domain and downloads will return HTML.
    if (capture.pre_redirect) {
        setStatus(
            `⚠ Pre-redirect capture: ${filename} (${age}). ` +
            `Re-capture from a request to takeout-download.usercontent.google.com.`,
            'warn'
        );
    } else if (!capture.cookie) {
        setStatus(`✗ No cookie captured. Try clicking the download again.`, 'err');
    } else if (cookieChars < 100) {
        setStatus(
            `⚠ Cookie only ${cookieChars} chars (${age}). ` +
            `Likely missing __Secure-* markers. Re-capture from final host.`,
            'warn'
        );
    } else {
        setStatus(`✓ Captured: ${filename} (${age}, cookie ${cookieChars} chars, #${count})`, 'ok');
    }

    // Build preview JSON
    const previewObj = {
        schema: capture.schema || 1,
        url: capture.url,
        method: capture.method || 'GET',
        headers: capture.headers || {},
        cookie: capture.cookie ? `[${capture.cookie.length} chars]` : ''
    };
    els.preview.textContent = JSON.stringify(previewObj, null, 2);
}

async function copyToClipboard(text, kind) {
    try {
        await navigator.clipboard.writeText(text);
        setStatus(`✓ ${kind} copied! Paste into the TUI.`, 'ok');
        setTimeout(() => refresh(), 1500);
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
            setStatus(`✓ ${kind} copied! Paste into the TUI.`, 'ok');
        } catch (e2) {
            setStatus(`✗ Could not copy: ${e2.message}. Select text manually.`, 'err');
        } finally {
            document.body.removeChild(ta);
        }
        setTimeout(() => refresh(), 1500);
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
    els.copyJsonBtn = $('copyJsonBtn');
    els.copyAllBtn = $('copyAllBtn');
    els.copyCurlBtn = $('copyCurlBtn');
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

    // Wire up buttons
    els.copyJsonBtn.addEventListener('click', () => {
        chrome.runtime.sendMessage({ action: 'getCapture' }, (response) => {
            if (response && response.json) {
                copyToClipboard(response.json, 'JSON');
            } else {
                setStatus('No capture to copy.', 'warn');
            }
        });
    });

    els.copyAllBtn.addEventListener('click', () => {
        chrome.runtime.sendMessage({ action: 'getCapture' }, (response) => {
            if (!response || !response.capture) {
                setStatus('No capture to copy. Click a download first.', 'warn');
                return;
            }
            const capture = response.capture;
            if (!capture.cookie) {
                setStatus('No cookie in capture. Re-click a download first.', 'warn');
                return;
            }

            setStatus('Fetching all exports from Takeout (using your cookie)...', 'dim');

            // Ask the background worker to fetch the Takeout manage page
            // using the captured cookie. This works even when the content
            // script couldn't scrape the DOM (e.g. JS-rendered URLs).
            chrome.runtime.sendMessage(
                { action: 'fetchAllExports', capture: capture },
                (fetchResp) => {
                    if (chrome.runtime.lastError) {
                        setStatus('Extension error: ' + chrome.runtime.lastError.message, 'err');
                        return;
                    }
                    if (!fetchResp || !fetchResp.ok) {
                        const err = (fetchResp && fetchResp.error) || 'unknown';
                        setStatus(`✗ Could not fetch exports: ${err}. Try re-capturing.`, 'err');
                        return;
                    }

                    const urls = fetchResp.urls || [];
                    if (urls.length === 0) {
                        setStatus('No URLs found in Takeout response.', 'warn');
                        return;
                    }

                    // Build a multi-export payload with all detected URLs
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
                    copyToClipboard(JSON.stringify(payload, null, 2), `ALL exports (${exports.length})`);
                }
            );
        });
    });

    els.copyCurlBtn.addEventListener('click', () => {
        chrome.runtime.sendMessage({ action: 'getCapture' }, (response) => {
            if (response && response.curl) {
                copyToClipboard(response.curl, 'cURL');
            } else {
                setStatus('No capture to copy.', 'warn');
            }
        });
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
            // Auto-copy if enabled
            if (els.autoCopy.checked && changes.lastCapture) {
                const newCap = changes.lastCapture.newValue;
                if (newCap && !newCap.pre_redirect && newCap.cookie) {
                    const json = JSON.stringify({
                        schema: newCap.schema || 1,
                        captured_at: newCap.captured_at,
                        source: newCap.source || 'extension',
                        url: newCap.url,
                        method: newCap.method || 'GET',
                        headers: newCap.headers || {},
                        cookie: newCap.cookie
                    }, null, 2);
                    copyToClipboard(json, 'JSON (auto)');
                }
            }
        }
    });

    // Refresh every 2s while popup is open so age stays accurate
    setInterval(refresh, 2000);
});
