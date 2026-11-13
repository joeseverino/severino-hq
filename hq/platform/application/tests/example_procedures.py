"""The example plugin's automation procedures, installed for a block as an extension supplying them would be."""

from contextlib import AbstractContextManager
from dataclasses import replace

from tests.fixtures.example_hq_plugin.operations import operations

from ..plugin_testing import installed, sibling


def supplied(*, alone: bool = False) -> AbstractContextManager[None]:
    """With ``alone``, the procedures are the whole installed set, as ``host_only`` is none."""

    manifest, integration = sibling(identifier="example.procedures", name="Procedures")
    return installed((manifest, replace(integration, operations=operations)), alone=alone)
