FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEEPEVAL_NO_INSPECT_PROMPT=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py models.py store.py runner.py scorer.py ws_manager.py test_judge.py ./
COPY assertions.py persistence.py report.py scenario_loader.py ./
COPY eval_config.yaml ./
COPY scenarios ./scenarios
COPY suites ./suites

EXPOSE 8001

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
