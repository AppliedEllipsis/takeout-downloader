# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Fixed

- **TUI froze after browsing into a large/slow directory** (had to
  `docker kill`). Textual's `DirectoryTree` stats every entry and recurses
  on the UI thread, which locks the whole app on a big JuiceFS/encfs FUSE
  mount. Replaced it with a manual, single-level `os.scandir` listing that
  runs off the UI thread and is capped at 1000 entries per directory, so
  the picker can no longer hang.

### Changed

- **Default parallel downloads raised 1 → 10** (`PARALLEL_DOWNLOADS` env
  still overrides).
- **Default output directory** now prefers `/srv/storage/google-takeout`
  when it exists, falling back to `./downloads`. `OUTPUT_DIR` env still
  takes priority over both.

### Added

- **Docker `/opt` mount** — `docker-compose.yml` recursively binds host
  `/opt` with `rslave` propagation so JuiceFS / encfs FUSE submounts
  (e.g. `/srv/storage`) are visible inside the container instead of
  showing as empty dirs. Settings persist on the `/downloads` volume
  (`TAKEOUT_SETTINGS`) so they survive `docker compose run --rm`.
- **Directory browser** in the TUI — a Browse button (and `b` key) opens a
  modal `DirectoryPicker`: navigate the tree, type/paste a path, or go Up.
  Symlinks resolve, so `./downloads/opt -> /opt` lands on the real target.
  Navigation is optimistic + threaded with a loading overlay, so slow
  JuiceFS / network mounts no longer freeze the UI or leave a stale header.
- **Settings persistence** — output dir, file count, and parallelism are
  saved on Start and restored on next launch. `ALLOWED_DIRS` env var lets
  you whitelist extra output roots (e.g. a deep JuiceFS path).
- **Paste routing** — the payload box is focused on launch, and an
  app-level paste router catches right-click / `Ctrl+Shift+V` pastes from
  anywhere (a common SSH+tmux problem where paste was swallowed by a
  focused button) and drops them into the payload box.
- **tmux-native refresh alert** — alongside the bell + title flash, the
  cookie-expired alert renames the terminal/tmux window and emits BEL
  through the raw PTY so tmux's `monitor-bell`/`monitor-activity` flags the
  window in the status line even from another pane. (The audible bell
  rarely survives Docker→tmux→SSH; the visual + tmux alerts are what
  actually reach you.)

### Fixed

- **Extension always reported "No cookie captured".** The MV3
  `webRequest.onBeforeSendHeaders` listener only requested
  `['requestHeaders']`, so Chrome (72+) stripped the `Cookie` header from
  what the service worker could see — the capture's `cookie` field came
  back empty even on valid requests (while tools like CurlWget, which read
  at a different layer, worked fine). Added `'extraHeaders'` to the
  `extraInfoSpec`, which is required to observe `Cookie` / `Referer` /
  `Authorization`. Extension bumped to 2.0.1.

### Added

- **Directory browser in the TUI.** A `📁 Browse` button (and `b` key)
  opens a modal filesystem picker — navigate with the tree, go `↑ Up`, or
  type/paste a path directly. Symlinks are resolved, so a host link such
  as `./downloads/opt -> /opt` lands on the real target. The output-dir
  field also still accepts a typed/pasted path directly.
- **Persistent settings.** The last-used output directory, file count, and
  parallel count are saved to `~/.takeout_downloader.json` (override with
  `TAKEOUT_SETTINGS`) and restored on the next launch.
- **`ALLOWED_DIRS` env var.** Extend the output-dir allowlist with extra
  roots (`os.pathsep`-separated), e.g. a JuiceFS path under `/opt`. Paths
  are resolved (symlinks followed) before the prefix check.
- **`Dockerfile` + `docker-compose.yml`** — the TUI is now self-contained
  in a container, with `aria2c` and all Python deps baked in. Because the
  TUI is interactive, run it with `docker compose run --rm takeout` (not
  `up -d`); the compose service sets `stdin_open` + `tty` so Textual gets
  a real terminal. Downloads and resume state persist in `./downloads` on
  the host. No server, no exposed port — the extension → clipboard → paste
  flow is unchanged.
  - Note: these files had been removed in v6.0 (they only served the old
    web UI); they are reintroduced here purpose-built for the TUI.
  - **Mounts host `/opt`** with `bind.recursive` + `rslave` propagation so
    the JuiceFS / gocryptfs FUSE submounts under `/opt` (e.g.
    `/srv/storage`) are visible inside the container instead of
    showing as empty dirs. Settings persist on the mounted volume via
    `TAKEOUT_SETTINGS=/downloads/.takeout_settings.json`.

## [6.1.0] — Retry hardening from cross-project research

Research pass over 9 related Takeout tools (see
[`docs/RELATED_PROJECTS.md`](./docs/RELATED_PROJECTS.md)); the strongest
lessons came from `tarballz/mass-takeout-downloader`.

### Changed

- **Backoff now uses full jitter, capped.** `compute_backoff()` replaces
  the lockstep `2 ** attempt` sleep at every retry site in both the
  engine (`takeout.py`) and the TUI download path. Parallel workers no
  longer retry in the same instant (thundering-herd fix). Capped at
  `RETRY_MAX_WAIT` (default 120 s).
- **`MAX_RETRIES` default raised 3 → 6** — field reports show large
  exports throw transient 5xx/network errors mid-run that usually recover.

### Added

- Explicit **429 / 503 rate-limit handling** that honours the
  `Retry-After` header (`_retry_after_seconds()`) before falling back to
  jittered backoff; returns a `RATE_LIMITED` terminal error once retries
  are exhausted.
- `RETRY_MAX_WAIT` env knob (default `120.0`).
- `docs/RELATED_PROJECTS.md` — survey of 9 related projects and what was
  borrowed or deliberately rejected from each.
- `USAGE.md`: signed-URL-expiry-vs-cookie-expiry troubleshooting, and a
  "processing photo archives" section pointing at `gpth` /
  google-photos-exif / google_takeout_parser.

### Removed

- Dead `generate_secret_key()` and the now-unused `secrets` import
  (leftover from the deleted Flask web UI).

---

## [6.0.0] — Extension-only capture flow

### Changed (breaking)

- **Removed the Web UI entirely.** The project is now TUI + browser
  extension only. `google_takeout_web.py` (Flask + Flask-SocketIO) is
  deleted along with its `Dockerfile` and `docker-compose.yml`.
- **The browser extension is now capture-only.** It no longer POSTs
  captures to any server. Instead it produces a self-contained JSON
  payload that you copy to the clipboard and paste into the TUI. The
  user is the transport layer — no auto-send, no localhost server, no
  port, no CORS, no auth handshake.
- **New payload schema** (`takeout_payload.py`, schema v1) is the single
  contract between the extension and the TUI. JSON carries the URL,
  cookie, and the headers Google validates (`User-Agent`, `Accept`,
  `Referer`, …).

### Added

- `takeout_payload.py` — schema, parser, and validator. Auto-detects
  JSON vs cURL (`parse_payload`). Validates cookie session markers and
  warns on captures older than 60 minutes.
- TUI **refresh alert**: when the cookie expires mid-download, the TUI
  rings the terminal bell and flashes its title bar every 5 seconds
  until you paste a fresh capture and click **Resume**. Cumulative
  download stats are preserved across the refresh.
- TUI now accepts a **JSON payload** (extension → Copy as JSON) or a
  cURL command in the same box, auto-detected.
- Extension popup: **Copy as JSON** / **Copy as cURL** buttons, JSON
  preview, capture age/cookie-length display, pre-redirect capture
  warning, and an auto-copy toggle.
- Extension now captures from the real download host
  `takeout-download.usercontent.google.com` (not just `takeout.google.com`),
  fixing the redirect-chain cookie problem.
- `tests/test_takeout_payload.py` — 26 tests covering round-trips,
  schema rejection, cookie-marker and age validation, cURL parsing, and
  auto-detect.
- Documentation: `docs/ARCHITECTURE.md`, `docs/EXTENSION.md`,
  `docs/BEST_PRACTICES.md`, `docs/RELATED_PROJECTS.md`, this changelog.

### Removed

- `google_takeout_web.py`, `Dockerfile`, `docker-compose.yml`.
- `helpers/takeout-extractor.user.js` (Tampermonkey userscript).
- `helpers/bookmarklet.html` (bookmarklet).
- Flask / Flask-SocketIO dependencies.
- Web-specific config: `AUTH_USER`, `AUTH_PASS`, `SECRET_KEY`,
  `CORS_ORIGINS`, server host/port.

### Migration

If you previously ran the Web UI:

1. Pull v6.0 and reinstall deps: `pip install -r requirements.txt`
   (Flask is gone; Textual remains).
2. Load `helpers/` as an unpacked extension — see
   [`docs/EXTENSION.md`](./EXTENSION.md).
3. Run `python takeout.py` (no `--web` flag anymore).
4. Capture in the browser → **Copy as JSON** → paste into the TUI.

Downloaded files, `.downloading` resume state, and `.takeout_sizes.json`
are unaffected — resume works across the upgrade.

---

## [5.0.0] — Security hardening (pre-v6 baseline)

- HTTP Basic Auth, CORS allowlist, CSP headers, path-traversal
  protection, rate limiting on the (now-removed) web UI.
- HTTP Range resume with `.downloading` temp files.
- Exponential-backoff retries (`MAX_RETRIES`, `RETRY_BACKOFF`).
- ZIP end-of-central-directory integrity verification.
- aria2c JSON-RPC backend (`aria2c_integration.py`).
- PowerShell cURL parsing support.
- Post-download hash dedupe helper (`dedupe_takeout.py`).

See [`NOTICE.md`](../NOTICE.md) for the full fork history and safety
audit.
