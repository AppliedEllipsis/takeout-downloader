// Takeout Downloader Helper - Background Service Worker
// Captures takeout download requests and stores them for the popup

// Default configuration
const DEFAULTS = {
    serverUrl: 'http://localhost:5000',
    authUser: '',
    authPass: '',
    autoSend: false,
    outputDir: '/downloads',
    parallel: 6,
    fileCount: 100
};

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
});
