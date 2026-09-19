"""The rapt trap, pinned down.

The first real run failed because the scrape navigated to the *bare* archive URL
and read download links that carried no `rapt`. The mint then bounced to
`accounts.google.com/ServiceLogin` and the run reported "needs ReAuth" — on a
session that was fully authenticated.

Every test here is offline. `pick_rapt_url` is pure precisely so the decision
that was wrong can be tested without a browser, and `page_has_rapt` is the guard
that stops a rapt-less page from spending an attempt.
"""
import pytest

from autopilot.scrape import (
    ArchivePage,
    PartLink,
    archive_url,
    page_has_rapt,
    pick_rapt_url,
)

AID = "f470fe32-76d6-43a6-9321-dbc76834919e"
USER = "116943325794238708860"
RAPT = "AEjHL4P8g2b2zjspieMuYeMIkaI6PB2zw1r1A45UxNoPJqWtQY684dn"

BARE = archive_url(AID)
TOKENED = f"{BARE}?user={USER}&rapt={RAPT}"


# --------------------------------------------------------------------------
# pick_rapt_url — the decision that was wrong
# --------------------------------------------------------------------------
def test_bare_url_is_never_chosen():
    """The exact regression: the bare URL has no rapt and must be rejected."""
    assert pick_rapt_url([BARE], AID) is None


def test_tokened_url_is_chosen():
    assert pick_rapt_url([TOKENED], AID) == TOKENED


def test_first_match_wins_so_callers_control_precedence():
    """Callers pass newest-first; the first usable candidate must win."""
    newer = f"{BARE}?user={USER}&rapt=NEWER"
    older = f"{BARE}?user={USER}&rapt=OLDER"
    assert pick_rapt_url([newer, older], AID) == newer


def test_skips_bare_entries_that_precede_a_tokened_one():
    """Histories are full of bare navigations; they must not shadow the token."""
    assert pick_rapt_url([BARE, BARE, TOKENED], AID) == TOKENED


def test_url_for_a_different_archive_is_ignored():
    """A token is bound to its archive; borrowing another one is not help."""
    other = archive_url("00000000-0000-0000-0000-000000000000")
    assert pick_rapt_url([f"{other}?rapt={RAPT}", TOKENED], AID) == TOKENED


def test_rapt_on_a_non_archive_page_is_ignored():
    """`takeout.google.com/manage?rapt=…` is real (seen in a live history) but
    it is not an archive page and carries no per-part download links."""
    assert pick_rapt_url([f"https://takeout.google.com/manage?rapt={RAPT}"], AID) is None


def test_empty_and_none_candidates_are_safe():
    assert pick_rapt_url([], AID) is None
    assert pick_rapt_url(None, AID) is None
    assert pick_rapt_url(["", None], AID) is None


def test_presence_of_rapt_is_judged_not_validity():
    """A stale token is still *chosen* — only navigating can reveal staleness,
    and a stale one yields an un-tokened page which the caller then detects."""
    stale = f"{BARE}?rapt=definitely-not-a-live-token"
    assert pick_rapt_url([stale], AID) == stale


# --------------------------------------------------------------------------
# page_has_rapt — the guard that refuses to spend an attempt
# --------------------------------------------------------------------------
def _page(redirector, **kw):
    return ArchivePage(parts=[PartLink(index=0, raw_uri="", redirector=redirector)], **kw)


def test_page_with_a_tokened_redirector_is_mintable():
    assert page_has_rapt(_page(f"https://takeout.google.com/takeout/download?j={AID}&rapt={RAPT}"))


def test_page_with_an_untokened_redirector_is_not_mintable():
    """This is precisely what the bare navigation produced, live:
    `takeout/download?j=…&i=0&user=…` with no rapt."""
    assert not page_has_rapt(_page(f"https://takeout.google.com/takeout/download?j={AID}&i=0&user={USER}"))


def test_one_tokened_part_among_several_is_enough():
    page = ArchivePage(parts=[
        PartLink(index=0, raw_uri="", redirector="https://x/takeout/download?j=a"),
        PartLink(index=1, raw_uri="", redirector="https://x/takeout/download?j=b&rapt=x"),
    ])
    assert page_has_rapt(page)


def test_page_with_no_parts_is_not_mintable():
    assert not page_has_rapt(ArchivePage(parts=[]))


def test_empty_redirector_is_not_mintable():
    assert not page_has_rapt(_page(""))


# --------------------------------------------------------------------------
# wiring: the orchestrator must actually use these
# --------------------------------------------------------------------------
def test_run_module_recovers_a_rapt_url_and_guards_on_rapt():
    """The unit tests above prove the predicates are right. This proves the
    orchestrator calls them — the failure mode this project hit four times was a
    guard that was correct and never invoked."""
    import inspect

    import autopilot.run as run_mod

    src = inspect.getsource(run_mod)
    assert "recover_rapt_url(" in src, "run_once must try to recover a tokened URL"
    assert "page_has_rapt(page)" in src, "run_once must refuse to mint an un-tokened page"
    # and must no longer navigate to the bare URL unconditionally
    assert "read_archive(session, cfg.resolved_work_url()" not in src, (
        "navigating to the bare archive URL is what destroyed the rapt — it must "
        "be a fallback, not the default")
