from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from dso_retele_electrice.models import LoadCurveSample, MeterReading, PodMetadata


@dataclass(frozen=True)
class SnapshotResult:
    metadata: list[PodMetadata] = field(default_factory=list)
    readings: list[MeterReading] = field(default_factory=list)
    curves: list[LoadCurveSample] = field(default_factory=list)


class DistributorClient(Protocol):
    async def list_pods(self) -> list[PodMetadata]:
        ...

    async def get_pod_metadata(self, pod: str) -> PodMetadata:
        ...

    async def get_meter_readings(self, pod: str, expected_date: date | None = None) -> list[MeterReading]:
        ...

    async def get_load_curve_samples(
        self,
        pod: str,
        day: date,
        *,
        channel: str = "active_import",
    ) -> list[LoadCurveSample]:
        ...
