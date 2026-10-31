"""The host as it runs with no extension installed, for tests that count what the host draws."""

from contextlib import AbstractContextManager

from ..plugin_testing import installed


def host_only() -> AbstractContextManager[None]:
    return installed(alone=True)
