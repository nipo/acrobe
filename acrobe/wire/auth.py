"""Auth backend interface and the open (no-auth) implementation.

Threaded through REST and WS handlers as a constructor argument.
Code anywhere that needs to resolve identity, authorize an action,
or mint/validate a connect token calls into the backend rather than
branching on "is auth on?".
"""

from abc import ABC, abstractmethod

from .principal import Principal, Scope


class AuthBackend(ABC):
    """Interface every auth backend implements.

    extract_principal:    inspect an HTTP request, decide who is calling.
    authorize:            decide whether `principal` may perform `action`
                          on `node` within `scope`.
    issue_connect_token:  produce the bearer token embedded in the WS
                          connect URL emitted by REST enumeration.
    validate_connect_token: verify a token presented at WS upgrade and
                          return the (Principal, Scope) it carries.
    """

    @abstractmethod
    def extract_principal(self, http_request) -> Principal: ...

    @abstractmethod
    def authorize(self, principal: Principal, node, action: str,
                  scope: Scope) -> bool: ...

    @abstractmethod
    def issue_connect_token(self, principal: Principal, node,
                            scope: Scope) -> str: ...

    @abstractmethod
    def validate_connect_token(self, token: str,
                               node) -> tuple[Principal, Scope]: ...


class OpenAuthBackend(AuthBackend):
    """No-auth backend used for LAN-friendly default deployments.

    Every request is anonymous, every action authorized, the token
    is the empty string, and any presented token resolves to the
    anonymous principal with the all-scope.
    """

    def extract_principal(self, http_request) -> Principal:
        return Principal.anonymous()

    def authorize(self, principal: Principal, node, action: str,
                  scope: Scope) -> bool:
        return True

    def issue_connect_token(self, principal: Principal, node,
                            scope: Scope) -> str:
        return ""

    def validate_connect_token(self, token: str,
                               node) -> tuple[Principal, Scope]:
        return Principal.anonymous(), Scope.all()


def audit_log(principal: Principal, node, action: str, outcome: str,
              **extra) -> None:
    """No-op audit hook. Real backends replace this; existing call sites
    pre-route the relevant fields so the swap is mechanical.
    """
    return None
