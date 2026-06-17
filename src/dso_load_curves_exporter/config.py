from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Account:
    name: str
    username: str
    password: str


@dataclass(frozen=True)
class Config:
    accounts: list[Account]
    only_pods: set[str]
    only_pods_by_account: dict[str, set[str]]
    runtime: str
    host: str
    port: int
    poll_seconds: int
    headless: bool
    load_curve_lookback_days: int


def load_config() -> Config:
    runtime = os.getenv("RETELE_ELECTRICE_RUNTIME", "http").strip().lower()
    if runtime not in {"http", "browser"}:
        raise RuntimeError("RETELE_ELECTRICE_RUNTIME must be either 'http' or 'browser'.")

    account_names = _csv(os.getenv("RETELE_ELECTRICE_ACCOUNTS", "main"))
    accounts: list[Account] = []
    for name in account_names:
        prefix = f"RETELE_ELECTRICE_{name.upper()}_"
        username = os.getenv(prefix + "USERNAME")
        password = os.getenv(prefix + "PASSWORD")
        if not username and name == "main":
            username = os.getenv("RETELE_ELECTRICE_USERNAME")
            password = os.getenv("RETELE_ELECTRICE_PASSWORD")
        if not username or not password:
            raise RuntimeError(f"Missing credentials for Rețele Electrice account {name!r}.")
        accounts.append(Account(name=name, username=username, password=password))
    global_only_pods = set(_csv(os.getenv("RETELE_ELECTRICE_ONLY_PODS", "")))
    only_pods_by_account = {
        name: set(_csv(os.getenv(f"RETELE_ELECTRICE_{name.upper()}_ONLY_PODS", ""))) for name in account_names
    }
    return Config(
        accounts=accounts,
        only_pods=global_only_pods,
        only_pods_by_account=only_pods_by_account,
        runtime=runtime,
        host=os.getenv("EXPORTER_HOST", "0.0.0.0"),
        port=int(os.getenv("EXPORTER_PORT", "9831")),
        poll_seconds=int(os.getenv("EXPORTER_POLL_SECONDS", "900")),
        headless=os.getenv("EXPORTER_HEADLESS", "true").lower() not in {"0", "false", "no"},
        load_curve_lookback_days=max(1, int(os.getenv("RETELE_ELECTRICE_LOAD_CURVE_LOOKBACK_DAYS", "7"))),
    )


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]
