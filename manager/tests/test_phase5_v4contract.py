"""Phase 5: verify the manager honors the extension v4 payload contract.

The extension v4 POSTs a payload that may carry a `_meta` block with
{email, user, authuser, archiveId}. The manager must:
  - derive the account label from _meta.email local-part,
  - expose /api/control/recapture-pending gated by the capture token,
  - report a needs_cookie job as pending so the extension can auto-recapture.

This is the manager side of the contract. The extension JS itself is
syntax-checked separately (node --check) since it needs a real browser.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["MANAGER_CAPTURE_TOKEN"] = "cap-tok"
os.environ["MANAGER_API_TOKEN"] = "api-tok"

_tmp = tempfile.mkdtemp(prefix="mgr-p5-")
os.environ["STORAGE_ROOT"] = _tmp
os.environ["ALLOWED_DIRS"] = _tmp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from manager.app import app  # noqa: E402
from manager import jobs as J  # noqa: E402
from manager.orchestrator import get_orchestrator  # noqa: E402


def _payload_with_email(email):
    # A v4-style single capture payload with the _meta identity block.
    return json.dumps({
        "schema": 1,
        "captured_at": "2026-06-16T15:45:00.000Z",
        "source": "extension",
        "url": ("https://takeout-download.usercontent.google.com/download/"
                "takeout-20260616T040104Z-1-001.zip?j=abc&i=0&user=11530"),
        "method": "GET",
        "headers": {"User-Agent": "x"},
        "cookie": "__Secure-1PSID=zzz; SID=qqq",
        "_meta": {"email": email, "user": "11530", "authuser": "0",
                  "archiveId": "abc"},
    })


def main():
    client = TestClient(app)
    orch = get_orchestrator()

    # 1. Account label derives from the email local-part, not the gaia id.
    #    We stub the runner so no real download fires — assert on derivation.
    import manager.engine_bridge as eb
    orig_start = eb.JobRunner.start
    eb.JobRunner.start = lambda self: None  # no-op: don't launch the thread
    try:
        r = client.post("/api/payload",
                        headers={"X-Capture-Token": "cap-tok"},
                        content=_payload_with_email("BrainCreation@gmail.com"))
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        job = orch.get_job(job_id)
        out = str(job.output_dir).replace("\\", "/")
        assert "/braincreation/" in out, out
        assert "/2026-06-16-04-01-04" in out, out
        print("[OK] email 'BrainCreation@gmail.com' -> label 'braincreation', dated dir")
        assert job.meta.get("account_label") == "braincreation"
        assert job.meta.get("export_ts") == "2026-06-16-04-01-04"
        print("[OK] job.meta carries account_label + export_ts for the manifest")

        # 2. recapture-pending requires the capture token.
        rp = client.get("/api/control/recapture-pending")
        assert rp.status_code == 401, rp.status_code
        print("[OK] recapture-pending rejects missing capture token (401)")

        # 3. With no needs_cookie job, pending is false.
        job.set_status(J.DOWNLOADING)
        rp = client.get("/api/control/recapture-pending",
                        headers={"X-Capture-Token": "cap-tok"})
        assert rp.status_code == 200 and rp.json()["pending"] is False, rp.text
        print("[OK] pending=false while downloading")

        # 4. Flip to needs_cookie -> pending true with the job id.
        job.set_status(J.NEEDS_COOKIE, error="cookie expired")
        rp = client.get("/api/control/recapture-pending",
                        headers={"X-Capture-Token": "cap-tok"})
        body = rp.json()
        assert body["pending"] is True, body
        assert job_id in body.get("job_ids", []), body
        print("[OK] pending=true with job id when a job needs a cookie")

        # 5. A fallback when no email: gaia-<user>.
        r2 = client.post("/api/payload",
                         headers={"X-Capture-Token": "cap-tok"},
                         content=_payload_with_email(""))
        assert r2.status_code == 200, r2.text
        job2 = orch.get_job(r2.json()["job_id"])
        out2 = str(job2.output_dir).replace("\\", "/")
        assert "/gaia-11530/" in out2, out2
        print("[OK] empty email falls back to 'gaia-11530' label")
    finally:
        eb.JobRunner.start = orig_start

    print("\n[PASS] Phase 5: manager honors the v4 payload + recapture contract")


if __name__ == "__main__":
    main()
