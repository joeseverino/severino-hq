"""The supported, versioned import surface for HQ extensions.

An extension imports from the ``hq_sdk`` modules, never from HQ's ``application``
or ``core`` packages, so the host can change its internals without moving every
extension with it. The contract, and which module answers which need:
docs/Plugins.md. Its version is ``hq_sdk.plugin.PLUGIN_API_VERSION``.
"""
