# Minimal runtime image — extend for production (see BACKLOG.md).
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    PYTHONPATH=/app/src

RUN pip install --no-cache-dir --upgrade pip

COPY pyproject.toml README.md ./
COPY src ./src
COPY .agents ./.agents
COPY config ./config

RUN pip install --no-cache-dir ".[gemini]"

EXPOSE 8080

CMD ["python", "-m", "monkeybot.gateway.main"]
