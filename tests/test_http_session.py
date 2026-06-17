import re

import pytest

from dso_retele_electrice.http_core import (
    AuraStatus,
    SessionState,
    VisualforcePartialStatus,
    classify_visualforce_partial_response,
    parse_visualforce_hidden_fields,
    validate_aura_response,
)
from dso_retele_electrice.http_session import HttpSessionStateMachine


def test_session_state_machine_accepts_expected_login_path():
    machine = HttpSessionStateMachine()

    machine.transition(SessionState.LOGIN_PAGE_FETCHED, note="login form discovered")
    machine.transition(SessionState.CREDENTIALS_POSTED)
    machine.transition(SessionState.FRONTDOOR_SESSION_ESTABLISHED)
    machine.transition(SessionState.ROUTE_BOOTSTRAPPED)
    machine.transition(SessionState.AURA_READY)
    machine.transition(SessionState.VISUALFORCE_READY)

    assert machine.state == SessionState.VISUALFORCE_READY
    assert [item.to_state for item in machine.history] == [
        SessionState.LOGIN_PAGE_FETCHED,
        SessionState.CREDENTIALS_POSTED,
        SessionState.FRONTDOOR_SESSION_ESTABLISHED,
        SessionState.ROUTE_BOOTSTRAPPED,
        SessionState.AURA_READY,
        SessionState.VISUALFORCE_READY,
    ]


def test_session_state_machine_rejects_skipped_login_path():
    machine = HttpSessionStateMachine()

    with pytest.raises(ValueError, match="unauthenticated -> aura_ready"):
        machine.transition(SessionState.AURA_READY)


def test_session_state_machine_marks_captcha_as_terminal():
    machine = HttpSessionStateMachine(SessionState.LOGIN_PAGE_FETCHED)

    machine.transition(SessionState.BLOCKED_CAPTCHA_MFA)

    assert machine.terminal
    with pytest.raises(ValueError):
        machine.transition(SessionState.CREDENTIALS_POSTED)


def test_parse_visualforce_hidden_fields_extracts_viewstate_and_generic_fields():
    html = """
    <form>
      <input type="hidden" name="com.salesforce.visualforce.ViewState" value="VS_TOKEN">
      <input type="hidden" name="com.salesforce.visualforce.ViewStateVersion" value="VSV">
      <input type="hidden" name="com.salesforce.visualforce.ViewStateMAC" value="MAC">
      <input type="hidden" name="com.salesforce.visualforce.ViewStateCSRF" value="CSRF">
      <input type="hidden" name="apex.submit" value="1">
      <input type="text" name="visible" value="ignored">
    </form>
    """

    fields = parse_visualforce_hidden_fields(html)

    assert fields.view_state == "VS_TOKEN"
    assert fields.view_state_version == "VSV"
    assert fields.view_state_mac == "MAC"
    assert fields.view_state_csrf == "CSRF"
    assert fields.fields["apex.submit"] == "1"
    assert "visible" not in fields.fields


def test_validate_aura_response_success_requires_success_actions():
    result = validate_aura_response(
        200,
        'for(;;);{"actions":[{"id":"1;a","state":"SUCCESS","returnValue":{"rows":[]}}]}',
    )

    assert result.ok
    assert result.status == AuraStatus.SUCCESS
    assert result.action_count == 1


def test_validate_aura_response_http_200_with_error_action_is_error():
    result = validate_aura_response(
        200,
        '{"actions":[{"id":"1;a","state":"ERROR","error":[{"message":"sanitized failure"}]}]}',
    )

    assert not result.ok
    assert result.status == AuraStatus.ERROR
    assert result.action_states == ("ERROR",)
    assert result.errors == ("sanitized failure",)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("not json", AuraStatus.MALFORMED_JSON),
        ('{"context":{"mode":"dev"}}', AuraStatus.MISSING_ACTIONS),
        (
            '<html><form action="/login"><input name="username"><input type="password" name="pw">AUTENTIFIC</form></html>',
            AuraStatus.LOGIN_PAGE,
        ),
    ],
)
def test_validate_aura_response_detects_non_success_semantics(body, expected):
    assert validate_aura_response(200, body).status == expected


def test_validate_aura_response_non_200_is_http_error():
    result = validate_aura_response(503, '{"actions":[{"state":"SUCCESS"}]}')

    assert result.status == AuraStatus.HTTP_ERROR
    assert result.errors == ("http_status=503",)


def test_classify_visualforce_partial_response_detects_success_marker():
    body = """
    <partial-response>
      <changes>
        <update id="panel"><![CDATA[<span data-export-state="ready">OK</span>]]></update>
      </changes>
    </partial-response>
    """

    result = classify_visualforce_partial_response(body, success_marker='data-export-state="ready"')

    assert result.ok
    assert result.status == VisualforcePartialStatus.SEMANTIC_SUCCESS
    assert result.update_count == 1
    assert result.update_ids == ("panel",)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            '<html><form action="/login"><input name="username"><input type="password" name="pw">AUTENTIFIC</form></html>',
            VisualforcePartialStatus.LOGIN_PAGE,
        ),
        ("<html>Introduceti cod de verificare pentru continuare</html>", VisualforcePartialStatus.CAPTCHA_MFA),
        (
            "<partial-response><error><error-message>View state could not be restored</error-message></error></partial-response>",
            VisualforcePartialStatus.VIEWSTATE_ERROR,
        ),
        ("<partial-response><changes></changes></partial-response>", VisualforcePartialStatus.EMPTY_RENDER),
        ("<partial-response><changes><update id='panel'>", VisualforcePartialStatus.MALFORMED_XML),
    ],
)
def test_classify_visualforce_partial_response_detects_failures(body, expected):
    assert classify_visualforce_partial_response(body, success_marker="READY").status == expected


def test_classify_visualforce_partial_response_reports_partial_render_without_marker():
    body = "<partial-response><changes><update id='panel'>Rendered but not complete</update></changes></partial-response>"

    result = classify_visualforce_partial_response(body)

    assert result.status == VisualforcePartialStatus.PARTIAL_RENDER
    assert result.update_count == 1


def test_classify_visualforce_partial_response_requires_caller_success_marker_when_supplied():
    body = "<partial-response><changes><update id='panel'>Rendered but not complete</update></changes></partial-response>"

    result = classify_visualforce_partial_response(body, success_marker=re.compile(r"download-ready"))

    assert result.status == VisualforcePartialStatus.SEMANTIC_MARKER_MISSING
    assert result.update_count == 1
