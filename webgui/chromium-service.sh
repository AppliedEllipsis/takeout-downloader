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

# Give Plasma a moment to finish bringing up the panel/compositor.
sleep 3

# Supervision LOOP (not exec). This is the critical fix for the tab-pile bug:
#
#   The old version did `exec chromium URL1 URL2`. When a chromium already owns
#   the persistent profile, a second invocation does NOT start a new browser —
#   it hands its URLs to the running instance and EXITS 0. s6 then saw the
#   service "exit", restarted it, and it opened the two tabs AGAIN. Repeat
#   forever => hundreds of Takeout tabs.
#
#   Instead we loop here and the script itself stays alive (s6 sees one
#   long-running, healthy service). We launch chromium ONLY when none is
#   running, and only then clear the stale single-process locks. A real crash
#   (chromium gone) is detected on the next tick and relaunched — self-healing
#   without ever delegating-and-piling.
while true; do
    if ! pgrep -x chromium >/dev/null 2>&1; then
        # No chromium alive: safe to clear a stale lock from a dead instance
        # (after a container recreate the pid in SingletonLock is gone).
        rm -f "$PROFILE_DIR/SingletonLock" \
              "$PROFILE_DIR/SingletonCookie" \
              "$PROFILE_DIR/SingletonSocket" 2>/dev/null || true
        # Launch in the background; the loop (not s6) owns the lifecycle.
        s6-setuidgid abc env DISPLAY=:1 HOME=/config \
            /usr/local/bin/takeout-chromium >/dev/null 2>&1 &
    fi
    sleep 10
done
