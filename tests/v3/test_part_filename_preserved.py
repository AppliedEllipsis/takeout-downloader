"""A recorded part filename must survive a re-scrape. This regressed, and it cost data.

**Measured 2026-09-22.** One `--mint-only` run whose mint failed re-scraped the archive
page and blanked **25 of the 62-export's 68 filenames**, replacing every one with the
placeholder `download`. The disk was untouched — only the ledger was damaged — but the
ledger is what identifies parts, so completeness reporting and the destination-index skip
both went blind.

The cause was a guard that looked like it protected recorded names and did the opposite:

    PART_FILENAME_RE = re.compile(r"^takeout-\\d{8}T\\d{6}Z-\\d+-\\d+\\.zip$", re.I)
    if looks_like_part_filename(current) and not looks_like_part_filename(incoming):
        incoming = current      # keep the stored name

That predicate only ever matches **`.zip`** names. So a part whose real name was
`Louie 2018-057.mp4` failed the test, the guard did not fire, and the incoming placeholder
overwrote the real name. It was correct for zips and *actively destructive* for everything
else — and its comment promised the opposite, which is why nobody looked.

The corrected rule is about the INCOMING value: a placeholder must never displace a stored
name, whatever shape that name has. Google serves `.zip`, `.mp4` and `.mbox` today, and the
next export type will be something else again — so the rule cannot be a list of the shapes
we happen to have seen.

Offline.
"""
from __future__ import annotations

import pytest

from autopilot.ledger import carries_part_identity, open_ledger

MOUNTS = [("/", "ext4"), ("/config", "xfs")]

#: Real names observed on disk in `/opt/archives/google-takeout/braincreation`. The first
#: three are what the old predicate destroyed.
REAL_NON_ZIP_NAMES = [
    "Louie 2018-057.mp4",
    "All mail Including Spam and Trash-002.mbox",
    "Imagine Dragons - It_s Time - Philips Arena - Feb 26, 2014-002.mp4",
    "2000 0101 032848 028-042.mp4",
    "Pettett Wedding - First Dance(s)-055.mp4",
    "TESTING SAMPLE - Georgia Renaissance Festival 2021 June 1, 2021 VR180 3D 5.7K "
    "trusthawkmonkey2021-045.mp4",
]
REAL_ZIP_NAMES = [
    "takeout-20260919T043451Z-1-001.zip",
    "takeout-20260919T043451Z-3-006.zip",
]


@pytest.fixture()
def led(tmp_path):
    l = open_ledger(str(tmp_path / "state.db"), mounts=MOUNTS)
    yield l
    l.close()


# ---------------------------------------------------------------------------
# the predicate
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", REAL_NON_ZIP_NAMES + REAL_ZIP_NAMES)
def test_every_real_part_name_carries_identity(name):
    assert carries_part_identity(name) is True


@pytest.mark.parametrize("name", ["download", "download.zip", "", "   ", None])
def test_a_placeholder_never_carries_identity(name):
    assert carries_part_identity(name) is False


def test_a_bare_word_is_not_a_part_name():
    """The scrape has produced other non-names; a name with no extension is not one."""
    assert carries_part_identity("downloads") is False
    assert carries_part_identity("Unconfirmed") is False


# ---------------------------------------------------------------------------
# the regression, at the ledger
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", REAL_NON_ZIP_NAMES)
def test_a_non_zip_name_survives_a_rescrape(led, name):
    """THE BUG. Before the fix this returned `download` for every one of these."""
    led.upsert_parts("a1", [(0, name, 100)])
    assert led.part("a1", 0)["filename"] == name

    # A later scrape: the page cannot produce a real name, only the placeholder.
    led.upsert_parts("a1", [(0, "download", 100)])
    assert led.part("a1", 0)["filename"] == name, (
        "a re-scrape replaced a real non-zip filename with the placeholder")


def test_a_zip_name_still_survives_a_rescrape(led):
    """The behaviour the old guard got right, kept honest rather than assumed."""
    led.upsert_parts("a1", [(0, REAL_ZIP_NAMES[0], 100)])
    led.upsert_parts("a1", [(0, "download", 100)])
    assert led.part("a1", 0)["filename"] == REAL_ZIP_NAMES[0]


def test_the_whole_62_export_survives_a_rescrape(led):
    """The exact shape of the damage: 40 zips and 28 non-zip parts, re-scraped."""
    rows = [(i, n, 1000 + i) for i, n in enumerate(REAL_ZIP_NAMES * 20)]
    rows += [(100 + i, n, 2000 + i) for i, n in enumerate(REAL_NON_ZIP_NAMES * 5)]
    led.upsert_parts("a1", rows)
    before = {r.idx: r.filename for r in led.parts("a1")}
    assert len(before) == len(rows)
    assert not [n for n in before.values() if n == "download"]

    led.upsert_parts("a1", [(i, "download", s) for i, _, s in rows])
    after = {r.idx: r.filename for r in led.parts("a1")}
    assert after == before, "a re-scrape blanked part filenames"


def test_a_first_scrape_records_the_placeholder(led):
    """With nothing to protect, the placeholder is stored as-is — it is still better
    than NULL, and `set_part_filename` replaces it after minting."""
    led.upsert_parts("a1", [(0, "download", 100)])
    assert led.part("a1", 0)["filename"] == "download"


def test_a_real_incoming_name_replaces_a_placeholder(led):
    """The other direction must keep working: the scrape's placeholder must not block a
    real name arriving later."""
    led.upsert_parts("a1", [(0, "download", 100)])
    led.upsert_parts("a1", [(0, "takeout-20260919T043451Z-1-001.zip", 100)])
    assert led.part("a1", 0)["filename"] == "takeout-20260919T043451Z-1-001.zip"


def test_a_rescrape_never_resets_progress_or_size(led):
    """The property `upsert_parts` promises: descriptive fields refresh, progress does
    not. A blanked filename is exactly the kind of damage this must not do twice."""
    led.upsert_parts("a1", [(0, "Louie 2018-057.mp4", 100)])
    led.set_part_status("a1", 0, "partial")
    led.upsert_parts("a1", [(0, "download", 999)])
    row = led.part("a1", 0)
    assert row["filename"] == "Louie 2018-057.mp4"
    assert row["status"] == "partial"


# ---------------------------------------------------------------------------
# the pattern itself must not come back
# ---------------------------------------------------------------------------
def test_the_zip_only_predicate_is_gone():
    """Source-level, deliberately. The bug was a regex that *read* like a guard for real
    names, so a behavioural test on zips alone would have kept passing. If this pattern
    returns, so does the damage — unless someone also re-reads the comment above it."""
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[2] / "autopilot" / "ledger.py"
    text = src.read_text(encoding="utf-8")
    assert "PART_FILENAME_RE" not in text, (
        "a zip-only part-filename regex is back; it destroyed 25 filenames once already")
    assert r"\.zip$" not in text, "a name pattern anchored to .zip is back"
    assert "carries_part_identity" in text
