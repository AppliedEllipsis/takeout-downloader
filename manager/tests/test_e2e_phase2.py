"""End-to-end Phase 2 test: fake Takeout server -> manager -> download -> manifest.

Runs the FastAPI app in-process via TestClient, POSTs a multi-export payload
pointing at a local fake-Takeout HTTP server that serves valid ZIP bytes, and
asserts:
  - the job completes,
  - files land under <root>/google-takeout/<account>/<export-ts>/,
  - manifest.json records files with sizes + times.
"""
import io
import json
import os
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Build a tiny valid zip to serve as each "part".
def _make_zip_bytes(n: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("data.txt", "x" * n)
    return buf.getvalue()

ZIP_BODY = _make_zip_bytes(2048)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _serve(self):
        # Honor Range so resume logic is exercised; here just serve full body.
        body = ZIP_BODY
        rng = self.headers.get("Range")
        start = 0
        if rng and rng.startswith("bytes="):
            try:
                start = int(rng.split("=")[1].split("-")[0])
            except (ValueError, IndexError):
                start = 0
        chunk = body[start:]
        if start > 0:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(body)-1}/{len(body)}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        self.wfile.write(chunk)

    def do_GET(self):
        self._serve()

def main():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    root = tempfile.mkdtemp(prefix="mgr-e2e-")
    os.environ["STORAGE_ROOT"] = root
    os.environ["TAKEOUT_SUBDIR"] = "google-takeout"
    os.environ["MANAGER_CAPTURE_TOKEN"] = ""  # open for the test
    os.environ["MANAGER_API_TOKEN"] = ""
    os.environ["TELEGRAM_ENABLED"] = "false"

    # Allow the temp root as an output prefix for the engine's validator.
    os.environ["ALLOWED_DIRS"] = root

    from fastapi.testclient import TestClient
    import manager.app as app_mod
    # Reset singletons so env vars are picked up.
    import manager.config as cfg
    cfg._cfg = None
    import manager.orchestrator as orch
    orch._orch = None
    app_mod.orch = orch.get_orchestrator()

    client = TestClient(app_mod.app)

    base = f"http://127.0.0.1:{port}/download"
    parts = []
    ts = "20260616T040104Z"
    for i in range(3):
        parts.append({
            "url": f"{base}/takeout-{ts}-1-{i:03d}.zip?j=ARCHIVE123&i={i}&user=1153&authuser=0",
            "filename": f"takeout-{ts}-1-{i:03d}.zip",
            "size": len(ZIP_BODY),
            "partIndex": i,
        })
    payload = {
        "schema": 1,
        "multi": True,
        "captured_at": "2026-06-16T15:45:00.000Z",
        "source": "extension",
        "cookie": "__Secure-1PSID=fakevalue; SID=foo",
        "url": parts[0]["url"],
        "headers": {"User-Agent": "test", "Referer": "https://takeout.google.com/"},
        "exports": parts,
        "meta": {"email": "braincreation@gmail.com", "user": "1153", "authuser": "0",
                 "archive_id": "ARCHIVE123"},
    }

    r = client.post("/api/payload", json=payload)
    print("POST /api/payload ->", r.status_code, r.json())
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    # Poll until complete.
    deadline = time.time() + 30
    status = None
    while time.time() < deadline:
        jr = client.get(f"/api/jobs/{job_id}")
        status = jr.json()["status"]
        if status in ("complete", "error", "needs_cookie"):
            break
        time.sleep(0.3)
    print("final status:", status)
    snap = client.get(f"/api/jobs/{job_id}").json()
    print("totals:", snap["totals"])

    # Check the derived output dir.
    out = Path(snap["output_dir"])
    print("output_dir:", out)
    assert "braincreation" in str(out), out
    assert "2026-06-16-04-01-04" in str(out), out

    zips = sorted(out.glob("*.zip"))
    print("zip files:", [z.name for z in zips])
    assert len(zips) == 3, zips

    man = json.loads((out / "manifest.json").read_text())
    print("manifest account_label:", man["account_label"], "export_ts:", man["export_timestamp"])
    print("manifest files:", [(f["filename"], f["size"], bool(f["completed_at"])) for f in man["files"]])
    assert man["account_label"] == "braincreation"
    assert man["export_timestamp"] == "2026-06-16-04-01-04"
    assert len(man["files"]) == 3
    assert all(f["size"] > 0 for f in man["files"])
    assert all(f["completed_at"] for f in man["files"])

    assert status == "complete", f"expected complete, got {status}: {snap.get('last_error')}"
    print("\n[PASS] Phase 2 e2e: payload -> dated dir -> 3 zips -> manifest with sizes+times")

    srv.shutdown()

if __name__ == "__main__":
    main()
