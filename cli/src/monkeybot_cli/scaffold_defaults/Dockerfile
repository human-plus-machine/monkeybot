FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /agent

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

ARG INSTALL_CHROMIUM=0
USER root
RUN if [ "$INSTALL_CHROMIUM" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends chromium \
      && rm -rf /var/lib/apt/lists/*; \
    fi

COPY monkeybot_config ./monkeybot_config
COPY skills ./skills
RUN useradd --create-home agent \
    && mkdir -p workspace data memory \
    && chown -R agent:agent workspace data memory

USER agent
ENV PATH="/agent/.venv/bin:$PATH" PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"
CMD ["python", "-m", "monkeybot.gateway.main"]
