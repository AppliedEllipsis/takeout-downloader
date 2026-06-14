// Content script for takeout.google.com — scrapes all download links
// from the export page and sends them to the background script.

(function () {
    'use strict';

    const FINAL_HOST = 'takeout-download.usercontent.google.com';
    const ARCHIVE_PATH_RE = /\/manage\/archive\//;

    // -----------------------------------------------------------------------
    // Export list (what the user sees on the manage page)
    // -----------------------------------------------------------------------
    function scrapeExports() {
        const exports = [];
        const seen = new Set();

        // Strategy 1: Find all <a> tags that look like takeout download links.
        // On the manage page, Google renders each export as a card with a
        // "Download" link.
        const links = document.querySelectorAll('a[href]');
        for (const link of links) {
            const url = link.href || '';
            // The actual download links point to the final host.
            if (url.includes('takeout-download.usercontent.google.com') &&
                url.includes('.zip')) {
                const filename = url.split('?')[0].split('/').pop();
                if (seen.has(filename)) continue;
                seen.add(filename);
                exports.push({ url, filename, text: link.textContent.trim() });
            }
        }

        // Strategy 2: The page may embed download URLs in data attributes or
        // onclick handlers. Scan all elements for attributes that contain
        // takeout-download URLs.
        const allEls = document.querySelectorAll('*');
        for (const el of allEls) {
            for (const attr of el.attributes || []) {
                const val = attr.value || '';
                if (val.includes('takeout-download') && val.includes('.zip')) {
                    const filename = val.split('?')[0].split('/').pop();
                    if (seen.has(filename)) continue;
                    seen.add(filename);
                    exports.push({ url: val, filename, text: el.textContent.trim() });
                }
            }
        }

        // Strategy 3: Look at the raw HTML for zip filenames. This catches
        // cases where the URL is hidden in JS or template data.
        const html = document.documentElement.innerHTML;
        const zipMatches = html.match(/takeout-\d{8}T\d{6}Z-\d+-\d{3}\.zip[^\s<"']*/g);
        if (zipMatches) {
            for (const match of zipMatches) {
                const filename = match.split('?')[0];
                if (seen.has(filename)) continue;
                seen.add(filename);
                // We don't have the full URL yet — it will be captured when
                // the user clicks the download button (webRequest sees it).
                exports.push({ url: null, filename, text: filename });
            }
        }

        return exports;
    }

    // -----------------------------------------------------------------------
    // Sizes: extract "155.3 MB", "1.17 GB", etc. from the page text
    // -----------------------------------------------------------------------
    function scrapeSizes() {
        const sizes = {};
        const walker = document.createTreeWalker(
            document.body, NodeFilter.SHOW_TEXT, null, false
        );
        let node;
        while ((node = walker.nextNode())) {
            const text = node.textContent.trim();
            const sizeMatch = text.match(/(\d+\.?\d*)\s*(MB|GB|KB)/);
            if (sizeMatch) {
                let el = node.parentElement;
                for (let i = 0; i < 3 && el; i++) {
                    const elText = el.textContent;
                    const partMatch = elText.match(/Part\s+(\d+)/);
                    if (partMatch) {
                        sizes[partMatch[1]] = {
                            value: parseFloat(sizeMatch[1]),
                            unit: sizeMatch[2],
                            raw: text
                        };
                        break;
                    }
                    el = el.parentElement;
                }
            }
        }
        return sizes;
    }

    // -----------------------------------------------------------------------
    // Intercept clicks on download buttons to capture URLs before navigation
    // -----------------------------------------------------------------------
    function interceptClicks() {
        document.addEventListener('click', (e) => {
            const el = e.target.closest('a, button, [role="button"]');
            if (!el) return;
            const text = (el.textContent || el.innerText || '').toLowerCase();
            if (!text.includes('download') && !text.includes('export')) return;

            // Try to extract a URL from the element or its ancestors
            let url = el.href || el.getAttribute('data-url') || '';
            let parent = el.parentElement;
            for (let i = 0; i < 3 && parent && !url; i++) {
                url = parent.getAttribute('data-url') || '';
                parent = parent.parentElement;
            }

            if (url && url.includes('takeout')) {
                chrome.runtime.sendMessage({
                    action: 'clickIntercept',
                    url,
                    text: el.textContent.trim(),
                    timestamp: new Date().toISOString()
                });
            }
        }, true);
    }

    // -----------------------------------------------------------------------
    // Send scraped data to background script
    // -----------------------------------------------------------------------
    function sendData() {
        const exports = scrapeExports();
        const sizes = scrapeSizes();
        if (exports.length > 0) {
            chrome.runtime.sendMessage({
                action: 'pageScrape',
                exports,
                sizes,
                url: window.location.href,
                timestamp: new Date().toISOString()
            });
        }
    }

    // -----------------------------------------------------------------------
    // Boot
    // -----------------------------------------------------------------------
    interceptClicks();
    sendData();

    // Re-scan periodically (some pages load content lazily)
    setInterval(sendData, 2000);

    // Also watch for DOM changes
    const observer = new MutationObserver(() => {
        clearTimeout(window._takeoutScrapeTimer);
        window._takeoutScrapeTimer = setTimeout(sendData, 500);
    });
    observer.observe(document.body, { childList: true, subtree: true });
})();
