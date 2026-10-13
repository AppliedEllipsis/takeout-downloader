"""Owned integration adapter: models frame ownership, not a live browser."""
from fake_cdp import FakeTransport as BaseTransport


class FakeTransport(BaseTransport):
    def __init__(self, *args, **kwargs):
        results = dict(kwargs.pop("command_results", {}) or {})
        results.setdefault("Page.getFrameTree", {"frameTree": {"frame": {"id": "F1"}}})
        super().__init__(*args, command_results=results, **kwargs)
