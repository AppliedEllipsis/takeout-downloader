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
