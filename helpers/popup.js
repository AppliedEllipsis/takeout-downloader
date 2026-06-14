// Takeout Downloader Helper - Popup Logic

// Load settings
chrome.storage.local.get([
    'serverUrl', 'authUser', 'authPass', 'outputDir',
    'parallel', 'fileCount', 'autoSend', 'hasCapture',
    'lastCapture', 'lastSendResult', 'lastSendTime'
], (data) => {
    document.getElementById('serverUrl').value = data.serverUrl || 'http://localhost:5000';
    document.getElementById('authUser').value = data.authUser || '';
    document.getElementById('authPass').value = data.authPass || '';
    document.getElementById('outputDir').value = data.outputDir || '/opt/takeout';
    document.getElementById('parallel').value = data.parallel || 6;
    document.getElementById('fileCount').value = data.fileCount || 100;
    document.getElementById('autoSend').checked = data.autoSend || false;

    // Show AUTO badge when auto-send is on (visual reminder of the silent-exfil risk)
    updateAutoBadge(data.autoSend);

    // Update badge whenever the toggle changes

    if (data.hasCapture && data.lastCapture) {
        const age = Math.round((Date.now() - data.lastCapture.timestamp) / 1000);
        const filename = data.lastCapture.url.split('/').pop() || 'unknown';
        const statusEl = document.getElementById('status');
        statusEl.textContent = `Captured: ${filename.substring(0, 25)} (${age}s ago)`;
        statusEl.className = 'status ok';
    }

    if (data.lastSendResult) {
        const statusEl = document.getElementById('status');
        if (data.lastSendResult.success) {
            statusEl.textContent = 'Last send: started successfully';
            statusEl.className = 'status ok';
        } else if (data.lastSendResult.error) {
            statusEl.textContent = 'Last send error: ' + data.lastSendResult.error;
            statusEl.className = 'status err';
        }
    }

    // Check for pending remote confirmation
    if (data.pendingRemoteConfirmation) {
        showRemoteConfirmDialog(data.pendingRemoteConfirmation);
    }
});

function showRemoteConfirmDialog(pending) {
    const msg = 'This is the FIRST capture being sent to a remote server:\n\n' +
                'Server: ' + pending.serverUrl + '\n\n' +
                'A remote server can see your Google session cookie.\n\n' +
                'Click OK to allow sends to this server for this session,\n' +
                'or Cancel to deny.';
    if (confirm(msg)) {
        chrome.runtime.sendMessage({ action: 'confirmRemote' }, () => {});
    } else {
        chrome.runtime.sendMessage({ action: 'denyRemote' }, () => {});
    }
}

// Save settings on change
['serverUrl', 'authUser', 'authPass', 'outputDir', 'parallel', 'fileCount'].forEach(id => {
    document.getElementById(id).addEventListener('change', () => {
        chrome.storage.local.set({
            serverUrl: document.getElementById('serverUrl').value,
            authUser: document.getElementById('authUser').value,
            authPass: document.getElementById('authPass').value,
            outputDir: document.getElementById('outputDir').value,
            parallel: parseInt(document.getElementById('parallel').value) || 6,
            fileCount: parseInt(document.getElementById('fileCount').value) || 100,
            autoSend: document.getElementById('autoSend').checked
        });
    });
});

document.getElementById('autoSend').addEventListener('change', () => {
    const checked = document.getElementById('autoSend').checked;
    chrome.storage.local.set({ autoSend: checked });
    updateAutoBadge(checked);
});

function updateAutoBadge(isOn) {
    const badge = document.getElementById('autoBadge');
    if (badge) badge.style.display = isOn ? 'inline' : 'none';
}

// Send capture to server
function sendCapture() {
    const statusEl = document.getElementById('status');
    statusEl.textContent = 'Sending...';
    statusEl.className = 'status warn';

    // Save current settings first
    chrome.storage.local.set({
        serverUrl: document.getElementById('serverUrl').value,
        authUser: document.getElementById('authUser').value,
        authPass: document.getElementById('authPass').value,
        outputDir: document.getElementById('outputDir').value,
        parallel: parseInt(document.getElementById('parallel').value) || 6,
        fileCount: parseInt(document.getElementById('fileCount').value) || 100
    }, () => {
        chrome.runtime.sendMessage({ action: 'sendCapture' }, (response) => {
            statusEl.textContent = 'Sent! Check server for status.';
            statusEl.className = 'status ok';
        });
    });
}

// Update cookie on server
function updateCookie() {
    const statusEl = document.getElementById('status');
    statusEl.textContent = 'Updating cookie...';
    statusEl.className = 'status warn';

    chrome.storage.local.set({
        serverUrl: document.getElementById('serverUrl').value,
        authUser: document.getElementById('authUser').value,
        authPass: document.getElementById('authPass').value,
    }, () => {
        chrome.runtime.sendMessage({ action: 'updateCookie' }, (response) => {
            if (response && response.error) {
                statusEl.textContent = 'Error: ' + response.error;
                statusEl.className = 'status err';
            } else {
                statusEl.textContent = 'Cookie updated!';
                statusEl.className = 'status ok';
            }
        });
    });
}
