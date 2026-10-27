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

# Pre-download model files (Silero VAD etc.) at build time so a cold start
# doesn't have to fetch them before the agent can join a room.
RUN python -m agent.main download-files

# Expose HTTP API & Frontend Port (used when running frontend/serve.py)
EXPOSE 5500

# Default command runs the voice agent worker. This is what LiveKit Cloud
# (`lk agent create` / `lk agent deploy`) builds and runs. The web server is
# started with its own command instead: `python frontend/serve.py`
# (Render start command, or `command:` in docker-compose.yml).
CMD ["python", "-m", "agent.main", "start"]