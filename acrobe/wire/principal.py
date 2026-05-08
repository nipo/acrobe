"""Principal and Scope — identity and capability value objects.

Threaded through every entry point that may eventually be subject
to authn/authz. The default implementation is anonymous + all-scopes;
real auth backends construct narrower instances.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Principal:
    """Identity of the caller. Anonymous by default."""

    name: str = "anonymous"
    attributes: tuple = field(default_factory=tuple)

    @classmethod
    def anonymous(cls) -> "Principal":
        return cls()

    @property
    def is_anonymous(self) -> bool:
        return self.name == "anonymous"


@dataclass(frozen=True, slots=True)
class Scope:
    """Set of capabilities granted to a Principal for an operation.

    `all` is the wildcard scope used when no auth backend constrains
    the request. Constrained scopes are tuples of named capabilities
    interpreted by the AuthBackend.
    """

    capabilities: tuple = ()
    unrestricted: bool = False

    @classmethod
    def all(cls) -> "Scope":
        return cls(unrestricted=True)

    def grants(self, capability: str) -> bool:
        return self.unrestricted or capability in self.capabilities
