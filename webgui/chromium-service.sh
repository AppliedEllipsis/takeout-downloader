#!/usr/bin/with-contenv bash
# s6-supervised Chromium launcher for the Takeout webtop.
#
# Why a service instead of an XDG autostart .desktop: this image runs KDE
# Plasma, and ~/.config/autostart entries are not honored reliably on container
# boot. An s6 service is deterministic — it waits for the X display, clears any
# stale single-process lock left by a previous container, and launches Chromium
# as the desktop user (abc / PUID 1000). s6 restarts it if it exits, so a
# closed window or crash self-heals (matching the manager's supervision model).
#
# Runs the live launcher at /usr/local/bin/takeout-chromium, which init_custom.sh
# writes with the correct flags (CDP on 127.0.0.1:9222, extension, tabs).

PROFILE_DIR=/config/.chrome-profile

# Wait for the X server (display :1) to be accepting connections. selkies/KasmVNC
# brings it up a few seconds after container start.
for _ in $(seq 1 60); do
    [ -S /tmp/.X11-unix/X1 ] && break
    sleep 1
done

# Wait for the launcher script that init_custom.sh generates on first boot.
for _ in $(seq 1 30); do
    [ -x /usr/local/bin/takeout-chromium ] && break
    sleep 1
done

# Clear a stale SingletonLock from a previous container. Chromium writes this
# symlink as <host>-<pid>; after a container recreate that pid is gone but the
# lock remains and blocks every new launch. Safe to remove when no chromium runs.
if ! pgrep -x chromium >/dev/null 2>&1; then
    rm -f "$PROFILE_DIR/SingletonLock" \
          "$PROFILE_DIR/SingletonCookie" \
          "$PROFILE_DIR/SingletonSocket" 2>/dev/null || true
fi

# Give Plasma a moment to finish bringing up the panel/compositor.
sleep 3

# Launch as the desktop user with the session display. exec so s6 supervises
# the real chromium process and restarts it on exit.
exec s6-setuidgid abc env DISPLAY=:1 HOME=/config /usr/local/bin/takeout-chromium
