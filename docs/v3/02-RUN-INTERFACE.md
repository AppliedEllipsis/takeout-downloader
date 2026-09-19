# v3 run interface — FROZEN CONTRACT

**Status: frozen 2026-09-19.** `autopilot/run.py` is being written against this spec
concurrently with `tests/v3/test_integration.py`. Do not change this file without
updating both.

## Purpose

`run.py` is the orchestrator: the thing that turns twelve tested modules into one
machine. Today `grep -rln 'def run|orchestrat|run_once|def main|__main__' autopilot/`
finds **nothing** — every module works alone and none work in sequence.

## Public surface (frozen)

```python
from dataclasses import dataclass
from typing import Callable, Optional

@dataclass
class RunConfig:
    archive_id: str
    ledger_path: str              # MUST be local disk; a FUSE path is refused
    staging_dir: str              # where bytes land, e.g. /config/jobs/<id>
    archive_dir: str              # final home, e.g. /opt/archives/google-takeout/<acct>/<ts>
    account: Optional[str] = None
    cdp_http: str = "http://127.0.0.1:9222"
    require_mount: bool = True    # False only for tests/local dev
    max_parts: Optional[int] = None   # bound a smoke run
    verify_hash: bool = False
    settle: float = 20.0          # seconds to let Network events arrive after a navigate
    work_url: Optional[str] = None    # the archive page; derived if absent

@dataclass
class RunOutcome:
    archive_id: str
    status: str          # see RUN_OUTCOME_STATUSES
    parts_expected: int = 0
    parts_done: int = 0
    bytes_moved: int = 0
    report_markdown: str = ""
    error: Optional[str] = None

RUN_OUTCOME_STATUSES = (
    "complete",               # every expected part verified and moved
    "needs_reauth",           # a ReAuth challenge was hit; a human must act
    "incomplete",             # ran out of work/budget; resumable
    "expired_unrecoverable",  # the export is gone; a NEW export is the only remedy
    "failed",                 # anything else; see `error`
)

async def run_once(
    cfg: RunConfig,
    *,
    session_factory: Optional[Callable] = None,   # () -> async CM yielding CdpSession
    http_client=None,                             # autopilot.transport.HttpClient
    now=None,                                     # datetime, injected for testability
) -> RunOutcome: ...

def run_once_sync(cfg: RunConfig, **kw) -> RunOutcome: ...   # asyncio.run wrapper
```

## Behavioural contract (what the integration test asserts)

Each rule below is a decision from `docs/v3/01-ARCHITECTURE.md`. The test may assert
any of them.

1. **Ordering of effects.** For each part, the sequence is
   `mint (only if not already cached) → transfer → verify (local) → move`, and the
   ledger is written after each step so a crash resumes rather than restarts.

2. **The mint cache is honoured.** If `ledger.needs_mint(archive_id, idx)` is False,
   `run_once` must **not** mint that part again — a mint is measured `Δ1` and a
   re-mint is that cost for nothing.

3. **`NeedsReauth` short-circuits into a status, not an exception.**
   If `mint` or `transfer` raises `autopilot.errors.NeedsReauth`, `run_once`:
   * sets the job status to `needs_reauth` in the ledger,
   * returns `RunOutcome.status == "needs_reauth"` with `error` mentioning the URL,
   * **does not raise**, and **does not** set any part to `failed`.

4. **An expired export is terminal.** If the scraped archive page reports an
   `expiry` in the past (relative to the injected `now`), or the job is already
   `expired_unrecoverable`, `run_once` returns that status and **stops** — it must
   not attempt to mint. This is the fix for the three-month tab-flood loop.

5. **`max_parts` bounds the work.** With `max_parts=1`, at most one part is
   transferred, and the outcome is `incomplete` unless that was the only part.

6. **The ledger path is never on a FUSE mount.** A FUSE `ledger_path` raises
   `autopilot.ledger.LedgerOnFuseMount` — the constructor path is exercised through
   `session_factory`/`http_client` injection, not by mocking `open_ledger`.

7. **Empty scrape is not success.** Zero parts scraped yields `status == "failed"`
   (or `incomplete`) with a non-empty `error` — never `complete`. The report already
   encodes this (`report.py` → "UNKNOWN — no parts were scraped").

8. **The report is always returned.** `report_markdown` is non-empty on every
   non-exception path, including `needs_reauth`.

9. **Injection is total.** With `session_factory` and `http_client` both supplied,
   `run_once` performs **no real network I/O and no real CDP connection**. This is
   what makes the integration test possible, and it is the property the test leans
   on hardest.

## Module interfaces the orchestrator uses (already implemented and tested)

| Called | From | Notes |
|---|---|---|
| `read_archive(session, url, settle=)` → `ArchivePage` | `scrape.py` | `page.parts: list[PartLink]` each with `.index`, `.filename`, `.redirector`; `page.challenged`, `page.expiry`, `page.assert_indexed()` |
| `mint(session, redirector, max_hops=, settle=, restore_url=)` → `MintResult` | `mint.py` | `.url`; raises `NeedsReauth`, `HopLimitExceeded`, `MintError` |
| `pull_jar(session)` → `CookieJar` | `jar.py` | `.header`; raises `CookieError` |
| `download(client, url, dest, jar_header=, expected_size=, etag=, ...)` → `TransferResult` | `transport.py` | `.status` in `complete/partial/already-complete`; raises `NeedsReauth`, `TransferError`, `RemoteChanged` |
| `verify_local(path, size_expected=, hash_check=)` → `VerifyOutcome` | `verify.py` | `.ok`, `.resumable`, `.corrupt` |
| `plan_moves(sources, dest_index)` / `move_part(src, dest_dir, expected_size=)` | `mover.py` | `index_destination(dir)`, `assert_destination_ready(dir, mounts=, require_mount=)` |
| `open_ledger(path, mounts=)` → `Ledger` | `ledger.py` | `upsert_job`, `upsert_parts`, `record_mint`, `minted_url`, `needs_mint`, `record_transfer`, `set_part_status`, `set_job_status`, `parts`, `summary` |
| `build_report(parts, archive_id=, account=, status=, expiry_at=, now=)` → `Report` | `report.py` | `.verdict`, `.as_markdown()` |
| `WebSocketTransport.connect(url)` | `ws_transport.py` | the only real socket; injectable away |

## CLI surface (frozen)

```
python -m autopilot run    --archive-id ID --ledger PATH --staging DIR --archive DIR [--account A] [--max-parts N] [--json]
python -m autopilot report --archive-id ID --ledger PATH [--json]
```

* `run` exits **0** on `complete`, **2** on `needs_reauth`, **3** on `expired_unrecoverable`,
  **1** otherwise — so a shell/cron caller can branch without parsing prose.
* `--json` prints the `RunOutcome`/`Report` as JSON instead of markdown.
* Both must work with `--ledger` pointing at a real local path and must not require
  a browser for `report` (it reads the ledger only).
