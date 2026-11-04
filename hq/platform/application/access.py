"""Who may use an operation a door offers, decided here so every door asks one rule.

A door offers an operation only to a caller its access ``admits``, and refuses
a call by ``check``, which raises the ``AuthorizationError`` the operation's
own read would, so what a door lists and what it refuses never disagree.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .security import AuthorizationError, Capability, Principal, require_all
lazy from . import capabilities, resources
lazy from .integration_specs import ResourceSpec


class Access(Protocol):
    def admits(self, principal: Principal, /) -> bool:
        """Whether a door lists the operation to this caller."""

    def check(self, principal: Principal, /) -> None:
        """Raise the operation's own ``AuthorizationError`` for a caller it does not admit."""


@dataclass(frozen=True, slots=True)
class Anyone:
    """Every caller: a catalog, which says ``permitted`` of each entry instead."""

    def admits(self, _principal: Principal, /) -> bool:
        return True

    def check(self, _principal: Principal, /) -> None:
        return None


@dataclass(frozen=True, slots=True)
class Holding:
    """A caller holding every one of ``capabilities``, as ``Principal.require`` reads them."""

    capabilities: tuple[Capability | str, ...]

    def admits(self, principal: Principal) -> bool:
        return principal.permits(*self.capabilities)

    def check(self, principal: Principal) -> None:
        require_all(principal, self.capabilities)


@dataclass(frozen=True, slots=True)
class Some:
    """A caller who may use some entry of a family, each entry by its own rule."""

    offered: Callable[[Principal], bool]
    # What the refusal says the caller may not do, after naming it.
    lacks: str

    def admits(self, principal: Principal) -> bool:
        return self.offered(principal)

    def check(self, principal: Principal) -> None:
        if not self.offered(principal):
            raise AuthorizationError(f"{principal.interface} principal {principal.actor!r} {self.lacks}.")


ANYONE = Anyone()


def holding(*held: Capability | str) -> Holding:
    return Holding(held)


def _runs_a_capability(principal: Principal) -> bool:
    # A change is held for a person only once its caller holds its grant
    # (run_capability authorizes before it asks the policy), so a caller who
    # could hold a grant but does not has nothing to run or to ask for.
    return any(principal.permits(*spec.required_capabilities) for spec in capabilities.capability_registry().values())


def _reads_a_kind(serves: Callable[[ResourceSpec], bool]) -> Callable[[Principal], bool]:
    def offered(principal: Principal) -> bool:
        kinds = resources.resource_registry().values()
        return any(serves(spec) and resources.readable(spec, principal) for spec in kinds)

    return offered


RUNS_A_CAPABILITY = Some(_runs_a_capability, "may run no capability")
READS_A_LIST = Some(
    _reads_a_kind(lambda spec: bool(spec.list_handler and spec.list_query_type)), "may list no kind of record"
)
READS_A_RECORD = Some(
    _reads_a_kind(lambda spec: bool(spec.detail_handler and spec.identifier)), "may read no kind of record"
)
