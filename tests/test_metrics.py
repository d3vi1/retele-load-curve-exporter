from datetime import datetime
from zoneinfo import ZoneInfo

from dso_load_curves_exporter.metrics import Snapshot
from dso_retele_electrice.models import LoadCurveSample, MeterReading, PodMetadata


def test_snapshot_renders_reading_and_curve_metrics():
    snap = Snapshot()
    snap.last_attempt = 1
    snap.last_success = 2
    meta = PodMetadata(
        pod="RO001EXXXXXXXXX",
        account="main",
        meter_serial="SERIAL",
        smartmeter_id="SERIAL",
        constant="1",
        address="redacted",
        meter_brand="SANITIZED_BRAND",
        approved_power_kw="42",
    )
    snap.metadata = {("main", meta.pod): meta}
    when = datetime(2026, 6, 1, tzinfo=ZoneInfo("Europe/Bucharest"))
    snap.readings = [
        MeterReading(
            pod=meta.pod,
            account="main",
            read_at=when,
            meter_serial="SERIAL",
            constant="1",
            reading_type="real",
            channel="active_import_zone_1",
            obis_code="1.8.1",
            value=34724,
            unit="kWh",
        )
    ]
    snap.curves = [
        LoadCurveSample(
            pod=meta.pod,
            account="main",
            start_at=when,
            interval_seconds=900,
            channel="active_import",
            obis_code="1.8.0",
            interval_value=306,
            interval_unit="Wh",
            average_value=1224,
            average_unit="W",
        )
    ]
    rendered = snap.render()
    assert "dso_meter_reading_active_energy_kwh" in rendered
    assert "obis_code=\"1.8.1\"" in rendered
    assert "dso_load_curve_interval_energy_wh" in rendered
    assert "dso_load_curve_average_power_w" in rendered
    assert "dso_meter_reading_source_timestamp_seconds" in rendered
    assert "dso_load_curve_source_timestamp_seconds" in rendered
    assert 'interval="PT15M"' in rendered
    assert 'value_source="portal_engineering_units"' in rendered
    assert 'scaling_status="portal_units_unscaled"' in rendered
    reading_line = next(line for line in rendered.splitlines() if line.startswith("dso_meter_reading_active_energy_kwh{"))
    curve_line = next(line for line in rendered.splitlines() if line.startswith("dso_load_curve_interval_energy_wh{"))
    assert 'constant="1"' in reading_line
    assert 'constant="1"' in curve_line
    assert "meter_brand=" not in reading_line
    assert "approved_power_kw=" not in reading_line
    assert "meter_brand=" not in curve_line
    assert "approved_power_kw=" not in curve_line
    info_line = next(line for line in rendered.splitlines() if line.startswith("dso_load_curve_meter_info{"))
    assert 'meter_brand="SANITIZED_BRAND"' in info_line
    assert 'approved_power_kw="42"' in info_line
    assert "1224" in rendered


def test_snapshot_does_not_render_sensitive_or_unbounded_labels():
    snap = Snapshot()
    snap.last_attempt = 2
    snap.last_error = "main: POD discovery Aura response is not usable: malformed json."
    meta = PodMetadata(
        pod="RO001EXXXXXXXXX",
        account="main",
        meter_serial="SERIAL",
        smartmeter_id="SERIAL",
        constant="1",
        address="REDACTED_STREET",
        atr_cer_number="123456",
        atr_cer_date="01.02.2024",
        supplier="SANITIZED_SUPPLIER",
        balancing_responsible_party="SANITIZED_PRE",
    )
    snap.metadata = {("main", meta.pod): meta}
    when = datetime(2026, 6, 1, tzinfo=ZoneInfo("Europe/Bucharest"))
    snap.readings = [
        MeterReading(
            pod=meta.pod,
            account="main",
            read_at=when,
            meter_serial="SERIAL",
            constant="1",
            reading_type="real",
            channel="active_import_zone_1",
            obis_code="1.8.1",
            value=34724,
            unit="kWh",
        ),
        MeterReading(
            pod=meta.pod,
            account="main",
            read_at=when.replace(day=2),
            meter_serial="SERIAL",
            constant="1",
            reading_type="real",
            channel="active_import_zone_1",
            obis_code="1.8.1",
            value=34725,
            unit="kWh",
        ),
    ]

    rendered = snap.render()

    assert 'last_error="' not in rendered
    assert 'error_code="pod_discovery_failed"' in rendered
    assert "source_timestamp=" not in rendered
    assert "consumption_address=" not in rendered
    assert "atr_cer_number=" not in rendered
    assert "atr_cer_date=" not in rendered
    assert "supplier=" not in rendered
    assert "balancing_responsible_party=" not in rendered
    assert rendered.count("dso_meter_reading_active_energy_kwh{") == 1
    assert "34725" in rendered
    assert "34724" not in rendered


def test_snapshot_fetch_success_allows_degraded_fresh_snapshot():
    snap = Snapshot()
    snap.last_attempt = 10
    snap.last_success = 11
    snap.last_error = "backup: readings failed"

    rendered = snap.render()

    assert "dso_exporter_fetch_success 1" in rendered
    assert 'dso_exporter_last_error_info{error_code="readings_failed"} 1' in rendered


def test_snapshot_renders_actual_curve_interval_duration():
    snap = Snapshot()
    snap.last_attempt = 1
    snap.last_success = 2
    meta = PodMetadata(pod="RO001EXXXXXXXXX", account="main", meter_serial="SERIAL")
    snap.metadata = {("main", meta.pod): meta}
    snap.curves = [
        LoadCurveSample(
            pod=meta.pod,
            account="main",
            start_at=datetime(2026, 6, 1, tzinfo=ZoneInfo("Europe/Bucharest")),
            interval_seconds=3600,
            channel="active_import",
            obis_code="1.8.0",
            interval_value=53000,
            interval_unit="Wh",
            average_value=53000,
            average_unit="W",
        )
    ]

    rendered = snap.render()

    assert 'interval="PT1H"' in rendered
    assert 'interval="PT15M"' not in rendered


def test_snapshot_fills_curve_constant_from_latest_reading_when_metadata_lacks_it():
    snap = Snapshot()
    snap.last_attempt = 1
    snap.last_success = 2
    meta = PodMetadata(pod="RO001EXXXXXXXXX", account="main", meter_serial="SERIAL")
    snap.metadata = {("main", meta.pod): meta}
    when = datetime(2026, 6, 1, tzinfo=ZoneInfo("Europe/Bucharest"))
    snap.readings = [
        MeterReading(
            pod=meta.pod,
            account="main",
            read_at=when,
            meter_serial="SERIAL",
            constant="2000",
            reading_type="real",
            channel="active_import",
            obis_code="1.8.0",
            value=1,
            unit="kWh",
        )
    ]
    snap.curves = [
        LoadCurveSample(
            pod=meta.pod,
            account="main",
            start_at=when,
            interval_seconds=3600,
            channel="active_import",
            obis_code="1.8.0",
            interval_value=1,
            interval_unit="Wh",
            average_value=1,
            average_unit="W",
        )
    ]

    rendered = snap.render()

    info_line = next(line for line in rendered.splitlines() if line.startswith("dso_load_curve_meter_info{"))
    curve_line = next(line for line in rendered.splitlines() if line.startswith("dso_load_curve_interval_energy_wh{"))
    assert 'constant="2000"' in info_line
    assert 'constant="2000"' in curve_line
