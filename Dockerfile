# Social-Media-Metrices — dashboard + pipeline image
#
# Code only. Data (DuckDB / parquet), reports and secrets are NOT baked in:
#   - data/, UI/data/, reports/, logs/  -> bind-mounted by docker-compose.yml
#   - .env                               -> passed with env_file (never copied into the image)
#
# Build:  docker compose build
# Run:    docker compose up -d        (dashboard on http://localhost:8879/)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so code edits do not invalidate this layer.
# requirements.txt = core + ontology/NLP libs (umap, hdbscan, ...). torch /
# sentence-transformers / transformers are intentionally NOT installed (GPU/NLP
# stages 04, 10, 12-14 only); add them in a derived image if you run those here.
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt statsmodels pyyaml requests openpyxl

COPY . /app

# Writable dirs for scripts that log or stage output (also bind-mounted at run time).
RUN mkdir -p /app/logs /app/data /app/reports /app/UI/data /app/UI/.tmp \
    && chmod -R a+rwX /app/logs && chmod 1777 /app/UI/.tmp
# /app/UI/.tmp: DuckDB's spill directory (relative to the server's cwd). Under the compose
# memory cap, big drill-down queries spill here instead of exhausting RAM.

EXPOSE 8877
WORKDIR /app/UI

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8877/', timeout=4).status == 200 else 1)"

# Serves the dashboard + the read-only click-to-inspect API.
CMD ["python", "api_server.py", "8877"]
