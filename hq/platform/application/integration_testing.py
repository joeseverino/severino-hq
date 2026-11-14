"""An explicit integration graph, or set of commands, in place of the composed one, for a test."""

from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
from contextvars import Token
from unittest import mock

from .integration_specs import CapabilitySpec
from .integrations import _GRAPH_OVERRIDE, IntegrationGraph


def override_integration_graph(graph: IntegrationGraph) -> Token[IntegrationGraph | None]:
    """``graph`` answers ``integration_graph()`` inside ``with override_integration_graph(graph):``."""

    return _GRAPH_OVERRIDE.set(graph)


@contextmanager
def commands_registered(registry: Mapping[str, CapabilitySpec]):
    """``registry`` answers every read of the commands inside the block: the
    whole (``capability_registry``) and one by name (``capability_spec``,
    which ``command_spec`` and the words read through)."""

    commands = dict(registry)
    with ExitStack() as stack:
        stack.enter_context(
            mock.patch("hq.platform.application.capabilities.capability_registry", return_value=commands)
        )
        stack.enter_context(
            mock.patch("hq.platform.application.capabilities.capability_spec", side_effect=commands.get)
        )
        yield
