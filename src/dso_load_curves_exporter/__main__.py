from __future__ import annotations

import argparse
import asyncio
import os
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dso_retele_electrice.models import LoadCurveSample, MeterReading, PodMetadata
from dso_retele_electrice.parsing import BUCHAREST

from .config import Config, load_config
from .metrics import Snapshot

SNAPSHOT = Snapshot()
LOCK = threading.Lock()


class PartialSnapshotError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        metadata: list[PodMetadata],
        readings: list[MeterReading],
        curves: list[LoadCurveSample],
        replace_readings: bool,
        replace_curves: bool,
    ) -> None:
        super().__init__(message)
        self.metadata = metadata
        self.readings = readings
        self.curves = curves
        self.replace_readings = replace_readings
        self.replace_curves = replace_curves


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.getenv("EXPORTER_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("EXPORTER_PORT", "9831")))
    args = parser.parse_args()
    config = load_config()
    config = Config(
        accounts=config.accounts,
        only_pods=config.only_pods,
        only_pods_by_account=config.only_pods_by_account,
        runtime=config.runtime,
        host=args.host,
        port=args.port,
        poll_seconds=config.poll_seconds,
        headless=config.headless,
    )
    worker = threading.Thread(target=lambda: asyncio.run(poll_loop(config)), daemon=True)
    worker.start()
    server = ThreadingHTTPServer((config.host, config.port), Handler)
    print(f"dso-load-curves-exporter listening on {config.host}:{config.port}", flush=True)
    server.serve_forever()


async def poll_loop(config: Config) -> None:
    while True:
        await poll_once(config)
        await asyncio.sleep(config.poll_seconds)


async def poll_once(config: Config) -> None:
    now = time.time()
    cycle_errors: list[str] = []
    with LOCK:
        SNAPSHOT.last_attempt = now

    for account in config.accounts:
        print(f"poll starting account={account.name}", flush=True)
        try:
            account_meta, account_readings, account_curves = await fetch_account_snapshot(
                config.runtime,
                account.name,
                account.username,
                account.password,
                only_pods=only_pods_for_account(config, account.name),
                headless=config.headless,
            )
            with LOCK:
                _publish_account_snapshot(account.name, account_meta, account_readings, account_curves)
                if not cycle_errors:
                    SNAPSHOT.last_error = ""
            pod_count = len({item.pod for item in account_meta})
            print(
                f"poll published account={account.name} pods={pod_count} "
                f"readings={len(account_readings)} curves={len(account_curves)}",
                flush=True,
            )
        except PartialSnapshotError as exc:
            last_error = f"{account.name}: {exc}"
            cycle_errors.append(last_error)
            with LOCK:
                _publish_account_snapshot(
                    account.name,
                    exc.metadata,
                    exc.readings,
                    exc.curves,
                    replace_readings=exc.replace_readings,
                    replace_curves=exc.replace_curves,
                    mark_success=False,
                )
                SNAPSHOT.last_attempt = now
                SNAPSHOT.last_error = last_error
                SNAPSHOT.errors_total += 1
            print(f"poll degraded account={account.name}: {exc}", flush=True)
        except Exception as exc:
            last_error = f"{account.name}: {exc}"
            cycle_errors.append(last_error)
            with LOCK:
                SNAPSHOT.last_attempt = now
                SNAPSHOT.last_error = last_error
                SNAPSHOT.errors_total += 1
            print(f"poll failed account={account.name}: {exc}", flush=True)


async def fetch_account_snapshot(
    runtime: str,
    account: str,
    username: str,
    password: str,
    only_pods: set[str] | None = None,
    headless: bool = True,
) -> tuple[list[PodMetadata], list[MeterReading], list[LoadCurveSample]]:
    if runtime == "http":
        return await _fetch_account_snapshot_http(account, username, password, only_pods=only_pods)
    if runtime == "browser":
        return await _fetch_account_snapshot_browser(
            account,
            username,
            password,
            only_pods=only_pods,
            headless=headless,
        )
    raise RuntimeError(f"Unsupported Rețele Electrice runtime {runtime!r}.")


async def _fetch_account_snapshot_http(
    account: str,
    username: str,
    password: str,
    *,
    only_pods: set[str] | None = None,
) -> tuple[list[PodMetadata], list[MeterReading], list[LoadCurveSample]]:
    from dso_retele_electrice.http_client import ReteleElectriceHttpClient

    async with ReteleElectriceHttpClient(username, password, account=account) as client:
        if only_pods:
            return await _fetch_configured_pods_http_snapshot(client, account, only_pods)

        pods = await client.list_pods()
        return await _fetch_configured_pods_http_snapshot(client, account, {pod.pod for pod in pods})


async def _fetch_configured_pods_http_snapshot(
    client: object,
    account: str,
    only_pods: set[str],
) -> tuple[list[PodMetadata], list[MeterReading], list[LoadCurveSample]]:
    pods = [PodMetadata(pod=pod, account=account) for pod in sorted(only_pods)]
    metadata: list[PodMetadata] = []
    readings: list[MeterReading] = []
    curves: list[LoadCurveSample] = []
    data_attempts = 0
    data_successes = 0
    metadata_successes = 0
    reading_successes = 0
    curve_successes = 0
    data_errors: list[str] = []
    curve_day = datetime.now(BUCHAREST).date() - timedelta(days=1)

    for pod in pods:
        try:
            metadata.append(await client.get_pod_metadata(pod.pod))  # type: ignore[attr-defined]
            metadata_successes += 1
        except Exception as exc:
            metadata.append(pod)
            print(f"poll warning account={account} pod={pod.pod} metadata failed: {exc}", flush=True)

        try:
            data_attempts += 1
            readings.extend(await client.get_meter_readings(pod.pod))  # type: ignore[attr-defined]
            data_successes += 1
            reading_successes += 1
        except Exception as exc:
            data_errors.append(f"{pod.pod} readings: {exc}")
            print(f"poll warning account={account} pod={pod.pod} readings failed: {exc}", flush=True)

        try:
            data_attempts += 1
            curves.extend(await client.get_load_curve_samples(pod.pod, curve_day))  # type: ignore[attr-defined]
            data_successes += 1
            curve_successes += 1
        except Exception as exc:
            data_errors.append(f"{pod.pod} curves: {exc}")
            print(f"poll warning account={account} pod={pod.pod} curves failed: {exc}", flush=True)

        await asyncio.sleep(0.2)

    if data_attempts and data_successes == 0 and metadata_successes == 0:
        details = "; ".join(data_errors[:4])
        if len(data_errors) > 4:
            details += f"; ... {len(data_errors) - 4} more"
        raise RuntimeError(f"All configured POD fetches failed for account {account}: {details}")
    if data_attempts and data_errors:
        details = "; ".join(data_errors[:4])
        if len(data_errors) > 4:
            details += f"; ... {len(data_errors) - 4} more"
        raise PartialSnapshotError(
            f"Configured POD snapshot degraded for account {account}: {details}",
            metadata=metadata,
            readings=readings,
            curves=curves,
            replace_readings=reading_successes > 0,
            replace_curves=curve_successes > 0,
        )

    return metadata, readings, curves


async def _fetch_account_snapshot_browser(
    account: str,
    username: str,
    password: str,
    *,
    only_pods: set[str] | None = None,
    headless: bool = True,
) -> tuple[list[PodMetadata], list[MeterReading], list[LoadCurveSample]]:
    try:
        __import__("playwright.async_api")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Browser runtime requires the optional Playwright dependency. "
            "Install with 'dso-load-curve-exporter[browser]' or set RETELE_ELECTRICE_RUNTIME=http."
        ) from exc

    from dso_retele_electrice.client import fetch_account_snapshot as fetch_browser_account_snapshot

    return await fetch_browser_account_snapshot(
        account,
        username,
        password,
        only_pods=only_pods,
        headless=headless,
    )


def _publish_account_snapshot(
    account: str,
    metadata: list[PodMetadata],
    readings: list[MeterReading],
    curves: list[LoadCurveSample],
    *,
    replace_readings: bool = True,
    replace_curves: bool = True,
    mark_success: bool = True,
) -> None:
    for key in [key for key in SNAPSHOT.metadata if key[0] == account]:
        del SNAPSHOT.metadata[key]
    SNAPSHOT.metadata.update({(item.account, item.pod): item for item in metadata})
    if replace_readings:
        SNAPSHOT.readings = [item for item in SNAPSHOT.readings if item.account != account]
        SNAPSHOT.readings.extend(readings)
    if replace_curves:
        SNAPSHOT.curves = [item for item in SNAPSHOT.curves if item.account != account]
        SNAPSHOT.curves.extend(curves)
    if mark_success:
        SNAPSHOT.last_success = time.time()


def only_pods_for_account(config: Config, account: str) -> set[str] | None:
    account_pods = config.only_pods_by_account.get(account, set())
    if account_pods:
        return account_pods
    return config.only_pods or None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/healthz":
            with LOCK:
                ok = SNAPSHOT.last_success > 0 and not SNAPSHOT.last_error
                body = b"ok\n" if ok else f"not ready: {SNAPSHOT.last_error}\n".encode()
            self.send_response(200 if ok else 503)
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        with LOCK:
            body = SNAPSHOT.render().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


if __name__ == "__main__":
    main()
