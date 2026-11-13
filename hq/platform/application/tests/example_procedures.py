"""The example plugin's automation procedures, installed for a block as an extension supplying them would be."""

from contextlib import AbstractContextManager
from dataclasses import replace

from tests.fixtures.example_hq_plugin.operations import operations

from ..plugin_testing import installed, sibling


def supplied() -> AbstractContextManager[None]:
    manifest, integration = sibling(identifier="example.procedures", name="Procedures")
    return installed((manifest, replace(integration, operations=operations)))
