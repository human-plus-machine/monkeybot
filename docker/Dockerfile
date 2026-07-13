# Minimal runtime image — extend for production (see BACKLOG.md).
# Config/templates come from monkeybot-cli scaffold_defaults via ``monkeybot new`` at build time.
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PORT=8080

RUN pip install --no-cache-dir --upgrade pip

COPY pyproject.toml README.md ./
COPY src ./src
COPY cli/pyproject.toml ./cli/pyproject.toml
COPY cli/src ./cli/src

ARG EXTRAS=gemini
RUN pip install --no-cache-dir ".[$EXTRAS]" \
    && pip install --no-cache-dir ./cli \
    && python -c "from pathlib import Path; from monkeybot_cli.scaffold import run_new; run_new(dest=Path('/app'), force=True)" \
    && mkdir -p /app/data /app/workspace/skills /app/memory

# Matches scaffolded monkeybot.yaml paths.skills_path (./workspace/skills).
ENV SKILLS_PATH=/app/workspace/skills

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"

EXPOSE 8080

CMD ["python", "-m", "monkeybot.gateway.main"]
