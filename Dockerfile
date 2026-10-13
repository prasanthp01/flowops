FROM python:3.12.15-alpine3.24@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4
ARG FLOWOPS_SOURCE_REVISION=""
ENV FLOWOPS_SOURCE_REVISION=$FLOWOPS_SOURCE_REVISION
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN apk upgrade --no-cache && pip install --no-cache-dir --require-hashes -r requirements.txt && \
    python -m pip uninstall -y pip && rm -rf /usr/local/lib/python3.12/ensurepip && \
    addgroup -g 10001 -S app && adduser -u 10001 -S -G app app
COPY flowops flowops
COPY contracts-v1/openapi contracts-v1/openapi
COPY migrations migrations
COPY alembic.ini .
COPY scripts/grants.py scripts/grants.py
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/projects/flowops/health',timeout=3)"
CMD ["uvicorn", "flowops.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--limit-concurrency", "64"]
