FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ripgrep jq build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
CMD ["python", "-c", "import time; time.sleep(3600)"]
