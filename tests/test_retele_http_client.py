import asyncio
from datetime import date
from pathlib import Path

import httpx
import pytest

import dso_load_curves_exporter.__main__ as exporter
import dso_retele_electrice.http_client as http_client
from dso_retele_electrice.http_client import (
    AURA_PATH,
    CURVE_PATH,
    ENERGY_CODE_BY_CHANNEL,
    LOGIN_PATH,
    POD_INFO_PATH,
    READINGS_PATH,
    ROUTE_PATH,
    ReteleElectriceHttpClient,
    ReteleElectriceHttpSemanticError,
    UnsupportedEnergyChannelError,
)
from dso_retele_electrice.http_core import SessionState

FIXTURES = Path(__file__).parent / "fixtures" / "retele_http_client"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_login_and_list_pods_use_httpx_transport_and_parse_aura_response():
    requested_paths: list[str] = []
    posted_login_form: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(f"{request.method} {request.url.path}")
        if request.method == "GET" and request.url.path == LOGIN_PATH:
            return httpx.Response(200, text=fixture("login_page.html"))
        if request.method == "POST" and request.url.path == LOGIN_PATH:
            posted_login_form.update(httpx.QueryParams(request.content.decode()))
            return httpx.Response(200, text=fixture("login_success.json"))
        if request.method == "GET" and request.url.path == ROUTE_PATH:
            return httpx.Response(200, text="<html><body>Salesforce route shell</body></html>")
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
            pods = await client.list_pods()
            return pods, client.session_state, [item.to_state for item in client.session_history]

    pods, session_state, transitions = asyncio.run(run())

    assert requested_paths == [
        f"GET {LOGIN_PATH}",
        f"POST {LOGIN_PATH}",
        f"GET {ROUTE_PATH}",
        f"POST {AURA_PATH}",
    ]
    assert posted_login_form["loginCsrf"] == "SANITIZED_LOGIN_CSRF"
    assert posted_login_form["username"] == "sanitized-user"
    assert posted_login_form["password"] == "sanitized-password"
    assert session_state == SessionState.AURA_READY
    assert transitions == [
        SessionState.LOGIN_PAGE_FETCHED,
        SessionState.CREDENTIALS_POSTED,
        SessionState.FRONTDOOR_SESSION_ESTABLISHED,
        SessionState.ROUTE_BOOTSTRAPPED,
        SessionState.AURA_READY,
    ]
    assert [pod.pod for pod in pods] == ["RO001EXXXXXXXXX", "RO001EYYYYYYYYY"]
    assert pods[0].account == "main"
    assert pods[0].city == "REDACTED_CITY"
    assert pods[0].approved_power_kw == "42"


def test_login_follows_javascript_frontdoor_redirect_before_marking_session_established():
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(f"{request.method} {request.url.path}")
        if request.method == "GET" and request.url.path == LOGIN_PATH:
            return httpx.Response(200, text=fixture("login_page_visualforce_prefixed.html"))
        if request.method == "POST" and request.url.path == LOGIN_PATH:
            return httpx.Response(200, text=fixture("login_frontdoor_js_redirect.html"))
        if request.method == "GET" and request.url.path == "/secur/frontdoor.jsp":
            assert request.url.params["sid"] == "SANITIZED_SESSION_ID"
            return httpx.Response(302, headers={"Location": ROUTE_PATH, "Set-Cookie": "sid=SANITIZED_SESSION_ID"})
        if request.method == "GET" and request.url.path == ROUTE_PATH:
            return httpx.Response(200, text="<html><body>Salesforce route shell</body></html>")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async def run():
        async with ReteleElectriceHttpClient(
            "sanitized-user",
            "sanitized-password",
            transport=httpx.MockTransport(handler),
        ) as client:
            await client.login()
            return client.session_state, [item.note for item in client.session_history]

    session_state, notes = asyncio.run(run())

    assert requested_paths == [
        f"GET {LOGIN_PATH}",
        f"POST {LOGIN_PATH}",
        "GET /secur/frontdoor.jsp",
        f"GET {ROUTE_PATH}",
    ]
    assert session_state == SessionState.FRONTDOOR_SESSION_ESTABLISHED
    assert all("SANITIZED_SESSION_ID" not in note for note in notes)


def test_login_rejects_http_200_error_payload():
    async def run():
        async with ReteleElectriceHttpClient(
            "sanitized-user",
            "sanitized-password",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    text=fixture("login_page.html") if request.method == "GET" else fixture("login_error.json"),
                )
            ),
        ) as client:
            await client.login()

    with pytest.raises(ReteleElectriceHttpSemanticError, match="Login"):
        asyncio.run(run())


def test_login_posts_visualforce_prefixed_fields_and_follows_frontdoor_redirect():
    requested_paths: list[str] = []
    posted_login_form: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(f"{request.method} {request.url.path}")
        if request.method == "GET" and request.url.path == LOGIN_PATH:
            return httpx.Response(200, text=fixture("login_page_visualforce_prefixed.html"))
        if request.method == "POST" and request.url.path == LOGIN_PATH:
            posted_login_form.update(httpx.QueryParams(request.content.decode()))
            return httpx.Response(302, headers={"Location": "/secur/frontdoor.jsp?sid=SANITIZED_SESSION_ID"})
        if request.method == "GET" and request.url.path == "/secur/frontdoor.jsp":
            return httpx.Response(302, headers={"Location": ROUTE_PATH})
        if request.method == "GET" and request.url.path == ROUTE_PATH:
            return httpx.Response(200, text="<html><body>Salesforce route shell</body></html>")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async def run():
        async with ReteleElectriceHttpClient(
            "sanitized-user",
            "sanitized-password",
            transport=httpx.MockTransport(handler),
        ) as client:
            await client.login()
            return client.session_state, [item.to_state for item in client.session_history]

    session_state, transitions = asyncio.run(run())

    assert requested_paths == [
        f"GET {LOGIN_PATH}",
        f"POST {LOGIN_PATH}",
        "GET /secur/frontdoor.jsp",
        f"GET {ROUTE_PATH}",
    ]
    assert posted_login_form["loginPage:loginForm"] == "loginPage:loginForm"
    assert posted_login_form["loginPage:loginForm:j_id25"] == "loginPage:loginForm:j_id25"
    assert posted_login_form["com.salesforce.visualforce.ViewState"] == "SANITIZED_VIEWSTATE"
    assert posted_login_form["com.salesforce.visualforce.ViewStateVersion"] == "SANITIZED_VIEWSTATE_VERSION"
    assert posted_login_form["com.salesforce.visualforce.ViewStateMAC"] == "SANITIZED_VIEWSTATE_MAC"
    assert posted_login_form["loginPage:loginForm:username"] == "sanitized-user"
    assert posted_login_form["loginPage:loginForm:password"] == "sanitized-password"
    assert "username" not in posted_login_form
    assert "password" not in posted_login_form
    assert session_state == SessionState.FRONTDOOR_SESSION_ESTABLISHED
    assert transitions == [
        SessionState.LOGIN_PAGE_FETCHED,
        SessionState.CREDENTIALS_POSTED,
        SessionState.FRONTDOOR_SESSION_ESTABLISHED,
    ]


def test_aura_parser_rejects_login_page_and_wrong_shape():
    with pytest.raises(ReteleElectriceHttpSemanticError, match="login page"):
        ReteleElectriceHttpClient.parse_aura_pod_discovery_response(
            '<html><form action="/login"><input name="username"><input type="password" name="pw">AUTENTIFIC</form></html>'
        )

    with pytest.raises(ReteleElectriceHttpSemanticError, match="no recognizable POD"):
        ReteleElectriceHttpClient.parse_aura_pod_discovery_response('{"actions":[{"state":"SUCCESS","returnValue":{}}]}')


@pytest.mark.parametrize("state", ["INCOMPLETE", "ERROR"])
def test_aura_parser_rejects_non_success_actions_even_with_pod_like_data(state):
    body = (
        'for(;;);{"actions":[{"state":"'
        + state
        + '","returnValue":{"pods":[{"pod":"RO001EXXXXXXXXX"}]},"error":[{"message":"sanitized failure"}]}]}'
    )

    with pytest.raises(ReteleElectriceHttpSemanticError, match=state):
        ReteleElectriceHttpClient.parse_aura_pod_discovery_response(body)


def test_get_pod_metadata_fetches_direct_http_endpoint_and_parses_label_values():
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(f"{request.method} {request.url.path}")
        if request.method == "GET" and request.url.path == LOGIN_PATH:
            return httpx.Response(200, text=fixture("login_page.html"))
        if request.method == "POST" and request.url.path == LOGIN_PATH:
            return httpx.Response(200, text=fixture("login_success.json"))
        if request.method == "GET" and request.url.path == POD_INFO_PATH:
            assert request.url.params["pod"] == "RO001EXXXXXXXXX"
            return httpx.Response(200, text=fixture("pod_metadata.html"))
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async def run():
        async with ReteleElectriceHttpClient(
            "sanitized-user",
            "sanitized-password",
            account="main",
            transport=httpx.MockTransport(handler),
        ) as client:
            return await client.get_pod_metadata("RO001EXXXXXXXXX")

    metadata = asyncio.run(run())

    assert requested_paths == [
        f"GET {LOGIN_PATH}",
        f"POST {LOGIN_PATH}",
        f"GET {POD_INFO_PATH}",
    ]
    assert metadata.pod == "RO001EXXXXXXXXX"
    assert metadata.account == "main"
    assert metadata.supplier == "SANITIZED_SUPPLIER"
    assert metadata.balancing_responsible_party == "SANITIZED_PRE"
    assert metadata.customer_name == "SANITIZED_CUSTOMER"
    assert metadata.approved_power_kw == "42"
    assert metadata.address == "REDACTED_STREET 1, REDACTED_CITY"
    assert metadata.atr_cer_number == "123456"
    assert metadata.atr_cer_date == "01.02.2024"
    assert metadata.voltage_level == "0,4 kV"
    assert metadata.delimitation_voltage == "0,4 kV"
    assert metadata.meter_status == "Activ"
    assert metadata.meter_serial == "REDACTED_METER_SERIAL"
    assert metadata.smartmeter_id == "REDACTED_METER_SERIAL"
    assert metadata.meter_brand == "SANITIZED_BRAND"
    assert metadata.meter_type == "SANITIZED_TYPE"
    assert metadata.interval == "15 min"
    assert metadata.accuracy_class == "Clasa B"
    assert metadata.mount_date == "03.04.2024"
    assert metadata.constant == "1"
    assert metadata.extra["Furnizor"] == "SANITIZED_SUPPLIER"


def test_http_configured_pods_bypass_aura_and_keep_metadata_on_per_pod_data_failure(monkeypatch):
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(f"{request.method} {request.url.path}")
        if request.url.path == AURA_PATH:
            raise AssertionError("configured POD runtime must not call Aura discovery")
        if request.method == "GET" and request.url.path == LOGIN_PATH:
            return httpx.Response(200, text=fixture("login_page.html"))
        if request.method == "POST" and request.url.path == LOGIN_PATH:
            return httpx.Response(200, text=fixture("login_success.json"))
        if request.method == "GET" and request.url.path == POD_INFO_PATH:
            if request.url.params["pod"] == "RO001EXXXXXXXXX":
                return httpx.Response(200, text=fixture("pod_metadata.html"))
            return httpx.Response(500, text="sanitized metadata failure")
        if request.method == "GET" and request.url.path == ROUTE_PATH:
            return httpx.Response(200, text="<html><body>Salesforce route shell</body></html>")
        if request.method == "GET" and request.url.path in {READINGS_PATH, CURVE_PATH}:
            return httpx.Response(200, text=fixture("visualforce_bootstrap.html"))
        if request.method == "POST" and request.url.path == READINGS_PATH:
            form = dict(httpx.QueryParams(request.content.decode()))
            if form["pod"] == "RO001EXXXXXXXXX":
                return httpx.Response(200, text=fixture("readings_visualforce.html"))
            return httpx.Response(500, text="sanitized readings failure")
        if request.method == "POST" and request.url.path == CURVE_PATH:
            form = dict(httpx.QueryParams(request.content.decode()))
            if form["pod"] == "RO001EXXXXXXXXX":
                return httpx.Response(200, text=fixture("curve_samples_wi.json"))
            return httpx.Response(500, text="sanitized curve failure")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    class TestClient(ReteleElectriceHttpClient):
        def __init__(self, username, password, account="default"):
            super().__init__(
                username,
                password,
                account=account,
                transport=httpx.MockTransport(handler),
            )

    monkeypatch.setattr(http_client, "ReteleElectriceHttpClient", TestClient)

    async def run():
        return await exporter._fetch_account_snapshot_http(
            "main",
            "sanitized-user",
            "sanitized-password",
            only_pods={"RO001EXXXXXXXXX", "RO001EYYYYYYYYY"},
        )

    metadata, readings, curves = asyncio.run(run())

    assert f"POST {AURA_PATH}" not in requested_paths
    assert [item.pod for item in metadata] == ["RO001EXXXXXXXXX", "RO001EYYYYYYYYY"]
    assert metadata[0].supplier == "SANITIZED_SUPPLIER"
    assert metadata[1].account == "main"
    assert metadata[1].supplier == ""
    assert [item.pod for item in readings] == ["RO001EXXXXXXXXX", "RO001EXXXXXXXXX"]
    assert curves == []


def test_http_configured_pods_raise_when_every_data_fetch_fails(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == AURA_PATH:
            raise AssertionError("configured POD runtime must not call Aura discovery")
        if request.method == "GET" and request.url.path == LOGIN_PATH:
            return httpx.Response(200, text=fixture("login_page.html"))
        if request.method == "POST" and request.url.path == LOGIN_PATH:
            return httpx.Response(200, text=fixture("login_success.json"))
        if request.method == "GET" and request.url.path == POD_INFO_PATH:
            return httpx.Response(500, text="sanitized metadata failure")
        if request.method == "GET" and request.url.path == ROUTE_PATH:
            return httpx.Response(200, text="<html><body>Salesforce route shell</body></html>")
        if request.method == "GET" and request.url.path in {READINGS_PATH, CURVE_PATH}:
            return httpx.Response(200, text=fixture("visualforce_bootstrap.html"))
        if request.method == "POST" and request.url.path in {READINGS_PATH, CURVE_PATH}:
            return httpx.Response(500, text="sanitized data failure")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    class TestClient(ReteleElectriceHttpClient):
        def __init__(self, username, password, account="default"):
            super().__init__(
                username,
                password,
                account=account,
                transport=httpx.MockTransport(handler),
            )

    monkeypatch.setattr(http_client, "ReteleElectriceHttpClient", TestClient)

    async def run():
        return await exporter._fetch_account_snapshot_http(
            "main",
            "sanitized-user",
            "sanitized-password",
            only_pods={"RO001EXXXXXXXXX"},
        )

    with pytest.raises(RuntimeError, match="All configured POD fetches failed"):
        asyncio.run(run())


def test_http_configured_pods_publish_metadata_when_all_data_fetches_fail(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == AURA_PATH:
            raise AssertionError("configured POD runtime must not call Aura discovery")
        if request.method == "GET" and request.url.path == LOGIN_PATH:
            return httpx.Response(200, text=fixture("login_page.html"))
        if request.method == "POST" and request.url.path == LOGIN_PATH:
            return httpx.Response(200, text=fixture("login_success.json"))
        if request.method == "GET" and request.url.path == POD_INFO_PATH:
            return httpx.Response(200, text=fixture("pod_metadata.html"))
        if request.method == "GET" and request.url.path == ROUTE_PATH:
            return httpx.Response(200, text="<html><body>Salesforce route shell with error keyword</body></html>")
        if request.method == "GET" and request.url.path in {READINGS_PATH, CURVE_PATH}:
            return httpx.Response(200, text=fixture("visualforce_bootstrap.html"))
        if request.method == "POST" and request.url.path in {READINGS_PATH, CURVE_PATH}:
            return httpx.Response(500, text="sanitized data failure")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    class TestClient(ReteleElectriceHttpClient):
        def __init__(self, username, password, account="default"):
            super().__init__(
                username,
                password,
                account=account,
                transport=httpx.MockTransport(handler),
            )

    monkeypatch.setattr(http_client, "ReteleElectriceHttpClient", TestClient)

    async def run():
        return await exporter._fetch_account_snapshot_http(
            "main",
            "sanitized-user",
            "sanitized-password",
            only_pods={"RO001EXXXXXXXXX"},
        )

    metadata, readings, curves = asyncio.run(run())

    assert len(metadata) == 1
    assert metadata[0].supplier == "SANITIZED_SUPPLIER"
    assert readings == []
    assert curves == []


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
    submitted_visualforce_forms: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == LOGIN_PATH:
            return httpx.Response(200, text=fixture("login_page.html"))
        if request.method == "POST" and request.url.path == LOGIN_PATH:
            return httpx.Response(200, text=fixture("login_success.json"))
        if request.method == "GET" and request.url.path == ROUTE_PATH:
            return httpx.Response(200, text="<html><body>Salesforce route shell</body></html>")
        if request.method == "GET" and request.url.path in {READINGS_PATH, CURVE_PATH}:
            return httpx.Response(200, text=fixture("visualforce_bootstrap.html"))
        if request.method == "POST" and request.url.path == READINGS_PATH:
            submitted_visualforce_forms.append(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(200, text=fixture("readings_visualforce.html"))
        if request.method == "POST" and request.url.path == CURVE_PATH:
            form = dict(httpx.QueryParams(request.content.decode()))
            submitted_visualforce_forms.append(form)
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
            return readings, samples, client.session_state

    readings, samples, session_state = asyncio.run(run())

    assert readings[0].channel == "active_import_zone_1"
    assert samples[0].channel == "active_import"
    assert submitted_visualforce_forms[0]["com.salesforce.visualforce.ViewState"] == "SANITIZED_VIEWSTATE"
    assert submitted_visualforce_forms[0]["pod"] == "RO001EXXXXXXXXX"
    assert submitted_visualforce_forms[1]["com.salesforce.visualforce.ViewState"] == "SANITIZED_VIEWSTATE"
    assert session_state == SessionState.VISUALFORCE_READY


def test_visualforce_form_bootstrap_rejects_missing_viewstate():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == LOGIN_PATH:
            return httpx.Response(200, text=fixture("login_page.html"))
        if request.method == "POST" and request.url.path == LOGIN_PATH:
            return httpx.Response(200, text=fixture("login_success.json"))
        if request.method == "GET" and request.url.path == ROUTE_PATH:
            return httpx.Response(200, text="<html><body>Salesforce route shell</body></html>")
        if request.method == "GET" and request.url.path == READINGS_PATH:
            return httpx.Response(200, text="<html><form><input type='hidden' name='apex.submit' value='1'></form></html>")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async def run():
        async with ReteleElectriceHttpClient(
            "sanitized-user",
            "sanitized-password",
            transport=httpx.MockTransport(handler),
        ) as client:
            await client.get_meter_readings("RO001EXXXXXXXXX")

    with pytest.raises(ReteleElectriceHttpSemanticError, match="ViewState"):
        asyncio.run(run())


def test_visualforce_post_rejects_login_page_masquerading_as_200():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == LOGIN_PATH:
            return httpx.Response(200, text=fixture("login_page.html"))
        if request.method == "POST" and request.url.path == LOGIN_PATH:
            return httpx.Response(200, text=fixture("login_success.json"))
        if request.method == "GET" and request.url.path == ROUTE_PATH:
            return httpx.Response(200, text="<html><body>Salesforce route shell</body></html>")
        if request.method == "GET" and request.url.path == READINGS_PATH:
            return httpx.Response(200, text=fixture("visualforce_bootstrap.html"))
        if request.method == "POST" and request.url.path == READINGS_PATH:
            return httpx.Response(
                200,
                text='<html><form action="/login"><input name="username"><input type="password" name="pw">AUTENTIFIC</form></html>',
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async def run():
        async with ReteleElectriceHttpClient(
            "sanitized-user",
            "sanitized-password",
            transport=httpx.MockTransport(handler),
        ) as client:
            await client.get_meter_readings("RO001EXXXXXXXXX")

    with pytest.raises(ReteleElectriceHttpSemanticError, match="login page"):
        asyncio.run(run())
