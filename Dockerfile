FROM python:3.12-slim

LABEL org.opencontainers.image.title="Google Takeout Downloader (TUI)"
LABEL org.opencontainers.image.description="Self-contained TUI for downloading Google Takeout archives. Paste a JSON payload (from the browser extension) or a cURL command and go."

# aria2 is the optional high-speed download backend; the TUI auto-detects it
# on PATH. ca-certificates is needed for HTTPS to Google's download hosts.
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

# Copy the application
COPY takeout.py google_takeout_tui.py takeout_payload.py aria2c_integration.py dedupe_takeout.py ./

# Downloads land here; mounted as a volume from the host via compose.
RUN mkdir -p /downloads
ENV OUTPUT_DIR=/downloads \
    PYTHONUNBUFFERED=1 \
    TERM=xterm-256color

# The TUI is interactive — it needs a TTY. `docker run -it` or
# `docker compose run` provides one. There is no server and no exposed port.
ENTRYPOINT ["python", "takeout.py"]
