# Server CORE deployment report

**UTC:** 2026-10-13T22:00:42Z
**Approved SHA (resolved in canonical repo):** `21495d9ca4551b614d658178fb299854c2050bdb`
**Deployed SHA:** same

## Scope and deployment

No local source edit, commit, reset, stash, force push, symlink/profile/extension reload, browser/container/rclone restart, watcher/download/mint/password/unlock/canary, `run_once`, `watch`, or CDP call was made. The report file itself is the requested evidence artifact.

Sanitized commands/results:

```sh
cd C:/Users/User/_projects/takeout-downloader-reliability
 git rev-parse --verify 21495d9^{commit}
# 21495d9ca4551b614d658178fb299854c2050bdb

ssh -o BatchMode=yes -o ConnectTimeout=10 takeout-server
cd /opt/local_cache_crypt/_projects/takeout-downloader/.v3
 git fetch --no-tags origin 21495d9ca4551b614d658178fb299854c2050bdb
 git merge --ff-only 21495d9ca4551b614d658178fb299854c2050bdb
# fetched exact SHA; Fast-forward 1b70ce2..21495d9
```

The deployment worktree was clean at `1b70ce2d80e2180a2b5eac7024e9fdecbe3df4fd`, detached before and detached after (existing state preserved), and is clean at the approved SHA after deployment. `.v3` mode/owner remained `775 ellipsis:ellipsis`.

## Dirty-main preservation

Before and after, `/opt/local_cache_crypt/_projects/takeout-downloader` remained at `c0e3b8d32cf54c72c76850cbaaa944d26ac46e3b`, branch `feat/internal-downloader`, with exactly:

```text
 M helpers/background.js
 M helpers/options.js
 M helpers/overlay.js
 M helpers/popup.js
?? .v3/
?? helpers.bak-1790097405/
```

No main worktree file was changed.

## Runtime verification

Container `takeout-webgui`, working directory `/work/.v3`:

```text
autopilot=/work/.v3/autopilot/__init__.py
takeout2=/work/.v3/takeout2/__init__.py
```

`RunConfig` fields imported from `autopilot.run`: `archive_id, ledger_path, staging_dir, archive_dir, account, cdp_http, require_mount, max_parts, verify_hash, settle, work_url, mint_only, min_headroom, move_timeout, move_stall, clean_staging, archive_remote, rclone_binary, rclone_config, remote_timeout, staging_reserve, cached_transfer_only`.

Help-only checks passed without running work:

```text
python -m autopilot --help       => {run,watch,report}
python -m autopilot run --help   => usage ... autopilot run ...
python -m autopilot watch --help => usage ... autopilot watch ...
```

LF-normalized Git-blob-vs-file check over all tracked `autopilot/**/*.py` and `takeout2/**/*.py`: **36 checked, 0 mismatches**. (Git blob SHA-1 was computed as `sha1("blob <len>\\0" + LF-normalized bytes)` and compared with `git rev-parse HEAD:path`.)

## Worker state / blockers

Final bounded process check: no `autopilot/(watch|run)`, manager orchestrator/jobs, or takeout2 worker matched. An earlier probe saw an existing `monitor_job.py` display observer; it was absent from the final checks and was not started/stopped. Existing Chromium/rclone services were left untouched. Executor/unit installation remains deferred pending the final gate; no deployment blocker remains for this CORE-only fast-forward.
