// Takeout Downloader Helper - Content Script
// Runs on https://takeout.google.com/* pages. Two responsibilities:
//
// 1. Scrape the visible DOM for download links (best-effort, works when
//    the page renders URLs directly).
//
// 2. On demand from the popup, fetch the full export list via the
//    same internal API the page itself uses. This works even when the
//    URLs are only loaded via XHR and never appear in the HTML source.

(function() {
    'use strict';

    const TAKEOUT_URL_RE = /https:\/\/takeout-download\.usercontent\.google\.com\/download\/takeout-[^"'\s<>]+\.zip(?:\?[^"'\s<>]*)?/g;

    function extractUrlsFromHtml(html) {
        if (!html) return [];
        const matches = html.match(TAKEOUT_URL_RE) || [];
        return Array.from(new Set(matches));
    }

    function extractUrlsFromJson(data) {
        if (!data) return [];
        const urls = [];
        const seen = new Set();
        function walk(node) {
            if (!node) return;
            if (typeof node === 'string') {
                const m = node.match(TAKEOUT_URL_RE);
                if (m) {
                    for (const url of m) {
                        if (!seen.has(url)) {
                            seen.add(url);
                            urls.push(url);
                        }
                    }
                }
                return;
            }
            if (Array.isArray(node)) {
                for (const item of node) walk(item);
                return;
            }
            if (typeof node === 'object') {
                for (const key of Object.keys(node)) walk(node[key]);
            }
        }
        walk(data);
        return urls;
    }

    function findArchiveId() {
        // The archive ID is in the URL path: /manage/archive/{id}
        const m = location.pathname.match(/\/manage\/archive\/([a-f0-9-]+)/);
        if (m) return m[1];
        // Also check the page's download links for the j= parameter
        const html = document.documentElement.innerHTML;
        const jm = html.match(/[?&]j=([a-f0-9-]+)/);
        if (jm) return jm[1];
        return null;
    }

    function findAuthuser() {
        // From URL: /u/{N}/
        const m = location.pathname.match(/\/u\/(\d+)\//);
        if (m) return m[1];
        // From the cookie
        const cookies = document.cookie;
        const cm = cookies.match(/authuser=(\d+)/);
        if (cm) return cm[1];
        return '0';
    }

    async function fetchExportList() {
        const archiveId = findArchiveId();
        if (!archiveId) {
            return { ok: false, error: 'no archive ID in URL' };
        }
        const authuser = findAuthuser();

        // Try the internal API endpoints the page itself uses.
        // We send same-origin requests with cookies automatically attached
        // because we're running in the page context.
        const apiUrls = [
            `/_/TakeoutApiUi/data?archiveId=${archiveId}&authuser=${authuser}`,
            `/api/v2/manage/archive?id=${archiveId}&authuser=${authuser}`,
            `/api/v2/manage/archives?authuser=${authuser}`,
            `/u/${authuser}/manage/archive/${archiveId}?json=1`
        ];

        const debug = [];
        for (const apiUrl of apiUrls) {
            try {
                const resp = await fetch(apiUrl, {
                    credentials: 'same-origin',
                    headers: { 'Accept': 'application/json,text/html' },
                    redirect: 'follow'
                });
                if (!resp.ok) {
                    debug.push(`${apiUrl} -> ${resp.status}`);
                    continue;
                }
                if (resp.url.includes('accounts.google.com')) {
                    return { ok: false, error: 'cookie expired', debug };
                }
                const ctype = resp.headers.get('content-type') || '';
                let urls = [];
                if (ctype.includes('json')) {
                    try {
                        const data = await resp.json();
                        urls = extractUrlsFromJson(data);
                    } catch (e) {
                        debug.push(`${apiUrl} -> JSON parse error: ${e.message}`);
                        continue;
                    }
                } else {
                    const html = await resp.text();
                    urls = extractUrlsFromHtml(html);
                }
                debug.push(`${apiUrl} -> ${resp.status} ${ctype} urls=${urls.length}`);
                if (urls.length > 0) {
                    return { ok: true, urls, debug, source: apiUrl };
                }
            } catch (e) {
                debug.push(`${apiUrl} -> ERROR: ${e.message}`);
            }
        }
        return { ok: false, error: 'no endpoints returned URLs', debug };
    }

    // Passively scrape the DOM for download links (best-effort).
    function scrapeExports() {
        const exports = [];
        const seen = new Set();
        const html = document.documentElement.innerHTML;

        // Strategy 1: <a> tags with takeout-download URLs
        for (const a of document.querySelectorAll('a[href*="takeout-download.usercontent.google.com"]')) {
            const href = a.getAttribute('href');
            if (href && !seen.has(href)) {
                seen.add(href);
                exports.push({
                    url: href,
                    filename: href.split('?')[0].split('/').pop() || ''
                });
            }
        }

        // Strategy 2: scan all element attributes
        for (const el of document.querySelectorAll('*')) {
            for (const attr of el.attributes || []) {
                if (attr.value && attr.value.includes('takeout-download.usercontent.google.com')
                    && !seen.has(attr.value)) {
                    seen.add(attr.value);
                    exports.push({
                        url: attr.value,
                        filename: attr.value.split('?')[0].split('/').pop() || ''
                    });
                }
            }
        }

        // Strategy 3: regex over raw HTML for any URLs we might have missed
        const matches = html.match(TAKEOUT_URL_RE) || [];
        for (const url of matches) {
            if (!seen.has(url)) {
                seen.add(url);
                exports.push({
                    url: url,
                    filename: url.split('?')[0].split('/').pop() || ''
                });
            }
        }

        return exports;
    }

    // Listen for messages from the popup
    chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
        if (msg.action === 'contentFetchExports') {
            fetchExportList().then(sendResponse);
            return true;  // async response
        }
        if (msg.action === 'contentScrape') {
            sendResponse({ ok: true, exports: scrapeExports() });
            return false;
        }
        return false;
    });

    // Periodic scrape — sends findings to background for popup display
    function sendScrape() {
        const exports = scrapeExports();
        if (exports.length > 0) {
            chrome.runtime.sendMessage({
                action: 'pageScrape',
                exports: exports,
                sizes: {},
                url: location.href,
                timestamp: Date.now()
            }).catch(() => {});
        }
    }
    sendScrape();
    setInterval(sendScrape, 5000);

    // Re-scrape on DOM changes
    let scrapeTimer = null;
    const observer = new MutationObserver(() => {
        if (scrapeTimer) clearTimeout(scrapeTimer);
        scrapeTimer = setTimeout(sendScrape, 1000);
    });
    observer.observe(document.body, { childList: true, subtree: true });
})();
