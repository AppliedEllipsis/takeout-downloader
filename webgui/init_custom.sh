#!/usr/bin/with-contenv bash
# First-boot setup for the Takeout webtop, run once by the s6 init as root.
# with-contenv shebang: s6-overlay runs custom init/services with a CLEAN env;
# this wrapper injects the container environment (STORAGE_ROOT, MANAGER_*,
# TELEGRAM_*) so the manager + profile seeding see the compose-provided values.
# Idempotent: every block guards on a marker so a container restart is cheap.
#
# Responsibilities:
#   1. Chromium launcher script + autostart (CDP on, extension loaded, tabs).
#   2. Seed a persistent Chrome profile with bookmarks (Takeout + manager).
#   3. Drop a Chrome managed-storage policy so the extension gets its
#      managerUrl + captureToken without anyone pasting it by hand.
#
# Design: docs/webgui/05-deployment.md ("Chromium launch flags",
# "Profile seeding"). All paths under /config persist across restarts.
set -e
echo "**** Takeout webtop custom init starting ****"

PROFILE_DIR=/config/.chrome-profile
SEED=/work/webgui/profile-seed
MANAGER_URL="${MANAGER_PUBLIC_URL:-http://127.0.0.1:8080}"
CAPTURE_TOKEN="${MANAGER_CAPTURE_TOKEN:-}"

mkdir -p "$PROFILE_DIR/Default" /config/Desktop /usr/local/bin

# --- 1. Chromium launcher ----------------------------------------------------
# A wrapper so the same flags are used by autostart and any manual relaunch.
cat > /usr/local/bin/takeout-chromium <<EOF
#!/bin/bash
# Resolve the chromium binary (deb package name varies).
CHROME=\$(command -v chromium-browser || command -v chromium || echo chromium)
# Clear a stale SingletonLock left by a previous container instance. The lock
# is a symlink "<host>-<pid>"; after a container recreate that pid is gone but
# the link persists and blocks every launch. Remove it if no chromium is live.
if [ -e $PROFILE_DIR/SingletonLock ] && ! pgrep -x chromium >/dev/null 2>&1; then
  rm -f $PROFILE_DIR/SingletonLock $PROFILE_DIR/SingletonCookie $PROFILE_DIR/SingletonSocket
fi
exec "\$CHROME" \\
  --user-data-dir=$PROFILE_DIR \\
  --remote-debugging-address=127.0.0.1 \\
  --remote-debugging-port=9222 \\
  --load-extension=/work/helpers \\
  --no-first-run \\
  --no-default-browser-check \\
  --restore-last-session \\
  --disable-features=TranslateUI \\
  --start-maximized \\
  https://takeout.google.com/ "$MANAGER_URL/" "\$@"
EOF
chmod +x /usr/local/bin/takeout-chromium

# Autostart entry for the KDE/openbox session the webtop runs.
mkdir -p /config/.config/autostart
if [ ! -f /config/.config/autostart/takeout-chromium.desktop ]; then
cat > /config/.config/autostart/takeout-chromium.desktop <<EOF
[Desktop Entry]
Type=Application
Name=Takeout Browser
Exec=/usr/local/bin/takeout-chromium
X-GNOME-Autostart-enabled=true
Terminal=false
EOF
fi

# Desktop shortcut for a manual relaunch if the user closes the window.
if [ ! -f /config/Desktop/Takeout-Browser.desktop ]; then
cat > /config/Desktop/Takeout-Browser.desktop <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Takeout Browser
Comment=Chromium logged into Google + Takeout manager
Exec=/usr/local/bin/takeout-chromium
Icon=chromium
Terminal=false
Categories=Network;
EOF
fi

# --- 2. Bookmarks (only if the profile has none yet) -------------------------
# Chrome reads Default/Bookmarks (JSON). We seed it once; after that the user's
# own edits win and we never overwrite.
if [ ! -f "$PROFILE_DIR/Default/Bookmarks" ] && [ -f "$SEED/Bookmarks" ]; then
    echo "Seeding bookmarks"
    cp "$SEED/Bookmarks" "$PROFILE_DIR/Default/Bookmarks"
fi

# --- 3. Managed-storage policy for the extension -----------------------------
# Chrome reads managed extension storage from a policy file. This injects the
# manager URL + capture token into the extension's settings on startup so the
# operator never types the token. The extension ID is derived from the unpacked
# key; for an unpacked load Chrome assigns a stable ID from the path, so we use
# a wildcard 3rd-party policy directory the extension reads via chrome.storage.
# (Falls back gracefully: if the policy isn't picked up, the popup options still
#  let you set these by hand.)
POLICY_DIR=/etc/chromium/policies/managed
mkdir -p "$POLICY_DIR"
cat > "$POLICY_DIR/takeout-manager.json" <<EOF
{
  "3rdparty": {
    "extensions": {
      "dgbbpdjpfeeaiheekoclkkkbipkikejl": {
        "managerUrl": "$MANAGER_URL",
        "captureToken": "$CAPTURE_TOKEN",
        "autoPost": true,
        "autoRecapture": true
      }
    }
  }
}
EOF

chown -R 1000:1000 /config/Desktop /config/.config "$PROFILE_DIR" 2>/dev/null || true
chmod +x /config/Desktop/*.desktop 2>/dev/null || true

echo "**** Takeout webtop custom init complete ****"
