import asyncio
import builtins
import importlib
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import dso_load_curves_exporter.__main__ as exporter
from dso_load_curves_exporter.config import Account, Config
from dso_load_curves_exporter.metrics import Snapshot
from dso_retele_electrice.models import MeterReading, PodMetadata


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


def test_default_config_runtime_is_http_and_browser_client_is_not_imported(monkeypatch):
    monkeypatch.setenv("RETELE_ELECTRICE_USERNAME", "user")
    monkeypatch.setenv("RETELE_ELECTRICE_PASSWORD", "pass")
    monkeypatch.delenv("RETELE_ELECTRICE_RUNTIME", raising=False)
    sys.modules.pop("dso_retele_electrice.client", None)

    from dso_load_curves_exporter.config import load_config

    importlib.reload(exporter)

    assert load_config().runtime == "http"
    assert "dso_retele_electrice.client" not in sys.modules


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


def _config(*accounts: str) -> Config:
    return Config(
        accounts=[Account(name=account, username="user", password="pass") for account in accounts],
        only_pods=set(),
        runtime="http",
        host="127.0.0.1",
        port=0,
        poll_seconds=900,
        headless=True,
    )


def _metadata(account: str, pod: str) -> PodMetadata:
    return PodMetadata(pod=pod, account=account, meter_serial=f"{account}-serial")


def _reading(account: str, pod: str) -> MeterReading:
    return MeterReading(
        pod=pod,
        account=account,
        read_at=datetime(2026, 6, 1, tzinfo=ZoneInfo("Europe/Bucharest")),
        meter_serial=f"{account}-serial",
        constant="1",
        reading_type="real",
        channel="active_import_zone_1",
        obis_code="1.8.1",
        value=1,
        unit="kWh",
    )
