FROM python:3.11-slim

LABEL maintainer="Google Takeout Downloader"
LABEL description="Web interface for downloading Google Takeout archives with aria2c support"

# Set working directory
WORKDIR /app

# Install curl for healthcheck + aria2c for download backend
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    aria2 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY takeout.py .
COPY google_takeout_web.py .
COPY aria2c_integration.py .
COPY google_takeout_tui.py .
COPY dedupe_takeout.py .

# Create downloads directory
RUN mkdir -p /downloads

# Copy .env.example as reference (users should mount their own .env)
COPY .env.example .env.example

# Environment variables (override via docker-compose or .env)
ENV OUTPUT_DIR=/downloads
ENV PARALLEL_DOWNLOADS=6
ENV FILE_COUNT=100
ENV ARIA2C_ENABLED=false
ENV ARIA2C_RPC_URL=http://localhost:6800/jsonrpc
ENV PYTHONUNBUFFERED=1

# Expose ports
# 5000 = web UI, 6800 = aria2c RPC (if auto-started)
EXPOSE 5000 6800

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5000/api/status || exit 1

# Run the web server
CMD ["python", "google_takeout_web.py", "--host", "0.0.0.0", "--port", "5000"]
