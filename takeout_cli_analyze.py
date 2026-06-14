#!/usr/bin/env python3
"""
Analyze a takeout_cli.log file and print a structured debug summary.

Usage:
    python takeout_cli_analyze.py takeout_cli.log
    python takeout_cli_analyze.py --follow takeout_cli.log   # tail -f style
    python takeout_cli_analyze.py --last=50 takeout_cli.log  # last N lines
    tail -F downloads/takeout_cli.log | python takeout_cli_analyze.py --follow -

What it shows
-------------
  Session banner:    start/end timestamps, run duration
  Errors:            every ERROR / WARNING line with surrounding context
  Probe timeline:    per-part discovery status (size / 404 / auth-fail)
  Auth failures:     when + how many in a row + total in the run
  Parts found:       discovered / needed / downloaded
  Top aria2c errors: anything aria2c complained about

This is intentionally NOT a real-time visualizer; it processes whole lines
so it works equally well on a 500KB rotated log file or on a streaming stdin.
For interactive watching, use `tail -f downloads/takeout_cli.log` directly.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator


LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+"
    r"(?P<level>\S+)\s+(?P<msg>.*)$"
)


@dataclass
class LogLine:
    ts: datetime
    level: str
    msg: str


def parse_lines(stream: Iterable[str]) -> Iterator[LogLine]:
    for raw in stream:
        line = raw.rstrip("\n")
        m = LOG_LINE_RE.match(line)
        if not m:
            continue
        try:
            ts = datetime.strptime(m["ts"], "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            continue
        yield LogLine(ts=ts, level=m["level"], msg=m["msg"])


@dataclass
class Session:
    """A single run of takeout_cli, bounded by header / Done lines."""
    started: datetime | None = None
    ended: datetime | None = None
    errors: list[LogLine] = field(default_factory=list)
    warnings: list[LogLine] = field(default_factory=list)
    probes: list[tuple[int, str]] = field(default_factory=list)  # (part_num, outcome)
    auth_failures: int = 0
    parts_discovered: int = 0
    parts_complete_final: int | None = None
    parts_total_final: int | None = None


def summarize(lines: list[LogLine]) -> dict:
    """Aggregate stats from a single session's lines."""
    out: dict = {
        "errors": [],
        "warnings": [],
        "probes": [],
        "auth_failures": 0,
        "parts_discovered": 0,
        "parts_complete_final": None,
        "parts_total_final": None,
        "first_ts": lines[0].ts if lines else None,
        "last_ts": lines[-1].ts if lines else None,
    }
    for ln in lines:
        if ln.level == "ERROR":
            out["errors"].append((ln.ts, ln.msg))
        elif ln.level == "WARNING":
            out["warnings"].append((ln.ts, ln.msg))
        m = re.search(r"probe #(\d+)\s*->\s*(.+)", ln.msg)
        if m and ln.level in ("DEBUG", "INFO"):
            out["probes"].append((int(m.group(1)), m.group(2).strip()))
        if "Auth failed" in ln.msg or "redirected to accounts" in ln.msg:
            out["auth_failures"] += 1
        m2 = re.search(r"Found (\d+) parts", ln.msg)
        if m2:
            out["parts_discovered"] = int(m2.group(1))
        m3 = re.search(r"Complete:\s*(\d+)/(\d+)", ln.msg)
        if m3:
            out["parts_complete_final"] = int(m3.group(1))
            out["parts_total_final"] = int(m3.group(2))
    return out


def split_into_sessions(lines: list[LogLine]) -> list[list[LogLine]]:
    """Each session starts with the 'Google Takeout Downloader (aria2c)' header
    and ends at EOF or the next header. We detect the header by its exact
    banner text; the surrounding '=' decoration lines belong to the same
    session."""
    sessions: list[list[LogLine]] = []
    cur: list[LogLine] = []
    started = False
    for ln in lines:
        if "Google Takeout Downloader (aria2c)" in ln.msg and ln.level == "INFO":
            if started and cur:
                sessions.append(cur)
            cur = [ln]
            started = True
        elif started:
            cur.append(ln)
        # Lines before any session header (e.g. a stray banner bar) are
        # discarded — they're the start of a torn-off banner from a rotated
        # file and have no useful payload.
    if started and cur:
        sessions.append(cur)
    return sessions


def fmt_duration(start: datetime, end: datetime) -> str:
    secs = (end - start).total_seconds()
    if secs < 60:
        return f"{secs:.1f}s"
    if secs < 3600:
        m, s = divmod(secs, 60)
        return f"{int(m)}m{int(s):02d}s"
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}h{int(m):02d}m{int(s):02d}s"


def render_summary(session_lines: list[LogLine]) -> str:
    s = summarize(session_lines)
    out: list[str] = []
    if not session_lines:
        return "(no log lines parsed)"

    if s["first_ts"] and s["last_ts"]:
        dur = fmt_duration(s["first_ts"], s["last_ts"])
        out.append(f"Session: {s['first_ts']:%Y-%m-%d %H:%M:%S} -> "
                   f"{s['last_ts']:%Y-%m-%d %H:%M:%S}  ({dur})")
    out.append(f"  Errors:     {len(s['errors'])}")
    out.append(f"  Warnings:   {len(s['warnings'])}")
    out.append(f"  Auth fails: {s['auth_failures']}")
    if s["parts_discovered"]:
        out.append(f"  Parts discovered: {s['parts_discovered']}")
    if s["parts_complete_final"] is not None:
        out.append(f"  Final state: {s['parts_complete_final']}/{s['parts_total_final']} complete")
    if s["probes"]:
        outcomes = Counter(o for _, o in s["probes"])
        sample = ", ".join(f"{k}:{v}" for k, v in outcomes.most_common(5))
        out.append(f"  Probes: {len(s['probes'])} total  ({sample})")

    if s["errors"]:
        out.append("")
        out.append("  Recent errors:")
        for ts, msg in s["errors"][-5:]:
            short = msg if len(msg) < 110 else msg[:107] + "..."
            out.append(f"    {ts:%H:%M:%S} {short}")
    if s["warnings"]:
        out.append("")
        out.append("  Recent warnings:")
        for ts, msg in s["warnings"][-5:]:
            short = msg if len(msg) < 110 else msg[:107] + "..."
            out.append(f"    {ts:%H:%M:%S} {short}")

    # Probe timeline (just the LAST 20 to avoid blowing up output).
    if s["probes"]:
        out.append("")
        out.append("  Probe timeline (last 20):")
        for num, outcome in s["probes"][-20:]:
            short = outcome if len(outcome) < 70 else outcome[:67] + "..."
            out.append(f"    #{num:03d} {short}")
    return "\n".join(out)


def cmd_summary(args) -> int:
    if args.path == "-":
        lines = list(parse_lines(sys.stdin))
    else:
        p = Path(args.path)
        # Handle rotated backups: takeout_cli.log.1, .log.2, .log.3
        targets = sorted(p.parent.glob(p.name + ".*"),
                         key=lambda x: int(x.suffix.lstrip(".") or "0"),
                         reverse=True)
        targets.insert(0, p)
        lines: list[LogLine] = []
        for t in targets:
            if t.is_file():
                with t.open(encoding="utf-8", errors="replace") as f:
                    lines.extend(parse_lines(f))
    if not lines:
        print("No parseable log lines found.", file=sys.stderr)
        return 1

    if args.last:
        lines = lines[-args.last:]

    sessions = split_into_sessions(lines)
    if args.json:
        print(json.dumps([summarize(s) for s in sessions],
                         default=str, indent=2))
        return 0

    if len(sessions) > 1:
        print(f"Found {len(sessions)} sessions in log.")
        for i, s in enumerate(sessions, 1):
            print()
            print(f"=== Session {i} ===")
            print(render_summary(s))
    elif sessions:
        print(render_summary(sessions[0]))
    else:
        # Tail / --last with no recognizable session header — just show what
        # we have as an "ad-hoc tail" so the user can still inspect it.
        print("(no session header found in the lines analyzed; "
              "showing ad-hoc tail)")
        print(render_summary(lines))
    return 0


def cmd_follow(args) -> int:
    """Tail -f style: print a rolling summary that updates every N seconds."""
    import time
    p = Path(args.path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Make sure the file exists so tail-like reading doesn't error.
    p.touch(exist_ok=True)
    last_summary = ""
    print(f"Following {p} (Ctrl-C to quit)...")
    with p.open(encoding="utf-8", errors="replace") as f:
        f.seek(0, 2)  # start at end
        last_flush = time.time()
        while True:
            new = f.read()
            if new:
                for ln in parse_lines(new.splitlines()):
                    sys.stdout.write(
                        f"\033[2m{ln.ts:%H:%M:%S}\033[0m "
                        f"[{ln.level}] {ln.msg}\n"
                    )
                sys.stdout.flush()
            if time.time() - last_flush > args.interval:
                last_flush = time.time()
                # Re-read whole file for a fresh summary.
                with p.open(encoding="utf-8", errors="replace") as fr:
                    all_lines = list(parse_lines(fr))
                sessions = split_into_sessions(all_lines)
                s = render_summary(sessions[-1]) if sessions else "(empty)"
                if s != last_summary:
                    print("\033[2m--- summary ---\033[0m")
                    print(s)
                    print("\033[2m---------------\033[0m")
                    last_summary = s
            time.sleep(0.5)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("path", nargs="?", default="-",
                   help="log file path, or '-' for stdin (default)")
    p.add_argument("--last", type=int, default=0,
                   help="only consider the last N lines")
    p.add_argument("--json", action="store_true",
                   help="emit structured JSON instead of human summary")
    p.add_argument("-F", "--follow", dest="follow", action="store_true",
                   help="tail -f mode (path is required and a file)")
    p.add_argument("--interval", type=float, default=10.0,
                   help="summary refresh interval in follow mode (default 10s)")
    args = p.parse_args()

    if args.follow:
        if args.path in ("-", ""):
            print("--follow requires a file path", file=sys.stderr)
            return 2
        return cmd_follow(args)
    return cmd_summary(args)


if __name__ == "__main__":
    sys.exit(main())
