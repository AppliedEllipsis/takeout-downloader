FROM python:3.12-slim

LABEL org.opencontainers.image.title="Google Takeout Downloader"
LABEL org.opencontainers.image.description="Self-contained downloader for Google Takeout archives. Includes the Textual TUI and the aria2c-driven CLI."

# aria2 is the high-speed download backend used by the CLI; the TUI auto-
# detects it on PATH. ca-certificates is needed for HTTPS to Google's
# download hosts.
RUN apt-get update && apt-get install -y --no-install-recommends \
        aria2 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

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
     takeout_cli_analyze.py ./

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
