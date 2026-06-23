#!/usr/bin/with-contenv bash
# Long-running manager service, supervised by the webtop's s6 init.
# NOTE: the `with-contenv` shebang is REQUIRED — s6-overlay runs custom
# services with a clean environment, so a plain `#!/bin/bash` would NOT see
# STORAGE_ROOT / MANAGER_* / TELEGRAM_* (they'd silently fall back to defaults
# and tokens would be empty). with-contenv imports the container env first.
# s6 restarts this script if it exits, giving us the "manager crash ->
# auto restart" behavior from docs/webgui/01-architecture.md (failure isolation).
#
# The manager runs from the bind-mounted /work source (not baked into the
# image), so a host `git pull` + container restart picks up new code.
set -e

# Wait for the bind mount to be present (compose mounts /work).
for _ in $(seq 1 30); do
    [ -f /work/manager/app.py ] && break
    sleep 1
done

cd /work
exec /opt/manager-venv/bin/python -m uvicorn manager.app:app \
    --host "${MANAGER_HOST:-127.0.0.1}" \
    --port "${MANAGER_PORT:-8080}"
