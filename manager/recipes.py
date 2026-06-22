"""Workflow recipes: the "repeat-without-LLM" store + replay.

Spec: docs/webgui/02-manager-service.md ("Repeat-without-LLM"),
      docs/webgui/01-architecture.md ("Repeat-without-LLM, in one line").

A *recipe* is the durable description of a completed run: which account label,
where it landed, how many parts, what parallelism — everything needed to repeat
the download later WITHOUT a model in the loop. The first run (with the Pi
agent/you driving) produces the recipe; subsequent runs replay it.

Replay re-triggers a fresh export + capture in the already-logged-in browser via
CDP (Chrome DevTools Protocol) — the same browser the extension lives in. The
extension's capture listener then auto-POSTs the fresh payload to /api/payload,
which the orchestrator routes exactly like a manual capture. So replay reuses
the entire Phase 2-5 pipeline; it only adds the *trigger*.

The CDP trigger is injected as a callable so this module stays testable without
a real browser. `CdpTrigger` is the production implementation; tests pass a fake.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("manager.recipes")

RECIPE_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_name(name: str) -> str:
    keep = "".join(c if (c.isalnum() or c in "._-") else "-" for c in (name or ""))
    return keep.strip("-._") or "recipe"


@dataclass
class Recipe:
    name: str
    account_label: str
    parallel: int
    max_exports: int
    last_output_dir: str
    last_parts_total: int
    last_bytes_total: int
    created_at: str
    updated_at: str
    run_count: int = 0
    schedule_cron: Optional[str] = None
    # Identity hints carried so a replay can re-derive the dated output dir.
    meta: Optional[dict] = None
    version: int = RECIPE_VERSION

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "Recipe":
        known = {k: d.get(k) for k in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**known)


class RecipeStore:
    """JSON-file-backed recipe store under <takeout_root>/.recipes/."""

    def __init__(self, recipes_dir: Path, trigger: Optional[Callable[[Recipe], bool]] = None):
        self.dir = Path(recipes_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        # trigger(recipe) -> bool: cause a fresh export+capture in the browser.
        # When None, replay falls back to "request recapture" semantics (the
        # browser must already be on a Takeout export the extension can grab).
        self._trigger = trigger
        self._lock = threading.Lock()

    # -- path helpers ---------------------------------------------------------
    def _path(self, name: str) -> Path:
        return self.dir / f"{_sanitize_name(name)}.json"

    # -- CRUD -----------------------------------------------------------------
    def record_from_job(self, job_snapshot: dict) -> Optional[Recipe]:
        """Create/update a recipe from a completed job snapshot. Idempotent by
        account label (the recipe name defaults to the workflow/account)."""
        meta = job_snapshot.get("meta") or {}
        name = meta.get("account_label") or job_snapshot.get("workflow") or "recipe"
        totals = job_snapshot.get("totals") or {}
        with self._lock:
            existing = self._load(name)
            now = _now()
            if existing:
                existing.parallel = job_snapshot.get("parallel", existing.parallel)
                existing.max_exports = job_snapshot.get("max_exports", existing.max_exports)
                existing.last_output_dir = job_snapshot.get("output_dir", existing.last_output_dir)
                existing.last_parts_total = totals.get("parts_total", existing.last_parts_total)
                existing.last_bytes_total = totals.get("bytes_total", existing.last_bytes_total)
                existing.updated_at = now
                existing.meta = meta or existing.meta
                self._save(existing)
                return existing
            rec = Recipe(
                name=_sanitize_name(name),
                account_label=meta.get("account_label") or name,
                parallel=job_snapshot.get("parallel", 4),
                max_exports=job_snapshot.get("max_exports", 500),
                last_output_dir=job_snapshot.get("output_dir", ""),
                last_parts_total=totals.get("parts_total", 0),
                last_bytes_total=totals.get("bytes_total", 0),
                created_at=now,
                updated_at=now,
                meta=meta,
            )
            self._save(rec)
            return rec

    def list_names(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.json"))

    def list_recipes(self) -> list[dict]:
        out = []
        for p in sorted(self.dir.glob("*.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
        return out

    def get(self, name: str) -> Optional[Recipe]:
        with self._lock:
            return self._load(name)

    def delete(self, name: str) -> bool:
        p = self._path(name)
        if p.exists():
            p.unlink()
            return True
        return False

    def set_schedule(self, name: str, cron: Optional[str]) -> bool:
        with self._lock:
            rec = self._load(name)
            if not rec:
                return False
            rec.schedule_cron = cron
            rec.updated_at = _now()
            self._save(rec)
            return True

    # -- replay (the repeat-without-LLM core) ---------------------------------
    def run(self, name: str) -> bool:
        """Replay a recipe: trigger a fresh export+capture in the browser. The
        extension auto-POSTs the resulting payload, which the orchestrator
        routes as a normal job. Returns True if the trigger fired."""
        rec = self.get(name)
        if not rec:
            log.warning("run: no such recipe %r", name)
            return False
        with self._lock:
            rec.run_count += 1
            rec.updated_at = _now()
            self._save(rec)
        if self._trigger is None:
            log.warning("run: no CDP trigger configured; recipe %r not auto-started", name)
            return False
        try:
            return bool(self._trigger(rec))
        except Exception as e:  # noqa: BLE001
            log.error("run: trigger for %r failed: %r", name, e)
            return False

    # -- persistence ----------------------------------------------------------
    def _load(self, name: str) -> Optional[Recipe]:
        p = self._path(name)
        if not p.exists():
            return None
        try:
            return Recipe.from_json(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError, TypeError) as e:
            log.debug("load recipe %r failed: %r", name, e)
            return None

    def _save(self, rec: Recipe) -> None:
        p = self._path(rec.name)
        fd, tmp = tempfile.mkstemp(dir=str(self.dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(rec.to_json(), fh, indent=2)
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


class CdpTrigger:
    """Production replay trigger: drive the hosted Chromium over CDP to open
    Takeout and start a fresh export, so the extension captures + auto-POSTs.

    This talks to Chrome's DevTools HTTP/WS endpoint on 127.0.0.1:<cdp_port>
    (the webtop launches chromium with --remote-debugging-port). It uses only
    stdlib + websocket frames kept minimal; if the browser/CDP is unreachable
    it returns False and the manager escalates (Telegram) just like a failed
    recapture. No model is involved.
    """

    def __init__(self, cdp_port: int = 9222, takeout_url: str = "https://takeout.google.com/manage"):
        self.cdp_port = cdp_port
        self.takeout_url = takeout_url

    def __call__(self, recipe: "Recipe") -> bool:
        import urllib.request
        # 1. Find a target (tab) or open one at the Takeout manage page.
        base = f"http://127.0.0.1:{self.cdp_port}"
        try:
            with urllib.request.urlopen(f"{base}/json/version", timeout=5) as r:
                json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            log.error("CdpTrigger: CDP unreachable on %s: %r", base, e)
            return False
        # 2. Open (or focus) a Takeout tab. The /json/new?URL endpoint creates a
        #    tab; the extension content script then reports identity + the page
        #    auto-shows the Download buttons. The actual "start export" click is
        #    handled by the extension's recaptureDownload routine once the
        #    manager flags needs_cookie, OR by the user the first time. Here we
        #    just ensure the tab exists so the extension can act.
        try:
            req = urllib.request.Request(
                f"{base}/json/new?{urllib.parse.quote(self.takeout_url, safe=':/?=&')}",
                method="PUT")
            with urllib.request.urlopen(req, timeout=5) as r:
                tab = json.loads(r.read().decode("utf-8"))
            log.info("CdpTrigger: opened Takeout tab %s for recipe %s",
                     tab.get("id"), recipe.name)
            return True
        except Exception as e:  # noqa: BLE001
            # Older Chrome wants GET on /json/new; try that.
            try:
                with urllib.request.urlopen(f"{base}/json/new?{self.takeout_url}", timeout=5) as r:
                    json.loads(r.read().decode("utf-8"))
                return True
            except Exception as e2:  # noqa: BLE001
                log.error("CdpTrigger: open tab failed: %r / %r", e, e2)
                return False


import urllib.parse  # noqa: E402  (used in CdpTrigger)
