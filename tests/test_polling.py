import asyncio
import builtins
import importlib
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import dso_load_curves_exporter.__main__ as exporter
from dso_load_curves_exporter.config import Account, Config
from dso_load_curves_exporter.metrics import Snapshot
from dso_retele_electrice.models import LoadCurveSample, MeterReading, PodMetadata


def test_poll_once_sets_last_attempt_before_first_fetch_returns(monkeypatch):
    async def run() -> None:
        fetch_started = asyncio.Event()
        release_fetch = asyncio.Event()

        async def fetch_account_snapshot(*_args, **_kwargs):
            fetch_started.set()
            await release_fetch.wait()
            return [_metadata("main", "RO001")], [], []

        monkeypatch.setattr(exporter, "SNAPSHOT", Snapshot())
        monkeypatch.setattr(exporter, "fetch_account_snapshot", fetch_account_snapshot)

        task = asyncio.create_task(exporter.poll_once(_config("main")))
        await fetch_started.wait()

        assert exporter.SNAPSHOT.last_attempt > 0
        assert exporter.SNAPSHOT.last_success == 0
        assert exporter.SNAPSHOT.metadata == {}

        release_fetch.set()
        await task

    asyncio.run(run())


def test_poll_once_publishes_successful_accounts_incrementally_and_preserves_them(monkeypatch):
    async def run() -> None:
        second_started = asyncio.Event()
        release_second = asyncio.Event()

        async def fetch_account_snapshot(runtime, account, *_args, **_kwargs):
            assert runtime == "http"
            if account == "main":
                return [_metadata("main", "RO001")], [_reading("main", "RO001")], []
            second_started.set()
            await release_second.wait()
            raise RuntimeError("portal timeout")

        monkeypatch.setattr(exporter, "SNAPSHOT", Snapshot())
        monkeypatch.setattr(exporter, "fetch_account_snapshot", fetch_account_snapshot)

        task = asyncio.create_task(exporter.poll_once(_config("main", "backup")))
        await second_started.wait()

        assert ("main", "RO001") in exporter.SNAPSHOT.metadata
        assert [reading.account for reading in exporter.SNAPSHOT.readings] == ["main"]
        assert exporter.SNAPSHOT.last_success > 0

        release_second.set()
        await task

        assert ("main", "RO001") in exporter.SNAPSHOT.metadata
        assert [reading.account for reading in exporter.SNAPSHOT.readings] == ["main"]
        assert exporter.SNAPSHOT.last_error == "backup: portal timeout"
        assert exporter.SNAPSHOT.errors_total == 1

    asyncio.run(run())


def test_poll_once_keeps_cycle_error_when_later_account_succeeds(monkeypatch):
    async def run() -> None:
        async def fetch_account_snapshot(runtime, account, *_args, **_kwargs):
            assert runtime == "http"
            if account == "main":
                raise RuntimeError("portal timeout")
            return [_metadata("backup", "RO002")], [], []

        monkeypatch.setattr(exporter, "SNAPSHOT", Snapshot())
        monkeypatch.setattr(exporter, "fetch_account_snapshot", fetch_account_snapshot)

        await exporter.poll_once(_config("main", "backup"))

        assert ("backup", "RO002") in exporter.SNAPSHOT.metadata
        assert exporter.SNAPSHOT.last_success > 0
        assert exporter.SNAPSHOT.last_error == "main: portal timeout"
        assert exporter.SNAPSHOT.errors_total == 1

    asyncio.run(run())


def test_poll_once_preserves_last_good_dynamic_data_on_partial_snapshot(monkeypatch):
    async def run() -> None:
        async def fetch_account_snapshot(runtime, account, *_args, **_kwargs):
            assert runtime == "http"
            raise exporter.PartialSnapshotError(
                "configured POD snapshot degraded",
                metadata=[_metadata("main", "RO001")],
                readings=[],
                curves=[],
                replace_readings=False,
                replace_curves=False,
            )

        snap = Snapshot()
        snap.metadata = {("main", "RO001"): _metadata("main", "RO001")}
        snap.readings = [_reading("main", "RO001")]
        monkeypatch.setattr(exporter, "SNAPSHOT", snap)
        monkeypatch.setattr(exporter, "fetch_account_snapshot", fetch_account_snapshot)

        await exporter.poll_once(_config("main"))

        assert ("main", "RO001") in exporter.SNAPSHOT.metadata
        assert exporter.SNAPSHOT.readings == snap.readings
        assert exporter.SNAPSHOT.last_success == 0
        assert exporter.SNAPSHOT.last_error == "main: configured POD snapshot degraded"
        assert exporter.SNAPSHOT.errors_total == 1

    asyncio.run(run())


def test_poll_once_marks_partial_snapshot_success_when_readings_are_fresh(monkeypatch):
    async def run() -> None:
        async def fetch_account_snapshot(runtime, account, *_args, **_kwargs):
            assert runtime == "http"
            raise exporter.PartialSnapshotError(
                "configured POD snapshot degraded",
                metadata=[_metadata("main", "RO001")],
                readings=[_reading("main", "RO001")],
                curves=[],
                replace_readings=True,
                replace_curves=False,
            )

        monkeypatch.setattr(exporter, "SNAPSHOT", Snapshot())
        monkeypatch.setattr(exporter, "fetch_account_snapshot", fetch_account_snapshot)

        await exporter.poll_once(_config("main"))

        assert exporter.SNAPSHOT.last_success > 0
        assert exporter.SNAPSHOT.last_error == "main: configured POD snapshot degraded"
        assert exporter.SNAPSHOT.readings
        assert exporter.SNAPSHOT.errors_total == 1

    asyncio.run(run())


def test_partial_snapshot_replaces_only_successful_reading_series_and_preserves_metadata(monkeypatch):
    snap = Snapshot()
    monkeypatch.setattr(exporter, "SNAPSHOT", snap)
    rich_meta = PodMetadata(pod="RO001", account="main", meter_serial="SERIAL-OLD", constant="1")
    snap.metadata = {("main", "RO001"): rich_meta, ("main", "RO002"): _metadata("main", "RO002")}
    old_ro001 = _reading("main", "RO001", channel="active_import", value=1)
    old_ro002 = _reading("main", "RO002", channel="active_import", value=2)
    old_export = _reading("main", "RO001", channel="active_export", value=3)
    snap.readings = [old_ro001, old_ro002, old_export]

    exporter._publish_account_snapshot(
        "main",
        [PodMetadata(pod="RO001", account="main")],
        [_reading("main", "RO001", channel="active_import", value=10)],
        [],
        replace_readings=True,
        replace_curves=False,
        mark_success=False,
    )

    assert snap.metadata[("main", "RO001")] == rich_meta
    assert _reading_values(snap.readings) == {
        ("RO001", "active_import"): 10,
        ("RO001", "active_export"): 3,
        ("RO002", "active_import"): 2,
    }


def test_default_config_runtime_is_http_and_browser_client_is_not_imported(monkeypatch):
    monkeypatch.setenv("RETELE_ELECTRICE_USERNAME", "user")
    monkeypatch.setenv("RETELE_ELECTRICE_PASSWORD", "pass")
    monkeypatch.delenv("RETELE_ELECTRICE_RUNTIME", raising=False)
    sys.modules.pop("dso_retele_electrice.client", None)

    from dso_load_curves_exporter.config import load_config

    importlib.reload(exporter)

    assert load_config().runtime == "http"
    assert "dso_retele_electrice.client" not in sys.modules


def test_config_supports_account_scoped_static_pods(monkeypatch):
    monkeypatch.setenv("RETELE_ELECTRICE_ACCOUNTS", "main,calin")
    monkeypatch.setenv("RETELE_ELECTRICE_MAIN_USERNAME", "main-user")
    monkeypatch.setenv("RETELE_ELECTRICE_MAIN_PASSWORD", "main-pass")
    monkeypatch.setenv("RETELE_ELECTRICE_CALIN_USERNAME", "calin-user")
    monkeypatch.setenv("RETELE_ELECTRICE_CALIN_PASSWORD", "calin-pass")
    monkeypatch.setenv("RETELE_ELECTRICE_ONLY_PODS", "RO001EGLOBAL")
    monkeypatch.setenv("RETELE_ELECTRICE_MAIN_ONLY_PODS", "RO001EMAIN")

    from dso_load_curves_exporter.config import load_config

    config = load_config()

    assert exporter.only_pods_for_account(config, "main") == {"RO001EMAIN"}
    assert exporter.only_pods_for_account(config, "calin") == {"RO001EGLOBAL"}


def test_default_exporter_import_does_not_load_browser_client_in_fresh_process():
    code = (
        "import sys; "
        "import dso_load_curves_exporter.__main__; "
        "raise SystemExit(1 if 'dso_retele_electrice.client' in sys.modules else 0)"
    )
    env = {**os.environ, "PYTHONPATH": "src"}
    subprocess.run([sys.executable, "-c", code], cwd=Path.cwd(), env=env, check=True)


def test_browser_runtime_fails_clearly_when_playwright_is_absent(monkeypatch):
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "playwright.async_api":
            raise ModuleNotFoundError("No module named 'playwright'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    async def run() -> None:
        try:
            await exporter._fetch_account_snapshot_browser("main", "user", "pass")
        except RuntimeError as exc:
            assert "optional Playwright dependency" in str(exc)
        else:
            raise AssertionError("browser runtime should require Playwright")

    asyncio.run(run())


def test_poll_once_passes_http_runtime_by_default(monkeypatch):
    async def run() -> None:
        seen_runtime = None

        async def fetch_account_snapshot(runtime, *_args, **_kwargs):
            nonlocal seen_runtime
            seen_runtime = runtime
            return [_metadata("main", "RO001")], [], []

        monkeypatch.setattr(exporter, "SNAPSHOT", Snapshot())
        monkeypatch.setattr(exporter, "fetch_account_snapshot", fetch_account_snapshot)

        await exporter.poll_once(_config("main"))

        assert seen_runtime == "http"

    asyncio.run(run())


def test_load_curve_fetch_looks_back_when_latest_day_has_no_samples():
    class Client:
        def __init__(self):
            self.days = []

        async def get_load_curve_samples(self, pod, day, *, channel="active_import"):
            self.days.append(day.isoformat())
            if len(self.days) == 1:
                raise RuntimeError("Load-curve response has no samples for the requested date.")
            return [
                LoadCurveSample(
                    pod=pod,
                    account="main",
                    start_at=datetime(2026, 6, 16, tzinfo=ZoneInfo("Europe/Bucharest")),
                    interval_seconds=3600,
                    channel=channel,
                    obis_code="1.8.0",
                    interval_value=1,
                    interval_unit="Wh",
                    average_value=1,
                    average_unit="W",
                )
            ]

    async def run() -> None:
        client = Client()
        samples = await exporter._get_load_curve_samples_with_lookback(
            client,
            "RO001",
            datetime(2026, 6, 17).date(),
            channel="active_import",
            lookback_days=3,
        )

        assert [sample.pod for sample in samples] == ["RO001"]
        assert client.days == ["2026-06-17", "2026-06-16"]

    asyncio.run(run())


def _config(*accounts: str) -> Config:
    return Config(
        accounts=[Account(name=account, username="user", password="pass") for account in accounts],
        only_pods=set(),
        only_pods_by_account={account: set() for account in accounts},
        runtime="http",
        host="127.0.0.1",
        port=0,
        poll_seconds=900,
        headless=True,
        load_curve_lookback_days=7,
    )


def _metadata(account: str, pod: str) -> PodMetadata:
    return PodMetadata(pod=pod, account=account, meter_serial=f"{account}-serial")


def _reading(account: str, pod: str, *, channel: str = "active_import_zone_1", value: float = 1) -> MeterReading:
    return MeterReading(
        pod=pod,
        account=account,
        read_at=datetime(2026, 6, 1, tzinfo=ZoneInfo("Europe/Bucharest")),
        meter_serial=f"{account}-serial",
        constant="1",
        reading_type="real",
        channel=channel,
        obis_code={"active_import": "1.8.0", "active_export": "2.8.0"}.get(channel, "1.8.1"),
        value=value,
        unit="kWh",
    )


def _reading_values(readings: list[MeterReading]) -> dict[tuple[str, str], float]:
    return {(item.pod, item.channel): item.value for item in readings}
