"""takeout2 — the v2 engine for attempt-limited multi-TB Google Takeout.

The redesign in one line: **bytes-on-the-wire is the only acceptable reason
to contact Google.** Everything else — how many parts, what size, is it valid,
how many attempts remain — comes from local state or from Google's own
manage-page counter, scraped free.

See ``docs/v2/README.md`` for the full plan.
"""

__version__ = "2.0.0"
