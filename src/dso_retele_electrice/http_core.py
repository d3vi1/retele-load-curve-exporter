from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from enum import StrEnum
from html.parser import HTMLParser
from typing import Callable, Mapping, Pattern


class SessionState(StrEnum):
    UNAUTHENTICATED = "unauthenticated"
    LOGIN_PAGE_FETCHED = "login_page_fetched"
    CREDENTIALS_POSTED = "credentials_posted"
    FRONTDOOR_SESSION_ESTABLISHED = "frontdoor_session_established"
    ROUTE_BOOTSTRAPPED = "route_bootstrapped"
    AURA_READY = "aura_ready"
    VISUALFORCE_READY = "visualforce_ready"
    EXPIRED_RELOGIN_REQUIRED = "expired_relogin_required"
    BLOCKED_CAPTCHA_MFA = "blocked_captcha_mfa"
    FAILED_TERMINAL = "failed_terminal"


class AuraStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    LOGIN_PAGE = "login_page"
    MALFORMED_JSON = "malformed_json"
    MISSING_ACTIONS = "missing_actions"
    HTTP_ERROR = "http_error"


class VisualforcePartialStatus(StrEnum):
    SEMANTIC_SUCCESS = "semantic_success"
    LOGIN_PAGE = "login_page"
    CAPTCHA_MFA = "captcha_mfa"
    VIEWSTATE_ERROR = "viewstate_error"
    EMPTY_RENDER = "empty_render"
    PARTIAL_RENDER = "partial_render"
    SEMANTIC_MARKER_MISSING = "semantic_marker_missing"
    MALFORMED_XML = "malformed_xml"


@dataclass(frozen=True)
class VisualforceHiddenFields:
    """Hidden form fields needed for a subsequent Visualforce POST.

    `fields` preserves all hidden inputs by their HTML name. The convenience
    attributes expose the Salesforce ViewState fields used by Visualforce.
    """

    fields: Mapping[str, str]
    view_state: str = ""
    view_state_version: str = ""
    view_state_mac: str = ""
    view_state_csrf: str = ""


@dataclass(frozen=True)
class AuraValidationResult:
    status: AuraStatus
    action_count: int = 0
    action_states: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == AuraStatus.SUCCESS


@dataclass(frozen=True)
class VisualforcePartialResult:
    status: VisualforcePartialStatus
    update_count: int = 0
    update_ids: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == VisualforcePartialStatus.SEMANTIC_SUCCESS


class _HiddenInputParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "input":
            return
        attr_map = {name.casefold(): value or "" for name, value in attrs}
        if attr_map.get("type", "").casefold() != "hidden":
            return
        name = attr_map.get("name") or attr_map.get("id")
        if not name:
            return
        self.fields[name] = attr_map.get("value", "")


def parse_visualforce_hidden_fields(html: str) -> VisualforceHiddenFields:
    """Parse hidden Visualforce form inputs without exposing response content."""

    parser = _HiddenInputParser()
    parser.feed(html or "")
    fields = dict(parser.fields)
    return VisualforceHiddenFields(
        fields=fields,
        view_state=_first_field(fields, "ViewState"),
        view_state_version=_first_field(fields, "ViewStateVersion"),
        view_state_mac=_first_field(fields, "ViewStateMAC"),
        view_state_csrf=_first_field(fields, "ViewStateCSRF"),
    )


def validate_aura_response(status_code: int, body: str) -> AuraValidationResult:
    """Classify an Aura response by semantics, not only HTTP status."""

    if _looks_like_login_page(body):
        return AuraValidationResult(AuraStatus.LOGIN_PAGE)
    if status_code != 200:
        return AuraValidationResult(AuraStatus.HTTP_ERROR, errors=(f"http_status={status_code}",))

    payload = _strip_aura_prefix(body or "")
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return AuraValidationResult(AuraStatus.MALFORMED_JSON)

    actions = parsed.get("actions") if isinstance(parsed, dict) else None
    if not isinstance(actions, list) or not actions:
        return AuraValidationResult(AuraStatus.MISSING_ACTIONS)

    states: list[str] = []
    errors: list[str] = []
    for action in actions:
        if not isinstance(action, dict):
            states.append("")
            errors.append("action_not_object")
            continue
        state = str(action.get("state", "")).upper()
        states.append(state)
        if state == "ERROR":
            errors.extend(_aura_error_messages(action.get("error")))
        elif state != "SUCCESS":
            errors.append(f"unexpected_state={state or 'missing'}")

    if all(state == "SUCCESS" for state in states):
        return AuraValidationResult(AuraStatus.SUCCESS, action_count=len(actions), action_states=tuple(states))
    return AuraValidationResult(
        AuraStatus.ERROR,
        action_count=len(actions),
        action_states=tuple(states),
        errors=tuple(errors),
    )


def classify_visualforce_partial_response(
    body: str,
    *,
    success_marker: str | Pattern[str] | Callable[[str], bool] | None = None,
) -> VisualforcePartialResult:
    """Classify a Visualforce partial-response body after a POST.

    If `success_marker` is provided, success requires that marker in the body;
    otherwise a non-empty partial render is reported as `PARTIAL_RENDER`.
    """

    text = body or ""
    if _looks_like_captcha_or_mfa(text):
        return VisualforcePartialResult(VisualforcePartialStatus.CAPTCHA_MFA)
    if _looks_like_login_page(text):
        return VisualforcePartialResult(VisualforcePartialStatus.LOGIN_PAGE)
    if _looks_like_viewstate_error(text):
        return VisualforcePartialResult(VisualforcePartialStatus.VIEWSTATE_ERROR)

    stripped = text.strip()
    if not stripped:
        return VisualforcePartialResult(VisualforcePartialStatus.EMPTY_RENDER)

    update_count = 0
    update_ids: tuple[str, ...] = ()
    xml_errors: tuple[str, ...] = ()
    if stripped.startswith("<"):
        try:
            update_count, update_ids = _partial_update_summary(stripped)
        except ET.ParseError:
            if "<partial-response" in stripped:
                return VisualforcePartialResult(VisualforcePartialStatus.MALFORMED_XML)
            xml_errors = ("not_partial_xml",)

    if update_count == 0 and "<partial-response" in stripped:
        return VisualforcePartialResult(VisualforcePartialStatus.EMPTY_RENDER)

    if success_marker is not None:
        if _marker_matches(success_marker, text):
            return VisualforcePartialResult(
                VisualforcePartialStatus.SEMANTIC_SUCCESS,
                update_count=update_count,
                update_ids=update_ids,
            )
        return VisualforcePartialResult(
            VisualforcePartialStatus.SEMANTIC_MARKER_MISSING,
            update_count=update_count,
            update_ids=update_ids,
            errors=xml_errors,
        )

    return VisualforcePartialResult(
        VisualforcePartialStatus.PARTIAL_RENDER,
        update_count=update_count,
        update_ids=update_ids,
        errors=xml_errors,
    )


def _first_field(fields: Mapping[str, str], suffix: str) -> str:
    for key, value in fields.items():
        if key == suffix or key.endswith(f".{suffix}"):
            return value
    return ""


def _strip_aura_prefix(body: str) -> str:
    text = body.strip()
    if text.startswith("for(;;);"):
        return text[len("for(;;);") :].strip()
    return text


def _aura_error_messages(raw_errors: object) -> list[str]:
    if not isinstance(raw_errors, list):
        return []
    messages: list[str] = []
    for item in raw_errors:
        if isinstance(item, dict) and item.get("message"):
            messages.append(str(item["message"]))
        elif item:
            messages.append(str(item))
    return messages


def _partial_update_summary(text: str) -> tuple[int, tuple[str, ...]]:
    root = ET.fromstring(text)
    updates = [element for element in root.iter() if _local_name(element.tag) == "update"]
    non_empty_updates = [element for element in updates if "".join(element.itertext()).strip()]
    update_ids = tuple(str(element.attrib.get("id", "")) for element in non_empty_updates)
    return len(non_empty_updates), update_ids


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _marker_matches(marker: str | Pattern[str] | Callable[[str], bool], text: str) -> bool:
    if callable(marker):
        return bool(marker(text))
    if isinstance(marker, str):
        return marker in text
    return marker.search(text) is not None


_ACCENT_TRANSLATION = str.maketrans({"ă": "a", "â": "a", "î": "i", "ș": "s", "ş": "s", "ț": "t", "ţ": "t"})


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\u00a0", " ")).casefold().translate(_ACCENT_TRANSLATION)


def _looks_like_login_page(body: str) -> bool:
    text = _normalized_text(body)
    has_password = any(token in text for token in ("password", "parola"))
    has_user = any(token in text for token in ("username", "utilizator", "email"))
    return (
        ("login.salesforce.com" in text or "/login" in text or "autentific" in text)
        and has_password
        and has_user
    )


def _looks_like_captcha_or_mfa(body: str) -> bool:
    text = _normalized_text(body)
    return any(
        token in text
        for token in (
            "captcha",
            "recaptcha",
            "cod de verificare",
            "verification code",
            "multi-factor",
            "multifactor",
            "two-factor",
            "mfa",
            "authenticator",
        )
    )


def _looks_like_viewstate_error(body: str) -> bool:
    text = _normalized_text(body)
    return any(
        token in text
        for token in (
            "viewstate",
            "view state",
            "javax.faces.viewstate",
            "visualforce view state",
        )
    ) and any(token in text for token in ("expired", "invalid", "could not be restored", "mac", "csrf", "error"))
