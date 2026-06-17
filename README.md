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
python -m playwright install chromium

export RETELE_ELECTRICE_ACCOUNTS=main
export RETELE_ELECTRICE_MAIN_USERNAME='user@example.com'
export RETELE_ELECTRICE_MAIN_PASSWORD='secret'
export RETELE_ELECTRICE_ONLY_PODS='RO001EXXXXXXXXX,RO001EYYYYYYYYY'
python -m dso_load_curves_exporter --host 0.0.0.0 --port 9831
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

## Synology Observability

Current DiskStation deployment:

- Exporter metrics: `http://diskstation.vilt.ro:9831/metrics`
- Prometheus: `http://diskstation.vilt.ro:9092/`
- Grafana: `http://192.168.37.21/grafana/`
- Grafana dashboard: `http://192.168.37.21/grafana/d/tancabesti-dso-meter-readings/tancabesti-dso-meter-readings`

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
