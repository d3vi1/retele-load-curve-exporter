from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

BUCHAREST = ZoneInfo("Europe/Bucharest")

OBIS_BY_CHANNEL = {
    "active_import": "1.8.0",
    "active_import_zone_1": "1.8.1",
    "active_import_zone_2": "1.8.2",
    "active_import_zone_3": "1.8.3",
    "active_export": "2.8.0",
    "reactive_inductive": "5.8.0",
    "reactive_capacitive": "8.8.0",
}


def decimal_ro(value: str) -> float | None:
    text = str(value or "").strip().strip('"').replace(".", "").replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_ro_date(value: str) -> date:
    text = str(value or "").strip().strip('"')
    for fmt in ("%Y.%m.%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Unsupported Romanian date: {value!r}")


def split_atr_cer(value: str) -> tuple[str, str]:
    text = str(value or "").strip()
    if "/" not in text:
        return text, ""
    number, when = text.split("/", 1)
    return number.strip(), when.strip()


def q_start(day: date, q_name: str) -> datetime:
    match = re.fullmatch(r"Q(\d+)", q_name)
    if not match:
        raise ValueError(f"Not a Q column: {q_name}")
    index = int(match.group(1))
    if not 1 <= index <= 96:
        raise ValueError(f"Q column out of range: {q_name}")
    return datetime.combine(day, time(0, 0), BUCHAREST) + timedelta(minutes=15 * (index - 1))


def parse_load_curve_csv(text: str) -> list[tuple[datetime, str, float]]:
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    rows: list[tuple[datetime, str, float]] = []
    for row in reader:
        day = parse_ro_date(row.get("Zi", ""))
        for key, raw in row.items():
            if not key or not key.startswith("Q"):
                continue
            value = decimal_ro(raw)
            if value is None:
                continue
            rows.append((q_start(day, key), key, value))
    return rows
