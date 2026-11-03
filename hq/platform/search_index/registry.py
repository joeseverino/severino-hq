"""Search projection definitions for HQ record types, read from the composition at each call."""

from collections.abc import Mapping

from hq.platform.application.integrations import integration_graph
from hq.platform.application.search_contracts import SearchDefinition


def by_model() -> Mapping[type, SearchDefinition]:
    return integration_graph().search_models


def by_scope() -> Mapping[str, SearchDefinition]:
    return integration_graph().search
