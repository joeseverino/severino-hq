"""Portainer's readings, declared through the one adapter mechanism.

Its resource kinds (stacks and containers), their actions and the connection
probe are still the controller core's, so the readings go through the
``portainer`` connection those declare.
"""

from __future__ import annotations

from .contracts import ControllerIntegrationAdapter
from .portainer_readings import PROVIDER, READINGS


def build_adapter() -> ControllerIntegrationAdapter:
    return ControllerIntegrationAdapter(
        definitions=(),
        inventory={},
        connection_probes={},
        actions={},
        readings=READINGS,
        reads_through=(PROVIDER,),
    )
