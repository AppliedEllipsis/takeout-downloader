# Changelog

All notable changes to this project are documented here.

## [6.8.4] — Single-stream by default (fixes the "1 active | 0 B/s" hang)

### Fixed

- **Parallel downloads stalled at 0 B/s.** Google serves Takeout archives
  single-stream per session: opening N parallel connections leaves N-1
  stalled at zero bytes until their read timeout fires (the
  `1 active | 0 B/s` hang you'd see after "Found N parts"). The internal
  engine now defaults to **1 concurrent stream**, so the one connection
  gets Google's full bandwidth instead of competing with stalled siblings.
  Pass `-p N` to override if you really want parallel. aria2c keeps its
  previous default.

- **Stalls now recover in ~30s instead of ~60s.** The read timeout dropped
  from 60s to 30s (override with `TAKEOUT_READ_TIMEOUT`). When a stream
  sends no data within that window the worker reconnects with Range from
  where it stopped, so a transient stall costs half a minute, not a full
  one.

- **Empty `PARALLEL_DOWNLOADS` no longer crashes startup.** An empty-string
  env value (now the compose default) was `int()`-parsed and raised
  `ValueError` at import. Both `takeout.py` and `takeout_cli.py` now treat
  blank/non-numeric values as unset.

### Added

- `TAKEOUT_READ_TIMEOUT` / `TAKEOUT_CONNECT_TIMEOUT` env vars to tune the
  socket timeouts.
- `_resolve_parallel()` helper centralises the precedence (explicit `-p` >
  engine default), covered by unit tests.

## [6.8.3] — Download heartbeat in the log file + per-part start/done logging

### Fixed

- **No feedback in the log file during downloads.** In grid mode the
  progress callback only painted the terminal, so `tail -f takeout_cli.log`
  from a second SSH session showed dead silence whether the download was
  healthy or stalled. `run_internal` now emits a throttled one-line
  progress heartbeat to the log (every ~5s) on every run, grid or not.

### Added

- **Per-part start/done/retry logging** in the internal downloader. Each
  worker now logs `part NNN start`, `part NNN done`, and (on a transient
  error) `part NNN attempt X/Y error, will retry` at INFO. This turns the
  log into a real activity trace so a stalled connection is distinguishable
  from a slow one -- previously the downloader only logged retries at DEBUG,
  which was invisible in the default log.

## [6.8.2] — Skip the blocking pre-flight for the internal engine

### Changed

- **Pre-flight full-GET is now skipped by default for the internal
  downloader.** It only ever mattered for aria2c, which would blindly
  write a 1.2 MB HTML sign-in page to disk if the session were challenged
  mid-download. The internal engine already inspects the first chunk of
  every part and raises an auth challenge *before* any bytes touch the
  archive file, so the pre-flight added nothing but a blocking,
  feedback-less request before downloads began (the "it hangs on
  pre-flight" symptom). The internal engine now goes straight to
  downloading parts in order.
- Pre-flight still runs automatically for `--engine aria2c`. Override
  either way with `TAKEOUT_PREFLIGHT=1` (force on) or `TAKEOUT_PREFLIGHT=0`
  (force off) regardless of engine.

## [6.8.1] — Subfolder picker roots at the shared base, not a saved subfolder

### Fixed

- **The picker was rooted at the wrong directory.** `OUTPUT_DIR` (from
  `.env`) usually points at a *subfolder* the user previously downloaded
  into (e.g. `.../google-takeout/braincreation`), not the shared base. The
  picker rooted itself there, so the user could never select a sibling
  folder or create a new one next to it — and `resolve_output_dir()` kept
  defaulting to that same subfolder every run no matter what was picked.
  New `_derive_picker_base()` walks the resolved path upward to the
  `google-takeout` component (configurable via `TAKEOUT_DIR_NAME`, or
  overridden outright with `TAKEOUT_BASE_DIR`) and roots the arrow-key
  picker there. So with `OUTPUT_DIR=/opt/storage.jfs002/google-takeout/foo`
  the picker now lists every subfolder under `.../google-takeout/` and lets
  you pick or create one.

## [6.8.0] — Containers run live source from /work (git pull, no rebuild)

### Changed

- **Both compose services now run the live source bind-mounted at `/work`**
  instead of the copy baked into the image at `/app`. The repo is already
  mounted (`.:/work`); the entrypoints now point there
  (`working_dir: /work`, `entrypoint: ["python", "-u", "/work/takeout_cli.py"]`).

  Why: the image bakes the code in at build time, so `docker compose run`
  kept executing stale code until someone remembered to
  `docker compose build`. Repeated field reports showed the server running
  old code (old output-dir prompt, pre-flood-fix grid) because the rebuild
  step was missed. Running from `/work` means a host-side
  `git pull` (or `git reset --hard origin/main`) updates the running code
  immediately — **no rebuild needed for a code change**. Python dependencies
  are still installed system-wide in the image, so only a dependency change
  requires `docker compose build`.

- The standalone `Dockerfile` is unchanged and still works for plain
  `docker run`; compose simply overrides its entrypoint (as it already did).

## [6.7.2] — Single-scandir part snapshot (no more multi-minute pre-download stall)

### Fixed

- **Multi-minute stall before download started on slow filesystems.** Building
  the parts list (`_build_parts_from_payloads`) and `verify_parts` did an
  `exists()` + `stat()` per part. On a network filesystem (JuiceFS/encfs)
  that is two serial round-trips per part, so a 290-part export cost ~580
  serial syscalls and several minutes of dead time *before* the first byte
  was fetched. Both now snapshot the output directory with a single
  `os.scandir` pass (`_dir_size_map`) and look filenames up in memory.
  `verify_parts` still only opens files that actually exist on disk for the
  ZIP/EOCD check, so a fresh run (nothing downloaded yet) does effectively
  zero per-part I/O.

## [6.7.1] — Fix grid flood that stalled large (100+ part) downloads

### Fixed

- **Large downloads appeared to hang and made no progress** (internal
  engine). With many parts (e.g. 290), the progress callback called
  `_render_file_row` once per part on every tick — one full-screen
  repaint per part, ~2 MB/s of escape sequences over an SSH pipe. The
  flood drowned the terminal and the per-tick blocking `stdout` writes
  starved the download worker threads, so throughput collapsed to ~0 and
  the run looked frozen. The fall-back to `--parallel 1` then re-flooded
  and hung again.

  The internal engine now paints **one aggregate repaint per tick**: a
  summary header (`N/M done | K active | got/total | speed`) plus only
  the currently-active rows, via the new `TermRender.set_rows()`. Grid
  output dropped ~700x (megabytes/sec to a few KB/sec) and large part
  counts download normally. Verified with a 290-part repro.

- **Grid reserved one screen row per part** (clamped to terminal height,
  so parts beyond ~21 thrashed over the last slot). The internal engine
  now reserves only the concurrent-slot count (`--parallel`); aria2c's
  line parser still uses one row per file.

## [6.7.0] — Derived default output dir (no hardcoded paths)

### Changed

- **Default output directory is now derived at runtime** instead of being
  a hardcoded path. The app auto-detects an existing
  ``<root>/<*>/google-takeout`` (or ``<root>/google-takeout``) directory
  under common storage mount roots (`/opt`, `/mnt`, `/media`, `/srv`,
  `/data`), falling back to ``./downloads``. ``OUTPUT_DIR`` (explicit) and
  ``TAKEOUT_BASE_DIR`` (picker base) still win when set. New overrides:
  ``TAKEOUT_DIR_NAME`` (folder name to look for, default ``google-takeout``)
  and ``TAKEOUT_SEARCH_ROOTS`` (os.pathsep-separated mount roots). This
  keeps any environment-specific path out of the source tree while still
  giving a server with a mounted storage volume a useful default with no
  config.

## [6.6.0] — Docker bundles the internal downloader; arrow-key subfolder picker

### Fixed

- **`ModuleNotFoundError: takeout_downloader` in Docker**. The 6.5.0
  internal downloader added `takeout_downloader.py`, but the Dockerfile
  `COPY`s an explicit file list and the new module wasn't on it — so
  `docker compose run --rm takeout-cli` crashed on import. Added
  `takeout_downloader.py` to the COPY list.

### Added

- **Arrow-key subfolder picker**. When the output base is a real
  directory (e.g. the server's `/srv/storage/google-takeout`),
  the CLI now shows an interactive menu of its existing subfolders plus
  "create a new subfolder", "use this folder directly", and "type a
  different path". On a POSIX TTY you navigate with ↑/↓ (or j/k), Enter
  to select, q/Esc to cancel; the menu redraws in place. Non-TTY /
  non-POSIX (Windows, pipes, Docker without `-t`) degrades to a numbered
  menu. Cancelling falls back to the existing free-form path prompt, so
  arbitrary locations are still reachable. The free-form
  `prompt_for_output_dir` (and its JSON-paste / long-path guards) is
  unchanged underneath.

## [6.5.0] — Internal parallel downloader (default engine), exact TTY-independent progress

### Changed

- **New default download engine: an in-process parallel HTTP downloader**
  (`takeout_downloader.py`), replacing the aria2c subprocess. aria2c
  worked, but its progress feedback required scraping another process's
  human-readable stdout — fragile, and silently disabled whenever stdout
  wasn't a TTY (the common SSH/tmux/Docker case), which is why downloads
  "worked but showed no feedback". The internal engine owns the byte loop,
  so:

  - Progress is **exact** (we count the bytes) and **TTY-independent** —
    the live grid is fed from in-process counters, and even on a
    non-TTY/piped run a throttled one-line status keeps showing movement.
    No more dead-silent downloads.
  - Auth-challenge HTML is detected on the **first chunk**, before a single
    byte hits the archive file — so a sign-in page can never corrupt an
    output file (the old "every .zip is 1.2 MB of HTML" failure is now
    structurally impossible).
  - Resume is plain HTTP `Range:` from the on-disk size — no control files,
    no second process.
  - No `aria2c` binary dependency for the default path.

- **`--engine` flag / `TAKEOUT_ENGINE` env var**: choose `internal`
  (default) or `aria2c` (legacy subprocess, still fully supported for
  anyone who prefers it — it just needs aria2c on PATH). The aria2c
  presence check now only runs when that engine is selected.

### Added

- `takeout_downloader.py`: `InternalDownloader` with a bounded thread pool,
  per-part `Range` resume, first-chunk HTML guard, smoothed speed (EMA),
  cooperative stop on Ctrl-C, and a decoupled `on_progress` snapshot
  callback so the renderer is fully testable without a TTY.
- `tests/test_internal_downloader.py`: 6 tests against a local HTTP server
  covering parallel download, `Range` resume with content integrity,
  first-chunk auth-challenge detection (never written to disk), live
  partial-progress snapshots, and the no-op path when everything is
  already `have`. 178 tests pass.

## [6.4.0] — Live grid that actually updates, resize-safe rendering, size-ordered parts, --no-color

### Fixed

- **Live grid never updated during download** (the big one). The aria2c
  output parser bound each download's GID to its filename *only* from
  aria2c's terminal `Download Results:` block — which prints after every
  download finishes. Verified against aria2c 1.35.0: the GID→filename
  binding actually arrives mid-download as a `FILE:` line interleaved
  with the `[#GID ...]` progress lines. The grid sat at "queued" for the
  whole transfer, then snapped straight to done. `_update_from_aria2_line`
  now binds from `FILE:` lines (remembering the most-recent progress GID)
  and flushes the buffered progress sample, so every row animates live.
  The `Download Results:` block is kept only as a final-status backstop.

- **Grid collided with scrolling log output**. `TermRender` positioned
  itself with absolute cursor coordinates (`\x1b[1;1H` — jump to screen
  row 1) on every redraw, while the logger kept scrolling normally, so
  the fixed grid and the scrolling log wrote over each other. Rewrote the
  renderer to use *relative* cursor movement (draw once, then move up N
  lines and overwrite in place). The grid now anchors to the surrounding
  log flow instead of fighting it — no collision, no flicker.

### Added

- **Terminal-resize handling**: a `SIGWINCH` handler (POSIX) repaints on
  resize, the visible row count is clamped to the terminal height, and
  every emitted line is hard-truncated to the terminal width so a
  narrowed window can never wrap a row (a wrapped row would desync the
  move-up redraw count and corrupt the grid). The per-row layout now
  protects the filename and an 8-char-minimum progress bar, dropping the
  size/speed/ETA columns first when space is tight.

- **Size-ordered parts (smallest → biggest)**: parts are now fed to
  aria2c smallest-first in both the v2 multi-payload path
  (`_build_parts_from_payloads`) and the probe-discovery path
  (`discover_parts`), so the quick parts finish first. Unknown sizes
  sort last. Part identity (`num` / `i=`) is preserved; only feed order
  changes.

- **`--no-color` flag**: disables ANSI colors per-invocation (the
  CLI-flag equivalent of `NO_COLOR=1`), for logs, pipes, and dumb
  terminals.

- **Clickable URLs (OSC 8 hyperlinks)**: the `--dry-run` part listing now
  emits OSC 8 terminal hyperlinks, so each part's filename and URL are a
  ctrl+click / ctrl+shift+click target in terminals that support them
  (Windows Terminal, iTerm2, kitty, WezTerm, GNOME Terminal / recent VTE).
  Terminals without OSC 8 just print the label, so it degrades cleanly.
  Set `NO_HYPERLINKS=1` (or `--no-color`, or pipe the output) to force
  plain text.

- New v2 multi-payload test fixture (`tests/fixtures/sample_multi_payload.json`)
  matching the exact shape the extension's "Copy ALL exports" button
  emits, plus tests covering live `FILE:`-line binding against real
  aria2c output, smallest-first ordering, parts-mode end-to-end, and the
  `--no-color` switch. 168 tests pass.

## [6.3.0] — Stream-abort pre-flight, adaptive parallelism, dry-run, cookie warnings

### Fixed

- **Stream-abort pre-flight** (was downloading entire smallest part, now
  only reads ~4 KB). The pre-flight still exercises the full-GET path
  (no Range header) but closes the connection after receiving enough
  bytes to detect HTML vs archive. This makes the pre-flight virtually
  free regardless of file size.

- **Adaptive parallelism**: If aria2c fails all parts and it looks like
  an auth failure, the script now automatically retries with `--parallel 1`
  (sequential) before asking for a new cookie. This fixes the common case
  where 5 parallel connections trigger Google's anti-bot challenge but a
  single connection works fine. Previously the script would loop asking
  for a fresh cookie that has the same problem.

- **Cookie age warning now shown**: The payload validation already
  computed cookie staleness (>30 min) but the warning was silently
  ignored. It's now printed at startup so the user knows their cookie
  may expire soon.

- **Threshold lowered from 60 to 30 minutes**: Parallel downloads
  can trigger challenges sooner, so we warn earlier.

### Added

- **`--dry-run` flag**: Validates the cookie, discovers parts, prints
  the download plan (filenames, sizes, URLs), and exits without
  running aria2c. Useful for verifying everything looks right before
  committing to a long download.

- 2 new tests: `_drain_response` reads up to max-bytes and closes
  response; adaptive parallelism reduces `args.parallel` from 5 to 1.

## [6.2.0] — Pre-flight full-GET check stops the 1.2 MB HTML loop

### Fixed

- **Pre-flight full-GET check stops the auth-challenge loop.**
  Before this fix, Range probes (`bytes=0-0`) passed cleanly but full
  GETs returned 1.2 MB of Google sign-in HTML — the script would then
  loop asking for a fresh cookie that had the same problem. The new
  `_preflight_full_download()` function does a single full GET of the
  smallest part before committing to aria2c; if the response is HTML,
  it saves the body to `auth_challenge_<timestamp>.html` and exits
  with a step-by-step re-capture recipe instead of looping.

- **Unified HTML detection (`_looks_like_html_bytes`).** The
  pre-flight and `verify_parts()` now share the same byte-level HTML
  detector, which also catches bare `<html>` responses (no doctype)
  that some Google endpoints return.

- **`_save_auth_challenge()` helper** saves the full HTML body with
  the probed URL and HTTP status as inline comments so the user can
  inspect what Google actually returned.

### Added

- 6 new tests: real-ZIP pass, HTML body, accounts.google.com redirect,
  wrong content-type lying header, short response warning, and
  byte-buffer edge cases.

- **`--version` flag** and startup banner now read from a single
  `VERSION` constant (imported from `takeout.py`).

## [Unreleased]

### Added

- **Schema v2 multi-payload with auto-detected part count.** The
  browser extension now scrapes the visible `[data-download-uri]`
  buttons on the Takeout manage page (filtered by `j=` so URLs from
  other Takeouts on the same account never leak in) and emits a v2
  multi-payload carrying `archiveId`, `expectedParts`, and per-part
  `partIndex` + `size`. The CLI uses these to skip its "How many parts
  are in this export?" prompt entirely — the page already told us N,
  so we just download them all. Schema v1 multi-payloads (no
  metadata) are rejected with a clear "re-capture" message; schema v1
  single-export captures still work everywhere.
- **`[a] Download ALL` now actually iterates.** Previously the
  multi-export menu's `[a]` option just downloaded the smallest
  archive and told the user to re-run for the rest. Now it loops
  through every archive in smallest-first order, asking for a fresh
  cookie when one expires and resuming partials on the next pass.

- **Ephemeral paste relay (`--relay`).** "ngrok but zero-config" for
  getting the JSON payload into the CLI over SSH → tmux → Docker, where
  terminal paste is unreliable (bracketed-paste markers get stripped,
  long lines wrap). The CLI starts a tiny single-use HTTP server, prints
  a URL with a 192-bit random token in the path, and blocks until you
  open it in the browser that has the extension and paste the payload
  into a textarea. Hardened three ways because the payload holds a live
  Google session cookie: (1) unguessable random token, (2) single-use
  — the first valid POST shuts the server down, (3) short TTL
  self-destruct (default 600s, `--relay-timeout`). Binds to 127.0.0.1 by
  default; never logs the cookie value; constant-time token compare.
  Add `--tunnel` to expose it publicly via a Cloudflare quick tunnel
  (no account, ephemeral `*.trycloudflare.com` URL, dials outbound so no
  port mapping needed even in Docker). Stdlib-only — lives in
  `paste_server.py`, also runnable standalone
  (`python paste_server.py --tunnel --print`).
- **Live grid UI in the CLI.** When stdout is a TTY, the CLI renders a
  fixed-position grid of every part — progress bar + bytes + speed +
  ETA + filename — using ANSI escape codes. Re-drawn in place on each
  1s aria2c summary tick. No flicker (save/restore cursor around the
  redraw). Falls back to plain line-printing when stdout is piped.
  Disable with `NO_GRID=1`.
- **Interactive output-directory prompt.** After parsing the JSON, the
  CLI asks where to save (default = `OUTPUT_DIR` env). Validates the
  path against the allowlist, creates it if missing, re-aims the log
  file at the chosen folder. Skip the prompt with `--output-dir`.
- **Per-folder resume state.** `takeout_state.json` is written in the
  output directory after every verify pass. Stores URL pattern, full
  part list with sizes, and per-part completion. On re-run, the CLI
  loads state, re-verifies files on disk (a stale state with truncated
  files won't lie), and resumes only the missing parts. State survives
  Ctrl-C, Docker `--rm`, and reboots.
- **98 tests passing** (up from 70). New coverage: TermRender grid,
  aria2c output parser, prompt-for-output-dir, state save/load roundtrip,
  resume logic, mocked end-to-end grid render.

### Fixed

- **CLI service failed at startup with `can't open file
  '/app/takeout_cli.py'`.** The Dockerfile was written when the project
  was TUI-only; `takeout_cli.py` and `takeout_cli_analyze.py` were not in
  the `COPY` list. Added both. Also relaxed `ENTRYPOINT` from
  `python takeout.py` to plain `python` so per-service `command:` cleanly
  selects the script.

### Changed

- **TUI is opt-in via Docker profile.** The TUI service (`takeout`) now
  declares `profiles: ["tui"]` so it is hidden from
  `docker compose config --services` by default. The CLI (`takeout-cli`)
  becomes the default and only visible entrypoint — matches the preferred
  flow for SSH→tmux→Docker. Launch the TUI explicitly with
  `docker compose --profile tui run --rm takeout` when you want a UI on a
  local terminal.

### Fixed

- **Accidental JSON paste into the output-dir prompt was silently
  accepted as a folder name.** The path prompt now sniffs input that
  starts with `{` or `[` and rejects it with a clear "wrong prompt"
  hint, instead of letting `Path(...)` create a folder literally named
  `{...}` or crashing on a multi-KB blob. Long-path / invalid-char
  errors from `mkdir` are now caught and re-prompted instead of
  crashing with an unhandled `OSError`. Ctrl-C at this prompt exits
  cleanly with code 130 and no misleading "resuming partial downloads"
  message (there are no partials yet).
- **TUI froze after browsing into a large/slow directory** (had to
  `docker kill`). Textual's `DirectoryTree` stats every entry and recurses
  on the UI thread, which locks the whole app on a big JuiceFS/encfs FUSE
  mount. Replaced it with a manual, single-level `os.scandir` listing that
  runs off the UI thread and is capped at 1000 entries per directory, so
  the picker can no longer hang.
- **Paste did not work over SSH → tmux → Docker.** The chain strips the
  bracketed-paste markers (`ESC[200~ … ESC[201~`), so Textual never
  receives a paste event — the same class of bug as Claude Code #30239.
  Added a **file-based input fallback**: type a single `.` in the payload
  box (or `@filename`) and the TUI reads the payload from `in.json` /
  `payload.json` / `curl.txt` in the output dir or cwd. Bypasses the
  terminal clipboard path entirely. The payload box is also focused on
  launch and an app-level paste router still catches bracketed pastes when
  they do arrive.

### Changed

- **Default parallel downloads 1 → 10** (`PARALLEL_DOWNLOADS` still
  overrides).
- **Default output dir** now prefers `/srv/storage/google-takeout`
  when it exists, else `./downloads` (`OUTPUT_DIR` still overrides).

### Changed

- **Default parallel downloads raised 1 → 10** (`PARALLEL_DOWNLOADS` env
  still overrides).
- **Default output directory** now prefers `/srv/storage/google-takeout`
  when it exists, falling back to `./downloads`. `OUTPUT_DIR` env still
  takes priority over both.

### Added

- **Docker `/opt` mount** — `docker-compose.yml` recursively binds host
  `/opt` with `rslave` propagation so JuiceFS / encfs FUSE submounts
  (e.g. `<storage-mount>`) are visible inside the container instead of
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
