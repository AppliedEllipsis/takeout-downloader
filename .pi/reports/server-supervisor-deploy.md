# Server supervisor deployment

UTC: 2026-10-14T03:05:31Z

Approved/deployed: `7fc3ac97ef5db6f335cd70a05d2d6b77b3c43229`.
Baseline: `21495d9ca4551b614d658178fb299854c2050bdb`.

Exact sanitized deployment commands (remote Git commands executed via bounded Python subprocesses):

```sh
git -C /c/Users/User/_projects/takeout-downloader-reliability rev-parse --verify '7fc3ac9^{commit}'
timeout 150s ssh -o BatchMode=yes -o ConnectTimeout=10 takeout-server python3 -B -
git -C /opt/local_cache_crypt/_projects/takeout-downloader/.v3 fetch --no-tags origin 7fc3ac97ef5db6f335cd70a05d2d6b77b3c43229
git -C /opt/local_cache_crypt/_projects/takeout-downloader/.v3 merge --ff-only 7fc3ac97ef5db6f335cd70a05d2d6b77b3c43229
```

Pre/post: clean detached checkout; ancestry verified; existing file/directory modes and ownership unchanged; `.v3` remains `775`, UID:GID `1001:1001`.

Verification used `timeout 100s ssh -o BatchMode=yes -o ConnectTimeout=10 takeout-server python3 -B -` and:

```sh
docker exec -i --workdir /work/.v3 takeout-webgui /opt/manager-venv/bin/python -I -B /work/.v3/tools/container_watch.py --help
systemctl show takeout-autopilot-watch.service -p LoadState -p ActiveState -p UnitFileState
```

Both packages import from `/work/.v3`; all 22 RunConfig fields match. Supervisor source and mount-admission functions exist; offline safe/unsafe mount checks pass. All 37 Python files match approved Git blobs exactly; SHA256 manifest: `9e13dcd4a59b78c436d091393bdaa0f0e58d2fb087b76483443d44a31a40e1ac`.

Main’s six dirty entries, four modified files, backup-tree bytes/permissions, index, HEAD and branch remain identical through final verification. Snapshot: `fd6ab76b83565b5cdac01d0a90799c56c55d75e828f04129e7f99915f1d19118`.

Unit absent/inactive; no host/container watch/run/takeout2 worker. Existing display observer untouched. Initial verification quoting error corrected without file writes.

No deployment blocker. Activation remains owner-deferred: rclone unlock and Google sign-in. No credentials/configuration, installation, service actions, restarts, container recreation, backend operations, mint/download, archive/ledger changes, local source edits, commits or pushes.
