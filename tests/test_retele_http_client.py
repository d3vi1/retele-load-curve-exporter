import asyncio
from datetime import date
from pathlib import Path

import httpx
import pytest

from dso_retele_electrice.http_client import (
    AURA_PATH,
    CURVE_PATH,
    ENERGY_CODE_BY_CHANNEL,
    LOGIN_PATH,
    READINGS_PATH,
    ReteleElectriceHttpClient,
    ReteleElectriceHttpSemanticError,
    UnsupportedEnergyChannelError,
)

FIXTURES = Path(__file__).parent / "fixtures" / "retele_http_client"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_login_and_list_pods_use_httpx_transport_and_parse_aura_response():
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == LOGIN_PATH:
            return httpx.Response(200, text=fixture("login_success.json"))
        if request.url.path == AURA_PATH:
            return httpx.Response(200, text=fixture("aura_pods_success.json"))
        raise AssertionError(f"unexpected request: {request.url}")

    async def run():
        async with ReteleElectriceHttpClient(
            "sanitized-user",
            "sanitized-password",
            account="main",
            transport=httpx.MockTransport(handler),
        ) as client:
            return await client.list_pods()

    pods = asyncio.run(run())

    assert requested_paths == [LOGIN_PATH, AURA_PATH]
    assert [pod.pod for pod in pods] == ["RO001EXXXXXXXXX", "RO001EYYYYYYYYY"]
    assert pods[0].account == "main"
    assert pods[0].city == "REDACTED_CITY"
    assert pods[0].approved_power_kw == "42"


def test_login_rejects_http_200_error_payload():
    async def run():
        async with ReteleElectriceHttpClient(
            "sanitized-user",
            "sanitized-password",
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, text=fixture("login_error.json"))),
        ) as client:
            await client.login()

    with pytest.raises(ReteleElectriceHttpSemanticError, match="Login"):
        asyncio.run(run())


def test_aura_parser_rejects_login_page_and_wrong_shape():
    with pytest.raises(ReteleElectriceHttpSemanticError, match="login page"):
        ReteleElectriceHttpClient.parse_aura_pod_discovery_response("<label>Utilizator</label><input><label>Parola</label>")

    with pytest.raises(ReteleElectriceHttpSemanticError, match="no recognizable POD"):
        ReteleElectriceHttpClient.parse_aura_pod_discovery_response('{"actions":[{"state":"SUCCESS","returnValue":{}}]}')


def test_parse_visualforce_readings_table_to_meter_readings():
    readings = ReteleElectriceHttpClient.parse_visualforce_readings_table(
        fixture("readings_visualforce.html"),
        pod="RO001EXXXXXXXXX",
        account="main",
        expected_date=date(2026, 6, 1),
    )

    assert len(readings) == 2
    assert readings[0].pod == "RO001EXXXXXXXXX"
    assert readings[0].meter_serial == "REDACTED_METER_SERIAL"
    assert readings[0].read_at.isoformat() == "2026-06-01T00:00:00+03:00"
    assert readings[0].channel == "active_import_zone_1"
    assert readings[0].obis_code == "1.8.1"
    assert readings[0].value == 12345.67
    assert readings[0].unit == "kWh"
    assert readings[1].channel == "active_export"
    assert readings[1].value == 0.5


def test_parse_visualforce_readings_rejects_wrong_pod_date_and_shape():
    with pytest.raises(ReteleElectriceHttpSemanticError, match="requested POD"):
        ReteleElectriceHttpClient.parse_visualforce_readings_table(
            fixture("readings_visualforce.html"),
            pod="RO001EZZZZZZZZZ",
        )

    with pytest.raises(ReteleElectriceHttpSemanticError, match="requested date"):
        ReteleElectriceHttpClient.parse_visualforce_readings_table(
            fixture("readings_visualforce.html"),
            pod="RO001EXXXXXXXXX",
            expected_date=date(2026, 6, 2),
        )

    with pytest.raises(ReteleElectriceHttpSemanticError, match="readings table"):
        ReteleElectriceHttpClient.parse_visualforce_readings_table(
            "<html><body>POD: RO001EXXXXXXXXX<table><tr><td>not readings</td></tr></table></body></html>",
            pod="RO001EXXXXXXXXX",
        )


def test_parse_curve_sample_values_wi_to_active_import_samples():
    assert ENERGY_CODE_BY_CHANNEL == {"active_import": "WI"}

    samples = ReteleElectriceHttpClient.parse_curve_sample_values_response(
        fixture("curve_samples_wi.json"),
        pod="RO001EXXXXXXXXX",
        account="main",
        expected_date=date(2026, 6, 1),
    )

    assert len(samples) == 2
    assert samples[0].pod == "RO001EXXXXXXXXX"
    assert samples[0].account == "main"
    assert samples[0].start_at.isoformat() == "2026-06-01T00:00:00+03:00"
    assert samples[0].interval_seconds == 900
    assert samples[0].channel == "active_import"
    assert samples[0].obis_code == "1.8.0"
    assert samples[0].interval_value == 306.0
    assert samples[0].interval_unit == "Wh"
    assert samples[0].average_value == 1224.0
    assert samples[0].average_unit == "W"
    assert samples[1].start_at.isoformat() == "2026-06-01T00:15:00+03:00"


def test_parse_curve_sample_values_rejects_wrong_pod_date_shape_and_unknown_channel_code():
    with pytest.raises(ReteleElectriceHttpSemanticError, match="different POD"):
        ReteleElectriceHttpClient.parse_curve_sample_values_response(
            fixture("curve_samples_wi.json"),
            pod="RO001EZZZZZZZZZ",
            expected_date=date(2026, 6, 1),
        )

    with pytest.raises(ReteleElectriceHttpSemanticError, match="requested date"):
        ReteleElectriceHttpClient.parse_curve_sample_values_response(
            fixture("curve_samples_wi.json"),
            pod="RO001EXXXXXXXXX",
            expected_date=date(2026, 6, 2),
        )

    with pytest.raises(ReteleElectriceHttpSemanticError, match="CurveDiCaricoGraph"):
        ReteleElectriceHttpClient.parse_curve_sample_values_response(
            '{"status":"OK"}',
            pod="RO001EXXXXXXXXX",
            expected_date=date(2026, 6, 1),
        )

    with pytest.raises(UnsupportedEnergyChannelError, match="not implemented"):
        ReteleElectriceHttpClient.parse_curve_sample_values_response(
            fixture("curve_samples_wi.json").replace('"WI"', '"WA"'),
            pod="RO001EXXXXXXXXX",
            expected_date=date(2026, 6, 1),
        )


def test_http_methods_parse_fixture_backed_readings_and_curves_without_browser():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == LOGIN_PATH:
            return httpx.Response(200, text=fixture("login_success.json"))
        if request.url.path == READINGS_PATH:
            return httpx.Response(200, text=fixture("readings_visualforce.html"))
        if request.url.path == CURVE_PATH:
            form = dict(httpx.QueryParams(request.content.decode()))
            assert form["measure"] == "WI"
            return httpx.Response(200, text=fixture("curve_samples_wi.json"))
        raise AssertionError(f"unexpected request: {request.url}")

    async def run():
        async with ReteleElectriceHttpClient(
            "sanitized-user",
            "sanitized-password",
            account="main",
            transport=httpx.MockTransport(handler),
        ) as client:
            readings = await client.get_meter_readings("RO001EXXXXXXXXX", expected_date=date(2026, 6, 1))
            samples = await client.get_load_curve_samples("RO001EXXXXXXXXX", date(2026, 6, 1))
            return readings, samples

    readings, samples = asyncio.run(run())

    assert readings[0].channel == "active_import_zone_1"
    assert samples[0].channel == "active_import"
