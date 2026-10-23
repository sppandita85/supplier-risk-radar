# Supplier Risk Radar: dashboard, pipeline jobs and chat in one image.
#   docker build .                 -> runtime image (default, last stage)
#   docker build --target test .   -> same code + pytest/ruff, used by CI

# ---- base: dependencies + application code --------------------------------------------------------
FROM python:3.12-slim AS base
LABEL org.opencontainers.image.source="https://github.com/sppandita85/supplier-risk-radar" \
      org.opencontainers.image.description="Supplier Risk Radar: local-LLM supplier risk signals, correlation, alerts, chat and dashboards"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PG_DSN="host=db dbname=risk_radar user=radar password=radar" \
    OLLAMA_URL=http://host.docker.internal:11434 \
    OLLAMA_MODEL=llama3.1:8b \
    PHOENIX_COLLECTOR_ENDPOINT=http://host.docker.internal:6006 \
    PHOENIX_PROJECT=supplier-risk-radar

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY config ./config
COPY db ./db
COPY radar ./radar
COPY dashboard ./dashboard
COPY scripts ./scripts
COPY .streamlit ./.streamlit
COPY docker-entrypoint.sh README.md ./
RUN mkdir -p data/input data/processed data/failed \
 && useradd --create-home --uid 1000 radar && chown -R radar /app && chmod +x docker-entrypoint.sh

# ---- test: CI runs lint + pytest inside this image --------------------------------------------------
FROM base AS test
COPY requirements-dev.txt pyproject.toml ./
RUN pip install -r requirements-dev.txt
COPY tests ./tests
RUN mkdir -p /app/reports && chown -R radar /app
USER radar
ENV TRACING=off
CMD ["python", "-m", "pytest", "-q"]

# ---- runtime: what gets shipped --------------------------------------------------------------------
FROM base AS runtime
USER radar
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')"

ENTRYPOINT ["./docker-entrypoint.sh"]
# Default: the dashboard. Other commands: python -m radar.synth | radar.ingest | radar.correlate_llm | radar.watch
CMD ["streamlit", "run", "dashboard/app.py", "--server.port=8501", "--server.address=0.0.0.0"]
