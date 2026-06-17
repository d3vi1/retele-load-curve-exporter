from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class PodMetadata:
    pod: str
    account: str
    distributor: str = "retele_electrice"
    distribution_company: str = ""
    city: str = ""
    county: str = ""
    address: str = ""
    approved_power_kw: str = ""
    voltage_level: str = ""
    supplier: str = ""
    balancing_responsible_party: str = ""
    customer_name: str = ""
    meter_serial: str = ""
    smartmeter_id: str = ""
    meter_brand: str = ""
    meter_type: str = ""
    accuracy_class: str = ""
    interval: str = ""
    meter_status: str = ""
    delimitation_voltage: str = ""
    atr_cer_number: str = ""
    atr_cer_date: str = ""
    mount_date: str = ""
    constant: str = ""
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MeterReading:
    pod: str
    account: str
    read_at: datetime
    meter_serial: str
    constant: str
    reading_type: str
    channel: str
    obis_code: str
    value: float
    unit: str


@dataclass(frozen=True)
class LoadCurveSample:
    pod: str
    account: str
    start_at: datetime
    interval_seconds: int
    channel: str
    obis_code: str
    interval_value: float
    interval_unit: str
    average_value: float
    average_unit: str
    source_quantity: str = "interval_energy"
