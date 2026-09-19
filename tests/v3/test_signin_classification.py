"""The ReAuth classifier must not fire on a HEALTHY mint.

Measured (docs/v3/01-ARCHITECTURE.md): the successful chain is

    302 takeout/download -> archive 302 -> accounts.google.com/RotateCookiesPage?rot=3
      -> 302 takeout-download.usercontent.google.com/...zip -> 200

So `accounts.google.com` appears on the path to SUCCESS. The old test —
`ACCOUNTS_HOST in r.location` — therefore reported a working mint as
`needs_reauth` whenever the RotateCookiesPage hop arrived in a batch before the
file-host hop. It survived only because the file-host check happens to run first
within a batch, which makes the bug timing-dependent rather than absent.

Offline.
"""
import pytest

from autopilot.mint import is_signin_location

SIGNIN = [
    # the measured ReAuth demands
    "https://accounts.google.com/ServiceLogin?passive=1209600&continue=https://takeout.google.com/",
    "https://accounts.google.com/v3/signin/challenge/pwd?TL=ACv9tz&authuser=0",
    "https://accounts.google.com/v3/signin/identifier?continue=https%3A%2F%2Ftakeout.google.com",
    "https://accounts.google.com/InteractiveLogin?continue=x",
]

BENIGN = [
    # the measured SUCCESS path
    "https://accounts.google.com/RotateCookiesPage?og_pid=192&rot=3",
    "https://accounts.google.com/rotatecookiespage?rot=3",   # case-insensitive
    # and things that are not the accounts host at all
    "https://takeout.google.com/manage/archive/abc",
    "https://takeout.google.com/takeout/download?j=abc",
    "https://takeout-download.usercontent.google.com/download/takeout-x.zip?j=abc",
    "",
]


@pytest.mark.parametrize("url", SIGNIN)
def test_a_real_challenge_is_classified_as_signin(url):
    assert is_signin_location(url) is True


@pytest.mark.parametrize("url", BENIGN)
def test_a_healthy_mint_hop_is_not_classified_as_signin(url):
    assert is_signin_location(url) is False


def test_rotate_cookies_specifically_must_not_trigger_reauth():
    """The single most important case: it is on the path to success."""
    assert is_signin_location(
        "https://accounts.google.com/RotateCookiesPage?og_pid=192&rot=3") is False


def test_the_old_predicate_is_gone_from_source():
    """The host-only test must not survive anywhere in the mint loop."""
    import inspect

    import autopilot.mint as mint_mod

    src = inspect.getsource(mint_mod.mint)
    assert "ACCOUNTS_HOST in r.location" not in src, (
        "matching the accounts host alone misclassifies RotateCookiesPage")
    assert "is_signin_location(r.location)" in src
