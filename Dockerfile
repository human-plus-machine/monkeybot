# =============================================================================
# Stage 1: Build stage (install dependencies)
# =============================================================================
FROM python:3.12-slim AS builder
ARG EMONK_EXTRAS=""

WORKDIR /app

# Install system dependencies for building Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Optional extras (pass --build-arg EMONK_EXTRAS="council voice" to enable)
RUN if echo "$EMONK_EXTRAS" | grep -q "voice"; then \
      pip install --no-cache-dir --user "google-cloud-speech>=2.27.0" "google-cloud-texttospeech>=2.17.0"; \
    fi && \
    if echo "$EMONK_EXTRAS" | grep -q "modal"; then \
      pip install --no-cache-dir --user "modal>=0.60.0"; \
    fi && \
    if echo "$EMONK_EXTRAS" | grep -q "council"; then \
      pip install --no-cache-dir --user "google-cloud-storage>=2.18.0"; \
    fi

# =============================================================================
# Stage 2: Runtime stage (minimal image)
# =============================================================================
FROM python:3.12-slim

WORKDIR /app

# Copy installed packages from builder stage
COPY --from=builder /root/.local /root/.local

# Add Python user site-packages to PATH
ENV PATH=/root/.local/bin:$PATH

# Copy application code
COPY src/ src/
COPY skills/ skills/
# Copy identity/config files (SOUL.md, IDENTITY.md, HEARTBEAT.md, etc.)
COPY *.md ./

# Create memory directory (will be overridden by volume in Cloud Run)
RUN mkdir -p /app/data/memory
RUN mkdir -p /app/data/memory/raw \
             /app/data/memory/raw/processed \
             /app/data/memory/episodic \
             /app/data/memory/semantic \
             /app/data/memory/procedural \
             /app/data/memory/working

# Set environment variables for Cloud Run
ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    MEMORY_DIR=/app/data/memory \
    SKILLS_DIR=/app/skills \
    SOUL_FILE=/app/SOUL.md \
    IDENTITY_FILE=/app/IDENTITY.md \
    HEARTBEAT_MD_PATH=/app/HEARTBEAT.md

# Health check (Cloud Run uses this for readiness/liveness)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health').read()"

# Expose port (Cloud Run injects PORT env var)
EXPOSE 8080

# Run application
CMD ["python", "-m", "src.main"]
