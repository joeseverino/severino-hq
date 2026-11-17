"""An explicit integration graph, or set of commands, in place of the composed one, for a test."""

from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
from contextvars import Token
from unittest import mock

from . import capabilities
from .integration_specs import CapabilitySpec
from .integrations import _GRAPH_OVERRIDE, IntegrationGraph


def override_integration_graph(graph: IntegrationGraph) -> Token[IntegrationGraph | None]:
    """``graph`` answers ``integration_graph()`` inside ``with override_integration_graph(graph):``."""

    return _GRAPH_OVERRIDE.set(graph)


@contextmanager
def commands_registered(registry: Mapping[str, CapabilitySpec]):
    """``registry`` is the whole set of commands inside the block
    (``capability_registry``), and answers first for one by name
    (``capability_spec``, which ``command_spec`` and the words read through).

    A name it leaves out still finds HQ's own command, since what HQ does on
    its own (queuing a reconcile after a save) words itself from that command.
    """

    commands = dict(registry)
    lookup = capabilities.capability_spec
    with ExitStack() as stack:
        stack.enter_context(
            mock.patch("hq.platform.application.capabilities.capability_registry", return_value=commands)
        )
        stack.enter_context(
            mock.patch(
                "hq.platform.application.capabilities.capability_spec",
                side_effect=lambda name: commands.get(name) or lookup(name),
            )
        )
        yield
