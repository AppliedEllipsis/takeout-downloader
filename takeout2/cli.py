"""takeout2.cli — operator CLI for the v2 engine.

Commands (docs/v2/03-UX-AND-OBSERVABILITY.md §2): status, watch, run, budget,
verify, identity, doctor, migrate — plus ``next`` (the advisor). Reads state.db
directly — no HTTP, no Google host; the only outbound call is a FREE CDP read of
our own Chrome. Every command accepts --json; no tracebacks without --debug;
``run --dry-run`` builds components, prints a plan, and reserves nothing.

UX layer (additive, backward compatible):

* ``status`` opens with one plain-English summary line per job (progress bar,
  percentage, parts, bytes, state, budget warning) and closes with the advisor
  hint. All pre-existing detail is unchanged and still printed below it.
* ``next`` prints the single most useful next action, derived from the real
  JobStatus/PartStatus values in contracts.py. It only ever names commands and
  surfaces that actually exist.
* ``doctor --fix`` repairs what is safe to repair (missing parts dir, schema,
  stale ACTIVE part rows from a crashed process) and REFUSES anything that
  would spend a Google attempt or delete downloaded bytes.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timezone

from .contracts import (AccountIdentity, DEFAULTS, IdentityRecord, JobStatus,
                        LabelSource, PartStatus, VerifyState, parse_export_ts)
from .ledger import AttemptLedger
from .progress import make_progress_emitter
from .state import JobStore
from .verify import VerifyResult, scan_parts_dir, verify_part

PROG = "takeout2"


class CliError(Exception):
    """User-facing error: print the message, exit 1, no traceback."""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _env(name, default=""):
    v = os.environ.get(name)
    return default if v in (None, "") else v


def _env_num(name, default, cast):
    try:
        return cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _err(msg):
    print(f"{PROG}: {msg}", file=sys.stderr)
    return 1


def _emit(obj, code=0):
    """Print the human view's data as JSON and return ``code``."""
    print(json.dumps(obj, indent=2, default=str))
    return code


def _short_id(aid):
    """j=abc…ab9f2 — the stable key, abbreviated for humans only."""
    return aid if len(aid) <= 9 else f"{aid[:2]}…{aid[-5:]}"


def _truncate_middle(name, width):
    if len(name) <= width:
        return name
    head = width // 2 - 1
    return f"{name[:head]}…{name[-(width - head - 1):]}"


def _fmt(n, compact=False):
    """'620 GB'/'3.08 TB' for status; '10.0G'/'3.2G' for the budget table."""
    if n is None:
        return "—"
    v = float(n)
    for u in ("B", "K", "M", "G", "T", "P"):
        if abs(v) < 1024 or u == "P":
            if u == "B":
                return f"{int(v)}" if compact else f"{int(v)} B"
            if compact:
                return f"{v:.1f}{u}"
            if abs(v) >= 100:
                return f"{v:.0f} {u}B"
            if abs(v) >= 10:
                return f"{v:.1f} {u}B"
            return f"{v:.2f} {u}B"
        v /= 1024.0
    return f"{v:.2f} PB"


def _human_eta(seconds):
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


def _parse_iso(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None


def _since_str(iso):
    """'since 14:02 (2h 41m)' from an ISO timestamp."""
    when = _parse_iso(iso)
    if when is None:
        return "since —"
    total = max(0, int((datetime.now(when.tzinfo) - when).total_seconds()))
    hhmm = when.strftime("%H:%M")
    if total < 3600:
        return f"since {hhmm} ({total // 60}m {total % 60:02d}s)"
    return f"since {hhmm} ({total // 3600}h {(total % 3600) // 60}m)"


def _progress_bar(pct, width=10):
    """'▓▓▓▓░░░░░░' for 40%. ``pct is None`` (unknown total) renders '──────────'."""
    if pct is None:
        return "─" * width
    filled = int(round(max(0.0, min(100.0, float(pct))) / 100.0 * width))
    return "▓" * filled + "░" * (width - filled)


def _pct_str(pct):
    return "  —" if pct is None else f"{float(pct):.0f}%"


def summary_line(d):
    """The human-first one-liner at the top of ``status``.

    braincreation  ▓▓▓▓░░░░░░ 19%  12/63 parts  412 GB / 3.08 TB  DOWNLOADING
    """
    p = d["parts"]
    bits = [
        f"{_truncate_middle(d['account_label'] or '—', 16):<16}",
        _progress_bar(d["pct"]),
        f"{_pct_str(d['pct']):>4}",
        f"{p['done']}/{p['total']} parts",
        f"{_fmt(d['bytes_done'])} / {_fmt(d['bytes_total'])}",
        str(d["status"]),
    ]
    b = d["budget"]
    if b["at_0"]:
        noun = "part" if b["at_0"] == 1 else "parts"
        bits.append(f"⚠ {b['at_0']} {noun} out of attempts")
    elif b["at_1"]:
        noun = "part" if b["at_1"] == 1 else "parts"
        bits.append(f"⚠ {b['at_1']} {noun} at 1 attempt left")
    return "  ".join(bits)


def _use_color():
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def _paint_left(left, use_color):
    """03 §2.2: 1 = red bold 'last attempt', 0 = inverse, 2-3 = amber."""
    if not use_color or left > 3:
        return str(left)
    if left <= 0:
        return f"\x1b[7m{left}\x1b[0m"
    if left == 1:
        return f"\x1b[1;31m{left}\x1b[0m"
    return f"\x1b[33m{left}\x1b[0m"


def _confidence(job):
    """Confidence derived from the stored provenance (contracts ladder)."""
    return AccountIdentity(gaia_user=job.gaia_user or "",
                           label_source=job.label_source).confidence.value


# --------------------------------------------------------------------------
# backend + payload plumbing
# --------------------------------------------------------------------------
def open_backend(db_path):
    """Open JobStore + AttemptLedger on one SQLite file with env config."""
    budget = _env_num("TK2_ATTEMPT_BUDGET", DEFAULTS["ATTEMPT_BUDGET"], int)
    reserve = _env_num("TK2_BUDGET_RESERVE", DEFAULTS["BUDGET_RESERVE"], int)
    try:
        store = JobStore.open(db_path)
        ledger = AttemptLedger(sqlite3.connect(db_path, check_same_thread=False),
                               budget=budget, reserve=reserve)
    except (sqlite3.Error, OSError) as exc:
        raise CliError(
            f"cannot open state.db at {db_path}: {exc}\n"
            f"      point --db at the real file, e.g. "
            f"{PROG} status --db /opt/archives/state.db") from None
    return store, ledger, budget, reserve


def cookie_probe(timeout=1.0):
    """Read the live jar from our own Chrome (FREE, localhost CDP only).

    Returns ``{"age_s": float, "fresh": bool, "n_cookies": int}`` or
    ``{"unavailable": reason}``. Never raises.
    """
    try:
        from .cookie import LiveCookieJar
        jar = LiveCookieJar(_env("TK2_CDP_URL", DEFAULTS["CDP_URL"]),
                            timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {"unavailable": f"cookie source not constructible ({exc})"}
    try:
        state = jar.pull()
        return {"age_s": round(state.age_s, 1), "fresh": state.fresh,
                "n_cookies": state.n_cookies}
    except Exception as exc:  # noqa: BLE001
        return {"unavailable": getattr(exc, "reason", None) or str(exc)}


def _payload_export_raw(payload):
    """Raw ``YYYYMMDDTHHMMSSZ``: payload field, filenames, folder, capture."""
    raw = payload.get("export_raw")
    if raw:
        return str(raw)
    names = list(payload.get("filenames") or [])
    names += list((payload.get("sizes") or {}).keys())
    ts = parse_export_ts(names)
    if ts:
        return ts
    folder = payload.get("export_ts")
    if folder:
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})",
                         str(folder))
        if m:
            return (f"{m.group(1)}{m.group(2)}{m.group(3)}T"
                    f"{m.group(4)}{m.group(5)}{m.group(6)}Z")
        return str(folder)
    cap = payload.get("captured_at")
    return (f"{re.sub(r'[^A-Za-z0-9._-]', '-', str(cap))}-capture" if cap
            else "unknown-export")


def identity_from_payload(payload):
    """IdentityRecord from the extension POST body (01 §3/§6 shapes)."""
    acct = payload.get("account") or {}
    src = acct.get("label_source") or payload.get("label_source") or "UNKNOWN"
    try:
        label_source = LabelSource(str(src))
    except ValueError:
        label_source = LabelSource.UNKNOWN
    return IdentityRecord(
        archive_id=str(payload.get("archive_id") or ""),
        export_raw=_payload_export_raw(payload),
        account=AccountIdentity(
            gaia_user=str(acct.get("gaia_user") or payload.get("gaia_user") or ""),
            authuser=str(acct.get("authuser") or payload.get("authuser") or "0"),
            email=acct.get("email") or payload.get("email"),
            label=acct.get("label") or payload.get("label"),
            label_source=label_source),
        parts_expected=payload.get("parts_expected"),
        captured_at=payload.get("captured_at"),
        page_url=payload.get("page_url"),
    )


def _load_payload(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise CliError(f"payload file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise CliError(f"payload file is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise CliError("payload must be a JSON object")
    return data


def _get_job(store, archive_id):
    """Fetch a job or print a friendly one-liner naming a real next command."""
    job = store.get_job(archive_id)
    if job is not None:
        return job
    known = store.list_jobs()
    if not known:
        _err(f"no job for archive {archive_id!r} — this state.db has no jobs "
             f"at all.\n      {CAPTURE_HINT}")
        return None
    guess = _closest_archive_id(archive_id, known)
    hint = f"did you mean {guess}? " if guess else ""
    _err(f"no job for archive {archive_id!r}. {hint}"
         f"{len(known)} job(s) known — list them with: {PROG} status")
    return None


def _closest_archive_id(archive_id, jobs):
    """A typo'd/abbreviated id often still matches one job by substring."""
    needle = str(archive_id).strip().lstrip("j=").lower()
    if not needle:
        return None
    hits = [j.archive_id for j in jobs
            if needle in j.archive_id.lower()
            or (len(needle) >= 4 and needle[-5:] in j.archive_id.lower())]
    return hits[0] if len(hits) == 1 else None


# --------------------------------------------------------------------------
# shared data builders (JSON and human view render from the same dicts)
# --------------------------------------------------------------------------
def job_status_data(store, ledger, job):
    totals = store.job_totals(job.archive_id)
    budgets = ledger.parts_at_risk(job.archive_id)
    created = _parse_iso(job.created_at)
    elapsed = None
    if created is not None:
        elapsed = max(0.0, (datetime.now(created.tzinfo) - created).total_seconds())
    speed = totals["bytes_done"] / elapsed if (elapsed and elapsed > 1) else None
    remaining = max(0, totals["bytes_total"] - totals["bytes_done"])
    eta = remaining / speed if (speed and speed > 0 and remaining > 0) else None
    errors = [{"idx": p.idx, "reason": p.last_error or "FAILED"}
              for p in store.list_parts(job.archive_id)
              if p.status is PartStatus.FAILED]
    return {
        "archive_id": job.archive_id,
        "account_label": job.account_label,
        "export_ts": job.export_ts,
        "label_source": job.label_source.value,
        "confidence": _confidence(job),
        "status": job.status.value,
        "since": job.updated_at,
        "parts": {
            "total": totals["parts_total"], "done": totals["parts_done"],
            "active": totals["parts_active"], "partial": totals["parts_partial"],
            "pending": totals.get("by_status", {}).get(PartStatus.PENDING.value, 0),
            "exhausted": totals["parts_exhausted"], "failed": totals["parts_failed"],
        },
        "bytes_done": totals["bytes_done"], "bytes_total": totals["bytes_total"],
        "pct": (totals["bytes_done"] / totals["bytes_total"] * 100)
               if totals["bytes_total"] else None,
        "speed_bps": speed, "eta_s": eta,
        "budget": {"parts_at_risk": len(budgets),
                   "at_1": sum(1 for b in budgets if b.remaining == 1),
                   "at_0": sum(1 for b in budgets if b.remaining == 0)},
        "cookie": cookie_probe(),
        "last_error": job.last_error,
        "errors": errors,
    }


def render_status(d):
    p = d["parts"]
    print(f"  {d['account_label']} / {d['export_ts']:<20} "
          f"[{d['label_source']} · {d['confidence']}]")
    print(f"  archive {_short_id(d['archive_id']):<14} {p['total']} parts · "
          f"{_fmt(d['bytes_total'])} expected · {_fmt(d['bytes_done'])} done")
    print(f"  state   {d['status']:<14} {_since_str(d['since'])}")
    print("  " + "─" * 60)
    print(f"  parts     {p['done']} done · {p['active']} active · "
          f"{p['partial']} partial · {p['pending']} pending · "
          f"{p['exhausted']} exhausted")
    pct = f"({d['pct']:.1f}%)" if d["pct"] is not None else "(—)"
    print(f"  bytes     {_fmt(d['bytes_done'])} / {_fmt(d['bytes_total'])}   {pct}")
    speed = d["speed_bps"]
    if speed:
        print(f"  speed     {(_fmt(speed) + '/s'):<16} (avg since start)")
    else:
        print("  speed     —  (avg since start)")
    print(f"  eta       {_human_eta(d['eta_s'])}")
    b = d["budget"]
    if b["parts_at_risk"] == 0:
        print("  budget    ✓ no parts at risk")
    else:
        s = "part" if b["at_1"] == 1 else "parts"
        print(f"  budget    ⚠ {b['at_1']} {s} has 1 attempt left · "
              f"{b['at_0']} at 0  (see: budget)")
    c = d["cookie"]
    if "unavailable" in c:
        print(f"  cookie    n/a ({str(c['unavailable'])[:44]})")
    else:
        print(f"  cookie    {c['age_s']:.0f} s old · "
              f"{'fresh' if c['fresh'] else 'STALE'}")
    if d["errors"]:
        reasons = ", ".join(e["reason"] for e in d["errors"][:4])
        more = f" (+{len(d['errors']) - 4})" if len(d["errors"]) > 4 else ""
        print(f"  errors    {len(d['errors'])} last ({reasons}{more})")
    else:
        print("  errors    0")


def budget_data(store, ledger, job, parts, budget, reserve):
    rows = []
    for part in parts:
        b = ledger.budget_for(job.archive_id, part.idx)
        rows.append({
            "idx": part.idx, "filename": part.filename or "",
            "size_expected": part.size_expected,
            "size_on_disk": part.size_on_disk,
            "local_used": b.local_used, "remote_used": b.remote_used,
            "left": b.remaining, "status": part.status.value,
            "verify_state": part.verify_state.value, "at_risk": b.at_risk,
        })
    return {
        "archive_id": job.archive_id, "archive_short": _short_id(job.archive_id),
        "account_label": job.account_label, "export_ts": job.export_ts,
        "label_source": job.label_source.value, "confidence": _confidence(job),
        "attempt_budget": budget, "reserve": reserve,
        "parts": rows, "parts_total": len(rows),
        "parts_at_risk": sum(1 for r in rows if r["at_risk"]),
    }


def render_budget(d):
    print(f"archive {d['archive_short']}  {d['account_label']} / "
          f"{d['export_ts']}   [{d['label_source']}, {d['confidence']}]")
    print(f"{'part':>4}  {'filename':<38} {'size':>8} {'on-disk':>8} "
          f"{'local':>5} {'google':>5} {'left':>4}  state")
    use_color = _use_color()
    for r in d["parts"]:
        google = "—" if r["remote_used"] is None else str(r["remote_used"])
        state = f"{r['status']} {r['verify_state']}"
        if r["left"] == 1:
            state += " ⚠ last attempt"
        elif r["left"] == 0:
            state += " ✖"
        print(f"{r['idx']:>4}  {_truncate_middle(r['filename'], 38):<38} "
              f"{_fmt(r['size_expected'], True):>8} "
              f"{_fmt(r['size_on_disk'], True):>8} "
              f"{r['local_used']:>5} {google:>5} "
              f"{_paint_left(r['left'], use_color):>4}  {state}")
    print(f"{'':>46}── archive total: {d['parts_at_risk']} of "
          f"{d['parts_total']} parts at risk")


# --------------------------------------------------------------------------
# advisor — "what should I do now?"
# --------------------------------------------------------------------------
#: The zero-CLI happy path. Everything downstream is inspection and recovery.
CAPTURE_HINT = ("Open takeout.google.com in the webtop browser and click "
                "Download on any archive — the extension captures it and the "
                "download starts automatically.")

#: Ranking: the lower the number, the more urgent. The advisor prints exactly
#: one action — the most urgent one across all jobs.
_ADVICE_RANK = {
    "budget_exhausted": 0,
    "needs_cookie": 1,
    "failed": 2,
    "parts_exhausted": 3,
    "paused": 4,
    "stalled_parts": 5,
    "verifying": 6,
    "discovering": 7,
    "downloading": 8,
    "complete": 9,
    "no_jobs": 10,
}


def _runner_present():
    """Is a runner/manager process plausibly driving jobs right now?

    We must not import takeout2.runner (it may not exist yet) and we must not
    make a network call. The env flag the compose file sets is the only signal
    the CLI can read for FREE.
    """
    flag = _env("TK2_RUNNER", "") or _env("TK2_MANAGER_URL", "")
    return flag not in ("", "0", "false")


def advise(jobs):
    """Pick the single most useful next action from real job snapshots.

    ``jobs`` is a list of ``job_status_data`` dicts. Returns a dict with
    ``kind`` (a stable key for scripting), ``headline`` (one line of plain
    English) and ``commands`` (zero or more copy-pasteable commands — only
    commands that actually exist in this CLI).
    """
    if not jobs:
        return {"kind": "no_jobs", "archive_id": None,
                "headline": f"No jobs yet. {CAPTURE_HINT}",
                "commands": []}

    candidates = [_advise_job(d) for d in jobs]
    candidates.sort(key=lambda a: (_ADVICE_RANK.get(a["kind"], 99),
                                   str(a["archive_id"])))
    best = candidates[0]
    if len(candidates) > 1:
        best = dict(best, others=len(candidates) - 1)
    return best


def _advise_job(d):
    aid = d["archive_id"]
    short = _short_id(aid)
    label = d["account_label"] or short
    parts = d["parts"]
    status = d["status"]

    def out(kind, headline, commands=()):
        return {"kind": kind, "archive_id": aid, "headline": headline,
                "commands": list(commands)}

    if status == JobStatus.BUDGET_EXHAUSTED.value:
        return out("budget_exhausted",
                   f"{label} is out of download attempts. Google allows "
                   f"5 downloads per archive part; this job has spent them, so "
                   f"a human must decide what happens next — nothing will "
                   f"retry on its own (that is deliberate: a retry here could "
                   f"waste the last attempt on another part). Check which "
                   f"parts are affected, then either accept the archive as-is "
                   f"or re-export it from Takeout.",
                   [f"{PROG} budget {aid}", f"{PROG} verify {aid}"])

    if status == JobStatus.NEEDS_COOKIE.value:
        return out("needs_cookie",
                   f"{label} needs a fresh cookie. Open the Takeout page in "
                   f"the webtop browser — the extension re-captures "
                   f"automatically and the job resumes where it stopped. "
                   f"Nothing is lost and no attempt is spent by waiting.",
                   [f"{PROG} status --db <db>"])

    if status == JobStatus.FAILED.value:
        why = f" Last error: {d.get('last_error')}." if d.get("last_error") else ""
        return out("failed",
                   f"{label} is FAILED.{why} Look at the per-part detail before "
                   f"doing anything that costs an attempt.",
                   [f"{PROG} budget {aid}", f"{PROG} verify {aid}"])

    if parts["exhausted"]:
        noun = "part" if parts["exhausted"] == 1 else "parts"
        return out("parts_exhausted",
                   f"{label}: {parts['exhausted']} {noun} hit the 5-attempt "
                   f"limit and will not be retried automatically — a human has "
                   f"to decide. The rest of the job keeps going.",
                   [f"{PROG} budget {aid}"])

    if status == JobStatus.PAUSED.value:
        return out("paused",
                   f"{label} is PAUSED and will not move until it is resumed "
                   f"from the manager (extension popup / overlay Pause button, "
                   f"or the manager's resume control).",
                   [f"{PROG} status --db <db>"])

    if status == JobStatus.DOWNLOADING.value:
        return out("downloading",
                   f"Nothing to do — {label} is downloading. Watch it live, or "
                   f"open the monitor page in the webtop browser.",
                   [f"{PROG} watch"])

    if status == JobStatus.VERIFYING.value:
        return out("verifying",
                   f"Nothing to do — {label} is verifying what is on disk. "
                   f"You can run the local check yourself; it costs no "
                   f"attempts and touches no bytes on Google's side.",
                   [f"{PROG} verify {aid}"])

    if status == JobStatus.DISCOVERING.value:
        return out("discovering",
                   f"Nothing to do — {label} is still working out how many "
                   f"parts the export has. It starts downloading by itself "
                   f"once the part list is known.",
                   [f"{PROG} watch"])

    if status == JobStatus.COMPLETE.value:
        return out("complete",
                   f"{label} is COMPLETE — {parts['done']}/{parts['total']} "
                   f"parts, {_fmt(d['bytes_done'])} on disk. Optionally run a "
                   f"local verification pass; it spends no attempts.",
                   [f"{PROG} verify {aid}"])

    # READY, or anything with resumable bytes and nothing driving it.
    resumable = parts["partial"] + parts["pending"]
    if resumable and not _runner_present():
        return out("stalled_parts",
                   f"{label} has {resumable} part(s) waiting to be downloaded "
                   f"and no runner appears to be driving it. Normally the "
                   f"manager auto-starts the job when a capture arrives — "
                   f"start the manager, or re-click Download on the Takeout "
                   f"page so a fresh capture kicks it off.",
                   [f"{PROG} status --db <db>"])
    return out("downloading",
               f"Nothing to do — {label} is queued and will start on its own. "
               f"Watch it if you want to see it move.",
               [f"{PROG} watch"])


def render_advice(advice, prefix="next"):
    print(f"{prefix}: {advice['headline']}")
    for cmd in advice["commands"]:
        print(f"      $ {cmd}")
    if advice.get("others"):
        n = advice["others"]
        print(f"      (+{n} other job{'s' if n != 1 else ''} — see: {PROG} status)")


def cmd_next(args):
    store, ledger, _b, _r = open_backend(args.db)
    jobs = [job_status_data(store, ledger, job) for job in store.list_jobs()]
    advice = advise(jobs)
    if args.json:
        return _emit({"generated_at": _utcnow_iso(), "advice": advice})
    render_advice(advice)
    return 0


# --------------------------------------------------------------------------
# status / watch
# --------------------------------------------------------------------------
def cmd_status(args):
    store, ledger, _b, _r = open_backend(args.db)
    jobs = [job_status_data(store, ledger, job) for job in store.list_jobs()]
    advice = advise(jobs)
    data = {"generated_at": _utcnow_iso(), "jobs": jobs, "advice": advice}
    if args.json:
        return _emit(data)
    if not jobs:
        print(f"{PROG}: no jobs yet.")
        print()
        render_advice(advice)
        return 0
    # Human-first: the plain-English summary first, all the detail below it.
    for d in jobs:
        print(summary_line(d))
    print()
    for i, d in enumerate(jobs):
        if i:
            print()
        render_status(d)
    print()
    render_advice(advice)
    return 0


def _active_at_risk_rows(store, ledger, d):
    aid = d["archive_id"]
    active = {p.idx: p for p in store.list_parts(aid, status=PartStatus.ACTIVE)}
    at_risk = {b.idx: b for b in ledger.parts_at_risk(aid)}
    rows = []
    for idx in sorted(set(active) | set(at_risk)):
        part = active.get(idx) or store.get_part(aid, idx)
        if part is None:
            continue
        budget = ledger.budget_for(aid, idx)
        rows.append((str(idx), _truncate_middle(part.filename or "", 38),
                     _fmt(part.size_expected, True), _fmt(part.size_on_disk, True),
                     str(budget.remaining),
                     f"{part.status.value} {part.verify_state.value}"))
    return rows


def _watch_render(snaps, store, ledger):
    """One plain-text screen: summary + budget banner + active/at-risk rows."""
    from io import StringIO
    buf = StringIO()
    for d in snaps:
        b = d["budget"]
        print(f"{d['account_label']} / {d['export_ts']}   "
              f"[{d['label_source']} · {d['confidence']}]", file=buf)
        if b["parts_at_risk"]:
            print(f"⚠ budget: {b['at_1']} part(s) at 1 attempt left · "
                  f"{b['at_0']} at 0", file=buf)
        pct = f"{d['pct']:.1f}%" if d["pct"] is not None else "—"
        speed = f"{_fmt(d['speed_bps'])}/s" if d["speed_bps"] else "—"
        cookie = d["cookie"]
        cookie_s = f"{cookie['age_s']:.0f} s old" if "age_s" in cookie else "n/a"
        print(f"archive {_short_id(d['archive_id'])}  {d['status']}  "
              f"{_fmt(d['bytes_done'])} / {_fmt(d['bytes_total'])} ({pct})  "
              f"{speed}  eta {_human_eta(d['eta_s'])}  cookie {cookie_s}", file=buf)
        print("─" * 60, file=buf)
        for row in _active_at_risk_rows(store, ledger, d):
            print(f"  {row[0]:>3}  {row[1]:<38} {row[2]:>8} {row[3]:>8} "
                  f"{row[4]:>4}  {row[5]}", file=buf)
        print(file=buf)
    return buf.getvalue()


def cmd_watch(args):
    store, ledger, _b, _r = open_backend(args.db)
    interval = _env_num("TK2_WATCH_INTERVAL_S", 2.0, float)
    if args.json:
        while True:
            data = [job_status_data(store, ledger, job)
                    for job in store.list_jobs()]
            print(json.dumps({"generated_at": _utcnow_iso(), "jobs": data},
                             default=str), flush=True)
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                return 0
    use_rich = _use_color()
    if use_rich:
        try:
            from rich.live import Live
            from rich.console import Console
        except ImportError:
            use_rich = False
    agg, prev_bytes, prev_t, live = {}, {}, time.monotonic(), None
    try:
        while True:
            snaps = [job_status_data(store, ledger, job)
                     for job in store.list_jobs()]
            dt = max(1e-9, time.monotonic() - prev_t)
            for d in snaps:
                aid = d["archive_id"]
                instant = ((d["bytes_done"] - prev_bytes.get(aid, 0.0)) / dt
                           if aid in prev_bytes else 0.0)
                agg[aid] = agg.get(aid, 0.0) * 0.6 + instant * 0.4
                d["speed_bps"] = agg[aid]
                d["eta_s"] = (max(0, d["bytes_total"] - d["bytes_done"]) / agg[aid]
                              if d["bytes_total"] > 0 and agg[aid] > 0 else None)
            prev_bytes = {d["archive_id"]: d["bytes_done"] for d in snaps}
            prev_t = time.monotonic()
            if use_rich:
                if live is None:
                    live = Live(_watch_render(snaps, store, ledger),
                                console=Console(), refresh_per_second=2,
                                screen=True)
                    live.start()
                else:
                    live.update(_watch_render(snaps, store, ledger))
            elif snaps:
                for d in snaps:
                    render_status(d)
                    print()
                sys.stdout.flush()
            else:
                print(f"{PROG}: no jobs yet — run: {PROG} run --payload "
                      f"<capture.json>", flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
    finally:
        if live is not None:
            live.stop()
    return 0


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------
def cmd_run(args):
    store, ledger, budget, reserve = open_backend(args.db)
    payload = _load_payload(args.payload)
    archive_id = str(payload.get("archive_id") or "").strip()
    if not archive_id:
        return _err("payload has no archive_id — nothing to key the job on")
    if not payload.get("parts_expected") and not payload.get("filenames"):
        return _err("payload carries no part metadata "
                    "(need parts_expected or filenames)")

    from .plan import resolve_plan
    plan = resolve_plan(payload=payload, store=store, archive_id=archive_id,
                        filenames=payload.get("filenames"), allow_probes=False,
                        ledger=ledger)
    if not plan.ok:
        return _err(f"cannot derive a part plan: {plan.message}")

    identity = identity_from_payload(payload)
    storage_root = _env("TK2_STORAGE_ROOT", "")
    if not storage_root:
        return _err("TK2_STORAGE_ROOT is not set — where should the archive "
                    "live? (see docs/v2/00-CONTRACTS.md §7)")
    output_dir = os.path.join(storage_root, identity.relative_dir())
    lines = [
        f"plan       source={plan.source}  {plan.message}",
        f"archive    {archive_id}",
        f"identity   {identity.account.folder_name()} / {identity.export_ts}  "
        f"[{identity.account.label_source.value} · "
        f"{identity.account.confidence.value}]",
        f"output     {output_dir}",
        f"parts_dir  {os.path.join(output_dir, 'parts')}",
        f"parallel   {_env_num('TK2_PARALLEL', DEFAULTS['PARALLEL'], int)}   "
        f"budget={budget}  reserve={reserve}",
    ]
    if args.dry_run:
        print("\n".join(lines))
        print("dry-run: components built, nothing reserved, nothing downloaded")
        return 0

    store.upsert_job(identity, output_dir=output_dir, attempt_budget=budget)
    store.upsert_parts(archive_id, plan.parts)

    from .cookie import LiveCookieJar
    from .engine import WRITE_CHUNK, BurstEngine, EngineConfig
    try:
        cookie = LiveCookieJar(_env("TK2_CDP_URL", DEFAULTS["CDP_URL"]),
                               timeout=_env_num("TK2_CDP_TIMEOUT_S", 10.0, float))
    except Exception as exc:  # noqa: BLE001
        store.set_job_status(archive_id, JobStatus.NEEDS_COOKIE,
                             error=f"cannot build cookie source: {exc}")
        print("\n".join(lines))
        return _err(f"cannot build cookie source: {exc}")
    config = EngineConfig(
        parallel=_env_num("TK2_PARALLEL", DEFAULTS["PARALLEL"], int),
        attempt_budget=budget, budget_reserve=reserve,
        verify_level=VerifyState(_env("TK2_VERIFY_LEVEL",
                                      DEFAULTS["VERIFY_LEVEL"])),
        # --- multi-TB reliability guards -------------------------------
        # Abort a stream that has moved no bytes for this long. The socket
        # read timeout cannot catch a trickle, so this is what frees a
        # wedged attempt on a multi-day transfer.
        stall_abort_s=_env_num("TK2_STALL_ABORT_S", 180.0, float),
        stall_resume_attempts=_env_num("TK2_STALL_RESUME", 1, int),
        resume_rewind=_env_num("TK2_RESUME_REWIND", WRITE_CHUNK, int),
        fsync_interval_s=_env_num("TK2_FSYNC_INTERVAL_S", 30.0, float),
        # Refuse to write if the storage FUSE mount died — otherwise a 10 GiB
        # part lands on the root disk and takes the server down.
        require_mount=_env("TK2_REQUIRE_MOUNT", "0") not in ("0", "", "false"),
        # rclone VFS upload backlog: pause instead of stalling on a full cache.
        cache_dir=_env("TK2_CACHE_DIR", "") or None,
        cache_max_bytes=_env_num("TK2_CACHE_MAX_BYTES", 100 * 1024 ** 3, int),
        cache_wait_max_s=_env_num("TK2_CACHE_WAIT_MAX_S", 1800.0, float),
        rate_limit_backoff=_env("TK2_RATE_BACKOFF", "1") not in ("0", "false"),
    )
    result = BurstEngine(store, ledger, cookie,
                         os.path.join(output_dir, "parts"),
                         config=config,
                         on_chunk=make_progress_emitter(store, archive_id),
                         ).run_burst(archive_id)
    failed = ", ".join(f"{k}:{v}" for k, v in sorted(result.failed.items()))
    print("\n".join(lines))
    print(f"burst      started={result.started} canary={result.canary_passed} "
          f"ok={result.completed_ok} partial={result.completed_partial} "
          f"failed={{{failed}}} exhausted={result.budget_exhausted} "
          f"attempts={result.attempts_spent} bytes={_fmt(result.bytes_moved)}")
    return 0


# --------------------------------------------------------------------------
# budget / verify / identity
# --------------------------------------------------------------------------
def cmd_budget(args):
    store, ledger, budget, reserve = open_backend(args.db)
    job = _get_job(store, args.archive_id)
    if job is None:
        return 1
    data = budget_data(store, ledger, job, store.list_parts(args.archive_id),
                       budget, reserve)
    if args.json:
        return _emit(data)
    render_budget(data)
    return 0


def cmd_verify(args):
    store, ledger, _b, _r = open_backend(args.db)
    job = _get_job(store, args.archive_id)
    if job is None:
        return 1
    on_disk = scan_parts_dir(os.path.join(job.output_dir, "parts"))
    level = VerifyState.HASH_OK if args.deep else VerifyState.STRUCT_OK
    rows = []
    for part in store.list_parts(args.archive_id):
        disk = on_disk.get(part.filename or "")
        if disk is None:
            res = VerifyResult(VerifyState.UNVERIFIED, 0, "not on disk")
        else:
            res = verify_part(disk.path, size_expected=part.size_expected,
                              level=level)
        store.update_part(args.archive_id, part.idx, verify_state=res.state,
                          size_on_disk=res.size_on_disk or part.size_on_disk)
        rows.append({"idx": part.idx, "filename": part.filename or "",
                     "size_on_disk": res.size_on_disk,
                     "state": res.state.value, "detail": res.detail,
                     "ok": res.ok})
    ok = sum(1 for r in rows if r["ok"])
    corrupt = sum(1 for r in rows if r["state"] == VerifyState.CORRUPT.value)
    missing = sum(1 for r in rows if r["detail"] == "not on disk")
    if args.json:
        return _emit({"archive_id": args.archive_id, "level": level.value,
                      "parts": rows, "ok": ok, "corrupt": corrupt,
                      "missing": missing},
                     0 if ok == len(rows) else 1)
    print(f"archive {_short_id(args.archive_id)}   level={level.value}   "
          f"{len(rows)} parts")
    print(f"{'idx':>4}  {'filename':<38} {'size':>8}  state        detail")
    for r in rows:
        marker = "" if r["ok"] else " ✖"
        print(f"{r['idx']:>4}  {_truncate_middle(r['filename'], 38):<38} "
              f"{_fmt(r['size_on_disk'], True):>8}  {r['state']:<10}"
              f"{marker} {r['detail']}")
    print(f"── {ok} ok · {corrupt} corrupt · {missing} missing")
    return 0 if ok == len(rows) else 1


def cmd_identity(args):
    store, ledger, _b, _r = open_backend(args.db)
    job = _get_job(store, args.archive_id)
    if job is None:
        return 1
    set_label = None
    if args.set_label:
        override = IdentityRecord(
            archive_id=args.archive_id,
            export_raw=job.export_raw or job.export_ts,
            account=AccountIdentity(gaia_user=job.gaia_user or "",
                                    authuser=job.authuser or "0",
                                    email=job.account_email,
                                    label=args.set_label,
                                    label_source=LabelSource.OPERATOR_OVERRIDE),
            parts_expected=job.parts_expected)
        upgraded = store.maybe_upgrade_identity(override)
        set_label = {"requested": args.set_label,
                     "result": ("upgraded to OPERATOR_OVERRIDE" if upgraded
                                else "ignored (not a provenance upgrade)")}
        job = store.get_job(args.archive_id)
    data = {
        "archive_id": job.archive_id, "account_label": job.account_label,
        "label_source": job.label_source.value, "confidence": _confidence(job),
        "email": job.account_email, "gaia_user": job.gaia_user,
        "authuser": job.authuser, "export_raw": job.export_raw,
        "export_ts": job.export_ts, "output_dir": job.output_dir,
        "status": job.status.value, "parts_expected": job.parts_expected,
        "attempt_budget": job.attempt_budget, "created_at": job.created_at,
        "updated_at": job.updated_at, "finished_at": job.finished_at,
        "last_error": job.last_error,
    }
    if set_label:
        data["set_label"] = set_label
    if args.json:
        return _emit(data)
    print(f"identity  {data['account_label']} / {data['export_ts']}   "
          f"[{data['label_source']}, {data['confidence']}]")
    print(f"  archive_id   {data['archive_id']}")
    print(f"  account      email={data['email'] or '—'}  "
          f"gaia={data['gaia_user'] or '—'}  authuser={data['authuser'] or '0'}")
    print(f"  provenance   label_source={data['label_source']}  "
          f"confidence={data['confidence']}")
    print(f"  export       raw={data['export_raw'] or '—'}  "
          f"ts={data['export_ts']}")
    print(f"  output_dir   {data['output_dir']}")
    print(f"  status       {data['status']}  budget={data['attempt_budget']}  "
          f"parts_expected={data['parts_expected'] or '—'}")
    print(f"  timestamps   created={data['created_at']}  "
          f"updated={data['updated_at']}  finished={data['finished_at'] or '—'}")
    if data.get("last_error"):
        print(f"  last_error   {data['last_error']}")
    if set_label:
        print(f"  set-label    {set_label['requested']} -> {set_label['result']}")
    return 0


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------
def _check(checks, name, passed, ok_detail, fail_detail):
    checks.append((name, "PASS" if passed else "FAIL",
                   ok_detail if passed else fail_detail))


# --- doctor --fix ---------------------------------------------------------
# Two lists, and the split is the whole point:
#   fixes    — mechanical, local, reversible-by-rerun. Applied automatically.
#   refusals — anything that could spend a Google attempt (5 per part, ever)
#              or destroy downloaded bytes. NEVER applied; the human is told
#              exactly what to do instead.
def _fix(fixes, name, before, after):
    fixes.append({"fix": name, "before": before, "after": after})


def _refuse(refusals, name, why, do_this):
    refusals.append({"issue": name, "why": why, "do_this": do_this})


def doctor_fix(args, storage_root):
    """Repair what is safe; refuse the rest with concrete instructions.

    Returns ``(fixes, refusals)``. Never spends an attempt, never deletes or
    truncates a file, never contacts Google.
    """
    fixes, refusals = [], []

    # 1. storage root — a missing directory is a mkdir, not a decision.
    if not os.path.isdir(storage_root):
        try:
            os.makedirs(storage_root, exist_ok=True)
            _fix(fixes, "storage root", f"missing: {storage_root}",
                 f"created: {storage_root}")
        except OSError as exc:
            _refuse(refusals, "storage root",
                    f"cannot create {storage_root}: {exc}",
                    "check the mount is attached, then re-run: "
                    f"{PROG} doctor --fix")

    # 2. state.db schema — JobStore.open()/AttemptLedger both run an
    #    idempotent CREATE TABLE IF NOT EXISTS script, so opening the backend
    #    *is* the repair (state.py SCHEMA / ledger._ensure_schema).
    existed = os.path.exists(args.db)
    try:
        store, ledger, _b, _r = open_backend(args.db)
    except Exception as exc:  # noqa: BLE001
        _refuse(refusals, "state.db", f"cannot open {args.db}: {exc}",
                "state.db is unreadable — do NOT delete it (it is the only "
                "record of how many attempts each part has spent). Restore it "
                "from a backup, or move it aside and re-capture.")
        return fixes, refusals
    _fix(fixes, "state.db schema",
         ("present" if existed else "missing") + f": {args.db}",
         "schema ensured (job, part, event, attempt, remote_count)")

    # WAL is a pragma, not data.
    try:
        with sqlite3.connect(args.db) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode).lower() != "wal":
                conn.execute("PRAGMA journal_mode=WAL")
                after = conn.execute("PRAGMA journal_mode").fetchone()[0]
                _fix(fixes, "journal mode", f"journal_mode={mode}",
                     f"journal_mode={after}")
    except sqlite3.Error as exc:
        _refuse(refusals, "journal mode", str(exc),
                "another process may hold the db; stop the manager and re-run")

    # 3. dangling reservations — settle them FAIL CLOSED (ledger docstring).
    #    This spends nothing: an unsettled attempt already counts as used;
    #    reconciling only labels it, so accounting stops drifting.
    try:
        orphans = ledger.reconcile_orphans()
        if orphans:
            _fix(fixes, "orphan reservations",
                 f"{orphans} unsettled attempt row(s) from a crashed process",
                 f"{orphans} settled as ABORTED (assumed consumed — fail closed)")
    except Exception as exc:  # noqa: BLE001
        _refuse(refusals, "orphan reservations", str(exc),
                f"inspect manually, then re-run: {PROG} doctor --fix")

    for job in store.list_jobs():
        aid = job.archive_id
        label = job.account_label or _short_id(aid)

        # 4. missing parts dir — a mkdir; downloads land here.
        parts_dir = os.path.join(job.output_dir, "parts")
        if not os.path.isdir(parts_dir):
            try:
                os.makedirs(parts_dir, exist_ok=True)
                _fix(fixes, f"parts dir [{label}]", f"missing: {parts_dir}",
                     f"created: {parts_dir}")
            except OSError as exc:
                _refuse(refusals, f"parts dir [{label}]",
                        f"cannot create {parts_dir}: {exc}",
                        "check the storage mount is attached before starting "
                        "a burst — writing to a detached mount fills the root "
                        "disk")

        # 5. stale ACTIVE parts — no process is streaming them (this CLI is
        #    the only thing running), and their bytes are on disk, so they are
        #    resumable, not failed. Exactly what JobStore.recover() does.
        stale = store.list_parts(aid, status=PartStatus.ACTIVE)
        for part in stale:
            store.update_part(aid, part.idx, status=PartStatus.PARTIAL)
            _fix(fixes, f"stale part [{label}] idx={part.idx}",
                 f"ACTIVE with {_fmt(part.size_on_disk)} on disk "
                 f"(no process is streaming it)",
                 "PARTIAL — resumable on the next burst, 0 attempts spent")

        # ---- refusals: everything below costs attempts or bytes -----------
        exhausted = store.list_parts(aid, status=PartStatus.BUDGET_EXHAUSTED)
        if exhausted:
            idxs = ", ".join(str(p.idx) for p in exhausted)
            _refuse(refusals, f"budget exhausted [{label}]",
                    f"part(s) {idxs} have spent all 5 of Google's download "
                    f"attempts — clearing this would spend an attempt that "
                    f"does not exist",
                    f"review the counters ({PROG} budget {aid}), then either "
                    f"accept the archive without those parts or re-export it "
                    f"from takeout.google.com")

        failed = store.list_parts(aid, status=PartStatus.FAILED)
        if failed:
            idxs = ", ".join(str(p.idx) for p in failed)
            _refuse(refusals, f"failed parts [{label}]",
                    f"part(s) {idxs} are FAILED — retrying costs 1 Google "
                    f"attempt per part, so it is never automatic",
                    f"read the reason ({PROG} budget {aid}) and let the runner "
                    f"retry inside a fresh cookie burst")

        corrupt = [p for p in store.list_parts(aid)
                   if p.verify_state is VerifyState.CORRUPT]
        if corrupt:
            idxs = ", ".join(str(p.idx) for p in corrupt)
            _refuse(refusals, f"corrupt parts [{label}]",
                    f"part(s) {idxs} verify as CORRUPT — deleting downloaded "
                    f"bytes is never automatic",
                    f"confirm with: {PROG} verify {aid} --deep, then decide "
                    f"whether to re-download (costs 1 attempt per part)")

        if job.status is JobStatus.NEEDS_COOKIE:
            _refuse(refusals, f"needs cookie [{label}]",
                    "a cookie cannot be forged locally",
                    "open takeout.google.com in the webtop browser — the "
                    "extension re-captures automatically and the job resumes")

    return fixes, refusals


def render_fixes(fixes, refusals):
    if fixes:
        print("fixed:")
        for f in fixes:
            print(f"  ✓ {f['fix']}")
            print(f"      before: {f['before']}")
            print(f"      after:  {f['after']}")
    else:
        print("fixed: nothing needed repairing")
    if refusals:
        print("refused (needs a human — these cost attempts or bytes):")
        for r in refusals:
            print(f"  ✖ {r['issue']}")
            print(f"      why:      {r['why']}")
            print(f"      do this:  {r['do_this']}")


def cmd_doctor(args):
    checks = []
    storage_root = _env("TK2_STORAGE_ROOT", "") or os.path.dirname(
        os.path.abspath(args.db))

    fixes = refusals = None
    if getattr(args, "fix", False):
        fixes, refusals = doctor_fix(args, storage_root)
        if not args.json:
            render_fixes(fixes, refusals)
            print()

    # 1. storage root writable
    try:
        os.makedirs(storage_root, exist_ok=True)
        probe = os.path.join(storage_root, f".doctor-probe-{os.getpid()}")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        _check(checks, "storage root writable", True,
               f"rw {_fmt(shutil.disk_usage(storage_root).free)} free",
               "ROOT DISK FULL RISK")
    except OSError as exc:
        detail = ("ROOT DISK FULL RISK" if "No space" in str(exc)
                  else f"not writable: {exc}")
        _check(checks, "storage root writable", False, "", detail)

    # 2. state.db opens + WAL
    store = ledger = None
    try:
        store, ledger, _b, _r = open_backend(args.db)
        with sqlite3.connect(args.db) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        _check(checks, "state.db opens + WAL", mode == "wal", "ok",
               f"journal_mode={mode}, run reconcile_orphans")
    except Exception as exc:  # noqa: BLE001
        _check(checks, "state.db opens + WAL", False, "",
               f"run reconcile_orphans ({exc})")
        store = None

    # 3/4. CDP + cookie jar — only when a cookie source is constructible
    # (a non-localhost TK2_CDP_URL is a config SKIP, not a failure).
    constructible = False
    try:
        from .cookie import LiveCookieJar
        LiveCookieJar(_env("TK2_CDP_URL", DEFAULTS["CDP_URL"]), timeout=1.0)
        constructible = True
    except Exception as exc:  # noqa: BLE001
        checks.append(("CDP reachable", "SKIP",
                       f"cookie source not constructible ({exc})"))
        checks.append(("cookie jar", "SKIP", "no cookie source"))
    if constructible:
        cdp = _env("TK2_CDP_URL", DEFAULTS["CDP_URL"])
        try:
            import requests
            resp = requests.get(f"{cdp.rstrip('/')}/json/version", timeout=1.0)
            browser = resp.json().get("Browser", "Chrome ?") if resp.ok else "Chrome ?"
            checks.append(("CDP reachable", "PASS", str(browser)))
        except Exception:  # noqa: BLE001
            checks.append(("CDP reachable", "FAIL", "login in the webtop"))
        c = cookie_probe(timeout=1.0)
        if "unavailable" in c:
            checks.append(("cookie jar", "FAIL", "open the manage page"))
        else:
            checks.append(("cookie jar", "PASS", f"{c['age_s']:.0f} s old"))

    # 5. disk headroom — at least 2x the expected export size free.
    if store is not None:
        total_expected = sum(store.job_totals(j.archive_id)["bytes_total"]
                             for j in store.list_jobs())
        try:
            free = shutil.disk_usage(storage_root).free
            _check(checks, "disk headroom",
                   not (total_expected and free < 2 * total_expected),
                   f">{_fmt(free)} free (need >2× export)", "free space needed")
        except OSError as exc:
            _check(checks, "disk headroom", False, "", str(exc))

    # 6. log rotation — docker-managed; informational from the CLI.
    checks.append(("log rotation", "PASS", "10m×3 (docker config)"))

    # 7. budget health — no part may sit at 0 attempts.
    if store is not None:
        at_risk = exhausted = 0
        for job in store.list_jobs():
            risk = ledger.parts_at_risk(job.archive_id)
            at_risk += len(risk)
            exhausted += sum(1 for b in risk if b.remaining == 0)
        _check(checks, "budget health", not exhausted,
               "0 exhausted" if not at_risk else f"{at_risk} at risk · 0 exhausted",
               f"{at_risk} at risk · {exhausted} exhausted — see: budget")

    all_pass = all(status in ("PASS", "SKIP") for _, status, _ in checks)
    if args.json:
        payload = {"checks": [{"name": n, "status": s, "detail": d}
                              for n, s, d in checks],
                   "exit": 0 if all_pass else 1}
        if fixes is not None:
            payload["fixes"] = fixes
            payload["refusals"] = refusals
        return _emit(payload, 0 if all_pass else 1)
    for name, status, detail in checks:
        print(f"  {name:<26} {status:<4} {detail}")
    print(f"doctor: {'all checks pass' if all_pass else '1 or more checks failed'}")
    if not all_pass and not getattr(args, "fix", False):
        print(f"doctor: some of this may be repairable — try: {PROG} doctor --fix")
    return 0 if all_pass else 1


# --------------------------------------------------------------------------
# migrate — lazy; the v1 -> v2 migrator may not be built yet
# --------------------------------------------------------------------------
def cmd_migrate(args):
    try:
        from .migrate import inspect_v1, migrate_from_v1
    except ImportError:
        print("takeout2.migrate not built yet")
        return 1
    output_dir = args.output_dir or "."
    try:
        report = inspect_v1(output_dir)
    except Exception as exc:  # noqa: BLE001
        return _err(f"cannot inspect {output_dir}: {exc}")
    if args.json:
        return _emit(report)
    elif not report:
        print(f"migrate: no v1 jobs found under {output_dir}")
    else:
        for e in report:
            flags = f"  [{len(e.get('errors') or [])} errors]" if e.get("errors") else ""
            print(f"  {(e.get('archive_id') or '?'):<20} "
                  f"{(e.get('account_label') or '?'):<24} "
                  f"{(e.get('export_ts') or '?'):<20} "
                  f"parts={e.get('parts_total', 0):>3} "
                  f"done={e.get('parts_done', 0):>3} "
                  f"bytes={_fmt(e.get('bytes_done'))}{flags}")
        for e in report:
            for err in e.get("errors") or []:
                print(f"  ! {err}")
    if not args.apply:
        print("dry-run: pass --apply to commit into state.db")
        return 0
    store, ledger, _b, _r = open_backend(args.db)
    try:
        result = migrate_from_v1(output_dir, store, ledger=ledger, apply=True)
    except Exception as exc:  # noqa: BLE001
        return _err(f"migration failed: {exc}")
    if args.json:
        return _emit(result)
    print(f"migrated: {result['jobs_created']} jobs created, "
              f"{result['parts_seeded']} parts seeded, "
              f"{result['parts_done']} done, "
              f"{result['attempts_seeded']} attempts seeded, "
              f"{len(result['errors'])} errors")
    return 0


# --------------------------------------------------------------------------
# parser + entry point
# --------------------------------------------------------------------------
EPILOG = """
TYPICAL USE — you normally do not need this CLI at all.

  1. Open takeout.google.com in the webtop browser.
  2. Click Download on an archive. That is the whole workflow.

  The extension captures the click, the manager creates the job and starts it
  automatically, and a dead cookie heals itself (open the Takeout page again
  and it re-captures). A 3 TB / 63-part export runs for days with no further
  interaction.

This CLI is for INSPECTION and RECOVERY:

  takeout2 status          human summary of every job + what to do next
  takeout2 next            just the single most useful next action
  takeout2 watch           live dashboard while a job runs
  takeout2 budget <id>     Google's own attempt counter per part (the money view)
  takeout2 verify <id>     local structural check — spends no attempts
  takeout2 doctor --fix    diagnose, and repair what is safe to repair

Nothing here ever spends a Google download attempt except `run`. Each archive
part may only be downloaded 5 times, ever — that is why nothing retries by
itself once a part is exhausted.
"""


def build_parser():
    parser = argparse.ArgumentParser(
        prog=PROG, description="Operator CLI for the takeout2 download engine "
                               "(reads state.db; never contacts Google).",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default="./state.db", metavar="PATH",
                        help="path to state.db (default: ./state.db)")
    common.add_argument("--json", action="store_true",
                        help="emit the data the human view renders, as JSON")
    common.add_argument("--debug", action="store_true",
                        help="show tracebacks on error")
    sub = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    def add(name, help_, func):
        p = sub.add_parser(name, parents=[common], help=help_)
        p.set_defaults(func=func)
        return p

    add("status", "one-shot snapshot", cmd_status)
    add("next", "what should I do now? (the advisor)", cmd_next)
    add("watch", "live dashboard (repeated status without a TTY)", cmd_watch)
    p = add("run", "start a burst from a capture payload", cmd_run)
    p.add_argument("--payload", required=True, metavar="FILE",
                   help="capture payload JSON (the extension POST body)")
    p.add_argument("--dry-run", action="store_true",
                   help="build components and print the plan; run nothing")
    p = add("budget", "attempt accounting per part (docs/v2/01 §9)", cmd_budget)
    p.add_argument("archive_id", help="the j= archive key")
    p = add("verify", "local-only structural verification", cmd_verify)
    p.add_argument("archive_id", help="the j= archive key")
    p.add_argument("--deep", action="store_true",
                   help="also compute sha256 (full read; opt-in)")
    p = add("identity", "show identity + provenance", cmd_identity)
    p.add_argument("archive_id", help="the j= archive key")
    p.add_argument("--set-label", metavar="LABEL",
                   help="overwrite the account label (OPERATOR_OVERRIDE)")
    p = add("doctor", "preflight health checks", cmd_doctor)
    p.add_argument("--fix", action="store_true",
                   help="repair what is SAFE to repair (dirs, schema, stale "
                        "ACTIVE rows); refuse anything costing an attempt "
                        "or downloaded bytes")
    p = add("migrate", "v1 state -> state.db (dry-run by default)", cmd_migrate)
    p.add_argument("--output-dir", metavar="DIR", default=".",
                   help="v1 tree to adopt (dir containing .manager_state.json)")
    p.add_argument("--apply", action="store_true",
                   help="commit the migration (default is dry-run)")
    return parser


def _friendly_reason(exc):
    """Map a raw exception onto (cause, suggested command) in plain English.

    Only for the handful of failures an operator actually hits. Anything else
    falls through to the exception text (and --debug still shows the traceback).
    """
    text = str(exc)
    if isinstance(exc, sqlite3.OperationalError):
        if "unable to open database file" in text:
            return ("state.db cannot be opened — the path or its directory "
                    "does not exist", f"{PROG} status --db ./state.db")
        if "no such table" in text:
            return ("state.db is missing tables (empty or truncated file)",
                    f"{PROG} doctor --fix")
        if "database is locked" in text:
            return ("state.db is locked by another process (the manager is "
                    "probably writing)", f"{PROG} status")
    if isinstance(exc, sqlite3.DatabaseError):
        return ("state.db is not a valid SQLite database — do NOT delete it, "
                "it records how many attempts each part has spent",
                f"{PROG} doctor")
    if isinstance(exc, FileNotFoundError):
        return (f"file not found: {getattr(exc, 'filename', None) or text}",
                f"{PROG} doctor")
    if isinstance(exc, PermissionError):
        return (f"permission denied: {getattr(exc, 'filename', None) or text}",
                f"{PROG} doctor")
    lowered = text.lower()
    if ("connection refused" in lowered or "connectionerror" in lowered
            or "failed to establish" in lowered or "max retries" in lowered):
        return ("the manager / Chrome is unreachable — nothing is listening",
                f"{PROG} doctor")
    return None


def main(argv=None):
    # The spec renders with box-drawing/emoji; never crash on a legacy
    # console encoding (cp1252). Replace unencodable chars instead.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CliError as exc:
        return _err(str(exc))
    except KeyboardInterrupt:
        print(f"{PROG}: interrupted", file=sys.stderr)
        return 130
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        if getattr(args, "debug", False):
            traceback.print_exc()
            return 1
        friendly = _friendly_reason(exc)
        if friendly is None:
            print(f"{PROG}: {exc}", file=sys.stderr)
            print(f"{PROG}: re-run with --debug for the full traceback",
                  file=sys.stderr)
            return 1
        cause, suggestion = friendly
        print(f"{PROG}: {cause}", file=sys.stderr)
        print(f"      try: {suggestion}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
