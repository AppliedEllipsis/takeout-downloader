# `.wiki` — takeout_downloader_script

A parallel map of this project for both humans and AI agents. If you can't answer
"where is X / how does Y work / what did we decide about Z" from this folder, the
wiki is incomplete — fix it as part of your current work.

**Last updated:** 2026-09-19 (v3 autopilot work, branch `feat/takeout-autopilot`)

| File | Purpose |
|---|---|
| [measured-facts.md](measured-facts.md) | ⭐ **START HERE.** Every value measured live 2026-09-19: the `rapt` credential chain, the file-host headers, attempt costs, DOM facts, environment. The reference v3 must be built against. |
| [decisions.md](decisions.md) | Append-only ADR log. **The measured root cause and the hybrid architecture are decided here.** |
| [architecture.md](architecture.md) | The full map: directory tree with per-file purpose, the three generations, component list, data flow. ⚠️ **PRELIMINARY — pending recon.** |
| [infrastructure.md](infrastructure.md) | Host, containers, mounts, disks, ports, service dependencies. **Verified live 2026-09-19.** |
| [runbooks.md](runbooks.md) | Copy-pasteable workflows: server probes, session forensics, container ops, the attempt-cost experiment. |
| [failure-modes.md](failure-modes.md) | **Index of all 12 missing failure modes**, plus the three that needed this session's measurements in full: **1.9** tab flood, **1.13** budget exhaustion, **1.18** container OOM. |
| [failure-modes-storage.md](failure-modes-storage.md) | 1.7 disk full on `/` · 1.8 JuiceFS FUSE stall · 1.16 `ENOSPC` mid-write |
| [failure-modes-exports.md](failure-modes-exports.md) | 1.14 archive expiry · 1.15 partial/corrupt part · 1.10 stale `SingletonLock` |
| [failure-modes-runtime.md](failure-modes-runtime.md) | 1.11 cloudflared rotation · 1.12 token mismatch · 1.17 server reboot |
| [archival-inventory.md](archival-inventory.md) | What Takeout data exists, verified through the archive mount: per-part integrity, what is complete vs truncated, the retirement record. **`braincreation` is partial with 3 corrupt parts; `andrew` is complete.** |
| [journal.md](journal.md) | Dated work journal with the exact commands run and their results. |
| [credentials.md](credentials.md) | Credential **pointers only** — never secrets. |

## What this project is

A self-hosted Google Takeout bulk downloader. Goal: use a remote server and its
Docker desktop environment with a browser to authenticate a Google session,
download **all** parts of a Takeout export, and **validate** that the download is
complete.

Three generations of that idea exist side by side (TUI → v1 manager → v2 engine).
All three share one root mistake — the Google cookie is taken out of the browser
and replayed from a separate HTTP client. A v3 ("autopilot") redesign is in
progress that lets Chromium download the files itself.

## Current status (2026-09-19)

- **v3 has not been written yet.** Branch `feat/takeout-autopilot` is at `c0e3b8d` with **zero commits**.
- A prior Claude Code session that planned v3 died on a rate limit; its work was recovered and is documented in the **main checkout's** untracked `.recon/` folder (see `.recon/00-handover.md`).
- The remote server is **memory-exhausted** by 314 stacked Google sign-in tabs in the container's Chromium. Cleanup plan: `.recon/07-live-state-and-cleanup-plan.md`.

> **Note on artifact locations:** reconnaissance lives in the **main checkout** at
> `D:/_projects/takeout_downloader_script/.recon/` (untracked). The wiki lives here
> in the worktree. Reconcile the two before the final commit.
