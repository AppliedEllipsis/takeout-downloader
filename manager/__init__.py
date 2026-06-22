"""Takeout web manager.

A small FastAPI app that receives captured payloads from the browser extension,
drives the existing ``takeout_dl`` engine to download every part, tracks job
state, serves a live progress UI, exposes a control API, and notifies over
Telegram.

It is localhost-only. Nothing here is exposed by the Cloudflare tunnel.

See ``docs/webgui/02-manager-service.md`` for the full design.
"""

__version__ = "0.1.0"
