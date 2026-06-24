#!/usr/bin/env python3
"""Poll the takeout manager job status and print a clean progress line."""
import json, subprocess, sys, time

def get_job():
    out = subprocess.check_output([
        "ssh", "takeout-server",
        "docker exec takeout-webgui curl -s 127.0.0.1:8080/api/jobs"
    ], timeout=15).decode()
    jobs = json.loads(out).get("jobs", [])
    return jobs[0] if jobs else None

def fmt_bytes(n):
    for u in ["B","KB","MB","GB","TB"]:
        if n < 1024: return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"

while True:
    try:
        j = get_job()
        if not j:
            print(f"{time.strftime('%H:%M:%S')}  no jobs"); sys.stdout.flush()
            time.sleep(5); continue
        t = j.get("totals", {})
        done = t.get("bytes_done", 0)
        total = t.get("bytes_total", 1)
        pct = done / total * 100 if total else 0
        spd = t.get("speed_bps", 0)
        pd = t.get("parts_done", 0)
        pt = t.get("parts_total", 0)
        print(f"{time.strftime('%H:%M:%S')}  {j['status']:12s}  "
              f"{pd}/{pt} parts  {fmt_bytes(done)}/{fmt_bytes(total)}  "
              f"{pct:5.1f}%  {fmt_bytes(spd)}/s  {j.get('last_error') or ''}")
        sys.stdout.flush()
    except Exception as e:
        print(f"{time.strftime('%H:%M:%S')}  ERR: {e}")
        sys.stdout.flush()
    if j and j["status"] in ("complete", "error"):
        break
    time.sleep(5)
