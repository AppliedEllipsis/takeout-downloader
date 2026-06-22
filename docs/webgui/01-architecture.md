# 01 — System Architecture

## Component overview

```
                          ┌─────────────────────────────────────────────┐
                          │  REMOTE SERVER (188.245.169.166)              │
                          │                                               │
  ┌──────────────┐        │  ┌─────────────────────────────────────────┐ │
  │  You (phone/ │  CF    │  │  webtop container (linuxserver/webtop)    │ │
  │  laptop)     │ tunnel │  │                                           │ │
  │  browser     ├────────┼──┼─► KasmVNC portal  :3000 (audio, web UI)   │ │
  └──────────────┘        │  │      │                                    │ │
                          │  │      ▼                                    │ │
  ┌──────────────┐        │  │  ┌──────────────────────────────────┐     │ │
  │ Pi coding    │  SSH   │  │  │ Chromium (persistent /config)    │     │ │
  │ agent        ├────────┼──┼─►│  - logged into Google            │     │ │
  └──────────────┘  :9222 │  │  │  - Takeout extension v4          │     │ │
        │                 │  │  │  - tabs: takeout, manager UI     │     │ │
        │ SSH :8080       │  │  │  - bookmarks + pinned ext        │     │ │
        │ (manager API)   │  │  │  --remote-debugging-port=9222    │     │ │
        ▼                 │  │  └──────────────┬───────────────────┘     │ │
  ┌──────────────┐        │  │                 │ HTTP POST /api/payload  │ │
  │ control API  │        │  │                 ▼                         │ │
  │ + repeat mode│        │  │  ┌──────────────────────────────────┐     │ │
  └──────────────┘        │  │  │ Manager service (FastAPI) :8080  │     │ │
                          │  │  │  - /api/payload  (capture sink)  │     │ │
                          │  │  │  - /api/jobs     (progress)      │     │ │
                          │  │  │  - /api/control  (agent hooks)   │     │ │
                          │  │  │  - web UI (progress manager)     │     │ │
                          │  │  │  - drives takeout_dl engine      │     │ │
                          │  │  │  - Telegram notifier             │     │ │
                          │  │  └──────────────┬───────────────────┘     │ │
                          │  │                 │ in-process / subprocess  │ │
                          │  │                 ▼                         │ │
                          │  │  ┌──────────────────────────────────┐     │ │
                          │  │  │ takeout_dl engine                │     │ │
                          │  │  │  download_exports() + resume     │     │ │
                          │  │  └──────────────┬───────────────────┘     │ │
                          │  └─────────────────┼───────────────────────┘ │
                          │                    ▼                          │
                          │  /opt/storage.jfs002/google-takeout/          │
                          │     <account>/<export-timestamp>/  + manifest │
                          │                                               │
                          │   Telegram bot ──► your Telegram channel      │
                          └─────────────────────────────────────────────┘
```

## Why same-IP capture is the whole point

Google binds the Takeout download session to the IP that initiated it (see
`docs/SERVER_DOWNLOAD.md`). Today you bridge this with `ssh -D 1080` + a proxied
local Chrome so the capture exits the server IP. When the **browser itself runs
on the server**, the capturing browser and the downloading engine already share
one IP. The cookie is valid by construction. The SOCKS dance is deleted.

## Ports and exposure

| Port | Service | Exposure | Reached by |
|---|---|---|---|
| 3000 | KasmVNC HTTP portal | **Cloudflare tunnel (public, Access-gated)** | You, anywhere |
| 3001 | KasmVNC HTTPS portal | local only (tunnel uses 3000) | — |
| 8080 | Manager web UI + API | localhost only | You (in-desktop tab) + SSH forward |
| 9222 | Chromium CDP | localhost only | Pi agent via SSH forward |

Only **3000** leaves the box, and it sits behind Cloudflare Access. Everything
that can *control* the browser or the downloader (8080, 9222) is localhost-only
and reached through SSH port-forwards:

```bash
# Pi agent / you, from a trusted machine:
ssh -L 8080:127.0.0.1:8080 -L 9222:127.0.0.1:9222 -N ellipsis@188.245.169.166
```

## Trust boundaries

1. **Public edge:** only the KasmVNC portal, gated by Cloudflare Access. A leaked
   URL alone does not grant entry (Access still requires your identity).
2. **Desktop session:** anyone inside the KasmVNC session controls a
   Google-logged-in browser. Treat KasmVNC credentials as account-equivalent.
3. **localhost control plane:** manager API + CDP. Reachable only via SSH, so it
   inherits SSH's auth. The Pi agent uses this plane.
4. **Engine + storage:** the manager is the only writer to the output dirs; it
   enforces `validate_output_dir` (allowlist) exactly as the CLI does today.

## Data flow: one successful download cycle

1. You open the KasmVNC portal, log into Google in Chromium (one time).
2. You start a Takeout export on `takeout.google.com` and click Download.
3. Extension v4 captures the request on the final host
   (`takeout-download.usercontent.google.com`), enumerates all parts, and
   **POSTs** the payload to `http://127.0.0.1:8080/api/payload`.
4. Manager validates the cookie, derives the output dir from the account label
   + export timestamp (`<root>/google-takeout/<account>/<YYYY-MM-DD-HH-MM-SS>/`),
   creates a **job**, writes a `manifest.json`, and starts the engine
   (`download_exports`) with the chosen parallelism.
5. Manager streams progress to its web UI and emits a Telegram "started" message.
6. Engine downloads each part, inspecting first bytes; partials resume via Range.
7. **Cookie expires** → engine raises an auth challenge → manager flips the job
   to `needs_cookie`, fires Telegram alert + KasmVNC sound, and signals the
   extension to **auto-re-capture** (see `03-extension-v4.md`). Fresh cookie
   POSTs in, manager resumes the same job from partials. No restart.
8. All parts verified → job `complete` → Telegram "done" with totals → manager
   records the workflow for **repeat-without-LLM** replay.

## Repeat-without-LLM, in one line

The manager persists each completed run as a **workflow recipe** (output dir,
parallelism, part-naming, account hint). A scheduled or button-triggered replay
re-opens Takeout in the existing logged-in browser, triggers a fresh export +
capture via the extension's stored automation, and feeds the new payload to the
engine — all driven by the manager, no model in the loop. The Pi agent is only
needed the *first* time, to learn/debug the recipe.

## Failure isolation

- Engine crash → manager marks job `error`, keeps partials, alerts; restart
  resumes from disk.
- Manager crash → systemd/compose restart; job state persisted to
  `<outdir>/.manager_state.json`; engine partials intact.
- Browser crash → webtop restarts Chromium; persistent profile means still
  logged in; manager job waits in `needs_cookie` until a fresh capture arrives.
- Tunnel down → downloads continue (they don't depend on the tunnel); you just
  can't *view* the portal until it's back. Telegram still reports.
