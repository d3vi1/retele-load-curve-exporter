# Synology Acceptance Evidence

Date: 2026-06-18

Environment: operator-managed Synology Docker host. Hostnames, private URLs,
credentials, and local `.env` contents are intentionally omitted.

## Build And Runtime

- Image built from `Dockerfile` with the pinned `python:3.12-slim` digest.
- Container recreated from `docker/docker-compose.synology.yml`.
- Effective runtime environment includes `RETELE_ELECTRICE_RUNTIME=http`.
- Runtime process list shows a single exporter process:
  `python -m dso_load_curves_exporter`.
- Runtime package probe reports `playwright=absent` and `selenium=absent`.

## Health And Metrics

After restart and initial poll:

- Container status: `healthy`.
- `/healthz`: HTTP 200 with `degraded` body only because the secondary account
  has no portal payload for its configured POD.
- Main account poll result: `pods=3 readings=296 curves=288`.
- `/metrics` current-family counts:
  - `dso_exporter_fetch_success`: `1`
  - `dso_load_curve_interval_energy_wh`: `6`
  - `dso_load_curve_interval_reactive_energy_varh`: `6`
  - `dso_meter_reading_active_energy_kwh`: `5`
  - `dso_meter_reading_export_active_energy_kwh`: `5`
  - `dso_meter_reading_reactive_energy_kvarh`: `10`

Prometheus queries against the local Prometheus endpoint returned the same
series counts after scrape.

## Known Degraded Account

The secondary account is configured and authenticated, but the portal returns no
`XML_Readings` and no `CurveDiCaricoGraph` payload for the configured POD. The
exporter keeps the fresh main-account snapshot published and exposes the
secondary-account condition through health text and `dso_exporter_last_error_info`.
