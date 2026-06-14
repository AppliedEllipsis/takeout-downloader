FROM python:3.12-slim

LABEL org.opencontainers.image.title="Google Takeout Downloader"
LABEL org.opencontainers.image.description="Self-contained downloader for Google Takeout archives. Includes the Textual TUI and the aria2c-driven CLI."

# aria2 is the high-speed download backend used by the CLI; the TUI auto-
# detects it on PATH. ca-certificates is needed for HTTPS to Google's
# download hosts. curl is used below to fetch cloudflared.
RUN apt-get update && apt-get install -y --no-install-recommends \
        aria2 \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# cloudflared powers the OPTIONAL ephemeral paste relay (`takeout_cli.py
# --relay --tunnel`). It dials OUTBOUND to Cloudflare's edge, so no inbound
# port mapping is needed — it works from inside the container as-is. The
# relay binds to 127.0.0.1 and is single-use + short-TTL; cloudflared just
# bridges a *.trycloudflare.com URL to it. Pinned to the static linux build.
# If the download fails at build time, the relay still works locally (and
# CLOUDFLARED_BIN can point at a host-mounted binary instead).
ARG TARGETARCH=amd64
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) cf_arch=amd64 ;; \
      arm64) cf_arch=arm64 ;; \
      *) cf_arch=amd64 ;; \
    esac; \
    curl -fsSL -o /usr/local/bin/cloudflared \
      "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${cf_arch}" \
      && chmod +x /usr/local/bin/cloudflared \
      || echo "cloudflared download failed; --tunnel will be unavailable inside the image"

WORKDIR /app

# Install Python deps first for layer caching. Drop PyInstaller — that's a
# build-host concern, not a runtime one.
COPY requirements.txt .
RUN grep -v -i pyinstaller requirements.txt > /tmp/req.txt \
    && pip install --no-cache-dir -r /tmp/req.txt

# Copy the application. Both the TUI and the CLI live in the same image;
# the entrypoint here defaults to the TUI but `takeout-cli` service overrides
# `command` in docker-compose.yml to launch `takeout_cli.py` instead.
COPY takeout.py \
     google_takeout_tui.py \
     takeout_payload.py \
     aria2c_integration.py \
     dedupe_takeout.py \
     takeout_cli.py \
     takeout_cli_analyze.py \
     paste_server.py ./

# Downloads land here; mounted as a volume from the host via compose.
RUN mkdir -p /downloads
ENV OUTPUT_DIR=/downloads \
    PYTHONUNBUFFERED=1 \
    TERM=xterm-256color

# Default entrypoint is the TUI; the `takeout-cli` service overrides this
# with `command: ["python", "-u", "takeout_cli.py"]`. Both services need a
# TTY (`docker compose run` provides one). There is no server and no
# exposed port.
ENTRYPOINT ["python"]
