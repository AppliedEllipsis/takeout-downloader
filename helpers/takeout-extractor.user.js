// ==UserScript==
// @name         Google Takeout cURL Extractor
// @namespace    https://github.com/takeout-downloader
// @version      1.0.0
// @description  Auto-extracts cURL commands from Google Takeout download requests and sends them to your downloader server
// @author       Takeout Downloader
// @match        https://takeout.google.com/*
// @match        https://storage.cloud.google.com/takeout*
// @grant        GM_xmlhttpRequest
// @grant        GM_notification
// @grant        GM_getValue
// @grant        GM_setValue
// @connect      localhost
// @run-at       document-idle
// ==/UserScript==

(function() {
    'use strict';

    // =========================================================================
    // CONFIGURATION - Edit these or set via the script settings panel
    // =========================================================================
    const DEFAULT_SERVER = 'http://localhost:5000';

    // SECURITY: validate serverUrl before sending
    function isSafeServerUrl(url) {
        if (!url || typeof url !== 'string') return false;
        try {
            const u = new URL(url);
            return u.protocol === 'http:' || u.protocol === 'https:';
        } catch (e) { return false; }
    }

    function getServerUrl() {
        return GM_getValue('serverUrl', DEFAULT_SERVER);
    }
    function getAuthUser() {
        return GM_getValue('authUser', '');
    }
    function getAuthPass() {
        return GM_getValue('authPass', '');
    }

    // =========================================================================
    // UI INJECTION
    // =========================================================================
    function injectPanel() {
        const panel = document.createElement('div');
        panel.id = 'takeout-helper-panel';
        panel.innerHTML = ''; // Will use DOM methods below
        panel.style.cssText = `
            position: fixed; bottom: 20px; right: 20px; z-index: 99999;
            background: #1e1e2e; border: 2px solid #7c3aed; border-radius: 12px;
            padding: 16px; color: #f8fafc; font-family: system-ui, sans-serif;
            font-size: 14px; max-width: 360px; box-shadow: 0 4px 20px rgba(0,0,0,0.5);
            display: none;
        `;

        const title = document.createElement('h3');
        title.textContent = '📦 Takeout Downloader Helper';
        title.style.cssText = 'margin: 0 0 12px 0; color: #a855f7;';
        panel.appendChild(title);

        // Server URL input
        const serverLabel = document.createElement('label');
        serverLabel.textContent = 'Server URL:';
        serverLabel.style.cssText = 'display: block; margin-bottom: 4px; color: #94a3b8; font-size: 12px;';
        panel.appendChild(serverLabel);

        const serverInput = document.createElement('input');
        serverInput.type = 'text';
        serverInput.value = getServerUrl();
        serverInput.id = 'takeout-server-url';
        serverInput.style.cssText = 'width: 100%; padding: 8px; margin-bottom: 10px; background: #2d2d3f; border: 1px solid #4b5563; border-radius: 6px; color: #f8fafc; box-sizing: border-box;';
        serverInput.addEventListener('change', () => {
            GM_setValue('serverUrl', serverInput.value);
        });
        panel.appendChild(serverInput);

        // Status display
        const status = document.createElement('div');
        status.id = 'takeout-helper-status';
        status.textContent = 'Monitoring network requests...';
        status.style.cssText = 'padding: 8px; background: #2d2d3f; border-radius: 6px; margin-bottom: 10px; font-size: 12px;';
        panel.appendChild(status);

        // Send button
        const sendBtn = document.createElement('button');
        sendBtn.textContent = '📤 Send Last cURL to Server';
        sendBtn.style.cssText = `
            width: 100%; padding: 10px; background: #7c3aed; color: white;
            border: none; border-radius: 6px; cursor: pointer; font-weight: 600;
            margin-bottom: 8px;
        `;
        sendBtn.addEventListener('click', sendLastCurl);
        panel.appendChild(sendBtn);

        // Auto-send toggle
        const autoLabel = document.createElement('label');
        autoLabel.style.cssText = 'display: flex; align-items: center; gap: 8px; font-size: 12px; color: #94a3b8; cursor: pointer;';
        const autoCheck = document.createElement('input');
        autoCheck.type = 'checkbox';
        autoCheck.checked = GM_getValue('autoSend', false);
        autoCheck.addEventListener('change', () => GM_setValue('autoSend', autoCheck.checked));
        autoLabel.appendChild(autoCheck);
        autoLabel.appendChild(document.createTextNode('Auto-send on detection'));
        panel.appendChild(autoLabel);

        // Toggle button (floating)
        const toggleBtn = document.createElement('button');
        toggleBtn.textContent = '📦';
        toggleBtn.title = 'Toggle Takeout Helper';
        toggleBtn.style.cssText = `
            position: fixed; bottom: 20px; right: 20px; z-index: 99998;
            width: 48px; height: 48px; border-radius: 50%; border: none;
            background: #7c3aed; color: white; font-size: 20px; cursor: pointer;
            box-shadow: 0 2px 10px rgba(0,0,0,0.3);
        `;
        toggleBtn.addEventListener('click', () => {
            panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
        });

        document.body.appendChild(panel);
        document.body.appendChild(toggleBtn);
    }

    // =========================================================================
    // NETWORK REQUEST INTERCEPTION
    // =========================================================================
    let lastCurlData = null;
    let capturedCookies = '';

    // Intercept XMLHttpRequest to capture takeout download requests
    const origOpen = XMLHttpRequest.prototype.open;
    const origSend = XMLHttpRequest.prototype.send;
    const origSetHeader = XMLHttpRequest.prototype.setRequestHeader;

    XMLHttpRequest.prototype.open = function(method, url, ...args) {
        this._takeoutUrl = url;
        this._takeoutHeaders = {};
        return origOpen.apply(this, [method, url, ...args]);
    };

    XMLHttpRequest.prototype.setRequestHeader = function(name, value) {
        if (this._takeoutHeaders) {
            this._takeoutHeaders[name] = value;
        }
        return origSetHeader.apply(this, [name, value]);
    };

    XMLHttpRequest.prototype.send = function(body) {
        const url = this._takeoutUrl || '';
        if (url.includes('takeout') || url.includes('storage.cloud.google.com')) {
            const cookie = this._takeoutHeaders?.['Cookie'] || document.cookie || '';
            lastCurlData = {
                url: url,
                cookie: cookie,
                method: (this._takeoutMethod || 'GET'),
                timestamp: Date.now()
            };

            const statusEl = document.getElementById('takeout-helper-status');
            if (statusEl) {
                const filename = url.split('/').pop() || url;
                statusEl.textContent = `✓ Captured: ${filename.substring(0, 30)}`;
                statusEl.style.color = '#22c55e';
            }

            if (GM_getValue('autoSend', false)) {
                sendLastCurl();
            }
        }
        return origSend.apply(this, [body]);
    };

    // Also capture fetch() calls
    const origFetch = window.fetch;
    window.fetch = function(input, init) {
        const url = (typeof input === 'string') ? input : input?.url || '';
        if (url.includes('takeout') || url.includes('storage.cloud.google.com')) {
            const cookie = init?.headers?.get?.('Cookie') || init?.headers?.Cookie || document.cookie || '';
            lastCurlData = {
                url: url,
                cookie: cookie,
                method: init?.method || 'GET',
                timestamp: Date.now()
            };

            const statusEl = document.getElementById('takeout-helper-status');
            if (statusEl) {
                const filename = url.split('/').pop() || url;
                statusEl.textContent = `✓ Captured: ${filename.substring(0, 30)}`;
                statusEl.style.color = '#22c55e';
            }

            if (GM_getValue('autoSend', false)) {
                sendLastCurl();
            }
        }
        return origFetch.apply(this, [input, init]);
    };

    // =========================================================================
    // SEND TO SERVER
    // =========================================================================
    function sendLastCurl() {
        if (!lastCurlData) {
            GM_notification({title: 'Takeout Helper', text: 'No takeout request captured yet. Click a download link first.'});
            return;
        }

        const serverUrl = getServerUrl();

        // SECURITY: refuse invalid or non-HTTP URLs
        if (!isSafeServerUrl(serverUrl)) {
            GM_notification({
                title: 'Takeout Helper - blocked',
                text: 'serverUrl is invalid. Open userscript settings to fix.'
            });
            return;
        }

        const authUser = getAuthUser();
        const authPass = getAuthPass();
        const headers = { 'Content-Type': 'application/json' };
        if (authUser) {
            headers['Authorization'] = 'Basic ' + btoa(`${authUser}:${authPass}`);
        }

        // Build a synthetic cURL command from captured data
        const curlText = `curl '${lastCurlData.url}' -H 'Cookie: ${lastCurlData.cookie}'`;

        const statusEl = document.getElementById('takeout-helper-status');
        if (statusEl) {
            statusEl.textContent = 'Sending to server...';
            statusEl.style.color = '#f59e0b';
        }

        GM_xmlhttpRequest({
            method: 'POST',
            url: `${serverUrl}/api/start`,
            headers: headers,
            data: JSON.stringify({
                curl_input: curlText,
                url: lastCurlData.url,
                output_dir: '/opt/takeout',
                parallel: 6,
                file_count: 100
            }),
            onload: function(response) {
                try {
                    const data = JSON.parse(response.responseText);
                    if (data.error) {
                        if (statusEl) {
                            statusEl.textContent = `Error: ${data.error}`;
                            statusEl.style.color = '#ef4444';
                        }
                        GM_notification({title: 'Takeout Helper', text: `Error: ${data.error}`});
                    } else {
                        if (statusEl) {
                            statusEl.textContent = 'Download started on server!';
                            statusEl.style.color = '#22c55e';
                        }
                        GM_notification({title: 'Takeout Helper', text: 'Download started on server!'});
                    }
                } catch (e) {
                    if (statusEl) {
                        statusEl.textContent = `Parse error: ${e.message}`;
                        statusEl.style.color = '#ef4444';
                    }
                }
            },
            onerror: function(error) {
                if (statusEl) {
                    statusEl.textContent = `Connection failed - is the server running?`;
                    statusEl.style.color = '#ef4444';
                }
                GM_notification({title: 'Takeout Helper', text: 'Connection failed - is the server running?'});
            }
        });
    }

    // =========================================================================
    // INIT
    // =========================================================================
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', injectPanel);
    } else {
        injectPanel();
    }
})();
