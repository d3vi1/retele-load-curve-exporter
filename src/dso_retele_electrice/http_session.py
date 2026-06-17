from __future__ import annotations

from dataclasses import dataclass, field

from .http_core import SessionState


ALLOWED_TRANSITIONS: dict[SessionState, frozenset[SessionState]] = {
    SessionState.UNAUTHENTICATED: frozenset(
        {
            SessionState.LOGIN_PAGE_FETCHED,
            SessionState.BLOCKED_CAPTCHA_MFA,
            SessionState.FAILED_TERMINAL,
        }
    ),
    SessionState.LOGIN_PAGE_FETCHED: frozenset(
        {
            SessionState.CREDENTIALS_POSTED,
            SessionState.BLOCKED_CAPTCHA_MFA,
            SessionState.FAILED_TERMINAL,
        }
    ),
    SessionState.CREDENTIALS_POSTED: frozenset(
        {
            SessionState.FRONTDOOR_SESSION_ESTABLISHED,
            SessionState.BLOCKED_CAPTCHA_MFA,
            SessionState.FAILED_TERMINAL,
        }
    ),
    SessionState.FRONTDOOR_SESSION_ESTABLISHED: frozenset(
        {
            SessionState.ROUTE_BOOTSTRAPPED,
            SessionState.EXPIRED_RELOGIN_REQUIRED,
            SessionState.BLOCKED_CAPTCHA_MFA,
            SessionState.FAILED_TERMINAL,
        }
    ),
    SessionState.ROUTE_BOOTSTRAPPED: frozenset(
        {
            SessionState.AURA_READY,
            SessionState.VISUALFORCE_READY,
            SessionState.EXPIRED_RELOGIN_REQUIRED,
            SessionState.BLOCKED_CAPTCHA_MFA,
            SessionState.FAILED_TERMINAL,
        }
    ),
    SessionState.AURA_READY: frozenset(
        {
            SessionState.VISUALFORCE_READY,
            SessionState.EXPIRED_RELOGIN_REQUIRED,
            SessionState.BLOCKED_CAPTCHA_MFA,
            SessionState.FAILED_TERMINAL,
        }
    ),
    SessionState.VISUALFORCE_READY: frozenset(
        {
            SessionState.AURA_READY,
            SessionState.EXPIRED_RELOGIN_REQUIRED,
            SessionState.BLOCKED_CAPTCHA_MFA,
            SessionState.FAILED_TERMINAL,
        }
    ),
    SessionState.EXPIRED_RELOGIN_REQUIRED: frozenset(
        {
            SessionState.LOGIN_PAGE_FETCHED,
            SessionState.FAILED_TERMINAL,
        }
    ),
    SessionState.BLOCKED_CAPTCHA_MFA: frozenset(),
    SessionState.FAILED_TERMINAL: frozenset(),
}


@dataclass(frozen=True)
class StateTransition:
    from_state: SessionState
    to_state: SessionState
    note: str = ""


@dataclass
class HttpSessionStateMachine:
    """Small state gate for a Salesforce/Visualforce HTTP login sequence.

    The object stores only state names and caller-supplied notes. Do not pass
    credentials, cookies, headers, or response bodies as transition notes.
    """

    state: SessionState = SessionState.UNAUTHENTICATED
    history: list[StateTransition] = field(default_factory=list)

    def can_transition_to(self, next_state: SessionState) -> bool:
        return next_state in ALLOWED_TRANSITIONS[self.state]

    def transition(self, next_state: SessionState, *, note: str = "") -> None:
        if not self.can_transition_to(next_state):
            raise ValueError(f"Invalid HTTP session transition: {self.state.value} -> {next_state.value}")
        self.history.append(StateTransition(self.state, next_state, note))
        self.state = next_state

    def require(self, expected: SessionState) -> None:
        if self.state != expected:
            raise RuntimeError(f"Expected HTTP session state {expected.value}, got {self.state.value}")

    @property
    def terminal(self) -> bool:
        return self.state in {SessionState.BLOCKED_CAPTCHA_MFA, SessionState.FAILED_TERMINAL}
