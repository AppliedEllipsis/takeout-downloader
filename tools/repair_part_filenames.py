#!/usr/bin/env python3
"""Repair a ledger's part filenames by deriving them from the minted URLs.

Two real cases, both measured on this deployment, both leaving a COMPLETE export reading
as incomplete because the report identifies parts by name:

  1. **Placeholder clobbering.** `upsert_parts` overwrote real names with the scrape's
     `download`, for every non-zip part, because the guard was a zip-only regex. One
     re-scrape blanked 25 of the 62-export's 68 names.
  2. **Percent-encoding.** Names recorded before the decoding fix stayed literally
     percent-encoded, so the 64-export's `.mbox` read as
     `All%20mail%20Including%20Spam%20and%20Trash-002.mbox` while the file on disk is
     `All mail Including Spam and Trash-002.mbox`. That alone made a complete export
     report as 18/19, 13.58 GB "short".

Both are derivations, not guesses: the ledger still holds each part's `minted_url`, and
that URL ends in the part's real name. This tool derives the name, then requires an
**exact byte-size match against a real file in the destination** before writing anything.
No match, no write — and the reason is printed.

Safe by construction: read-only against the archive, writes only the ledger, takes a
backup, and reports every part it could not resolve instead of inventing a name.

    python3 tools/repair_part_filenames.py --archive-id ID --ledger P --archive-dir D
    python3 tools/repair_part_filenames.py ... --apply
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys

# The module runs from a checkout (`tools/...`), from the deployed tree, or from /config
# on its own — so try every plausible root rather than one guessed grandparent. Running
# from `/config` made `dirname(dirname(__file__))` resolve to `/`, which is how the first
# attempt at this died with ModuleNotFoundError.
for _cand in (os.environ.get("AUTOPILOT_REPO"),
              os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
              os.getcwd(),
              "/work/.v3"):
    if _cand and os.path.isdir(_cand) and _cand not in sys.path:
        sys.path.insert(0, _cand)

from autopilot.ledger import (  # noqa: E402
    carries_part_identity,
    filename_aliases as candidates,
    open_ledger,
)
from autopilot.mint import part_filename_from_url  # noqa: E402

# `candidates` is `autopilot.ledger.filename_aliases`, imported under the local name so the
# tool and the report agree by construction. Two copies of that rule would drift, and the
# drift would surface as an export reading complete in one place and short in the other.


# drift would surface as an export reading complete in one place and short in the other.


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive-id", required=True)
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--archive-dir", required=True)
    ap.add_argument("--apply", action="store_true", help="write the repairs")
    args = ap.parse_args()

    dest = {n: os.path.getsize(os.path.join(args.archive_dir, n))
            for n in os.listdir(args.archive_dir)
            if os.path.isfile(os.path.join(args.archive_dir, n))}

    db = sqlite3.connect("file:%s?mode=ro" % args.ledger, uri=True, timeout=25)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT idx, filename, size_expected, minted_url FROM parts WHERE archive_id=? "
        "ORDER BY idx", (args.archive_id,)).fetchall()
    db.close()

    print("ledger parts      : %d" % len(rows))
    print("destination files : %d" % len(dest))
    print()

    fixes, already_ok, unresolved = [], [], []
    for r in rows:
        stored = r["filename"] or ""
        exp = r["size_expected"]

        def matches(name):
            got = dest.get(name)
            return name if got is not None and (exp is None or int(got) == int(exp)) else None

        # Already correct?
        if any(matches(c) for c in candidates(stored)):
            already_ok.append(r["idx"])
            continue

        # Derive from the minted URL, which ends in the real (percent-encoded) name.
        derived = None
        if r["minted_url"]:
            try:
                derived = part_filename_from_url(r["minted_url"])
            except Exception:
                derived = None

        found = None
        for cand in [derived] + (candidates(stored) if not derived else []):
            if not cand:
                continue
            # The derived name must carry identity AND match a real file by exact size.
            for form in candidates(cand):
                if carries_part_identity(form) and matches(form):
                    found = form
                    break
            if found:
                break

        if found:
            fixes.append((r["idx"], stored, found, exp))
        else:
            unresolved.append((r["idx"], stored, exp))

    print("=== already correct: %d ===" % len(already_ok))
    print("=== repairable: %d ===" % len(fixes))
    for idx, was, now, exp in fixes:
        print("   idx %-3s %r" % (idx, was))
        print("          -> %r  (size %s matches a real file)" % (now, exp))
    if unresolved:
        print()
        print("=== UNRESOLVED: %d — no name could be verified against the archive ===" % len(unresolved))
        for idx, stored, exp in unresolved:
            print("   idx %-3s %r  expected=%s  in dest by that size: %s" % (
                idx, stored, exp,
                [n for n, s in dest.items() if exp is not None and s == int(exp)] or "none"))

    if not args.apply:
        print()
        print("(report only — pass --apply to write %d name(s))" % len(fixes))
        return 0

    if not fixes:
        print()
        print("nothing to write")
        return 0

    backup = args.ledger + ".before-filename-repair"
    if not os.path.exists(backup):
        shutil.copy2(args.ledger, backup)
        print()
        print("backup written: %s" % backup)

    led = open_ledger(args.ledger)
    try:
        for idx, _was, now, _exp in fixes:
            led.set_part_filename(args.archive_id, idx, now)
    finally:
        led.close()
    print("wrote %d filename(s)" % len(fixes))

    db = sqlite3.connect("file:%s?mode=ro" % args.ledger, uri=True, timeout=25)
    db.row_factory = sqlite3.Row
    after = db.execute("SELECT idx, filename, size_expected FROM parts WHERE archive_id=?",
                       (args.archive_id,)).fetchall()
    still = [r["idx"] for r in after
             if not any(dest.get(c) is not None
                        and (r["size_expected"] is None
                             or dest[c] == int(r["size_expected"]))
                        for c in candidates(r["filename"] or ""))]
    print()
    print("=== after ===")
    print("  parts not resolvable against the archive: %d %s" % (len(still), still))
    return 0


if __name__ == "__main__":
    sys.exit(main())
