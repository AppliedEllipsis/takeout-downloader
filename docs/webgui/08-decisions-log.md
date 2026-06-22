# 08 — Build Decisions Log

Running record of decisions made *during* implementation (not the up-front
design Q&A — that's in `README.md`). Newest at the bottom of each phase. This is
the "why" companion to the code, so a later model/operator doesn't re-litigate
settled choices.

---

## Phase 1 — Engine callback seam (2026-06-22)

- **Additive-only refactor.** `download_exports()` gained `progress_cb=None` and
  `auth_cb=None`. With both `None` the function is byte-identical to before
  (verified: 3/3 fake parts download to valid zips, no behavior change). This
  keeps the CLI/TUI untouched while giving the manager structured progress.
- **Snapshot inside the lock, callback outside it.** `progress_cb` receives a
  `PartProgress` *copy* taken while holding the lock, but is *invoked* after
  releasing it. Prevents an observer from deadlocking or slowing the download
  pool.
- **Callback exceptions are swallowed** (logged at debug). A buggy observer must
  never break a download.
- **`auth_cb` fires exactly once**, guarded by the existing cross-thread
  `auth.set()` event, so only the first worker to hit a challenge notifies.

---

## Phase 2 — Manager service core (2026-06-22)

- **In-process worker thread, not subprocess per job.** The engine is a clean
  importable module, so the manager imports it and runs `download_exports` in a
  daemon thread (`manager/engine_bridge.py`). Simpler progress (direct callback)
  and one process to supervise. Subprocess isolation can come later if needed;
  noted as an open question in `02-manager-service.md`.
- **Isolated venv `.venv-manager/`.** The global anaconda env had
  `starlette 1.0.0`, which is incompatible with `fastapi 0.118` (needs
  `starlette <0.49`) — `FastAPI()` construction raised
  `Router.__init__() got an unexpected keyword argument 'on_startup'`. Pinned a
  working set in `manager/requirements.txt` (`starlette==0.48.0`). The venv is
  gitignored; deployment uses the requirements file. This also makes the
  manager's deps reproducible and decoupled from whatever the host Python has.
- **Derivation values live on `job.meta`.** `account_label`, `export_ts`,
  `export_raw` are merged into `job.meta` at job creation so the manifest (which
  reads identity from `job.meta`) records them. Single source of truth for the
  dated-folder identity.
- **Output dir derived once, at payload intake**, then frozen on the job. A
  re-capture/resume of the same export resolves to the same
  `<account>/<export-ts>/` dir and resumes partials; a genuinely new export
  (new timestamp) gets a new dir. Every derived path still passes through the
  engine's `validate_output_dir` allowlist — the manager never writes outside
  allowed roots.
- **Resume vs new-job routing** keys on the output dir: if a job for that exact
  dir is alive and waiting (`needs_cookie`/`downloading`/`paused`), a fresh
  payload is treated as a cookie refresh (`set_payload` wakes the runner);
  otherwise a new job starts.
- **SSE (not WebSocket) for progress.** One-way progress only; SSE is simpler
  and needs no extra deps. Matches the doc's call.
- **Two-token model present from the start.** `MANAGER_CAPTURE_TOKEN` (narrow:
  POST /api/payload) and `MANAGER_API_TOKEN` (control plane). In Phase 2 the
  control plane is read-only; tokens are enforced in Phase 4. Empty token =
  open surface (dev only), logged as a warning.
