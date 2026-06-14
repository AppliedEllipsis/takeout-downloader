// Takeout Downloader Bookmarklet - source
// Minified into a javascript: URL for bookmarklet.html
// SECURITY: validates serverUrl (http/https only), includes URL in cURL,
// and uses /opt/takeout as default output dir.

(function() {
    var s = prompt('Takeout Downloader Server URL:', 'http://localhost:5000');
    if (!s) return;
    try {
        var u = new URL(s);
        if (u.protocol !== 'http:' && u.protocol !== 'https:') {
            alert('Invalid server URL. Must start with http:// or https://');
            return;
        }
    } catch (e) {
        alert('Invalid server URL.');
        return;
    }
    var c = document.cookie;
    var l = document.querySelectorAll('a[href*="takeout"],a[href*="storage.cloud.google.com"]');
    var dl = l.length ? l[0].href : prompt('Paste takeout download URL:');
    if (!dl) return;
    var au = prompt('Auth user (blank to skip):');
    var ap = au ? prompt('Auth password:') : '';
    var h = {'Content-Type': 'application/json'};
    if (au) h['Authorization'] = 'Basic ' + btoa(au + ':' + ap);
    var curl = "curl '" + dl + "' -H 'Cookie: " + c + "'";
    fetch(s + '/api/start', {
        method: 'POST',
        headers: h,
        body: JSON.stringify({
            curl_input: curl,
            url: dl,
            output_dir: '/opt/takeout',
            parallel: 6,
            file_count: 100
        })
    })
    .then(function(r) { return r.json(); })
    .then(function(d) { alert(d.error || 'Download started!'); })
    .catch(function(e) { alert('Error: ' + e); });
})();
