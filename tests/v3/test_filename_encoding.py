"""Regression tests for the two defects the 64-product mint pass exposed.

Both were found by running the real workflow against a real export, not by reasoning:

1. Part 18 of the 64-product export is
   `All%20mail%20Including%20Spam%20and%20Trash-002.mbox` — the filename was taken from
   the minted URL's path WITHOUT percent-decoding, so the part would have landed on the
   archive under a name no human wrote and no later tool would match against Google's
   listing. It is also an `.mbox`, not a `.zip`, so nothing may assume the extension.

2. `--clean-staging`: staging was never freed, so the 131.6 GB pull wanted
   131.6 GB of staging PLUS rclone's 100 GB VFS cache on a volume with 194 GB free.
"""
from __future__ import annotations

from autopilot.mint import part_filename_from_url


def test_percent_encoded_filename_is_decoded():
    """Observed live 2026-09-21, 64-product export, part idx 18."""
    url = ("https://takeout-download.usercontent.google.com/download/"
           "All%20mail%20Including%20Spam%20and%20Trash-002.mbox?j=abc&i=18")
    assert part_filename_from_url(url) == "All mail Including Spam and Trash-002.mbox"


def test_a_non_zip_part_keeps_its_real_extension():
    """Google serves .mbox as well as .zip. Nothing may force a .zip suffix."""
    url = "https://takeout-download.usercontent.google.com/download/Inbox-001.mbox?j=x"
    assert part_filename_from_url(url) == "Inbox-001.mbox"


def test_ordinary_zip_names_are_untouched():
    url = ("https://takeout-download.usercontent.google.com/download/"
           "takeout-20260919T163231Z-1-001.zip?j=x")
    assert part_filename_from_url(url) == "takeout-20260919T163231Z-1-001.zip"


def test_the_redirector_basename_is_still_rejected():
    """`takeout/download?j=...` yields the literal basename `download` for every part;
    staging those under that name is the multi-part data-loss bug this function exists
    to prevent."""
    assert part_filename_from_url("https://takeout.google.com/takeout/download?j=x") == ""
    assert part_filename_from_url("https://takeout.google.com/download?j=x") == ""


def test_plus_is_not_decoded_as_space():
    """Only %XX decodes. `+` is a legal filename character and must survive — the
    filesystem has no query-string semantics."""
    url = "https://takeout-download.usercontent.google.com/download/a+b-001.zip?j=x"
    assert part_filename_from_url(url) == "a+b-001.zip"
