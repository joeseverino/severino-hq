"""The supported, versioned import surface for trusted HQ plugins.

Plugins should import from ``hq_sdk`` modules, never from HQ's ``application``
or ``core`` implementation packages. The facade is intentionally thin: HQ can
refactor its internals without forcing every private plugin to move in lockstep.
Its version is ``hq_sdk.plugin.PLUGIN_API_VERSION``.
"""
