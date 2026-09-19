"""The v3 autopilot — browser-mints, transporter-moves.

Design: `docs/v3/01-ARCHITECTURE.md`. Measurements: `.wiki/measured-facts.md`.

The one-sentence architecture: **only a real browser can mint a download URL**
(it alone can satisfy the password ReAuth that produces the `rapt` token), and
once minted an ordinary HTTP client moves the bytes, because `Range` resumes are
measured free.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
