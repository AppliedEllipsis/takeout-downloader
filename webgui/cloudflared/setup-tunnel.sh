#!/bin/bash
# Cloudflare tunnel setup — PORTAL ONLY.
#
# Creates a named tunnel and wires DNS so ONE hostname maps to the KasmVNC
# portal (:3000). The manager (:8080) and Chromium CDP (:9222) are NEVER given
# an ingress rule, so they are unreachable from the internet even if someone
# guesses the hostname. See docs/webgui/05-deployment.md ("Cloudflare tunnel").
#
# Run this ONCE on the server (interactive: it opens a browser-login URL).
# After it finishes, `docker compose -f docker-compose.webgui.yml up -d` runs
# the tunnel via the cloudflared service using the generated credentials.
#
# Prereqs: cloudflared installed on the host (or run via the image), and a
# Cloudflare account with the zone for your domain.
set -euo pipefail

TUNNEL_NAME="${TUNNEL_NAME:-takeout-portal}"
HOSTNAME="${TUNNEL_HOSTNAME:-}"
CONFIG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -z "$HOSTNAME" ]; then
    echo "Set TUNNEL_HOSTNAME=takeout.yourdomain.com first." >&2
    exit 1
fi

echo "==> 1. Login (opens a browser URL; pick the zone for $HOSTNAME)"
cloudflared tunnel login

echo "==> 2. Create tunnel '$TUNNEL_NAME' (idempotent)"
if cloudflared tunnel list | grep -q "[[:space:]]${TUNNEL_NAME}[[:space:]]"; then
    echo "    tunnel already exists, reusing"
else
    cloudflared tunnel create "$TUNNEL_NAME"
fi

TUNNEL_ID="$(cloudflared tunnel list | awk -v n="$TUNNEL_NAME" '$2==n {print $1}')"
echo "    tunnel id: $TUNNEL_ID"

echo "==> 3. Route DNS $HOSTNAME -> tunnel"
cloudflared tunnel route dns "$TUNNEL_NAME" "$HOSTNAME"

echo "==> 4. Copy credentials next to config.yml"
CRED_SRC="$HOME/.cloudflared/${TUNNEL_ID}.json"
cp "$CRED_SRC" "$CONFIG_DIR/${TUNNEL_ID}.json"

echo "==> 5. Write config.yml from template values"
sed -e "s|<TUNNEL_ID>|${TUNNEL_ID}|g" \
    -e "s|takeout.example.com|${HOSTNAME}|g" \
    "$CONFIG_DIR/config.yml" > "$CONFIG_DIR/config.generated.yml"

cat <<EOF

Done. Next:
  1. Review $CONFIG_DIR/config.generated.yml (portal-only ingress).
  2. mv config.generated.yml config.yml   (or keep both; compose mounts the dir)
  3. Put a Cloudflare Access policy in front of $HOSTNAME (your email only).
  4. docker compose -f docker-compose.webgui.yml up -d
  5. Run webgui/cloudflared/verify-exposure.sh to confirm 8080/9222 are NOT
     reachable through the tunnel.

NEVER add an ingress rule for :8080 or :9222. Those stay SSH-only.
EOF
