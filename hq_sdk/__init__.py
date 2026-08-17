"""The supported, versioned import surface for trusted HQ plugins.

Plugins should import from ``hq_sdk`` modules, never from HQ's ``application``
or ``core`` implementation packages. The facade is intentionally thin: HQ can
refactor its internals without forcing every private plugin to move in lockstep.
"""

SDK_VERSION = 1

__all__ = ["SDK_VERSION"]
