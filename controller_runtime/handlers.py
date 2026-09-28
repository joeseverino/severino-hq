"""What the controller registers against a kind or a connection.

Every handler registers itself beside its own definition: ``@reads`` for a
reading, ``@lists`` for a resource kind's inventory, ``@acts`` for an action on
a kind, ``@probes`` for a connection provider. An admitted adapter's readers,
inventory, probes and actions are registered by ``_register_adapters``. The
dispatch tables in ``providers`` are these, and nothing lists a handler twice.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from control_plane.provider_adapters.contracts import ProviderResult


_Handler = TypeVar("_Handler", bound=Callable[..., Any])


def _register(table: dict[Any, Any], key: Any, what: str) -> Callable[[_Handler], _Handler]:
    def register(handler: _Handler) -> _Handler:
        if key in table:
            raise ValueError(f"Duplicate {what} for {key!r}.")
        table[key] = handler
        return handler

    return register


# ----- Readings ---------------------------------------------------------------
#
# A reader for a kind in control_plane.observations is registered one of two
# ways: an integration's adapter declares it (``readings=``) and
# ``_register_adapters`` admits it, or a core reader carries @reads beside its
# own definition. ``test_reader_registration`` holds it so.

OBSERVATION_READERS: dict[str, Callable[[], list[dict[str, Any]]]] = {}


def reads(kind: str) -> Callable[[_Handler], _Handler]:
    return _register(OBSERVATION_READERS, kind, "reader")


# ----- Resources --------------------------------------------------------------

# What each resource kind's provider holds, whether or not HQ declared it.
INVENTORY: dict[str, Callable[[], list[dict[str, Any]]]] = {}

# ``(kind, action) -> handler``. Only actions the registry says the controller
# may apply; a locked action is refused by ``providers``, never registered.
ACTIONS: dict[tuple[str, str], Callable[..., ProviderResult]] = {}

# ``provider -> probe``: whether a connection still works, and what it reaches.
PROBES: dict[str, Callable[[str], dict[str, Any]]] = {}


def lists(kind: str) -> Callable[[_Handler], _Handler]:
    return _register(INVENTORY, kind, "inventory reader")


def acts(kind: str, action: str) -> Callable[[_Handler], _Handler]:
    return _register(ACTIONS, (kind, action), "controller action")


def probes(provider: str) -> Callable[[_Handler], _Handler]:
    return _register(PROBES, provider, "connection probe")
