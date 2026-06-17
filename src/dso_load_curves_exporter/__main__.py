from __future__ import annotations

import argparse
import asyncio
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dso_retele_electrice.client import fetch_account_snapshot
from dso_retele_electrice.models import LoadCurveSample, MeterReading, PodMetadata

from .config import Config, load_config
from .metrics import Snapshot

SNAPSHOT = Snapshot()
LOCK = threading.Lock()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.getenv("EXPORTER_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("EXPORTER_PORT", "9831")))
    args = parser.parse_args()
    config = load_config()
    config = Config(
        accounts=config.accounts,
        only_pods=config.only_pods,
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
                account.name,
                account.username,
                account.password,
                only_pods=config.only_pods or None,
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
        except Exception as exc:
            last_error = f"{account.name}: {exc}"
            cycle_errors.append(last_error)
            with LOCK:
                SNAPSHOT.last_attempt = now
                SNAPSHOT.last_error = last_error
                SNAPSHOT.errors_total += 1
            print(f"poll failed account={account.name}: {exc}", flush=True)


def _publish_account_snapshot(
    account: str,
    metadata: list[PodMetadata],
    readings: list[MeterReading],
    curves: list[LoadCurveSample],
) -> None:
    for key in [key for key in SNAPSHOT.metadata if key[0] == account]:
        del SNAPSHOT.metadata[key]
    SNAPSHOT.metadata.update({(item.account, item.pod): item for item in metadata})
    SNAPSHOT.readings = [item for item in SNAPSHOT.readings if item.account != account]
    SNAPSHOT.readings.extend(readings)
    SNAPSHOT.curves = [item for item in SNAPSHOT.curves if item.account != account]
    SNAPSHOT.curves.extend(curves)
    SNAPSHOT.last_success = time.time()


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
