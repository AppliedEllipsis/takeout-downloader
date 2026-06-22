# 06 — Telegram Integration

The manager uses a Telegram bot as a second notification + control channel,
alongside the in-browser progress UI. It reports progress, errors, and
login-needed events, and answers status commands.

Decision (from planning): reuse the existing bot token from
`~/.pi/agent/auth.json` (`telegram.token`). That file currently has
`telegram.type` and `telegram.token` but **no `chat_id`** — capturing the chat
id is the one setup step (below).

## Config keys

Source the token from pi auth, the chat id from a one-time capture. The manager
reads them from its own `.env` (copied from pi auth at deploy time — see
`05-deployment.md`), not directly from `~/.pi/agent/auth.json`, so the container
doesn't need to mount your pi home.

```
TELEGRAM_TOKEN      # = telegram.token from ~/.pi/agent/auth.json
TELEGRAM_CHAT_ID    # the channel/chat the bot posts to (captured once)
TELEGRAM_ENABLED    # default true; set false to silence without removing config
```

### Capturing the chat id once

```
1. Create a channel (or use a private chat). Add the bot as an admin.
2. Send any message in it.
3. GET https://api.telegram.org/bot<TOKEN>/getUpdates
4. Read result[].channel_post.chat.id  (or message.chat.id for a DM).
5. Put it in the server .env as TELEGRAM_CHAT_ID.
```

The manager has a helper for this: `python -m manager.notify --capture-chat-id`
prints the id from the latest update so you don't hand-parse JSON.

## What the bot sends (events)

| Event | Trigger | Message shape |
|---|---|---|
| Started | job → downloading | `▶️ <workflow>: started, <N> parts, <size> total` |
| Milestone | every 10% or N parts | `⏳ <workflow>: 41/290 parts, 442 GB / 3.1 TB, 88 MB/s` |
| Needs cookie | job → needs_cookie | `🔑 <workflow>: cookie expired. Auto-recapturing…` |
| Login needed | recapture failed | `🚨 <workflow>: manual login needed. Open the portal: <url>` |
| Error | job → error | `❌ <workflow>: <reason_code> — <short msg>. Partials kept.` |
| Complete | job → complete | `✅ <workflow>: done. 290/290 parts, 3.1 TB in 4h12m.` |

Milestone cadence is rate-limited (no spam): at most one progress message per
`TELEGRAM_PROGRESS_INTERVAL` (default 5 min) plus the state-change events, which
always fire.

The `reason_code` in the error message is the exact same code from
`/api/control/diagnose` (see `04-decision-trees.md`), so what you see on Telegram
maps directly to a runbook entry.

## Status commands (you → bot)

The manager runs a long-poll loop (`getUpdates`) and handles commands. All
commands are scoped to `TELEGRAM_CHAT_ID` — messages from any other chat are
ignored (simple authz).

| Command | Action |
|---|---|
| `/status` | current job: status, parts done, bytes, speed, ETA |
| `/jobs` | list all jobs with one-line status each |
| `/health` | engine + browser(CDP) + disk free + cookie age (same as `/api/control/health`) |
| `/pause` | pause the active job |
| `/resume` | resume the active/paused job |
| `/recapture` | force a cookie re-capture now |
| `/recipes` | list saved workflow recipes |
| `/run <name>` | replay a recipe (repeat-without-LLM) |
| `/diagnose` | reason-code report for the active job |
| `/mute` / `/unmute` | toggle progress milestones (state changes still fire) |

Commands map 1:1 onto the manager control API, so Telegram is just a thin front
end over `/api/control/*`. Destructive commands (`/run`, `/recapture`) reply with
a confirm step the first time.

## Wiring in the manager

```
manager/notify.py
  - TelegramNotifier(token, chat_id, enabled)
      .send(text)                     # one message, rate-limit aware
      .send_event(kind, job)          # formats from the table above
  - poll_commands(manager)            # long-poll getUpdates, dispatch to control API
  - --capture-chat-id helper (CLI)
```

- `notify.py` has **no other dependencies** on the engine — it takes a job
  snapshot dict and formats it. Keeps it testable offline.
- The poll loop runs as a manager background task. If `TELEGRAM_ENABLED=false`
  or no token/chat id, the notifier is a no-op (manager still runs fully).
- Network failures to Telegram are logged and swallowed — Telegram being down
  must never stall a download.

## Safety notes

- The bot only acts on messages from `TELEGRAM_CHAT_ID`. Anyone who finds the bot
  username gets nothing.
- The token is account-equivalent for the bot; keep it in `.env` (mode 600),
  never in the image or git.
- Telegram is a **convenience** channel. The authoritative control plane is the
  localhost manager API over SSH; Telegram just fronts a safe subset.

## Build order

1. `TelegramNotifier.send` + the chat-id capture helper. Verify a hello message.
2. State-change events (started / needs_cookie / complete / error).
3. Rate-limited milestone progress.
4. Command long-poll (`/status`, `/health` first; then control commands).
5. Confirm-step for destructive commands.
