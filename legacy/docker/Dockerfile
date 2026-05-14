# Stage 1: builder
FROM python:3.12-slim AS builder
WORKDIR /build
COPY pyproject.toml .
RUN pip install --no-cache-dir ".[gchat,gemini]"

# Stage 2: runtime
FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir -e . --no-deps

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

CMD ["monkeybot", "serve", "--bot-dir", "/bot", "--host", "0.0.0.0", "--port", "8080"]
