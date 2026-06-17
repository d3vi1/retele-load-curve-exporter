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
    assert "1224" in rendered
