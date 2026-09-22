# Credentials — pointers only

**Never put actual secrets in this file.** Pointers, variable names, and locations only.

## Remote server

| What | Where |
|---|---|
| SSH access | alias `takeout-server` in the workstation's `~/.ssh/config` (key-based) |
| Host | `ubuntu-8gb-fsn1-1` |
| Container venv (manager) | `/opt/manager-venv` inside `takeout-webgui` |
| Container venv (browser) | `/opt/browser-venv` inside `takeout-webgui` |

Always connect with `-o BatchMode=yes` so a missing key fails fast rather than
hanging on an interactive prompt.

## Google / Takeout

| What | Where |
|---|---|
| Google account auth | Browser session inside `takeout-webgui`'s Chromium, profile at `/config/.chrome-profile` |
| Cookie source | CDP over `127.0.0.1:9222` **inside** the container (`Storage.getCookies`) — Linux only, never exposed to the host |
| Accounts in use | Multiple; the Manage-exports page exposes them via the account switcher |

> **Design note (v3):** the cookie is *not* extracted or replayed any more. The browser
> performs downloads itself. Any remaining credential handling must stay inside the
> container.

## Application-level (env)

Check `.env` / `.env.example` in the repo for the authoritative list of variable
**names** (do not copy values here). Known categories: Telegram notifier bot token &
chat id, manager API auth, archive paths.

## Ports / dashboards

| What | Where |
|---|---|
| Webgui UI | `http://<host>:8080/` (and the container-local `127.0.0.1:8080`) |
| Chromium DevTools | `127.0.0.1:9222` inside `takeout-webgui` only |
| External reachability | via the `takeout-tunnel` container |

## Browser extension

| What | Value |
|---|---|
| Extension id | `dgbbpdjpfeeaiheekoclkkkbipkikejl` |
| Key file | `webgui/extension-key` (in repo — check whether it is a private key before publishing) |

## Temporary credential (ReAuth)

| What | Where |
|---|---|
| Temp password for the Google ReAuth "Password challenge" | `<repo>/config/gPass.txt` — **gitignored** (`.gitignore:126`), so it never reaches the public repo. Read at run time; never in argv, never in env, never echoed |
| Override path | env `TAKEOUT_GPASS` |
| Handler | `tools/satisfy_login.py` — `--dry-run` reports form geometry and types nothing |
| Extension switches | `autoCancelDownloads=false`, `autoRecapture=false`; flip with `tools/extension_switch.py` |

> **The file is the secret. Never echo it.** The handler prints only the source path and the
> character count. Use `--dry-run` to confirm the form is findable before typing anything.
> If Google demands a second factor, `gPass.txt` cannot satisfy it — a human is required.
> Set the file mode to `0600` (`chmod 600 config/gPass.txt`; the host dir is owned by the
> container's uid 1000, so chmod it **inside** the container).
