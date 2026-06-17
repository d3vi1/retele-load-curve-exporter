from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable
from datetime import date, datetime, time
from html.parser import HTMLParser
from typing import Any

import httpx

from .http_core import (
    AuraStatus,
    SessionState,
    VisualforcePartialStatus,
    classify_visualforce_partial_response,
    parse_visualforce_hidden_fields,
    validate_aura_response,
)
from .http_session import HttpSessionStateMachine, StateTransition
from .models import LoadCurveSample, MeterReading, PodMetadata
from .parsing import BUCHAREST, OBIS_BY_CHANNEL, decimal_ro, parse_ro_date

BASE_URL = "https://contulmeu.reteleelectrice.ro"
LOGIN_PATH = "/PEDRO_SiteLogin"
ROUTE_PATH = "/s/"
AURA_PATH = "/s/sfsites/aura"
READINGS_PATH = "/PED_ProxyCallWSAsynSingleSelf_VF"
CURVE_PATH = "/PED_ProxyCallWSAsync_Curve_VF"

ENERGY_CODE_BY_CHANNEL = {
    "active_import": "WI",
}
CHANNEL_BY_ENERGY_CODE = {code: channel for channel, code in ENERGY_CODE_BY_CHANNEL.items()}

POD_RE = re.compile(r"\bRO\d{3}E[A-Z0-9X]{6,}\b", re.I)
ERROR_MARKERS = (
    "captcha",
    "cod de verificare",
    "eroare",
    "error",
    "invalid",
    "login failed",
    "autentificare esuata",
    "sesiune expirata",
    "session expired",
)


class ReteleElectriceHttpError(RuntimeError):
    """Base error for semantic HTTP adapter failures."""


class ReteleElectriceHttpSemanticError(ReteleElectriceHttpError):
    """Raised when the portal returns HTTP 200 with an unusable payload."""


class UnsupportedEnergyChannelError(NotImplementedError):
    """Raised for Rețele Electrice curve channel codes that are not verified."""


class ReteleElectriceHttpClient:
    def __init__(
        self,
        username: str,
        password: str,
        account: str = "default",
        *,
        base_url: str = BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.username = username
        self.password = password
        self.account = account
        self._session = HttpSessionStateMachine()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=base_url, transport=transport, timeout=timeout)

    async def __aenter__(self) -> "ReteleElectriceHttpClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def session_state(self) -> SessionState:
        return self._session.state

    @property
    def session_history(self) -> tuple[StateTransition, ...]:
        return tuple(self._session.history)

    async def login(self) -> None:
        if self._session.state in {
            SessionState.FRONTDOOR_SESSION_ESTABLISHED,
            SessionState.ROUTE_BOOTSTRAPPED,
            SessionState.AURA_READY,
            SessionState.VISUALFORCE_READY,
        }:
            return

        login_page = await self._client.get(LOGIN_PATH)
        self._validate_status(login_page, "Login page")
        login_fields = parse_visualforce_hidden_fields(login_page.text)
        self._session.transition(SessionState.LOGIN_PAGE_FETCHED, note="login page fetched")

        form_data = dict(login_fields.fields)
        form_data.update({"username": self.username, "password": self.password})
        response = await self._client.post(LOGIN_PATH, data=form_data)
        self._validate_status(response, "Login")
        self._session.transition(SessionState.CREDENTIALS_POSTED, note="credentials posted")

        payload = _json_or_none(response.text)
        if isinstance(payload, dict):
            if payload.get("success") is False or payload.get("authenticated") is False:
                raise ReteleElectriceHttpSemanticError("Login failed in HTTP 200 response.")
            if _dict_has_error(payload):
                raise ReteleElectriceHttpSemanticError("Login response contains an error.")
            if payload.get("success") is True or payload.get("authenticated") is True or payload.get("sessionId"):
                self._session.transition(
                    SessionState.FRONTDOOR_SESSION_ESTABLISHED,
                    note="frontdoor session established",
                )
                return

        normalized = _normalize_text(response.text)
        if "utilizator" in normalized and "parola" in normalized:
            raise ReteleElectriceHttpSemanticError("Login response is still the login form.")
        if any(marker in normalized for marker in ERROR_MARKERS):
            raise ReteleElectriceHttpSemanticError("Login response contains an error marker.")
        if "frontdoor.jsp" in normalized or "/s/" in normalized or "autentificare reusita" in normalized:
            self._session.transition(
                SessionState.FRONTDOOR_SESSION_ESTABLISHED,
                note="frontdoor session established",
            )
            return
        raise ReteleElectriceHttpSemanticError("Login response has an unrecognized shape.")

    async def list_pods(self) -> list[PodMetadata]:
        await self._ensure_aura_ready()
        response = await self._client.post(AURA_PATH, data={"message": "listPods"})
        self._validate_aura_response(response, "POD discovery")
        return self.parse_aura_pod_discovery_response(response.text, account=self.account)

    async def get_meter_readings(self, pod: str, expected_date: date | None = None) -> list[MeterReading]:
        form_data = await self._visualforce_form_payload(READINGS_PATH, "meter readings")
        form_data["pod"] = pod
        response = await self._client.post(READINGS_PATH, data=form_data)
        self._validate_visualforce_response(response, "meter readings", success_marker=pod)
        return self.parse_visualforce_readings_table(
            response.text,
            pod=pod,
            account=self.account,
            expected_date=expected_date,
        )

    async def get_load_curve_samples(
        self,
        pod: str,
        day: date,
        *,
        channel: str = "active_import",
    ) -> list[LoadCurveSample]:
        await self._ensure_login()
        code = ENERGY_CODE_BY_CHANNEL.get(channel)
        if code is None:
            raise UnsupportedEnergyChannelError(f"Energy channel {channel!r} is not implemented for HTTP curves.")
        form_data = await self._visualforce_form_payload(CURVE_PATH, "load curve")
        form_data.update({"pod": pod, "date": day.isoformat(), "measure": code})
        response = await self._client.post(CURVE_PATH, data=form_data)
        self._validate_visualforce_response(response, "load curve", success_marker="CurveDiCaricoGraph")
        return self.parse_curve_sample_values_response(response.text, pod=pod, account=self.account, expected_date=day)

    @staticmethod
    def parse_aura_pod_discovery_response(text: str, *, account: str = "default") -> list[PodMetadata]:
        validation = validate_aura_response(200, text)
        if not validation.ok:
            raise ReteleElectriceHttpSemanticError(
                _aura_validation_message(
                    "POD discovery",
                    validation.status.value,
                    validation.action_states,
                    validation.errors,
                )
            )
        payload = _loads_portal_json(text, "POD discovery")
        records = list(_iter_pod_records(payload))
        if not records:
            raise ReteleElectriceHttpSemanticError("POD discovery response has no recognizable POD records.")

        pods: list[PodMetadata] = []
        seen: set[str] = set()
        for record in records:
            pod = _pick_pod(record)
            if not pod or pod in seen:
                continue
            seen.add(pod)
            pods.append(
                PodMetadata(
                    pod=pod,
                    account=account,
                    distribution_company=_pick(record, "distributionCompany", "distribution_company", "operator", "company"),
                    city=_pick(record, "city", "locality", "localitate"),
                    county=_pick(record, "county", "judet"),
                    address=_pick(record, "address", "adresa"),
                    approved_power_kw=_pick(record, "approvedPowerKw", "approved_power_kw", "putereAprobata"),
                    extra={str(k): str(v) for k, v in record.items() if isinstance(v, str)},
                )
            )
        if not pods:
            raise ReteleElectriceHttpSemanticError("POD discovery response contains only malformed POD records.")
        return pods

    @staticmethod
    def parse_visualforce_readings_table(
        html: str,
        *,
        pod: str,
        account: str = "default",
        expected_date: date | None = None,
    ) -> list[MeterReading]:
        _reject_error_payload(html, "meter readings")
        normalized = _normalize_text(html)
        if _normalize_text(pod) not in normalized:
            raise ReteleElectriceHttpSemanticError("Meter readings response does not contain the requested POD.")

        tables = _TableParser.parse(html)
        reading_table = _find_reading_table(tables)
        if reading_table is None:
            raise ReteleElectriceHttpSemanticError("Meter readings response has no recognized readings table.")

        header = [_collapse(cell).upper() for cell in reading_table[0]]
        readings: list[MeterReading] = []
        for row in reading_table[1:]:
            if len(row) < 5:
                continue
            read_date = parse_ro_date(row[0])
            if expected_date is not None and read_date != expected_date:
                continue
            read_at = datetime.combine(read_date, time.min, BUCHAREST)
            for index, title in enumerate(header[4:], start=4):
                if index >= len(row):
                    continue
                value = decimal_ro(row[index])
                if value is None:
                    continue
                channel, obis_code, unit = _reading_channel(title)
                readings.append(
                    MeterReading(
                        pod=pod,
                        account=account,
                        read_at=read_at,
                        meter_serial=_collapse(row[1]),
                        constant=_collapse(row[2]),
                        reading_type=_collapse(row[3]),
                        channel=channel,
                        obis_code=obis_code,
                        value=value,
                        unit=unit,
                    )
                )

        if not readings:
            if expected_date is not None:
                raise ReteleElectriceHttpSemanticError("Meter readings response has no rows for the requested date.")
            raise ReteleElectriceHttpSemanticError("Meter readings response has no parseable values.")
        return readings

    @staticmethod
    def parse_curve_sample_values_response(
        text: str,
        *,
        pod: str,
        account: str = "default",
        expected_date: date | None = None,
    ) -> list[LoadCurveSample]:
        _reject_error_payload(text, "load curve")
        payload = _loads_portal_json(text, "load curve")
        graph = _find_curve_graph(payload)
        if graph is None:
            raise ReteleElectriceHttpSemanticError("Load-curve response has no CurveDiCaricoGraph payload.")

        response_pod = _pick_pod(graph)
        if response_pod and response_pod != pod:
            raise ReteleElectriceHttpSemanticError("Load-curve response belongs to a different POD.")

        code = _pick(graph, "measure", "measureCode", "channelCode", "codMisura", "code").upper()
        channel = CHANNEL_BY_ENERGY_CODE.get(code)
        if channel is None:
            raise UnsupportedEnergyChannelError(f"Curve energy code {code or '<missing>'!r} is not implemented.")

        rows = graph.get("sampleValues")
        if not isinstance(rows, list) or not rows:
            raise ReteleElectriceHttpSemanticError("Load-curve response has no sampleValues rows.")

        samples: list[LoadCurveSample] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ReteleElectriceHttpSemanticError("Load-curve sample row has the wrong shape.")
            start_at = _sample_start_at(row, expected_date)
            if expected_date is not None and start_at.date() != expected_date:
                continue
            value = _sample_value(row)
            unit = _pick(row, "unit", "unitOfMeasure", "uom") or _pick(graph, "unit", "unitOfMeasure", "uom") or "kWh"
            interval_value, interval_unit = _active_import_interval(value, unit)
            interval_seconds = _sample_interval_seconds(row)
            samples.append(
                LoadCurveSample(
                    pod=pod,
                    account=account,
                    start_at=start_at,
                    interval_seconds=interval_seconds,
                    channel=channel,
                    obis_code=OBIS_BY_CHANNEL[channel],
                    interval_value=interval_value,
                    interval_unit=interval_unit,
                    average_value=interval_value * 3600.0 / interval_seconds,
                    average_unit="W",
                )
            )

        if not samples:
            raise ReteleElectriceHttpSemanticError("Load-curve response has no samples for the requested date.")
        return samples

    async def _ensure_login(self) -> None:
        if self._session.state == SessionState.UNAUTHENTICATED:
            await self.login()
        elif self._session.state == SessionState.EXPIRED_RELOGIN_REQUIRED:
            await self.login()

    async def _ensure_route_bootstrapped(self) -> None:
        await self._ensure_login()
        if self._session.state in {
            SessionState.ROUTE_BOOTSTRAPPED,
            SessionState.AURA_READY,
            SessionState.VISUALFORCE_READY,
        }:
            return
        self._session.require(SessionState.FRONTDOOR_SESSION_ESTABLISHED)
        response = await self._client.get(ROUTE_PATH)
        self._validate_response(response, "Route bootstrap")
        self._session.transition(SessionState.ROUTE_BOOTSTRAPPED, note="route bootstrapped")

    async def _ensure_aura_ready(self) -> None:
        await self._ensure_route_bootstrapped()
        if self._session.state == SessionState.AURA_READY:
            return
        if self._session.state == SessionState.VISUALFORCE_READY:
            self._session.transition(SessionState.AURA_READY, note="aura ready")
            return
        self._session.require(SessionState.ROUTE_BOOTSTRAPPED)
        self._session.transition(SessionState.AURA_READY, note="aura ready")

    async def _ensure_visualforce_ready(self) -> None:
        await self._ensure_route_bootstrapped()
        if self._session.state == SessionState.VISUALFORCE_READY:
            return
        if self._session.state == SessionState.AURA_READY:
            self._session.transition(SessionState.VISUALFORCE_READY, note="visualforce ready")
            return
        self._session.require(SessionState.ROUTE_BOOTSTRAPPED)
        self._session.transition(SessionState.VISUALFORCE_READY, note="visualforce ready")

    async def _visualforce_form_payload(self, path: str, label: str) -> dict[str, str]:
        await self._ensure_visualforce_ready()
        response = await self._client.get(path)
        self._validate_status(response, f"{label} form")
        hidden = parse_visualforce_hidden_fields(response.text)
        if not hidden.view_state:
            result = classify_visualforce_partial_response(response.text)
            if result.status != VisualforcePartialStatus.PARTIAL_RENDER:
                raise ReteleElectriceHttpSemanticError(
                    f"{label} form Visualforce response is not usable: {result.status.value.replace('_', ' ')}."
                )
            _reject_error_payload(response.text, f"{label} form")
            raise ReteleElectriceHttpSemanticError(f"{label} form is missing Visualforce ViewState.")
        return dict(hidden.fields)

    @staticmethod
    def _validate_status(response: httpx.Response, label: str) -> None:
        if response.status_code != 200:
            raise ReteleElectriceHttpError(f"{label} request failed with HTTP {response.status_code}.")

    @staticmethod
    def _validate_response(response: httpx.Response, label: str) -> None:
        ReteleElectriceHttpClient._validate_status(response, label)
        _reject_error_payload(response.text, label)

    def _validate_aura_response(self, response: httpx.Response, label: str) -> None:
        validation = validate_aura_response(response.status_code, response.text)
        if validation.ok:
            return
        if validation.status == AuraStatus.LOGIN_PAGE and self._session.state == SessionState.AURA_READY:
            self._session.transition(SessionState.EXPIRED_RELOGIN_REQUIRED, note="aura returned login page")
        if validation.status == AuraStatus.HTTP_ERROR:
            raise ReteleElectriceHttpError(f"{label} request failed with {', '.join(validation.errors)}.")
        raise ReteleElectriceHttpSemanticError(
            _aura_validation_message(label, validation.status.value, validation.action_states, validation.errors)
        )

    def _validate_visualforce_response(
        self,
        response: httpx.Response,
        label: str,
        *,
        success_marker: str | re.Pattern[str] | None = None,
    ) -> None:
        self._validate_status(response, label)
        text = response.text
        result = classify_visualforce_partial_response(text, success_marker=success_marker)
        if result.ok:
            return
        if result.status == VisualforcePartialStatus.PARTIAL_RENDER and success_marker is None:
            return
        if result.status == VisualforcePartialStatus.LOGIN_PAGE and self._session.state == SessionState.VISUALFORCE_READY:
            self._session.transition(
                SessionState.EXPIRED_RELOGIN_REQUIRED,
                note="visualforce returned login page",
            )
        if result.status != VisualforcePartialStatus.SEMANTIC_MARKER_MISSING:
            raise ReteleElectriceHttpSemanticError(
                f"{label} Visualforce response is not usable: {result.status.value.replace('_', ' ')}."
            )
        _reject_error_payload(text, label)
        raise ReteleElectriceHttpSemanticError(
            f"{label} Visualforce response is not usable: {result.status.value.replace('_', ' ')}."
        )


def _reject_error_payload(text: str, label: str) -> None:
    normalized = _normalize_text(text)
    if "utilizator" in normalized and "parola" in normalized:
        raise ReteleElectriceHttpSemanticError(f"{label} response is a login page.")
    if any(marker in normalized for marker in ERROR_MARKERS):
        raise ReteleElectriceHttpSemanticError(f"{label} response contains an error marker.")


def _loads_portal_json(text: str, label: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("for(;;);"):
        stripped = stripped[len("for(;;);") :].strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ReteleElectriceHttpSemanticError(f"{label} response is not valid JSON.") from exc


def _json_or_none(text: str) -> Any | None:
    stripped = text.strip()
    if stripped.startswith("for(;;);"):
        stripped = stripped[len("for(;;);") :].strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _dict_has_error(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if _normalize_text(key) in {"error", "errors", "exception"} and item:
                return True
            if _dict_has_error(item):
                return True
    if isinstance(value, list):
        return any(_dict_has_error(item) for item in value)
    if isinstance(value, str):
        return any(marker in _normalize_text(value) for marker in ERROR_MARKERS)
    return False


def _aura_validation_message(
    label: str,
    status: str,
    action_states: tuple[str, ...] = (),
    errors: tuple[str, ...] = (),
) -> str:
    parts = [f"{label} Aura response is not usable: {status.replace('_', ' ')}"]
    if action_states:
        parts.append(f"states={','.join(action_states)}")
    if errors:
        parts.append(f"errors={'; '.join(errors)}")
    if len(parts) == 1:
        return f"{parts[0]}."
    return f"{parts[0]} ({'; '.join(parts[1:])})."


def _iter_pod_records(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        if _pick_pod(value):
            yield value
        for item in value.values():
            yield from _iter_pod_records(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_pod_records(item)


def _pick_pod(record: dict[str, Any]) -> str:
    for key in ("pod", "POD", "podId", "podNumber", "podCode", "codPod", "CodPOD", "pointOfDelivery"):
        value = record.get(key)
        if isinstance(value, str):
            match = POD_RE.search(value)
            if match:
                return match.group(0).upper()
    for value in record.values():
        if isinstance(value, str):
            match = POD_RE.search(value)
            if match:
                return match.group(0).upper()
    return ""


def _pick(record: dict[str, Any], *keys: str) -> str:
    wanted = {_normalize_key(key) for key in keys}
    for key, value in record.items():
        if _normalize_key(str(key)) in wanted and value is not None:
            return _collapse(str(value))
    return ""


def _find_curve_graph(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        if isinstance(value.get("sampleValues"), list):
            return value
        for key, item in value.items():
            if key == "CurveDiCaricoGraph" and isinstance(item, dict):
                return item
            found = _find_curve_graph(item)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_curve_graph(item)
            if found is not None:
                return found
    return None


def _sample_start_at(row: dict[str, Any], expected_date: date | None) -> datetime:
    for key in ("startAt", "start_at", "timestamp", "dateTime", "datetime"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=BUCHAREST)

    day_text = _pick(row, "date", "day", "sampleDate", "data")
    day = _parse_portal_date(day_text) if day_text else expected_date
    if day is None:
        raise ReteleElectriceHttpSemanticError("Load-curve sample row has no date.")

    quarter = _pick(row, "quarter", "q", "slot", "interval")
    match = re.fullmatch(r"Q?(\d+)", quarter, re.I)
    if not match:
        raise ReteleElectriceHttpSemanticError("Load-curve sample row has no quarter index.")
    index = int(match.group(1))
    if not 1 <= index <= 96:
        raise ReteleElectriceHttpSemanticError("Load-curve sample quarter index is out of range.")
    return datetime.combine(day, time.min, BUCHAREST).replace(minute=15 * ((index - 1) % 4), hour=(index - 1) // 4)


def _sample_value(row: dict[str, Any]) -> float:
    for key in ("value", "sampleValue", "valore", "consumption", "energy"):
        if key in row:
            parsed = decimal_ro(str(row[key]))
            if parsed is not None:
                return parsed
    raise ReteleElectriceHttpSemanticError("Load-curve sample row has no numeric value.")


def _parse_portal_date(value: str) -> date:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        return parse_ro_date(text)


def _sample_interval_seconds(row: dict[str, Any]) -> int:
    raw = _pick(row, "intervalSeconds", "interval_seconds")
    if raw:
        try:
            interval = int(raw)
        except ValueError as exc:
            raise ReteleElectriceHttpSemanticError("Load-curve interval is not numeric.") from exc
        if interval <= 0:
            raise ReteleElectriceHttpSemanticError("Load-curve interval must be positive.")
        return interval
    return 900


def _active_import_interval(value: float, unit: str) -> tuple[float, str]:
    normalized = _normalize_text(unit)
    if normalized == "wh":
        return value, "Wh"
    if normalized in {"kwh", ""}:
        return value * 1000.0, "Wh"
    raise ReteleElectriceHttpSemanticError(f"Unsupported active_import curve unit: {unit!r}.")


def _find_reading_table(tables: list[list[list[str]]]) -> list[list[str]] | None:
    for table in tables:
        if not table:
            continue
        header = [_normalize_text(cell) for cell in table[0]]
        if any("data citirii" in cell for cell in header) and any("serie de contor" in cell for cell in header):
            return table
    return None


def _reading_channel(title: str) -> tuple[str, str, str]:
    normalized = _normalize_text(title)
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


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", _normalize_text(value))


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFD", str(value or "").replace("\u00a0", " "))
    without_marks = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    return _collapse(without_marks).casefold()


def _collapse(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    @classmethod
    def parse(cls, html: str) -> list[list[list[str]]]:
        parser = cls()
        parser.feed(html)
        return parser.tables

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"th", "td"} and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"th", "td"} and self._cell is not None and self._row is not None:
            self._row.append(_collapse("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if any(cell for cell in self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)
