# syntax=docker/dockerfile:1.7
# =============================================================================
# Dockerfile — DynamoDB Checkpointer worked example
# -----------------------------------------------------------------------------
# Mirrors ``Dockerfile.extension-template`` at the monkey-bot repo root, with
# the DynamoDB extension package pre-installed into the image. Use this as a
# copy-paste starting point; in a production build you would:
#
#   1. Pin monkey-bot, aioboto3, and emonk-ext-dynamodb-ckpt into a hashed
#      ``requirements.lock.txt`` (generated with ``uv pip compile --generate-hashes``).
#   2. Replace the COPY of ``./`` with a COPY of your agent code + ``harness.yaml``.
#   3. Point IAM at the DynamoDB table via the AgentCore / ECS task role.
# =============================================================================

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HARNESS_PLUGINS_FROM_ENTRY_POINTS=1 \
    HARNESS_CONFIG=/app/harness.yaml

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- Dependency install layer -------------------------------------------------
# Replace this with ``COPY requirements.lock.txt . && pip install --require-hashes``
# in your production build. The inline install is shown here for example clarity.
COPY pyproject.toml ./pyproject.toml
COPY src/ ./src/
RUN pip install --no-cache-dir "emonk>=1.0" "aioboto3>=12.0" \
 && pip install --no-cache-dir .

# --- Application layer --------------------------------------------------------
# Your agent code + HarnessConfig go here. The README.md walks through a
# minimal ``app/main.py`` that wires the DynamoDB Checkpointer via entry-point
# resolution.

RUN useradd --create-home --shell /bin/bash emonk \
 && chown -R emonk:emonk /app
USER emonk

EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app.main"]
