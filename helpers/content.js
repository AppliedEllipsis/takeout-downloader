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
    // injectPageSpy();  // DISABLED: caused Chrome SIGKILL on takeout.google.com after login (CSP violation blocking inline script injection)
    let lastHref = location.href;
    setInterval(() => {
        if (location.href !== lastHref) {
            lastHref = location.href;
            sendV2Capture();  // v2: re-capture on SPA navigation (§6.2)
            // injectPageSpy(); // DISABLED
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
    // v2 structured capture payload (docs/v2/01-IDENTITY-AND-SCRAPE.md §6).
    //
    // Every scraped field is produced by a multi-strategy scraper returning
    // {value, source, ok}; a miss is null + reason, NEVER an empty string that
    // could silently become a folder name. The winning strategy per field is
    // recorded in scrape_report so the manager can show provenance. Building
    // this payload costs zero Google download requests.
    // -------------------------------------------------------------------------
    const TAKEOUT_PART_RE = /takeout-(\d{8}T\d{6}Z)-\d+-(\d+)\.zip/i;

    function ok(value, source, ambiguous) {
        return { value: value, source: source, ok: true, ambiguous: !!ambiguous };
    }
    function miss(reason) {
        return { value: null, ok: false, reason: reason };
    }

    function decodeTakeoutUri(uri) {
        if (!uri) return '';
        return decodeURIComponent(uri.replace(/&amp;/g, '&'));
    }

    function filenameFromUri(uri) {
        const decoded = decodeTakeoutUri(uri);
        return decoded.split('?')[0].split('/').pop() || '';
    }

    function archiveIdFromUri(uri) {
        const m = (uri || '').match(/[?&]j=([a-f0-9-]+)/i);
        return m ? m[1] : null;
    }

    function filenameIdx(name) {
        const m = TAKEOUT_PART_RE.exec(name || '');
        if (!m) return null;
        const n = parseInt(m[2], 10);
        return isNaN(n) ? null : n;
    }

    function sortFilenames(names) {
        return names.slice().sort(function (a, b) {
            const ia = filenameIdx(a), ib = filenameIdx(b);
            if (ia !== null && ib !== null) return ia - ib;
            return 0;
        });
    }

    // Try a field's strategies in order; the first {ok, non-empty} wins.
    // Returns { value, entry, ambiguous } where entry is the scrape_report row.
    function scrapeField(field, strategies) {
        const entry = { field: field, source: null, ok: false, ms: 0 };
        let lastReason = '';
        let totalMs = 0;
        for (let si = 0; si < strategies.length; si++) {
            const t0 = performance.now();
            let out;
            try { out = strategies[si].fn(); } catch (e) { out = miss((e && e.message) || String(e)); }
            totalMs += performance.now() - t0;
            if (out && out.ok && out.value !== null && out.value !== undefined && out.value !== '') {
                entry.source = out.source || strategies[si].name;
                entry.ok = true;
                entry.ms = Math.round(totalMs);
                return { value: out.value, entry: entry, ambiguous: !!out.ambiguous };
            }
            lastReason = (out && out.reason) ? out.reason : (strategies[si].name + ' missed');
        }
        entry.source = strategies.length ? strategies[strategies.length - 1].name : '';
        entry.ok = false;
        entry.ms = Math.round(totalMs);
        entry.reason = lastReason || 'no strategy produced a value';
        return { value: null, entry: entry, ambiguous: false };
    }

    // The normative v2 payload (field names fixed — see docs/v2/01 §6).
    function capturePayload() {
        const captured_at = Date.now();
        const report = [];
        const pageText = document.body ? document.body.innerText : '';

        // Raw [data-download-uri] buttons (all archives Google may show).
        const rawRows = [];
        for (const btn of document.querySelectorAll('[data-download-uri]')) {
            const uri = decodeTakeoutUri(btn.getAttribute('data-download-uri') || '');
            const filename = filenameFromUri(uri);
            if (!filename) continue;
            const sizeStr = btn.getAttribute('data-size') || '';
            const size = parseInt(sizeStr, 10);
            rawRows.push({
                btn: btn,
                uri: uri,
                filename: filename,
                size: (isNaN(size) || size <= 0) ? null : size
            });
        }

        // -- archive_id (the stable job key; never used for display) -----------
        const archive = scrapeField('archive_id', [
            { name: 'url-j-param', fn: function () {
                const v = new URLSearchParams(location.search).get('j');
                return v ? ok(v, 'url-j-param') : miss('no j= in page URL');
            } },
            { name: 'button-j-param', fn: function () {
                for (const r of rawRows) {
                    const j = archiveIdFromUri(r.uri);
                    if (j) return ok(j, 'button-j-param');
                }
                return miss('no j= in any data-download-uri');
            } },
            { name: 'href-j-param', fn: function () {
                const m = location.href.match(/[?&]j=([a-f0-9-]+)/i);
                return m ? ok(m[1], 'href-j-param') : miss('no j= in location.href');
            } }
        ]);
        report.push(archive.entry);
        if (!archive.value) {
            // Not on a manage/export view — return a null payload (sender skips
            // payloads without an archive_id; the manager 400s on them).
            report.push({ field: 'capture', source: 'pre-check', ok: false, ms: 0,
                reason: 'no archive_id; page is not an export/manage view' });
            return {
                archive_id: null, user: null, authuser: null, parts_expected: null,
                uris: null, sizes: null, filenames: [], dl_counts: null,
                account: null, export_ts_raw: null, scrape_report: report,
                locale_warning: false, captured_at: captured_at
            };
        }

        // Buttons belonging to THIS archive only (mirrors scrapePartsFromButtons).
        const rows = rawRows.filter(function (r) {
            const j = archiveIdFromUri(r.uri);
            return j === archive.value || j === null;
        });

        // Distinct export timestamps among this archive's button filenames.
        const buttonTs = new Set();
        for (const r of rows) {
            const m = TAKEOUT_PART_RE.exec(r.filename);
            if (m) buttonTs.add(m[1]);
        }
        if (buttonTs.size > 1) {
            console.warn('[takeout-helper] multiple export timestamps among part buttons:',
                Array.from(buttonTs).join(', '));
        }
        const tsFilter = buttonTs.size === 1 ? Array.from(buttonTs)[0] : null;

        // -- parts_expected: "part X of N" -> N, else button count -------------
        const parts = scrapeField('parts_expected', [
            { name: 'aria-part-of-n', fn: function () {
                for (const r of rows) {
                    const al = r.btn.getAttribute('aria-label') || '';
                    const m = al.match(/part\s+\d+\s+of\s+(\d+)/i);
                    if (m) {
                        const n = parseInt(m[1], 10);
                        if (!isNaN(n) && n > 0) return ok(n, 'aria-part-of-n');
                    }
                }
                return miss('no "part X of N" aria-label');
            } },
            { name: 'numeric-fallback', fn: function () {
                // Localised totals: "Teil X von N", "X/N", etc.
                for (const r of rows) {
                    const al = r.btn.getAttribute('aria-label') || '';
                    const m = al.match(/(\d+)\s*(?:[/\u2215]|\b(?:of|von|de|sur|da|od|iz|из|де)\b)\s*(\d+)/i);
                    if (m) {
                        const n = parseInt(m[2], 10);
                        if (!isNaN(n) && n > 0) return ok(n, 'numeric-fallback');
                    }
                }
                return miss('no localised part total in aria-label');
            } },
            { name: 'button-count', fn: function () {
                if (rows.length > 0) return ok(rows.length, 'button-count');
                return miss('no download buttons');
            } }
        ]);
        report.push(parts.entry);

        // -- filenames (union of page text + buttons, in part-index order) ------
        const textFilenames = Array.from(new Set(pageText.match(/takeout-\d{8}T\d{6}Z-\d+-\d+\.zip/g) || []));
        const filteredTextFilenames = tsFilter
            ? textFilenames.filter(function (f) { return f.indexOf('takeout-' + tsFilter) === 0; })
            : textFilenames;
        const filenames = scrapeField('filenames', [
            { name: 'page-text-plus-buttons', fn: function () {
                const all = Array.from(new Set(filteredTextFilenames.concat(rows.map(function (r) { return r.filename; }))));
                if (all.length > 0) return ok(sortFilenames(all), 'page-text-plus-buttons');
                return miss('no filenames in page text or buttons');
            } },
            { name: 'buttons-only', fn: function () {
                if (rows.length > 0) return ok(sortFilenames(rows.map(function (r) { return r.filename; })), 'buttons-only');
                return miss('no download buttons');
            } }
        ]);
        report.push(filenames.entry);

        // -- uris {filename: download_uri} --------------------------------------
        const uris = scrapeField('uris', [
            { name: 'data-download-uri', fn: function () {
                const map = {};
                for (const r of rows) {
                    if (!map[r.filename]) map[r.filename] = r.uri;
                }
                const keys = Object.keys(map);
                return keys.length > 0 ? ok(map, 'data-download-uri') : miss('no data-download-uri buttons');
            } },
            { name: 'anchor-hrefs', fn: function () {
                const map = {};
                for (const a of document.querySelectorAll('a[href*="takeout-download.usercontent.google.com"]')) {
                    const uri = decodeTakeoutUri(a.getAttribute('href') || '');
                    const filename = filenameFromUri(uri);
                    if (filename && !map[filename]) map[filename] = uri;
                }
                const keys = Object.keys(map);
                return keys.length > 0 ? ok(map, 'anchor-hrefs') : miss('no anchor hrefs');
            } }
        ]);
        report.push(uris.entry);

        // -- sizes {filename: bytes} (from data-size) ----------------------------
        const sizes = scrapeField('sizes', [
            { name: 'data-size', fn: function () {
                const map = {};
                for (const r of rows) {
                    if (r.size !== null && !map[r.filename]) map[r.filename] = r.size;
                }
                const keys = Object.keys(map);
                return keys.length > 0 ? ok(map, 'data-size') : miss('no data-size attributes');
            } },
            { name: 'anchor-data-size', fn: function () {
                const map = {};
                for (const a of document.querySelectorAll('a[href*="takeout-download.usercontent.google.com"][data-size]')) {
                    const uri = decodeTakeoutUri(a.getAttribute('href') || '');
                    const filename = filenameFromUri(uri);
                    const size = parseInt(a.getAttribute('data-size') || '', 10);
                    if (filename && !isNaN(size) && size > 0 && !map[filename]) map[filename] = size;
                }
                const keys = Object.keys(map);
                return keys.length > 0 ? ok(map, 'anchor-data-size') : miss('no anchor data-size');
            } }
        ]);
        report.push(sizes.entry);

        // -- dl_counts {filename: n} — Google's OWN attempt counter --------------
        const dlEng = {};
        {
            const re = /(takeout-\d{8}T\d{6}Z-\d+-\d+\.zip)\s*\(Number of times already downloaded:\s*(\d+)\)/gi;
            let m;
            while ((m = re.exec(pageText)) !== null) {
                if (!tsFilter || m[1].indexOf('takeout-' + tsFilter) === 0) {
                    dlEng[m[1]] = parseInt(m[2], 10);
                }
            }
        }
        const dlNum = {};
        {
            // Numeric fallback: trailing integer of any parenthesised suffix
            // (non-English UIs, e.g. "(Téléchargements: 5)").
            const re = /(takeout-\d{8}T\d{6}Z-\d+-\d+\.zip)\s*\(([^)]*)\)/gi;
            let m;
            while ((m = re.exec(pageText)) !== null) {
                if (tsFilter && m[1].indexOf('takeout-' + tsFilter) !== 0) continue;
                const nums = m[2].match(/\d+/g);
                if (nums && nums.length > 0) dlNum[m[1]] = parseInt(nums[nums.length - 1], 10);
            }
        }
        const dlCounts = scrapeField('dl_counts', [
            { name: 'english-counter', fn: function () {
                const keys = Object.keys(dlEng);
                return keys.length > 0 ? ok(dlEng, 'english-counter') : miss('no "Number of times already downloaded" text');
            } },
            { name: 'numeric-fallback', fn: function () {
                const keys = Object.keys(dlNum);
                return keys.length > 0 ? ok(dlNum, 'numeric-fallback') : miss('no parenthesised count in page text');
            } }
        ]);
        report.push(dlCounts.entry);

        // -- account {email, label, label_source} — provenance ladder (doc 01 §4)
        const emailVal = scrapeAccountEmail();
        const labelVal = scrapeAccountLabel();
        const userParam = new URLSearchParams(location.search).get('user');
        const labelParam = new URLSearchParams(location.search).get('label');
        const account = scrapeField('account', [
            { name: 'operator-override', fn: function () {
                if (labelParam) {
                    return ok({ email: emailVal, label: labelParam, label_source: 'OPERATOR_OVERRIDE' }, 'operator-override');
                }
                return miss('no ?label= operator override');
            } },
            { name: 'scraped-email', fn: function () {
                if (emailVal) {
                    return ok({ email: emailVal, label: emailVal.split('@')[0] || null, label_source: 'SCRAPED_EMAIL' }, 'scraped-email');
                }
                return miss('no account-switcher email');
            } },
            { name: 'scraped-label', fn: function () {
                if (labelVal) {
                    return ok({ email: emailVal, label: labelVal, label_source: 'SCRAPED_LABEL' }, 'scraped-label');
                }
                return miss('no "Google Account:" display name');
            } },
            { name: 'gaia-fallback', fn: function () {
                if (userParam) {
                    return ok({ email: null, label: 'gaia-' + userParam, label_source: 'GAIA_FALLBACK' }, 'gaia-fallback');
                }
                return miss('no user= URL param');
            } },
            { name: 'unknown', fn: function () {
                return ok({ email: null, label: null, label_source: 'UNKNOWN' }, 'unknown');
            } }
        ]);
        report.push(account.entry);

        // -- user / authuser (URL params; deterministic multi-login fallback) ---
        const user = scrapeField('user', [
            { name: 'url-user-param', fn: function () {
                const v = new URLSearchParams(location.search).get('user');
                return v ? ok(v, 'url-user-param') : miss('no user= URL param');
            } },
            { name: 'button-uri-user', fn: function () {
                for (const r of rows) {
                    const m = r.uri.match(/[?&]user=([^&]+)/);
                    if (m) return ok(decodeURIComponent(m[1]), 'button-uri-user');
                }
                return miss('no user= in download URI');
            } }
        ]);
        report.push(user.entry);
        const authuser = scrapeField('authuser', [
            { name: 'url-authuser-param', fn: function () {
                const v = new URLSearchParams(location.search).get('authuser');
                return v ? ok(v, 'url-authuser-param') : ok('0', 'default-0');
            } }
        ]);
        report.push(authuser.entry);

        // -- export_ts_raw: regex over filenames, then URIs, then page text ------
        const exportTs = scrapeField('export_ts_raw', [
            { name: 'filenames-regex', fn: function () {
                const vals = [];
                for (const f of (filenames.value || [])) {
                    const m = TAKEOUT_PART_RE.exec(f);
                    if (m) vals.push(m[1]);
                }
                if (vals.length === 0) return miss('no takeout filename on page');
                const counts = {};
                for (const v of vals) counts[v] = (counts[v] || 0) + 1;
                const entries = Object.keys(counts).sort(function (a, b) { return counts[b] - counts[a]; });
                if (entries.length > 1) {
                    console.warn('[takeout-helper] multiple export timestamps among filenames:', entries.join(', '));
                    return ok(entries[0], 'filenames-regex', true);
                }
                return ok(entries[0], 'filenames-regex');
            } },
            { name: 'uri-regex', fn: function () {
                for (const r of rawRows) {
                    const m = TAKEOUT_PART_RE.exec(r.uri);
                    if (m) return ok(m[1], 'uri-regex');
                }
                return miss('no timestamp in download URIs');
            } },
            { name: 'page-text-regex', fn: function () {
                const m = /takeout-(\d{8}T\d{6}Z)-/.exec(pageText);
                return m ? ok(m[1], 'page-text-regex') : miss('no timestamp in page text');
            } }
        ]);
        report.push(exportTs.entry);

        // -- locale_warning: English dlCounts / part-X-of-N misses while buttons
        //    are clearly rendered (non-English UI).
        const englishDlHit = dlCounts.entry.ok && dlCounts.entry.source === 'english-counter';
        const englishPartsHit = parts.entry.ok && parts.entry.source === 'aria-part-of-n';
        const locale_warning = rawRows.length > 0 && (!englishDlHit || !englishPartsHit);

        const payload = {
            archive_id: archive.value,
            user: user.value,
            authuser: authuser.value,
            parts_expected: parts.value,
            uris: uris.value,
            sizes: sizes.value,
            filenames: filenames.value,
            dl_counts: dlCounts.value,
            account: account.value,
            export_ts_raw: exportTs.value,
            scrape_report: report,
            locale_warning: !!locale_warning,
            captured_at: captured_at
        };
        if (exportTs.ambiguous) payload.export_ts_ambiguous = true;
        return payload;
    }

    // Build the payload and hand it to the background for storage + POST to
    // /api/v2/capture. Free: costs zero Google download requests. The background
    // decides whether the manager is reachable. Never breaks the page.
    function sendV2Capture() {
        try {
            const payload = capturePayload();
            if (!payload || !payload.archive_id) return;
            chrome.runtime.sendMessage({ action: 'v2Capture', payload: payload, captured_at: Date.now() })
                .catch(function () {});
        } catch (e) { /* non-critical */ }
    }

    // -------------------------------------------------------------------------
    // Try to fetch the export list from Takeout's internal API.
    // Each strategy is timed and recorded so the popup can show a play-by-play.
    // -------------------------------------------------------------------------
    async function fetchExportList(capturedUrl) {
        const params = parseUrlParams(capturedUrl || location.href);
        const archiveId = params.archiveId;
        const authuser = params.authuser || '0';
        const user = params.user;
        const rapt = params.rapt;

        const strategies = []; /* { name, ok, detail, elapsedMs } */

        function addStrategy(name, ok, detail, elapsedMs) {
            strategies.push({ name, ok, detail: detail || '', elapsedMs: elapsedMs || 0 });
        }

        if (!archiveId) {
            addStrategy('pre-check', false, 'no archive ID in captured URL', 0);
            return { ok: false, error: 'no archive ID in captured URL', strategies };
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
        var t0 = performance.now();
        const buttonParts = scrapePartsFromButtons(capturedUrl);
        var t1 = performance.now();
        if (buttonParts && buttonParts.urls.length > 0) {
            const sizesKnown = (buttonParts.sizes || []).filter(function(s) { return s > 0; }).length;
            var totalSize = (buttonParts.sizes || []).reduce(function(a,b) { return a + (b||0); }, 0);
            addStrategy('page buttons', true,
                buttonParts.urls.length + ' parts, ' + sizesKnown + ' with sizes, ' +
                (totalSize > 0 ? (totalSize >= 1e9 ? (totalSize/1e9).toFixed(1)+'GB' :
                 totalSize >= 1e6 ? (totalSize/1e6).toFixed(0)+'MB' : totalSize+'B') + ' total' : 'size unknown'),
                t1 - t0);
            return {
                ok: true,
                urls: buttonParts.urls,
                debug,
                strategies: strategies,
                source: buttonParts.source,
                meta: {
                    archiveId: buttonParts.archiveId,
                    expectedParts: buttonParts.expectedParts,
                    sizes: buttonParts.sizes
                }
            };
        }
        addStrategy('page buttons', false,
            'no [data-download-uri] buttons found for this archive', t1 - t0);

        // Strategy 2: check the spy cache for URLs.
        t0 = performance.now();
        const spyUrls = Array.from(spyCache.urls).filter(u => {
            if (!u.includes('takeout-download.usercontent.google.com')) return false;
            try {
                return new URL(u).searchParams.get('j') === archiveId;
            } catch (e) {
                return false;
            }
        });
        t1 = performance.now();
        if (spyUrls.length > 0) {
            addStrategy('spy cache', true, spyUrls.length + ' URLs for this archive', t1 - t0);
            return { ok: true, urls: spyUrls, debug, strategies: strategies, source: 'spy' };
        }
        addStrategy('spy cache', false,
            'cache has ' + spyCache.urls.size + ' URLs, none for this archive', t1 - t0);

        // Strategy 3: <script class="ds:"> embedded filenames.
        t0 = performance.now();
        const dsScripts = document.querySelectorAll('script[class^="ds:"]');
        var dsFilenames = [];
        for (var si = 0; si < dsScripts.length; si++) {
            const s = dsScripts[si];
            const text = s.textContent || '';
            const m = text.match(/takeout-\d{8}T\d{6}Z-\d+-\d+\.zip/g);
            if (m) for (var fi = 0; fi < m.length; fi++) dsFilenames.push(m[fi]);
        }
        dsFilenames = Array.from(new Set(dsFilenames));
        t1 = performance.now();
        if (dsFilenames.length > 0 && user) {
            addStrategy('ds-scripts', true, dsFilenames.length + ' filenames', t1 - t0);
            const reconstructed = dsFilenames.map(function(filename) {
                const u = new URL('https://takeout-download.usercontent.google.com/download/' + filename);
                u.searchParams.set('j', archiveId);
                u.searchParams.set('i', '1');
                u.searchParams.set('user', user);
                u.searchParams.set('authuser', authuser);
                return u.toString();
            });
            return { ok: true, urls: reconstructed, debug, strategies: strategies, source: 'ds-scripts' };
        }
        addStrategy('ds-scripts', false,
            dsScripts.length + ' script(s) found, ' + dsFilenames.length + ' filenames', t1 - t0);

        // Strategy 4: spy-captured filenames (no full URLs).
        t0 = performance.now();
        if (spyCache.filenames.length > 0 && user) {
            t1 = performance.now();
            addStrategy('spy filenames', true, spyCache.filenames.length + ' filenames', t1 - t0);
            const reconstructed = spyCache.filenames.map(function(filename) {
                const u = new URL('https://takeout-download.usercontent.google.com/download/' + filename);
                u.searchParams.set('j', archiveId);
                u.searchParams.set('i', '1');
                u.searchParams.set('user', user);
                u.searchParams.set('authuser', authuser);
                return u.toString();
            });
            return { ok: true, urls: reconstructed, debug, strategies: strategies, source: 'spy-filenames' };
        }
        t1 = performance.now();
        addStrategy('spy filenames', false, spyCache.filenames.length + ' in cache', t1 - t0);

        // Strategy 5: page text filenames + button data.
        t0 = performance.now();
        const meta = scrapePageMetadata(capturedUrl);
        t1 = performance.now();
        if (meta.filenames.length > 0 && meta.archiveId && meta.user) {
            addStrategy('page text', true,
                meta.filenames.length + ' filenames, ' + meta.buttonData.length + ' buttons', t1 - t0);
            const exports = meta.filenames.map(function(filename) {
                const u = new URL('https://takeout-download.usercontent.google.com/download/' + filename);
                u.searchParams.set('j', meta.archiveId);
                u.searchParams.set('i', '1');
                u.searchParams.set('user', meta.user);
                u.searchParams.set('authuser', meta.authuser);
                return u.toString();
            });
            return { ok: true, urls: exports, debug, strategies: strategies, source: 'page-text', meta: meta };
        }
        addStrategy('page text', false,
            meta.filenames.length + ' filenames from text', t1 - t0);

        // Strategy 6: internal API endpoints.
        var apiStarts = [];
        for (var ai = 0; ai < apiUrls.length; ai++) {
            var apiUrl = apiUrls[ai];
            t0 = performance.now();
            try {
                const resp = await fetch(apiUrl, {
                    credentials: 'same-origin',
                    headers: { 'Accept': 'application/json,text/html' },
                    redirect: 'follow'
                });
                t1 = performance.now();
                if (!resp.ok) {
                    addStrategy('API: ' + apiUrl.split('?')[0], false,
                        'HTTP ' + resp.status, t1 - t0);
                    continue;
                }
                if (resp.url.includes('accounts.google.com')) {
                    addStrategy('API: ' + apiUrl.split('?')[0], false,
                        'redirected to sign-in (cookie expired)', t1 - t0);
                    return { ok: false, error: 'cookie expired', debug: debug, strategies: strategies };
                }
                const ctype = resp.headers.get('content-type') || '';
                var urls = [];
                if (ctype.includes('json')) {
                    try {
                        const data = await resp.json();
                        urls = extractUrlsFromJson(data);
                    } catch (e) {
                        addStrategy('API: ' + apiUrl.split('?')[0], false,
                            'JSON parse error: ' + e.message, t1 - t0);
                        continue;
                    }
                } else {
                    const html = await resp.text();
                    urls = extractUrlsFromHtml(html);
                }
                if (urls.length > 0) {
                    addStrategy('API: ' + apiUrl.split('?')[0], true,
                        urls.length + ' URLs', t1 - t0);
                    return { ok: true, urls: urls, debug: debug, strategies: strategies, source: apiUrl };
                }
                addStrategy('API: ' + apiUrl.split('?')[0], false,
                    'HTTP ' + resp.status + ' ' + ctype + ', 0 URLs extracted', t1 - t0);
            } catch (e) {
                t1 = performance.now();
                addStrategy('API: ' + apiUrl.split('?')[0], false,
                    'fetch error: ' + (e.message || String(e)).slice(0, 60), t1 - t0);
            }
        }

        // Last resort: DOM scrape.
        t0 = performance.now();
        const domExports = scrapeExports();
        const domUrls = domExports.map(function(e) { return e.url; });
        t1 = performance.now();
        if (domUrls.length > 0) {
            addStrategy('DOM scrape', true, domUrls.length + ' URLs', t1 - t0);
            return { ok: true, urls: domUrls, debug: debug, strategies: strategies, source: 'dom' };
        }
        addStrategy('DOM scrape', false, 'no download links in page', t1 - t0);

        return { ok: false, error: 'no strategy returned URLs', debug: debug, strategies: strategies };
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

    // The signed-in human-readable label. Google's account switcher renders
    // an aria-label like "Google Account: braincreation\n(braincreation@gmail.com)".
    // When there is no real @email (e.g. a Workspace handle), the NAME after
    // "Google Account:" and before the email/paren is the best label.
    function scrapeAccountLabel() {
        const sel = [
            '[aria-label*="Google Account"]', 'a[aria-label*="Account:"]',
            'a[aria-label*="@"]', 'a[title*="@"]', 'header [aria-label*="@"]'
        ];
        for (const s of sel) {
            for (const el of document.querySelectorAll(s)) {
                const hay = (el.getAttribute('aria-label') || '') + ' ' +
                            (el.getAttribute('title') || '');
                // Pull the chunk after "Google Account:" up to a newline,
                // open-paren, or the email itself.
                const m = hay.match(/Google Account:\s*([^\n(]+)/i);
                if (m) {
                    let name = m[1].trim();
                    // If the captured chunk IS an email, leave it for the email path.
                    if (EMAIL_RE.test(name)) {
                        name = name.replace(EMAIL_RE, '').trim();
                    }
                    name = name.replace(/[(),]/g, '').trim();
                    if (name) return name;
                }
            }
        }
        return null;
    }

    function reportAccountMeta() {
        try {
            const email = scrapeAccountEmail();
            const label = scrapeAccountLabel();
            const params = new URLSearchParams(location.search);
            const meta = {
                email: email || null,
                label: label || null,
                user: params.get('user') || null,
                authuser: params.get('authuser') || '0'
            };
            if (email || label || meta.user) {
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
        if (msg.action === 'buildCapturePayload') {
            // Tester / background 'requestPayload' — return the live payload.
            try {
                const payload = capturePayload();
                sendResponse({ ok: !!payload, payload: payload || null, at: Date.now() });
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
    // v2: structured capture payload — build on load, then every 60 s (§6.2).
    sendV2Capture();
    // setInterval(sendScrape, 5000); // DISABLED: heavy DOM scan
    setInterval(reportAccountMeta, 15000);
    setInterval(sendV2Capture, 60000);

    let scrapeTimer = null;
    /* DISABLED: heavy DOM scanning can hang renderer on large Takeout page
    const observer = new MutationObserver(() => {
        if (scrapeTimer) clearTimeout(scrapeTimer);
        scrapeTimer = setTimeout(sendScrape, 1000);
    });
    if (document.body) {
        observer.observe(document.body, { childList: true, subtree: true });
    } */
    // DISABLED

    // -------------------------------------------------------------------------
    // On-page overlay (helpers/overlay.js, docs/v2/08-SELF-DRIVING-UX.md §3).
    // overlay.js is listed BEFORE this file in the manifest, so
    // window.__tkOverlay already exists by now. It owns its own Shadow DOM,
    // streams and teardown; init() is idempotent and never throws. The guard
    // below is belt-and-braces: a missing or broken overlay must never break
    // scraping or the Takeout page itself.
    // -------------------------------------------------------------------------
    try {
        if (window.__tkOverlay && typeof window.__tkOverlay.init === 'function') {
            window.__tkOverlay.init();
        }
    } catch (e) {
        try { console.debug('[takeout-helper] overlay init failed:', e && e.message); }
        catch (_) {}
    }
})();