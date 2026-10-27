# =============================================================================
# Production Dockerfile for the AI Voice Assistant
# Multi-stage / minimal footprint with unprivileged appuser & SQLite WAL volume
# =============================================================================

FROM python:3.12-slim AS runtime

# Install essential system dependencies and curl for container health checks
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create unprivileged system user
RUN groupadd -r appgroup && useradd -r -g appgroup -u 1000 -m appuser

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY agent/ ./agent/
COPY frontend/ ./frontend/
COPY scripts/ ./scripts/
COPY tests/ ./tests/
COPY pytest.ini .
COPY .env.example .

# Create persistent data directory and grant permissions to appuser
RUN mkdir -p /app/data && chown -R appuser:appgroup /app

USER appuser

# Expose HTTP API & Frontend Port
EXPOSE 5500

# Container Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:5500/health || exit 1

# Default command starts the production HTTP server with isolated API and static UI
CMD ["python", "frontend/serve.py"]
