// Takeout Helper - Options Page Logic

function showStatus(text, ok) {
    const el = document.getElementById('status');
    el.textContent = text;
    el.className = 'status ' + (ok ? 'ok' : 'err');
}

document.addEventListener('DOMContentLoaded', () => {
    const autoCopyEl = document.getElementById('autoCopy');
    const badgeEl = document.getElementById('badgeFilename');

    // Load
    chrome.runtime.sendMessage({ action: 'getPreferences' }, (prefs) => {
        autoCopyEl.checked = !!prefs?.autoCopy;
        badgeEl.checked = prefs?.badgeFilename !== false;
    });

    function save() {
        chrome.runtime.sendMessage({
            action: 'setPreference',
            key: 'autoCopy',
            value: autoCopyEl.checked
        }, () => {
            chrome.runtime.sendMessage({
                action: 'setPreference',
                key: 'badgeFilename',
                value: badgeEl.checked
            }, () => {
                showStatus('Settings saved.', true);
                setTimeout(() => {
                    const el = document.getElementById('status');
                    el.style.display = 'none';
                }, 1500);
            });
        });
    }

    autoCopyEl.addEventListener('change', save);
    badgeEl.addEventListener('change', save);
});
