# DSO Load Curve Exporter

Python libraries for Romanian distribution operator portals plus a Prometheus
exporter. The repository is public-safe: examples use anonymized PODs and no
credentials.

## Packages

- `dso_retele_electrice`: Rețele Electrice client for POD discovery, POD
  metadata, meter readings, and load-curve interval energy.
- `dso_electrica_deer`: deferred DEER/Electrica adapter stub with the same
  public contract.
- `dso_load_curves_exporter`: Prometheus exporter and scheduler.

## Local Run

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e .

export RETELE_ELECTRICE_ACCOUNTS=main
export RETELE_ELECTRICE_RUNTIME=http
export RETELE_ELECTRICE_MAIN_USERNAME='user@example.com'
export RETELE_ELECTRICE_MAIN_PASSWORD='secret'
export RETELE_ELECTRICE_ONLY_PODS='RO001EXXXXXXXXX,RO001EYYYYYYYYY'
python -m dso_load_curves_exporter --host 0.0.0.0 --port 9831
```

The exporter defaults to the HTTP runtime. The browser runtime is an optional
fallback for local diagnostics:

```sh
pip install -e '.[browser]'
python -m playwright install chromium
export RETELE_ELECTRICE_RUNTIME=browser
```

Scrape:

```sh
curl http://localhost:9831/metrics
```

## Docker

```sh
docker build -t dso-load-curve-exporter:local .
docker run --rm -p 9831:9831 \
  -e RETELE_ELECTRICE_ACCOUNTS=main \
  -e RETELE_ELECTRICE_MAIN_USERNAME=user@example.com \
  -e RETELE_ELECTRICE_MAIN_PASSWORD=secret \
  dso-load-curve-exporter:local
```

## Observability

Prometheus verification queries:

```promql
up{job="dso-load-curve-exporter"}
dso_exporter_fetch_success
count(dso_load_curve_meter_info)
count(dso_meter_reading_active_energy_kwh)
count(dso_meter_reading_export_active_energy_kwh)
count(dso_meter_reading_reactive_energy_kvarh)
```

## Notes

Prometheus scrapes current exporter state. Historical imports should be written
as OpenMetrics blocks with `promtool tsdb create-blocks-from openmetrics`, not
served as thousands of old timestamped samples from `/metrics`.

## Public Safety

- Do not commit portal usernames, passwords, cookies, Aura tokens, session IDs,
  raw POD lists, or raw portal payloads.
- Keep deployment hostnames, private IPs, and Grafana/Prometheus URLs in local
  runbooks or ignored files.
- Fixtures must be sanitized and should preserve only protocol shape.
