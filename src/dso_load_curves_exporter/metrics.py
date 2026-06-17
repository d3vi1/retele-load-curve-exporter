from __future__ import annotations

import math
import time
from collections.abc import Iterable

from dso_retele_electrice.models import LoadCurveSample, MeterReading, PodMetadata


class Snapshot:
    def __init__(self) -> None:
        self.metadata: dict[tuple[str, str], PodMetadata] = {}
        self.readings: list[MeterReading] = []
        self.curves: list[LoadCurveSample] = []
        self.last_success: float = 0
        self.last_attempt: float = 0
        self.last_error: str = ""
        self.errors_total: int = 0

    def render(self) -> str:
        readings = latest_readings(self.readings)
        reading_constants = constants_by_pod(readings)
        lines = [
            "# HELP dso_exporter_last_attempt_timestamp_seconds Last scrape attempt timestamp.",
            "# TYPE dso_exporter_last_attempt_timestamp_seconds gauge",
            f"dso_exporter_last_attempt_timestamp_seconds {self.last_attempt or 0}",
            "# HELP dso_exporter_last_success_timestamp_seconds Last successful portal scrape timestamp.",
            "# TYPE dso_exporter_last_success_timestamp_seconds gauge",
            f"dso_exporter_last_success_timestamp_seconds {self.last_success or 0}",
            "# HELP dso_exporter_fetch_success Last fetch success as 1 or 0.",
            "# TYPE dso_exporter_fetch_success gauge",
            f"dso_exporter_fetch_success {1 if self.last_success >= self.last_attempt else 0}",
            "# HELP dso_exporter_last_error_info Last scrape error class, if any.",
            "# TYPE dso_exporter_last_error_info gauge",
            f'dso_exporter_last_error_info{{error_code="{esc(error_code(self.last_error))}"}} {1 if self.last_error else 0}',
            "# HELP dso_exporter_fetch_errors_total Portal scrape errors.",
            "# TYPE dso_exporter_fetch_errors_total counter",
            f"dso_exporter_fetch_errors_total {self.errors_total}",
            "# HELP dso_load_curve_meter_info Static meter metadata.",
            "# TYPE dso_load_curve_meter_info gauge",
        ]
        for meta in self.metadata.values():
            labels = meta_labels(meta)
            if not labels.get("constant"):
                labels["constant"] = reading_constants.get((meta.account, meta.pod), "")
            lines.append(f"dso_load_curve_meter_info{{{label_text(labels)}}} 1")

        lines.extend(
            [
                "# HELP dso_meter_reading_active_energy_kwh Meter cumulative active import reading.",
                "# TYPE dso_meter_reading_active_energy_kwh gauge",
                "# HELP dso_meter_reading_export_active_energy_kwh Meter cumulative active export reading.",
                "# TYPE dso_meter_reading_export_active_energy_kwh gauge",
                "# HELP dso_meter_reading_reactive_energy_kvarh Meter cumulative reactive reading.",
                "# TYPE dso_meter_reading_reactive_energy_kvarh gauge",
                "# HELP dso_meter_reading_source_timestamp_seconds Source timestamp of the latest meter reading.",
                "# TYPE dso_meter_reading_source_timestamp_seconds gauge",
            ]
        )
        for reading in readings:
            meta = self.metadata.get((reading.account, reading.pod))
            labels = common_labels(meta, reading.account, reading.pod)
            labels.update(
                {
                    "meter_serial": reading.meter_serial or labels.get("meter_serial", ""),
                    "smartmeter_id": reading.meter_serial or labels.get("smartmeter_id", ""),
                    "obis_code": reading.obis_code,
                    "channel": reading.channel,
                    "reading_type": reading.reading_type,
                    "constant": reading.constant or labels.get("constant", ""),
                }
            )
            metric = "dso_meter_reading_active_energy_kwh"
            if reading.channel == "active_export":
                metric = "dso_meter_reading_export_active_energy_kwh"
            elif reading.unit.lower() == "kvarh":
                metric = "dso_meter_reading_reactive_energy_kvarh"
            lines.append(f"{metric}{{{label_text(labels)}}} {fmt(reading.value)}")
            lines.append(
                f"dso_meter_reading_source_timestamp_seconds{{{label_text(labels)}}} "
                f"{fmt(reading.read_at.timestamp())}"
            )

        lines.extend(
            [
                "# HELP dso_load_curve_interval_energy_wh Load-curve interval active energy.",
                "# TYPE dso_load_curve_interval_energy_wh gauge",
                "# HELP dso_load_curve_interval_reactive_energy_varh Load-curve interval reactive energy.",
                "# TYPE dso_load_curve_interval_reactive_energy_varh gauge",
                "# HELP dso_load_curve_average_power_w Derived average active power.",
                "# TYPE dso_load_curve_average_power_w gauge",
                "# HELP dso_load_curve_average_reactive_power_var Derived average reactive power.",
                "# TYPE dso_load_curve_average_reactive_power_var gauge",
                "# HELP dso_load_curve_source_timestamp_seconds Source timestamp of the latest load-curve interval.",
                "# TYPE dso_load_curve_source_timestamp_seconds gauge",
            ]
        )
        for curve in latest_curves(self.curves):
            meta = self.metadata.get((curve.account, curve.pod))
            labels = common_labels(meta, curve.account, curve.pod)
            if not labels.get("constant"):
                labels["constant"] = reading_constants.get((curve.account, curve.pod), "")
            labels.update(
                {
                    "obis_code": curve.obis_code,
                    "channel": curve.channel,
                    "interval": iso_duration_seconds(curve.interval_seconds),
                    "source_quantity": "interval_energy",
                    "value_source": "portal_engineering_units",
                    "scaling_status": "portal_units_unscaled",
                }
            )
            lines.append(
                f"dso_load_curve_source_timestamp_seconds{{{label_text(labels)}}} "
                f"{fmt(curve.start_at.timestamp())}"
            )
            if curve.interval_unit == "Wh":
                lines.append(f"dso_load_curve_interval_energy_wh{{{label_text(labels)}}} {fmt(curve.interval_value)}")
                p_labels = labels | {"source_quantity": "derived_power", "value_source": "derived_from_portal_energy"}
                lines.append(f"dso_load_curve_average_power_w{{{label_text(p_labels)}}} {fmt(curve.average_value)}")
            else:
                lines.append(f"dso_load_curve_interval_reactive_energy_varh{{{label_text(labels)}}} {fmt(curve.interval_value)}")
                p_labels = labels | {"source_quantity": "derived_reactive_power", "value_source": "derived_from_portal_reactive_energy"}
                lines.append(f"dso_load_curve_average_reactive_power_var{{{label_text(p_labels)}}} {fmt(curve.average_value)}")
        return "\n".join(lines) + "\n"


def meta_labels(meta: PodMetadata) -> dict[str, str]:
    labels = common_labels(meta, meta.account, meta.pod)
    labels.update(
        {
            "meter_brand": meta.meter_brand,
            "meter_type": meta.meter_type,
            "accuracy_class": meta.accuracy_class,
            "interval": meta.interval,
            "meter_status": meta.meter_status,
            "delimitation_voltage": meta.delimitation_voltage,
            "approved_power_kw": meta.approved_power_kw,
            "mount_date": meta.mount_date,
            "constant": meta.constant,
        }
    )
    return labels


def common_labels(meta: PodMetadata | None, account: str, pod: str) -> dict[str, str]:
    if meta is None:
        return {
            "distributor": "retele_electrice",
            "account": account,
            "pod": pod,
            "smartmeter_id": "",
            "meter_serial": "",
            "constant": "",
        }
    return {
        "distributor": meta.distributor,
        "account": meta.account,
        "pod": meta.pod,
        "smartmeter_id": meta.smartmeter_id,
        "meter_serial": meta.meter_serial,
        "constant": meta.constant,
    }


def label_text(labels: dict[str, str]) -> str:
    return ",".join(f'{key}="{esc(value)}"' for key, value in sorted(labels.items()))


def esc(value: object) -> str:
    return str(value or "").replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def fmt(value: float) -> str:
    if not math.isfinite(value):
        return "NaN"
    return f"{value:.12g}"


def iso_duration_seconds(seconds: int) -> str:
    if seconds <= 0:
        return "PT0S"
    if seconds % 3600 == 0:
        return f"PT{seconds // 3600}H"
    if seconds % 60 == 0:
        return f"PT{seconds // 60}M"
    return f"PT{seconds}S"


def latest_readings(readings: Iterable[MeterReading]) -> list[MeterReading]:
    latest: dict[tuple[str, str, str, str, str], MeterReading] = {}
    for reading in readings:
        key = (reading.account, reading.pod, reading.channel, reading.obis_code, reading.reading_type)
        previous = latest.get(key)
        if previous is None or reading.read_at > previous.read_at:
            latest[key] = reading
    return sorted(latest.values(), key=lambda item: (item.account, item.pod, item.channel, item.obis_code))


def constants_by_pod(readings: Iterable[MeterReading]) -> dict[tuple[str, str], str]:
    constants: dict[tuple[str, str], str] = {}
    for reading in readings:
        if reading.constant:
            constants[(reading.account, reading.pod)] = reading.constant
    return constants


def latest_curves(curves: Iterable[LoadCurveSample]) -> list[LoadCurveSample]:
    latest: dict[tuple[str, str, str, str], LoadCurveSample] = {}
    for curve in curves:
        key = (curve.account, curve.pod, curve.channel, curve.obis_code)
        previous = latest.get(key)
        if previous is None or curve.start_at > previous.start_at:
            latest[key] = curve
    return sorted(latest.values(), key=lambda item: (item.account, item.pod, item.channel, item.obis_code))


def error_code(message: str) -> str:
    normalized = str(message or "").casefold()
    if not normalized:
        return ""
    if "pod discovery" in normalized or "aura" in normalized:
        return "pod_discovery_failed"
    if "metadata" in normalized:
        return "metadata_failed"
    if "readings" in normalized:
        return "readings_failed"
    if "curves" in normalized or "load curve" in normalized:
        return "load_curve_failed"
    if "login" in normalized or "frontdoor" in normalized:
        return "login_failed"
    if "timeout" in normalized:
        return "timeout"
    return "portal_fetch_failed"
