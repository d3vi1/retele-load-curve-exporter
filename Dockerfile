FROM python:3.12-slim@sha256:d764629ce0ddd8c71fd371e9901efb324a95789d2315a47db7e4d27e78f1b0e9

WORKDIR /app
COPY pyproject.toml README.md constraints.txt ./
COPY src ./src
RUN pip install --no-cache-dir -c constraints.txt .
RUN addgroup --system exporter && adduser --system --ingroup exporter --home /nonexistent --no-create-home exporter

ENV EXPORTER_HOST=0.0.0.0
ENV EXPORTER_PORT=9831
EXPOSE 9831
USER exporter
HEALTHCHECK --interval=60s --timeout=10s --start-period=45s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"EXPORTER_PORT\", \"9831\")}/healthz', timeout=5).read()"

CMD ["python", "-m", "dso_load_curves_exporter"]
