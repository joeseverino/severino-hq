"""An explicit integration graph in place of the composed one, for a test."""

from contextvars import Token

from .integrations import _GRAPH_OVERRIDE, IntegrationGraph


def override_integration_graph(graph: IntegrationGraph) -> Token[IntegrationGraph | None]:
    """``graph`` answers ``integration_graph()`` inside ``with override_integration_graph(graph):``."""

    return _GRAPH_OVERRIDE.set(graph)
