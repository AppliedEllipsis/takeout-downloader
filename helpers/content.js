// Takeout Downloader Helper - Content Script
// Runs on https://takeout.google.com/* pages.
//
// Two responsibilities:
// 1. Passively scrape the visible DOM for download links.
// 2. On demand from the popup, return the full export list. We try three
//    sources in order:
//      a. A page-script spy that watches the page's own XHR/fetch calls.
//      b. A set of guessed internal API endpoints, with rapt/user params.
//      c. The passive DOM scrape.

(function() {
    'use strict';

    const TAKEOUT_URL_RE = /https:\/\/takeout-download\.usercontent\.google\.com\/download\/takeout-[^"'\s<>]+\.zip(?:\?[^"'\s<>]*)?/g;

    // -------------------------------------------------------------------------
    // URL extraction helpers
    // -------------------------------------------------------------------------
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

    function parseUrlParams(url) {
        try {
            const u = new URL(url, location.href);
            return {
                archiveId: u.searchParams.get('j') || null,
                authuser: u.searchParams.get('authuser') || null,
                user: u.searchParams.get('user') || null,
                rapt: u.searchParams.get('rapt') || null,
                i: u.searchParams.get('i') || null
            };
        } catch (e) {
            return {};
        }
    }

    // -------------------------------------------------------------------------
    // Page-script spy: inject a script into the main world to intercept
    // the page's own fetch/XHR calls. We cannot do this directly from the
    // isolated content-script world, but a <script> tag in the page context
    // can monkey-patch window.fetch and XMLHttpRequest.
    // -------------------------------------------------------------------------
    const spyCache = {
        urls: new Set(),
        responses: [],
        globals: null
    };

    function injectPageSpy() {
        if (document.getElementById('takeout-helper-spy')) return;
        const script = document.createElement('script');
        script.id = 'takeout-helper-spy';
        script.textContent = `
(function() {
    'use strict';
    const SOURCE = 'takeout-downloader-spy';
    function post(type, data) {
        window.postMessage({ source: SOURCE, type: type, data: data, href: location.href }, '*');
    }
    function scanText(text) {
        if (typeof text !== 'string') return [];
        const re = /https:\\/\\/takeout-download\\.usercontent\\.google\\.com\\/download\\/takeout-[^"'\\s<>]+\\.zip(?:\\?[^"'\\s<>]*)?/g;
        return Array.from(new Set(text.match(re) || []));
    }
    function scanObject(obj) {
        try {
            const str = JSON.stringify(obj);
            return scanText(str);
        } catch (e) { return []; }
    }
    function reportUrl(url) {
        if (url && (url.includes('takeout-download.usercontent.google.com') || url.includes('manage/archive') || url.includes('TakeoutApiUi'))) {
            post('url', { url: url, time: Date.now() });
        }
    }
    function reportResponse(url, text, obj) {
        const fromText = scanText(text);
        const fromObj = obj ? scanObject(obj) : [];
        const all = Array.from(new Set(fromText.concat(fromObj)));
        if (all.length > 0) {
            post('urls', { urls: all, sourceUrl: url, time: Date.now() });
        }
    }

    // Patch fetch
    const origFetch = window.fetch;
    window.fetch = function(...args) {
        let url = '';
        if (args[0]) {
            url = (typeof args[0] === 'string') ? args[0] : (args[0].url || String(args[0]));
        }
        reportUrl(url);
        return origFetch.apply(this, args).then(async (resp) => {
            try {
                const clone = resp.clone();
                const ctype = (clone.headers.get('content-type') || '').toLowerCase();
                if (ctype.includes('json')) {
                    const obj = await clone.json().catch(() => null);
                    reportResponse(url, '', obj);
                } else if (ctype.includes('html') || ctype.includes('text') || ctype.includes('javascript')) {
                    const text = await clone.text().catch(() => '');
                    reportResponse(url, text, null);
                }
            } catch (e) {}
            return resp;
        });
    };

    // Patch XHR
    const origOpen = XMLHttpRequest.prototype.open;
    const origSend = XMLHttpRequest.prototype.send;
    let lastXhrUrl = '';
    XMLHttpRequest.prototype.open = function(method, url, ...rest) {
        lastXhrUrl = url;
        reportUrl(url);
        return origOpen.call(this, method, url, ...rest);
    };
    XMLHttpRequest.prototype.send = function(...args) {
        const self = this;
        const url = lastXhrUrl;
        function onReady() {
            if (self.readyState === 4) {
                try {
                    const ctype = (self.getResponseHeader('content-type') || '').toLowerCase();
                    if (ctype.includes('json')) {
                        const obj = JSON.parse(self.responseText);
                        reportResponse(url, '', obj);
                    } else {
                        reportResponse(url, self.responseText || '', null);
                    }
                } catch (e) {}
            }
        }
        self.addEventListener('readystatechange', onReady);
        return origSend.call(this, ...args);
    };

    // Try to read globals the page might expose
    function readGlobals() {
        const candidates = ['WIZ_global_data', '_takeout', 'takeout', 'googlesite'];
        const out = {};
        for (const k of candidates) {
            if (k in window) {
                try { out[k] = JSON.parse(JSON.stringify(window[k])); } catch (e) { out[k] = 'unserializable'; }
            }
        }
        return out;
    }
    post('globals', { globals: readGlobals(), href: location.href });
})();
        `;
        (document.head || document.documentElement).appendChild(script);
        script.onload = () => script.remove();
    }

    window.addEventListener('message', (event) => {
        if (!event.data || event.data.source !== 'takeout-downloader-spy') return;
        if (event.data.type === 'url') {
            if (event.data.data && event.data.data.url) {
                spyCache.urls.add(event.data.data.url);
            }
        } else if (event.data.type === 'urls') {
            for (const u of (event.data.data.urls || [])) spyCache.urls.add(u);
            spyCache.responses.push(event.data.data);
        } else if (event.data.type === 'globals') {
            spyCache.globals = event.data.data;
        }
    });

    // Inject spy as soon as possible, and again on URL changes (SPA nav).
    injectPageSpy();
    let lastHref = location.href;
    setInterval(() => {
        if (location.href !== lastHref) {
            lastHref = location.href;
            injectPageSpy();
        }
    }, 1000);

    // -------------------------------------------------------------------------
    // Scrape the visible DOM for download links.
    // -------------------------------------------------------------------------
    function scrapeExports() {
        const exports = [];
        const seen = new Set();
        const html = document.documentElement.innerHTML;

        for (const a of document.querySelectorAll('a[href*="takeout-download.usercontent.google.com"]')) {
            const href = a.getAttribute('href');
            if (href && !seen.has(href)) {
                seen.add(href);
                exports.push({ url: href, filename: href.split('?')[0].split('/').pop() || '' });
            }
        }
        for (const el of document.querySelectorAll('*')) {
            for (const attr of el.attributes || []) {
                if (attr.value && attr.value.includes('takeout-download.usercontent.google.com') && !seen.has(attr.value)) {
                    seen.add(attr.value);
                    exports.push({ url: attr.value, filename: attr.value.split('?')[0].split('/').pop() || '' });
                }
            }
        }
        const matches = html.match(TAKEOUT_URL_RE) || [];
        for (const url of matches) {
            if (!seen.has(url)) {
                seen.add(url);
                exports.push({ url: url, filename: url.split('?')[0].split('/').pop() || '' });
            }
        }
        return exports;
    }

    // -------------------------------------------------------------------------
    // Try to fetch the export list from Takeout's internal API.
    // -------------------------------------------------------------------------
    async function fetchExportList(capturedUrl) {
        const params = parseUrlParams(capturedUrl || location.href);
        const archiveId = params.archiveId;
        const authuser = params.authuser || '0';
        const user = params.user;
        const rapt = params.rapt;

        if (!archiveId) {
            return { ok: false, error: 'no archive ID in captured URL' };
        }

        const query = new URLSearchParams();
        query.set('archiveId', archiveId);
        query.set('authuser', authuser);
        if (user) query.set('user', user);
        if (rapt) query.set('rapt', rapt);

        const apiUrls = [
            `/_/TakeoutApiUi/data?${query.toString()}`,
            `/api/v2/manage/archive?${query.toString()}`,
            `/api/v2/manage/archives?${query.toString()}`,
            `/u/${authuser}/manage/archive/${archiveId}?json=1&${query.toString()}`,
            `/u/${authuser}/manage/archive/${archiveId}?${query.toString()}`,
            `/settings/takeout/downloads?${query.toString()}`
        ];

        const debug = [];

        // First, check if the spy has already seen the URLs.
        const spyUrls = Array.from(spyCache.urls).filter(u => u.includes('takeout-download.usercontent.google.com'));
        if (spyUrls.length > 0) {
            return { ok: true, urls: spyUrls, debug: ['spy cache'], source: 'spy' };
        }

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

        // Last resort: DOM scrape
        const domExports = scrapeExports();
        const domUrls = domExports.map(e => e.url);
        if (domUrls.length > 0) {
            return { ok: true, urls: domUrls, debug: debug.concat(['DOM scrape fallback']), source: 'dom' };
        }

        return { ok: false, error: 'no endpoints returned URLs', debug };
    }

    // -------------------------------------------------------------------------
    // Listen for messages from the popup.
    // -------------------------------------------------------------------------
    chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
        if (msg.action === 'contentFetchExports') {
            fetchExportList(msg.url).then(sendResponse);
            return true;  // async response
        }
        if (msg.action === 'contentScrape') {
            sendResponse({ ok: true, exports: scrapeExports() });
            return false;
        }
        return false;
    });

    // Periodic scrape — sends findings to background for popup display.
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

    let scrapeTimer = null;
    const observer = new MutationObserver(() => {
        if (scrapeTimer) clearTimeout(scrapeTimer);
        scrapeTimer = setTimeout(sendScrape, 1000);
    });
    if (document.body) {
        observer.observe(document.body, { childList: true, subtree: true });
    }
})();
