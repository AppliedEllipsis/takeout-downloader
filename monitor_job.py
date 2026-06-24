#!/usr/bin/env python3
"""Poll the takeout manager job status and redraw a single in-place line.

Uses carriage return (\r) so the status updates on the same line instead of
scrolling. Clears trailing characters each redraw so a shorter line leaves no
stale tail from a longer one.
"""
import json, subprocess, sys, time, shutil

def get_job():
    out = subprocess.check_output([
        "ssh", "takeout-server",
        "docker exec takeout-webgui curl -s 127.0.0.1:8080/api/jobs"
    ], timeout=15).decode()
    jobs = json.loads(out).get("jobs", [])
    return jobs[0] if jobs else None

def fmt_bytes(n):
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"

def render(j):
    cols = shutil.get_terminal_size((80, 24)).columns
    if not j:
        line = f"{time.strftime('%H:%M:%S')}  no jobs"
    else:
        t = j.get("totals", {})
        done = t.get("bytes_done", 0)
        total = t.get("bytes_total", 1)
        pct = done / total * 100 if total else 0
        spd = t.get("speed_bps", 0)
        pd = t.get("parts_done", 0)
        pt = t.get("parts_total", 0)
        err = j.get("last_error") or ""
        line = (
            f"{time.strftime('%H:%M:%S')}  {j['status']:12s}  "
            f"{pd}/{pt} parts  {fmt_bytes(done)}/{fmt_bytes(total)}  "
            f"{pct:5.1f}%  {fmt_bytes(spd)}/s  {err}"
        )
    # Truncate to terminal width, pad to overwrite any stale tail.
    line = line[:cols].ljust(cols)
    sys.stdout.write("\r" + line)
    sys.stdout.flush()

last_status = None
while True:
    try:
        j = get_job()
        render(j)
        if j:
            last_status = j["status"]
            if last_status in ("complete", "error"):
                sys.stdout.write("\n")  # final newline so the prompt doesn't clobber it
                break
    except Exception as e:
        cols = shutil.get_terminal_size((80, 24)).columns
        msg = f"{time.strftime('%H:%M:%S')}  ERR: {e}"
        sys.stdout.write("\r" + msg[:cols].ljust(cols))
        sys.stdout.flush()
    time.sleep(5)
