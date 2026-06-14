// Takeout Downloader Helper - Background Service Worker
// Captures takeout download requests and stores them for the popup

// Default configuration
const DEFAULTS = {
    serverUrl: 'http://localhost:5000',
    authUser: '',
    authPass: '',
    autoSend: false,
    outputDir: '/opt/takeout',
    parallel: 6,
    fileCount: 100
};

// SECURITY: Validate serverUrl before sending data anywhere.
// Refuse non-HTTP schemes (javascript:, file:, data:, etc.)
// Warn (do not block) on non-localhost to avoid silent exfiltration.
function isSafeServerUrl(url) {
    if (!url || typeof url !== 'string') return false;
    try {
        const u = new URL(url);
        if (u.protocol !== 'http:' && u.protocol !== 'https:') return false;
        return true;
    } catch (e) {
        return false;
    }
}

function isLocalhostUrl(url) {
    try {
        const u = new URL(url);
        const h = u.hostname.toLowerCase();
        return h === 'localhost' || h === '127.0.0.1' || h === '::1';
    } catch (e) {
        return false;
    }
}

// Initialize storage
chrome.storage.local.get(DEFAULTS, (settings) => {
    chrome.storage.local.set(settings);
});

// Intercept takeout download requests
chrome.webRequest.onBeforeSendHeaders.addListener(
    (details) => {
        const url = details.url;
        if (url.includes('takeout') || url.includes('storage.cloud.google.com')) {
            // Extract cookie from headers
            const cookieHeader = details.requestHeaders
                .find(h => h.name.toLowerCase() === 'cookie');
            const cookie = cookieHeader ? cookieHeader.value : '';

            // Store the captured request
            const capture = {
                url: url,
                cookie: cookie,
                method: details.method,
                timestamp: Date.now()
            };

            chrome.storage.local.set({ lastCapture: capture, hasCapture: true });

            // Auto-send if enabled
            chrome.storage.local.get(['autoSend', 'serverUrl', 'authUser', 'authPass'], (settings) => {
                if (settings.autoSend) {
                    sendToServer(capture, settings);
                }
            });
        }
    },
    { urls: ['https://takeout.google.com/*', 'https://storage.cloud.google.com/takeout*'] },
    ['requestHeaders']
);

// Send captured data to the downloader server
function sendToServer(capture, settings) {
    const serverUrl = settings.serverUrl || DEFAULTS.serverUrl;

    // SECURITY: refuse to send if serverUrl is malformed or non-HTTP
    if (!isSafeServerUrl(serverUrl)) {
        const err = 'Refusing to send: serverUrl is invalid or non-HTTP. Open extension Options to fix.';
        chrome.storage.local.set({
            lastSendResult: { error: err },
            lastSendTime: Date.now()
        });
        chrome.notifications.create({
            type: 'basic',
            iconUrl: 'icon48.png',
            title: 'Takeout Downloader - blocked',
            message: err
        });
        return;
    }

    // SECURITY: require explicit confirmation for non-localhost serverUrls
    // when sending for the first time per session.
    // MV3 service workers can't use window.confirm(), so we set a pending
    // flag and surface the confirmation in the popup (which has DOM access).
    if (!isLocalhostUrl(serverUrl)) {
        chrome.storage.local.get(['confirmedRemoteHost'], (data) => {
            if (data.confirmedRemoteHost === serverUrl) {
                doSendToServer(capture, settings);
            } else {
                // Save the pending capture and notify the user to confirm in the popup
                chrome.storage.local.set({
                    pendingRemoteConfirmation: {
                        capture: capture,
                        serverUrl: serverUrl,
                        timestamp: Date.now()
                    },
                    lastSendResult: {
                        error: 'Remote server not yet confirmed. Open the extension popup to allow sends to ' + serverUrl
                    },
                    lastSendTime: Date.now()
                });
                chrome.notifications.create({
                    type: 'basic',
                    iconUrl: 'icon48.png',
                    title: 'Takeout Downloader - confirm required',
                    message: 'New remote server: ' + serverUrl + '. Click the extension icon to allow sends.'
                });
                // Set badge to draw attention
                chrome.action.setBadgeText({ text: '!' });
                chrome.action.setBadgeBackgroundColor({ color: '#f59e0b' });
            }
        });
        return;
    }

    doSendToServer(capture, settings);
}

function doSendToServer(capture, settings) {
    const serverUrl = settings.serverUrl || DEFAULTS.serverUrl;
    const headers = { 'Content-Type': 'application/json' };

    if (settings.authUser) {
        headers['Authorization'] = 'Basic ' + btoa(settings.authUser + ':' + settings.authPass);
    }

    // Build synthetic cURL
    const curlText = `curl '${capture.url}' -H 'Cookie: ${capture.cookie}'`;

    fetch(serverUrl + '/api/start', {
        method: 'POST',
        headers: headers,
        body: JSON.stringify({
            curl_input: curlText,
            url: capture.url,
            output_dir: settings.outputDir || DEFAULTS.outputDir,
            parallel: settings.parallel || DEFAULTS.parallel,
            file_count: settings.fileCount || DEFAULTS.fileCount
        })
    })
    .then(r => r.json())
    .then(data => {
        chrome.storage.local.set({
            lastSendResult: data.error ? { error: data.error } : { success: true },
            lastSendTime: Date.now()
        });
    })
    .catch(err => {
        chrome.storage.local.set({
            lastSendResult: { error: 'Connection failed: ' + err.message },
            lastSendTime: Date.now()
        });
    });
}

// Listen for messages from popup
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.action === 'sendCapture') {
        chrome.storage.local.get([
            'lastCapture', 'serverUrl', 'authUser', 'authPass',
            'outputDir', 'parallel', 'fileCount'
        ], (settings) => {
            sendToServer(settings.lastCapture, settings);
            sendResponse({ status: 'sent' });
        });
        return true; // async
    }
    if (msg.action === 'updateCookie') {
        chrome.storage.local.get(['lastCapture', 'serverUrl', 'authUser', 'authPass'], (settings) => {
            const serverUrl = settings.serverUrl || DEFAULTS.serverUrl;
            if (!isSafeServerUrl(serverUrl)) {
                sendResponse({ error: 'serverUrl is invalid' });
                return;
            }
            const headers = { 'Content-Type': 'application/json' };
            if (settings.authUser) {
                headers['Authorization'] = 'Basic ' + btoa(settings.authUser + ':' + settings.authPass);
            }
            const curlText = `curl '${settings.lastCapture.url}' -H 'Cookie: ${settings.lastCapture.cookie}'`;
            fetch(serverUrl + '/api/update-cookie', {
                method: 'POST',
                headers: headers,
                body: JSON.stringify({ curl_input: curlText, url: settings.lastCapture.url })
            })
            .then(r => r.json())
            .then(data => sendResponse(data))
            .catch(err => sendResponse({ error: err.message }));
            return true; // async
        });
    }
    if (msg.action === 'confirmRemote') {
        // User confirmed in the popup that it's OK to send to the remote server
        chrome.storage.local.get([
            'pendingRemoteConfirmation', 'serverUrl', 'authUser', 'authPass',
            'outputDir', 'parallel', 'fileCount'
        ], (data) => {
            const pending = data.pendingRemoteConfirmation;
            if (!pending) {
                sendResponse({ error: 'No pending confirmation' });
                return;
            }
            chrome.storage.local.set({
                confirmedRemoteHost: pending.serverUrl,
                pendingRemoteConfirmation: null
            }, () => {
                chrome.action.setBadgeText({ text: '' });
                const settings = {
                    serverUrl: pending.serverUrl,
                    authUser: data.authUser,
                    authPass: data.authPass,
                    outputDir: data.outputDir,
                    parallel: data.parallel,
                    fileCount: data.fileCount
                };
                doSendToServer(pending.capture, settings);
                sendResponse({ status: 'confirmed and sent' });
            });
        });
        return true; // async
    }
    if (msg.action === 'denyRemote') {
        // User denied — clear the pending flag, keep capture so they can retry manually
        chrome.storage.local.set({
            pendingRemoteConfirmation: null,
            lastSendResult: { error: 'Remote server denied. Update serverUrl or manually click Send.' },
            lastSendTime: Date.now()
        }, () => {
            chrome.action.setBadgeText({ text: '' });
            sendResponse({ status: 'denied' });
        });
        return true; // async
    }
});
