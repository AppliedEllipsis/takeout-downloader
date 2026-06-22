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
        filenames: [],
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

    function reportFilenames(url, text) {
        if (!url || !text) return;
        if (!url.includes('/_/TakeoutUi/data/batchexecute') && !url.includes('rpcids=lFYxZd')) return;
        const matches = text.match(/takeout-\d{8}T\d{6}Z-\d+-\d+\.zip/g);
        if (matches && matches.length > 0) {
            const unique = Array.from(new Set(matches));
            post('filenames', { filenames: unique, sourceUrl: url, time: Date.now() });
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
                    reportFilenames(url, text);
                    reportResponse(url, text, null);
                }
            } catch (e) {}
            return resp;
        });
    };

    function reportFilenamesFromText(url, text) {
        if (!url || !text) return;
        if (!url.includes('/_/TakeoutUi/data/batchexecute') && !url.includes('rpcids=lFYxZd')) return;
        const matches = text.match(/takeout-\d{8}T\d{6}Z-\d+-\d+\.zip/g);
        if (matches && matches.length > 0) {
            const unique = Array.from(new Set(matches));
            post('filenames', { filenames: unique, sourceUrl: url, time: Date.now() });
        }
    }

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
                        reportFilenamesFromText(url, self.responseText || '');
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
        const candidates = ['WIZ_global_data', '_takeout', 'takeout', 'googlesite', 'AF_initDataChunkQueue'];
        const out = {};
        for (const k of candidates) {
            if (k in window) {
                try {
                    const val = window[k];
                    if (k === 'AF_initDataChunkQueue' && Array.isArray(val)) {
                        // Extract filenames from init data chunks
                        const filenames = [];
                        for (const chunk of val) {
                            try {
                                const s = JSON.stringify(chunk);
                                const m = s.match(/takeout-\d{8}T\d{6}Z-\d+-\d+\.zip/g);
                                if (m) for (const f of m) filenames.push(f);
                            } catch (e) {}
                        }
                        if (filenames.length > 0) {
                            post('filenames', { filenames: Array.from(new Set(filenames)), sourceUrl: 'AF_initDataChunkQueue', time: Date.now() });
                        }
                        out[k] = 'array[' + val.length + ']';
                    } else {
                        out[k] = JSON.parse(JSON.stringify(val));
                    }
                } catch (e) { out[k] = 'unserializable'; }
            }
        }
        return out;
    }

    // Also scan script[class^="ds:"] tags for embedded takeout data
    function scanDsScripts() {
        const scripts = document.querySelectorAll('script[class^="ds:"]');
        const filenames = [];
        for (const s of scripts) {
            const text = s.textContent || '';
            const m = text.match(/takeout-\d{8}T\d{6}Z-\d+-\d+\.zip/g);
            if (m) for (const f of m) filenames.push(f);
        }
        if (filenames.length > 0) {
            post('filenames', { filenames: Array.from(new Set(filenames)), sourceUrl: 'ds-scripts', time: Date.now() });
        }
    }

    post('globals', { globals: readGlobals(), href: location.href });
    setTimeout(scanDsScripts, 100);
    setTimeout(scanDsScripts, 1000);
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
        } else if (event.data.type === 'filenames') {
            for (const f of (event.data.data.filenames || [])) {
                if (!spyCache.filenames.includes(f)) spyCache.filenames.push(f);
            }
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
    // Scrape the visible DOM for download links and filenames.
    // -------------------------------------------------------------------------
    function scrapeExports() {
        const exports = [];
        const seen = new Set();

        // 1. Scan all <a> tags with takeout-download hrefs
        for (const a of document.querySelectorAll('a[href*="takeout-download.usercontent.google.com"]')) {
            const href = a.getAttribute('href');
            if (href && !seen.has(href)) {
                seen.add(href);
                exports.push({ url: href, filename: href.split('?')[0].split('/').pop() || '' });
            }
        }

        // 2. Scan element attributes for takeout-download URLs
        for (const el of document.querySelectorAll('*')) {
            for (const attr of el.attributes || []) {
                if (attr.value && attr.value.includes('takeout-download.usercontent.google.com') && !seen.has(attr.value)) {
                    seen.add(attr.value);
                    exports.push({ url: attr.value, filename: attr.value.split('?')[0].split('/').pop() || '' });
                }
            }
        }

        // 3. Regex scan full HTML
        const html = document.documentElement.innerHTML;
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
    // Scrape download buttons for metadata (data-download-uri, data-size)
    // and extract visible filenames from the page text.
    // -------------------------------------------------------------------------
    function scrapePageMetadata(capturedUrl) {
        const params = parseUrlParams(capturedUrl || location.href);
        const archiveId = params.archiveId;
        const user = params.user;
        const authuser = params.authuser || '0';

        // Extract filenames from page text (summary section, etc.)
        const pageText = document.body ? document.body.innerText : '';
        const filenameRe = /takeout-\d{8}T\d{6}Z-\d+-\d+\.zip/g;
        const textFilenames = Array.from(new Set(pageText.match(filenameRe) || []));

        // Extract download counts from summary text
        // e.g. "takeout-...-001.zip (Number of times already downloaded: 5)"
        const dlCountRe = /(takeout-\d{8}T\d{6}Z-\d+-\d+\.zip)\s*\(Number of times already downloaded:\s*(\d+)\)/g;
        const dlCounts = {};
        let m;
        while ((m = dlCountRe.exec(pageText)) !== null) {
            dlCounts[m[1]] = parseInt(m[2], 10);
        }

        // Extract data-download-uri and data-size from download buttons
        const downloadButtons = document.querySelectorAll('[data-download-uri]');
        const buttonData = [];
        for (const btn of downloadButtons) {
            const uri = btn.getAttribute('data-download-uri') || '';
            const size = parseInt(btn.getAttribute('data-size') || '0', 10);
            buttonData.push({ uri, size });
        }

        return {
            filenames: textFilenames,
            dlCounts,
            buttonData,
            archiveId,
            user,
            authuser
        };
    }

    // -------------------------------------------------------------------------
    // Scrape [data-download-uri] buttons directly from the manage page.
    //
    // Most reliable source: the page renders one button per part with
    // `aria-label="Download again part X of N"`, the part URL pattern
    // (`data-download-uri`), and the part size (`data-size`). We only
    // return buttons that match the captured URL's `j=` (archive ID)
    // so URLs from OTHER Takeouts the user has on their account don't
    // leak into the multi-payload.
    //
    // The download URL itself is built by varying `i=` from 0..N-1 on
    // the captured URL template — per the takeout_dl.py contract, the
    // filename component is cosmetic and the server returns the right
    // file based on `i`.
    // -------------------------------------------------------------------------
    function scrapePartsFromButtons(capturedUrl) {
        const params = parseUrlParams(capturedUrl);
        const targetArchiveId = params.archiveId;
        if (!targetArchiveId) return null;

        // Re-query each call (cheap, ~10 nodes). The manage page rerenders
        // the <ul> on every nav so we can't cache the NodeList.
        const buttons = Array.from(document.querySelectorAll('[data-download-uri]'));
        if (buttons.length === 0) return null;

        // Filter to buttons that belong to the captured archive. Buttons
        // from other Takeouts (if Google ever shows multiple on one page)
        // are dropped here.
        const myButtons = buttons.filter(btn => {
            const uri = btn.getAttribute('data-download-uri') || '';
            // data-download-uri is HTML-attribute-encoded (e.g. "&amp;").
            const decoded = decodeURIComponent(uri.replace(/&amp;/g, '&'));
            const jm = decoded.match(/[?&]j=([a-f0-9-]+)/i);
            return jm && jm[1] === targetArchiveId;
        });
        if (myButtons.length === 0) return null;

        // Extract the part count from the first aria-label that matches
        // "part X of N". Falls back to the button count if no aria-label
        // is found (older UI or stripped markup).
        let expectedParts = myButtons.length;
        for (const btn of myButtons) {
            const al = btn.getAttribute('aria-label') || '';
            const m = al.match(/part\s+(\d+)\s+of\s+(\d+)/i);
            if (m) {
                expectedParts = parseInt(m[2], 10);
                break;
            }
        }

        // Per-part sizes keyed by `i=`. The CLI uses these to show
        // "155.3 MB" before downloading and to skip Range probes when
        // the extension already knows the size.
        const sizesByI = {};
        for (const btn of myButtons) {
            const uri = btn.getAttribute('data-download-uri') || '';
            const decoded = decodeURIComponent(uri.replace(/&amp;/g, '&'));
            const im = decoded.match(/[?&]i=(\d+)/);
            if (!im) continue;
            const i = parseInt(im[1], 10);
            const sizeStr = btn.getAttribute('data-size') || '';
            const size = parseInt(sizeStr, 10);
            if (!isNaN(size) && size > 0) sizesByI[i] = size;
        }

        // Build N URLs by varying `i=` on the captured URL template.
        // The captured URL has the right host, cookie, j=, user, rapt,
        // authuser. The filename in the path is irrelevant.
        let baseUrl;
        try {
            const u = new URL(capturedUrl);
            u.searchParams.delete('i');
            baseUrl = u;
        } catch (e) {
            return null;
        }

        const urls = [];
        const sizes = [];
        for (let i = 0; i < expectedParts; i++) {
            const u = new URL(baseUrl.toString());
            u.searchParams.set('i', String(i));
            urls.push(u.toString());
            sizes.push(sizesByI[i] || 0);
        }

        return {
            urls,
            sizes,
            archiveId: targetArchiveId,
            expectedParts,
            source: 'buttons'
        };
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

        // Strategy 1: scrape the visible [data-download-uri] buttons.
        // The page tells us "Part X of N" up front and each button's
        // data-size lets the CLI skip the Range probe entirely.
        // Filter by archive ID so we never leak URLs from other
        // Takeouts the user has on their account.
        const buttonParts = scrapePartsFromButtons(capturedUrl);
        if (buttonParts && buttonParts.urls.length > 0) {
            debug.push(`buttons: ${buttonParts.urls.length} parts for archive ${buttonParts.archiveId}`);
            return {
                ok: true,
                urls: buttonParts.urls,
                debug,
                source: buttonParts.source,
                meta: {
                    archiveId: buttonParts.archiveId,
                    expectedParts: buttonParts.expectedParts,
                    sizes: buttonParts.sizes
                }
            };
        }

        // Strategy 2: check the spy cache for URLs, BUT only those
        // matching the captured archive ID. Unfiltered, the spy cache
        // picks up URLs from other Takeouts (the manage page makes
        // calls to /api/v2/manage/archives which returns every archive)
        // and we used to return all of them — that's how the CLI ended
        // up showing 10 archives when the user was looking at one
        // Takeout with 5 parts.
        const spyUrls = Array.from(spyCache.urls).filter(u => {
            if (!u.includes('takeout-download.usercontent.google.com')) return false;
            try {
                const u2 = new URL(u);
                return u2.searchParams.get('j') === archiveId;
            } catch (e) {
                return false;
            }
        });
        if (spyUrls.length > 0) {
            return { ok: true, urls: spyUrls, debug: ['spy cache (filtered)'], source: 'spy' };
        }

        // Direct scan: look for <script class="ds:0"> containing filenames.
        // These are embedded in the page HTML and available immediately.
        const dsScripts = document.querySelectorAll('script[class^="ds:"]');
        let dsFilenames = [];
        for (const s of dsScripts) {
            const text = s.textContent || '';
            const m = text.match(/takeout-\d{8}T\d{6}Z-\d+-\d+\.zip/g);
            if (m) for (const f of m) dsFilenames.push(f);
        }
        dsFilenames = Array.from(new Set(dsFilenames));
        if (dsFilenames.length > 0 && user) {
            const reconstructed = dsFilenames.map(filename => {
                const u = new URL('https://takeout-download.usercontent.google.com/download/' + filename);
                u.searchParams.set('j', archiveId);
                u.searchParams.set('i', '1');
                u.searchParams.set('user', user);
                u.searchParams.set('authuser', authuser);
                return u.toString();
            });
            debug.push(`ds-scripts: ${dsFilenames.length} filenames`);
            return { ok: true, urls: reconstructed, debug, source: 'ds-scripts' };
        }

        // If the spy captured filenames from a batchexecute response but not
        // full URLs, reconstruct the download URLs from the captured URL template.
        if (spyCache.filenames.length > 0 && user) {
            const reconstructed = spyCache.filenames.map(filename => {
                const u = new URL('https://takeout-download.usercontent.google.com/download/' + filename);
                u.searchParams.set('j', archiveId);
                u.searchParams.set('i', '1');
                u.searchParams.set('user', user);
                u.searchParams.set('authuser', authuser);
                return u.toString();
            });
            return { ok: true, urls: reconstructed, debug: ['reconstructed from spy filenames'], source: 'spy-filenames' };
        }

        // Try page metadata: filenames from visible text + download button data
        const meta = scrapePageMetadata(capturedUrl);
        if (meta.filenames.length > 0 && meta.archiveId && meta.user) {
            // Match filenames with button sizes if available
            const exports = meta.filenames.map(filename => {
                const u = new URL('https://takeout-download.usercontent.google.com/download/' + filename);
                u.searchParams.set('j', meta.archiveId);
                u.searchParams.set('i', '1');
                u.searchParams.set('user', meta.user);
                u.searchParams.set('authuser', meta.authuser);
                return u.toString();
            });
            debug.push(`page-text filenames: ${meta.filenames.length}, buttons: ${meta.buttonData.length}`);
            return { ok: true, urls: exports, debug, source: 'page-text', meta };
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
    // ---------------------------------------------------------------------
    // v4: best-effort account email scrape, so the manager can derive the
    // <account-label> folder (e.g. braincreation) instead of a gaia id.
    // Google does NOT expose a friendly username in the download URL — the
    // user= param is an obfuscated numeric id. The signed-in email is visible
    // in the account switcher button's aria-label / title on Google pages.
    // This is read-only DOM scraping; it never leaves the box except inside
    // the payload meta POSTed to the localhost manager.
    // ---------------------------------------------------------------------
    const EMAIL_RE = /[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}/i;
    function scrapeAccountEmail() {
        // 1. Account switcher button aria-label often: "Google Account: Name\n(email@gmail.com)"
        const sel = [
            'a[aria-label*="@"]', 'a[title*="@"]',
            '[aria-label*="Google Account"]', 'header [aria-label*="@"]'
        ];
        for (const s of sel) {
            for (const el of document.querySelectorAll(s)) {
                const hay = (el.getAttribute('aria-label') || '') + ' ' +
                            (el.getAttribute('title') || '');
                const m = hay.match(EMAIL_RE);
                if (m) return m[0].toLowerCase();
            }
        }
        return null;
    }

    function reportAccountMeta() {
        try {
            const email = scrapeAccountEmail();
            const params = new URLSearchParams(location.search);
            const meta = {
                email: email || null,
                user: params.get('user') || null,
                authuser: params.get('authuser') || '0'
            };
            if (email || meta.user) {
                chrome.runtime.sendMessage({ action: 'setAccountMeta', meta })
                    .catch(() => {});
            }
        } catch (e) { /* non-critical */ }
    }

    chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
        if (msg.action === 'contentFetchExports') {
            fetchExportList(msg.url).then(sendResponse);
            return true;  // async response
        }
        if (msg.action === 'contentScrape') {
            sendResponse({ ok: true, exports: scrapeExports() });
            return false;
        }
        if (msg.action === 'recaptureDownload') {
            // Manager asked for a fresh cookie. Re-click the most recent
            // Download button so a new request hits the final host and the
            // capture listener fires. We do NOT type credentials.
            try {
                const btn = document.querySelector(
                    'a[href*="takeout-download.usercontent.google.com"], ' +
                    'button[aria-label*="Download" i], a[aria-label*="Download" i]');
                if (btn) { btn.click(); sendResponse({ ok: true, clicked: true }); }
                else { sendResponse({ ok: false, error: 'no download button found' }); }
            } catch (e) {
                sendResponse({ ok: false, error: e.message || String(e) });
            }
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
    reportAccountMeta();
    setInterval(sendScrape, 5000);
    setInterval(reportAccountMeta, 15000);

    let scrapeTimer = null;
    const observer = new MutationObserver(() => {
        if (scrapeTimer) clearTimeout(scrapeTimer);
        scrapeTimer = setTimeout(sendScrape, 1000);
    });
    if (document.body) {
        observer.observe(document.body, { childList: true, subtree: true });
    }
})();
