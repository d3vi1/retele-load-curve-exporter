from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterable

from .models import LoadCurveSample, MeterReading, PodMetadata
from .parsing import BUCHAREST, OBIS_BY_CHANNEL, parse_load_curve_csv, parse_ro_date, split_atr_cer

BASE_URL = "https://contulmeu.reteleelectrice.ro"
LOAD_CURVES_PATH = "/s/new-load-curves-client"
POD_INFO_PATH = "/s/new-pod-info-client"
READINGS_PATH = "/s/new-reading-archive-client"

ENERGY_LABELS = {
    "active_import": "energie activa consumata",
    "active_export": "energie activa produsa",
    "reactive_inductive": "energie reactiva inductiva",
    "reactive_capacitive": "energie reactiva capacitiva",
}


def normalize_text(value: str) -> str:
    return (
        str(value or "")
        .replace("\u00a0", " ")
        .casefold()
        .translate(str.maketrans({"ă": "a", "â": "a", "î": "i", "ș": "s", "ş": "s", "ț": "t", "ţ": "t"}))
    )


def collapse(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


class ReteleElectriceClient:
    def __init__(self, username: str, password: str, account: str = "default", headless: bool = True):
        self.username = username
        self.password = password
        self.account = account
        self.headless = headless

    async def __aenter__(self) -> "ReteleElectriceClient":
        module = __import__("playwright.async_api", fromlist=["async_playwright"])
        self._playwright = await module.async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        self._context = await self._browser.new_context(
            accept_downloads=True,
            viewport={"width": 1440, "height": 1200},
            locale="ro-RO",
            timezone_id="Europe/Bucharest",
        )
        self._page = await self._context.new_page()
        self._page.set_default_timeout(45_000)
        await self.login()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self._context.close()
        await self._browser.close()
        await self._playwright.stop()

    async def login(self) -> None:
        page = self._page
        await page.goto(BASE_URL, wait_until="domcontentloaded")
        user_input = page.get_by_role("textbox", name=re.compile("Utilizator", re.I))
        if await user_input.count():
            await user_input.fill(self.username)
            await page.get_by_role("textbox", name=re.compile("Parol", re.I)).fill(self.password)
            await page.locator('a[value^="AUTENTIFIC"], button[value^="AUTENTIFIC"], a:has-text("AUTENTIFIC")').first.click()
        try:
            await page.wait_for_url(re.compile(r"/s/|frontdoor\.jsp"), timeout=45_000)
        except Exception:
            pass
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(1500)
        body = normalize_text(await page.locator("body").inner_text())
        if "utilizator" in body and "parola" in body:
            raise RuntimeError("Login failed; portal still shows login form.")
        if "captcha" in body or "cod de verificare" in body:
            raise RuntimeError("Login requires CAPTCHA or verification code.")

    async def list_pods(self) -> list[PodMetadata]:
        await self._goto(f"{BASE_URL}{LOAD_CURVES_PATH}")
        await self._page.wait_for_function("() => document.body && document.body.innerText.includes('RO001')")
        rows = await self._table_rows()
        pods: list[PodMetadata] = []
        seen: set[str] = set()
        for cells in rows:
            if len(cells) < 5 or not re.match(r"^RO\d+E", cells[0], re.I):
                continue
            pod = cells[0]
            if pod in seen:
                continue
            seen.add(pod)
            pods.append(
                PodMetadata(
                    pod=pod,
                    account=self.account,
                    city=cells[1],
                    county=cells[2],
                    approved_power_kw=cells[3],
                    distribution_company=cells[4],
                )
            )
        return pods

    async def get_pod_metadata(self, pod: str) -> PodMetadata:
        await self._goto(f"{BASE_URL}{POD_INFO_PATH}?pod={pod}")
        await self._page.wait_for_function("(pod) => document.body && document.body.innerText.includes(pod)", arg=pod)
        await self._page.wait_for_timeout(1500)
        fields = await self._extract_label_values()
        atr_number, atr_date = split_atr_cer(self._pick(fields, "Nr. si dara ATR/CER", "Nr. si data ATR/CER"))
        return PodMetadata(
            pod=pod,
            account=self.account,
            supplier=self._pick(fields, "Furnizor"),
            balancing_responsible_party=self._pick(fields, "PRE"),
            customer_name=self._pick(fields, "Nume Client"),
            approved_power_kw=self._pick(fields, "Puterea aprobata (kW)"),
            address=self._pick(fields, "Adresa loc de consum", "Adresa"),
            atr_cer_number=atr_number,
            atr_cer_date=atr_date,
            voltage_level=self._pick(fields, "Tensiunea in punctul de delimitare"),
            delimitation_voltage=self._pick(fields, "Tensiunea in punctul de delimitare"),
            meter_status=self._pick(fields, "Stare"),
            meter_serial=self._pick(fields, "Seria Contorului"),
            smartmeter_id=self._pick(fields, "Seria Contorului"),
            meter_brand=self._pick(fields, "Marca"),
            meter_type=self._pick(fields, "Tip contor"),
            interval=self._pick(fields, "Interval"),
            accuracy_class=self._pick(fields, "Precize", "Precizie"),
            mount_date=self._pick(fields, "Data montare"),
            constant=self._pick(fields, "Constanta"),
            extra=fields,
        )

    async def get_meter_readings(self, pod: str) -> list[MeterReading]:
        await self._goto(f"{BASE_URL}{READINGS_PATH}?pod={pod}")
        await self._page.wait_for_function("() => document.body && document.body.innerText.includes('Detaliu')")
        await self._page.wait_for_function("() => document.body && document.body.innerText.includes('SERIE DE CONTOR')")
        await self._page.wait_for_timeout(3500)
        tables = await self._tables()
        reading_tables = [table for table in tables if table and table[0] and "DATA CITIRII" in table[0][0].upper()]
        if not reading_tables:
            return []
        table = max(reading_tables, key=len)
        header = [collapse(h).upper() for h in table[0]]
        readings: list[MeterReading] = []
        for cells in table[1:]:
            if len(cells) < 4:
                continue
            read_at = datetime.combine(parse_ro_date(cells[0]), datetime.min.time(), BUCHAREST)
            meter_serial = cells[1]
            constant = cells[2]
            reading_type = cells[3]
            for idx, title in enumerate(header[4:], start=4):
                if idx >= len(cells) or not cells[idx]:
                    continue
                value = self._float(cells[idx])
                if value is None:
                    continue
                channel, obis, unit = self._reading_channel(title)
                readings.append(
                    MeterReading(
                        pod=pod,
                        account=self.account,
                        read_at=read_at,
                        meter_serial=meter_serial,
                        constant=constant,
                        reading_type=reading_type,
                        channel=channel,
                        obis_code=obis,
                        value=value,
                        unit=unit,
                    )
                )
        return readings

    async def get_recent_load_curve_samples(self, pod: str, channels: Iterable[str] | None = None) -> list[LoadCurveSample]:
        channels = list(channels or ENERGY_LABELS.keys())
        samples: list[LoadCurveSample] = []
        for channel in channels:
            try:
                csv_text = await self._download_current_load_curve(pod, channel)
            except Exception:
                continue
            parsed = parse_load_curve_csv(csv_text)
            if not parsed:
                continue
            start_at, _q, raw_k = max(parsed, key=lambda item: item[0])
            interval_seconds = 900
            if channel.startswith("reactive_"):
                interval_value = raw_k * 1000.0
                average_value = interval_value * 3600.0 / interval_seconds
                interval_unit = "varh"
                average_unit = "var"
            else:
                interval_value = raw_k * 1000.0
                average_value = interval_value * 3600.0 / interval_seconds
                interval_unit = "Wh"
                average_unit = "W"
            samples.append(
                LoadCurveSample(
                    pod=pod,
                    account=self.account,
                    start_at=start_at,
                    interval_seconds=interval_seconds,
                    channel=channel,
                    obis_code=OBIS_BY_CHANNEL[channel],
                    interval_value=interval_value,
                    interval_unit=interval_unit,
                    average_value=average_value,
                    average_unit=average_unit,
                )
            )
        return samples

    async def _download_current_load_curve(self, pod: str, channel: str) -> str:
        await self._goto(f"{BASE_URL}{LOAD_CURVES_PATH}?pod={pod}")
        await self._page.wait_for_function("(pod) => document.body && document.body.innerText.includes(pod)", arg=pod)
        await self._page.wait_for_timeout(1000)
        selects = self._page.locator("select")
        count = await selects.count()
        if count < 3:
            raise RuntimeError("Load-curve selects not found.")
        energy_select = selects.nth(count - 1)
        await energy_select.select_option(
            value=await energy_select.evaluate(
                """(select, target) => {
                  const norm = s => String(s || '').normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').replace(/\\s+/g, ' ').trim().toLowerCase();
                  const option = Array.from(select.options).find(o => norm(o.textContent) === target);
                  if (!option) throw new Error('missing energy option');
                  return option.value;
                }""",
                ENERGY_LABELS[channel],
            )
        )
        await self._page.get_by_role("button", name=re.compile(r"^CAUT[ĂA]$", re.I)).click()
        download_button = self._page.get_by_role(
            "button", name=re.compile(r"Desc.rcare curba la granularitate maxim[ăa] disponibil[ăa]$", re.I)
        )
        await download_button.wait_for(state="visible")
        await self._page.wait_for_timeout(1200)
        with TemporaryDirectory() as tmp:
            async with self._page.expect_download(timeout=20_000) as download_info:
                await download_button.click()
            download = await download_info.value
            target = Path(tmp) / download.suggested_filename
            await download.save_as(target)
            return target.read_text(encoding="utf-8-sig")

    async def _goto(self, url: str) -> None:
        await self._page.goto(url, wait_until="domcontentloaded")

    async def _table_rows(self) -> list[list[str]]:
        return await self._page.evaluate(
            """() => Array.from(document.querySelectorAll('tr')).map(row =>
              Array.from(row.querySelectorAll('td')).map(cell => cell.innerText.replace(/\\s+/g, ' ').trim())
            ).filter(cells => cells.length)"""
        )

    async def _tables(self) -> list[list[list[str]]]:
        return await self._page.evaluate(
            """() => Array.from(document.querySelectorAll('table')).map(table =>
              Array.from(table.querySelectorAll('tr')).map(row =>
                Array.from(row.querySelectorAll('th,td')).map(cell => cell.innerText.replace(/\\s+/g, ' ').trim())
              )
            )"""
        )

    async def _extract_label_values(self) -> dict[str, str]:
        return await self._page.evaluate(
            """() => {
              const out = {};
              for (const label of Array.from(document.querySelectorAll('label'))) {
                const key = (label.innerText || label.textContent || '').replace(/\\s+/g, ' ').trim();
                if (!key) continue;
                let value = '';
                const root = label.parentElement;
                if (root) {
                  const input = root.querySelector('input, textarea');
                  if (input) value = input.value || input.textContent || '';
                }
                if (!value) {
                  const next = label.nextElementSibling;
                  const input = next && next.querySelector && next.querySelector('input, textarea');
                  if (input) value = input.value || input.textContent || '';
                }
                if (value) out[key] = String(value).replace(/\\s+/g, ' ').trim();
              }
              return out;
            }"""
        )

    @staticmethod
    def _pick(fields: dict[str, str], *names: str) -> str:
        normalized = {normalize_text(k): v for k, v in fields.items()}
        for name in names:
            value = normalized.get(normalize_text(name))
            if value:
                return value
        return ""

    @staticmethod
    def _float(value: str) -> float | None:
        text = collapse(value).replace(".", "").replace(",", ".")
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    @staticmethod
    def _reading_channel(title: str) -> tuple[str, str, str]:
        normalized = normalize_text(title)
        if "zona orara 1" in normalized:
            return "active_import_zone_1", OBIS_BY_CHANNEL["active_import_zone_1"], "kWh"
        if "zona orara 2" in normalized:
            return "active_import_zone_2", OBIS_BY_CHANNEL["active_import_zone_2"], "kWh"
        if "zona orara 3" in normalized:
            return "active_import_zone_3", OBIS_BY_CHANNEL["active_import_zone_3"], "kWh"
        if "produsa" in normalized or "prosumatori" in normalized:
            return "active_export", OBIS_BY_CHANNEL["active_export"], "kWh"
        if "capacitiva" in normalized:
            return "reactive_capacitive", OBIS_BY_CHANNEL["reactive_capacitive"], "kvarh"
        if "reactiva inductiva" in normalized:
            return "reactive_inductive", OBIS_BY_CHANNEL["reactive_inductive"], "kvarh"
        return "unknown", "", ""


async def fetch_account_snapshot(
    account: str,
    username: str,
    password: str,
    only_pods: set[str] | None = None,
    headless: bool = True,
) -> tuple[list[PodMetadata], list[MeterReading], list[LoadCurveSample]]:
    async with ReteleElectriceClient(username, password, account=account, headless=headless) as client:
        pods = await client.list_pods()
        if only_pods:
            pods = [pod for pod in pods if pod.pod in only_pods]
        metadata: list[PodMetadata] = []
        readings: list[MeterReading] = []
        curves: list[LoadCurveSample] = []
        for listed in pods:
            try:
                detail = await client.get_pod_metadata(listed.pod)
                metadata.append(_merge_metadata(listed, detail))
            except Exception:
                metadata.append(listed)
            readings.extend(await client.get_meter_readings(listed.pod))
            curves.extend(await client.get_recent_load_curve_samples(listed.pod))
            await asyncio.sleep(0.2)
        return metadata, readings, curves


def _merge_metadata(listed: PodMetadata, detail: PodMetadata) -> PodMetadata:
    values = detail.__dict__.copy()
    for key in ("city", "county", "distribution_company"):
        if not values.get(key):
            values[key] = getattr(listed, key)
    if not values.get("approved_power_kw"):
        values["approved_power_kw"] = listed.approved_power_kw
    return PodMetadata(**values)
