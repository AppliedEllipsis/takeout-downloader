# v3 "autopilot" — design index

The redesign of the Takeout downloader, written **after** the architecture was settled by measurement
rather than by argument. Read `01-ARCHITECTURE.md` first.

| File | Purpose |
|---|---|
| [01-ARCHITECTURE.md](01-ARCHITECTURE.md) | The design: premise, actors, state machine, modules, decisions, invariants, what we keep from v2, open unknowns |

**Companion documents (outside this folder):**

| Where | What |
|---|---|
| `.wiki/measured-facts.md` | Every value the design is built on, measured live 2026-09-19 |
| `.wiki/decisions.md` | The ADR trail, including the superseded architectures |
| `.wiki/failure-modes.md` | Failure modes, with 1.9 (tab flood) fully documented |
| `.wiki/runbooks.md` | Operational procedures, including how to run the attempt-cost experiment |
| `.recon/11-root-cause-confirmed.md` | The ReAuth diagnosis, with the redirect chain |
| `.recon/12-ladder-results-and-architecture.md` | The full measurement ladder and what it determines |

## The design in six lines

1. **The browser mints; the transporter moves.** Only a real browser can satisfy the password ReAuth
   that produces the `rapt` token — and `rapt` gates minting, not transferring.
2. **Mint without downloading** — read the redirect `Location` and abandon the request, so no bytes move
   and there is no race with the auto-cancel. **Goal firm; mechanism unverified** (three candidates,
   see `01-ARCHITECTURE.md` §4.1) — settle it with one experiment before writing code.
3. **`NEEDS_REAUTH` is a first-class state**, alerted and owned — never a `needs_cookie` park that
   spawns a tab a minute for three months.
4. **Stage on `/config`, verify there, then move** — never write to `/`, never run a SQLite ledger on the
   rclone FUSE mount.
5. **`Range` resumes are free; minting is the scarce resource.** Cache minted URLs and never re-mint.
6. **`dl_counts` is telemetry, not an invariant** — the counter is demonstrably not a precise
   per-download oracle.

## Why the old approach is deleted rather than repaired

All three earlier generations extracted the cookie and replayed it for **minting**. That is impossible:
a replayed jar carries no `rapt` and cannot answer an interactive prompt. `takeout2/cookie.py`, the
`Storage.getCookies` capture path, the capture→POST→replay pipeline, the `needs_cookie` park and the
1-minute recapture alarm all exist to serve that false premise and go with it.

The cookie is still used — but only for **transferring**, where it is measured to work.
