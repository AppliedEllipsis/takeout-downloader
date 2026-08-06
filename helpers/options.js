// Takeout Helper - Options Page Logic

function showStatus(text, ok) {
    const el = document.getElementById('status');
    el.textContent = text;
    el.className = 'status ' + (ok ? 'ok' : 'err');
}

document.addEventListener('DOMContentLoaded', () => {
    const els = {
        autoCopy: document.getElementById('autoCopy'),
        badgeFilename: document.getElementById('badgeFilename'),
        autoPost: document.getElementById('autoPost'),
        autoRecapture: document.getElementById('autoRecapture'),
        managerUrl: document.getElementById('managerUrl'),
        captureToken: document.getElementById('captureToken'),
        openMonitorBtn: document.getElementById('openMonitorBtn'),
    };

    // Resolve the manager base URL the same way popup.js does: the saved
    // setting, else the localhost default the webtop uses in production.
    function resolveManagerUrl() {
        const raw = (els.managerUrl && els.managerUrl.value.trim()) || '';
        return raw || 'http://127.0.0.1:8080';
    }

    if (els.openMonitorBtn) {
        // Open the live monitor page ({managerUrl}/ui/monitor.html) in a new
        // tab so the operator watches a multi-day download without keeping
        // this extension page open.
        els.openMonitorBtn.addEventListener('click', () => {
            const base = resolveManagerUrl().replace(/\/+$/, '');
            chrome.tabs.create({ url: base + '/ui/monitor.html' });
        });
    }

    // Load from storage.local directly (these are manager-level settings, not
    // per-badge preferences saved via background.js).
    chrome.storage.local.get(
        ['autoCopy','badgeFilename','autoPost','autoRecapture','managerUrl','captureToken'],
        (d) => {
            els.autoCopy.checked = d.autoCopy !== false;
            els.badgeFilename.checked = d.badgeFilename !== false;
            if (els.autoPost) els.autoPost.checked = d.autoPost !== false;
            if (els.autoRecapture) els.autoRecapture.checked = d.autoRecapture !== false;
            if (els.managerUrl) els.managerUrl.value = d.managerUrl || '';
            if (els.captureToken) els.captureToken.value = d.captureToken || '';
        }
    );

    function save() {
        const data = {
            autoCopy: els.autoCopy.checked,
            badgeFilename: els.badgeFilename.checked,
        };
        if (els.autoPost) data.autoPost = els.autoPost.checked;
        if (els.autoRecapture) data.autoRecapture = els.autoRecapture.checked;
        if (els.managerUrl) data.managerUrl = els.managerUrl.value.trim();
        if (els.captureToken) data.captureToken = els.captureToken.value.trim();
        chrome.storage.local.set(data, () => {
            showStatus('Settings saved.', true);
            setTimeout(() => {
                const el = document.getElementById('status');
                el.style.display = 'none';
            }, 1500);
        });
    }

    els.autoCopy.addEventListener('change', save);
    els.badgeFilename.addEventListener('change', save);
    if (els.autoPost) els.autoPost.addEventListener('change', save);
    if (els.autoRecapture) els.autoRecapture.addEventListener('change', save);
    if (els.managerUrl) els.managerUrl.addEventListener('input', save);
    if (els.captureToken) els.captureToken.addEventListener('input', save);
});
