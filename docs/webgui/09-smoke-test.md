# 09 — Smoke Test & Deploy Runbook

The end-to-end checklist to deploy the web-hosted browser + manager on the
server and prove it works. Follow top to bottom. Each step has a command and an
expected result. This closes the Phase 10 gate: *a fresh operator can deploy and
run a full workflow using only `docs/webgui/`.*

Server: `ellipsis@188.245.169.166`. Repo clone:
`/opt/storage.local_1/projects/takeout-downloader`.

---

## 0. Prerequisites

```bash
ssh ellipsis@188.245.169.166
cd /opt/storage.local_1/projects/takeout-downloader
git pull
docker --version          # Docker present
docker compose version    # compose v2 present
ls /opt                   # storage root exists (output lands under here)
```
Expected: Docker + compose available, `/opt` visible with the JuiceFS submounts.

---

## 1. Secrets (.env, mode 600)

```bash
cp webgui/.env.example .env
chmod 600 .env
# Generate tokens:
echo "MANAGER_API_TOKEN=$(openssl rand -hex 32)"      >> .env
echo "MANAGER_CAPTURE_TOKEN=$(openssl rand -hex 32)"  >> .env
nano .env   # paste TELEGRAM_TOKEN (= telegram.token from ~/.pi/agent/auth.json)
```
Expected: `.env` exists, mode `-rw-------`, both manager tokens set, telegram
token pasted. **`.env` is gitignored — never commit it** (verify: `git status`
shows no `.env`).

---

## 2. Build + start the stack

```bash
docker compose -f docker-compose.webgui.yml build webgui
docker compose -f docker-compose.webgui.yml up -d
docker compose -f docker-compose.webgui.yml ps
```
Expected: `takeout-webgui` and `takeout-tunnel` both `running`.

---

## 3. Manager health (over SSH forward)

From your laptop:
```bash
ssh -L 8080:127.0.0.1:8080 -L 9222:127.0.0.1:9222 -N ellipsis@188.245.169.166 &
curl -s 127.0.0.1:8080/api/control/health | python -m json.tool
```
Expected: `{"ok": true, ..., "capture_token_set": true, "api_token_set": true,
"disk": {"free": <big>, ...}}`. If `*_token_set` is false, the `.env` didn't
load — recheck step 1.

---

## 4. Negative test — control plane is NOT public

```bash
# CDP reachable locally (over the SSH forward) ...
curl -s 127.0.0.1:9222/json/version | python -m json.tool   # Chrome version
# ... but NOT through the tunnel hostname:
bash webgui/cloudflared/verify-exposure.sh takeout.<your-domain>
```
Expected: `9222/json/version` returns a Chrome version over the SSH forward; the
exposure script reports **8080 and 9222 are NOT reachable** via the public
hostname, only the portal (which itself sits behind Cloudflare Access).

---

## 5. Portal + one-time Google login

1. Open `https://takeout.<your-domain>` in your normal browser.
2. Pass the **Cloudflare Access** prompt (your email/SSO).
3. Pass the **KasmVNC** password prompt.
4. In the hosted Chromium, log into Google **once**. The profile persists in
   `/config`, so this survives restarts.

Expected: two gates before the desktop; Chromium opens with the Takeout tab and
the manager tab (`http://127.0.0.1:8080`), bookmarks present, extension pinned.

---

## 6. One-part download (the real end-to-end gate)

In the hosted Chromium:
1. Go to `takeout.google.com` → Manage exports → click **Download** on one part.
2. The v4 extension auto-POSTs the capture to the manager (toast: "Sent to
   manager, job ...").
3. Watch the manager tab: a job appears, parts table fills, bars move.

Or force a tiny run from the API (over the SSH forward), using a captured
payload saved to `payload.json`:
```bash
curl -s -X POST 127.0.0.1:8080/api/payload \
  -H "X-Capture-Token: $(grep MANAGER_CAPTURE_TOKEN .env | cut -d= -f2)" \
  --data-binary @payload.json
```
Expected: job → `downloading` → `complete`. Output under
`/opt/google-takeout/<account>/<export-ts>/` with valid `.zip` files and a
`manifest.json` listing each file's size + timestamps.

```bash
ls /opt/google-takeout/*/*/                 # dated folder + zips + manifest.json
head -c4 /opt/google-takeout/*/*/*.zip | xxd | head   # PK.. magic, not HTML
```

---

## 7. Cookie expiry → auto-recapture (the heartbeat)

Let a multi-part run continue past ~45 min, or force it: clear the Google session
in another tab so the next part 302s to sign-in.

Expected: job flips to `needs_cookie`; Telegram fires `🔑 ... cookie expired.
Auto-recapturing…`; the extension re-captures from the still-logged-in profile
and POSTs a fresh cookie; the job resumes from partials. If Google forced a real
logout, you instead get `🚨 manual login needed` — log in via the portal.

---

## 8. Telegram

```bash
# one-time chat id capture (if not already in .env):
docker compose -f docker-compose.webgui.yml exec webgui \
  /opt/manager-venv/bin/python -m manager.notify --capture-chat-id --token "$TELEGRAM_TOKEN"
```
Then from your Telegram chat: `/status`, `/health`, `/diagnose`.
Expected: the bot replies with the current job state; `/recapture` asks for
`/yes` confirmation before acting.

---

## 9. Repeat-without-LLM

```bash
# list recorded recipes (a completed run auto-records one):
curl -s 127.0.0.1:8080/api/recipes | python -m json.tool
# replay one with no model in the loop:
curl -s -X POST 127.0.0.1:8080/api/recipes/<account>/run \
  -H "X-Api-Token: $(grep MANAGER_API_TOKEN .env | cut -d= -f2)"
```
Expected: the manager drives the hosted Chromium over CDP to open Takeout and
trigger a fresh export+capture, then downloads it — no LLM. (Telegram `/run
<name>` does the same with a `/yes` confirm.)

---

## Reason-code runbook (when something is stuck)

`GET /api/control/diagnose?job_id=...` returns one reason code. Each maps to a
section in [`04-decision-trees.md`](./04-decision-trees.md):

| Reason | Section | Auto-recovers | Needs you |
|---|---|---|---|
| `cookie_expired` | §A | yes (auto-recapture) | only if it becomes `auth_loop` |
| `auth_loop` | §B | no | yes — log in via portal |
| `disk_full` | §C | no | yes — free space |
| `network_stall` | §D | usually (retries) | only if persistent |
| `zip_validation_failed` | §E | often (resume) | only if a part is truly corrupt |
| `browser_down` | §F | no | restart the webtop container |
| `manager_down` | §G | no | restart the service |

---

## Update workflow

```bash
cd /opt/storage.local_1/projects/takeout-downloader
git pull
# engine + manager + extension are bind-mounted at /work, so:
docker compose -f docker-compose.webgui.yml restart webgui
# rebuild only when Dockerfile / requirements change:
docker compose -f docker-compose.webgui.yml build webgui
```
