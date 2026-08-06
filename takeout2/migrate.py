"""Adopt v1 download state into v2 ``state.db`` without re-downloading bytes.

Phase 8 of ``docs/v2/02-BUILD-PLAN-B.md``.

v1 kept live job bookkeeping in ``<output_dir>/.manager_state.json`` (see
``manager/jobs.py``) and a per-run record in ``<output_dir>/manifest.json``
(``manager/manifest.py``). v2 keeps everything in SQLite ``state.db`` keyed by
the STABLE ``archive_id`` (see ``docs/v2/01-IDENTITY-AND-SCRAPE.md`` §4).

This module READS the v1 files and RE-SEEDS ``state.db``:

  * ``archive_id`` — v1 ``archive_id`` (state meta or manifest header), else
    the ``j=`` URL param scraped from any part URL; a job with neither is
    reported as an error and skipped (it could never be re-keyed safely).
  * identity — ``AccountIdentity`` rebuilt from v1 meta with the label
    provenance ladder of 01 §4. The stored v1 label is matched against what
    each ladder rung would have sanitized to (v1's derivation precedence is
    email > label > gaia), so an email-derived label records ``SCRAPED_EMAIL``
    while a ``gaia-<user>`` fallback records ``GAIA_FALLBACK``. A stored label
    that matches no rung (e.g. an operator override v1 could not persist) is
    kept with ``UNKNOWN`` provenance rather than guessed.
  * parts — v1 ``done``/``size`` become ``size_on_disk``/``size_expected``;
    v1 ``done`` + ``zip_valid`` becomes ``DONE`` + ``STRUCT_OK``; v1 ``done``
    without ``zip_valid`` becomes ``PARTIAL`` + ``UNVERIFIED`` (never trusted
    blindly); anything else with bytes on disk becomes ``PARTIAL`` (resumable);
    empty parts stay ``PENDING``. A part v1 calls DONE whose file is missing
    on disk is reported as an error and stays ``PENDING`` — we never invent a
    completed file.

Reversible by design: nothing here deletes or overwrites a v1 file, and
deleting ``state.db`` returns the tree to a working v1 layout. ``--apply``
renames nothing and moves no bytes.

Public API:
    inspect_v1(output_dir) -> list[dict]
    migrate_from_v1(output_dir, store, ledger=None, apply=False) -> dict
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

from .contracts import (AccountIdentity, IdentityRecord, LabelSource, PartPlan,
                        PartStatus, VerifyState, parse_export_ts, sanitize_label)
from .ledger import AttemptLedger
from .state import JobStore

__all__ = ["inspect_v1", "migrate_from_v1"]

log = logging.getLogger("takeout2.migrate")

#: v1 file names — same constants as manager/jobs.py and manager/manifest.py.
STATE_NAME = ".manager_state.json"
MANIFEST_NAME = "manifest.json"

#: A Google download URL carries the STABLE archive id as the ``j=`` param
#: (same pattern takeout_cli.py uses to recover it).
_J_PARAM_RE = re.compile(r"[?&]j=([a-f0-9-]+)", re.I)


# --------------------------------------------------------------------------
# v1 file reading
# --------------------------------------------------------------------------
def _read_json(path: Path) -> Optional[dict]:
    """Return the JSON dict, or None when missing/unreadable/not a dict."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _find_v1_jobs(output_dir: str) -> list[Path]:
    """Every directory under ``output_dir`` holding a ``.manager_state.json``.

    Recursive on purpose: the v1 CLI writes one job per ``<account>/<ts>`` dir,
    but the migrate entry point may be pointed at a whole takeout root or at a
    single job dir — both work.
    """
    root = Path(output_dir)
    if not root.is_dir():
        return []
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Never descend into VCS/cache dirs — they cannot hold v1 jobs.
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and d != "__pycache__"]
        if STATE_NAME in filenames:
            found.append(Path(dirpath))
    return sorted(found)


# --------------------------------------------------------------------------
# field resolution
# --------------------------------------------------------------------------
def _resolve_archive_id(state: dict, manifest: Optional[dict],
                        parts: list[dict]) -> tuple[Optional[str], str]:
    """archive_id from v1 state/manifest, else the ``j=`` URL param.

    STOP RULE (02-BUILD-PLAN-B §8): the id is never derived from the label
    path — only from state or the ``j=`` param.
    """
    meta = state.get("meta") or {}
    candidate = (meta.get("archive_id")
                 or (manifest or {}).get("archive_id") or "").strip()
    if candidate:
        return candidate, ""
    for p in parts:
        m = _J_PARAM_RE.search(str(p.get("url") or ""))
        if m:
            return m.group(1), ""
    return None, "no archive_id in state, manifest, or any part URL"


def _resolve_identity(state: dict, manifest: Optional[dict]) -> AccountIdentity:
    """Rebuild the v2 identity, mapping v1's label derivation onto the ladder.

    v1 (manager/derive.py) derived the account label with precedence
    override > email > label > gaia and folded the result into
    ``meta.account_label`` / the manifest header. v2 (01 §4) ranks provenance
    as SCRAPED_EMAIL > SCRAPED_LABEL > GAIA_FALLBACK > UNKNOWN. Matching the
    stored label against each rung's sanitization tells us exactly which rung
    produced it — without that, a late-scraped email would claim provenance
    over a label that was actually a gaia fallback.
    """
    meta = state.get("meta") or {}
    mf = manifest or {}

    email = meta.get("email") or mf.get("account_email") or None
    label_raw = meta.get("label") or None
    gaia = meta.get("user") or mf.get("gaia_user") or None
    authuser = meta.get("authuser") or mf.get("authuser") or "0"

    # The label v1 actually derived and used for its output directory.
    stored_label = (mf.get("account_label") or meta.get("account_label")
                    or "").strip()
    stored_s = sanitize_label(stored_label) if stored_label else ""

    # Ladder in v1 precedence order (email > label > gaia).
    ladder: list[tuple[LabelSource, str]] = []
    if email:
        ladder.append((LabelSource.SCRAPED_EMAIL, sanitize_label(email)))
    if label_raw:
        ladder.append((LabelSource.SCRAPED_LABEL, sanitize_label(label_raw)))
    if gaia:
        gaia_s = re.sub(r"[^A-Za-z0-9]", "", str(gaia))
        if gaia_s:
            ladder.append((LabelSource.GAIA_FALLBACK, f"gaia-{gaia_s}"))

    if stored_s:
        for source, derived in ladder:
            if derived == stored_s:
                label_source, label = source, stored_s
                break
        else:
            # Stored label matches no derivable rung — keep it, admit we
            # don't know where it came from. Never downgrade to gaia.
            label_source, label = LabelSource.UNKNOWN, stored_s
    elif ladder:
        # No stored label: fall back to the ladder order.
        label_source, label = ladder[0]
    else:
        label_source, label = LabelSource.UNKNOWN, ""

    return AccountIdentity(
        gaia_user=str(gaia or ""),
        authuser=str(authuser or "0"),
        email=str(email) if email else None,
        label=label or None,
        label_source=label_source,
    )


def _resolve_export_raw(state: dict, manifest: Optional[dict],
                        filenames: list[str]) -> str:
    """Raw ``YYYYMMDDTHHMMSSZ`` — majority from filenames, else the v1 value."""
    meta = state.get("meta") or {}
    raw = parse_export_ts(filenames) or ""
    if not raw:
        raw = meta.get("export_raw") or (manifest or {}).get("export_raw") or ""
    if not raw:
        # Last resort: v1's folder-safe export_ts. format_export_ts will keep
        # it unchanged rather than mangle it into something else.
        raw = meta.get("export_ts") or ""
    return raw


# --------------------------------------------------------------------------
# part mapping
# --------------------------------------------------------------------------
def _map_parts(state: dict, job_dir: Path) -> tuple[list[dict], list[str]]:
    """Map v1 part rows to v2 statuses. Returns (rows, errors).

    v1 part row keys (manager/jobs.py): index, filename, url, size, done,
    status, started_at, completed_at, zip_valid, sha256.
    """
    parts = state.get("parts") or []
    rows: list[dict] = []
    errors: list[str] = []
    for pos, p in enumerate(parts):
        if not isinstance(p, dict):
            continue
        idx = _to_int(p.get("index"), pos)
        filename = p.get("filename")
        size_expected = None if p.get("size") is None else _to_int(p.get("size"), None)
        claimed_done = _to_int(p.get("done"), 0)
        v1_status = str(p.get("status") or "queued").lower()
        v1_done = v1_status == "done"
        zip_valid = bool(p.get("zip_valid"))

        on_disk = None
        if filename:
            candidate = job_dir / str(filename)
            # Defensive: a hand-edited v1 file must not make us look outside
            # the job directory.
            if str(candidate.resolve()).startswith(str(job_dir.resolve())):
                if candidate.is_file():
                    on_disk = candidate

        # size_on_disk reflects what is ACTUALLY on disk, not v1's claim — a
        # DONE part whose file vanished must not carry phantom bytes that the
        # engine would count against totals. (For every part whose file is
        # present this equals v1's `done`.)
        actual_size = on_disk.stat().st_size if on_disk is not None else 0

        row = {
            "idx": idx,
            "filename": filename,
            "url": p.get("url"),
            "size_expected": size_expected,
            "size_on_disk": actual_size,
            # v1 never recorded attempts, but honour the field if present.
            "attempts": _to_int(p.get("attempts_used") or p.get("attempts"), 0),
            # Google's own counter, if v1 ever captured it.
            "dl_count": _to_int(
                p.get("dl_count_remote")
                if p.get("dl_count_remote") is not None else p.get("dl_count"),
                None),
            "status": PartStatus.PENDING,
            "verify_state": VerifyState.UNVERIFIED,
        }

        if on_disk is None and v1_done:
            # Guard (02-BUILD-PLAN-B §8): refuse to trust a completed file
            # that is not there. Report it and keep the part PENDING.
            errors.append(
                f"part {idx}: v1 says DONE but file missing on disk: {filename}")
        elif v1_done and zip_valid:
            row["status"] = PartStatus.DONE
            row["verify_state"] = VerifyState.STRUCT_OK
        elif v1_done:
            # Bytes finished but never validated — resumable, never trusted
            # blindly.
            row["status"] = PartStatus.PARTIAL
            row["verify_state"] = VerifyState.UNVERIFIED
        elif actual_size > 0:
            # Interrupted mid-stream; the bytes on disk are worth resuming.
            row["status"] = PartStatus.PARTIAL
            row["verify_state"] = VerifyState.UNVERIFIED
        else:
            row["status"] = PartStatus.PENDING
            row["verify_state"] = VerifyState.UNVERIFIED

        rows.append(row)
    return rows, errors


# --------------------------------------------------------------------------
# per-job record
# --------------------------------------------------------------------------
def _read_job(job_dir: Path) -> dict:
    """Parse one v1 job dir into a normalized record (report + internals)."""
    empty = {"job_dir": job_dir, "archive_id": None, "account_label": "",
             "export_ts": "", "status": "unknown", "parts_total": 0,
             "parts_done": 0, "bytes_done": 0, "errors": [],
             "identity": None, "mapped": []}
    state = _read_json(job_dir / STATE_NAME)
    if state is None:
        empty["errors"] = [f"unreadable {STATE_NAME}"]
        return empty
    manifest = _read_json(job_dir / MANIFEST_NAME)
    parts = [p for p in (state.get("parts") or []) if isinstance(p, dict)]
    filenames = [str(p.get("filename")) for p in parts if p.get("filename")]

    archive_id, aid_error = _resolve_archive_id(state, manifest, parts)
    mapped, part_errors = _map_parts(state, job_dir)
    identity = _resolve_identity(state, manifest)
    export_raw = _resolve_export_raw(state, manifest, filenames)
    record = IdentityRecord(archive_id=archive_id or "",
                            export_raw=export_raw, account=identity,
                            parts_expected=len(parts))

    errors = ([aid_error] if aid_error else []) + part_errors
    return {
        "job_dir": job_dir,
        "archive_id": archive_id,
        "account_label": identity.folder_name(),
        "export_ts": record.export_ts,
        "status": str(state.get("status") or "unknown"),
        "parts_total": len(parts),
        "parts_done": sum(1 for r in mapped if r["status"] is PartStatus.DONE),
        "bytes_done": sum(r["size_on_disk"] for r in mapped
                          if r["status"] is PartStatus.DONE),
        "errors": errors,
        "identity": record,
        "mapped": mapped,
    }


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def inspect_v1(output_dir: str) -> list[dict]:
    """Dry-run report: one dict per v1 job found. Changes nothing.

    Each entry carries exactly: ``archive_id``, ``account_label``,
    ``export_ts``, ``status``, ``parts_total``, ``parts_done``, ``bytes_done``
    and ``errors`` (list of str). A job whose archive_id cannot be resolved is
    reported with ``archive_id=None`` and an explanatory error.
    """
    reports = []
    for job_dir in _find_v1_jobs(output_dir):
        rec = _read_job(job_dir)
        reports.append({
            "archive_id": rec["archive_id"],
            "account_label": rec["account_label"],
            "export_ts": rec["export_ts"],
            "status": rec["status"],
            "parts_total": rec["parts_total"],
            "parts_done": rec["parts_done"],
            "bytes_done": rec["bytes_done"],
            "errors": rec["errors"],
        })
    return reports


def _seed_attempts(store: JobStore, archive_id: str, idx: int,
                   attempts: int) -> None:
    """Seed a recorded attempt count onto the part row.

    ``JobStore.update_part`` has no ``attempts_used=`` parameter, so this
    writes directly on the same connection. Idempotent by construction (the
    value is rewritten with itself). v1 does not record attempts, so this is
    a defensive path only.
    """
    conn = getattr(store, "_conn", None)
    if conn is None:
        return
    conn.execute(
        "UPDATE part SET attempts_used=? WHERE archive_id=? AND idx=?",
        (int(attempts), archive_id, idx))
    conn.commit()


def _apply_part(store: JobStore, archive_id: str, row: dict) -> bool:
    """Write one part row. Returns True when anything needed writing.

    Skipping already-seeded rows is what makes a second ``--apply`` a true
    no-op: no UPDATE, no part_update event, no completed_at refresh.
    """
    cur = store.get_part(archive_id, row["idx"])
    if cur is not None:
        size_ok = (row["size_expected"] is None
                   or cur.size_expected == row["size_expected"])
        if (cur.status == row["status"]
                and cur.verify_state == row["verify_state"]
                and cur.size_on_disk == row["size_on_disk"]
                and cur.filename == row["filename"]
                and cur.attempts_used == row["attempts"]
                and size_ok):
            return False

    kw = {"status": row["status"], "verify_state": row["verify_state"],
          "size_on_disk": row["size_on_disk"], "filename": row["filename"]}
    if row["size_expected"] is not None:
        kw["size_expected"] = row["size_expected"]
    store.update_part(archive_id, row["idx"], **kw)
    if row["attempts"]:
        _seed_attempts(store, archive_id, row["idx"], row["attempts"])
    return True


def migrate_from_v1(output_dir: str, store: JobStore,
                    ledger: Optional[AttemptLedger] = None,
                    apply: bool = False) -> dict:
    """Adopt v1 jobs into ``store``; returns a counts/errors report.

    ``apply=False`` inspects only — zero DB writes, zero file writes.
    ``apply=True`` upserts each job and its parts and re-seeds statuses via
    ``JobStore.upsert_job`` / ``upsert_parts`` / ``update_part``. Running
    twice is a no-op: jobs already present are not recreated and parts already
    in their target state are left untouched.

    v1 files are NEVER modified. No bytes are fetched, renamed, or moved.

    ``ledger``, when given, receives ``observe_remote()`` for any part that
    carries Google's own attempt counter (``dl_count``) — v1 does not record
    these, but the seed is free if they exist.

    Returns ``{jobs_found, jobs_created, parts_seeded, parts_done,
    attempts_seeded, errors}``. When ``apply=False``, ``jobs_created``,
    ``parts_seeded`` and ``attempts_seeded`` are all 0 and ``errors`` still
    reports every validation problem (missing DONE files, unresolvable
    archive ids, unreadable state).
    """
    job_dirs = _find_v1_jobs(output_dir)
    jobs_found = len(job_dirs)
    jobs_created = parts_seeded = parts_done = attempts_seeded = 0
    errors: list[str] = []

    for job_dir in job_dirs:
        rec = _read_job(job_dir)
        for err in rec["errors"]:
            errors.append(f"{job_dir.name}: {err}")
        if rec["archive_id"] is None:
            continue
        parts_done += rec["parts_done"]
        if not apply:
            continue

        archive_id = rec["archive_id"]
        if store.get_job(archive_id) is None:
            jobs_created += 1
        store.upsert_job(rec["identity"], output_dir=str(job_dir))

        plans = [PartPlan(idx=r["idx"], filename=r["filename"], url=r["url"],
                          size_expected=r["size_expected"])
                 for r in rec["mapped"]]
        store.upsert_parts(archive_id, plans)
        parts_seeded += len(plans)

        for r in rec["mapped"]:
            if _apply_part(store, archive_id, r):
                attempts_seeded += r["attempts"]

        if ledger is not None:
            for r in rec["mapped"]:
                if r["dl_count"] is not None:
                    ledger.observe_remote(archive_id, r["idx"], r["dl_count"])

    if apply:
        store.emit("migration_applied",
                   jobs_found=jobs_found, jobs_created=jobs_created,
                   parts_seeded=parts_seeded, parts_done=parts_done,
                   attempts_seeded=attempts_seeded, errors=len(errors))

    return {
        "jobs_found": jobs_found,
        "jobs_created": jobs_created,
        "parts_seeded": parts_seeded,
        "parts_done": parts_done,
        "attempts_seeded": attempts_seeded,
        "errors": errors,
    }
