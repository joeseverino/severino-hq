"""Automation procedures an extension supplies: each one ``OperationManifest``.

An extension declares the procedures HQ may run, with the reviewed revision,
targets, privilege and recovery of each, in ``PluginIntegration.operations``;
the host names no procedure, repository or host of its own. What each field
holds: docs/Architecture.md, Operation manifests.
"""

from hq.platform.application.automation_manifest import OperationManifest

__all__ = ["OperationManifest"]
