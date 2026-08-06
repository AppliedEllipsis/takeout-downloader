# v2 — Project Plan: Attempt-Limited Multi-TB Takeout Downloader

> **Status: DESIGN COMPLETE, FOUNDATION IMPLEMENTED.** Contracts, the
> attempt-ledger core, and 111 passing tests are in the tree. The remaining
> phases are specified to the level a weaker model can execute without making
> design decisions.

## The problem in one paragraph

Google Takeout allows **5 downloads per archive file**, and archives **expire
in ~7 days**. The download cookie is **idle-fragile (~1–2 min)** but an
**in-flight stream survives for hours**. v1 burned both budgets on
non-download work — a 1-byte Range probe per part during discovery, a
multi-minute JuiceFS stat pre-pass, and a final full-file `testzip()` — so the
operator ran out of attempts before getting the bytes.

## The v2 answer in one paragraph

**Bytes-on-the-wire is the only acceptable reason to contact Google.** The
part list comes from the page scrape (`expectedParts`, free), the attempt
budget is seeded from **Google's own counter** scraped off the page
("Number of times already downloaded: N"), every request is gated by a durable
**attempt ledger**, streams are started inside a **cookie-window burst** (all
at once, then left to run for hours), and verification is **local-only two-seek
STRUCT_OK** — never a full read, never a probe.

## The docs (read in order)

| # | Doc | What it is |
|---|-----|-----------|
| [00](00-CONTRACTS.md) | **Contracts** — the single source of truth | schemas, enums, module layout, invariants |
| [01](01-IDENTITY-AND-SCRAPE.md) | **Identity & the side-channel scrape** | account label provenance ladder, export timestamp, the dl_counts budget ground truth, empirical cost study |
| [02-A](02-BUILD-PLAN-A.md) | **Build plan, phases 0–5** | scaffolding → state/plan/cookie/engine, dep graph |
| [02-B](02-BUILD-PLAN-B.md) | **Build plan, phases 6–10** | API+CLI → extension → migration → cost study → cutover |
| [03](03-UX-AND-OBSERVABILITY.md) | **UX & observability** | CLI/extension/Telegram surfaces, budget UX, formulas |
| [04](04-FAILURE-MODES-AND-RECOVERY.md) | **Failure modes & the operational bible** | every failure, decision trees, attempt-cost-ranked recovery |
| [05](05-PARALLELISM-AND-THROUGHPUT.md) | **Parallelism & throughput** | burst scheduler, choosing N, engine choice, I/O tuning |

## Already implemented and green

```
takeout2/contracts.py    enums, identity provenance, regexes
 takeout2/classify.py     HTTP response -> ReasonCode, END_OF_RANGE vs AUTH_REDIRECT
 takeout2/ledger.py       AttemptLedger: reserve/commit/release, budget ceiling,
                         crash reconciliation, Google dl_count ground truth
 takeout2/state.py        JobStore: SQLite job/part/event, identity upgrade+rename,
                         restart recovery
 takeout2/verify.py       SIZE/STRUCT/HASH ladder, one-scan parts directory
 takeout2/plan.py         zero-probe discovery (payload -> state.db -> filenames -> opt-in probes)
 takeout2/cookie.py       live CDP jar pull (Storage.getCookies), non-localhost guard
 takeout2/engine.py       cookie-window burst scheduler with canary-first
 takeout2/api.py          FastAPI v2: capture sink, jobs/budget/parts, SSE ?since=, doctor
tests/v2/               155+ tests, all network-free, all passing
```

Run the suite:

```bash
cd /d/_projects/takeout_downloader_script && python -m pytest tests/v2/ -q
# → 155 passed
```

## Testing with the small export (the user's plan)

A smaller account export is available for end-to-end testing. The intended flow:

1. **Scaffold a state.db + manager** and point the extension at the v2 capture
   endpoint (`POST /api/v2/capture`) so the manage-page scrape (dl_counts,
   parts_expected, sizes) feeds the ledger for free.
2. **`python -m takeout2.cli run --payload in.json`** to plan + burst a part.
3. **`takeout2 budget <archive_id>`** to watch Google's own counter feed the
   budget view — this doubles as the attempt-cost experiment source.
4. **`takeout2 doctor`** before the run; **`takeout2 verify`** after.

The attempt-cost study (`experiment.py`, phase 9) should run on THIS small
account before any real multi-TB run: a probe / aborted GET / resume on a
throwaway part, diffing dl_counts, settles whether those cost attempts.

## The three design pillars

1. **The attempt ledger** (`ledger.py`) — no Google request without a durable
   reservation; a crash fail-closes (unsettled = assumed consumed);
   Google's own counter outranks our estimate.
2. **The cookie-window burst** (`05` §2, engine in phase 5) — start all
   streams inside one narrow window after a live CDP jar pull, then let them
   run for hours. Never instant-retry into a possibly-dead cookie. Always
   canary the burst with one stream.
3. **Zero-probe everything else** (`plan.py`, `verify.py`) — discovery from
   `expectedParts`, verification from head magic + tail EOCD, both free.

## What is deliberately NOT done yet

The build plan is executed in phases 2–10. Modules `state.py` (phase 2) and
`plan/cookie/engine/api` (phases 3–6) are built. Remaining: the rich CLI
(`cli.py`), the extension wiring (`POST /api/v2/capture`), v1→v2 migration
(`migrate.py`), the attempt-cost study harness (`experiment.py`), and the
server cutover. Each has a spec, a DONE-WHEN, a validation command, and stop
rules — see `02-BUILD-PLAN-A/B.md`.

## The single most valuable next change

Wire the extension's already-scraped `dl_counts` ("Number of times already
downloaded: N") into the backend (`02-BUILD-PLAN-B.md` §7). It costs zero
attempts, it seeds the budget with Google's truth, and it turns the empirical
attempt-cost questions (does a probe/resume/abort cost an attempt?) from
unknowable into measurable (`02-BUILD-PLAN-B.md` §9).
