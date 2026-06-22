#!/bin/bash
# Negative + positive exposure test for the Cloudflare tunnel.
#
# PASS criteria (the Phase 7 gate):
#   - portal hostname responds (Access challenge or 200)   [positive]
#   - the SAME hostname does NOT serve manager/CDP content  [negative]
#   - locally, 8080 + 9222 ARE reachable (SSH-forward path) [positive]
#
# This does not need the tunnel creds; it just probes URLs. Run it from the
# server (for the local checks) and optionally from your laptop (for the
# public hostname check). See docs/webgui/05-deployment.md.
set -uo pipefail

HOSTNAME="${TUNNEL_HOSTNAME:-}"
FAIL=0

note() { printf '%s\n' "$*"; }
ok()   { printf '[OK] %s\n' "$*"; }
bad()  { printf '[FAIL] %s\n' "$*"; FAIL=1; }

# --- local control plane (should be up on the box) --------------------------
note "== local checks (run on the server) =="
if curl -fsS --max-time 5 http://127.0.0.1:8080/api/control/health >/dev/null 2>&1; then
    ok "manager :8080 reachable locally (expected)"
else
    note "(manager :8080 not up locally — start the stack first)"
fi
if curl -fsS --max-time 5 http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
    ok "Chromium CDP :9222 reachable locally (expected)"
else
    note "(CDP :9222 not up locally — start the stack first)"
fi

# --- public hostname (the tunnel) -------------------------------------------
if [ -z "$HOSTNAME" ]; then
    note ""
    note "Set TUNNEL_HOSTNAME to also run the public-exposure checks."
    exit $FAIL
fi

note ""
note "== public checks (hostname: $HOSTNAME) =="

# Positive: portal responds at all (200, or a Cloudflare Access 302/403).
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://$HOSTNAME/" || echo 000)"
if [ "$code" != "000" ]; then
    ok "portal hostname responds (HTTP $code — Access challenge or portal)"
else
    bad "portal hostname did not respond"
fi

# Negative: the manager API must NOT be served on the public hostname. If the
# tunnel were misconfigured to forward :8080, this path would return manager
# JSON. We want a 404 / Access page / anything that is NOT our health JSON.
body="$(curl -s --max-time 10 "https://$HOSTNAME/api/control/health" || true)"
if printf '%s' "$body" | grep -q '"storage_root"'; then
    bad "manager health JSON is reachable via the tunnel — INGRESS MISCONFIGURED"
else
    ok "manager API not served on the public hostname (expected)"
fi

# Negative: CDP must not be reachable publicly.
body="$(curl -s --max-time 10 "https://$HOSTNAME/json/version" || true)"
if printf '%s' "$body" | grep -qi '"webSocketDebuggerUrl"\|"Browser"'; then
    bad "Chromium CDP reachable via the tunnel — INGRESS MISCONFIGURED"
else
    ok "CDP not served on the public hostname (expected)"
fi

note ""
if [ "$FAIL" -eq 0 ]; then
    ok "Phase 7 exposure gate PASSED"
else
    bad "Phase 7 exposure gate FAILED — fix ingress before going live"
fi
exit $FAIL
