"""The report must see the ARCHIVE, not just this ledger's own bookkeeping.

**Measured 2026-09-22, on the live 62-product export.** The archive was complete and
CRC-verified on disk — 68/68 parts, 126,868,242,288 bytes, all 41 zips CRC-clean. The
report said:

    **Verdict: BLOCKED — re-authentication required (0/68 held)**
    - parts: **0/68** (0%)
    - bytes on disk: 0 of 126867340402 expected

Every word of that is wrong for the question an operator is asking ("is my data safe?").
The reason is structural: v2 did the downloading, so this ledger had transferred nothing,
and the report only ever looked at `status` and `size_on_disk`. A report that describes a
complete archive as BLOCKED and 0% is worse than no report, because the decision it feeds
is "delete or not".

`build_report(present=...)` fixes it: a part counts as held if the ledger says `done` **or**
its file is in the destination at the expected size. Two properties are load-bearing:

* a **size mismatch is not a pass** — it becomes a warning, because "a file of that name
  exists" is not the same claim as "the file is correct";
* `parts_in_archive` is reported **separately**, because "we downloaded this" and "it was
  already here" are different claims and folding them together would flatter the tool.

Offline.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from autopilot.report import build_report

NOW = datetime(2026, 9, 22, 18, 0, tzinfo=timezone.utc)


@dataclass
class P:
    idx: int
    status: str = "pending"
    filename: Optional[str] = None
    size_expected: Optional[int] = 100
    size_on_disk: int = 0
    attempts: int = 0
    dl_count_seen: Optional[int] = None


class FakeIndex:
    """Stands in for `mover.DestinationIndex`, which exposes `size_of`."""

    def __init__(self, mapping):
        self.names = dict(mapping)

    def size_of(self, name):
        return self.names.get(name)


def parts(*specs):
    return [P(**s) for s in specs]


# ---------------------------------------------------------------------------
# the live failure, as a test
# ---------------------------------------------------------------------------
def test_a_complete_archive_is_not_reported_as_blocked():
    """The exact live shape: needs_reauth, nothing transferred, everything on disk."""
    rows = parts(
        {"idx": 0, "filename": "takeout-a-1-001.zip", "size_expected": 1000},
        {"idx": 1, "filename": "Louie 2018-057.mp4", "size_expected": 2000},
    )
    present = {"takeout-a-1-001.zip": 1000, "Louie 2018-057.mp4": 2000}

    rep = build_report(rows, archive_id="a1", status="needs_reauth", now=NOW,
                       present=present)

    assert rep.parts_done == 2
    assert rep.parts_in_archive == 2
    assert rep.missing_indices == []
    assert rep.complete is True
    assert rep.verdict.startswith("COMPLETE"), rep.verdict
    assert "BLOCKED" not in rep.verdict


def test_the_ledger_only_behaviour_is_unchanged_when_no_index_is_given():
    """`present=None` must reproduce the old semantics exactly, or every existing
    caller silently changes meaning."""
    rows = parts({"idx": 0, "filename": "x.zip", "size_expected": 1000})
    rep = build_report(rows, archive_id="a1", status="needs_reauth", now=NOW)
    assert rep.parts_done == 0
    assert rep.parts_in_archive == 0
    assert rep.missing_indices == [0]
    assert "BLOCKED" in rep.verdict


# ---------------------------------------------------------------------------
# provenance is stated, not folded in
# ---------------------------------------------------------------------------
def test_the_markdown_says_the_bytes_did_not_come_from_this_ledger():
    rows = parts({"idx": 0, "filename": "x.zip", "size_expected": 10})
    md = build_report(rows, archive_id="a1", status="needs_reauth", now=NOW,
                      present={"x.zip": 10}).as_markdown()
    assert "already in the archive" in md
    assert "not** transferred by this ledger" in md


def test_a_part_the_ledger_transferred_is_not_counted_as_archive_present():
    """No double-counting: `done` and 'in the archive' are the same bytes."""
    rows = parts({"idx": 0, "filename": "x.zip", "size_expected": 10, "size_on_disk": 10,
                  "status": "done"})
    rep = build_report(rows, archive_id="a1", status="complete", now=NOW,
                       present={"x.zip": 10})
    assert rep.parts_done == 1
    assert rep.parts_in_archive == 0
    assert rep.bytes_on_disk == 10


# ---------------------------------------------------------------------------
# a name match is not proof
# ---------------------------------------------------------------------------
def test_a_size_mismatch_in_the_archive_is_not_counted_as_held():
    """'A file of that name exists' is not 'the file is correct'. This is the check that
    lets a truncated part be caught rather than counted."""
    rows = parts({"idx": 7, "filename": "x.zip", "size_expected": 1000})
    rep = build_report(rows, archive_id="a1", status="needs_reauth", now=NOW,
                       present={"x.zip": 499})
    assert rep.parts_done == 0
    assert rep.parts_in_archive == 0
    assert rep.missing_indices == [7]
    assert rep.bytes_on_disk == 0
    assert any("not the expected" in w for w in rep.warnings), rep.warnings


def test_an_unknown_expected_size_still_counts_the_file():
    """A part row with no recorded size cannot be checked; counting it is the honest
    reading, and the alternative would report a present file as missing."""
    rows = parts({"idx": 0, "filename": "x.mbox", "size_expected": None})
    rep = build_report(rows, archive_id="a1", status="needs_reauth", now=NOW,
                       present={"x.mbox": 12345})
    assert rep.parts_done == 1
    assert rep.bytes_on_disk == 12345


def test_a_differently_named_file_does_not_count():
    rows = parts({"idx": 0, "filename": "x.zip", "size_expected": 100})
    rep = build_report(rows, archive_id="a1", status="needs_reauth", now=NOW,
                       present={"something-else.zip": 100})
    assert rep.parts_done == 0
    assert rep.missing_indices == [0]


def test_a_placeholder_filename_cannot_match_the_archive():
    """A part still named `download` has no identity, so nothing in the archive may be
    claimed for it. Matching on the placeholder would let an unidentified part read as
    present — the same class of mistake as the regex that destroyed names, and the
    reason the lookup is gated on `carries_part_identity`."""
    rows = parts({"idx": 0, "filename": "download", "size_expected": 100})
    rep = build_report(rows, archive_id="a1", status="needs_reauth", now=NOW,
                       present={"download": 100})
    assert rep.parts_done == 0, "a placeholder name must never verify against the archive"
    assert rep.missing_indices == [0]


# ---------------------------------------------------------------------------
# shapes
# ---------------------------------------------------------------------------
def test_it_accepts_a_destination_index_object():
    rows = parts({"idx": 0, "filename": "x.zip", "size_expected": 100})
    rep = build_report(rows, archive_id="a1", status="needs_reauth", now=NOW,
                       present=FakeIndex({"x.zip": 100}))
    assert rep.parts_done == 1 and rep.parts_in_archive == 1


def test_a_partial_archive_gives_an_incomplete_verdict_not_a_complete_one():
    rows = parts(
        {"idx": 0, "filename": "a.zip", "size_expected": 10},
        {"idx": 1, "filename": "b.zip", "size_expected": 20},
    )
    rep = build_report(rows, archive_id="a1", status="needs_reauth", now=NOW,
                       present={"a.zip": 10})
    assert rep.parts_done == 1
    assert rep.missing_indices == [1]
    assert not rep.complete
    assert not rep.verdict.startswith("COMPLETE"), rep.verdict
