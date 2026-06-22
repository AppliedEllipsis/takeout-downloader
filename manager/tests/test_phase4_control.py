"""Phase 4: control plane auth gating + diagnose reason codes.

Run: PYTHONPATH=. .venv-manager/Scripts/python manager/tests/test_phase4_control.py
"""
import os
os.environ["MANAGER_API_TOKEN"] = "secret-api"
os.environ["MANAGER_CAPTURE_TOKEN"] = "secret-cap"

from fastapi.testclient import TestClient
from manager.app import app
from manager import jobs as J
from manager.orchestrator import get_orchestrator
from manager import diagnose as Dg


def main():
    client = TestClient(app)
    orch = get_orchestrator()

    # --- token gating --------------------------------------------------------
    r = client.post("/api/control/pause", json={"job_id": "nope"})
    assert r.status_code == 401, f"expected 401 without token, got {r.status_code}"
    print("[OK] control endpoint rejects missing api token (401)")

    r = client.post("/api/control/pause", json={"job_id": "nope"},
                    headers={"X-Api-Token": "wrong"})
    assert r.status_code == 401
    print("[OK] control endpoint rejects wrong api token (401)")

    r = client.post("/api/control/pause", json={"job_id": "nope"},
                    headers={"X-Api-Token": "secret-api"})
    # job doesn't exist -> 404, but auth passed
    assert r.status_code == 404, f"expected 404 with good token + bad job, got {r.status_code}"
    print("[OK] control endpoint accepts good api token (passes to 404 for missing job)")

    # capture token still required on payload
    r = client.post("/api/payload", content=b"{}")
    assert r.status_code == 401
    r = client.post("/api/payload", content=b"{}", headers={"X-Capture-Token": "secret-cap"})
    assert r.status_code == 422  # bad payload but auth passed
    print("[OK] capture token gates /api/payload independently of api token")

    # --- diagnose reason codes (unit) ---------------------------------------
    from pathlib import Path
    import tempfile
    d = Path(tempfile.mkdtemp())

    job = J.Job("j1", "acct", d, parallel=1, max_exports=10)
    job.update_part(0, filename="p0.zip", size=100, done=100, status="done")

    # needs_cookie, 0 recaptures -> cookie_expired
    job.set_status(J.NEEDS_COOKIE, error="cookie expired: ...")
    diag = Dg.diagnose(job, recapture_count=0)
    assert diag["reason"] == "cookie_expired", diag["reason"]
    assert diag["auto_recovers"] is True
    print(f"[OK] needs_cookie + 0 recaptures -> cookie_expired ({diag['section']})")

    # needs_cookie, >=2 recaptures -> auth_loop
    diag = Dg.diagnose(job, recapture_count=2)
    assert diag["reason"] == "auth_loop", diag["reason"]
    assert diag["needs_human"] is True
    print(f"[OK] needs_cookie + 2 recaptures -> auth_loop ({diag['section']})")

    # error + disk in message -> disk_full
    job.set_status(J.ERROR, error="OSError: No space left on device")
    diag = Dg.diagnose(job)
    assert diag["reason"] == "disk_full", diag["reason"]
    print(f"[OK] error 'no space' -> disk_full ({diag['section']})")

    # error + validation -> zip_validation_failed
    job.set_status(J.ERROR, error="2 part(s) failed validation")
    diag = Dg.diagnose(job)
    assert diag["reason"] == "zip_validation_failed", diag["reason"]
    print(f"[OK] error 'failed validation' -> zip_validation_failed ({diag['section']})")

    # complete -> ok
    job.set_status(J.COMPLETE)
    diag = Dg.diagnose(job)
    assert diag["reason"] == "ok", diag["reason"]
    print(f"[OK] complete -> ok")

    print("\n[PASS] Phase 4: token gating + diagnose reason codes verified")


if __name__ == "__main__":
    main()
