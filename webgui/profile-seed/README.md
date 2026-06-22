# Profile seed — first-boot browser configuration

`init_custom.sh` copies these into `/config/.chrome-profile/` on first boot
**only if that profile does not already exist**. After first boot the profile
is persistent (your Google login, any manual bookmark edits, extension state
all survive restarts), and reseeding never happens unless `/config` is wiped.

## Files

| File | Seeds | Chromium target |
|---|---|---|
| `Bookmarks` | Bookmark bar: Takeout home, Manage exports, Manager UI, Manager recipes | `Default/Bookmarks` |
| `managed-policy.json` | Extension settings (`managerUrl`, `captureToken`, `autoPost`, `autoRecapture`) via managed storage | `/etc/.../policies/managed/` (see init script) |

## Why managed storage for the token

The capture token must reach the extension without you pasting it into the popup
by hand on every fresh profile. Chromium reads a managed-storage policy file at
startup and exposes it to the extension via `chrome.storage.managed`. The
background script reads `chrome.storage.managed` once and copies the values into
`chrome.storage.local` (its working settings). `init_custom.sh` renders
`managed-policy.json` from the `MANAGER_CAPTURE_TOKEN` env at boot, so the token
lives only in `.env` + the container, never in git.

## Bookmarks note

Chromium reads the `Bookmarks` JSON only when no existing bookmarks DB is
present. The seed provides the bookmark bar; you can rearrange freely afterward
and your changes persist in `/config`.
