# Web-Hosted Takeout Browser + Download Manager — Planning Index

Status: **IMPLEMENTED (Phases 1-10)**. This folder is the design authority; the
code in `manager/`, `helpers/` (v4), `webgui/`, and the `takeout_dl.py` callback
seam was built from it. Phases 1-5, 8, 9 are verified by offline tests in
`manager/tests/`; phases 6-7 (webtop container + Cloudflare tunnel) are authored
infra whose runtime gate is server-side. See `07-build-phases.md` for the
per-phase status and `09-smoke-test.md` for the end-to-end server runbook.

## The one-paragraph idea

Run a real Chrome browser *on the server*, inside a webtop (KasmVNC) container
you reach through a Cloudflare tunnel. You log into Google once in that browser;
the profile persists. A custom extension captures the Takeout "all exports"
payload and POSTs it to a local **manager service**, which drives the existing
`takeout_dl.py` engine to download every part using server storage and
bandwidth. Because the browser and the downloader share the **same server IP**,
the IP-bound Takeout cookie is always valid — no SOCKS tunnel, no proxied local
Chrome. When the cookie expires (~45 min), the extension auto-re-captures from
the still-logged-in browser. The manager shows a live progress UI (viewable as a
browser tab inside the desktop), notifies you over Telegram, and exposes a
control API the Pi coding agent can call to debug, decide, and learn. The end
goal: capture a workflow once, then **repeat it without an LLM**.

## Why this beats the current workflow

| Pain today (`docs/SERVER_DOWNLOAD.md`) | This design |
|---|---|
| `ssh -D 1080` SOCKS tunnel + separate proxied Chrome on your laptop | Browser lives on the server; capture is same-IP by construction |
| Manual re-capture + `nano /tmp/payload.json` every ~45 min | Extension auto-re-captures from the persistent logged-in profile |
| Clipboard paste is the only transport | Extension POSTs straight to the manager; clipboard kept as fallback |
| Progress watched via `tail -f` + `watch ls` | Live web progress manager + Telegram status |
| Needs you (or an LLM) in the loop each run | Repeat mode replays a saved workflow with no LLM |

## Locked decisions (from planning Q&A)

1. **Desktop base:** `linuxserver/webtop` (KasmVNC). Built-in web audio,
   persistent `/config` profile, GPU passthrough. Chromium is launched with
   `--remote-debugging-port=9222` for agent (CDP) control.
2. **Tunnel scope:** Cloudflare exposes **only** the KasmVNC portal. Manager UI,
   CDP, and the control API are reached over SSH port-forwards. Most locked-down
   option — a browser logged into your Google account is never world-reachable.
3. **Login model:** Persistent profile + one-time manual login + auto-re-capture.
   No stored passwords, no automated 2FA. Full re-auth needed → alert you.
4. **Capture transport:** Extension POSTs the captured payload to the local
   manager service; clipboard "Copy as JSON" stays as a manual/debug fallback.
5. **Notifications/control:** Telegram bot using the existing `telegram.token`
   from `~/.pi/agent/auth.json`. Progress, error, and login-needed events, plus
   status commands.

## Document map

| Doc | What it covers | Audience |
|---|---|---|
| [`01-architecture.md`](./01-architecture.md) | Full component wiring, data flow, ports, trust boundaries | Anyone |
| [`02-manager-service.md`](./02-manager-service.md) | Manager API, engine integration, state model, repeat-without-LLM | Implementer |
| [`03-extension-v4.md`](./03-extension-v4.md) | Extension v4: POST handoff, auto-re-capture, agent hooks | Implementer |
| [`04-decision-trees.md`](./04-decision-trees.md) | Flowcharts + runbooks for failure handling | Weaker models / operators |
| [`05-deployment.md`](./05-deployment.md) | Server build, compose, Cloudflare tunnel, security hardening | Operator |
| [`06-telegram.md`](./06-telegram.md) | Telegram bot wiring, commands, config keys | Implementer |
| [`07-build-phases.md`](./07-build-phases.md) | Ordered build phases with acceptance gates | Implementer |
| [`08-decisions-log.md`](./08-decisions-log.md) | Build-time decisions (the "why" companion to the code) | Anyone |
| [`09-smoke-test.md`](./09-smoke-test.md) | Server deploy + end-to-end smoke-test runbook | Operator |

## Existing assets this builds on

- `takeout_dl.py` — proven internal threaded downloader. Inspects first bytes
  (`PK\x03\x04` vs HTML login page) so it never saves a fake zip. Already
  auto-resumes by polling a payload file and resuming partials via HTTP Range.
- `helpers/` — MV3 extension that captures from the **final** download host and
  can "Copy ALL exports" (enumerates every part URL + size).
- `takeout_payload.py` — the v1 payload schema contract (extension ↔ engine).
- `_ask/debian_lxqt_env/` + `_ask/linux_desktop_env/` — webtop/KasmVNC compose
  references (audio, persistence, GPU).
- `d:/_projects/openclaw/` — lean Xvfb + x11vnc + noVNC + CDP reference (fallback
  pattern for the agent-control wiring).

## Non-goals (explicitly out of scope)

- No stored Google credentials, no automated password/2FA entry.
- No public, unauthenticated control of the browser or manager.
- No change to the engine's core download/integrity logic — we wrap it, not
  rewrite it.

- `11-session-changes.md` — post-Phase-10 changes (paste box, labels, job deletion, cache-busting, CDP extension reload).
- `12-operations-runbook.md` — daily operations, recovery, and monitoring
- `13-migration-diskfull.md` — disk-full root cause, repo migration to LUKS disk, compose env-file gotchas.
