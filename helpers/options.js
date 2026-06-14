// Takeout Helper - Options Page Logic

chrome.storage.local.get([
    'serverUrl', 'authUser', 'authPass', 'outputDir', 'parallel', 'fileCount'
], (data) => {
    document.getElementById('serverUrl').value = data.serverUrl || 'http://localhost:5000';
    document.getElementById('authUser').value = data.authUser || '';
    document.getElementById('authPass').value = data.authPass || '';
    document.getElementById('outputDir').value = data.outputDir || '/opt/takeout';
    document.getElementById('parallel').value = data.parallel || 6;
    document.getElementById('fileCount').value = data.fileCount || 100;
});

function save() {
    const statusEl = document.getElementById('status');
    chrome.storage.local.set({
        serverUrl: document.getElementById('serverUrl').value,
        authUser: document.getElementById('authUser').value,
        authPass: document.getElementById('authPass').value,
        outputDir: document.getElementById('outputDir').value,
        parallel: parseInt(document.getElementById('parallel').value) || 6,
        fileCount: parseInt(document.getElementById('fileCount').value) || 100
    }, () => {
        statusEl.textContent = 'Settings saved!';
        statusEl.className = 'status ok';
    });
}
