"""Bugs 10 and 11 — the two the first *successful* run exposed.

Both were invisible while the pipeline was failing, and both are in seams.

  10. The refreshed-rapt retry required a `j` parameter that the bounce does not
      always carry, discarding a live token. A mintable export reported "no
      archive bounce in the chain".

  11. Every part was named `download` — the basename of the redirector path
      `takeout/download?j=...`. Staging under that name, and the destination
      index being keyed by filename, means a multi-part export silently collapses
      onto one file.

Offline.
"""
import os

from autopilot.mint import (
    archive_id_from_archive_url,
    build_redirector,
    extract_redirects,
    parse_query,
    part_filename_from_url,
)
from autopilot.mover import move_part

AID = "f9a17be0-65e2-4b1f-a9c6-04c840204419"
USER = "116943325794238708860"
RAPT = "AEjHL4P8g2b2zjspieMuYeMIkaI6PB2zw1r1A45UxNoPJqWtQY684dn"

# Measured live 2026-09-19 — the bounce that carries a fresh rapt and NO `j`.
BOUNCE_NO_J = f"https://takeout.google.com/manage/archive/{AID}?user={USER}&pli=1&rapt={RAPT}"
# The older bounce shape, which does carry `j`.
BOUNCE_WITH_J = f"{BOUNCE_NO_J}&j={AID}"


# --------------------------------------------------------------------------
# bug 10 — the token must survive a bounce that omits `j`
# --------------------------------------------------------------------------
def test_archive_id_is_recoverable_from_the_path_alone():
    assert archive_id_from_archive_url(BOUNCE_NO_J) == AID
    assert archive_id_from_archive_url(BOUNCE_WITH_J) == AID


def test_archive_id_helper_is_safe_on_junk():
    assert archive_id_from_archive_url("") is None
    assert archive_id_from_archive_url("https://takeout.google.com/manage") is None
    assert archive_id_from_archive_url("https://x/manage/archive/") is None


def test_bounce_without_j_still_yields_a_usable_archive_id():
    """The exact regression: `params.get("j")` alone is falsy here, and the old
    code therefore refused to retry with a token it had just been handed."""
    params = parse_query(BOUNCE_NO_J)
    assert not params.get("j"), "this bounce deliberately has no j"
    assert params.get("rapt")
    archive_id = (params.get("j")
                  or parse_query("https://takeout.google.com/takeout/download?j=" + AID).get("j")
                  or archive_id_from_archive_url(BOUNCE_NO_J))
    assert archive_id == AID
    rebuilt = build_redirector(archive_id, 0, USER, params["rapt"])
    assert AID in rebuilt and "rapt=" in rebuilt


def test_the_bounce_is_actually_extractable_from_cdp_events():
    """Detection is useless if the hop never surfaces."""
    events = [("Network.responseReceived", {
        "response": {"url": "https://takeout.google.com/takeout/download?j=x",
                     "status": 302, "headers": {"location": BOUNCE_NO_J}},
    })]
    redirects = extract_redirects(events)
    assert redirects
    assert any("/manage/archive/" in (r.location or "") for r in redirects)


def test_refresh_branch_no_longer_requires_j_in_source():
    """Ordering/predicate asserted, not assumed: the retry must key on the rapt
    and fall back for the archive id."""
    import inspect

    import autopilot.mint as mint_mod

    src = inspect.getsource(mint_mod.mint)
    assert 'params.get("rapt") and params.get("j")' not in src, (
        "the old predicate demanded `j` and discarded a valid rapt")
    assert "archive_id_from_archive_url(refresh.location)" in src, (
        "the archive id must be recoverable from the path when the bounce omits j")


# --------------------------------------------------------------------------
# bug 11 — the real filename, or a multi-part export collapses
# --------------------------------------------------------------------------
def test_real_filename_comes_off_the_minted_url():
    url = ("https://takeout-download.usercontent.google.com/download/"
           "takeout-20260919T163231Z-1-001.zip?j=" + AID + "&i=0&user=" + USER)
    assert part_filename_from_url(url) == "takeout-20260919T163231Z-1-001.zip"


def test_the_redirector_basename_is_never_used_as_a_filename():
    """This is the whole bug: `download` is what the scrape sees, for EVERY part."""
    redirector = build_redirector(AID, 0, USER, RAPT)
    assert redirector.split("?")[0].rsplit("/", 1)[-1] == "download"
    # and the helper refuses it, rather than propagating it
    assert part_filename_from_url(redirector) == ""


def test_distinct_parts_yield_distinct_filenames():
    """The property whose absence caused silent data loss in the destination
    index, which is keyed by filename."""
    names = {
        part_filename_from_url(
            "https://takeout-download.usercontent.google.com/download/"
            f"takeout-20260919T163231Z-1-{i:03d}.zip?j={AID}&i={i}")
        for i in range(1, 4)
    }
    assert len(names) == 3, f"parts collided onto {names}"


def test_filename_helper_never_invents_a_name():
    assert part_filename_from_url("") == ""
    assert part_filename_from_url("https://host/") == ""
    assert part_filename_from_url("https://host/download") == ""
    assert part_filename_from_url("https://host/takeout") == ""


def test_move_part_uses_the_given_filename_not_the_source_basename(tmp_path):
    """The move must land under the real name even though the staging path is
    called `download` — which is exactly the live shape."""
    staging = tmp_path / "staging"
    dest = tmp_path / "dest"
    staging.mkdir()
    dest.mkdir()
    src = staging / "download"
    src.write_bytes(b"x" * 32)

    result = move_part(str(src), str(dest), filename="takeout-2026-1-001.zip",
                       expected_size=32)
    assert result.action == "moved"
    assert os.path.exists(dest / "takeout-2026-1-001.zip")
    assert not os.path.exists(dest / "download")


def test_move_part_still_defaults_to_the_basename_for_compatibility(tmp_path):
    staging = tmp_path / "s"
    dest = tmp_path / "d"
    staging.mkdir()
    dest.mkdir()
    (staging / "plain.zip").write_bytes(b"y" * 8)
    result = move_part(str(staging / "plain.zip"), str(dest), expected_size=8)
    assert result.action == "moved"
    assert os.path.exists(dest / "plain.zip")


def test_multi_part_move_does_not_collapse(tmp_path):
    """The end-to-end consequence: three parts, three files, no skips."""
    staging = tmp_path / "s"
    dest = tmp_path / "d"
    staging.mkdir()
    dest.mkdir()
    for i in (1, 2, 3):
        (staging / "download").write_bytes(b"z" * (10 * i))
        move_part(str(staging / "download"), str(dest),
                  filename=f"takeout-part-{i:03d}.zip", expected_size=10 * i)
    landed = sorted(p.name for p in dest.iterdir())
    assert landed == ["takeout-part-001.zip", "takeout-part-002.zip", "takeout-part-003.zip"]
