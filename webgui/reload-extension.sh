#!/usr/bin/env bash
# Reload the Takeout helper extension inside the webtop over CDP and report
# any manifest/load errors. No browser interaction needed.
#   usage: ./webgui/reload-extension.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
docker cp "$HERE/cdp_reload_ext.py" takeout-webgui:/tmp/cdp_reload_ext.py
docker exec takeout-webgui python3 /tmp/cdp_reload_ext.py 127.0.0.1:9222
