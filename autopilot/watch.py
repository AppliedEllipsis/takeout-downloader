"""Persistent, fair unattended scheduler. No authentication/password automation."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .discovery import DiscoveryBlocked, archive_id, automation_session, discover, safe_url
from .ledger import inspect_ledger_path
from .run import RunConfig, run_once_sync

LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
TERMINAL = {"complete", "expired_unrecoverable", "quota_exceeded", "google_failed"}
STATUSES = TERMINAL | {"pending", "ready", "incomplete", "failed", "needs_reauth", "storage_blocked"}


@dataclass(frozen=True)
class Account:
    name: str
    manage_url: str
    user_id: str


@dataclass(frozen=True)
class WatchConfig:
    state_dir: str
    ledger: str
    staging_root: str
    archive_root: str
    archive_remote: str
    cdp_http: str
    accounts: tuple[Account, ...]
    poll_seconds: float = 60
    retry_max_seconds: float = 3600
    reauth_retry_seconds: float = 300
    min_headroom: int = 5 << 30
    staging_reserve: int = 5 << 30
    rclone_binary: str = "rclone"
    rclone_config: str | None = None

    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or set(data) - set(cls.__dataclass_fields__):
            raise ValueError("invalid watcher configuration fields")
        data = dict(data)
        accounts = data.get("accounts")
        if not isinstance(accounts, list) or not accounts or len(accounts) > 100:
            raise ValueError("invalid accounts")
        parsed = []
        for a in accounts:
            if not isinstance(a, dict) or set(a) != {"name", "manage_url", "user_id"}:
                raise ValueError("invalid account fields")
            if not isinstance(a["name"], str) or not LABEL.fullmatch(a["name"]):
                raise ValueError("invalid account label")
            safe_url(a["manage_url"], a["user_id"])
            parsed.append(Account(**a))
        if len({a.name.lower() for a in parsed}) != len(parsed) or len({a.user_id for a in parsed}) != len(parsed):
            raise ValueError("duplicate account")
        data["accounts"] = tuple(parsed)
        cfg = cls(**data)
        cfg.validate()
        return cfg

    def validate(self):
        for key in ("state_dir", "ledger", "staging_root", "archive_root", "rclone_config"):
            value = getattr(self, key)
            if key == "rclone_config" and value is None:
                continue
            if not isinstance(value, str) or not value or any(c in value for c in "\x00\r\n") or value.startswith(("\\\\", "//")) or not Path(value).is_absolute() or ".." in Path(value).parts:
                raise ValueError("invalid absolute path: " + key)
            # Refuse symlink roots/ancestors to prevent derived paths escaping.
            p = Path(value)
            if any(q.is_symlink() for q in (p, *p.parents)):
                raise ValueError("symlink path refused: " + key)
        for key in ("state_dir", "ledger", "staging_root"):
            inspect_ledger_path(getattr(self, key))
        for key in ("poll_seconds", "retry_max_seconds", "reauth_retry_seconds"):
            v = getattr(self, key)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 1 <= v <= 604800:
                raise ValueError("invalid interval: " + key)
        if self.retry_max_seconds < self.poll_seconds:
            raise ValueError("retry maximum below polling interval")
        for key in ("min_headroom", "staging_reserve"):
            v = getattr(self, key)
            if type(v) is not int or v < 0 or v > 1 << 60:
                raise ValueError("invalid budget: " + key)
        if not isinstance(self.archive_remote, str) or not re.fullmatch(r"[A-Za-z0-9_-]+:[A-Za-z0-9_./ -]*", self.archive_remote) or ".." in self.archive_remote.split(":", 1)[-1].split("/"):
            raise ValueError("invalid production remote")
        if not isinstance(self.rclone_binary, str) or not self.rclone_binary or self.rclone_binary.startswith("-") or any(c in self.rclone_binary for c in "\x00\r\n"):
            raise ValueError("invalid rclone binary")
        p = urlsplit(self.cdp_http) if isinstance(self.cdp_http, str) else None
        if not p or p.scheme != "http" or p.hostname not in {"localhost", "127.0.0.1", "::1"} or p.username or p.password or p.path or p.query or p.fragment or any(c.isspace() for c in self.cdp_http):
            raise ValueError("CDP must be loopback HTTP")
        if not 1 <= (p.port or 80) <= 65535:
            raise ValueError("invalid CDP port")
        for a in self.accounts:
            if not LABEL.fullmatch(a.name):
                raise ValueError("invalid account label")
            safe_url(a.manage_url, a.user_id)


class LockBusy(RuntimeError):
    pass


class InstanceLock:
    """OS-owned lock (released on crash), retained across every service tick."""
    def __init__(self, path):
        self.path, self.file = path, None

    def __enter__(self):
        self.file = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                if not self.file.read(1):
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise LockBusy("another watcher owns state") from None
        return self

    def __exit__(self, *args):
        if self.file:
            self.file.close()


def notify(message):
    """Optional bounded standard-library Telegram; never surface HTTP exceptions."""
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if os.getenv("TELEGRAM_ENABLED", "false").lower() != "true" or not token or not chat:
        return
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        return
    try:
        body = json.dumps({"chat_id": chat, "text": message[:1000]}).encode()
        req = Request("https://api.telegram.org/bot" + token + "/sendMessage", data=body,
                      headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=5) as r:
            r.read(4096)
    except Exception:
        pass


def run_isolated(cfg):
    return run_once_sync(cfg, session_factory=lambda: automation_session(cfg.cdp_http))


class Watcher:
    def __init__(self, cfg, *, discovery=discover, runner=run_isolated, clock=time.time, notifier=notify):
        cfg.validate()  # no browser or DB effects before validation
        self.cfg, self.discovery, self.runner = cfg, discovery, runner
        self.clock, self.notifier = clock, notifier
        self.state = {"version": 1, "accounts": {}, "jobs": {}}
        self.path = Path(cfg.state_dir) / "watch-status.json"

    def load(self):
        if not self.path.exists():
            return
        if self.path.stat().st_size > 4 << 20:
            raise ValueError("watch state too large")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        # Whitelist reconstruction discards unknown fields, secrets and URLs.
        for a in self.cfg.accounts:
            account_state = data.get("accounts", {}).get(a.name, {})
            if account_state.get("status") in {"ready", "needs_reauth", "identity_or_discovery_blocked"}:
                clean_account = {"status": account_state["status"]}
                for field in ("retry_at", "last_discovery", "failures"):
                    value = account_state.get(field, 0)
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                        raise ValueError("invalid account counters")
                    clean_account[field] = value
                self.state["accounts"][a.name] = clean_account
            for key, job in data.get("jobs", {}).items():
                if not key.startswith(a.name + "/"):
                    continue
                ident = archive_id(key.split("/", 1)[1])
                if job.get("status") not in STATUSES:
                    raise ValueError("invalid persisted status")
                clean = {"status": job["status"]}
                for field in ("retry_at", "mint_retry_at", "failures", "parts_expected", "parts_done", "last_discovery"):
                    value = job.get(field, 0)
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                        raise ValueError("invalid persisted counters")
                    clean[field] = value
                clean["minted"] = job.get("minted") is True
                if job.get("mint_status") in {"urls_earned", "needs_reauth", "failed"}:
                    clean["mint_status"] = job["mint_status"]
                clean["mint_blocked"] = job.get("mint_blocked") is True
                self.state["jobs"][a.name + "/" + ident] = clean

    def save(self):
        fd, temp = tempfile.mkstemp(prefix=".watch-", dir=self.cfg.state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.state, f, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, self.path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def run_config(self, a, export, *, mint=False):
        c = self.cfg
        suffix = a.name + "/" + export.archive_id
        for root in (c.staging_root, c.archive_root):
            p = Path(root) / suffix
            if any(q.is_symlink() for q in (p, *p.parents)):
                raise ValueError("symlink job directory refused")
        return RunConfig(archive_id=export.archive_id, ledger_path=c.ledger,
                         staging_dir=str(Path(c.staging_root) / suffix),
                         archive_dir=str(Path(c.archive_root) / suffix), account=a.name,
                         cdp_http=c.cdp_http, work_url=export.archive_url, max_parts=None if mint else 1,
                         mint_only=mint, cached_transfer_only=not mint,
                         archive_remote=c.archive_remote.rstrip("/") + "/" + suffix,
                         rclone_binary=c.rclone_binary, rclone_config=c.rclone_config,
                         min_headroom=c.min_headroom, staging_reserve=c.staging_reserve,
                         verify_hash=True, clean_staging=True)

    def announce(self, message):
        try:
            self.notifier(message)
        except Exception:
            pass

    def tick(self):
        now = self.clock()
        for a in self.cfg.accounts:
            ast = self.state["accounts"].setdefault(a.name, {})
            if ast.get("retry_at", 0) > now:
                continue
            try:
                exports = list(self.discovery(a, self.cfg.cdp_http))
                if len(exports) > 100:
                    raise DiscoveryBlocked("export limit exceeded")
                # Validate the entire account before any runner/DB operation.
                for e in exports:
                    archive_id(e.archive_id)
                    safe_url(e.archive_url, a.user_id, archive=True)
                    if e.user_id != a.user_id or e.status not in STATUSES or type(e.parts_expected) is not int or e.parts_expected < 0:
                        raise DiscoveryBlocked("invalid discovery identity or metadata")
                    if urlsplit(e.archive_url).path.split("/")[-1].lower() != e.archive_id:
                        raise DiscoveryBlocked("archive identity mismatch")
                ast.update(status="ready", last_discovery=now, retry_at=0, failures=0)
            except Exception as exc:
                status = "needs_reauth" if isinstance(exc, DiscoveryBlocked) and str(exc) == "needs_reauth" else "identity_or_discovery_blocked"
                failures = min(ast.get("failures", 0) + 1, 20)
                delay = self.cfg.reauth_retry_seconds if status == "needs_reauth" else min(self.cfg.retry_max_seconds, self.cfg.poll_seconds * 2 ** failures)
                ast.update(status=status, last_discovery=now, retry_at=now + delay, failures=failures)
                self.announce(a.name + ": " + status)
                continue
            seen = set()
            for e in exports:
                key = a.name + "/" + e.archive_id
                if key in seen:
                    continue
                seen.add(key)
                j = self.state["jobs"].setdefault(key, {"status": "pending", "minted": False, "failures": 0})
                j["last_discovery"] = now
                if j["status"] in TERMINAL or j.get("retry_at", 0) > now:
                    continue
                if e.status != "ready" or e.parts_expected == 0:
                    j["status"] = e.status if e.status != "ready" else "pending"
                    continue
                try:
                    # Cached transfers precede any later mint retry. First contact batches URLs.
                    if not j.get("minted"):
                        out = self.runner(self.run_config(a, e, mint=True))
                        j["minted"] = True
                        j["mint_blocked"] = out.status == "needs_reauth"
                        j["mint_status"] = "urls_earned" if out.status == "incomplete" else "needs_reauth" if out.status == "needs_reauth" else "failed"
                        j["mint_retry_at"] = now + self.cfg.reauth_retry_seconds
                        if out.status in TERMINAL - {"complete"}:
                            j["status"] = out.status
                            continue
                    out = self.runner(self.run_config(a, e))
                    status = out.status if out.status in STATUSES else "failed"
                    if status == "failed" and isinstance(getattr(out, "error", None), str) and re.search(r"headroom|insufficient.*space|no space left|staging.*space", out.error, re.I):
                        status = "storage_blocked"
                    if status == "complete" and (out.parts_expected <= 0 or out.parts_done != out.parts_expected):
                        status = "incomplete"
                    if any(type(v) is not int or v < 0 for v in (out.parts_expected, out.parts_done)):
                        raise ValueError("invalid runner counters")
                    j.update(parts_expected=out.parts_expected, parts_done=out.parts_done)
                    if status not in TERMINAL and now >= j.get("mint_retry_at", 0):
                        minted = self.runner(self.run_config(a, e, mint=True))
                        j["mint_blocked"] = minted.status == "needs_reauth"
                        j["mint_status"] = "urls_earned" if minted.status == "incomplete" else "needs_reauth" if minted.status == "needs_reauth" else "failed"
                        j["mint_retry_at"] = now + self.cfg.reauth_retry_seconds
                        if minted.status in TERMINAL - {"complete"}:
                            status = minted.status
                    if j.get("mint_blocked") and status not in TERMINAL:
                        status = "needs_reauth"
                except Exception:
                    status = "failed"  # raw exceptions may contain signed URLs/cookies
                previous = j["status"]
                j["status"] = status
                j["failures"] = min(j.get("failures", 0) + 1, 20) if status in {"failed", "storage_blocked"} else 0
                delay = min(self.cfg.retry_max_seconds, self.cfg.poll_seconds * 2 ** j["failures"])
                j["retry_at"] = now + delay
                if previous != status:
                    self.announce(key + ": " + status)
        self.save()
        return self.state


def watch(cfg, *, once=False, dry_run=False, **dependencies):
    worker = Watcher(cfg, **dependencies)
    if dry_run:
        return {"status": "dry_run", "accounts": [a.name for a in cfg.accounts]}
    Path(cfg.state_dir).mkdir(parents=True, exist_ok=True)
    for reserved in (worker.path, Path(cfg.state_dir) / "watch.lock"):
        if reserved.is_symlink():
            raise ValueError("symlink state file refused")
    with InstanceLock(Path(cfg.state_dir) / "watch.lock"):
        worker.load()
        while True:
            state = worker.tick()
            if once:
                return state
            time.sleep(cfg.poll_seconds)
