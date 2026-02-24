FROM python:3.12-slim

# Keep Python output unbuffered so logs appear immediately in docker logs
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Default DB path — override by mounting /config as a volume
ENV DB_PATH=/config/ddns.db

# Install curl — needed for HTMX download and the HEALTHCHECK probe
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Pre-create the config volume mount point so the container works without a volume attached
RUN mkdir -p /config/logs

# Install Python dependencies first so this layer is cached unless requirements.txt changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

# Download HTMX so it is served locally — no CDN dependency at runtime
RUN curl -fsSL https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js \
      -o /app/static/htmx.min.js

EXPOSE 8080

# Verify the app is responding before marking the container healthy
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Same image for dev and prod — no separate dev Dockerfile
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
