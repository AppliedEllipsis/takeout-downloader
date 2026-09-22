"""The completeness report — "did we actually get the whole export?".

The question this project kept answering wrongly. v2's verification only ever
checked a zip's header and its end-of-archive marker, and completeness was never
tied to the export's **full part list**. A report that says "all downloaded parts
are valid" while three parts are missing and the export has expired is worse than
no report, because it reads as success.

So this module:

* states the verdict **against the expected part count**, not against what
  happened to arrive;
* says plainly when an export has expired, that the loss is unrecoverable and a
  **new export** is the only remedy (failure mode 1.14);
* keeps `dl_counts` in a clearly-labelled **telemetry** section, because that
  counter is not an invariant;
* treats "attempts spent, zero bytes" as a warning — that is the exact signature
  of v2's parts that were booked as NETWORK_ERROR without a request ever being
  sent.

`now` is injectable so expiry logic is testable without waiting for time to pass.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .ledger import AttemptKind, carries_part_identity

__all__ = ["Report", "build_report", "parse_expiry"]

#: Expiry strings as the manage page renders them, plus ISO for tests.
_EXPIRY_FORMATS = (
    "%B %d, %Y at %I:%M %p",
    "%B %d, %Y, %I:%M %p",
    "%B %d, %Y",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
)


def parse_expiry(value: Optional[str]) -> Optional[datetime]:
    """Parse the page's `Available until …` text. Returns None if unrecognised."""
    if not value:
        return None
    text = " ".join(value.split())
    for fmt in _EXPIRY_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


@dataclass
class Report:
    archive_id: str
    account: Optional[str] = None
    status: str = "pending"
    parts_expected: int = 0
    parts_done: int = 0
    bytes_expected: int = 0
    bytes_on_disk: int = 0
    missing_indices: list = field(default_factory=list)
    partial_indices: list = field(default_factory=list)
    failed_indices: list = field(default_factory=list)
    attempts: dict = field(default_factory=dict)
    expiry_at: Optional[str] = None
    expired: bool = False
    dl_counts: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    verdict: str = "UNKNOWN"
    #: Parts whose bytes are in the archive but which THIS ledger never transferred —
    #: pulled by v2, or by an earlier run whose ledger was reset. Counted in
    #: `parts_done` (they are held) and reported separately so the operator can tell
    #: "v3 downloaded this" from "this was already here".
    parts_in_archive: int = 0

    @property
    def complete(self) -> bool:
        return self.parts_expected > 0 and self.parts_done == self.parts_expected

    def as_markdown(self) -> str:
        pct = (100.0 * self.parts_done / self.parts_expected) if self.parts_expected else 0.0
        lines = [
            f"# Takeout export report — `{self.archive_id}`",
            "",
            f"**Verdict: {self.verdict}**",
            "",
            f"- account: `{self.account or 'unknown'}`",
            f"- job status: `{self.status}`",
            f"- parts: **{self.parts_done}/{self.parts_expected}** ({pct:.0f}%)",
            f"- bytes on disk: {self.bytes_on_disk} of {self.bytes_expected or '?'} expected",
            f"- export expiry: {self.expiry_at or 'not read'}"
            + ("  **← EXPIRED**" if self.expired else ""),
            "",
        ]
        if self.parts_in_archive:
            lines += [
                f"> {self.parts_in_archive} of these part(s) were already in the "
                "archive and were **not** transferred by this ledger. Without this "
                "line the count reads as work v3 did.",
                "",
            ]
        if self.missing_indices:
            lines += [
                "## Missing parts",
                "",
                f"Indices with no completed file: `{self.missing_indices}`",
                "",
            ]
        if self.partial_indices:
            lines += ["## Partial parts (resumable)", "",
                      f"`{self.partial_indices}` — a `Range` resume is measured **Δ0**, "
                      "so these cost nothing to finish.", ""]
        if self.failed_indices:
            lines += ["## Failed parts", "", f"`{self.failed_indices}`", ""]
        if self.warnings:
            lines += ["## Warnings", ""] + [f"- {w}" for w in self.warnings] + [""]
        lines += [
            "## Attempts spent",
            "",
            "| kind | count | measured cost |",
            "|---|---|---|",
            f"| mint | {self.attempts.get('mint', 0)} | Δ1 each |",
            f"| transfer | {self.attempts.get('transfer', 0)} | **unmeasured** |",
            f"| resume | {self.attempts.get('resume', 0)} | Δ0 each |",
            "",
            "> `transfer` is a fresh full client-side GET. Its cost was **never "
            "measured** — every measured Δ0 was a `Range` request. Do not budget "
            "it as free.",
            "",
        ]
        if self.dl_counts:
            lines += [
                "## Telemetry (NOT an invariant)",
                "",
                "Google's own counter, as last observed. It gave Δ2 then Δ0 for "
                "superficially identical actions, so it is reported for the operator "
                "and consulted by no decision:",
                "",
            ] + [f"- `{name}`: {n}" for name, n in sorted(self.dl_counts.items())] + [""]
        return "\n".join(lines)


def build_report(parts, *, archive_id: str, account: Optional[str] = None,
                 status: str = "pending", expiry_at: Optional[str] = None,
                 now: Optional[datetime] = None,
                 attempts: Optional[dict] = None,
                 dl_counts: Optional[dict] = None,
                 attempt_kinds: Optional[dict] = None,
                 present: Optional[dict] = None) -> Report:
    """Assemble a report from ledger part rows.

    `parts` is any iterable of objects with `idx`, `status`, `size_expected`,
    `size_on_disk`, `attempts`, `dl_count_seen` — i.e. `ledger.PartRow`, but not
    tied to it so this stays trivially testable.

    `attempts` is `ledger.attempts_by_kind(archive_id)`. It was originally
    omitted, which made the report's "Attempts spent" table print **zeros
    always** — a decorative instrument in a project whose whole lesson is not to
    print numbers that are not measurements. `tests/v3/test_report.py` now pins
    that the table reflects real counts.

    `present` maps a part filename to its size in the DESTINATION archive (that is
    `index_destination(archive_dir)`). It exists because the report used to judge
    completeness purely from this ledger's own transfer bookkeeping, and that read
    **dangerously wrong** on a real export: the 62-product archive was complete and
    CRC-verified on disk, and the report announced
    `BLOCKED — re-authentication required (0/68 held)` because v2 had done the
    downloading and this ledger had transferred nothing. An archive-before-delete
    decision made on that line would be made on a falsehood.

    A part counts as held if the ledger says `done` **or** its file is in the archive
    at the expected size. A size mismatch is NOT counted — it becomes a warning, not a
    silent pass. "Held" and "v3 downloaded this" are different claims, so the archive
    count is reported separately rather than folded in.
    """
    now = now or datetime.now(timezone.utc)
    parts = list(parts)

    rep = Report(archive_id=archive_id, account=account, status=status,
                 expiry_at=expiry_at, parts_expected=len(parts),
                 attempts=dict(attempts or {}))
    if dl_counts:
        rep.dl_counts.update(dl_counts)

    for p in parts:
        idx = getattr(p, "idx", None)
        pstatus = getattr(p, "status", "pending")

        # Is this part's file in the DESTINATION, even though the ledger never
        # transferred it? Checked only when the status does not already say `done`, so
        # the two sources cannot double-count.
        archived_size = None
        if present is not None and pstatus != "done":
            name = getattr(p, "filename", None)
            # `present` is a `mover.DestinationIndex` in production (it exposes
            # `size_of`) and a plain dict in tests. Accept both rather than forcing a
            # conversion at every call site.
            hit = None
            if name and carries_part_identity(name):
                size_of = getattr(present, "size_of", None)
                if callable(size_of):
                    hit = size_of(name)
                elif hasattr(present, "get"):
                    hit = present.get(name)
            if hit is not None:
                expected = getattr(p, "size_expected", None)
                if expected is None or int(hit) == int(expected):
                    archived_size = int(hit)
                else:
                    rep.warnings.append(
                        f"part {idx} ({name}): a file of that name is in the archive "
                        f"but is {hit} bytes, not the expected {expected} — NOT counted "
                        "as held")

        if pstatus == "done" or archived_size is not None:
            rep.parts_done += 1
            if archived_size is not None:
                rep.parts_in_archive += 1
        elif pstatus == "partial":
            rep.partial_indices.append(idx)
        elif pstatus in ("failed", "budget_exhausted"):
            rep.failed_indices.append(idx)
        else:
            rep.missing_indices.append(idx)

        if getattr(p, "size_expected", None):
            rep.bytes_expected += int(p.size_expected)
        if archived_size is not None:
            rep.bytes_on_disk += archived_size
        elif getattr(p, "size_on_disk", None):
            rep.bytes_on_disk += int(p.size_on_disk)

        # The v2 signature: a TRANSFER was booked but nothing was ever sent.
        #
        # This used to fire on `attempts > 0 and size_on_disk == 0`, which a MINT
        # satisfies — and a mint legitimately books an attempt while moving zero bytes.
        # Observed live 2026-09-21: a 5-part `--mint-only` run warned about **all five**
        # parts ("an attempt booked with no request sent"), which reads as five failures
        # when the run had done exactly what was asked. On the 19-part export that would
        # have been 19 false alarms.
        #
        # So the KIND now decides. `attempt_kinds` maps idx -> set of kinds; when it is
        # not supplied the warning is suppressed rather than guessed, because a bare
        # count cannot distinguish a legitimate mint from a phantom transfer.
        _kinds = ((attempt_kinds or {}).get(int(idx), set())
                  if attempt_kinds is not None else set())
        _moved = _kinds & {AttemptKind.TRANSFER, AttemptKind.RESUME}
        if (_moved and getattr(p, "attempts", 0)
                and not getattr(p, "size_on_disk", 0)
                and archived_size is None
                and pstatus in ("failed", "pending")):
            rep.warnings.append(
                f"part {idx}: attempt(s) spent with zero bytes on disk "
                "— the v2 NETWORK_ERROR signature (an attempt booked with no "
                "request sent)"
            )

        seen = getattr(p, "dl_count_seen", None)
        if seen is not None:
            rep.dl_counts[getattr(p, "filename", None) or f"idx {idx}"] = seen

    exp = parse_expiry(expiry_at)
    if expiry_at and exp is None:
        rep.warnings.append(f"could not parse the expiry string {expiry_at!r}")
    rep.expired = bool(exp and now > exp)

    if rep.parts_expected == 0:
        rep.verdict = "UNKNOWN — no parts were scraped"
        rep.warnings.append("no parts in the ledger; nothing can be said about completeness")
    elif rep.complete:
        rep.verdict = "COMPLETE — every expected part verified on disk"
    elif rep.expired or status == "expired_unrecoverable":
        rep.verdict = (
            "INCOMPLETE — EXPORT EXPIRED (unrecoverable). "
            "The source objects are gone; a NEW export is the only remedy."
        )
    elif status == "needs_reauth":
        rep.verdict = f"BLOCKED — re-authentication required ({rep.parts_done}/{rep.parts_expected} held)"
    elif status == "quota_exceeded":
        # Deliberately NOT "resumable": every retry of this export is refused.
        # Measured 2026-09-19 — Google caps downloads per export (5) and answers
        # further attempts with `quotaExceeded=true` on the archive-page bounce.
        rep.verdict = (
            f"BLOCKED — export download allowance exhausted "
            f"({rep.parts_done}/{rep.parts_expected} held). "
            "NOT resumable: create a NEW export."
        )
        rep.warnings.append(
            "Google caps downloads per export (measured: 5). This does not "
            "change over time — a new export is required, and the export's own "
            "counter (`dl_counts`) is independent of the ledger's attempt count."
        )
    elif status == "failed":
        # `failed` is a first-class run outcome (`RUN_OUTCOME_STATUSES`) and was the
        # only member with no branch here, so it fell through to the `else` and was
        # reported as *resumable* — telling an operator that a hard failure (a dead
        # browser, an unusable scrape, a rejected destination) would clear itself on
        # the next run. It may or may not; the report should state what happened
        # rather than promise a recovery it cannot know about.
        rep.verdict = (
            f"FAILED — {rep.parts_done}/{rep.parts_expected} parts held. "
            "The run stopped on an error; see `error:` above. Do not assume a "
            "retry will clear it."
        )
    else:
        rep.verdict = (
            f"INCOMPLETE — {rep.parts_done}/{rep.parts_expected} parts; "
            "resumable (Range resumes are free)"
        )

    if rep.partial_indices:
        rep.warnings.append(
            f"{len(rep.partial_indices)} partial part(s): resume with Range (Δ0) "
            "rather than re-minting (Δ1)"
        )
    return rep
