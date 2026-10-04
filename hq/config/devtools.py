"""Development-only layers. Off unless asked for, and never in the host image.

The Django Debug Toolbar is installed from `the dev dependency group`, which the
image never installs, so production cannot import it. The settings enable it
only when all four hold: DEBUG is on, `SEVERINO_DEBUG_TOOLBAR` is set, the
suite is not running, and the package is importable.
"""

from __future__ import annotations

import importlib.util

DEBUG_TOOLBAR_APP = "debug_toolbar"
DEBUG_TOOLBAR_MIDDLEWARE = "debug_toolbar.middleware.DebugToolbarMiddleware"

# The toolbar renders its panels by assigning fetched HTML to `innerHTML`,
# which Trusted Types refuses. These directives are dropped while it is on;
# every other directive, the nonce included, stays as production sends it.
TRUSTED_TYPES_DIRECTIVES = frozenset({"require-trusted-types-for", "trusted-types"})


def debug_toolbar_enabled(*, debug: bool, requested: bool, testing: bool) -> bool:
    """Whether the settings may install the toolbar."""

    if not (debug and requested) or testing:
        return False
    return importlib.util.find_spec(DEBUG_TOOLBAR_APP) is not None


def without_trusted_types(policy: dict[str, list[str]]) -> dict[str, list[str]]:
    """The policy minus the Trusted Types directives."""

    return {
        key: value
        for key, value in policy.items()
        if key not in TRUSTED_TYPES_DIRECTIVES
    }
