# 01 — IDENTITY, PROVENANCE & THE SIDE-CHANNEL SCRAPE

> Companion to `00-CONTRACTS.md`. This file governs **who** the export belongs
> to, **when** it was created, and **how many attempts Google says remain** —
> all derived from the Takeout "Manage exports" page via the extension.

---

## 1. Why this matters (the 2.8 TB near-miss)

In the first big run the account label flipped mid-job from the URL fallback
`gaia-1005482974000` to the scraped friendly label `braincreation`. Because the
job was keyed on the **label-derived output directory**, the manager did not
recognise 2.8 TB already on disk and began re-downloading everything
(`docs/webgui/14-resume-cookies-multiaccount.md`, root cause #5; fixed in
`fdf8535`).

**Rule that follows:** identity fields are for *humans and folder names*. They
are NEVER used for job matching. Job matching uses `archive_id` only.
Identity may be **upgraded** at any time; the folder is renamed, the job is not
re-created.

---

## 2. The buried treasure: Google tells us the attempt count

`helpers/content.js:168–174` already scrapes this from the manage page:

```js
// "takeout-...-001.zip (Number of times already downloaded: 5)"
const dlCountRe = /(takeout-\d{8}T\d{6}Z-\d+-\d+\.zip)\s*\(Number of times already downloaded:\s*(\d+)\)/g;
```

**This is Google's own authoritative counter for the 5-download limit.**

In v1 it flows only as far as `popup.js:207` for cosmetic display. It is
**never POSTed to the manager**, so the engine has no idea how many attempts a
part has left and happily burns them.

### v2 requirement (highest-value change in the whole project)

`dlCounts` MUST be included in the capture payload, persisted to
`part.attempts_used_remote`, and reconciled with the local ledger on every
capture. Where remote and local disagree, **the remote (Google) value wins** —
it is ground truth; our ledger is only an estimate.

Additional consequence: this gives us a **free empirical answer** to the
question the research agents could not confirm — whether a probe or an aborted
GET consumes an attempt. Take a reading before and after a known operation and
diff it. See §7.

---

## 3. Canonical identity record

```jsonc
{
  "archive_id":   "j= param",              // STABLE KEY. never changes, never used for display
  "export_raw":   "20260616T040104Z",      // baked into every part filename
  "export_ts":    "2026-06-16-04-01-04",   // folder-safe form of export_raw
  "account": {
    "email":        "braincreation@gmail.com",  // may be null
    "label":        "braincreation",            // friendly, may be null
    "gaia_user":    "1005482974000",            // user= param, always present
    "authuser":     "0",
    "label_source": "SCRAPED_EMAIL",            // provenance, see §4
    "confidence":   "HIGH"
  },
  "parts_expected": 62,
  "dl_counts": { "takeout-20260616T040104Z-9-001.zip": 5 },
  "sizes":     { "takeout-20260616T040104Z-9-001.zip": 10737418240 },
  "captured_at": "2026-06-16T15:45:00.000Z",
  "page_url":    "https://takeout.google.com/manage..."
}
```

---

## 4. Label provenance ladder (`label_source`, highest → lowest)

| # | `label_source` | Origin | Confidence | Stable? |
|---|----------------|--------|-----------|---------|
| 1 | `OPERATOR_OVERRIDE` | explicit `?label=` / CLI `--label` | HIGH | yes |
| 2 | `SCRAPED_EMAIL` | account-switcher `aria-label` email local-part | HIGH | yes |
| 3 | `SCRAPED_LABEL` | display name after `"Google Account:"` | MEDIUM | mostly |
| 4 | `GAIA_FALLBACK` | `gaia-<user>` from the `user=` URL param | LOW but **deterministic** | yes |
| 5 | `UNKNOWN` | nothing available | LOWEST | no |

### 4.1 The upgrade rule (replaces v1's silent orphaning)

```
IF a new capture yields a label with strictly HIGHER provenance
   AND archive_id matches an existing job:
     1. keep the SAME job row (archive_id is the key)
     2. rename the output directory   old -> new   (atomic os.rename)
     3. update job.account_label + job.label_source
     4. emit event  {kind: "identity_upgraded", from, to, reason}
     5. NEVER create a second job, NEVER re-download
IF the new label has LOWER provenance than the stored one:
     ignore it entirely (a failed scrape must not downgrade a good label)
```

> v1's bug was treating a *better* label as a *different account*. Provenance
> ranking makes the upgrade explicit and directional.

### 4.2 Folder rename safety

Renaming a directory holding terabytes is instant (same filesystem, inode
rename) but MUST be guarded:
- refuse if any part is `ACTIVE` (pause first, rename, resume);
- refuse if source and target are on different mounts — fall back to writing a
  `LABEL` pointer file and leaving bytes in place;
- always `fsync` the parent directory after rename.

---

## 5. Export timestamp — the reliable field

Unlike the account label, the export timestamp is **trustworthy**: Google bakes
`20260616T040104Z` into every part filename, identical across all parts of one
export.

Extraction order:
1. regex `(\d{8}T\d{6}Z)` over part filenames (authoritative)
2. same regex over the `data-download-uri` attributes
3. same regex over page text
4. **only then** `captured_at` + `-capture` suffix (marks it as inferred)

**Guard:** if two different `\d{8}T\d{6}Z` values appear among the filenames of
one `archive_id`, that is a page-scrape error, not two exports. Take the value
from the **majority of filenames**, log a `WARN`, and record
`export_ts_ambiguous: true`. Never silently pick the first.

A given `(account, export_raw)` pair is globally unique and is what makes
`<account-label>/<export-ts>/` collision-free across accounts and re-exports.

---

## 6. Extension side-channel: what MUST be captured

The extension is our only window into the manage page. Every field below is
free (no Google download request) and must be in the POST body.

| Field | Selector / source | Why it matters |
|-------|-------------------|----------------|
| `dl_counts` | `(Number of times already downloaded: N)` regex | **attempt budget ground truth** |
| `parts_expected` | `aria-label="Download again part X of N"` → N | **zero-probe discovery** |
| `sizes` | `[data-download-uri][data-size]` | verify by size without a network call |
| `uris` | `data-download-uri` | exact per-part URLs; no index guessing |
| `email` / `label` | account-switcher `aria-label`/`title` | folder naming |
| `gaia_user`, `authuser` | URL params | deterministic fallback + multi-login |
| `archive_id` | `j=` URL param | the stable job key |
| `expiry_text` | any "expires" text on the page | 7-day window warning |
| `captured_at` | `Date.now()` | cookie-freshness accounting |

### 6.1 Scrape hardening (v1 selectors are brittle)

Google ships obfuscated, rotating class names; only `aria-label`, `title`,
`data-*` attributes are semi-stable. Therefore:

- Every scraper returns `{value, source, ok}` — never a bare string.
- Each field has **multiple independent strategies**, tried in order, with the
  winning strategy recorded in `scrape_report`.
- A failed scrape is a **null with a reason**, never an empty string that gets
  silently sanitised into a folder name.
- The POST always includes `scrape_report[]` so the operator can see in the CLI
  exactly which strategy produced each field and how long it took.
- Localisation: the `dlCounts` / `part X of N` regexes are English-only. Add a
  numeric-fallback strategy (parse the trailing integer of any parenthesised
  suffix) and surface `locale_warning: true` when the English form misses while
  buttons are present.

### 6.2 Re-scrape cadence

The manage page is already open in the hosted Chromium. The extension MUST
re-scrape and re-POST identity + `dl_counts` (no download click needed, costs
zero attempts):
- on page load / SPA navigation,
- every 60 s while the page is open,
- immediately before the manager begins a burst (on request),
- after any part completes.

This keeps the remote attempt counter fresh without spending anything.

---

## 7. Empirical attempt-cost probe (resolves the open research question)

Neither research agent could confirm whether a `Range` probe or an aborted GET
decrements Google's counter. With `dl_counts` we can simply **measure** it:

```
1. Scrape dl_counts for part P            -> before
2. Perform exactly ONE controlled action on P:
      (a) Range: bytes=0-0 probe, or
      (b) GET aborted after 1 MiB, or
      (c) full successful download
3. Wait for the manage page to refresh; re-scrape -> after
4. cost(action) = after - before
5. Persist the result to docs/v2/ATTEMPT-COST-FINDINGS.md
```

Run this on a **small throwaway export**, never on the real multi-TB one.
Until measured, the conservative assumption in `00-CONTRACTS.md` §1.2 stands
(every request costs 1).

---

## 8. Reconciliation rules

On every capture:

```
for each part filename in dl_counts:
    remote = dl_counts[filename]
    local  = ledger.attempts_used(archive_id, idx)
    part.attempts_used_remote = remote
    if remote > local:
        # Google counted attempts we did not (e.g. browser-native downloads,
        # earlier manual curl, another machine). Trust Google.
        ledger.reconcile_up(archive_id, idx, remote, reason="remote_authoritative")
        emit budget_warning if remaining <= TK2_BUDGET_RESERVE
    if remote < local:
        # our ledger over-counted (probes may be free after all).
        record observation for the cost study; do NOT lower the ledger silently
        emit {kind: "budget_observation", remote, local}
```

`remaining = attempt_budget - max(local, remote)`.
A part with `remaining <= TK2_BUDGET_RESERVE` parks in `BUDGET_EXHAUSTED` and
requires `--force` to touch.

---

## 9. CLI surface for identity & budget

```bash
takeout2 identity <archive_id>     # show identity record + provenance + scrape_report
takeout2 identity <archive_id> --set-label braincreation   # OPERATOR_OVERRIDE + rename
takeout2 budget   <archive_id>     # per-part: used(local) used(remote) remaining
takeout2 budget   <archive_id> --refresh   # ask extension to re-scrape now
```

`takeout2 budget` output (normative shape):

```
archive 9f3a…  braincreation / 2026-06-16-04-01-04   [SCRAPED_EMAIL, HIGH]
part  filename                              size    on-disk  local  google  left  state
   0  takeout-…-9-000.zip                  10.0G     10.0G      1       1     4  DONE STRUCT_OK
   7  takeout-…-9-007.zip                  10.0G      3.2G      4       4     1  PARTIAL ⚠ last attempt
  61  takeout-…-9-061.zip                  10.0G         0      5       5     0  BUDGET_EXHAUSTED ✖
                                                       ── archive total: 5 of 62 parts at risk
```

---

## 10. Invariants (reviewer checklist)

1. `archive_id` is the ONLY job-matching key. Never the folder path, never the label.
2. A label may only be replaced by one of strictly higher provenance.
3. Identity upgrade renames the folder; it never creates a second job.
4. `dl_counts` reaches the manager and outranks the local ledger.
5. Export timestamp comes from filenames first, `captured_at` last (and is then marked inferred).
6. Every scraped field carries `{value, source, ok}` + a `scrape_report` entry.
7. Re-scraping identity/budget costs zero Google download attempts — it must never issue a download request.
