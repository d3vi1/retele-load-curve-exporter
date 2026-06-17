FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV EXPORTER_HOST=0.0.0.0
ENV EXPORTER_PORT=9831
EXPOSE 9831

CMD ["python", "-m", "dso_load_curves_exporter"]
